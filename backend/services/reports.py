"""Report building and CSV/PDF export."""

from __future__ import annotations

import csv
import html
import logging
from datetime import date, datetime
from io import BytesIO, StringIO
from typing import Any

from config.settings import Settings, get_settings
from models import DailyReport
from services.alert_system import AlertSystem
from services.mongodb_handler import MongoHandler
from services.settings_service import SettingsService
from utils.helpers import (
    iso_week_label,
    local_date,
    parse_date,
    parse_month,
    parse_week,
    range_bounds,
    to_local,
)

logger = logging.getLogger(__name__)


class ReportService:
    def __init__(
        self,
        store: MongoHandler,
        alerts: AlertSystem,
        owner_settings: SettingsService,
        settings: Settings | None = None,
    ) -> None:
        self.store = store
        self.alerts = alerts
        self.owner_settings = owner_settings
        self.settings = settings or get_settings()

    # --------------------------------------------------------------- builders
    def summary(
        self,
        days: int = 7,
        start: date | None = None,
        end: date | None = None,
    ) -> dict[str, Any]:
        return self.store.summary(days=days, start=start, end=end)

    def daily(self, value: str | None = None, persist: bool = False) -> dict[str, Any]:
        target = parse_date(value)
        report = self.store.daily_report(target)
        if persist:
            stored = self.store.upsert_daily_report(report)
            payload = dict(stored)
        else:
            payload = report.to_api()
        payload["top_missed"] = self._missed_interactions(target)
        return payload

    def weekly(self, value: str | None = None, persist: bool = False) -> dict[str, Any]:
        start, end = parse_week(value)
        payload = self.store.weekly_report(start, end)
        payload["week"] = iso_week_label(start)
        if persist:
            for item in self.store._daily_models(start, end):  # noqa: SLF001 - shared builder
                self.store.upsert_daily_report(item)
        return payload

    def monthly(self, value: str | None = None, persist: bool = False) -> dict[str, Any]:
        start, end = parse_month(value)
        payload = self.store.monthly_report(start, end)
        payload["month"] = f"{start.year}-{start.month:02d}"
        if persist:
            for item in self.store._daily_models(start, end):  # noqa: SLF001
                self.store.upsert_daily_report(item)
        return payload

    def _missed_interactions(self, target: date) -> list[dict[str, Any]]:
        start_utc, end_utc = range_bounds(start=target, end=target)
        items, _ = self.store.list_interactions(
            limit=10,
            interaction_type="MISSED",
            start=start_utc,
            end=end_utc,
        )
        return items

    # ------------------------------------------------------------------- CSV
    def export_csv(
        self,
        days: int = 7,
        start: date | None = None,
        end: date | None = None,
    ) -> str:
        start_utc, end_utc = range_bounds(days=days, start=start, end=end)
        interactions, _ = self.store.list_interactions(
            limit=1000, start=start_utc, end=end_utc
        )
        entries, _ = self.store.list_entries(
            limit=1000, start=start_utc, end=end_utc
        )
        alerts, _ = self.store.list_alerts(
            limit=500, start=start_utc, end=end_utc
        )

        columns = [
            "record_type",
            "timestamp",
            "camera_id",
            "interaction_type",
            "customer_count",
            "shopkeeper_present",
            "greeting_detected",
            "greeting_confidence",
            "response_time_seconds",
            "duration_seconds",
            "person_count",
            "authorized",
            "after_hours",
            "entry_location",
            "severity",
            "alert_channels",
            "alert_type",
            "alert_status",
            "acknowledged",
            "false_alarm",
            "notes",
            "speech_text",
        ]
        buffer = StringIO()
        writer = csv.DictWriter(buffer, fieldnames=columns)
        writer.writeheader()

        for item in interactions:
            writer.writerow(
                {
                    "record_type": "interaction",
                    "timestamp": item.get("timestamp"),
                    "camera_id": item.get("camera_id"),
                    "interaction_type": item.get("interaction_type"),
                    "customer_count": item.get("customer_count"),
                    "shopkeeper_present": item.get("shopkeeper_present"),
                    "greeting_detected": item.get("greeting_detected"),
                    "greeting_confidence": item.get("greeting_confidence"),
                    "response_time_seconds": item.get("response_time_seconds"),
                    "duration_seconds": item.get("duration_seconds"),
                    "person_count": item.get("person_count"),
                    "notes": item.get("notes"),
                    "speech_text": item.get("speech_text"),
                }
            )

        for item in entries:
            writer.writerow(
                {
                    "record_type": "warehouse_entry",
                    "timestamp": item.get("timestamp"),
                    "camera_id": item.get("camera_id"),
                    "person_count": item.get("person_count"),
                    "authorized": item.get("authorized"),
                    "after_hours": item.get("after_hours"),
                    "entry_location": item.get("entry_location"),
                    "severity": item.get("severity"),
                    "alert_channels": ",".join(item.get("alert_channels") or []),
                    "false_alarm": item.get("false_alarm"),
                    "notes": item.get("notes"),
                }
            )

        for item in alerts:
            writer.writerow(
                {
                    "record_type": "alert",
                    "timestamp": item.get("created_at"),
                    "camera_id": item.get("channel"),
                    "severity": item.get("severity"),
                    "alert_channels": ",".join(item.get("sent_channels") or []),
                    "alert_type": item.get("alert_type"),
                    "alert_status": item.get("status"),
                    "acknowledged": item.get("acknowledged"),
                    "notes": item.get("title"),
                }
            )

        return buffer.getvalue()

    # ------------------------------------------------------------------- PDF
    def export_pdf(
        self,
        days: int = 7,
        start: date | None = None,
        end: date | None = None,
    ) -> bytes:
        summary = self.summary(days=days, start=start, end=end)
        start_date = parse_date(summary["range"]["start_date"])
        end_date = parse_date(summary["range"]["end_date"])

        try:
            from fpdf import FPDF
        except ImportError as error:  # pragma: no cover - dependency guard
            raise RuntimeError("PDF export requires fpdf2 (pip install fpdf2)") from error

        pdf = FPDF(orientation="P", unit="mm", format="A4")
        pdf.set_auto_page_break(auto=True, margin=15)
        pdf.add_page()
        pdf.set_font("Helvetica", "B", 18)
        pdf.cell(0, 10, self._pdf_text("Sentinel AI surveillance report"), new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 10)
        pdf.set_text_color(100, 116, 139)
        pdf.cell(
            0,
            6,
            self._pdf_text(
                f"{start_date.isoformat()} to {end_date.isoformat()} - generated "
                f"{datetime.now().strftime('%d %b %Y, %H:%M')}"
            ),
            new_x="LMARGIN",
            new_y="NEXT",
        )
        cameras_label = ", ".join(f"CH {c:02d}" for c in getattr(self.settings, "allowed_cameras", [2, 4, 5, 7, 8, 9, 11]))
        pdf.cell(
            0,
            6,
            self._pdf_text(f"Cameras analyzed: {cameras_label}"),
            new_x="LMARGIN",
            new_y="NEXT",
        )
        pdf.ln(4)

        shop = summary["shop"]
        warehouse = summary["warehouse"]
        alerts = summary["alerts"]
        rows = [
            (
                "Customer interactions",
                f"{shop['total_interactions']} ({shop['total_customers']} customers)",
            ),
            ("Greeting compliance", f"{round(shop['greeting_rate'] * 100)}%"),
            ("Successful / missed", f"{shop['successful_greetings']} / {shop['missed_greetings']}"),
            ("Unattended customers", str(shop["unattended"])),
            ("Average response time", f"{shop['avg_response_time']} s"),
            ("Shopkeeper presence", f"{round(shop['shopkeeper_presence_rate'] * 100)}%"),
            ("Warehouse entries", str(warehouse["total_entries"])),
            ("After-hours entries", str(warehouse["after_hours_entries"])),
            ("Unauthorized entries", str(warehouse["unauthorized_entries"])),
            ("False alarms", f"{warehouse['false_alarms']} ({round(warehouse['false_alarm_rate'] * 100)}%)"),
            ("Alerts raised", str(alerts["total"])),
            ("Unacknowledged alerts", str(alerts["unacknowledged"])),
        ]

        self._pdf_table(pdf, rows)
        pdf.ln(6)

        pdf.set_font("Helvetica", "B", 12)
        pdf.set_text_color(15, 23, 42)
        pdf.cell(0, 8, self._pdf_text("Daily breakdown"), new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "B", 9)
        headers = ["Date", "Customers", "Greeted", "Missed", "Entries", "Alerts"]
        widths = [40, 28, 28, 28, 28, 28]
        for header, width in zip(headers, widths):
            pdf.cell(width, 8, header, border=1)
        pdf.ln()
        pdf.set_font("Helvetica", "", 9)
        for row in summary["daily"]:
            values = [
                row["date"],
                str(row["customers"]),
                str(row["greeted"]),
                str(row["missed"]),
                str(row["entries"]),
                str(row["alerts"]),
            ]
            for value, width in zip(values, widths):
                pdf.cell(width, 7, self._pdf_text(value), border=1)
            pdf.ln()

        pdf.ln(6)
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(100, 116, 139)
        pdf.multi_cell(
            0,
            5,
            self._pdf_text(
                "Generated automatically by Sentinel AI. Review camera snapshots and "
                "acknowledge critical alerts in the dashboard."
            ),
        )
        output = pdf.output()
        return bytes(output)

    @staticmethod
    def _pdf_text(value: Any) -> str:
        return str(value or "").encode("latin-1", "replace").decode("latin-1")

    def _pdf_table(self, pdf, rows: list[tuple[str, str]]) -> None:
        pdf.set_font("Helvetica", "", 10)
        for label, value in rows:
            pdf.set_text_color(71, 85, 105)
            pdf.cell(90, 8, self._pdf_text(label), border="B")
            pdf.set_text_color(15, 23, 42)
            pdf.set_font("Helvetica", "B", 10)
            pdf.cell(0, 8, self._pdf_text(value), border="B", new_x="LMARGIN", new_y="NEXT")
            pdf.set_font("Helvetica", "", 10)

    # ------------------------------------------------------------------ email
    def send_weekly_email(
        self,
        start: date,
        end: date,
        recipients: list[str] | None = None,
    ) -> dict[str, Any]:
        summary = self.summary(start=start, end=end)
        recipients = recipients or self.owner_settings.report_recipients()
        deliveries = []
        for recipient in recipients:
            deliveries.append(
                self.alerts.send_report_email(
                    summary,
                    to_email=recipient,
                    subject_prefix="Weekly surveillance report",
                )
            )
        return {"recipients": recipients, "deliveries": deliveries}

    def send_daily_report(self, report: DailyReport) -> dict[str, Any]:
        recipients = self.owner_settings.report_recipients()
        subject = f"Daily shop report - {report.date}"
        if report.missed_greetings:
            subject += f" - {report.missed_greetings} missed greeting(s)"

        rows = [
            ("Customers", str(report.total_customers)),
            ("Interactions", str(report.total_interactions)),
            ("Successful greetings", str(report.successful_greetings)),
            ("Missed greetings", str(report.missed_greetings)),
            ("Unattended customers", str(report.unattended_customers)),
            ("Average response", f"{report.avg_response_time} s"),
            ("Shopkeeper presence", f"{round(report.shopkeeper_presence_rate * 100)}%"),
            ("Peak hour", f"{report.peak_hour:02d}:00" if report.peak_hour is not None else "-"),
            ("Warehouse entries", str(report.warehouse_entries)),
            ("After-hours entries", str(report.after_hours_entries)),
            ("Alerts", str(report.alerts)),
        ]
        table = "".join(
            f"<tr><td style='padding:8px 12px;color:#475569'>{html.escape(label)}</td>"
            f"<td style='padding:8px 12px;font-weight:600;color:#0f172a'>{html.escape(value)}</td></tr>"
            for label, value in rows
        )
        body = f"""
        <div style="font-family:Arial,sans-serif;max-width:560px;margin:auto">
          <h2 style="color:#0f172a">Sentinel AI - daily shop report</h2>
          <p style="color:#64748b">{html.escape(report.date)}</p>
          <table style="width:100%;border-collapse:collapse;border:1px solid #e2e8f0">{table}</table>
          <p style="color:#94a3b8;font-size:12px;margin-top:24px">
            Greeting compliance is {round(report.greeting_rate * 100)}% today.
          </p>
        </div>
        """

        deliveries = []
        for recipient in recipients:
            deliveries.append(
                self.alerts.send_email(subject, body, to_email=recipient)
            )
        if not recipients:
            return {
                "recipients": [],
                "deliveries": [],
                "message": "No report recipients configured",
            }
        return {"recipients": recipients, "deliveries": deliveries}
