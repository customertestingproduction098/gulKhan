"""Warehouse entry documents (``warehouse_entries`` collection)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from utils.helpers import clamp, serialize_doc, to_iso, utcnow

COLLECTION = "warehouse_entries"

SEVERITIES = {"critical", "warning", "info"}


@dataclass
class Entry:
    """A person crossing the warehouse threshold."""

    channel: int
    authorized: bool = False
    after_hours: bool = True
    person_count: int = 0
    confidence: float = 0.0
    warehouse_id: str = "warehouse_001"
    entry_location: str = "main_gate"
    snapshot: str | None = None
    notes: str = ""
    alert_sent: bool = False
    alert_channel: list[str] = field(default_factory=list)
    severity: str = "warning"
    false_alarm: bool = False
    created_at: datetime = field(default_factory=utcnow)
    id: str | None = None

    def __post_init__(self) -> None:
        self.channel = int(self.channel)
        self.person_count = max(0, int(self.person_count))
        self.confidence = clamp(float(self.confidence or 0.0), 0.0, 1.0)
        self.notes = (self.notes or "").strip()[:500]
        self.warehouse_id = (self.warehouse_id or "warehouse_001").strip()[:80]
        self.entry_location = (self.entry_location or "main_gate").strip()[:80]
        self.alert_channel = [str(item)[:20] for item in (self.alert_channel or [])]
        if self.severity not in SEVERITIES:
            self.severity = "warning"

    def to_document(self) -> dict[str, Any]:
        document = asdict(self)
        document.pop("id", None)
        return document

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> "Entry":
        data = dict(document)
        identifier = data.pop("_id", None)
        return cls(
            id=str(identifier) if identifier else None,
            channel=data.get("camera_id", data.get("channel", 1)),
            authorized=bool(data.get("authorized")),
            after_hours=bool(data.get("after_hours")),
            person_count=data.get("person_count", 0),
            confidence=data.get("confidence", 0.0),
            warehouse_id=data.get("warehouse_id", "warehouse_001"),
            entry_location=data.get("entry_location", "main_gate"),
            snapshot=data.get("snapshot", data.get("frame_url")),
            notes=data.get("notes", ""),
            alert_sent=bool(data.get("alert_sent")),
            alert_channel=data.get("alert_channel", data.get("alert_channels", [])),
            severity=data.get("severity", "warning"),
            false_alarm=bool(data.get("false_alarm")),
            created_at=data.get("created_at") or utcnow(),
        )

    def to_api(self) -> dict[str, Any]:
        payload = serialize_doc(self.to_document())
        payload["id"] = self.id
        payload["camera_id"] = self.channel
        payload["frame_url"] = self.snapshot
        payload["snapshot_url"] = self.snapshot
        payload["alert_channels"] = self.alert_channel
        payload["timestamp"] = to_iso(self.created_at)
        return payload
