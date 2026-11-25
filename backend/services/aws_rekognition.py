"""AWS Rekognition client for person detection.

Wraps boto3 Rekognition calls with timeouts, error handling, and a consistent
return shape so callers don't need to know about the provider.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
import logging
import threading
from typing import Any

import boto3
from botocore.config import Config

from config.settings import get_settings

logger = logging.getLogger(__name__)

ENTRY_MIN_CONFIDENCE = 85.0
OCCUPANT_MIN_CONFIDENCE = 65.0
DEFAULT_MIN_CONFIDENCE = ENTRY_MIN_CONFIDENCE
DEFAULT_MAX_CALLS_PER_DAY = 150
MIN_BOX_WIDTH = 0.05
MIN_BOX_HEIGHT = 0.14
MIN_BOX_AREA = 0.008
MAX_BOX_AREA = 0.92
MIN_ASPECT_RATIO = 0.12
MAX_ASPECT_RATIO = 2.5

_client = None
_lock = threading.Lock()


def _get_client():
    global _client
    with _lock:
        if _client is not None:
            return _client
        settings = get_settings()
        cfg = Config(
            connect_timeout=3,
            read_timeout=6,
            max_pool_connections=25,
            retries={"max_attempts": 1},
        )
        session_kwargs = {
            "region_name": settings.aws_region or "ap-south-1",
        }
        if settings.aws_access_key_id and settings.aws_secret_access_key:
            session_kwargs["aws_access_key_id"] = settings.aws_access_key_id
            session_kwargs["aws_secret_access_key"] = settings.aws_secret_access_key
        session = boto3.session.Session(**session_kwargs)
        _client = session.client("rekognition", config=cfg)
        return _client


class AWSRekognitionService:
    """Detect people in a JPEG frame using AWS Rekognition with a daily budget cap."""

    def __init__(self, settings=None) -> None:
        self.settings = settings or get_settings()
        self._error: str | None = None
        self._budget_lock = threading.Lock()
        self._calls_date: date = self._today_utc()
        self._calls_today: int = 0
        self._cap_warned_date: date | None = None

    @staticmethod
    def _today_utc() -> date:
        return datetime.now(timezone.utc).date()

    @property
    def daily_limit(self) -> int:
        raw = getattr(self.settings, "rekognition_max_calls_per_day", DEFAULT_MAX_CALLS_PER_DAY)
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            return DEFAULT_MAX_CALLS_PER_DAY

    @property
    def calls_today(self) -> int:
        today = self._today_utc()
        with self._budget_lock:
            if self._calls_date != today:
                self._calls_date = today
                self._calls_today = 0
                self._cap_warned_date = None
            return self._calls_today

    def _consume_budget(self) -> bool:
        """Increment daily call count if under cap; return False when daily cap is reached."""
        today = self._today_utc()
        limit = self.daily_limit
        with self._budget_lock:
            if self._calls_date != today:
                self._calls_date = today
                self._calls_today = 0
                self._cap_warned_date = None
            if limit > 0 and self._calls_today >= limit:
                if self._cap_warned_date != today:
                    self._cap_warned_date = today
                    logger.warning(
                        "AWS Rekognition daily budget cap reached (%d/%d calls on %s UTC); "
                        "skipping further AWS calls until UTC midnight",
                        self._calls_today,
                        limit,
                        today.isoformat(),
                    )
                return False
            self._calls_today += 1
            return True

    @property
    def available(self) -> bool:
        return bool(
            self.settings.aws_access_key_id
            and self.settings.aws_secret_access_key
            and self.settings.rekognition_enabled
        )

    def detect_persons(self, image_bytes: bytes) -> dict[str, Any] | None:
        if not self.available or not image_bytes:
            return None
        if len(image_bytes) > 5 * 1024 * 1024:
            logger.warning("Image too large for Rekognition (%d bytes)", len(image_bytes))
            return None
        if not self._consume_budget():
            return {
                "person_count": 0,
                "confidence": 0.0,
                "labels": [],
                "persons": [],
                "error": True,
                "source": "aws-rekognition-budget-cap",
            }
        try:
            client = _get_client()
            entry_conf = float(
                getattr(self.settings, "rekognition_min_confidence", ENTRY_MIN_CONFIDENCE)
                or ENTRY_MIN_CONFIDENCE
            )
            query_min_conf = min(OCCUPANT_MIN_CONFIDENCE, entry_conf)
            response = client.detect_labels(
                Image={"Bytes": image_bytes},
                MaxLabels=25,
                MinConfidence=query_min_conf,
            )
            persons: list[dict[str, Any]] = []
            for label in response.get("Labels", []):
                if label.get("Name") != "Person":
                    continue
                for instance in label.get("Instances") or []:
                    box = instance.get("BoundingBox") or {}
                    if not box:
                        continue
                    raw_conf = instance.get("Confidence")
                    if raw_conf is None:
                        raw_conf = label.get("Confidence")
                    inst_conf = float(raw_conf or 0.0)
                    if inst_conf < query_min_conf:
                        continue
                    x1 = float(box.get("Left", 0.0) or 0.0)
                    y1 = float(box.get("Top", 0.0) or 0.0)
                    w = float(box.get("Width", 0.0) or 0.0)
                    h = float(box.get("Height", 0.0) or 0.0)
                    area = w * h
                    # Reject tiny reflections, furniture slivers, and full-frame artifacts
                    if (
                        w < MIN_BOX_WIDTH
                        or h < MIN_BOX_HEIGHT
                        or area < MIN_BOX_AREA
                        or area > MAX_BOX_AREA
                    ):
                        continue
                    aspect = w / h
                    if aspect < MIN_ASPECT_RATIO or aspect > MAX_ASPECT_RATIO:
                        continue
                    persons.append(
                        {
                            "x1": round(max(0.0, min(x1, 1.0)), 4),
                            "y1": round(max(0.0, min(y1, 1.0)), 4),
                            "x2": round(max(0.0, min(x1 + w, 1.0)), 4),
                            "y2": round(max(0.0, min(y1 + h, 1.0)), 4),
                            "score": round(inst_conf / 100.0, 4),
                        }
                    )
            confidence = sum(p["score"] for p in persons) / len(persons) if persons else 0.0
            labels = [lbl.get("Name") for lbl in response.get("Labels", []) if lbl.get("Name")]
            return {
                "person_count": len(persons),
                "confidence": round(confidence, 4),
                "labels": labels[:8],
                "persons": persons,
                "source": "aws-rekognition",
            }
        except Exception as error:  # noqa: BLE001
            self._error = str(error)
            logger.error("AWS Rekognition detection failed: %s", error)
            return None


_service = None


def get_vision_service() -> AWSRekognitionService:
    global _service
    if _service is None:
        _service = AWSRekognitionService()
    return _service


def get_rekognition_service() -> AWSRekognitionService:
    return get_vision_service()
