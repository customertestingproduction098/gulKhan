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

    try:
        # 1st person enters on a blue-tinted frame at T0
        frame_1 = _make_frame((200, 10, 10))
        t0 = utcnow()
        call_start_1 = time.monotonic()
        processor._handle_warehouse_channel(7, state, frame_1, _detection(1), detected_at=t0)
        elapsed_handle_1 = time.monotonic() - call_start_1
        assert elapsed_handle_1 < 0.25, f"Detection handler blocked for {elapsed_handle_1:.2f}s"

        # Mutate original frame buffer after passing to prove frame.copy() froze the exact frame
        frame_1[:, :] = (0, 0, 0)

        # Wait 0.4s (still deep inside the 3-second Cloudinary upload!) and send 2nd person entering
        time.sleep(0.4)
        frame_2 = _make_frame((10, 210, 10))
        t1 = utcnow()
        call_start_2 = time.monotonic()
        processor._handle_warehouse_channel(7, state, frame_2, _detection(2), detected_at=t1)
        elapsed_handle_2 = time.monotonic() - call_start_2
        assert elapsed_handle_2 < 0.25, f"2nd entry was blocked for {elapsed_handle_2:.2f}s"
        frame_2[:, :] = (0, 0, 0)

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
    frame = _make_frame()
    base_utc = datetime(2026, 11, 21, 18, 0, 0, tzinfo=UTC)  # 23:00 PKT (after hours)

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
    frame = _make_frame()
    base_utc = datetime(2026, 11, 21, 18, 0, 0, tzinfo=UTC)
    clock = [100.0]
    processor._monotonic = lambda: clock[0]

    # 0 -> 1
    processor._handle_warehouse_channel(7, state, frame, _detection(1), detected_at=base_utc)
    # Keep 1 present for 5 minutes
    for s in range(1, 150):
        clock[0] = 100.0 + s * 2.0
        processor._handle_warehouse_channel(
            7, state, frame, _detection(1), detected_at=base_utc + timedelta(seconds=s * 2)
        )
    # 1 -> 2 (2nd person enters)
    clock[0] = 500.0
    processor._handle_warehouse_channel(
        7, state, frame, _detection(2), detected_at=base_utc + timedelta(seconds=400)
    )
    # Keep 2 present
    clock[0] = 600.0
    processor._handle_warehouse_channel(
        7, state, frame, _detection(2), detected_at=base_utc + timedelta(seconds=500)
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
    frame = _make_frame()
    base_utc = datetime(2026, 11, 21, 18, 0, 0, tzinfo=UTC)
    clock = [10.0]
    processor._monotonic = lambda: clock[0]

    # 0 -> 1 (Alert #1)
    processor._handle_warehouse_channel(7, state, frame, _detection(1), detected_at=base_utc)
    assert state.active_occupants == 1

    # 1 -> 0 for 2s -> 1 (brief occlusion inside 8s grace window)
    clock[0] = 12.0
    processor._handle_warehouse_channel(7, state, frame, _detection(0), detected_at=base_utc + timedelta(seconds=2))
    clock[0] = 14.0
    processor._handle_warehouse_channel(7, state, frame, _detection(0), detected_at=base_utc + timedelta(seconds=4))
    clock[0] = 14.5
    processor._handle_warehouse_channel(7, state, frame, _detection(1), detected_at=base_utc + timedelta(seconds=4.5))
    assert state.active_occupants == 1

    # 1 -> 2 (Alert #2)
    clock[0] = 20.0
    processor._handle_warehouse_channel(7, state, frame, _detection(2), detected_at=base_utc + timedelta(seconds=10))
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
    frame = _make_frame()
    base_utc = datetime(2026, 11, 21, 18, 0, 0, tzinfo=UTC)
    clock = [50.0]
    processor._monotonic = lambda: clock[0]

    # Entry #1
    processor._handle_warehouse_channel(7, state, frame, _detection(1), detected_at=base_utc)
    assert state.active_occupants == 1

    # Depart and remain empty past 8s grace window
    clock[0] = 52.0
    processor._handle_warehouse_channel(7, state, frame, _detection(0), detected_at=base_utc + timedelta(seconds=2))
    assert state.active_occupants == 1  # Not yet expired
    clock[0] = 60.5  # 8.5s elapsed since 52.0 >= 8.0s
    processor._handle_warehouse_channel(7, state, frame, _detection(0), detected_at=base_utc + timedelta(seconds=10.5))
    assert state.active_occupants == 0

    # Re-enter -> immediate fresh alert
    clock[0] = 61.0
    processor._handle_warehouse_channel(7, state, frame, _detection(1), detected_at=base_utc + timedelta(seconds=11))
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
    frame = _make_frame()
    base_utc = datetime(2026, 11, 21, 18, 0, 0, tzinfo=UTC)

    processor._handle_warehouse_channel(7, state, frame, _detection(2), detected_at=base_utc)
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
    fake_vision.detect_persons.return_value = _detection(1)
    processor._analyse_channel(7, state, cluttered_frame, detected_at=base_utc + timedelta(seconds=2))
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
                    # Tiny reflection sliver (too small)
                    {"Confidence": 96.0, "BoundingBox": {"Left": 0.1, "Top": 0.1, "Width": 0.03, "Height": 0.06}},
                    # Low confidence artifact (below 85%)
                    {"Confidence": 78.0, "BoundingBox": {"Left": 0.2, "Top": 0.2, "Width": 0.15, "Height": 0.40}},
                    # Wide horizontal shelf bar (bad aspect ratio > 2.5)
                    {"Confidence": 92.0, "BoundingBox": {"Left": 0.1, "Top": 0.5, "Width": 0.60, "Height": 0.15}},
                    # Full-frame dark wall hallucination (area > 0.92)
                    {"Confidence": 91.0, "BoundingBox": {"Left": 0.0, "Top": 0.0, "Width": 0.99, "Height": 0.98}},
                    # Real person entering room
                    {"Confidence": 93.4, "BoundingBox": {"Left": 0.4, "Top": 0.2, "Width": 0.14, "Height": 0.48}},
                ],
            }
        ]
    }

    with patch("services.aws_rekognition._get_client", return_value=fake_client):
        result = service.detect_persons(b"\xff\xd8\xffdummy")

    assert result is not None
    assert result["person_count"] == 1
    assert len(result["persons"]) == 1
    assert result["persons"][0]["score"] == 0.934


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

    frame = _make_frame()
    night_utc = datetime(2026, 11, 21, 18, 0, 0, tzinfo=UTC)

    # CH 07 configured as Lounge 2
    state_7 = _ChannelMonitorState(channel=7)
    processor._handle_warehouse_channel(7, state_7, frame, _detection(1), detected_at=night_utc)

    # CH 02 with NO custom location configured
    state_2 = _ChannelMonitorState(channel=2)
    processor._handle_warehouse_channel(2, state_2, frame, _detection(1), detected_at=night_utc + timedelta(seconds=1))

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
        # 1st call = empty static room; 2nd call = person walks in on motion frame!
        return _detection(0) if call_idx[0] == 1 else _detection(1)

    monkeypatch.setattr(processor, "_detect", fake_detect)

    state = _ChannelMonitorState(channel=7)
    worker = threading.Thread(target=processor._run_channel, args=(7, state), daemon=True)
    worker.start()

    try:
        # Frame 1: empty room (runs initial detection immediately upon arrival)
        empty_frame = _make_frame((20, 20, 20))
        grabber.push(empty_frame)
        deadline_1 = time.monotonic() + 1.0
        while len(detect_times) < 1 and time.monotonic() < deadline_1:
            time.sleep(0.02)
        assert len(detect_times) == 1

        # Frame 2: identical static frame (no motion -> sets last_had_motion=False)
        grabber.push(empty_frame)
        time.sleep(0.10)
        assert len(detect_times) == 1

        # Frame 3: sudden motion (person enters) — must trigger IMMEDIATELY (< 0.5s, NOT 5.0s!)
        motion_frame = _make_frame((220, 220, 220))
        motion_pushed_at = time.monotonic()
        grabber.push(motion_frame)

        deadline_2 = time.monotonic() + 1.0
        while len(detect_times) < 2 and time.monotonic() < deadline_2:
            time.sleep(0.02)
    finally:
        state.stop_event.set()
        worker.join(timeout=2.0)
        assert processor.wait_for_dispatches(timeout=5.0)
        processor.shutdown()

    assert len(detect_times) == 2
    assert (detect_times[1] - motion_pushed_at) < 0.5
    assert len(store.alerts) == 1
    assert "1 person detected" in store.alerts[0].message


