"""Shop interaction documents (``shop_interactions`` collection)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from utils.helpers import clamp, serialize_doc, to_iso, utcnow

COLLECTION = "shop_interactions"

TYPE_SUCCESSFUL = "SUCCESSFUL"
TYPE_MISSED = "MISSED"
TYPE_UNATTENDED = "UNATTENDED"
INTERACTION_TYPES = {TYPE_SUCCESSFUL, TYPE_MISSED, TYPE_UNATTENDED}


@dataclass
class Interaction:
    """One customer/shopkeeper encounter detected by the pipeline."""

    channel: int
    greeting_detected: bool
    confidence: float = 0.0
    person_count: int = 0
    customer_count: int = 1
    shopkeeper_present: bool = True
    interaction_type: str = TYPE_MISSED
    duration_seconds: float = 0.0
    response_time_seconds: float | None = None
    shop_id: str = "shop_001"
    speech_text: str = ""
    source: str = "none"
    notes: str = ""
    snapshot: str | None = None
    created_at: datetime = field(default_factory=utcnow)
    id: str | None = None

    def __post_init__(self) -> None:
        self.channel = int(self.channel)
        self.person_count = max(0, int(self.person_count))
        self.customer_count = max(0, int(self.customer_count))
        self.shopkeeper_present = bool(self.shopkeeper_present)
        self.confidence = clamp(float(self.confidence or 0.0), 0.0, 1.0)
        self.duration_seconds = max(0.0, float(self.duration_seconds or 0.0))
        if self.response_time_seconds is not None:
            self.response_time_seconds = max(
                0.0, float(self.response_time_seconds)
            )
        self.speech_text = (self.speech_text or "").strip()[:500]
        self.notes = (self.notes or "").strip()[:500]
        self.shop_id = (self.shop_id or "shop_001").strip()[:80]

        interaction_type = (self.interaction_type or "").upper()
        if interaction_type not in INTERACTION_TYPES:
            interaction_type = TYPE_SUCCESSFUL if self.greeting_detected else TYPE_MISSED
        if self.greeting_detected:
            interaction_type = TYPE_SUCCESSFUL
        self.interaction_type = interaction_type

    def to_document(self) -> dict[str, Any]:
        document = asdict(self)
        document.pop("id", None)
        return document

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> "Interaction":
        data = dict(document)
        identifier = data.pop("_id", None)
        person_count = data.get("person_count", 0)
        shopkeeper_present = bool(data.get("shopkeeper_present", True))
        default_customers = max(0, int(person_count or 0) - (1 if shopkeeper_present else 0))
        return cls(
            id=str(identifier) if identifier else None,
            channel=data.get("camera_id", data.get("channel", 1)),
            greeting_detected=bool(data.get("greeting_detected")),
            confidence=data.get("confidence", data.get("greeting_confidence", 0.0)),
            person_count=person_count,
            customer_count=data.get("customer_count", default_customers),
            shopkeeper_present=shopkeeper_present,
            interaction_type=data.get("interaction_type", "")
            or (TYPE_SUCCESSFUL if data.get("greeting_detected") else TYPE_MISSED),
            duration_seconds=data.get("duration_seconds", 0.0),
            response_time_seconds=data.get("response_time_seconds"),
            shop_id=data.get("shop_id", "shop_001"),
            speech_text=data.get("speech_text", ""),
            source=data.get("source", "none"),
            notes=data.get("notes", ""),
            snapshot=data.get("snapshot", data.get("frame_url")),
            created_at=data.get("created_at") or utcnow(),
        )

    def to_api(self) -> dict[str, Any]:
        payload = serialize_doc(self.to_document())
        payload["id"] = self.id
        payload["camera_id"] = self.channel
        payload["greeting_confidence"] = round(self.confidence, 4)
        payload["frame_url"] = self.snapshot
        payload["snapshot_url"] = self.snapshot
        payload["type"] = self.interaction_type
        payload["timestamp"] = to_iso(self.created_at)
        return payload
