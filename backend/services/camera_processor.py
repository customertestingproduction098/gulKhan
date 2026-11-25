"""RTSP frame acquisition and the real-time multi-camera surveillance pipeline.

Two cooperating pieces live here:

* ``FrameGrabber`` keeps a single low-latency RTSP connection per channel,
  continuously draining network packets via ``cap.grab()`` so frames are never
  delayed in FFmpeg/TCP buffers, and exposes the latest live frame.
* ``SurveillanceProcessor`` monitors ALL enabled cameras in parallel (one worker
  thread per camera channel), runs instant motion-triggered + periodic person
  detection via AWS Rekognition, tracks active occupants per channel, and
  dispatches non-blocking alerts only on positive entry deltas.
"""

from __future__ import annotations

from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor, wait as wait_futures
from dataclasses import dataclass, field
from datetime import datetime
import logging
import os
from pathlib import Path
import threading
import time
from typing import Any

# Configure FFmpeg RTSP options for crisp Full-HD Main Streams without dropping B/P-frame slices
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = (
    "rtsp_transport;tcp"
    "|max_delay;500000"
    "|fflags;nobuffer"
    "|flags;low_delay"
)

import cv2
import numpy as np

from config.settings import Settings, get_settings
from models import Entry, Interaction
from models.interaction import TYPE_MISSED, TYPE_SUCCESSFUL, TYPE_UNATTENDED
from services.alert_system import AlertSystem
from services.aws_rekognition import (
    ENTRY_MIN_CONFIDENCE,
    OCCUPANT_MIN_CONFIDENCE,
    get_vision_service,
)
from services.cloudinary_storage import get_cloudinary_storage
from services.events import EventBroker
from services.mongodb_handler import DataStoreError, MongoHandler
from services.openrouter import get_openrouter_service
from services.settings_service import SettingsService
from utils.helpers import (
    is_after_hours,
    serialize_doc,
    to_iso,
    utcnow,
)

logger = logging.getLogger(__name__)

MODE_SHOP = "shop"
MODE_WAREHOUSE = "warehouse"
VALID_MODES = {MODE_SHOP, MODE_WAREHOUSE}

SHOP_ABSENCE_GRACE_SECONDS = 2.5
WAREHOUSE_ABSENCE_GRACE_SECONDS = 8.0
FAST_POLL_SECONDS = 0.05
MOTION_MIN_INTERVAL_SECONDS = 0.25
WAREHOUSE_OCCUPIED_MOTION_INTERVAL_SECONDS = 4.0
WAREHOUSE_OCCUPIED_HEARTBEAT_SECONDS = 45.0
CONFIRMATION_FRAMES = 2
MOTION_PIXEL_THRESHOLD = 20
MOTION_RATIO_THRESHOLD = 0.004
BBOX_MOTION_PIXEL_THRESHOLD = 18
BBOX_MOTION_RATIO_THRESHOLD = 0.08
OCCUPANT_BOX_IOU_THRESHOLD = 0.30
REKOGNITION_MAX_IMAGE_BYTES = int(4.5 * 1024 * 1024)


@dataclass
class _ChannelMonitorState:
    """Independent per-camera monitoring state."""

    channel: int
    stop_event: threading.Event = field(default_factory=threading.Event)
    thread: threading.Thread | None = None
    person_count: int = 0
    customer_count: int = 0
    shopkeeper_present: bool = True
    unattended_active: bool = False
    last_analysis_at: datetime | None = None
    last_error: str | None = None
    last_result: dict[str, Any] | None = None

    # Shop state
    interaction: dict[str, Any] | None = None
    shop_cooldown_until: float = 0.0
    empty_since: float | None = None
    unattended_since: float | None = None
    last_unattended_alert: float = 0.0
    unattended_alerted: bool = False
    person_present_since: float | None = None
    staff_established: bool = False

    # Warehouse occupant state (strictly per-camera)
    active_occupants: int = 0
    occupant_absent_since: dict[int, float] = field(default_factory=dict)
    last_occupied_gray: Any = None
    last_occupied_boxes: list[dict[str, Any]] = field(default_factory=list)

    # 2-frame entry confirmation candidate (preserves exact frame 1 & timestamp)
    pending_entry_frame: Any = None
    pending_entry_at: datetime | None = None
    pending_entry_count: int = 0
    pending_entry_frames: int = 0
    pending_entry_confidence: float = 0.0
    pending_entry_persons: list[dict[str, Any]] = field(default_factory=list)
    pending_entry_labels: list[str] = field(default_factory=list)

    # Motion & clean static background state
    prev_gray: Any = None
    background_gray: Any = None
    last_had_motion: bool = False
    last_detection_mono: float = 0.0
    last_frame_revision: int = -1