def test_multi_channel_occupant_tracking_is_independent(monkeypatch):
    processor, store, alerts, _ = _build_processor()
    processor.cloudinary = MagicMock(available=False)
    monkeypatch.setattr(alerts, "send_email", lambda *a, **kw: {"sent": True, "skipped": False})

    s7 = _ChannelMonitorState(channel=7)
    s4 = _ChannelMonitorState(channel=4)
    frame = _make_frame()
    night = datetime(2026, 11, 21, 18, 0, 0, tzinfo=UTC)

    processor._handle_warehouse_channel(7, s7, frame, _detection(1), detected_at=night)
    processor._handle_warehouse_channel(4, s4, frame, _detection(1), detected_at=night + timedelta(seconds=1))
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
    frame = _make_frame()
    night = datetime(2026, 11, 21, 18, 0, 0, tzinfo=UTC)
    clock = [0.0]
    processor._monotonic = lambda: clock[0]

    # 2 people enter simultaneously
    processor._handle_warehouse_channel(7, state, frame, _detection(2), detected_at=night)
    assert state.active_occupants == 2

    # Person 2 leaves at T=10
    clock[0] = 10.0
    processor._handle_warehouse_channel(7, state, frame, _detection(1), detected_at=night + timedelta(seconds=10))
    # Person 1 leaves at T=14
    clock[0] = 14.0
    processor._handle_warehouse_channel(7, state, frame, _detection(0), detected_at=night + timedelta(seconds=14))
    assert state.active_occupants == 2

    # At T=18.5: slot 2 has been gone 8.5s (>= 8s), slot 1 only 4.5s (< 8s) -> drops to 1
    clock[0] = 18.5
    processor._handle_warehouse_channel(7, state, frame, _detection(0), detected_at=night + timedelta(seconds=18.5))
    assert state.active_occupants == 1

    # At T=22.5: slot 1 has been gone 8.5s (>= 8s) -> drops to 0
    clock[0] = 22.5
    processor._handle_warehouse_channel(7, state, frame, _detection(0), detected_at=night + timedelta(seconds=22.5))
    assert state.active_occupants == 0

    # Fresh entry fires immediately
    clock[0] = 23.0
    processor._handle_warehouse_channel(7, state, frame, _detection(1), detected_at=night + timedelta(seconds=23))
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
    frame = _make_frame()

    # 05:59:50 PKT == 00:59:50 UTC (after hours) -> alerts
    processor._handle_warehouse_channel(
        7, state, frame, _detection(1), detected_at=datetime(2026, 11, 21, 0, 59, 50, tzinfo=UTC)
    )
    assert state.active_occupants == 1

    # 10:28:39 PKT == 05:28:39 UTC (daytime) -> disarms and resets active_occupants, 0 alerts
    processor._handle_warehouse_channel(
        7, state, frame, _detection(1), detected_at=datetime(2026, 11, 21, 5, 28, 39, tzinfo=UTC)
    )
    assert state.active_occupants == 0

    # 21:05:00 PKT == 16:05:00 UTC (after hours again) -> alerts cleanly
    processor._handle_warehouse_channel(
        7, state, frame, _detection(1), detected_at=datetime(2026, 11, 21, 16, 5, 0, tzinfo=UTC)
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
    frame = _make_frame((90, 90, 90))
    exact_t = datetime(2026, 11, 21, 18, 12, 34, tzinfo=UTC)

    processor._handle_warehouse_channel(7, state, frame, _detection(1), detected_at=exact_t)
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
