"""AWS Rekognition client for person detection.

Wraps boto3 Rekognition calls with timeouts, error handling, and a consistent
return shape so callers don't need to know about the provider.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

import boto3
from botocore.config import Config

from config.settings import get_settings

logger = logging.getLogger(__name__)

_client = None
_lock = threading.Lock()


def _get_client():
    global _client
    with _lock:
        if _client is not None:
            return _client
        settings = get_settings()
        cfg = Config(
            connect_timeout=4,
            read_timeout=8,
            max_pool_connections=25,
            retries={"max_attempts": 2},
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
    """Detect people in a JPEG frame using AWS Rekognition."""

    def __init__(self, settings=None) -> None:
        self.settings = settings or get_settings()
        self._error: str | None = None

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
        try:
            client = _get_client()
            min_conf = float(self.settings.rekognition_min_confidence)
            response = client.detect_labels(
                Image={"Bytes": image_bytes},
                MinConfidence=min_conf,
            )
            persons: list[dict[str, Any]] = []
            for label in response.get("Labels", []):
                if label.get("Name") != "Person":
                    continue
                for instance in label.get("Instances") or []:
                    box = instance.get("BoundingBox") or {}
                    if not box:
                        continue
                    inst_conf = float(instance.get("Confidence") or label.get("Confidence") or 0.0)
                    if inst_conf < min_conf:
                        continue
                    x1 = float(box.get("Left", 0.0) or 0.0)
                    y1 = float(box.get("Top", 0.0) or 0.0)
                    w = float(box.get("Width", 0.0) or 0.0)
                    h = float(box.get("Height", 0.0) or 0.0)
                    # Require a non-trivial bounding box so noise/artifacts are ignored
                    if w <= 0.01 or h <= 0.01:
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
            logger.warning("Rekognition detection failed, using OpenCV fallback: %s", error)
            return None


_service = None


def get_vision_service() -> AWSRekognitionService:
    global _service
    if _service is None:
        _service = AWSRekognitionService()
    return _service


def get_rekognition_service() -> AWSRekognitionService:
    return get_vision_service()
