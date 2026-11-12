"""Security alert documents (``alerts`` collection)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from utils.helpers import serialize_doc, to_iso, utcnow

COLLECTION = "alerts"

SEVERITIES = {"critical", "warning", "info"}
STATUSES = {"PENDING", "SENT", "PARTIAL", "SKIPPED", "FAILED", "ACKNOWLEDGED"}


@dataclass
class Alert:
    """An event that the owner must know about."""

    type: str
    title: str
    message: str
    severity: str = "warning"
    channel: int | None = None
    acknowledged: bool = False
    acknowledged_at: datetime | None = None
    delivery: dict[str, Any] = field(default_factory=dict)
    sent_channels: list[str] = field(default_factory=list)
    status: str = ""
    escalations: list[dict[str, Any]] = field(default_factory=list)
    owner_id: str = "owner_001"
    owner_email: str = ""
    snapshot: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utcnow)
    id: str | None = None

    def __post_init__(self) -> None:
        if self.severity not in SEVERITIES:
            self.severity = "warning"
        self.type = (self.type or "system").strip()[:80]
        self.title = (self.title or "Alert").strip()[:160]
        self.message = (self.message or "").strip()[:2000]
        self.owner_email = (self.owner_email or "").strip()
        self.snapshot = self.snapshot or self.metadata.get("snapshot_url")

        channels = [
            name
            for name, result in (self.delivery or {}).items()
            if isinstance(result, dict) and result.get("sent")
        ]
        if channels:
            self.sent_channels = sorted(set(self.sent_channels) | set(channels))

        status = (self.status or "").upper()
        if self.acknowledged:
            status = "ACKNOWLEDGED"
        elif status not in STATUSES or status == "PENDING":
            if self.sent_channels:
                attempted = len(self.delivery or {})
                status = "SENT" if len(self.sent_channels) == attempted else "PARTIAL"
            elif any(
                isinstance(result, dict) and result.get("sent") is False
                for result in (self.delivery or {}).values()
            ):
                status = "FAILED"
            elif self.delivery:
                status = "SKIPPED"
            else:
                status = "PENDING"
        self.status = status

    def to_document(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "title": self.title,
            "message": self.message,
            "severity": self.severity,
            "channel": self.channel,
            "acknowledged": self.acknowledged,
            "acknowledged_at": self.acknowledged_at,
            "delivery": self.delivery,
            "sent_channels": self.sent_channels,
            "status": self.status,
            "escalations": self.escalations,
            "owner_id": self.owner_id,
            "owner_email": self.owner_email,
            "snapshot": self.snapshot,
            "metadata": self.metadata,
            "created_at": self.created_at,
        }

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> "Alert":
        data = dict(document)
        identifier = data.pop("_id", None)
        return cls(
            id=str(identifier) if identifier else None,
            type=data.get("alert_type", data.get("type", "system")),
            title=data.get("title", "Alert"),
            message=data.get("message", ""),
            severity=data.get("severity", "warning"),
            channel=data.get("channel"),
            acknowledged=bool(data.get("acknowledged")),
            acknowledged_at=data.get("acknowledged_at"),
            delivery=data.get("delivery") or {},
            sent_channels=data.get("sent_channels") or [],
            status=data.get("status", ""),
            escalations=data.get("escalations") or [],
            owner_id=data.get("owner_id", "owner_001"),
            owner_email=data.get("owner_email", ""),
            snapshot=data.get("snapshot", data.get("frame_url")),
            metadata=data.get("metadata") or {},
            created_at=data.get("created_at") or utcnow(),
        )

    def to_api(self) -> dict[str, Any]:
        payload = serialize_doc(self.to_document())
        payload["id"] = self.id
        payload["alert_type"] = self.type
        payload["frame_url"] = self.snapshot
        payload["snapshot_url"] = self.snapshot
        payload["acknowledged_at"] = to_iso(self.acknowledged_at)
        payload["created_at"] = to_iso(self.created_at)
        return payload