class FrameGrabber:
    """Continuously read one RTSP channel at zero buffer lag and cache the latest frame."""

    def __init__(self, channel: int, settings: Settings) -> None:
        self.channel = channel
        self.settings = settings
        self._cap: cv2.VideoCapture | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._frame_cond = threading.Condition(self._lock)
        self._first_frame = threading.Event()
        self._jpeg: bytes | None = None
        self._frame = None
        self._last_frame_at: datetime | None = None
        self._last_access = time.monotonic()
        self._last_stream_access = 0.0
        self._error: str | None = None
        self._running = False
        self._frames = 0
        self._revision = 0
        self._started_at = time.monotonic()
        max_frames = max(10, int(self.settings.camera_fps * self.settings.frame_buffer_seconds))
        self._buffer: deque[tuple[float, bytes]] = deque(maxlen=max_frames)

    # -------------------------------------------------------------- lifecycle
    def start(self) -> "FrameGrabber":
        if self._running and self._thread and self._thread.is_alive():
            return self
        self._stop.clear()
        self._running = True
        self._thread = threading.Thread(
            target=self._capture_loop,
            name=f"camera-{self.channel}",
            daemon=True,
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        with self._frame_cond:
            self._frame_cond.notify_all()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)
        self._release()
        self._running = False

    def _release(self) -> None:
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:  # noqa: BLE001
                pass
            self._cap = None

    # ---------------------------------------------------------------- capture
    def _open(self) -> bool:
        try:
            url = self.settings.rtsp_url(self.channel)
        except ValueError as error:
            self._error = str(error)
            return False

        try:
            cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, self.settings.camera_open_timeout_ms)
            cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, self.settings.camera_read_timeout_ms)
            if not cap.isOpened():
                cap.release()
                self._error = f"Camera {self.channel} could not be opened"
                return False
            self._cap = cap
            self._error = None
            logger.info("Camera %s connected", self.channel)
            return True
        except Exception as error:  # noqa: BLE001
            self._error = f"Camera {self.channel} error: {error}"
            logger.warning(self._error)
            return False

    def _capture_loop(self) -> None:
        retry_delay = 3.0
        stream_decode_interval = 1.0 / max(1, min(self.settings.camera_fps, 25))
        monitor_decode_interval = 1.0 / min(max(1, self.settings.camera_fps), 8)
        last_decode = 0.0
        consecutive_fast_grabs = 0

        while not self._stop.is_set():
            if self._cap is None and not self._open():
                self._stop.wait(retry_delay)
                continue

            # Continuously grab every incoming RTSP packet without sleeping so
            # the network socket buffer never accumulates old/delayed frames.
            grab_start = time.monotonic()
            grabbed = self._cap.grab() if self._cap is not None else False
            grab_elapsed = time.monotonic() - grab_start
            if not grabbed:
                self._error = f"Camera {self.channel} stream interrupted"
                self._release()
                consecutive_fast_grabs = 0
                self._stop.wait(retry_delay)
                continue

            # If grab() returned almost instantaneously (<4ms), packets were queued
            # in FFmpeg/TCP receive buffers. Drain queued stale packets until we
            # reach the live edge before decoding.
            if grab_elapsed < 0.004 and self._frame is not None and consecutive_fast_grabs < 60:
                consecutive_fast_grabs += 1
                continue
            consecutive_fast_grabs = 0

            now = time.monotonic()
            viewer_active = (now - self._last_stream_access) < 5.0
            decode_interval = stream_decode_interval if viewer_active else monitor_decode_interval
            if (now - last_decode) < decode_interval and self._frame is not None:
                continue

            ret, frame = self._cap.retrieve() if self._cap is not None else (False, None)
            if not ret or frame is None:
                continue

            captured_at = utcnow()
            last_decode = now

            if self.settings.frame_width and frame.shape[1] > self.settings.frame_width:
                scale = self.settings.frame_width / frame.shape[1]
                frame = cv2.resize(
                    frame,
                    (self.settings.frame_width, int(frame.shape[0] * scale)),
                    interpolation=cv2.INTER_AREA,
                )

            jpeg: bytes | None = None
            if viewer_active:
                ok, buffer = cv2.imencode(
                    ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, self.settings.jpeg_quality]
                )
                if ok:
                    jpeg = buffer.tobytes()

            with self._frame_cond:
                self._frame = frame
                self._jpeg = jpeg
                self._last_frame_at = captured_at
                self._frames += 1
                self._revision += 1
                if jpeg is not None:
                    self._buffer.append((now, jpeg))
                self._first_frame.set()
                self._frame_cond.notify_all()

            self._error = None

        self._release()

    # ---------------------------------------------------------------- readers
    def wait_for_frame(self, timeout: float = 8.0) -> bool:
        return self._first_frame.wait(timeout=timeout) and self._frame is not None

    def read_jpeg(self, quality: int | None = None) -> bytes | None:
        now = time.monotonic()
        self._last_access = now
        self._last_stream_access = now
        with self._lock:
            if quality is None and self._jpeg is not None:
                return self._jpeg
            frame = None if self._frame is None else self._frame.copy()
        if frame is None:
            return None
        encode_quality = int(quality) if quality is not None else int(self.settings.jpeg_quality)
        ok, buffer = cv2.imencode(
            ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, encode_quality]
        )
        if not ok:
            return None
        jpeg = buffer.tobytes()
        if quality is None:
            with self._lock:
                if self._jpeg is None:
                    self._jpeg = jpeg
        return jpeg

    def read_frame(self):
        self._last_access = time.monotonic()
        with self._lock:
            return None if self._frame is None else self._frame.copy()

    def read_frame_with_revision(self) -> tuple[int, Any]:
        self._last_access = time.monotonic()
        with self._lock:
            frame = None if self._frame is None else self._frame.copy()
            return self._revision, frame

    def wait_for_new_frame(
        self,
        last_revision: int,
        timeout: float = 0.25,
    ) -> tuple[int, Any, datetime | None]:
        """Block until a frame newer than ``last_revision`` is available or timeout expires."""
        self._last_access = time.monotonic()
        with self._frame_cond:
            if (
                (self._frame is None or self._revision == last_revision)
                and not self._stop.is_set()
            ):
                self._frame_cond.wait(timeout=timeout)
            frame = None if self._frame is None else self._frame.copy()
            return self._revision, frame, self._last_frame_at

    def recent_jpegs(self, seconds: float = 5.0) -> list[bytes]:
        cutoff = time.monotonic() - max(0.5, seconds)
        with self._lock:
            return [jpeg for stamp, jpeg in self._buffer if stamp >= cutoff]

    def touch(self) -> None:
        self._last_access = time.monotonic()

    @property
    def revision(self) -> int:
        with self._lock:
            return self._revision

    @property
    def idle_seconds(self) -> float:
        return time.monotonic() - self._last_access

    def status(self) -> dict[str, Any]:
        elapsed = max(0.001, time.monotonic() - self._started_at)
        with self._lock:
            buffered = len(self._buffer)
        return {
            "channel": self.channel,
            "connected": self._frame is not None and self._last_frame_at is not None,
            "fps": round(self._frames / elapsed, 1),
            "last_frame_at": to_iso(self._last_frame_at),
            "buffered_frames": buffered,
            "error": self._error,
        }


