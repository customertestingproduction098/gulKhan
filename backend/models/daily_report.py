"""Daily analytics snapshots (``daily_reports`` collection)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from utils.helpers import serialize_doc, to_iso, utcnow

COLLECTION = "daily_reports"


@dataclass
class DailyReport:
    date: str
    shop_id: str = "shop_001"
    total_interactions: int = 0
    total_customers: int = 0
    successful_greetings: int = 0
    missed_greetings: int = 0
    unattended_customers: int = 0
    greeting_rate: float = 0.0
    peak_hour: int | None = None
    avg_response_time: float = 0.0
    shopkeeper_presence_rate: float = 0.0
    warehouse_entries: int = 0
    after_hours_entries: int = 0
    unauthorized_entries: int = 0
    false_alarm_entries: int = 0
    alerts: int = 0
    unacknowledged_alerts: int = 0
    emailed_at: datetime | None = None
    created_at: datetime = field(default_factory=utcnow)
    id: str | None = None

    def to_document(self) -> dict[str, Any]:
        document = serialize_doc(
            {
                "date": self.date,
                "shop_id": self.shop_id,
                "total_interactions": self.total_interactions,
                "total_customers": self.total_customers,
                "successful_greetings": self.successful_greetings,
                "missed_greetings": self.missed_greetings,
                "unattended_customers": self.unattended_customers,
                "greeting_rate": self.greeting_rate,
                "peak_hour": self.peak_hour,
                "avg_response_time": self.avg_response_time,
                "shopkeeper_presence_rate": self.shopkeeper_presence_rate,
                "warehouse_entries": self.warehouse_entries,
                "after_hours_entries": self.after_hours_entries,
                "unauthorized_entries": self.unauthorized_entries,
                "false_alarm_entries": self.false_alarm_entries,
                "alerts": self.alerts,
                "unacknowledged_alerts": self.unacknowledged_alerts,
                "emailed_at": to_iso(self.emailed_at),
                "created_at": to_iso(self.created_at),
            }
        )
        return document

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> "DailyReport":
        data = dict(document)
        identifier = data.pop("_id", None)
        return cls(
            id=str(identifier) if identifier else None,
            date=str(data.get("date", "")),
            shop_id=data.get("shop_id", "shop_001"),
            total_interactions=int(data.get("total_interactions", 0) or 0),
            total_customers=int(data.get("total_customers", 0) or 0),
            successful_greetings=int(data.get("successful_greetings", 0) or 0),
            missed_greetings=int(data.get("missed_greetings", 0) or 0),
            unattended_customers=int(data.get("unattended_customers", 0) or 0),
            greeting_rate=float(data.get("greeting_rate", 0.0) or 0.0),
            peak_hour=data.get("peak_hour"),
            avg_response_time=float(data.get("avg_response_time", 0.0) or 0.0),
            shopkeeper_presence_rate=float(
                data.get("shopkeeper_presence_rate", 0.0) or 0.0
            ),
            warehouse_entries=int(data.get("warehouse_entries", 0) or 0),
            after_hours_entries=int(data.get("after_hours_entries", 0) or 0),
            unauthorized_entries=int(data.get("unauthorized_entries", 0) or 0),
            false_alarm_entries=int(data.get("false_alarm_entries", 0) or 0),
            alerts=int(data.get("alerts", 0) or 0),
            unacknowledged_alerts=int(data.get("unacknowledged_alerts", 0) or 0),
            emailed_at=data.get("emailed_at"),
            created_at=data.get("created_at") or utcnow(),
        )

    def to_api(self) -> dict[str, Any]:
        payload = serialize_doc(self.to_document())
        payload["id"] = self.id
        return payload
