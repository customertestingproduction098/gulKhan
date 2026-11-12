"""Unacknowledged critical-alert escalation.

The initial alert email goes out immediately. If it is still unacknowledged,
a reminder email is sent after 5 minutes and a secondary-contact email after
15 minutes. Every stage is recorded on the alert and duplicates are prevented
with an atomic MongoDB update.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Any

from config.settings import Settings, get_settings
from services.alert_system import AlertSystem
from services.mongodb_handler import DataStoreError, MongoHandler
from services.settings_service import SettingsService
from utils.helpers import UTC, utcnow

logger = logging.getLogger(__name__)


class EscalationManager:
    TICK_SECONDS = 20.0
    MAX_AGE = timedelta(hours=24)

    def __init__(
        self,
        store: MongoHandler,
        alerts: AlertSystem,
        owner_settings: SettingsService,
        events=None,
        settings: Settings | None = None,
    ) -> None:
        self.store = store
        self.alerts = alerts
        self.owner_settings = owner_settings
        self.events = events
        self.settings = settings or get_settings()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.last_run_at: datetime | None = None
        self.last_error: str | None = None
        self.escalations_sent = 0

    # ------------------------------------------------------------- lifecycle
    def start(self) -> "EscalationManager":
        if self._thread and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="alert-escalation",
            daemon=True,
        )
        self._thread.start()
        logger.info("Alert escalation worker started")
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)
        self._thread = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def status(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "last_error": self.last_error,
            "escalations_sent": self.escalations_sent,
            "policy": self.owner_settings.escalation(),
        }

    # ------------------------------------------------------------------- loop
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as error:  # noqa: BLE001 - worker must survive
                self.last_error = str(error)
                logger.exception("Escalation tick failed: %s", error)
            self._stop.wait(self.TICK_SECONDS)

    def run_once(self) -> int:
        if not self.owner_settings.escalation().get("enabled", True):
            self.last_run_at = utcnow()
            return 0

        try:
            alerts, _ = self.store.list_alerts(limit=100, acknowledged=False, days=1)
        except DataStoreError as error:
            self.last_error = str(error)
            return 0

        now = utcnow()
        sent = 0
        for alert in alerts:
            if alert.get("severity") != "critical":
                continue
            # Never re-send duplicate emails for warehouse detections that were already caught/emailed
            if alert.get("type") == "warehouse_entry":
                continue
            email_delivery = (alert.get("delivery") or {}).get("email") or {}
            if email_delivery.get("sent"):
                continue
            created_at = self._parse_datetime(alert.get("created_at"))
            if created_at is None:
                continue
            age = now - created_at
            if age > self.MAX_AGE:
                continue

            stages = self._due_stages(alert, age.total_seconds())
            for stage in stages:
                if self._apply_stage(alert, stage):
                    sent += 1
                    self.escalations_sent += 1

        self.last_run_at = utcnow()
        self.last_error = None
        return sent

    # ---------------------------------------------------------------- policy
    def _due_stages(self, alert: dict[str, Any], age_seconds: float) -> list[str]:
        policy = self.owner_settings.escalation()
        existing = {
            str(item.get("stage"))
            for item in alert.get("escalations", [])
            if isinstance(item, dict)
        }
        stages = [
            ("email_reminder", policy.get("email_after", 300)),
            ("secondary_contact", policy.get("secondary_after", 900)),
        ]
        due = []
        for name, delay in stages:
            if name not in existing and age_seconds >= float(delay):
                due.append(name)
        return due

    def _apply_stage(self, alert: dict[str, Any], stage: str) -> bool:
        title = alert.get("title", "Critical alert")
        message = alert.get("message", "")
        contacts = self.owner_settings.alert_contacts()

        if stage == "email_reminder":
            delivery = {
                "email": self.alerts.send_email(
                    subject=f"[Sentinel AI] Reminder - {title}",
                    html_body=self.alerts._simple_email_html(  # noqa: SLF001
                        f"Reminder: {title}",
                        message,
                        alert.get("severity", "critical"),
                    ),
                    to_email=contacts.get("email"),
                )
            }
        else:
            delivery = {
                "email": self.alerts.send_email(
                    subject=f"[Sentinel AI] Secondary contact - {title}",
                    html_body=self.alerts._simple_email_html(  # noqa: SLF001
                        f"Secondary contact: {title}",
                        message,
                        alert.get("severity", "critical"),
                    ),
                    to_email=contacts.get("secondary_email") or contacts.get("email"),
                )
            }

        try:
            recorded = self.store.record_alert_escalation(
                alert["id"], stage, delivery
            )
        except DataStoreError as error:
            self.last_error = str(error)
            logger.error("Could not record escalation for %s: %s", alert.get("id"), error)
            return False

        if recorded:
            logger.warning("Escalated alert %s to stage %s", alert.get("id"), stage)
            if self.events is not None:
                self.events.publish(
                    "alert.escalated",
                    {
                        "id": alert.get("id"),
                        "stage": stage,
                        "delivery": delivery,
                    },
                )
            self.store.log(
                "warning",
                f"Alert escalated ({stage})",
                {"alert_id": alert.get("id"), "delivery": delivery},
            )
        return recorded

    @staticmethod
    def _parse_datetime(value: Any) -> datetime | None:
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=UTC)
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
            except ValueError:
                return None
        return None
