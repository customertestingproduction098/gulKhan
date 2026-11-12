"""Email/push alert delivery and alert persistence.

Alerts are always written to MongoDB first. Email is delivered via Python's
built-in smtplib; delivery is best-effort, so missing SMTP config or delivery
failures downgrade to "skipped"/"failed" instead of failing the monitoring pipeline.
"""

from __future__ import annotations

import html
import logging
import smtplib
import ssl
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

import requests

from config.settings import Settings, get_settings
from models import Alert, Entry, Interaction
from services.mongodb_handler import DataStoreError, MongoHandler
from services.settings_service import SettingsService
from utils.helpers import to_iso, utcnow

logger = logging.getLogger(__name__)

FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"


class AlertSystem:
    def __init__(
        self,
        store: MongoHandler,
        settings: Settings | None = None,
        owner_settings: SettingsService | None = None,
        events=None,
    ) -> None:
        self.store = store
        self.settings = settings or get_settings()
        self.owner_settings = owner_settings
        self.events = events

    # -------------------------------------------------------------- settings
    def _owner(self) -> dict[str, str]:
        if self.owner_settings is not None:
            return self.owner_settings.alert_contacts()
        return {
            "email": self.settings.owner_email,
            "secondary_email": self.settings.secondary_email,
        }

    def _channel_enabled(self, channel: str) -> bool:
        if self.owner_settings is None:
            return True
        return bool(self.owner_settings.notifications().get(channel, True))

    # ------------------------------------------------------------- delivery
    def send_email(
        self,
        subject: str,
        html_body: str,
        to_email: str | None = None,
        text_body: str | None = None,
    ) -> dict[str, Any]:
        recipient = (to_email or self._owner().get("email") or "").strip()
        if not self._channel_enabled("email"):
            return {
                "sent": False,
                "skipped": True,
                "error": "Email notifications are disabled in settings",
            }
        if not self.settings.smtp_host or not self.settings.mail_from or not recipient:
            return {
                "sent": False,
                "skipped": True,
                "error": "SMTP or owner email not configured",
            }

        try:
            message = MIMEMultipart("alternative")
            message["From"] = self.settings.mail_from
            message["To"] = recipient
            message["Subject"] = subject
            message.attach(MIMEText(text_body or html_body, "plain", "utf-8"))
            message.attach(MIMEText(html_body, "html", "utf-8"))

            context = ssl.create_default_context()
            if self.settings.smtp_secure:
                with smtplib.SMTP_SSL(
                    self.settings.smtp_host, self.settings.smtp_port, context=context
                ) as server:
                    if self.settings.smtp_user:
                        server.login(self.settings.smtp_user, self.settings.smtp_pass)
                    server.sendmail(self.settings.mail_from, [recipient], message.as_string())
            else:
                with smtplib.SMTP(self.settings.smtp_host, self.settings.smtp_port) as server:
                    server.ehlo()
                    server.starttls(context=context)
                    server.ehlo()
                    if self.settings.smtp_user:
                        server.login(self.settings.smtp_user, self.settings.smtp_pass)
                    server.sendmail(self.settings.mail_from, [recipient], message.as_string())

            return {"sent": True, "skipped": False, "to": recipient}
        except Exception as error:  # noqa: BLE001
            logger.error("SMTP delivery failed: %s", error)
            return {"sent": False, "skipped": False, "error": str(error), "to": recipient}

    def send_push(
        self,
        title: str,
        body: str,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self._channel_enabled("push"):
            return {
                "sent": False,
                "skipped": True,
                "error": "Push notifications are disabled in settings",
            }
        if not self.settings.push_configured:
            return {
                "sent": False,
                "skipped": True,
                "error": "Firebase project/credentials/device token not configured",
            }

        try:
            from google.auth.transport.requests import Request
            from google.oauth2 import service_account

            credentials = service_account.Credentials.from_service_account_file(
                self.settings.google_credentials,
                scopes=[FCM_SCOPE],
            )
            credentials.refresh(Request())
            payload = {
                "message": {
                    "token": self.settings.firebase_device_token,
                    "notification": {"title": title[:120], "body": body[:500]},
                    "data": {
                        str(key): str(value)
                        for key, value in (data or {}).items()
                        if value is not None
                    },
                }
            }
            response = requests.post(
                "https://fcm.googleapis.com/v1/projects/"
                f"{self.settings.firebase_project_id}/messages:send",
                headers={
                    "Authorization": f"Bearer {credentials.token}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=15,
            )
            response.raise_for_status()
            return {
                "sent": True,
                "skipped": False,
                "message_id": response.json().get("name"),
            }
        except Exception as error:  # noqa: BLE001
            logger.error("Firebase push failed: %s", error)
            return {"sent": False, "skipped": False, "error": str(error)}

    def _deliver(
        self,
        title: str,
        message: str,
        severity: str,
        html_body: str,
        snapshot_url: str | None = None,
        channel: int | None = None,
    ) -> dict[str, Any]:
        return {
            "email": self.send_email(
                subject=f"[Sentinel AI] {title}"
                + (f" - Camera {channel}" if channel else ""),
                html_body=html_body,
            ),
            "push": self.send_push(
                title,
                message,
                {"severity": severity, "channel": channel, "snapshot": snapshot_url},
            ),
        }

    # --------------------------------------------------------------- alerts
    def create_warehouse_alert(
        self,
        entry: Entry,
        snapshot_url: str | None = None,
    ) -> Alert:
        after_hours = "after hours" if entry.after_hours else "during working hours"
        title = (
            "Unauthorized after-hours entry"
            if entry.after_hours and not entry.authorized
            else "Warehouse entry detected (after hours)"
            if entry.after_hours
            else "Warehouse entry detected"
        )
        local_time = datetime.now().strftime("%d %b %Y, %H:%M:%S")
        message = (
            f"{entry.person_count} person(s) detected on camera {entry.channel} "
            f"{after_hours} at {local_time}. Snapshot attached. Location: {entry.entry_location or 'warehouse'}."
        )
        severity = (
            "critical"
            if entry.after_hours and not entry.authorized
            else "warning"
            if entry.after_hours
            else "info"
        )
        owner = self._owner()
        delivery = self._deliver(
            title,
            message,
            severity,
            self._warehouse_email_html(message, entry, snapshot_url),
            snapshot_url=snapshot_url,
            channel=entry.channel,
        )

        alert = Alert(
            type="warehouse_entry",
            title=title,
            message=message,
            severity=severity,
            channel=entry.channel,
            delivery=delivery,
            owner_id="owner_001",
            owner_email=owner.get("email", ""),
            snapshot=snapshot_url,
            metadata={
                "entry_id": entry.id,
                "authorized": entry.authorized,
                "after_hours": entry.after_hours,
                "person_count": entry.person_count,
                "snapshot_url": snapshot_url,
                "entry_location": entry.entry_location,
            },
        )

        try:
            self.store.insert_alert(alert)
            self.store.update_entry_alert(
                entry.id,
                channels=alert.sent_channels,
                severity=severity,
            )
            entry.alert_sent = bool(alert.id)
            entry.alert_channel = list(alert.sent_channels)
        except DataStoreError as error:
            logger.error("Could not persist alert: %s", error)

        self.store.log(
            "warning",
            f"Warehouse alert raised on channel {entry.channel}",
            {"alert_id": alert.id, "delivery": delivery},
        )
        self._publish("alert.created", alert)
        return alert

    def create_unattended_alert(
        self,
        interaction: Interaction,
        snapshot_url: str | None = None,
    ) -> Alert:
        title = "Unattended customer at the shop"
        message = (
            f"{interaction.customer_count or interaction.person_count} customer(s) "
            f"detected on camera {interaction.channel} without a shopkeeper for "
            f"{round(interaction.duration_seconds)} seconds."
        )
        owner = self._owner()
        delivery = self._deliver(
            title,
            message,
            "critical",
            self._unattended_email_html(message, interaction, snapshot_url),
            snapshot_url=snapshot_url,
            channel=interaction.channel,
        )
        alert = Alert(
            type="unattended_customer",
            title=title,
            message=message,
            severity="critical",
            channel=interaction.channel,
            delivery=delivery,
            owner_id="owner_001",
            owner_email=owner.get("email", ""),
            snapshot=snapshot_url,
            metadata={
                "interaction_id": interaction.id,
                "customer_count": interaction.customer_count,
                "duration_seconds": interaction.duration_seconds,
                "snapshot_url": snapshot_url,
            },
        )
        try:
            self.store.insert_alert(alert)
        except DataStoreError as error:
            logger.error("Could not persist unattended alert: %s", error)
        self.store.log(
            "warning",
            "Unattended customer alert raised",
            {"alert_id": alert.id, "interaction_id": interaction.id},
        )
        self._publish("alert.created", alert)
        return alert

    def create_greeting_report_alert(
        self,
        report,
        delivery: dict[str, Any] | None = None,
    ) -> Alert:
        title = f"{report.missed_greetings} missed greeting(s) yesterday"
        message = (
            f"Daily shop report for {report.date}: {report.successful_greetings} "
            f"greeted, {report.missed_greetings} missed, {report.unattended_customers} "
            f"unattended. Greeting compliance {round(report.greeting_rate * 100)}%."
        )
        owner = self._owner()
        alert = Alert(
            type="greeting_missed",
            title=title,
            message=message,
            severity="warning",
            delivery=delivery or {},
            owner_id="owner_001",
            owner_email=owner.get("email", ""),
            metadata={
                "report_date": report.date,
                "missed_greetings": report.missed_greetings,
                "successful_greetings": report.successful_greetings,
                "unattended_customers": report.unattended_customers,
            },
        )
        try:
            self.store.insert_alert(alert)
        except DataStoreError as error:
            logger.error("Could not persist greeting report alert: %s", error)
        self._publish("alert.created", alert)
        return alert

    def create_system_alert(
        self,
        title: str,
        message: str,
        severity: str = "info",
        channel: int | None = None,
        deliver: bool = False,
    ) -> Alert:
        delivery: dict[str, Any] = {}
        if deliver:
            delivery = self._deliver(
                title,
                message,
                severity,
                self._simple_email_html(title, message, severity),
                channel=channel,
            )

        owner = self._owner()
        alert = Alert(
            type="system",
            title=title,
            message=message,
            severity=severity,
            channel=channel,
            delivery=delivery,
            owner_id="owner_001",
            owner_email=owner.get("email", ""),
        )
        try:
            self.store.insert_alert(alert)
        except DataStoreError as error:
            logger.error("Could not persist system alert: %s", error)
        self._publish("alert.created", alert)
        return alert

    def acknowledge(self, alert_id: str) -> bool:
        acknowledged = self.store.acknowledge_alert(alert_id)
        if acknowledged:
            if self.events is not None:
                self.events.publish("alert.acknowledged", {"id": alert_id})
            self.store.log("info", "Alert acknowledged", {"alert_id": alert_id})
        return acknowledged


    def send_report_email(
        self,
        summary: dict[str, Any],
        to_email: str | None = None,
        subject_prefix: str | None = None,
    ) -> dict[str, Any]:
        days = summary.get("range", {}).get("days", 7)
        shop = summary.get("shop", {})
        warehouse = summary.get("warehouse", {})
        alerts = summary.get("alerts", {})
        generated = datetime.now().strftime("%d %b %Y, %H:%M")

        rows = [
            ("Customers greeted", f"{shop.get('greeted', 0)} of {shop.get('total_interactions', 0)}"),
            ("Greeting compliance", f"{round(shop.get('greeting_rate', 0) * 100)}%"),
            ("Missed greetings", str(shop.get("missed", 0))),
            ("Unattended customers", str(shop.get("unattended", 0))),
            ("Average response time", f"{shop.get('avg_response_time', 0)} s"),
            ("Warehouse entries", str(warehouse.get("total_entries", 0))),
            ("After-hours entries", str(warehouse.get("after_hours_entries", 0))),
            ("Unauthorized entries", str(warehouse.get("unauthorized_entries", 0))),
            ("Alerts raised", str(alerts.get("total", 0))),
            ("Unacknowledged alerts", str(alerts.get("unacknowledged", 0))),
        ]
        row_html = "".join(
            f"<tr><td style='padding:8px 12px;color:#475569'>{html.escape(label)}</td>"
            f"<td style='padding:8px 12px;font-weight:600;color:#0f172a'>{html.escape(value)}</td></tr>"
            for label, value in rows
        )

        body = f"""
        <div style="font-family:Arial,sans-serif;max-width:560px;margin:auto">
          <h2 style="color:#0f172a">Sentinel AI - {days}-day report</h2>
          <p style="color:#64748b">Generated {html.escape(generated)}</p>
          <table style="width:100%;border-collapse:collapse;border:1px solid #e2e8f0;border-radius:12px">
            {row_html}
          </table>
          <p style="color:#94a3b8;font-size:12px;margin-top:24px">
            View the full dashboard for the complete interaction and entry history.
          </p>
        </div>
        """

        subject = subject_prefix or f"{days}-day surveillance report"
        delivery = self.send_email(
            subject=f"[Sentinel AI] {subject}",
            html_body=body,
            to_email=to_email,
        )
        self.store.log(
            "info",
            f"{days}-day report dispatched",
            {"delivery": delivery, "generated_at": to_iso(utcnow())},
        )
        return {
            "delivery": delivery,
            "message": (
                "Report emailed successfully"
                if delivery.get("sent")
                else "Report generated but email was not delivered. Check the email service and a recipient."
            ),
        }

    # ---------------------------------------------------------------- helpers
    def _publish(self, event_type: str, alert: Alert) -> None:
        if self.events is not None:
            self.events.publish(event_type, alert.to_api())

    @staticmethod
    def _warehouse_email_html(
        message: str,
        entry: Entry,
        snapshot_url: str | None,
    ) -> str:
        snapshot = (
            f"<img src='{html.escape(snapshot_url)}' alt='Snapshot' "
            "style='width:100%;border-radius:12px;margin-top:16px' />"
            if snapshot_url
            else ""
        )
        return f"""
        <div style="font-family:Arial,sans-serif;max-width:560px;margin:auto">
          <div style="background:#fee2e2;color:#991b1b;padding:16px 20px;border-radius:12px">
            <h2 style="margin:0">Warehouse security alert</h2>
          </div>
          <p style="color:#334155;font-size:15px">{html.escape(message)}</p>
          <p style="color:#64748b;font-size:13px">
            Camera {entry.channel} - {html.escape(entry.entry_location)} -
            {html.escape(to_iso(entry.created_at) or "")}
          </p>
          {snapshot}
          <p style="color:#94a3b8;font-size:12px;margin-top:24px">
            Please review the live camera and contact the authorities if needed.
          </p>
        </div>
        """

    @staticmethod
    def _unattended_email_html(
        message: str,
        interaction: Interaction,
        snapshot_url: str | None,
    ) -> str:
        snapshot = (
            f"<img src='{html.escape(snapshot_url)}' alt='Snapshot' "
            "style='width:100%;border-radius:12px;margin-top:16px' />"
            if snapshot_url
            else ""
        )
        return f"""
        <div style="font-family:Arial,sans-serif;max-width:560px;margin:auto">
          <div style="background:#fee2e2;color:#991b1b;padding:16px 20px;border-radius:12px">
            <h2 style="margin:0">Shopkeeper not at the counter</h2>
          </div>
          <p style="color:#334155;font-size:15px">{html.escape(message)}</p>
          <p style="color:#64748b;font-size:13px">
            Camera {interaction.channel} - {html.escape(to_iso(interaction.created_at) or "")}
          </p>
          {snapshot}
          <p style="color:#94a3b8;font-size:12px;margin-top:24px">
            This alert escalates until it is acknowledged in the dashboard.
          </p>
        </div>
        """

    @staticmethod
    def _simple_email_html(title: str, message: str, severity: str) -> str:
        color = {
            "critical": ("#fee2e2", "#991b1b"),
            "warning": ("#fef3c7", "#92400e"),
            "info": ("#e0f2fe", "#075985"),
        }.get(severity, ("#e0f2fe", "#075985"))
        return f"""
        <div style="font-family:Arial,sans-serif;max-width:560px;margin:auto">
          <div style="background:{color[0]};color:{color[1]};padding:16px 20px;border-radius:12px">
            <h2 style="margin:0">{html.escape(title)}</h2>
          </div>
          <p style="color:#334155;font-size:15px">{html.escape(message)}</p>
          <p style="color:#94a3b8;font-size:12px">Sentinel AI surveillance</p>
        </div>
        """
