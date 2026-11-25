"""End-to-end automated tests for the surveillance detection, tracking, and alerting pipeline.

Mocks ONLY external I/O boundaries (AWS Rekognition HTTP client, Cloudinary HTTP upload,
SMTP socket, RTSP VideoCapture, and MongoDB collection storage) while exercising the real
production logic across FrameGrabber, SurveillanceProcessor, AWSRekognitionService,
AlertSystem, SettingsService, Alert/Entry models, and Flask API routes.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import sys
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import cv2
import numpy as np
import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from config.settings import Settings
from models import Alert, Entry
from services.alert_system import AlertSystem
from services.aws_rekognition import AWSRekognitionService
from services.camera_processor import (
    CameraManager,
    FrameGrabber,
    MODE_WAREHOUSE,
    SurveillanceProcessor,
    _ChannelMonitorState,
)
from services.cloudinary_storage import CloudinaryStorage
from services.settings_service import SettingsService
from utils.helpers import UTC, current_time, format_local_datetime, is_after_hours, to_iso, to_local, utcnow


# ---------------------------------------------------------------------------
# Minimal in-memory store stub (I/O boundary only)
# ---------------------------------------------------------------------------
class InMemoryStore:
    def __init__(self, owner_settings_doc: dict[str, Any] | None = None) -> None:
        self._lock = threading.Lock()
        self.owner_settings_doc: dict[str, Any] | None = owner_settings_doc
        self.entries: list[Entry] = []
        self.alerts: list[Alert] = []
        self.logs: list[dict[str, Any]] = []
        self._seq = 0

    def _next_id(self) -> str:
        self._seq += 1
        return f"{self._seq:024x}"

    def get_owner_settings(self) -> dict[str, Any] | None:
        return self.owner_settings_doc

    def save_owner_settings(self, doc: dict[str, Any]) -> dict[str, Any]:
        self.owner_settings_doc = dict(doc)
        return self.owner_settings_doc

    def insert_entry(self, entry: Entry) -> str:
        with self._lock:
            entry.id = self._next_id()
            self.entries.append(entry)
            return entry.id

    def insert_alert(self, alert: Alert) -> str:
        with self._lock:
            alert.id = self._next_id()
            self.alerts.append(alert)
            return alert.id

    def update_alert_delivery(
        self,
        alert_id: str | None,
        *,
        delivery: dict[str, Any],
        sent_channels: list[str],
        status: str,
    ) -> None:
        with self._lock:
            for alert in self.alerts:
                if alert.id == alert_id:
                    alert.delivery = delivery
                    alert.sent_channels = list(sent_channels)
                    alert.status = status
                    break

    def update_entry_alert(
        self,
        entry_id: str | None,
        channels=None,
        severity=None,
        alert_sent: bool = True,
    ) -> None:
        with self._lock:
            for entry in self.entries:
                if entry.id == entry_id:
                    entry.alert_sent = bool(alert_sent)
                    if channels is not None:
                        entry.alert_channel = list(channels)
                    if severity:
                        entry.severity = severity
                    break

    def log(self, level: str, message: str, context: dict[str, Any] | None = None) -> None:
        with self._lock:
            self.logs.append({"level": level, "message": message, "context": context or {}})


def _make_settings(**overrides: Any) -> Settings:
    base = Settings(
        allowed_cameras=[2, 4, 7],
        after_hours_start="21:00",
        after_hours_end="06:00",
        warehouse_absence_grace_seconds=8.0,
        aws_access_key_id="test-key",
        aws_secret_access_key="test-secret",
        rekognition_enabled=True,
        rekognition_min_confidence=85.0,
        smtp_host="smtp.test.local",
        smtp_port=587,
        smtp_secure=False,
        mail_from="alerts@test.local",
        notification_email="owner@test.local",
        owner_email="owner@test.local",
        push_enabled=False,
        cloudinary_cloud_name="test-cloud",
        cloudinary_api_key="test-key",
        cloudinary_api_secret="test-secret",
        cloudinary_timeout_seconds=15,
    )
    return replace(base, **overrides)


def _make_frame(bgr: tuple[int, int, int] = (40, 80, 120), width: int = 320, height: int = 180) -> np.ndarray:
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    frame[:, :] = bgr
    return frame


def _detection(count: int, confidence: float = 0.94, error: bool = False) -> dict[str, Any]:
    if error:
        return {
            "person_count": 0,
            "confidence": 0.0,
            "labels": [],
            "persons": [],
            "source": "aws-rekognition-error",
            "error": True,
        }
    persons = [
        {"x1": 0.1 + 0.15 * i, "y1": 0.2, "x2": 0.22 + 0.15 * i, "y2": 0.75, "score": confidence}
        for i in range(count)
    ]
    return {
        "person_count": count,
        "confidence": confidence if count else 0.0,
        "labels": ["Person"] if count else [],
        "persons": persons,
        "source": "aws-rekognition",
    }


def _build_processor(
    *,
    settings: Settings | None = None,
    cameras: list[dict[str, Any]] | None = None,
    after_hours_start: str = "21:00",
    after_hours_end: str = "06:00",
) -> tuple[SurveillanceProcessor, InMemoryStore, AlertSystem, SettingsService]:
    cfg = settings or _make_settings(
        after_hours_start=after_hours_start,
        after_hours_end=after_hours_end,
    )
    owner_doc: dict[str, Any] = {
        "auth_seeded": True,
        "owner": {
            "id": "owner_001",
            "name": "Owner",
            "auth_email": "admin@test.local",
            "notification_email": "owner@test.local",
            "email": "owner@test.local",
            "secondary_email": "",
            "username": "admin",
            "password": "secret",
        },
        "warehouse": {
            "id": "warehouse_001",
            "name": "Main Warehouse",
            "after_hours_start": after_hours_start,
            "after_hours_end": after_hours_end,
            "entry_location": "",
        },
        "cameras": cameras
        or [
            {"channel": 2, "name": "Camera 02", "location": "", "enabled": True},
            {"channel": 4, "name": "Camera 04", "location": "Storage Hall", "enabled": True},
            {"channel": 7, "name": "Lounge 2", "location": "Lounge 2", "enabled": True},
        ],
    }
    store = InMemoryStore(owner_settings_doc=owner_doc)
    owner_settings = SettingsService(store, cfg)
    alerts = AlertSystem(store, cfg, owner_settings)
    manager = CameraManager(cfg)
    processor = SurveillanceProcessor(
        store=store,
        manager=manager,
        alerts=alerts,
        settings=cfg,
        owner_settings=owner_settings,
    )
    processor.mode = MODE_WAREHOUSE
    return processor, store, alerts, owner_settings


def _seed_empty_room(
    processor: SurveillanceProcessor,
    channel: int,
    state: _ChannelMonitorState,
    detected_at: datetime,
    color: tuple[int, int, int] = (20, 20, 20),
) -> np.ndarray:
    """Establish a clean static empty-room background baseline on a channel."""
    empty = _make_frame(color)
    processor._handle_warehouse_channel(channel, state, empty, _detection(0), detected_at=detected_at)
    return empty


def _confirm_entry(
    processor: SurveillanceProcessor,
    channel: int,
    state: _ChannelMonitorState,
    frame: np.ndarray,
    detection: dict[str, Any],
    detected_at: datetime,
    confirm_delay_ms: int = 250,
) -> None:
    """Feed frame 1 (entry at detected_at) and fast confirmation frame 2 (250ms later)."""
    processor._handle_warehouse_channel(channel, state, frame, detection, detected_at=detected_at)
    processor._handle_warehouse_channel(
        channel,
        state,
        frame.copy(),
        detection,
        detected_at=detected_at + timedelta(milliseconds=confirm_delay_ms),
    )


# ---------------------------------------------------------------------------
# TEST 1: Zero-delay timestamp & frame integrity under 3s Cloudinary + 3s SMTP lag
# ---------------------------------------------------------------------------
def test_zero_delay_timestamp_and_frame_integrity_with_slow_io(monkeypatch):
    monkeypatch.setenv("APP_TIMEZONE", "Asia/Karachi")
    now_local = current_time()
    win_start = (now_local - timedelta(hours=1)).strftime("%H:%M")
    win_end = (now_local + timedelta(hours=2)).strftime("%H:%M")
    processor, store, alerts, _ = _build_processor(
        after_hours_start=win_start,
        after_hours_end=win_end,
    )

    uploaded_frames: list[tuple[str, tuple[int, int, int]]] = []
    sent_emails: list[dict[str, str]] = []
    upload_lock = threading.Lock()
    email_lock = threading.Lock()

    def slow_upload(frame: np.ndarray, prefix: str = "snapshot", quality=None) -> str:
        # Sleep 3 seconds to simulate slow Cloudinary network I/O
        time.sleep(3.0)
        mean_bgr = tuple(int(round(v)) for v in frame.mean(axis=(0, 1)))
        with upload_lock:
            idx = len(uploaded_frames) + 1
            url = f"https://res.cloudinary.com/test/image/upload/v1/{prefix}_{idx}.jpg"
            uploaded_frames.append((url, mean_bgr))
        return url

    def slow_email(subject: str, html_body: str, to_email=None, text_body=None) -> dict[str, Any]:
        # Sleep 3 seconds to simulate slow SMTP handshake
        time.sleep(3.0)
        with email_lock:
            sent_emails.append({"subject": subject, "html": html_body})
        return {"sent": True, "skipped": False, "to": "owner@test.local"}

    fake_cloud = MagicMock()
    fake_cloud.available = True
    fake_cloud.upload_frame.side_effect = slow_upload
    processor.cloudinary = fake_cloud
    monkeypatch.setattr(alerts, "send_email", slow_email)

    state = _ChannelMonitorState(channel=7)
    _seed_empty_room(processor, 7, state, utcnow() - timedelta(seconds=5), color=(20, 20, 20))

    try:
        # 1st person enters on a blue-tinted frame at T0 (confirmed on frame 2 250ms later)
        frame_1 = _make_frame((200, 10, 10))
        frame_1_confirm = _make_frame((190, 20, 20))
        t0 = utcnow()
        call_start_1 = time.monotonic()
        processor._handle_warehouse_channel(7, state, frame_1, _detection(1), detected_at=t0)
        # Mutate original frame 1 buffer immediately after frame 1 to prove frame.copy() froze it
        frame_1[:, :] = (0, 0, 0)
        processor._handle_warehouse_channel(
            7, state, frame_1_confirm, _detection(1), detected_at=t0 + timedelta(milliseconds=250)
        )
        elapsed_handle_1 = time.monotonic() - call_start_1
        assert elapsed_handle_1 < 0.25, f"Detection handler blocked for {elapsed_handle_1:.2f}s"

        # Wait 0.4s (still deep inside the 3-second Cloudinary upload!) and send 2nd person entering
        time.sleep(0.4)
        frame_2 = _make_frame((10, 210, 10))
        frame_2_confirm = _make_frame((20, 200, 20))
        t1 = utcnow()
        call_start_2 = time.monotonic()
        processor._handle_warehouse_channel(7, state, frame_2, _detection(2), detected_at=t1)
        frame_2[:, :] = (0, 0, 0)
        processor._handle_warehouse_channel(
            7, state, frame_2_confirm, _detection(2), detected_at=t1 + timedelta(milliseconds=250)
        )
        elapsed_handle_2 = time.monotonic() - call_start_2
        assert elapsed_handle_2 < 0.25, f"2nd entry was blocked for {elapsed_handle_2:.2f}s"

        # Wait for both background dispatches (each sleeps 3s Cloudinary + 3s SMTP = ~6s concurrently)
        assert processor.wait_for_dispatches(timeout=15.0) is True
    finally:
        processor.wait_for_dispatches(timeout=10.0)
        processor.shutdown()

    assert len(store.entries) == 2
    assert len(store.alerts) == 2
    assert len(sent_emails) == 2

    entries_by_time = sorted(store.entries, key=lambda e: e.created_at)
    alerts_by_time = sorted(store.alerts, key=lambda a: a.created_at)

    # Verify timestamps equal exact frame capture time (< 0.5s), NOT post-upload/post-SMTP (+6s)
    assert abs((entries_by_time[0].created_at - t0).total_seconds()) < 0.5
    assert abs((alerts_by_time[0].created_at - t0).total_seconds()) < 0.5
    assert abs((entries_by_time[1].created_at - t1).total_seconds()) < 0.5
    assert abs((alerts_by_time[1].created_at - t1).total_seconds()) < 0.5

    # Verify exact entry frames were frozen and uploaded (not mutated black frames)
    bgr_set = {bgr for _, bgr in uploaded_frames}
    assert (200, 10, 10) in bgr_set
    assert (10, 210, 10) in bgr_set

    # Both entries represent delta=1 ("1 person detected"), NOT "2 people detected"
    assert entries_by_time[0].person_count == 1
    assert entries_by_time[1].person_count == 1
    assert "1 person detected" in alerts_by_time[0].message
    assert "1 person detected" in alerts_by_time[1].message
    assert "2 people detected" not in alerts_by_time[1].message


def test_cloudinary_upload_passes_explicit_timeout():
    cfg = _make_settings(cloudinary_timeout_seconds=12)
    with patch("services.cloudinary_storage._init_cloudinary", return_value=True):
        storage = CloudinaryStorage(cfg)
    storage._available = True

    with patch("cloudinary.uploader.upload", return_value={"secure_url": "https://cdn/img.jpg"}) as mock_upload:
        url = storage.upload_bytes(b"\xff\xd8\xfftest", prefix="warehouse_ch07")

    assert url == "https://cdn/img.jpg"
    assert mock_upload.call_args.kwargs["timeout"] == 12


# ---------------------------------------------------------------------------
# TEST 2: Same-person stay over simulated 15+ minutes -> exactly 1 alert (0 repeats)
# ---------------------------------------------------------------------------
def test_same_person_stay_no_repeat_alerts(monkeypatch):
    processor, store, alerts, _ = _build_processor()
    processor.cloudinary = MagicMock(available=False)
    monkeypatch.setattr(alerts, "send_email", lambda *a, **kw: {"sent": True, "skipped": False})

    state = _ChannelMonitorState(channel=7)
    base_utc = datetime(2026, 11, 21, 18, 0, 0, tzinfo=UTC)  # 23:00 PKT (after hours)
    _seed_empty_room(processor, 7, state, base_utc - timedelta(seconds=5), color=(20, 20, 20))
    frame = _make_frame((200, 180, 160))

    clock = [1000.0]
    processor._monotonic = lambda: clock[0]

    # Simulate 20 minutes (1200 seconds) with person_count=1 every 2 seconds
    for step in range(601):
        clock[0] = 1000.0 + step * 2.0
        event_at = base_utc + timedelta(seconds=step * 2)
        processor._handle_warehouse_channel(7, state, frame, _detection(1), detected_at=event_at)

    assert processor.wait_for_dispatches(timeout=5.0)
    processor.shutdown()

    assert state.active_occupants == 1
    assert len(store.entries) == 1
    assert len(store.alerts) == 1
    assert "1 person detected" in store.alerts[0].message
    assert "follow-up" not in store.alerts[0].message.lower()


# ---------------------------------------------------------------------------
# TEST 3: Second person enters while first is still inside (1 -> 2)
# ---------------------------------------------------------------------------
def test_second_person_enters_alerts_delta_one_not_two(monkeypatch):
    processor, store, alerts, _ = _build_processor()
    processor.cloudinary = MagicMock(available=False)
    monkeypatch.setattr(alerts, "send_email", lambda *a, **kw: {"sent": True, "skipped": False})

    state = _ChannelMonitorState(channel=7)
    base_utc = datetime(2026, 11, 21, 18, 0, 0, tzinfo=UTC)
    _seed_empty_room(processor, 7, state, base_utc - timedelta(seconds=5), color=(20, 20, 20))
    frame = _make_frame((200, 180, 160))
    clock = [100.0]
    processor._monotonic = lambda: clock[0]

    # 0 -> 1
    _confirm_entry(processor, 7, state, frame, _detection(1), detected_at=base_utc)
    # Keep 1 present for 5 minutes
    for s in range(1, 150):
        clock[0] = 100.0 + s * 2.0
        processor._handle_warehouse_channel(
            7, state, frame, _detection(1), detected_at=base_utc + timedelta(seconds=s * 2)
        )
    # 1 -> 2 (2nd person walks in, changing pixels inside person 2's bounding box)
    frame_two = _make_frame((40, 210, 60))
    clock[0] = 500.0
    _confirm_entry(
        processor, 7, state, frame_two, _detection(2), detected_at=base_utc + timedelta(seconds=400)
    )
    # Keep 2 present
    clock[0] = 600.0
    processor._handle_warehouse_channel(
        7, state, frame_two, _detection(2), detected_at=base_utc + timedelta(seconds=500)
    )

    assert processor.wait_for_dispatches(timeout=5.0)
    processor.shutdown()

    assert state.active_occupants == 2
    assert len(store.alerts) == 2
    assert "1 person detected" in store.alerts[0].message
    assert "1 person detected" in store.alerts[1].message
    assert "2 people detected" not in store.alerts[1].message


# ---------------------------------------------------------------------------
# TEST 4: Brief occlusion / detector flicker (1 -> 0 for 2s -> 1, 2 -> 1 for 2s -> 2)
# ---------------------------------------------------------------------------
def test_brief_occlusion_and_flicker_produce_zero_duplicate_alerts(monkeypatch):
    processor, store, alerts, _ = _build_processor()
    processor.cloudinary = MagicMock(available=False)
    monkeypatch.setattr(alerts, "send_email", lambda *a, **kw: {"sent": True, "skipped": False})

    state = _ChannelMonitorState(channel=7)
    base_utc = datetime(2026, 11, 21, 18, 0, 0, tzinfo=UTC)
    _seed_empty_room(processor, 7, state, base_utc - timedelta(seconds=5), color=(20, 20, 20))
    frame = _make_frame((200, 180, 160))
    clock = [10.0]
    processor._monotonic = lambda: clock[0]

    # 0 -> 1 (Alert #1)
    _confirm_entry(processor, 7, state, frame, _detection(1), detected_at=base_utc)
    assert state.active_occupants == 1

    # 1 -> 0 for 2s -> 1 (brief occlusion inside 8s grace window)
    clock[0] = 12.0
    processor._handle_warehouse_channel(7, state, frame, _detection(0), detected_at=base_utc + timedelta(seconds=2))
    clock[0] = 14.0
    processor._handle_warehouse_channel(7, state, frame, _detection(0), detected_at=base_utc + timedelta(seconds=4))
    clock[0] = 14.5
    processor._handle_warehouse_channel(7, state, frame, _detection(1), detected_at=base_utc + timedelta(seconds=4.5))
    assert state.active_occupants == 1

    # 1 -> 2 (Alert #2 — 2nd person actually walks into the scene)
    frame_two = _make_frame((40, 210, 60))
    clock[0] = 20.0
    _confirm_entry(processor, 7, state, frame_two, _detection(2), detected_at=base_utc + timedelta(seconds=10))
    assert state.active_occupants == 2

    # 2 -> 1 for 2s -> 2 (brief occlusion of 1 of 2 occupants)
    clock[0] = 22.0
    processor._handle_warehouse_channel(7, state, frame, _detection(1), detected_at=base_utc + timedelta(seconds=12))
    clock[0] = 24.0
    processor._handle_warehouse_channel(7, state, frame, _detection(1), detected_at=base_utc + timedelta(seconds=14))
    clock[0] = 24.5
    processor._handle_warehouse_channel(7, state, frame, _detection(2), detected_at=base_utc + timedelta(seconds=14.5))
    assert state.active_occupants == 2

    # Adversarial case: Person 1 leaves (count=1 for 7s), Person 2 flickers to 0 at T=7.5s,
    # then back to 1 at T=8.5s -> active_occupants must drop to 1 (NOT 0!) so 0 false re-alert!
    clock[0] = 30.0
    processor._handle_warehouse_channel(7, state, frame, _detection(1), detected_at=base_utc + timedelta(seconds=20))
    clock[0] = 37.5
    processor._handle_warehouse_channel(7, state, frame, _detection(0), detected_at=base_utc + timedelta(seconds=27.5))
    clock[0] = 38.5
    processor._handle_warehouse_channel(7, state, frame, _detection(1), detected_at=base_utc + timedelta(seconds=28.5))
    assert state.active_occupants == 1

    assert processor.wait_for_dispatches(timeout=5.0)
    processor.shutdown()
    assert len(store.alerts) == 2


# ---------------------------------------------------------------------------
# TEST 5: True departure + re-entry (1 -> 0 past grace window -> 1)
# ---------------------------------------------------------------------------
def test_true_departure_and_reentry_alerts_immediately(monkeypatch):
    processor, store, alerts, _ = _build_processor()
    processor.cloudinary = MagicMock(available=False)
    monkeypatch.setattr(alerts, "send_email", lambda *a, **kw: {"sent": True, "skipped": False})

    state = _ChannelMonitorState(channel=7)
    base_utc = datetime(2026, 11, 21, 18, 0, 0, tzinfo=UTC)
    empty = _seed_empty_room(processor, 7, state, base_utc - timedelta(seconds=5), color=(20, 20, 20))
    frame = _make_frame((200, 180, 160))
    clock = [50.0]
    processor._monotonic = lambda: clock[0]

    # Entry #1
    _confirm_entry(processor, 7, state, frame, _detection(1), detected_at=base_utc)
    assert state.active_occupants == 1

    # Depart and remain empty past 8s grace window
    clock[0] = 52.0
    processor._handle_warehouse_channel(7, state, empty, _detection(0), detected_at=base_utc + timedelta(seconds=2))
    assert state.active_occupants == 1  # Not yet expired
    clock[0] = 60.5  # 8.5s elapsed since 52.0 >= 8.0s
    processor._handle_warehouse_channel(7, state, empty, _detection(0), detected_at=base_utc + timedelta(seconds=10.5))
    assert state.active_occupants == 0

    # Re-enter -> immediate fresh alert
    clock[0] = 61.0
    _confirm_entry(processor, 7, state, frame, _detection(1), detected_at=base_utc + timedelta(seconds=11))
    assert state.active_occupants == 1

    assert processor.wait_for_dispatches(timeout=5.0)
    processor.shutdown()

    assert len(store.alerts) == 2
    assert "1 person detected" in store.alerts[0].message
    assert "1 person detected" in store.alerts[1].message


# ---------------------------------------------------------------------------
# TEST 6: Simultaneous multi-entry (0 -> 2)
# ---------------------------------------------------------------------------
def test_simultaneous_multi_entry_says_two_people_detected(monkeypatch):
    processor, store, alerts, _ = _build_processor()
    processor.cloudinary = MagicMock(available=False)
    monkeypatch.setattr(alerts, "send_email", lambda *a, **kw: {"sent": True, "skipped": False})

    state = _ChannelMonitorState(channel=7)
    base_utc = datetime(2026, 11, 21, 18, 0, 0, tzinfo=UTC)
    _seed_empty_room(processor, 7, state, base_utc - timedelta(seconds=5), color=(20, 20, 20))
    frame = _make_frame((200, 180, 160))

    _confirm_entry(processor, 7, state, frame, _detection(2), detected_at=base_utc)
    assert processor.wait_for_dispatches(timeout=5.0)
    processor.shutdown()

    assert state.active_occupants == 2
    assert len(store.entries) == 1
    assert store.entries[0].person_count == 2
    assert len(store.alerts) == 1
    assert "2 people detected" in store.alerts[0].message
    assert "person(s)" not in store.alerts[0].message


# ---------------------------------------------------------------------------
# TEST 7: Empty room / Rekognition unavailable or error -> 0 alerts, no HOG fallback
# ---------------------------------------------------------------------------
def test_rekognition_unavailable_or_error_never_falls_back_to_hog(monkeypatch):
    processor, store, alerts, _ = _build_processor()
    processor.cloudinary = MagicMock(available=False)
    monkeypatch.setattr(alerts, "send_email", lambda *a, **kw: {"sent": True, "skipped": False})

    # Verify HOG fallback was completely deleted from SurveillanceProcessor
    assert not hasattr(processor, "_hog_detect")
    assert not hasattr(processor, "_hog")

    # Cluttered room-like noisy frame (would frequently trigger OpenCV HOG)
    rng = np.random.default_rng(42)
    cluttered_frame = rng.integers(0, 255, size=(360, 640, 3), dtype=np.uint8)
    state = _ChannelMonitorState(channel=7)
    base_utc = datetime(2026, 11, 21, 18, 0, 0, tzinfo=UTC)

    # Case A: Rekognition unavailable
    fake_vision = MagicMock()
    fake_vision.available = False
    processor.vision = fake_vision

    result_unavail = processor._detect(cluttered_frame)
    assert result_unavail["error"] is True
    assert result_unavail["person_count"] == 0
    assert result_unavail["source"] == "aws-rekognition-unavailable"

    processor._analyse_channel(7, state, cluttered_frame, detected_at=base_utc)
    assert state.active_occupants == 0
    assert len(store.alerts) == 0

    # Case B: Rekognition configured but raises/returns None
    fake_vision.available = True
    fake_vision.detect_persons.return_value = None
    result_err = processor._detect(cluttered_frame)
    assert result_err["error"] is True
    assert result_err["person_count"] == 0
    assert result_err["source"] == "aws-rekognition-error"

    processor._analyse_channel(7, state, cluttered_frame, detected_at=base_utc + timedelta(seconds=1))
    assert processor.wait_for_dispatches(timeout=5.0)
    assert len(store.alerts) == 0

    # Case C: Transient Rekognition error while a person is ALREADY inside must NOT reset active_occupants
    _seed_empty_room(processor, 7, state, base_utc + timedelta(seconds=1), color=(20, 20, 20))
    fake_vision.detect_persons.return_value = _detection(1)
    processor._analyse_channel(7, state, cluttered_frame, detected_at=base_utc + timedelta(seconds=2))
    processor._analyse_channel(7, state, cluttered_frame, detected_at=base_utc + timedelta(seconds=2, milliseconds=250))
    assert state.active_occupants == 1

    fake_vision.detect_persons.return_value = None
    processor._analyse_channel(7, state, cluttered_frame, detected_at=base_utc + timedelta(seconds=4))
    assert state.active_occupants == 1  # Preserved during transient error

    fake_vision.detect_persons.return_value = _detection(1)
    processor._analyse_channel(7, state, cluttered_frame, detected_at=base_utc + timedelta(seconds=6))
    assert processor.wait_for_dispatches(timeout=5.0)
    processor.shutdown()
    assert len(store.alerts) == 1  # Zero duplicate alert on recovery!


def test_aws_rekognition_filtering_rejects_artifacts_accepts_real_person():
    cfg = _make_settings(rekognition_min_confidence=85.0)
    service = AWSRekognitionService(settings=cfg)

    fake_client = MagicMock()
    fake_client.detect_labels.return_value = {
        "Labels": [
            {
                "Name": "Person",
                "Confidence": 95.0,
                "Instances": [
                    # Tiny reflection sliver / shelf figurine (below MIN_BOX_WIDTH=0.05 / MIN_BOX_HEIGHT=0.14)
                    {"Confidence": 96.0, "BoundingBox": {"Left": 0.1, "Top": 0.1, "Width": 0.04, "Height": 0.12}},
                    # Very low confidence noise (below OCCUPANT_MIN_CONFIDENCE=65%)
                    {"Confidence": 58.0, "BoundingBox": {"Left": 0.2, "Top": 0.2, "Width": 0.15, "Height": 0.40}},
                    # Seated person in glare (between 65% occupant floor and 85% entry floor -> kept)
                    {"Confidence": 72.0, "BoundingBox": {"Left": 0.65, "Top": 0.25, "Width": 0.14, "Height": 0.40}},
                    # Wide horizontal shelf bar (bad aspect ratio > 2.5)
                    {"Confidence": 92.0, "BoundingBox": {"Left": 0.1, "Top": 0.5, "Width": 0.60, "Height": 0.15}},
                    # Full-frame dark wall hallucination (area > 0.92)
                    {"Confidence": 91.0, "BoundingBox": {"Left": 0.0, "Top": 0.0, "Width": 0.99, "Height": 0.98}},
                    # Real person entering room (>= 85%)
                    {"Confidence": 93.4, "BoundingBox": {"Left": 0.4, "Top": 0.2, "Width": 0.14, "Height": 0.48}},
                ],
            }
        ]
    }

    with patch("services.aws_rekognition._get_client", return_value=fake_client):
        result = service.detect_persons(b"\xff\xd8\xffdummy")

    assert fake_client.detect_labels.call_args.kwargs["MinConfidence"] == 65.0
    assert result is not None
    assert result["person_count"] == 2
    scores = sorted(p["score"] for p in result["persons"])
    assert scores == [0.72, 0.934]


# ---------------------------------------------------------------------------
# TEST 8: Timezone consistency (10:28:39 PKT == 05:28:39 UTC) & after-hours defaults
# ---------------------------------------------------------------------------
def test_timezone_consistency_and_after_hours_window(monkeypatch):
    monkeypatch.setenv("APP_TIMEZONE", "Asia/Karachi")
    cfg = _make_settings()
    store = InMemoryStore()
    owner_settings = SettingsService(store, cfg)
    alerts = AlertSystem(store, cfg, owner_settings)

    captured_email: dict[str, str] = {}

    def capture_email(subject: str, html_body: str, to_email=None, text_body=None):
        captured_email["subject"] = subject
        captured_email["html"] = html_body
        return {"sent": True, "skipped": False, "to": "owner@test.local"}

    monkeypatch.setattr(alerts, "send_email", capture_email)

    # 10:28:39 PKT == 05:28:39 UTC
    detected_utc = datetime(2026, 11, 21, 5, 28, 39, tzinfo=UTC)

    # Verify default after-hours window (21:00 - 06:00) treats 10:28:39 PKT as daytime
    assert cfg.after_hours_start == "21:00"
    assert cfg.after_hours_end == "06:00"
    assert is_after_hours(cfg.after_hours_start, cfg.after_hours_end, now=detected_utc) is False
    assert is_after_hours(cfg.after_hours_start, cfg.after_hours_end, now=datetime(2026, 11, 21, 17, 15, 0, tzinfo=UTC)) is True  # 22:15 PKT
    assert is_after_hours(cfg.after_hours_start, cfg.after_hours_end, now=datetime(2026, 11, 21, 0, 30, 0, tzinfo=UTC)) is True   # 05:30 PKT

    entry = Entry(
        channel=7,
        authorized=False,
        after_hours=True,
        person_count=1,
        entry_location="Lounge 2",
        snapshot="https://cdn.test/snap.jpg",
        created_at=detected_utc,
    )
    store.insert_entry(entry)
    alert = alerts.create_warehouse_alert(
        entry,
        snapshot_url="https://cdn.test/snap.jpg",
        detected_at=detected_utc,
    )

    # Message body, email HTML, and serialized API timestamp must all represent 10:28:39 PKT
    assert "21 Nov 2026, 10:28:39" in alert.message
    assert "05:28" not in alert.message
    assert "21 Nov 2026, 10:28:39" in captured_email["html"]
    assert "05:28" not in captured_email["html"]

    api_payload = alert.to_api()
    assert api_payload["created_at"] == "2026-11-21T05:28:39+00:00"
    assert to_local(alert.created_at).strftime("%H:%M:%S") == "10:28:39"
    assert format_local_datetime(alert.created_at) == "21 Nov 2026, 10:28:39"


# ---------------------------------------------------------------------------
# TEST 9: Location accuracy (CH 07 = Lounge 2, never main_gate or person(s))
# ---------------------------------------------------------------------------
def test_location_accuracy_and_no_main_gate(monkeypatch):
    monkeypatch.setenv("APP_TIMEZONE", "Asia/Karachi")
    processor, store, alerts, _ = _build_processor(
        cameras=[
            {"channel": 2, "name": "Camera 02", "location": "", "enabled": True},
            {"channel": 7, "name": "Camera 07", "location": "Lounge 2", "enabled": True},
        ]
    )
    processor.cloudinary = MagicMock(available=False)

    emails: list[dict[str, str]] = []
    monkeypatch.setattr(
        alerts,
        "send_email",
        lambda subject, html_body, **kw: (emails.append({"subject": subject, "html": html_body}) or {"sent": True, "skipped": False}),
    )

    frame = _make_frame((200, 180, 160))
    night_utc = datetime(2026, 11, 21, 18, 0, 0, tzinfo=UTC)

    # CH 07 configured as Lounge 2
    state_7 = _ChannelMonitorState(channel=7)
    _seed_empty_room(processor, 7, state_7, night_utc - timedelta(seconds=5), color=(20, 20, 20))
    _confirm_entry(processor, 7, state_7, frame, _detection(1), detected_at=night_utc)

    # CH 02 with NO custom location configured
    state_2 = _ChannelMonitorState(channel=2)
    _seed_empty_room(processor, 2, state_2, night_utc - timedelta(seconds=5), color=(20, 20, 20))
    _confirm_entry(processor, 2, state_2, frame, _detection(1), detected_at=night_utc + timedelta(seconds=1))

    assert processor.wait_for_dispatches(timeout=5.0)
    processor.shutdown()

    alert_by_ch = {a.channel: a for a in store.alerts}
    assert 7 in alert_by_ch and 2 in alert_by_ch

    # CH 07 assertions
    a7 = alert_by_ch[7]
    assert "Lounge 2" in a7.title
    assert "Lounge 2 (CH 07)" in a7.message
    assert "1 person detected" in a7.message
    assert "main_gate" not in a7.message.lower()
    assert "person(s)" not in a7.message
    assert a7.metadata.get("location") == "Lounge 2"
    email_7 = next(e for e in emails if "Lounge 2" in e["subject"])
    assert "Lounge 2 (CH 07)" in email_7["html"]
    assert "main_gate" not in email_7["html"].lower()
    assert "person(s)" not in email_7["html"]

    # CH 02 assertions (unset location must omit "Location:" cleanly)
    a2 = alert_by_ch[2]
    assert "Camera 02" in a2.title
    assert "Camera 02" in a2.message
    assert "Location:" not in a2.message
    assert "main_gate" not in a2.message.lower()
    assert "location" not in a2.metadata


# ---------------------------------------------------------------------------
# TEST 10: Adversarial checks — no green OSD overlay, duplicate property removed,
#          Alert delivery status accuracy, legacy DB sanitization, multi-channel isolation
# ---------------------------------------------------------------------------
def test_frame_grabber_does_not_burn_duplicate_osd():
    cfg = _make_settings(frame_width=320, camera_fps=12)
    grabber = FrameGrabber(7, cfg)

    solid = _make_frame((30, 40, 50), width=320, height=180)

    class FakeCap:
        def __init__(self) -> None:
            self.calls = 0

        def isOpened(self) -> bool:
            return True

        def set(self, *args) -> bool:
            return True

        def grab(self) -> bool:
            self.calls += 1
            time.sleep(0.01)
            return self.calls <= 3

        def retrieve(self):
            return True, solid.copy()

        def release(self) -> None:
            pass

    grabber._cap = FakeCap()
    grabber._running = True
    t = threading.Thread(target=grabber._capture_loop, daemon=True)
    t.start()
    assert grabber.wait_for_frame(timeout=2.0)
    frame = grabber.read_frame()
    grabber.stop()
    t.join(timeout=2.0)

    assert frame is not None
    # Verify exact pixel integrity in top-left where cv2.putText previously burned green text
    assert np.array_equal(frame, solid)


def test_settings_vision_configured_and_alert_status_semantics():
    cfg = _make_settings()
    assert cfg.vision_configured is True
    assert not hasattr(cfg, "warehouse_realert_seconds")

    cfg_off = _make_settings(rekognition_enabled=False)
    assert cfg_off.vision_configured is False

    # Email sent + push skipped must be SENT (not PARTIAL)
    a_sent = Alert(
        type="warehouse_entry",
        title="Test",
        message="Msg",
        delivery={
            "email": {"sent": True, "skipped": False},
            "push": {"sent": False, "skipped": True, "error": "not configured"},
        },
    )
    assert a_sent.status == "SENT"
    assert a_sent.sent_channels == ["email"]

    # Email failed + push skipped must be FAILED
    a_failed = Alert(
        type="warehouse_entry",
        title="Test",
        message="Msg",
        delivery={
            "email": {"sent": False, "skipped": False, "error": "SMTP auth rejected"},
            "push": {"sent": False, "skipped": True, "error": "not configured"},
        },
    )
    assert a_failed.status == "FAILED"
    assert a_failed.sent_channels == []

    # Both skipped must be SKIPPED (not FAILED)
    a_skipped = Alert(
        type="warehouse_entry",
        title="Test",
        message="Msg",
        delivery={
            "email": {"sent": False, "skipped": True, "error": "disabled"},
            "push": {"sent": False, "skipped": True, "error": "disabled"},
        },
    )
    assert a_skipped.status == "SKIPPED"


def test_cleanup_legacy_locations_scrubs_main_gate():
    from scripts.cleanup_legacy_locations import sanitize_database

    class FakeCollection:
        def __init__(self, docs: list[dict[str, Any]]) -> None:
            self.docs = docs

        def find_one(self, query):
            for d in self.docs:
                if all(d.get(k) == v for k, v in query.items()):
                    return d
            return None

        def update_one(self, query, update):
            doc = self.find_one(query)
            if not doc:
                return MagicMock(modified_count=0)
            for k, v in (update.get("$set") or {}).items():
                if "." in k:
                    p1, p2 = k.split(".", 1)
                    doc.setdefault(p1, {})[p2] = v
                else:
                    doc[k] = v
            return MagicMock(modified_count=1)

        def update_many(self, query, update):
            modified = 0
            for doc in self.docs:
                if "entry_location" in query and str(doc.get("entry_location") or "").lower() == "main_gate":
                    doc["entry_location"] = ""
                    modified += 1
                if "metadata.entry_location" in query:
                    meta = doc.get("metadata") or {}
                    if str(meta.get("entry_location") or "").lower() == "main_gate":
                        meta.pop("entry_location", None)
                        modified += 1
            return MagicMock(modified_count=modified)

    fake_db = {
        "settings": FakeCollection(
            [
                {
                    "_id": "owner",
                    "warehouse": {"entry_location": "main_gate"},
                    "cameras": [{"channel": 7, "name": "main_gate", "location": "main_gate"}],
                }
            ]
        ),
        "warehouse_entries": FakeCollection([{"entry_location": "main_gate"}, {"entry_location": "Lounge 2"}]),
        "alerts": FakeCollection([{"metadata": {"entry_location": "main_gate"}}]),
    }

    counts = sanitize_database(fake_db)
    assert counts == {"settings": 1, "entries": 1, "alerts": 1}
    assert fake_db["settings"].docs[0]["warehouse"]["entry_location"] == ""
    assert fake_db["settings"].docs[0]["cameras"][0]["location"] == ""
    assert fake_db["settings"].docs[0]["cameras"][0]["name"] == "Camera 07"
    assert fake_db["warehouse_entries"].docs[0]["entry_location"] == ""
    assert fake_db["warehouse_entries"].docs[1]["entry_location"] == "Lounge 2"
    assert "entry_location" not in fake_db["alerts"].docs[0]["metadata"]


def test_instant_motion_trigger_does_not_wait_for_idle_periodic_interval(monkeypatch):
    """Verify _run_channel detects a rising-edge motion frame immediately without 5s periodic wait."""
    now_local = current_time()
    win_start = (now_local - timedelta(hours=1)).strftime("%H:%M")
    win_end = (now_local + timedelta(hours=2)).strftime("%H:%M")
    processor, store, alerts, _ = _build_processor(
        after_hours_start=win_start,
        after_hours_end=win_end,
    )
    processor.cloudinary = MagicMock(available=False)
    monkeypatch.setattr(alerts, "send_email", lambda *a, **kw: {"sent": True, "skipped": False})

    class ScriptedGrabber:
        def __init__(self) -> None:
            self._cond = threading.Condition()
            self._rev = 0
            self._frame = None
            self._at = None

        def push(self, frame: np.ndarray) -> None:
            with self._cond:
                self._rev += 1
                self._frame = frame.copy()
                self._at = utcnow()
                self._cond.notify_all()

        def wait_for_new_frame(self, last_rev: int, timeout: float = 0.25):
            with self._cond:
                if self._frame is None or self._rev == last_rev:
                    self._cond.wait(timeout=timeout)
                return self._rev, (None if self._frame is None else self._frame.copy()), self._at

        def wait_for_frame(self, timeout: float = 1.5) -> bool:
            with self._cond:
                if self._frame is None:
                    self._cond.wait(timeout=timeout)
                return self._frame is not None

        def status(self) -> dict[str, Any]:
            return {"error": None}

    grabber = ScriptedGrabber()
    processor.manager.get = lambda ch, autostart=True: grabber

    detect_times: list[float] = []
    call_idx = [0]

    def fake_detect(frame):
        call_idx[0] += 1
        detect_times.append(time.monotonic())
        return _detection(1)

    monkeypatch.setattr(processor, "_detect", fake_detect)

    state = _ChannelMonitorState(channel=7)
    worker = threading.Thread(target=processor._run_channel, args=(7, state), daemon=True)
    worker.start()

    try:
        # Frame 1 & 2: empty static room -> ZERO Rekognition calls (only initializes background)
        empty_frame = _make_frame((20, 20, 20))
        grabber.push(empty_frame)
        time.sleep(0.08)
        grabber.push(empty_frame)
        time.sleep(0.08)
        assert len(detect_times) == 0

        # Frame 3: sudden motion (person enters) — must trigger IMMEDIATELY (< 0.5s, 0 idle wait!)
        motion_frame = _make_frame((220, 220, 220))
        motion_pushed_at = time.monotonic()
        grabber.push(motion_frame)

        deadline_1 = time.monotonic() + 1.0
        while len(detect_times) < 1 and time.monotonic() < deadline_1:
            time.sleep(0.02)
        assert len(detect_times) == 1

        # Frame 4: fast confirmation frame (bypasses interval wait due to pending_entry_frames > 0)
        grabber.push(_make_frame((215, 215, 215)))
        deadline_2 = time.monotonic() + 1.0
        while len(detect_times) < 2 and time.monotonic() < deadline_2:
            time.sleep(0.02)
    finally:
        state.stop_event.set()
        worker.join(timeout=2.0)
        assert processor.wait_for_dispatches(timeout=5.0)
        processor.shutdown()

    assert len(detect_times) == 2
    assert (detect_times[0] - motion_pushed_at) < 0.5
    assert len(store.alerts) == 1
    assert "1 person detected" in store.alerts[0].message


def test_multi_channel_occupant_tracking_is_independent(monkeypatch):
    processor, store, alerts, _ = _build_processor()
    processor.cloudinary = MagicMock(available=False)
    monkeypatch.setattr(alerts, "send_email", lambda *a, **kw: {"sent": True, "skipped": False})

    s7 = _ChannelMonitorState(channel=7)
    s4 = _ChannelMonitorState(channel=4)
    frame = _make_frame((200, 180, 160))
    night = datetime(2026, 11, 21, 18, 0, 0, tzinfo=UTC)
    _seed_empty_room(processor, 7, s7, night - timedelta(seconds=5), color=(20, 20, 20))
    _seed_empty_room(processor, 4, s4, night - timedelta(seconds=5), color=(20, 20, 20))

    _confirm_entry(processor, 7, s7, frame, _detection(1), detected_at=night)
    _confirm_entry(processor, 4, s4, frame, _detection(1), detected_at=night + timedelta(seconds=1))
    # CH 07 still has 1 occupant -> no new alert; CH 04 still has 1 -> no new alert
    processor._handle_warehouse_channel(7, s7, frame, _detection(1), detected_at=night + timedelta(seconds=2))
    processor._handle_warehouse_channel(4, s4, frame, _detection(1), detected_at=night + timedelta(seconds=2))

    assert processor.wait_for_dispatches(timeout=5.0)
    processor.shutdown()

    assert s7.active_occupants == 1
    assert s4.active_occupants == 1
    assert len(store.alerts) == 2
    labels = {a.metadata.get("location") for a in store.alerts}
    assert labels == {"Lounge 2", "Storage Hall"}


def test_settings_service_saves_after_hours_and_camera_locations():
    cfg = _make_settings()
    store = InMemoryStore()
    service = SettingsService(store, cfg)

    updated = service.update(
        {
            "warehouse": {"after_hours_start": "22:00", "after_hours_end": "07:00"},
            "cameras": [
                {"channel": 7, "name": "Lounge Cam", "location": "Lounge 2", "enabled": True},
                {"channel": 2, "name": "Front Hall", "location": "", "enabled": False},
            ],
        }
    )

    assert updated["warehouse"]["after_hours_start"] == "22:00"
    assert updated["warehouse"]["after_hours_end"] == "07:00"
    assert "operations_revision" not in updated

    cam_map = {c["channel"]: c for c in updated["cameras"]}
    assert cam_map[7]["name"] == "Lounge Cam"
    assert cam_map[7]["location"] == "Lounge 2"
    assert cam_map[7]["enabled"] is True
    assert cam_map[2]["enabled"] is False
    assert cam_map[2]["location"] == ""


def test_flask_routes_settings_and_alert_filtering():
    from flask import Flask
    from routes.alert import bp as alerts_bp
    from routes.settings import bp as settings_bp

    cfg = _make_settings()
    store = InMemoryStore()
    owner_settings = SettingsService(store, cfg)
    alert_system = AlertSystem(store, cfg, owner_settings)

    captured_filters: dict[str, Any] = {}

    def fake_list_alerts(**kwargs):
        captured_filters.clear()
        captured_filters.update(kwargs)
        return [], 0

    store.list_alerts = fake_list_alerts  # type: ignore[attr-defined]

    app = Flask(__name__)
    app.extensions["store"] = store
    app.extensions["owner_settings"] = owner_settings
    app.extensions["alert_system"] = alert_system
    app.register_blueprint(settings_bp)
    app.register_blueprint(alerts_bp)

    client = app.test_client()

    # PUT /api/settings persists after-hours and per-camera locations
    resp = client.put(
        "/api/settings",
        json={
            "warehouse": {"after_hours_start": "22:30", "after_hours_end": "06:30"},
            "cameras": [
                {"channel": 7, "name": "Lounge 2 Cam", "location": "Lounge 2", "enabled": True}
            ],
        },
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["settings"]["warehouse"]["after_hours_start"] == "22:30"
    assert body["settings"]["warehouse"]["after_hours_end"] == "06:30"
    ch7 = next(c for c in body["settings"]["cameras"] if c["channel"] == 7)
    assert ch7["location"] == "Lounge 2"

    # GET /api/alerts?severity=critical&acknowledged=false passes lowercase severity & bool acknowledged
    resp_alerts = client.get("/api/alerts?severity=critical&acknowledged=false")
    assert resp_alerts.status_code == 200
    assert captured_filters["severity"] == "critical"
    assert captured_filters["acknowledged"] is False

    # Legacy status=pending also resolves to acknowledged=False
    resp_pending = client.get("/api/alerts?status=pending")
    assert resp_pending.status_code == 200
    assert captured_filters["acknowledged"] is False


def test_staggered_two_person_departure_and_reentry(monkeypatch):
    processor, store, alerts, _ = _build_processor()
    processor.cloudinary = MagicMock(available=False)
    monkeypatch.setattr(alerts, "send_email", lambda *a, **kw: {"sent": True, "skipped": False})

    state = _ChannelMonitorState(channel=7)
    night = datetime(2026, 11, 21, 18, 0, 0, tzinfo=UTC)
    empty = _seed_empty_room(processor, 7, state, night - timedelta(seconds=5), color=(20, 20, 20))
    frame = _make_frame((200, 180, 160))
    clock = [0.0]
    processor._monotonic = lambda: clock[0]

    # 2 people enter simultaneously
    _confirm_entry(processor, 7, state, frame, _detection(2), detected_at=night)
    assert state.active_occupants == 2

    # Person 2 leaves at T=10
    clock[0] = 10.0
    processor._handle_warehouse_channel(7, state, frame, _detection(1), detected_at=night + timedelta(seconds=10))
    # Person 1 leaves at T=14
    clock[0] = 14.0
    processor._handle_warehouse_channel(7, state, empty, _detection(0), detected_at=night + timedelta(seconds=14))
    assert state.active_occupants == 2

    # At T=18.5: slot 2 has been gone 8.5s (>= 8s), slot 1 only 4.5s (< 8s) -> drops to 1
    clock[0] = 18.5
    processor._handle_warehouse_channel(7, state, empty, _detection(0), detected_at=night + timedelta(seconds=18.5))
    assert state.active_occupants == 1

    # At T=22.5: slot 1 has been gone 8.5s (>= 8s) -> drops to 0
    clock[0] = 22.5
    processor._handle_warehouse_channel(7, state, empty, _detection(0), detected_at=night + timedelta(seconds=22.5))
    assert state.active_occupants == 0

    # Fresh entry fires immediately
    clock[0] = 23.0
    _confirm_entry(processor, 7, state, frame, _detection(1), detected_at=night + timedelta(seconds=23))
    assert state.active_occupants == 1

    assert processor.wait_for_dispatches(timeout=5.0)
    processor.shutdown()
    assert len(store.alerts) == 2
    assert "2 people detected" in store.alerts[0].message
    assert "1 person detected" in store.alerts[1].message


def test_daytime_disarms_and_resets_occupants_until_next_after_hours(monkeypatch):
    monkeypatch.setenv("APP_TIMEZONE", "Asia/Karachi")
    processor, store, alerts, _ = _build_processor(
        after_hours_start="21:00",
        after_hours_end="06:00",
    )
    processor.cloudinary = MagicMock(available=False)
    monkeypatch.setattr(alerts, "send_email", lambda *a, **kw: {"sent": True, "skipped": False})

    state = _ChannelMonitorState(channel=7)
    empty = _seed_empty_room(
        processor, 7, state, datetime(2026, 11, 21, 0, 59, 40, tzinfo=UTC), color=(20, 20, 20)
    )
    frame = _make_frame((200, 180, 160))

    # 05:59:50 PKT == 00:59:50 UTC (after hours) -> alerts
    _confirm_entry(
        processor, 7, state, frame, _detection(1), detected_at=datetime(2026, 11, 21, 0, 59, 50, tzinfo=UTC)
    )
    assert state.active_occupants == 1

    # 10:28:39 PKT == 05:28:39 UTC (daytime) -> disarms and resets active_occupants, 0 alerts
    processor._handle_warehouse_channel(
        7, state, empty, _detection(1), detected_at=datetime(2026, 11, 21, 5, 28, 39, tzinfo=UTC)
    )
    assert state.active_occupants == 0

    # 21:05:00 PKT == 16:05:00 UTC (after hours again) -> alerts cleanly
    _confirm_entry(
        processor, 7, state, frame, _detection(1), detected_at=datetime(2026, 11, 21, 16, 5, 0, tzinfo=UTC)
    )
    assert state.active_occupants == 1

    assert processor.wait_for_dispatches(timeout=5.0)
    processor.shutdown()
    assert len(store.alerts) == 2


def test_cloudinary_timeout_falls_back_to_local_snapshot_with_exact_timestamp(monkeypatch, tmp_path):
    cfg = _make_settings(upload_dir=tmp_path)
    processor, store, alerts, _ = _build_processor(settings=cfg)
    fake_cloud = MagicMock()
    fake_cloud.available = True
    fake_cloud.upload_frame.return_value = None  # Simulated timeout/failure
    processor.cloudinary = fake_cloud
    monkeypatch.setattr(alerts, "send_email", lambda *a, **kw: {"sent": False, "skipped": False, "error": "SMTP refused"})

    state = _ChannelMonitorState(channel=7)
    exact_t = datetime(2026, 11, 21, 18, 12, 34, tzinfo=UTC)
    _seed_empty_room(processor, 7, state, exact_t - timedelta(seconds=5), color=(20, 20, 20))
    frame = _make_frame((200, 180, 160))

    _confirm_entry(processor, 7, state, frame, _detection(1), detected_at=exact_t)
    assert processor.wait_for_dispatches(timeout=5.0)
    processor.shutdown()

    assert len(store.entries) == 1
    assert len(store.alerts) == 1
    assert store.entries[0].created_at == exact_t
    assert store.alerts[0].created_at == exact_t
    assert store.entries[0].snapshot and store.entries[0].snapshot.startswith("/static/snapshots/")
    assert store.alerts[0].status == "FAILED"
    assert store.alerts[0].delivery["email"]["error"] == "SMTP refused"
    assert store.entries[0].alert_sent is False




def test_api_timestamp_serialization_is_exact_and_never_stale():
    """Alert/Entry.to_api must emit the exact stored instant as UTC ISO-8601 (no day shift)."""
    # Aware UTC, aware +05:00 (same instant), and naive (stored as UTC by Mongo tz_aware=False)
    instant = datetime(2026, 11, 25, 4, 30, 0, tzinfo=UTC)
    pkt = timezone(timedelta(hours=5))
    for created in (instant, instant.astimezone(pkt), instant.replace(tzinfo=None)):
        alert = Alert(type="warehouse_entry", title="t", message="m", created_at=created)
        entry = Entry(channel=7, created_at=created)
        assert alert.to_api()["created_at"] == "2026-11-25T04:30:00+00:00"
        assert entry.to_api()["timestamp"] == "2026-11-25T04:30:00+00:00"

    # Default created_at is "now" (not a stale import-time constant)
    before = utcnow()
    fresh = Alert(type="system", title="t", message="m")
    after = utcnow()
    assert before <= fresh.created_at <= after
    time.sleep(0.01)
    assert Alert(type="system", title="t", message="m").created_at > fresh.created_at

    # Round-trip through from_document keeps the exact instant
    restored = Alert.from_document({"_id": "a" * 24, **fresh.to_document()})
    assert restored.created_at == fresh.created_at


# ---------------------------------------------------------------------------
# ADVERSARIAL TESTS: False-positive rejection, 2-frame confirmation, idle heartbeat,
# bbox metadata, and IPv4 SMTP + Resend HTTPS email fallback
# ---------------------------------------------------------------------------
def test_single_frame_hallucination_blocked(monkeypatch):
    """Single-frame Person hallucination (static or transient 1-frame spike) must create 0 alerts."""
    processor, store, alerts, _ = _build_processor()
    processor.cloudinary = MagicMock(available=False)
    monkeypatch.setattr(alerts, "send_email", lambda *a, **kw: {"sent": True, "skipped": False})

    base_utc = datetime(2026, 11, 21, 18, 0, 0, tzinfo=UTC)

    # Case A: Static empty background + single frame with Person (92% confidence) over static region
    state_a = _ChannelMonitorState(channel=7)
    static_room = _seed_empty_room(processor, 7, state_a, base_utc - timedelta(seconds=5), color=(50, 50, 50))
    processor._handle_warehouse_channel(
        7, state_a, static_room, _detection(1, confidence=0.92), detected_at=base_utc
    )
    assert state_a.active_occupants == 0
    assert state_a.pending_entry_frames == 0

    # Case B: Motion frame 1 with Person (92% confidence), followed by frame 2 with 0 persons
    state_b = _ChannelMonitorState(channel=7)
    _seed_empty_room(processor, 7, state_b, base_utc - timedelta(seconds=5), color=(50, 50, 50))
    spike_frame = _make_frame((200, 180, 160))
    processor._handle_warehouse_channel(
        7, state_b, spike_frame, _detection(1, confidence=0.92), detected_at=base_utc
    )
    assert state_b.pending_entry_frames == 1
    assert state_b.active_occupants == 0

    processor._handle_warehouse_channel(
        7, state_b, static_room, _detection(0), detected_at=base_utc + timedelta(milliseconds=250)
    )
    assert state_b.pending_entry_frames == 0
    assert state_b.active_occupants == 0

    assert processor.wait_for_dispatches(timeout=5.0)
    processor.shutdown()
    assert len(store.alerts) == 0
    assert len(store.entries) == 0


def test_stationary_chair_or_vase_false_positive_rejected(monkeypatch):
    """5 consecutive frames detecting a Person over a static chair/vase region must fire 0 alerts."""
    processor, store, alerts, _ = _build_processor()
    processor.cloudinary = MagicMock(available=False)
    monkeypatch.setattr(alerts, "send_email", lambda *a, **kw: {"sent": True, "skipped": False})

    # Construct a cluttered room frame with a dark "chair" rectangle inside the false-positive box
    cluttered_room = _make_frame((180, 180, 180))
    cluttered_room[90:270, 220:360] = (20, 20, 20)  # Stationary leather chair behind glass
    false_box = {
        "person_count": 1,
        "confidence": 0.92,
        "labels": ["Person", "Furniture", "Chair"],
        "persons": [{"x1": 0.34, "y1": 0.25, "x2": 0.56, "y2": 0.75, "score": 0.92}],
        "source": "aws-rekognition",
    }

    base_utc = datetime(2026, 11, 21, 18, 0, 0, tzinfo=UTC)

    # Scenario 1: No prior empty frame yet — 5 consecutive identical cluttered frames
    state_1 = _ChannelMonitorState(channel=7)
    for i in range(5):
        processor._handle_warehouse_channel(
            7, state_1, cluttered_room.copy(), false_box, detected_at=base_utc + timedelta(milliseconds=250 * i)
        )
    assert state_1.active_occupants == 0

    # Scenario 2: Clean static baseline already established — 5 consecutive false-positive frames
    state_2 = _ChannelMonitorState(channel=7)
    processor._handle_warehouse_channel(
        7, state_2, cluttered_room.copy(), _detection(0), detected_at=base_utc - timedelta(seconds=2)
    )
    for i in range(5):
        processor._handle_warehouse_channel(
            7, state_2, cluttered_room.copy(), false_box, detected_at=base_utc + timedelta(milliseconds=250 * i)
        )
    assert state_2.active_occupants == 0

    assert processor.wait_for_dispatches(timeout=5.0)
    processor.shutdown()
    assert len(store.alerts) == 0
    assert len(store.entries) == 0


def test_real_entry_zero_delay_preserves_frame1_timestamp_and_detections(monkeypatch):
    """Static empty frame -> frame 1 entry at T0 -> frame 2 at T0+250ms fires 1 alert with frame 1, T0, and bbox metadata."""
    processor, store, alerts, _ = _build_processor()
    uploaded_means: list[tuple[int, int, int]] = []

    def capture_upload(frame: np.ndarray, prefix: str = "snapshot", quality=None) -> str:
        uploaded_means.append(tuple(int(round(v)) for v in frame.mean(axis=(0, 1))))
        return f"https://res.cloudinary.com/test/{prefix}.jpg"

    fake_cloud = MagicMock(available=True)
    fake_cloud.upload_frame.side_effect = capture_upload
    processor.cloudinary = fake_cloud
    monkeypatch.setattr(alerts, "send_email", lambda *a, **kw: {"sent": True, "skipped": False})

    state = _ChannelMonitorState(channel=7)
    t0 = datetime(2026, 11, 21, 18, 15, 0, tzinfo=UTC)
    _seed_empty_room(processor, 7, state, t0 - timedelta(seconds=5), color=(25, 25, 25))

    frame_1 = _make_frame((210, 40, 40))  # Distinct blue frame at exact entry moment T0
    frame_2 = _make_frame((40, 210, 40))  # Distinct green frame 250ms later
    det_1 = {
        "person_count": 1,
        "confidence": 0.94,
        "labels": ["Person", "Human", "Clothing"],
        "persons": [{"x1": 0.25, "y1": 0.15, "x2": 0.48, "y2": 0.85, "score": 0.94}],
        "source": "aws-rekognition",
    }
    det_2 = {
        "person_count": 1,
        "confidence": 0.91,
        "labels": ["Person"],
        "persons": [{"x1": 0.27, "y1": 0.16, "x2": 0.50, "y2": 0.86, "score": 0.91}],
        "source": "aws-rekognition",
    }

    processor._handle_warehouse_channel(7, state, frame_1, det_1, detected_at=t0)
    assert len(store.alerts) == 0  # Held for 2nd-frame confirmation
    assert state.pending_entry_frames == 1

    processor._handle_warehouse_channel(
        7, state, frame_2, det_2, detected_at=t0 + timedelta(milliseconds=250)
    )
    assert processor.wait_for_dispatches(timeout=5.0)
    processor.shutdown()

    assert state.active_occupants == 1
    assert len(store.alerts) == 1
    assert len(store.entries) == 1
    assert store.alerts[0].created_at == t0
    assert store.entries[0].created_at == t0
    # Frame 1 (blue) was uploaded, NOT frame 2 (green)
    assert uploaded_means == [(210, 40, 40)]
    # Detections bounding boxes & labels saved in alert.metadata
    meta = store.alerts[0].metadata
    assert meta["detections"] == [{"x1": 0.25, "y1": 0.15, "x2": 0.48, "y2": 0.85, "score": 0.94}]
    assert meta["labels"] == ["Person", "Human", "Clothing"]


def test_high_resolution_camera_and_snapshot_quality(monkeypatch):
    """Verify 1920px Full-HD defaults, no 640px downscale in _detect(), and 92 snapshot quality."""
    import os
    import cv2
    import services.camera_processor as cp_mod

    # Clean env so Settings() tests its true code defaults
    for key in (
        "FRAME_WIDTH",
        "ANALYSIS_WIDTH",
        "JPEG_QUALITY",
        "SNAPSHOT_JPEG_QUALITY",
        "CAMERA_SUBTYPE",
        "REKOGNITION_MAX_CALLS_PER_DAY",
    ):
        monkeypatch.delenv(key, raising=False)

    defaults = Settings()
    assert defaults.frame_width == 1920
    assert defaults.analysis_width == 1920
    assert defaults.jpeg_quality == 85
    assert defaults.snapshot_jpeg_quality == 92
    assert defaults.camera_subtype == 0
    assert defaults.rekognition_max_calls_per_day == 150

    # Verify FFmpeg options omit reorder_queue_size and discardcorrupt
    ffmpeg_opts = os.environ.get("OPENCV_FFMPEG_CAPTURE_OPTIONS", "")
    assert "rtsp_transport;tcp" in ffmpeg_opts
    assert "reorder_queue_size" not in ffmpeg_opts
    assert "discardcorrupt" not in ffmpeg_opts

    # Verify _detect() sends full 1920x1080 frame without downscaling to 640px
    processor, _, _, _ = _build_processor(settings=_make_settings(analysis_width=1920, snapshot_jpeg_quality=92))
    captured_bytes: list[bytes] = []

    fake_vision = MagicMock(available=True)

    def capture_detect(img_bytes: bytes):
        captured_bytes.append(img_bytes)
        return _detection(0)

    fake_vision.detect_persons.side_effect = capture_detect
    processor.vision = fake_vision

    hd_frame = np.full((1080, 1920, 3), (80, 120, 160), dtype=np.uint8)
    processor._detect(hd_frame)
    processor.shutdown()

    assert len(captured_bytes) == 1
    decoded = cv2.imdecode(np.frombuffer(captured_bytes[0], dtype=np.uint8), cv2.IMREAD_COLOR)
    assert decoded is not None
    assert decoded.shape == (1080, 1920, 3)

    # Verify CloudinaryStorage.upload_frame() encodes at snapshot_jpeg_quality (92)
    cfg = _make_settings(jpeg_quality=85, snapshot_jpeg_quality=92)
    with patch("services.cloudinary_storage._init_cloudinary", return_value=True):
        storage = CloudinaryStorage(cfg)
    storage._available = True

    encode_calls: list[list[int]] = []
    real_imencode = cv2.imencode

    def spy_imencode(ext, img, params=None):
        encode_calls.append(list(params or []))
        return real_imencode(ext, img, params or [])

    with (
        patch("services.cloudinary_storage.cv2.imencode", side_effect=spy_imencode),
        patch.object(storage, "upload_bytes", return_value="https://cdn/hd.jpg"),
    ):
        url = storage.upload_frame(hd_frame, prefix="warehouse_ch05")

    assert url == "https://cdn/hd.jpg"
    assert encode_calls == [[cv2.IMWRITE_JPEG_QUALITY, 92]]


def test_cost_cap_zero_idle_calls_and_occupied_heartbeat(monkeypatch):
    """60s static empty room -> 0 calls; 60s static occupied room -> <=1 call; daily cap preserves occupants."""
    from datetime import date
    import services.camera_processor as cp_mod

    # Dead high-cost constants must be completely deleted
    assert not hasattr(cp_mod, "WAREHOUSE_IDLE_PERIODIC_SECONDS")
    assert not hasattr(cp_mod, "WAREHOUSE_IDLE_HEARTBEAT_SECONDS")
    assert not hasattr(cp_mod, "WAREHOUSE_PRESENT_PERIODIC_SECONDS")
    assert cp_mod.WAREHOUSE_OCCUPIED_HEARTBEAT_SECONDS == 45.0
    assert cp_mod.WAREHOUSE_OCCUPIED_MOTION_INTERVAL_SECONDS == 4.0

    now_local = current_time()
    win_start = (now_local - timedelta(hours=1)).strftime("%H:%M")
    win_end = (now_local + timedelta(hours=2)).strftime("%H:%M")

    # Part A: 60 seconds of static empty room -> ZERO Rekognition calls
    processor, _, alerts, _ = _build_processor(after_hours_start=win_start, after_hours_end=win_end)
    processor.cloudinary = MagicMock(available=False)
    monkeypatch.setattr(alerts, "send_email", lambda *a, **kw: {"sent": True, "skipped": False})

    virtual_clock = [1000.0]
    processor._monotonic = lambda: virtual_clock[0]
    static_frame = _make_frame((30, 30, 30))

    class ScriptedStaticGrabber:
        def __init__(self, target_state: _ChannelMonitorState, frames: int) -> None:
            self._state = target_state
            self._frames = frames
            self._rev = 0

        def wait_for_new_frame(self, last_rev: int, timeout: float = 0.25):
            if self._rev >= self._frames:
                self._state.stop_event.set()
                return self._rev, None, None
            self._rev += 1
            virtual_clock[0] = 1000.0 + (self._rev - 1) * 1.0
            return self._rev, static_frame.copy(), utcnow()

        def wait_for_frame(self, timeout: float = 1.0) -> bool:
            return False

        def status(self) -> dict[str, Any]:
            return {"error": None}

    empty_state = _ChannelMonitorState(channel=7)
    processor.manager.get = lambda ch, autostart=True: ScriptedStaticGrabber(empty_state, 61)
    empty_calls = [0]
    monkeypatch.setattr(processor, "_detect", lambda f: (empty_calls.__setitem__(0, empty_calls[0] + 1) or _detection(0)))
    processor._run_channel(7, empty_state)
    assert empty_calls[0] == 0, f"Expected 0 Rekognition calls in 60s static empty room, got {empty_calls[0]}"

    # Part B: 60 seconds of static occupied room (active_occupants == 1) -> at most 1 heartbeat call
    occupied_state = _ChannelMonitorState(channel=7, active_occupants=1)
    virtual_clock[0] = 1000.0
    processor.manager.get = lambda ch, autostart=True: ScriptedStaticGrabber(occupied_state, 61)
    occupied_calls = [0]
    monkeypatch.setattr(processor, "_detect", lambda f: (occupied_calls.__setitem__(0, occupied_calls[0] + 1) or _detection(1)))
    processor._run_channel(7, occupied_state)
    assert occupied_calls[0] <= 1, f"Expected <=1 heartbeat call in 60s static occupied room, got {occupied_calls[0]}"
    processor.shutdown()

    # Part C: Hard daily AWS budget cap blocks calls without resetting active_occupants & resets at UTC midnight
    cfg = _make_settings(rekognition_max_calls_per_day=2)
    service = AWSRekognitionService(settings=cfg)
    day_clock = [date(2026, 11, 25)]
    monkeypatch.setattr(service, "_today_utc", lambda: day_clock[0])

    fake_client = MagicMock()
    fake_client.detect_labels.return_value = {"Labels": []}

    with patch("services.aws_rekognition._get_client", return_value=fake_client):
        r1 = service.detect_persons(b"\xff\xd8\xff1")
        r2 = service.detect_persons(b"\xff\xd8\xff2")
        r3 = service.detect_persons(b"\xff\xd8\xff3")

    assert r1 is not None and not r1.get("error")
    assert r2 is not None and not r2.get("error")
    assert r3 is not None and r3.get("error") is True
    assert r3["source"] == "aws-rekognition-budget-cap"
    assert service.calls_today == 2
    assert service.daily_limit == 2
    assert fake_client.detect_labels.call_count == 2  # 3rd call blocked before AWS!

    # Feeding budget-cap response to warehouse handler must NOT reset active_occupants
    proc2, _, _, _ = _build_processor(settings=cfg)
    st = _ChannelMonitorState(channel=7, active_occupants=3)
    proc2._handle_warehouse_channel(7, st, _make_frame(), r3, detected_at=datetime(2026, 11, 21, 18, 0, 0, tzinfo=UTC))
    assert st.active_occupants == 3
    proc2.shutdown()

    # Next UTC day automatically resets daily budget counter
    day_clock[0] = date(2026, 11, 26)
    assert service.calls_today == 0
    with patch("services.aws_rekognition._get_client", return_value=fake_client):
        r4 = service.detect_persons(b"\xff\xd8\xff4")
    assert r4 is not None and not r4.get("error")
    assert service.calls_today == 1


def test_four_person_room_hysteresis_and_no_phantom_plus_one_alert(monkeypatch):
    """Room 3: 3 people at 92% + 1 seated at 72% -> counts as 4 immediately; later 72%->89% emits 0 duplicate alerts;
    stationary seated shift (3 -> 4 with unchanged pixels) emits 0 phantom +1 alerts."""
    processor, store, alerts, _ = _build_processor()
    processor.cloudinary = MagicMock(available=False)
    monkeypatch.setattr(alerts, "send_email", lambda *a, **kw: {"sent": True, "skipped": False})

    state = _ChannelMonitorState(channel=5)
    t0 = datetime(2026, 11, 21, 18, 0, 0, tzinfo=UTC)
    _seed_empty_room(processor, 5, state, t0 - timedelta(seconds=5), color=(20, 20, 20))

    room_frame = _make_frame((195, 180, 165))
    four_people_initial = {
        "person_count": 4,
        "confidence": 0.87,
        "labels": ["Person", "Sitting"],
        "persons": [
            {"x1": 0.05, "y1": 0.20, "x2": 0.20, "y2": 0.80, "score": 0.92},
            {"x1": 0.25, "y1": 0.20, "x2": 0.40, "y2": 0.80, "score": 0.92},
            {"x1": 0.45, "y1": 0.20, "x2": 0.60, "y2": 0.80, "score": 0.92},
            {"x1": 0.72, "y1": 0.22, "x2": 0.88, "y2": 0.82, "score": 0.72},  # Seated in window glare
        ],
        "source": "aws-rekognition",
    }

    _confirm_entry(processor, 5, state, room_frame, four_people_initial, detected_at=t0)
    assert state.active_occupants == 4

    # 17 seconds later, the 4th seated person lowers his hand -> confidence rises 72% -> 89%
    four_people_later = {
        "person_count": 4,
        "confidence": 0.9125,
        "labels": ["Person", "Sitting"],
        "persons": [
            {"x1": 0.05, "y1": 0.20, "x2": 0.20, "y2": 0.80, "score": 0.92},
            {"x1": 0.25, "y1": 0.20, "x2": 0.40, "y2": 0.80, "score": 0.92},
            {"x1": 0.45, "y1": 0.20, "x2": 0.60, "y2": 0.80, "score": 0.92},
            {"x1": 0.72, "y1": 0.22, "x2": 0.88, "y2": 0.82, "score": 0.89},
        ],
        "source": "aws-rekognition",
    }
    processor._handle_warehouse_channel(5, state, room_frame, four_people_later, detected_at=t0 + timedelta(seconds=17))
    processor._handle_warehouse_channel(
        5, state, room_frame, four_people_later, detected_at=t0 + timedelta(seconds=17, milliseconds=250)
    )

    # Also test stationary seated shift: room starts with 3 counted people, then a 4th stationary
    # seated person's confidence ticks from below 65% to 86% on the EXACT same frame pixels -> 0 phantom alert!
    state_shift = _ChannelMonitorState(channel=5)
    _seed_empty_room(processor, 5, state_shift, t0 - timedelta(seconds=5), color=(20, 20, 20))
    three_people = {
        "person_count": 3,
        "confidence": 0.92,
        "labels": ["Person"],
        "persons": four_people_initial["persons"][:3],
        "source": "aws-rekognition",
    }
    _confirm_entry(processor, 5, state_shift, room_frame, three_people, detected_at=t0)
    assert state_shift.active_occupants == 3
    # Same static frame pixels, 4th seated box suddenly ticks to 86% without walking in
    four_stationary = {
        "person_count": 4,
        "confidence": 0.905,
        "labels": ["Person"],
        "persons": [
            *four_people_initial["persons"][:3],
            {"x1": 0.72, "y1": 0.22, "x2": 0.88, "y2": 0.82, "score": 0.86},
        ],
        "source": "aws-rekognition",
    }
    _confirm_entry(processor, 5, state_shift, room_frame, four_stationary, detected_at=t0 + timedelta(seconds=17))
    assert state_shift.active_occupants == 3  # Stationary posture shift rejected!

    assert processor.wait_for_dispatches(timeout=5.0)
    processor.shutdown()

    ch5_alerts = [a for a in store.alerts if a.channel == 5]
    # 1 alert for the 4-person scenario ("4 people detected") + 1 alert for the 3-person scenario ("3 people detected")
    assert len(ch5_alerts) == 2
    messages = [a.message for a in ch5_alerts]
    assert any("4 people detected" in m for m in messages)
    assert any("3 people detected" in m for m in messages)
    assert not any("1 person detected" in m for m in messages)


def test_email_ipv4_resolution_and_resend_https_fallback(monkeypatch):
    """send_email() must resolve SMTP over AF_INET and fall back to Resend HTTPS API on Errno 101."""
    import socket
    from services import alert_system as alert_mod

    # 1. Verify _create_ipv4_socket forces socket.AF_INET
    addrinfo_calls: list[tuple] = []

    def fake_getaddrinfo(host, port, family=0, socktype=0, *args, **kwargs):
        addrinfo_calls.append((host, port, family, socktype))
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("142.250.1.108", port))]

    fake_sock = MagicMock()
    monkeypatch.setattr(alert_mod.socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(alert_mod.socket, "socket", lambda *a, **kw: fake_sock)

    sock = alert_mod._create_ipv4_socket("smtp.gmail.com", 587, 15)
    assert sock is fake_sock
    assert addrinfo_calls == [("smtp.gmail.com", 587, socket.AF_INET, socket.SOCK_STREAM)]
    fake_sock.connect.assert_called_once_with(("142.250.1.108", 587))

    # 2. Verify SMTP Errno 101 falls back cleanly to Resend HTTPS API (port 443)
    cfg = _make_settings(
        smtp_host="smtp.gmail.com",
        smtp_port=587,
        smtp_secure=False,
        smtp_user="sender@test.local",
        smtp_pass="app-pass",
        mail_from="sender@test.local",
        resend_api_key="re_test_key_123",
    )
    store = InMemoryStore()
    owner_settings = SettingsService(store, cfg)
    alerts_svc = AlertSystem(store, cfg, owner_settings)

    def raise_errno_101(*args, **kwargs):
        raise OSError(101, "Network is unreachable")

    monkeypatch.setattr(alert_mod, "_IPv4SMTP", raise_errno_101)

    resend_requests: list[dict[str, Any]] = []

    class FakeResp:
        content = b'{"id": "email_abc123"}'

        def raise_for_status(self):
            return None

        def json(self):
            return {"id": "email_abc123"}

    def fake_post(url, headers=None, json=None, timeout=None):
        resend_requests.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return FakeResp()

    monkeypatch.setattr(alert_mod.requests, "post", fake_post)

    result = alerts_svc.send_email(
        subject="Intrusion Alert",
        html_body="<p>1 person detected</p>",
        to_email="owner@test.local",
    )
    assert result["sent"] is True
    assert result["provider"] == "resend"
    assert result["id"] == "email_abc123"
    assert len(resend_requests) == 1
    assert resend_requests[0]["url"] == "https://api.resend.com/emails"
    assert resend_requests[0]["headers"]["Authorization"] == "Bearer re_test_key_123"
    assert resend_requests[0]["json"]["to"] == ["owner@test.local"]

    # 3. Verify clean failure without crash when Resend is not configured and SMTP raises Errno 101
    cfg_no_resend = _make_settings(
        smtp_host="smtp.gmail.com",
        smtp_port=587,
        mail_from="sender@test.local",
        resend_api_key="",
    )
    alerts_no_resend = AlertSystem(store, cfg_no_resend, owner_settings)
    fail_res = alerts_no_resend.send_email(
        subject="Intrusion Alert",
        html_body="<p>1 person detected</p>",
        to_email="owner@test.local",
    )
    assert fail_res["sent"] is False
    assert "Network is unreachable" in fail_res["error"]
