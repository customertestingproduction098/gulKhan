"""Scheduled daily and weekly report delivery."""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Any

from config.settings import Settings, get_settings
from services.escalation import EscalationManager
from services.reports import ReportService
from services.settings_service import SettingsService
from utils.helpers import (
    current_time,
    iso_week_label,
    local_date,
    normalize_hhmm,
    parse_hhmm,
    utcnow,
)

logger = logging.getLogger(__name__)


class ReportScheduler:
    TICK_SECONDS = 30.0

    def __init__(
        self,
        reports: ReportService,
        owner_settings: SettingsService,
        events=None,
        settings: Settings | None = None,
    ) -> None:
        self.reports = reports
        self.owner_settings = owner_settings
        self.events = events
        self.settings = settings or get_settings()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.last_run_at: datetime | None = None
        self.last_error: str | None = None
        self.last_daily: str | None = None
        self.last_weekly: str | None = None

    # ------------------------------------------------------------- lifecycle
    def start(self) -> "ReportScheduler":
        if self._thread and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="report-scheduler",
            daemon=True,
        )
        self._thread.start()
        logger.info("Report scheduler started")
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
        reports = self.owner_settings.reports()
        return {
            "running": self.running,
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "last_error": self.last_error,
            "last_daily": self.last_daily,
            "last_weekly": self.last_weekly,
            "daily_enabled": reports.get("daily_enabled", True),
            "daily_time": reports.get("daily_time", "21:00"),
            "weekly_enabled": reports.get("weekly_enabled", True),
            "weekly_day": reports.get("weekly_day", "monday"),
            "recipients": self.owner_settings.report_recipients(),
        }

    # ------------------------------------------------------------------- loop
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as error:  # noqa: BLE001 - worker must survive
                self.last_error = str(error)
                logger.exception("Report scheduler tick failed: %s", error)
            self._stop.wait(self.TICK_SECONDS)

    def run_once(self) -> list[str]:
        actions: list[str] = []
        now = current_time()
        preferences = self.owner_settings.reports()
        daily_time = parse_hhmm(
            preferences.get("daily_time", "21:00"),
            datetime.strptime("21:00", "%H:%M").time(),
        )

        if preferences.get("daily_enabled", True) and now.time() >= daily_time:
            target = local_date()
            stored = self.reports.store.get_daily_report(target.isoformat())
            if not stored or not stored.get("emailed_at"):
                self._deliver_daily(target)
                actions.append("daily")

        if (
            preferences.get("weekly_enabled", True)
            and now.strftime("%A").lower() == preferences.get("weekly_day", "monday")
            and now.time() >= daily_time
        ):
            start = local_date() - timedelta(days=6)
            end = local_date()
            key = f"weekly:{iso_week_label(start)}"
            run = self.reports.store.get_report_run(key)
            if not run:
                self._deliver_weekly(start, end)
                self.reports.store.mark_report_run(
                    key,
                    {"start": start.isoformat(), "end": end.isoformat()},
                )
                actions.append("weekly")

        self.last_run_at = utcnow()
        self.last_error = None
        return actions

    def _deliver_daily(self, target) -> None:
        payload = self.reports.daily(target.isoformat(), persist=True)
        report = self.reports.store.daily_report(target)
        result = self.reports.send_daily_report(report)

        delivery: dict[str, Any] = {}
        deliveries = result.get("deliveries") or []
        if deliveries:
            delivery["email"] = deliveries[0]
        else:
            delivery["email"] = {
                "sent": False,
                "skipped": True,
                "error": "No report recipients configured",
            }

        if report.missed_greetings > 0 or report.unattended_customers > 0:
            self.reports.alerts.create_greeting_report_alert(report, delivery=delivery)

        self.reports.store.mark_daily_report_emailed(target.isoformat())
        self.last_daily = target.isoformat()
        if self.events is not None:
            self.events.publish(
                "report.generated",
                {
                    "kind": "daily",
                    "date": target.isoformat(),
                    "report": payload,
                    "delivery": delivery,
                },
            )
        logger.info(
            "Daily report generated for %s (%s recipients)",
            target.isoformat(),
            len(result.get("recipients") or []),
        )

    def _deliver_weekly(self, start, end) -> None:
        result = self.reports.send_weekly_email(start, end)
        self.last_weekly = iso_week_label(start)
        if self.events is not None:
            self.events.publish(
                "report.generated",
                {
                    "kind": "weekly",
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "delivery": result,
                },
            )
        logger.info(
            "Weekly report generated for %s..%s",
            start.isoformat(),
            end.isoformat(),
        )

    def send_now(self, kind: str = "daily", value: str | None = None) -> dict[str, Any]:
        if kind == "weekly":
            start = local_date() - timedelta(days=6)
            end = local_date()
            result = self.reports.send_weekly_email(start, end)
            return {
                "kind": "weekly",
                "start": start.isoformat(),
                "end": end.isoformat(),
                **result,
            }
        target = local_date()
        payload = self.reports.daily(value or target.isoformat(), persist=True)
        report = self.reports.store.daily_report(value or target.isoformat())
        result = self.reports.send_daily_report(report)
        return {"kind": "daily", "date": target.isoformat(), "report": payload, **result}


def normalize_daily_time(value: str) -> str:
    return normalize_hhmm(value, "21:00") or "21:00"