class CameraManager:
    """Owns one ``FrameGrabber`` per channel."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._grabbers: dict[int, FrameGrabber] = {}
        self._lock = threading.Lock()
        self.protected_channels: set[int] = set()

    def get(self, channel: int, autostart: bool = True) -> FrameGrabber:
        with self._lock:
            grabber = self._grabbers.get(channel)
            if grabber is None:
                grabber = FrameGrabber(channel, self.settings)
                self._grabbers[channel] = grabber
            if autostart:
                grabber.start()
            return grabber

    def peek(self, channel: int) -> FrameGrabber | None:
        with self._lock:
            return self._grabbers.get(channel)

    def release(self, channel: int) -> None:
        with self._lock:
            if channel in self.protected_channels:
                return
            grabber = self._grabbers.pop(channel, None)
        if grabber:
            grabber.stop()

    def release_idle(self, max_idle_seconds: float = 300.0) -> None:
        with self._lock:
            idle = [
                channel
                for channel, grabber in self._grabbers.items()
                if channel not in self.protected_channels
                and grabber.idle_seconds > max_idle_seconds
            ]
        for channel in idle:
            self.release(channel)

    def status(self) -> list[dict[str, Any]]:
        with self._lock:
            grabbers = list(self._grabbers.values())
        return [grabber.status() for grabber in grabbers]

    def shutdown(self) -> None:
        with self._lock:
            self.protected_channels.clear()
            grabbers = list(self._grabbers.values())
            self._grabbers.clear()
        for grabber in grabbers:
            grabber.stop()


class SurveillanceProcessor:
    """Multi-camera background analysis manager for shop and warehouse security."""

    def __init__(
        self,
        store: MongoHandler,
        manager: CameraManager,
        alerts: AlertSystem,
        settings: Settings | None = None,
        owner_settings: SettingsService | None = None,
        events: EventBroker | None = None,
    ) -> None:
        self.store = store
        self.manager = manager
        self.alerts = alerts
        self.settings = settings or get_settings()
        self.owner_settings = owner_settings or SettingsService(store, self.settings, events)
        self.events = events
        self.vision = get_vision_service()
        self.openrouter = get_openrouter_service()
        self.cloudinary = get_cloudinary_storage()

        self._lock = threading.RLock()
        self._monotonic = time.monotonic
        self._channel_states: dict[int, _ChannelMonitorState] = {}
        self._dispatch_executor = ThreadPoolExecutor(
            max_workers=8,
            thread_name_prefix="warehouse-alert",
        )
        self._dispatch_lock = threading.Lock()
        self._pending_dispatches: set[Future] = set()

        self.channel: int | None = None
        self.mode: str | None = None
        self.started_at: datetime | None = None
        self.last_analysis_at: datetime | None = None
        self.last_error: str | None = None
        self.last_result: dict[str, Any] | None = None
        self.person_count = 0
        self.customer_count = 0
        self.shopkeeper_present = True
        self.unattended_active = False

    # -------------------------------------------------------------- lifecycle
    @property
    def monitoring(self) -> bool:
        with self._lock:
            return any(
                state.thread is not None and state.thread.is_alive()
                for state in self._channel_states.values()
            )

    @property
    def active_channels(self) -> list[int]:
        with self._lock:
            return sorted(
                ch
                for ch, state in self._channel_states.items()
                if state.thread is not None and state.thread.is_alive()
            )

    def _enabled_allowed_channels(self) -> list[int]:
        channels = [
            int(ch)
            for ch in self.settings.allowed_cameras
            if self.owner_settings.camera_enabled(int(ch))
        ]
        if not channels:
            channels = [int(ch) for ch in self.settings.allowed_cameras]
        return sorted(set(channels))

    def start(self, channel: int | None = None, mode: str = MODE_WAREHOUSE) -> dict[str, Any]:
        """Start monitoring across ALL available/enabled cameras simultaneously."""
        if mode not in VALID_MODES:
            raise ValueError(f"Unsupported mode '{mode}'")

        target_channels = self._enabled_allowed_channels()
        if channel is not None and int(channel) in self.settings.allowed_cameras:
            primary_channel = int(channel)
            if primary_channel not in target_channels:
                target_channels.append(primary_channel)
                target_channels.sort()
        else:
            primary_channel = target_channels[0] if target_channels else 2

        with self._lock:
            # If switching mode, stop previous mode threads cleanly
            if self.monitoring and self.mode != mode:
                self.stop()

            self.channel = primary_channel
            self.mode = mode
            if self.started_at is None or not self.monitoring:
                self.started_at = utcnow()
            self.last_error = None

            self.manager.protected_channels = set(target_channels)

            started_now: list[int] = []
            for ch in target_channels:
                existing = self._channel_states.get(ch)
                if existing and existing.thread and existing.thread.is_alive():
                    continue

                state = _ChannelMonitorState(channel=ch)
                self._channel_states[ch] = state
                self.manager.get(ch, autostart=True)
                thread = threading.Thread(
                    target=self._run_channel,
                    args=(ch, state),
                    name=f"analysis-{mode}-ch-{ch}",
                    daemon=True,
                )
                state.thread = thread
                thread.start()
                started_now.append(ch)

        if started_now:
            self.store.log(
                "info",
                f"Multi-camera monitoring started ({mode})",
                {"channels": target_channels, "primary_channel": primary_channel},
            )
            self._publish(
                "monitoring.started",
                {
                    "channel": primary_channel,
                    "channels": target_channels,
                    "mode": mode,
                },
            )
        return self.status()

    def stop(self) -> dict[str, Any]:
        was_monitoring = self.monitoring
        with self._lock:
            states = list(self._channel_states.values())
            self._channel_states.clear()
            self.manager.protected_channels.clear()
            for state in states:
                state.stop_event.set()

        for state in states:
            if state.thread and state.thread.is_alive():
                state.thread.join(timeout=3.0)

        if was_monitoring and self.mode == MODE_SHOP:
            for state in states:
                if state.interaction is not None:
                    try:
                        grabber = self.manager.get(state.channel, autostart=False)
                        forced = TYPE_UNATTENDED if state.unattended_alerted else None
                        self._complete_shop_interaction_channel(
                            state,
                            grabber.read_frame(),
                            greeted=False,
                            forced_type=forced,
                        )
                    except Exception as error:  # noqa: BLE001
                        logger.debug("Could not finalize pending interaction on ch %s: %s", state.channel, error)

        if was_monitoring:
            self.store.log(
                "info",
                "Multi-camera monitoring stopped",
                {"channels": [s.channel for s in states], "mode": self.mode},
            )
            self._publish(
                "monitoring.stopped",
                {"channel": self.channel, "mode": self.mode},
            )

        with self._lock:
            self.person_count = 0
            self.customer_count = 0
            self.unattended_active = False
        return self.status()

    def _submit_dispatch(self, fn, *args) -> Future:
        future = self._dispatch_executor.submit(fn, *args)
        with self._dispatch_lock:
            self._pending_dispatches.add(future)

        def _cleanup(done_future: Future) -> None:
            with self._dispatch_lock:
                self._pending_dispatches.discard(done_future)

        future.add_done_callback(_cleanup)
        return future

    def wait_for_dispatches(self, timeout: float = 10.0) -> bool:
        """Wait for all currently queued alert dispatches to complete."""
        with self._dispatch_lock:
            pending = list(self._pending_dispatches)
        if not pending:
            return True
        _, not_done = wait_futures(pending, timeout=timeout)
        return len(not_done) == 0

    def shutdown(self) -> None:
        self.stop()
        self._dispatch_executor.shutdown(wait=False, cancel_futures=False)
        self.manager.shutdown()

    # ------------------------------------------------------------------- loop
    @staticmethod
    def _to_bg_gray(frame):
        """Downscaled blurred BGR image for fast background/bbox motion checks."""
        if frame is None:
            return None
        try:
            small = cv2.resize(frame, (320, 180), interpolation=cv2.INTER_NEAREST)
            return cv2.GaussianBlur(small, (5, 5), 0)
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _bbox_has_motion(bg_gray, curr_gray, box: dict[str, Any]) -> bool:
        """Return True when the normalized bounding box has meaningful pixel change vs background."""
        if bg_gray is None or curr_gray is None:
            return True
        try:
            h, w = curr_gray.shape[:2]
            x1 = max(0, min(w - 1, int(float(box.get("x1", 0.0)) * w)))
            y1 = max(0, min(h - 1, int(float(box.get("y1", 0.0)) * h)))
            x2 = max(x1 + 1, min(w, int(float(box.get("x2", 1.0)) * w)))
            y2 = max(y1 + 1, min(h, int(float(box.get("y2", 1.0)) * h)))
            roi_bg = bg_gray[y1:y2, x1:x2]
            roi_curr = curr_gray[y1:y2, x1:x2]
            if roi_bg.size == 0 or roi_curr.size == 0:
                return False
            diff = cv2.absdiff(roi_bg, roi_curr)
            if diff.ndim == 3:
                diff = np.max(diff, axis=2)
            changed = int(np.count_nonzero(diff > BBOX_MOTION_PIXEL_THRESHOLD))
            total_pixels = int(diff.size)
            return (changed / float(total_pixels)) >= BBOX_MOTION_RATIO_THRESHOLD
        except Exception:  # noqa: BLE001
            return True

    @staticmethod
    def _box_iou(box_a: dict[str, Any], box_b: dict[str, Any]) -> float:
        ax1, ay1 = float(box_a.get("x1", 0.0)), float(box_a.get("y1", 0.0))
        ax2, ay2 = float(box_a.get("x2", 0.0)), float(box_a.get("y2", 0.0))
        bx1, by1 = float(box_b.get("x1", 0.0)), float(box_b.get("y1", 0.0))
        bx2, by2 = float(box_b.get("x2", 0.0)), float(box_b.get("y2", 0.0))
        ix1, iy1 = max(ax1, bx1), max(ay1, by1)
        ix2, iy2 = min(ax2, bx2), min(ay2, by2)
        inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
        if inter <= 0.0:
            return 0.0
        area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
        area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
        union = area_a + area_b - inter
        return inter / union if union > 0.0 else 0.0

    @staticmethod
    def _clear_pending_entry(state: _ChannelMonitorState) -> None:
        state.pending_entry_frame = None
        state.pending_entry_at = None
        state.pending_entry_count = 0
        state.pending_entry_frames = 0
        state.pending_entry_confidence = 0.0
        state.pending_entry_persons = []
        state.pending_entry_labels = []

    @staticmethod
    def _compute_motion(prev_gray, frame) -> tuple[bool, Any]:
        """Fast <0.3ms motion check on a 160x90 thumbnail to catch entries immediately."""
        try:
            small = cv2.resize(frame, (160, 90), interpolation=cv2.INTER_NEAREST)
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            gray = cv2.GaussianBlur(gray, (5, 5), 0)
            if prev_gray is None:
                return False, gray
            diff = cv2.absdiff(prev_gray, gray)
            changed = int(np.count_nonzero(diff > MOTION_PIXEL_THRESHOLD))
            ratio = changed / float(160 * 90)
            return ratio >= MOTION_RATIO_THRESHOLD, gray
        except Exception:  # noqa: BLE001
            return False, prev_gray

    def _run_channel(self, channel: int, state: _ChannelMonitorState) -> None:
        grabber = self.manager.get(channel, autostart=True)
        logger.info("Analysis loop started for channel %s (%s)", channel, self.mode)
        shop_periodic_interval = max(1.0, min(float(self.settings.analysis_interval_seconds), 2.0))
        supports_wait = hasattr(grabber, "wait_for_new_frame")

        while not state.stop_event.is_set():
            try:
                if supports_wait:
                    rev, frame, frame_at = grabber.wait_for_new_frame(
                        state.last_frame_revision,
                        timeout=0.25,
                    )
                else:
                    rev, frame = grabber.read_frame_with_revision()
                    frame_at = None

                if frame is None:
                    if grabber.wait_for_frame(timeout=1.0):
                        continue
                    state.last_error = grabber.status().get("error") or f"Waiting for camera {channel} frames"
                    self.last_error = state.last_error
                    state.stop_event.wait(0.25)
                    continue

                if rev == state.last_frame_revision:
                    if not supports_wait:
                        state.stop_event.wait(FAST_POLL_SECONDS)
                    continue

                # Freeze exact frame and capture timestamp immediately on receipt.
                detected_at = frame_at or utcnow()
                captured_frame = frame.copy()
                state.last_frame_revision = rev

                # In warehouse mode, skip vision calls outside after-hours.
                if self.mode == MODE_WAREHOUSE:
                    hours = self.owner_settings.after_hours()
                    if not is_after_hours(hours["start"], hours["end"], now=detected_at):
                        state.active_occupants = 0
                        state.occupant_absent_since.clear()
                        state.last_occupied_gray = None
                        state.last_occupied_boxes = []
                        self._clear_pending_entry(state)
                        state.person_count = 0
                        state.last_had_motion = False
                        _, state.prev_gray = self._compute_motion(state.prev_gray, captured_frame)
                        state.background_gray = self._to_bg_gray(captured_frame)
                        state.stop_event.wait(1.0)
                        continue

                now = self._monotonic()
                has_motion, next_gray = self._compute_motion(state.prev_gray, captured_frame)
                motion_rising_edge = has_motion and not state.last_had_motion

                if self.mode == MODE_WAREHOUSE:
                    has_pending_entry = state.pending_entry_frames > 0
                    if has_pending_entry:
                        # Immediately confirm on the very next live frame (~150-250ms later)
                        should_detect = True
                    elif state.active_occupants == 0:
                        # Zero idle-room Rekognition calls: wake ONLY on real OpenCV motion
                        elapsed_since_detect = (
                            float("inf")
                            if state.last_detection_mono == 0.0
                            else now - state.last_detection_mono
                        )
                        should_detect = motion_rising_edge or (
                            has_motion and elapsed_since_detect >= MOTION_MIN_INTERVAL_SECONDS
                        )
                    else:
                        # Occupied room: throttle motion checks to 4.0s, slow 45.0s departure heartbeat
                        if state.last_detection_mono == 0.0:
                            state.last_detection_mono = now
                        elapsed_since_detect = now - state.last_detection_mono
                        should_detect = (
                            has_motion
                            and elapsed_since_detect >= WAREHOUSE_OCCUPIED_MOTION_INTERVAL_SECONDS
                        ) or elapsed_since_detect >= WAREHOUSE_OCCUPIED_HEARTBEAT_SECONDS
                else:
                    has_pending_entry = False
                    elapsed_since_detect = now - state.last_detection_mono
                    should_detect = (
                        state.last_detection_mono == 0.0
                        or motion_rising_edge
                        or (has_motion and elapsed_since_detect >= MOTION_MIN_INTERVAL_SECONDS)
                        or elapsed_since_detect >= shop_periodic_interval
                    )

                # Refresh static background smoothly while the room is empty and completely still.
                if (
                    self.mode == MODE_WAREHOUSE
                    and state.active_occupants == 0
                    and not has_motion
                    and not has_pending_entry
                ):
                    curr_bg = self._to_bg_gray(captured_frame)
                    if curr_bg is not None:
                        if state.background_gray is None:
                            state.background_gray = curr_bg
                        else:
                            state.background_gray = cv2.addWeighted(
                                state.background_gray, 0.9, curr_bg, 0.1, 0
                            )

                # Only advance prev_gray when idle or actually analyzing so throttled motion
                # frames never erase a real entry delta before analysis runs.
                if should_detect or not has_motion:
                    state.prev_gray = next_gray
                    state.last_had_motion = has_motion

                if should_detect:
                    state.last_detection_mono = self._monotonic()
                    self._analyse_channel(channel, state, captured_frame, detected_at=detected_at)
                    state.last_error = None
                    if self.channel == channel:
                        self.last_error = None
            except Exception as error:  # noqa: BLE001 - keep the loop alive
                state.last_error = str(error)
                self.last_error = str(error)
                logger.exception("Analysis tick failed on channel %s: %s", channel, error)
                self.store.log("error", "Analysis tick failed", {"channel": channel, "error": str(error)})
                state.stop_event.wait(FAST_POLL_SECONDS)

        logger.info("Analysis loop stopped for channel %s", channel)

    def _encode_for_rekognition(self, frame) -> bytes:
        """Encode full-HD analysis frame at snapshot_jpeg_quality, staying under 4.5 MB."""
        analysis_frame = frame
        max_width = int(getattr(self.settings, "analysis_width", 1920) or 0)
        if max_width > 0 and analysis_frame.shape[1] > max_width:
            scale = max_width / analysis_frame.shape[1]
            analysis_frame = cv2.resize(
                analysis_frame,
                (max_width, max(1, int(analysis_frame.shape[0] * scale))),
                interpolation=cv2.INTER_AREA,
            )

        quality = int(getattr(self.settings, "snapshot_jpeg_quality", 92) or 92)
        ok, buffer = cv2.imencode(".jpg", analysis_frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
        image_bytes = buffer.tobytes() if ok else b""

        # If a massive raw sensor image still exceeds 4.5 MB, step down smoothly without upscaling.
        while len(image_bytes) > REKOGNITION_MAX_IMAGE_BYTES:
            if quality > 75:
                quality = max(75, quality - 10)
            else:
                new_w = max(640, int(analysis_frame.shape[1] * 0.75))
                if new_w >= analysis_frame.shape[1]:
                    break
                scale = new_w / analysis_frame.shape[1]
                analysis_frame = cv2.resize(
                    analysis_frame,
                    (new_w, max(1, int(analysis_frame.shape[0] * scale))),
                    interpolation=cv2.INTER_AREA,
                )
            ok, buffer = cv2.imencode(".jpg", analysis_frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
            if not ok:
                return b""
            image_bytes = buffer.tobytes()

        return image_bytes

    def _detect(self, frame) -> dict[str, Any]:
        image_bytes = self._encode_for_rekognition(frame)
        if not image_bytes:
            logger.error("Failed to JPEG-encode frame for AWS Rekognition analysis")
            return {
                "person_count": 0,
                "confidence": 0.0,
                "labels": [],
                "persons": [],
                "source": "encode-error",
                "error": True,
            }

        if not self.vision.available:
            logger.error("AWS Rekognition is unavailable; skipping detection without fallback")
            return {
                "person_count": 0,
                "confidence": 0.0,
                "labels": [],
                "persons": [],
                "source": "aws-rekognition-unavailable",
                "error": True,
            }

        result = self.vision.detect_persons(image_bytes)
        if result is None:
            logger.error("AWS Rekognition returned an error; skipping detection without fallback")
            return {
                "person_count": 0,
                "confidence": 0.0,
                "labels": [],
                "persons": [],
                "source": "aws-rekognition-error",
                "error": True,
            }
        if result.get("error"):
            return result

        entry_conf = float(
            getattr(self.settings, "rekognition_min_confidence", ENTRY_MIN_CONFIDENCE)
            or ENTRY_MIN_CONFIDENCE
        )
        occupant_ratio = min(OCCUPANT_MIN_CONFIDENCE, entry_conf) / 100.0
        entry_ratio = entry_conf / 100.0
        persons = result.get("persons") if isinstance(result.get("persons"), list) else []
        valid_persons = [
            p for p in persons if float(p.get("score", result.get("confidence", 0.0))) >= occupant_ratio
        ]
        result["persons"] = valid_persons
        result["person_count"] = len(valid_persons)
        result["entry_person_count"] = sum(
            1 for p in valid_persons if float(p.get("score", result.get("confidence", 0.0))) >= entry_ratio
        )
        if valid_persons:
            result["confidence"] = round(
                sum(float(p.get("score", 0.0)) for p in valid_persons) / len(valid_persons),
                4,
            )
        else:
            result["confidence"] = 0.0
        return result

    def _analyse_channel(
        self,
        channel: int,
        state: _ChannelMonitorState,
        frame,
        detected_at: datetime | None = None,
    ) -> None:
        # Freeze exact entry frame and timestamp BEFORE running detection I/O.
        exact_at = detected_at or utcnow()
        exact_frame = frame.copy()
        result = self._detect(exact_frame)
        person_count = int(result.get("person_count", 0))
        serialized = serialize_doc(result)

        with self._lock:
            state.person_count = person_count
            state.last_analysis_at = exact_at
            state.last_result = serialized
            self.last_analysis_at = exact_at
            self.last_result = serialized
            self.person_count = sum(s.person_count for s in self._channel_states.values())

        if self.mode == MODE_SHOP:
            self._handle_shop_channel(channel, state, exact_frame, result)
        elif self.mode == MODE_WAREHOUSE:
            self._handle_warehouse_channel(
                channel,
                state,
                exact_frame,
                result,
                detected_at=exact_at,
            )

    # ------------------------------------------------------------------- shop
    def _handle_shop_channel(
        self,
        channel: int,
        state: _ChannelMonitorState,
        frame,
        result: dict[str, Any],
    ) -> None:
        person_count = int(result.get("person_count", 0))
        now = time.monotonic()
        monitoring_settings = self.owner_settings.unattended()
        unattended_enabled = bool(monitoring_settings.get("enabled"))

        if person_count == 0:
            state.staff_established = False
            state.person_present_since = None
            state.customer_count = 0
            state.unattended_active = False
            if state.interaction is None:
                state.empty_since = None
                return
            if state.empty_since is None:
                state.empty_since = now
            if now - state.empty_since >= SHOP_ABSENCE_GRACE_SECONDS:
                forced = TYPE_UNATTENDED if state.unattended_alerted else None
                self._complete_shop_interaction_channel(
                    state,
                    frame,
                    greeted=False,
                    forced_type=forced,
                )
            return

        state.empty_since = None
        if state.person_present_since is None:
            state.person_present_since = now
        elif (
            not state.staff_established
            and now - state.person_present_since
            >= float(monitoring_settings.get("staff_presence_seconds", 20))
        ):
            state.staff_established = True

        staff_present = self._estimate_staff(
            state,
            result.get("persons") or [],
            person_count,
            unattended_enabled,
            monitoring_settings,
        )
        state.shopkeeper_present = staff_present
        customer_count = max(0, person_count - (1 if staff_present else 0))
        state.customer_count = customer_count

        if state.interaction is None:
            if now < state.shop_cooldown_until:
                return
            state.interaction = {
                "started_at": utcnow(),
                "expires_at": now + self.settings.greeting_window_seconds,
                "person_count": person_count,
                "customer_count": customer_count,
                "shopkeeper_present": staff_present,
                "confidence": result.get("confidence", 0.0),
                "labels": result.get("labels", []),
                "transcript": "",
                "speech_confidence": 0.0,
            }
            state.unattended_since = None
            state.unattended_alerted = False
            return

        state.interaction["person_count"] = max(
            int(state.interaction.get("person_count", 0)), person_count
        )
        state.interaction["customer_count"] = max(
            int(state.interaction.get("customer_count", 0)),
            customer_count,
        )
        state.interaction["shopkeeper_present"] = (
            state.interaction.get("shopkeeper_present") or staff_present
        )

        if not unattended_enabled:
            state.unattended_since = None
            state.unattended_active = False
        elif customer_count > 0 and not staff_present:
            if state.unattended_since is None:
                state.unattended_since = now
            state.unattended_active = True
            state.interaction["expires_at"] = max(
                state.interaction["expires_at"],
                now + 1.0,
            )
            elapsed = now - state.unattended_since
            threshold = float(monitoring_settings.get("after_seconds", 10))
            if (
                not state.unattended_alerted
                and elapsed >= threshold
                and now >= state.last_unattended_alert
            ):
                state.unattended_alerted = True
                state.last_unattended_alert = (
                    now + self.settings.alert_cooldown_seconds
                )
                self._complete_shop_interaction_channel(
                    state,
                    frame,
                    greeted=False,
                    forced_type=TYPE_UNATTENDED,
                )
                return
        else:
            state.unattended_since = None
            state.unattended_active = False

        if now >= state.interaction["expires_at"]:
            self._complete_shop_interaction_channel(state, frame, greeted=False)

    @staticmethod
    def _estimate_staff(
        state: _ChannelMonitorState,
        persons: list[dict[str, Any]],
        person_count: int,
        unattended_enabled: bool,
        monitoring_settings: dict[str, Any],
    ) -> bool:
        if not unattended_enabled:
            return True

        zone = monitoring_settings.get("staff_zone")
        if zone:
            x1, y1, x2, y2 = zone
            for person in persons:
                try:
                    center_x = (float(person["x1"]) + float(person["x2"])) / 2
                    center_y = (float(person["y1"]) + float(person["y2"])) / 2
                except (KeyError, TypeError, ValueError):
                    continue
                if x1 <= center_x <= x2 and y1 <= center_y <= y2:
                    return True

        if person_count >= 2:
            return True
        if state.staff_established:
            return True
        return False

    def record_speech(self, transcript: str, speech_confidence: float = 0.0) -> dict[str, Any]:
        """Feed a transcript from the STT pipeline into the active shop interaction."""
        transcript = (transcript or "").strip()
        if not transcript:
            return {"accepted": False, "reason": "empty transcript"}

        with self._lock:
            if not self.monitoring or self.mode != MODE_SHOP:
                return {"accepted": False, "reason": "shop monitoring is not running"}

            target_ch = self.channel or (self.active_channels[0] if self.active_channels else 2)
            state = self._channel_states.get(target_ch)
            if state is None:
                return {"accepted": False, "reason": "channel state not found"}

            if state.interaction is None:
                state.interaction = {
                    "started_at": utcnow(),
                    "expires_at": time.monotonic() + self.settings.greeting_window_seconds,
                    "person_count": max(1, state.person_count),
                    "customer_count": max(1, state.person_count),
                    "shopkeeper_present": True,
                    "confidence": 0.0,
                    "labels": [],
                    "transcript": "",
                    "speech_confidence": 0.0,
                }

            state.interaction["transcript"] = transcript
            state.interaction["speech_confidence"] = speech_confidence
            state.interaction["shopkeeper_present"] = True
            state.shopkeeper_present = True
            state.unattended_since = None
            state.unattended_active = False

            verdict = self.openrouter.classify_greeting(
                transcript,
                person_count=state.interaction["person_count"],
                labels=state.interaction["labels"],
            )

            if verdict.get("greeting_detected"):
                frame = self.manager.get(target_ch).read_frame()
                interaction = self._complete_shop_interaction_channel(
                    state,
                    frame,
                    greeted=True,
                    verdict=verdict,
                )
                return {
                    "accepted": True,
                    "greeting_detected": True,
                    "verdict": verdict,
                    "interaction": interaction,
                }

            return {
                "accepted": True,
                "greeting_detected": False,
                "verdict": verdict,
                "interaction": None,
            }

    def _complete_shop_interaction_channel(
        self,
        state: _ChannelMonitorState,
        frame,
        greeted: bool,
        verdict: dict[str, Any] | None = None,
        forced_type: str | None = None,
    ) -> dict[str, Any] | None:
        with self._lock:
            interaction_state = state.interaction
            if interaction_state is None:
                return None
            state.interaction = None
            state.shop_cooldown_until = (
                time.monotonic() + self.settings.interaction_cooldown_seconds
            )
            state.unattended_since = None
            state.unattended_active = False

        ended_at = utcnow()
        started_at = interaction_state.get("started_at", ended_at)
        duration = max(0.0, (ended_at - started_at).total_seconds())
        response_time = duration if greeted else None
        interaction_type = forced_type or (
            TYPE_SUCCESSFUL if greeted else TYPE_MISSED
        )
        confidence = max(
            float(interaction_state.get("speech_confidence") or 0.0),
            float((verdict or {}).get("confidence") or 0.0),
            float(interaction_state.get("confidence") or 0.0),
        )
        snapshot = self._save_snapshot(frame, f"shop_ch{state.channel:02d}") if frame is not None else None
        notes = (verdict or {}).get("reason") or (
            "Greeting detected"
            if greeted
            else "Customer left without a greeting"
            if interaction_type == TYPE_MISSED
            else "Customer unattended - shopkeeper not at the counter"
        )

        interaction = Interaction(
            channel=state.channel,
            greeting_detected=greeted,
            confidence=confidence if greeted else interaction_state.get("confidence", 0.0),
            person_count=interaction_state.get("person_count", 1),
            customer_count=interaction_state.get(
                "customer_count",
                max(1, interaction_state.get("person_count", 1) - 1),
            ),
            shopkeeper_present=bool(
                greeted or interaction_state.get("shopkeeper_present", True)
            ),
            interaction_type=interaction_type,
            duration_seconds=duration,
            response_time_seconds=response_time,
            shop_id=self.owner_settings.get()["shop"]["id"],
            speech_text=interaction_state.get("transcript", ""),
            source=(verdict or {}).get("source") or ("unknown" if greeted else "no-speech"),
            notes=notes,
            snapshot=snapshot,
            created_at=started_at,
        )

        try:
            self.store.insert_interaction(interaction)
        except DataStoreError as error:
            state.last_error = str(error)
            logger.error("Could not save interaction: %s", error)
            return None

        self.store.log(
            "info",
            "Greeting detected"
            if greeted
            else "Unattended customer"
            if interaction_type == TYPE_UNATTENDED
            else "Greeting missed",
            {"channel": state.channel, "interaction_id": interaction.id},
        )
        self._publish("interaction.created", interaction.to_api())

        if interaction_type == TYPE_UNATTENDED:
            snapshot_url = self._snapshot_url(snapshot)
            try:
                self.alerts.create_unattended_alert(
                    interaction, snapshot_url=snapshot_url
                )
            except Exception as error:  # noqa: BLE001
                state.last_error = str(error)
                logger.exception("Unattended alert failed: %s", error)

        return interaction.to_api()

    # -------------------------------------------------------------- warehouse
    def _resolve_camera_location(self, channel: int) -> str:
        """Return the camera's configured room/location label, or empty if unset."""
        default_names = {f"camera {channel:02d}", f"camera {channel}"}
        for cam in self.owner_settings.cameras():
            if int(cam.get("channel", 0)) == int(channel):
                loc = str(cam.get("location") or "").strip()
                if loc:
                    return loc
                name = str(cam.get("name") or "").strip()
                if name and name.lower() not in default_names:
                    return name
                return ""
        return ""

    def _absence_grace_seconds(self) -> float:
        value = getattr(
            self.settings,
            "warehouse_absence_grace_seconds",
            WAREHOUSE_ABSENCE_GRACE_SECONDS,
        )
        return max(0.0, float(WAREHOUSE_ABSENCE_GRACE_SECONDS if value is None else value))

    def _handle_warehouse_channel(
        self,
        channel: int,
        state: _ChannelMonitorState,
        frame,
        result: dict[str, Any],
        detected_at: datetime | None = None,
    ) -> None:
        event_time = detected_at or utcnow()
        curr_bg = self._to_bg_gray(frame)
        hours = self.owner_settings.after_hours()
        if not is_after_hours(hours["start"], hours["end"], now=event_time):
            state.active_occupants = 0
            state.occupant_absent_since.clear()
            state.last_occupied_gray = None
            state.last_occupied_boxes = []
            self._clear_pending_entry(state)
            if curr_bg is not None:
                state.background_gray = curr_bg
            return

        # Ignore transient detector errors or daily budget cap without resetting active_occupants.
        if result.get("error"):
            return

        entry_conf = float(
            getattr(self.settings, "rekognition_min_confidence", ENTRY_MIN_CONFIDENCE)
            or ENTRY_MIN_CONFIDENCE
        )
        occupant_ratio = min(OCCUPANT_MIN_CONFIDENCE, entry_conf) / 100.0
        entry_ratio = entry_conf / 100.0

        raw_persons = result.get("persons") if isinstance(result.get("persons"), list) else []
        occupant_persons = [
            p
            for p in raw_persons
            if float(p.get("score", result.get("confidence", 0.0))) >= occupant_ratio
        ]
        labels = [str(lbl) for lbl in (result.get("labels") or []) if lbl]
        detected_count = len(occupant_persons)

        now = self._monotonic()
        grace_seconds = self._absence_grace_seconds()

        # Visible slots 1..detected_count are present -> clear their departure timers.
        for slot in range(1, detected_count + 1):
            state.occupant_absent_since.pop(slot, None)

        if detected_count < state.active_occupants:
            self._clear_pending_entry(state)
            # Start/track departure timers per missing occupant slot without dropping
            # active_occupants on brief occlusion or detector flicker.
            for slot in range(detected_count + 1, state.active_occupants + 1):
                state.occupant_absent_since.setdefault(slot, now)

            expired_slots = [
                slot
                for slot in range(detected_count + 1, state.active_occupants + 1)
                if now - state.occupant_absent_since[slot] >= grace_seconds
            ]
            if expired_slots:
                for slot in expired_slots:
                    state.occupant_absent_since.pop(slot, None)
                state.active_occupants -= len(expired_slots)
            if state.active_occupants == 0:
                state.last_occupied_gray = None
                state.last_occupied_boxes = []
                if curr_bg is not None:
                    state.background_gray = curr_bg
            return

        if detected_count == state.active_occupants:
            self._clear_pending_entry(state)
            state.occupant_absent_since.clear()
            if state.active_occupants == 0 and curr_bg is not None:
                if state.background_gray is None:
                    state.background_gray = curr_bg
                else:
                    state.background_gray = cv2.addWeighted(
                        state.background_gray, 0.9, curr_bg, 0.1, 0
                    )
            elif state.active_occupants > 0 and curr_bg is not None:
                state.last_occupied_gray = curr_bg
                state.last_occupied_boxes = [dict(p) for p in occupant_persons]
            return

        # Positive entry delta: verify candidate bounding boxes actually moved vs static background.
        if state.background_gray is not None and curr_bg is not None:
            moving_persons = [
                p for p in occupant_persons if self._bbox_has_motion(state.background_gray, curr_bg, p)
            ]
        else:
            if state.background_gray is None and curr_bg is not None:
                state.background_gray = curr_bg
            moving_persons = list(occupant_persons)

        # Dual-threshold hysteresis & seated-posture shift rejection:
        if state.active_occupants == 0:
            # At least one moving box must meet ENTRY_MIN_CONFIDENCE (85%); all moving
            # occupant-tier boxes (>= 65%) are counted immediately so seated people aren't missed.
            has_entry_box = any(
                float(p.get("score", result.get("confidence", 0.0))) >= entry_ratio
                for p in moving_persons
            )
            validated_persons = moving_persons if has_entry_box else []
        else:
            # Room is already occupied: keep boxes matching previously present occupants, and
            # require any newly appeared box to show local pixel motion vs last_occupied_gray
            # (rejects stationary seated people whose confidence ticks 84% -> 86%).
            existing_boxes: list[dict[str, Any]] = []
            new_boxes: list[dict[str, Any]] = []
            for box in moving_persons:
                is_existing = any(
                    self._box_iou(box, prev_box) >= OCCUPANT_BOX_IOU_THRESHOLD
                    for prev_box in state.last_occupied_boxes
                )
                if is_existing:
                    existing_boxes.append(box)
                else:
                    if (
                        state.last_occupied_gray is None
                        or curr_bg is None
                        or self._bbox_has_motion(state.last_occupied_gray, curr_bg, box)
                    ):
                        new_boxes.append(box)
            has_high_conf_new = any(
                float(p.get("score", result.get("confidence", 0.0))) >= entry_ratio
                for p in new_boxes
            )
            validated_persons = (existing_boxes + new_boxes) if has_high_conf_new else []

        effective_count = len(validated_persons)
        if effective_count <= state.active_occupants:
            self._clear_pending_entry(state)
            return

        # Require CONFIRMATION_FRAMES consecutive positive frames before alerting,
        # while preserving frame 1 and its exact capture timestamp for zero delay.
        if state.pending_entry_frames == 0:
            state.pending_entry_frame = frame.copy()
            state.pending_entry_at = event_time
            state.pending_entry_count = effective_count
            state.pending_entry_frames = 1
            state.pending_entry_confidence = (
                round(
                    sum(float(p.get("score", 0.0)) for p in validated_persons)
                    / len(validated_persons),
                    4,
                )
                if validated_persons
                else float(result.get("confidence", 0.0))
            )
            state.pending_entry_persons = [dict(p) for p in validated_persons]
            state.pending_entry_labels = labels
            if CONFIRMATION_FRAMES > 1:
                return
        else:
            state.pending_entry_frames += 1
            if state.pending_entry_frames < CONFIRMATION_FRAMES:
                return

        confirmed_count = min(state.pending_entry_count, effective_count)
        if confirmed_count <= state.active_occupants:
            self._clear_pending_entry(state)
            return

        captured_frame = (
            state.pending_entry_frame.copy()
            if state.pending_entry_frame is not None
            else frame.copy()
        )
        captured_at = state.pending_entry_at or event_time
        confidence = state.pending_entry_confidence or float(result.get("confidence", 0.0))
        detections = list(state.pending_entry_persons)
        confirmed_labels = list(state.pending_entry_labels)
        self._clear_pending_entry(state)

        new_entries = confirmed_count - state.active_occupants
        state.active_occupants = confirmed_count
        state.occupant_absent_since.clear()
        if curr_bg is not None:
            state.last_occupied_gray = curr_bg
            state.last_occupied_boxes = [dict(p) for p in validated_persons]

        # Queue snapshot upload + DB persistence + delivery on the non-blocking pool
        # so subsequent entries on this channel are never blocked or dropped.
        self._submit_dispatch(
            self._record_and_email_warehouse_intrusion,
            channel,
            state,
            captured_frame,
            new_entries,
            confirmed_count,
            confidence,
            captured_at,
            detections,
            confirmed_labels,
        )

    def _record_and_email_warehouse_intrusion(
        self,
        channel: int,
        state: _ChannelMonitorState,
        captured_frame,
        new_entries: int,
        total_occupants: int,
        confidence: float,
        detected_at: datetime,
        detections: list[dict[str, Any]] | None = None,
        labels: list[str] | None = None,
    ) -> None:
        warehouse = self.owner_settings.get()["warehouse"]
        location = self._resolve_camera_location(channel)
        snapshot = self._save_snapshot(captured_frame, f"warehouse_ch{channel:02d}")
        entry = Entry(
            channel=channel,
            authorized=False,
            after_hours=True,
            person_count=new_entries,
            confidence=confidence,
            warehouse_id=warehouse["id"],
            entry_location=location,
            snapshot=snapshot,
            notes="Detected outside working hours (unknown person)",
            severity="critical",
            created_at=detected_at,
        )

        try:
            self.store.insert_entry(entry)
        except DataStoreError as error:
            state.last_error = str(error)
            logger.error("Could not save entry on channel %s: %s", channel, error)
            return

        self.store.log(
            "warning",
            "After-hours entry recorded",
            {
                "channel": channel,
                "after_hours": True,
                "entry_id": entry.id,
                "new_entries": new_entries,
                "active_occupants": total_occupants,
                "location": location,
            },
        )
        self._publish("entry.created", entry.to_api())

        snapshot_url = self._snapshot_url(snapshot)
        try:
            self.alerts.create_warehouse_alert(
                entry,
                snapshot_url=snapshot_url,
                detected_at=detected_at,
                detections=detections,
                labels=labels,
            )
        except Exception as error:  # noqa: BLE001
            state.last_error = str(error)
            logger.exception("Room entry alert failed on channel %s: %s", channel, error)

    # --------------------------------------------------------------- helpers
    def inspect_frame(self, channel: int) -> dict[str, Any]:
        """Run detection on the current frame without writing records."""
        if not self.settings.is_camera_allowed(channel):
            raise ValueError(f"Camera {channel} is not in allowed cameras ({self.settings.allowed_cameras})")
        if not self.owner_settings.camera_enabled(channel):
            raise ValueError(f"Camera {channel} is disabled in settings")

        grabber = self.manager.get(channel)
        if not grabber.wait_for_frame(timeout=8.0):
            raise RuntimeError(grabber.status().get("error") or "Camera stream unavailable")
        frame = grabber.read_frame()
        if frame is None:
            raise RuntimeError("No frame available")
        result = self._detect(frame)
        return {
            "channel": channel,
            "timestamp": to_iso(utcnow()),
            "detection": serialize_doc(result),
            "buffer_frames": len(grabber.recent_jpegs(self.settings.frame_buffer_seconds)),
        }

    def _save_snapshot(self, frame, prefix: str) -> str | None:
        if frame is None:
            return None
        try:
            # 1. Upload directly to Cloudinary if configured
            if getattr(self, "cloudinary", None) and self.cloudinary.available:
                cloud_url = self.cloudinary.upload_frame(frame, prefix=prefix)
                if cloud_url:
                    return cloud_url
                logger.warning("Cloudinary upload failed; falling back to local storage")

            # 2. Fall back to local storage
            self.settings.ensure_directories()
            filename = f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.jpg"
            path = Path(self.settings.upload_dir) / filename
            cv2.imwrite(str(path), frame)
            return f"/static/snapshots/{filename}"
        except Exception as error:  # noqa: BLE001
            logger.debug("Snapshot could not be saved: %s", error)
            return None

    def _snapshot_url(self, snapshot: str | None) -> str | None:
        if not snapshot:
            return None
        if snapshot.startswith("http://") or snapshot.startswith("https://"):
            return snapshot
        base = (self.settings.public_base_url or "").rstrip("/")
        return f"{base}{snapshot}" if base else snapshot

    def _publish(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.events is not None:
            self.events.publish(event_type, payload)

    def status(self) -> dict[str, Any]:
        hours = self.owner_settings.after_hours()
        shop_hours = self.owner_settings.shop_hours()
        monitoring_settings = self.owner_settings.unattended()
        active_chs = self.active_channels
        with self._lock:
            per_channel = {
                ch: {
                    "channel": ch,
                    "monitoring": bool(st.thread and st.thread.is_alive()),
                    "person_count": st.person_count,
                    "last_analysis_at": to_iso(st.last_analysis_at),
                    "last_error": st.last_error,
                    "active_occupants": st.active_occupants,
                }
                for ch, st in self._channel_states.items()
            }
        return {
            "monitoring": bool(active_chs),
            "mode": self.mode,
            "channel": self.channel,
            "active_channels": active_chs,
            "channel_states": per_channel,
            "started_at": to_iso(self.started_at),
            "last_analysis_at": to_iso(self.last_analysis_at),
            "last_error": self.last_error,
            "person_count": self.person_count,
            "customer_count": self.customer_count,
            "shopkeeper_present": self.shopkeeper_present,
            "unattended_active": self.unattended_active,
            "last_result": self.last_result,
            "greeting_window_active": False,
            "shop_hours": shop_hours,
            "after_hours": {
                "start": hours["start"],
                "end": hours["end"],
                "active": is_after_hours(hours["start"], hours["end"]),
            },
            "monitoring_settings": {
                "unattended_enabled": monitoring_settings.get("enabled"),
                "unattended_after_seconds": monitoring_settings.get("after_seconds"),
                "staff_zone": monitoring_settings.get("staff_zone"),
            },
            "features": self.owner_settings.features(),
        }
