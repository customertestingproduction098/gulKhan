"""RTSP frame acquisition and the real-time multi-camera surveillance pipeline.

Two cooperating pieces live here:

* ``FrameGrabber`` keeps a single low-latency RTSP connection per channel,
  continuously draining network packets via ``cap.grab()`` so frames are never
  delayed in FFmpeg/TCP buffers, and exposes the latest live frame.
* ``SurveillanceProcessor`` monitors ALL enabled cameras in parallel (one worker
  thread per camera channel), runs instant motion-triggered + periodic person
  detection (AWS Rekognition with OpenCV HOG fallback), captures the exact frame
  where the person is detected, and dispatches one single email per intrusion.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
import logging
import os
from pathlib import Path
import threading
import time
from typing import Any

# Configure FFmpeg RTSP options for minimum latency (no packet buffering)
os.environ.setdefault(
    "OPENCV_FFMPEG_CAPTURE_OPTIONS",
    "rtsp_transport;tcp|fflags;nobuffer|flags;low_delay",
)

import cv2
import numpy as np

from config.settings import Settings, get_settings
from models import Entry, Interaction
from models.interaction import TYPE_MISSED, TYPE_SUCCESSFUL, TYPE_UNATTENDED
from services.alert_system import AlertSystem
from services.aws_rekognition import get_vision_service
from services.cloudinary_storage import get_cloudinary_storage
from services.events import EventBroker
from services.mongodb_handler import DataStoreError, MongoHandler
from services.openrouter import get_openrouter_service
from services.settings_service import SettingsService
from utils.helpers import (
    current_time,
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
WAREHOUSE_ABSENCE_GRACE_SECONDS = 30.0
FAST_POLL_SECONDS = 0.25
MOTION_MIN_INTERVAL_SECONDS = 0.65
MOTION_PIXEL_THRESHOLD = 22
MOTION_RATIO_THRESHOLD = 0.006


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

    # Warehouse state (strictly per-camera deduplicated)
    warehouse_present: bool = False
    warehouse_alert_sent: bool = False
    warehouse_cooldown_until: float = 0.0
    warehouse_empty_since: float | None = None
    dispatching_alert: bool = False

    # Motion & timing state
    prev_gray: Any = None
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
        self._first_frame = threading.Event()
        self._jpeg: bytes | None = None
        self._frame = None
        self._last_frame_at: datetime | None = None
        self._last_access = time.monotonic()
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
        decode_interval = 1.0 / max(1, min(self.settings.camera_fps, 25))
        last_decode = 0.0

        while not self._stop.is_set():
            if self._cap is None and not self._open():
                self._stop.wait(retry_delay)
                continue

            # Continuously grab every incoming RTSP packet without sleeping so
            # the network socket buffer never accumulates old/delayed frames.
            grabbed = self._cap.grab() if self._cap is not None else False
            if not grabbed:
                self._error = f"Camera {self.channel} stream interrupted"
                self._release()
                self._stop.wait(retry_delay)
                continue

            now = time.monotonic()
            if (now - last_decode) < decode_interval and self._jpeg is not None:
                continue

            ret, frame = self._cap.retrieve() if self._cap is not None else (False, None)
            if not ret or frame is None:
                continue

            last_decode = now

            if self.settings.frame_width and frame.shape[1] > self.settings.frame_width:
                scale = self.settings.frame_width / frame.shape[1]
                frame = cv2.resize(
                    frame,
                    (self.settings.frame_width, int(frame.shape[0] * scale)),
                    interpolation=cv2.INTER_AREA,
                )

            stamp = current_time().strftime("%Y-%m-%d %H:%M:%S")
            cv2.putText(
                frame,
                f"CAM {self.channel:02d}  {stamp}",
                (12, 28),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.65,
                (0, 255, 120),
                2,
                cv2.LINE_AA,
            )

            ok, buffer = cv2.imencode(
                ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, self.settings.jpeg_quality]
            )
            if not ok:
                continue

            jpeg = buffer.tobytes()
            with self._lock:
                self._frame = frame
                self._jpeg = jpeg
                self._last_frame_at = utcnow()
                self._frames += 1
                self._revision += 1
                self._buffer.append((now, jpeg))
                self._first_frame.set()

            self._error = None

        self._release()

    # ---------------------------------------------------------------- readers
    def wait_for_frame(self, timeout: float = 8.0) -> bool:
        return self._first_frame.wait(timeout=timeout) and self._jpeg is not None

    def read_jpeg(self) -> bytes | None:
        self._last_access = time.monotonic()
        with self._lock:
            return self._jpeg

    def read_frame(self):
        self._last_access = time.monotonic()
        with self._lock:
            return None if self._frame is None else self._frame.copy()

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
            "connected": self._jpeg is not None and self._last_frame_at is not None,
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
        self._hog_lock = threading.Lock()
        self._channel_states: dict[int, _ChannelMonitorState] = {}

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

        self._hog = cv2.HOGDescriptor()
        self._hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())

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

    def shutdown(self) -> None:
        self.stop()
        self.manager.shutdown()

    # ------------------------------------------------------------------- loop
    @staticmethod
    def _compute_motion(prev_gray, frame) -> tuple[bool, Any]:
        """Fast <0.3ms motion check on a 160x90 thumbnail to catch entries immediately."""
        try:
            small = cv2.resize(frame, (160, 90), interpolation=cv2.INTER_NEAREST)
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            gray = cv2.GaussianBlur(gray, (5, 5), 0)
            if prev_gray is None:
                return True, gray
            diff = cv2.absdiff(prev_gray, gray)
            changed = int(np.count_nonzero(diff > MOTION_PIXEL_THRESHOLD))
            ratio = changed / float(160 * 90)
            return ratio >= MOTION_RATIO_THRESHOLD, gray
        except Exception:  # noqa: BLE001
            return True, prev_gray

    def _run_channel(self, channel: int, state: _ChannelMonitorState) -> None:
        grabber = self.manager.get(channel, autostart=True)
        logger.info("Analysis loop started for channel %s (%s)", channel, self.mode)
        periodic_interval = max(1.0, min(float(self.settings.analysis_interval_seconds), 2.0))

        while not state.stop_event.is_set():
            try:
                rev = grabber.revision
                frame = grabber.read_frame()
                if frame is None:
                    grabber.wait_for_frame(timeout=1.5)
                    state.last_error = grabber.status().get("error") or f"Waiting for camera {channel} frames"
                    self.last_error = state.last_error
                    state.stop_event.wait(0.5)
                    continue

                if rev == state.last_frame_revision:
                    state.stop_event.wait(FAST_POLL_SECONDS)
                    continue
                state.last_frame_revision = rev

                now = time.monotonic()
                has_motion, state.prev_gray = self._compute_motion(state.prev_gray, frame)
                elapsed_since_detect = now - state.last_detection_mono

                should_detect = (
                    state.last_detection_mono == 0.0
                    or elapsed_since_detect >= periodic_interval
                    or (has_motion and elapsed_since_detect >= MOTION_MIN_INTERVAL_SECONDS)
                )

                if should_detect:
                    # If triggered by fresh motion while room was empty, wait 0.15s and grab
                    # the latest frame so a person stepping through a doorway is fully in view.
                    if has_motion and not state.warehouse_present and state.person_count == 0:
                        state.stop_event.wait(0.15)
                        fresh_frame = grabber.read_frame()
                        if fresh_frame is not None:
                            frame = fresh_frame
                            _, state.prev_gray = self._compute_motion(state.prev_gray, frame)

                    state.last_detection_mono = time.monotonic()
                    self._analyse_channel(channel, state, frame)
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

    def _detect(self, frame) -> dict[str, Any]:
        analysis_frame = frame
        if (
            self.settings.analysis_width
            and frame.shape[1] > self.settings.analysis_width
        ):
            scale = self.settings.analysis_width / frame.shape[1]
            analysis_frame = cv2.resize(
                frame,
                (self.settings.analysis_width, int(frame.shape[0] * scale)),
                interpolation=cv2.INTER_AREA,
            )

        ok, buffer = cv2.imencode(
            ".jpg", analysis_frame, [cv2.IMWRITE_JPEG_QUALITY, self.settings.jpeg_quality]
        )
        image_bytes = buffer.tobytes() if ok else b""

        result = None
        if image_bytes and self.vision.available:
            result = self.vision.detect_persons(image_bytes)

        if result is None:
            count, confidence, persons = self._hog_detect(analysis_frame)
            result = {
                "person_count": count,
                "confidence": confidence,
                "labels": [],
                "persons": persons,
                "source": "opencv-hog",
            }

        min_conf = getattr(self.settings, "rekognition_min_confidence", None)
        if min_conf is None:
            min_conf = getattr(self.settings, "vision_min_confidence", 70.0)
        if result.get("person_count", 0) and result.get("confidence", 1.0) < float(min_conf) / 100.0:
            result["person_count"] = 0
            result["persons"] = []
        if not isinstance(result.get("persons"), list):
            result["persons"] = []
        return result

    def _analyse_channel(self, channel: int, state: _ChannelMonitorState, frame) -> None:
        # Keep an immutable copy of the exact frame being analyzed so that if a person
        # is detected in this frame, this exact image is saved and emailed.
        exact_frame = frame.copy()
        result = self._detect(exact_frame)
        person_count = int(result.get("person_count", 0))
        now_utc = utcnow()
        serialized = serialize_doc(result)

        with self._lock:
            state.person_count = person_count
            state.last_analysis_at = now_utc
            state.last_result = serialized
            self.last_analysis_at = now_utc
            self.last_result = serialized
            self.person_count = sum(s.person_count for s in self._channel_states.values())

        if self.mode == MODE_SHOP:
            self._handle_shop_channel(channel, state, exact_frame, result)
        elif self.mode == MODE_WAREHOUSE:
            self._handle_warehouse_channel(channel, state, exact_frame, result)

    def _hog_detect(self, frame) -> tuple[int, float, list[dict[str, float]]]:
        try:
            resized = frame
            if frame.shape[1] > 800:
                scale = 800 / frame.shape[1]
                resized = cv2.resize(frame, (800, int(frame.shape[0] * scale)))

            gray = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY)
            with self._hog_lock:
                rectangles, weights = self._hog.detectMultiScale(
                    gray,
                    winStride=(8, 8),
                    padding=(8, 8),
                    scale=1.05,
                )
            confidence = (
                sum(float(weight) for weight in weights) / len(weights) if len(weights) else 0.0
            )
            confidence = max(0.0, min(1.0, 0.5 + confidence / 2))
            height, width = resized.shape[:2]
            persons = [
                {
                    "x1": round(max(0.0, x / width), 4),
                    "y1": round(max(0.0, y / height), 4),
                    "x2": round(min(1.0, (x + w) / width), 4),
                    "y2": round(min(1.0, (y + h) / height), 4),
                    "score": round(confidence, 4),
                }
                for (x, y, w, h) in rectangles
            ]
            return len(rectangles), round(confidence, 4), persons
        except Exception as error:  # noqa: BLE001
            logger.debug("OpenCV person detection failed: %s", error)
            return 0, 0.0, []

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
    def _handle_warehouse_channel(
        self,
        channel: int,
        state: _ChannelMonitorState,
        frame,
        result: dict[str, Any],
    ) -> None:
        hours = self.owner_settings.after_hours()
        if not is_after_hours(hours["start"], hours["end"]):
            # Security only runs during non-operational hours (21:00–11:00 PKT).
            state.warehouse_present = False
            state.warehouse_alert_sent = False
            state.warehouse_empty_since = None
            return

        person_count = int(result.get("person_count", 0))
        now = time.monotonic()

        if person_count == 0:
            if state.warehouse_empty_since is None:
                state.warehouse_empty_since = now
            elif now - state.warehouse_empty_since >= WAREHOUSE_ABSENCE_GRACE_SECONDS:
                # Room has been continuously empty for 45+ seconds; reset presence session
                state.warehouse_present = False
                state.warehouse_alert_sent = False
            return

        # Person is present in the frame! Reset empty timer.
        state.warehouse_empty_since = None

        # As long as someone is continuously present in the room, suppress any new email.
        # Once they leave and the room becomes empty, the absence grace period resets
        # warehouse_present = False and warehouse_alert_sent = False so that when someone
        # returns, a new log and email are immediately dispatched.
        if state.warehouse_present or state.warehouse_alert_sent or state.dispatching_alert:
            state.warehouse_present = True
            return

        # Mark that someone has entered and the one alert for this session is being sent
        state.warehouse_present = True
        state.warehouse_alert_sent = True
        state.dispatching_alert = True

        # Exact frame where the person was detected right now
        captured_frame = frame.copy()
        confidence = float(result.get("confidence", 0.0))

        # Dispatch snapshot upload + DB insert + email asynchronously so camera monitoring never blocks
        threading.Thread(
            target=self._record_and_email_warehouse_intrusion,
            args=(channel, state, captured_frame, person_count, confidence),
            name=f"warehouse-alert-ch-{channel}",
            daemon=True,
        ).start()

    def _record_and_email_warehouse_intrusion(
        self,
        channel: int,
        state: _ChannelMonitorState,
        captured_frame,
        person_count: int,
        confidence: float,
    ) -> None:
        try:
            warehouse = self.owner_settings.get()["warehouse"]
            snapshot = self._save_snapshot(captured_frame, f"warehouse_ch{channel:02d}")
            entry = Entry(
                channel=channel,
                authorized=False,
                after_hours=True,
                person_count=person_count,
                confidence=confidence,
                warehouse_id=warehouse["id"],
                entry_location=warehouse["entry_location"],
                snapshot=snapshot,
                notes="Detected outside working hours (unknown person)",
                severity="critical",
            )

            try:
                self.store.insert_entry(entry)
            except DataStoreError as error:
                state.last_error = str(error)
                logger.error("Could not save entry on channel %s: %s", channel, error)
                return

            self.store.log(
                "warning",
                "Warehouse entry recorded",
                {"channel": channel, "after_hours": True, "entry_id": entry.id},
            )
            self._publish("entry.created", entry.to_api())

            snapshot_url = self._snapshot_url(snapshot)
            try:
                self.alerts.create_warehouse_alert(entry, snapshot_url=snapshot_url)
            except Exception as error:  # noqa: BLE001
                state.last_error = str(error)
                logger.exception("Warehouse alert failed on channel %s: %s", channel, error)
        finally:
            state.dispatching_alert = False

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
                    "warehouse_present": st.warehouse_present,
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
