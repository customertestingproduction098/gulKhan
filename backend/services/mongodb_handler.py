"""MongoDB access layer.

The rest of the application never touches PyMongo directly. That keeps the
connection handling, serialization and analytics logic in one place.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from datetime import date, datetime, timedelta
from typing import Any, Iterable

from pymongo import DESCENDING, ReturnDocument
from pymongo import MongoClient
from pymongo.errors import PyMongoError

from config.settings import Settings
from models import Alert, DailyReport, Entry, Interaction
from models.alert import COLLECTION as ALERTS
from models.daily_report import COLLECTION as DAILY_REPORTS
from models.entry import COLLECTION as WAREHOUSE_ENTRIES
from models.interaction import COLLECTION as SHOP_INTERACTIONS
from utils.helpers import (
    date_series_between,
    day_bounds,
    local_date,
    object_id,
    parse_date,
    range_bounds,
    serialize_doc,
    to_iso,
    to_local,
    utcnow,
)

logger = logging.getLogger(__name__)

LOGS = "logs"
SETTINGS = "settings"
REPORT_RUNS = "report_runs"

# Legacy collection names kept only so older databases can still be inspected.
LEGACY_INTERACTIONS = "interactions"
LEGACY_ENTRIES = "entries"


class DataStoreError(RuntimeError):
    """Raised when MongoDB is unavailable."""


class MongoHandler:
    """Thin repository around the surveillance MongoDB database."""

    RECONNECT_AFTER_SECONDS = 20.0
    MAX_ANALYTICS_ROWS = 100_000

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client: MongoClient | None = None
        self._db = None
        self._lock = threading.RLock()
        self._last_error: str | None = None
        self._last_attempt = 0.0

    # ------------------------------------------------------------------ setup
    def connect(self) -> None:
        with self._lock:
            if self._db is not None:
                return

            if self._last_error and (time.monotonic() - self._last_attempt) < self.RECONNECT_AFTER_SECONDS:
                raise DataStoreError(self._last_error)

            self._last_attempt = time.monotonic()
            try:
                client = MongoClient(
                    self.settings.mongo_uri,
                    serverSelectionTimeoutMS=4000,
                    connectTimeoutMS=4000,
                    socketTimeoutMS=8000,
                    tz_aware=True,
                )
                client.admin.command("ping")
                self._client = client
                self._db = client[self.settings.mongo_db]
                self._ensure_indexes()
                self._last_error = None
                logger.info("Connected to MongoDB database '%s'", self.settings.mongo_db)
            except Exception as error:
                self._last_error = (
                    f"MongoDB unavailable: {type(error).__name__}: {error}"
                )
                logger.exception(self._last_error)
                raise DataStoreError(self._last_error) from error

    def _ensure_indexes(self) -> None:
        assert self._db is not None
        self._db[SHOP_INTERACTIONS].create_index([("created_at", DESCENDING)])
        self._db[SHOP_INTERACTIONS].create_index(
            [("channel", 1), ("created_at", DESCENDING)]
        )
        self._db[SHOP_INTERACTIONS].create_index(
            [("interaction_type", 1), ("created_at", DESCENDING)]
        )
        self._db[WAREHOUSE_ENTRIES].create_index([("created_at", DESCENDING)])
        self._db[WAREHOUSE_ENTRIES].create_index(
            [("after_hours", 1), ("created_at", DESCENDING)]
        )
        self._db[ALERTS].create_index([("created_at", DESCENDING)])
        self._db[ALERTS].create_index(
            [("acknowledged", 1), ("created_at", DESCENDING)]
        )
        self._db[ALERTS].create_index(
            [("severity", 1), ("acknowledged", 1), ("created_at", DESCENDING)]
        )
        self._db[DAILY_REPORTS].create_index([("date", 1)], unique=True)
        self._db[LOGS].create_index([("created_at", DESCENDING)])
        self._db[REPORT_RUNS].create_index([("created_at", DESCENDING)])

    @property
    def db(self):
        self.connect()
        return self._db

    def health(self) -> dict[str, Any]:
        try:
            self.connect()
            self._client.admin.command("ping")
            return {"connected": True, "database": self.settings.mongo_db, "error": None}
        except (DataStoreError, PyMongoError) as error:
            return {"connected": False, "database": self.settings.mongo_db, "error": str(error)}
        except Exception as error:  # noqa: BLE001 - health checks must never raise
            return {
                "connected": False,
                "database": self.settings.mongo_db,
                "error": f"{type(error).__name__}: {error}",
            }

    def close(self) -> None:
        with self._lock:
            if self._client is not None:
                self._client.close()
            self._client = None
            self._db = None

    # ------------------------------------------------------------- mutations
    def insert_interaction(self, interaction: Interaction) -> str:
        try:
            result = self.db[SHOP_INTERACTIONS].insert_one(interaction.to_document())
            interaction.id = str(result.inserted_id)
            return interaction.id
        except PyMongoError as error:
            raise DataStoreError(str(error)) from error

    def insert_entry(self, entry: Entry) -> str:
        try:
            result = self.db[WAREHOUSE_ENTRIES].insert_one(entry.to_document())
            entry.id = str(result.inserted_id)
            return entry.id
        except PyMongoError as error:
            raise DataStoreError(str(error)) from error

    def insert_alert(self, alert: Alert) -> str:
        try:
            result = self.db[ALERTS].insert_one(alert.to_document())
            alert.id = str(result.inserted_id)
            return alert.id
        except PyMongoError as error:
            raise DataStoreError(str(error)) from error

    def log(self, level: str, message: str, context: dict[str, Any] | None = None) -> None:
        """Best-effort structured application log; never breaks a request."""

        try:
            self.db[LOGS].insert_one(
                {
                    "level": level,
                    "message": message[:1000],
                    "context": context or {},
                    "created_at": utcnow(),
                }
            )
        except (DataStoreError, PyMongoError) as error:
            logger.debug("Skipping Mongo log write: %s", error)

    def acknowledge_alert(self, alert_id: str) -> bool:
        identifier = object_id(alert_id)
        if identifier is None:
            return False
        try:
            result = self.db[ALERTS].update_one(
                {"_id": identifier},
                {
                    "$set": {
                        "acknowledged": True,
                        "acknowledged_at": utcnow(),
                        "status": "ACKNOWLEDGED",
                    }
                },
            )
            return result.matched_count > 0
        except PyMongoError as error:
            raise DataStoreError(str(error)) from error

    def update_entry_alert(
        self,
        entry_id: str | None,
        channels: Iterable[str] | None = None,
        severity: str | None = None,
    ) -> None:
        identifier = object_id(entry_id)
        if identifier is None:
            return
        update: dict[str, Any] = {"alert_sent": True}
        if channels is not None:
            update["alert_channel"] = sorted(set(channels))
        if severity:
            update["severity"] = severity
        try:
            self.db[WAREHOUSE_ENTRIES].update_one(
                {"_id": identifier}, {"$set": update}
            )
        except PyMongoError:
            logger.debug("Could not flag entry %s as alerted", entry_id)

    def set_entry_false_alarm(self, entry_id: str, value: bool = True) -> bool:
        identifier = object_id(entry_id)
        if identifier is None:
            return False
        try:
            result = self.db[WAREHOUSE_ENTRIES].update_one(
                {"_id": identifier}, {"$set": {"false_alarm": bool(value)}}
            )
            return result.matched_count > 0
        except PyMongoError as error:
            raise DataStoreError(str(error)) from error

    def record_alert_escalation(
        self,
        alert_id: str,
        stage: str,
        delivery: dict[str, Any],
    ) -> bool:
        """Atomically append an escalation stage; ignore duplicate ticks."""

        identifier = object_id(alert_id)
        if identifier is None:
            return False
        entry = {
            "stage": stage,
            "sent_at": utcnow(),
            "delivery": delivery,
        }
        try:
            result = self.db[ALERTS].update_one(
                {"_id": identifier, "escalations.stage": {"$ne": stage}},
                {"$push": {"escalations": entry}, "$set": {"last_escalation_at": utcnow()}},
            )
            return result.modified_count > 0
        except PyMongoError as error:
            raise DataStoreError(str(error)) from error

    # -------------------------------------------------------------- settings
    def get_owner_settings(self) -> dict[str, Any] | None:
        try:
            document = self.db[SETTINGS].find_one({"_id": "owner"})
            if not document:
                return None
            document.pop("_id", None)
            return serialize_doc(document)
        except PyMongoError as error:
            raise DataStoreError(str(error)) from error

    def save_owner_settings(self, settings_document: dict[str, Any]) -> dict[str, Any]:
        payload = dict(settings_document)
        payload.pop("_id", None)
        payload["updated_at"] = utcnow()
        try:
            self.db[SETTINGS].update_one(
                {"_id": "owner"},
                {"$set": payload, "$setOnInsert": {"created_at": utcnow()}},
                upsert=True,
            )
            return self.get_owner_settings() or serialize_doc(payload)
        except PyMongoError as error:
            raise DataStoreError(str(error)) from error

    # ---------------------------------------------------------- daily reports
    @staticmethod
    def _daily_document(report: DailyReport) -> dict[str, Any]:
        return {
            "date": report.date,
            "shop_id": report.shop_id,
            "total_interactions": report.total_interactions,
            "total_customers": report.total_customers,
            "successful_greetings": report.successful_greetings,
            "missed_greetings": report.missed_greetings,
            "unattended_customers": report.unattended_customers,
            "greeting_rate": report.greeting_rate,
            "peak_hour": report.peak_hour,
            "avg_response_time": report.avg_response_time,
            "shopkeeper_presence_rate": report.shopkeeper_presence_rate,
            "warehouse_entries": report.warehouse_entries,
            "after_hours_entries": report.after_hours_entries,
            "unauthorized_entries": report.unauthorized_entries,
            "false_alarm_entries": report.false_alarm_entries,
            "alerts": report.alerts,
            "unacknowledged_alerts": report.unacknowledged_alerts,
            "emailed_at": report.emailed_at,
            "created_at": report.created_at,
        }

    def upsert_daily_report(self, report: DailyReport) -> dict[str, Any]:
        payload = self._daily_document(report)
        payload.pop("created_at", None)
        try:
            document = self.db[DAILY_REPORTS].find_one_and_update(
                {"date": report.date},
                {
                    "$set": payload,
                    "$setOnInsert": {"created_at": utcnow()},
                },
                upsert=True,
                return_document=ReturnDocument.AFTER,
            )
            return DailyReport.from_document(document).to_api() if document else report.to_api()
        except PyMongoError as error:
            raise DataStoreError(str(error)) from error

    def mark_daily_report_emailed(self, report_date: str, emailed_at: datetime | None = None) -> None:
        try:
            self.db[DAILY_REPORTS].update_one(
                {"date": report_date},
                {"$set": {"emailed_at": emailed_at or utcnow()}},
            )
        except PyMongoError:
            logger.debug("Could not mark daily report %s as emailed", report_date)

    def get_daily_report(self, report_date: str) -> dict[str, Any] | None:
        try:
            document = self.db[DAILY_REPORTS].find_one({"date": report_date})
            return DailyReport.from_document(document).to_api() if document else None
        except PyMongoError as error:
            raise DataStoreError(str(error)) from error

    def list_daily_reports(self, limit: int = 30) -> list[dict[str, Any]]:
        try:
            cursor = (
                self.db[DAILY_REPORTS]
                .find({})
                .sort("date", DESCENDING)
                .limit(max(1, min(limit, 365)))
            )
            return [DailyReport.from_document(doc).to_api() for doc in cursor]
        except PyMongoError as error:
            raise DataStoreError(str(error)) from error

    # ------------------------------------------------------------ report runs
    def get_report_run(self, key: str) -> dict[str, Any] | None:
        try:
            document = self.db[REPORT_RUNS].find_one({"_id": key})
            if not document:
                return None
            document.pop("_id", None)
            return serialize_doc(document)
        except PyMongoError as error:
            raise DataStoreError(str(error)) from error

    def mark_report_run(self, key: str, payload: dict[str, Any] | None = None) -> None:
        try:
            self.db[REPORT_RUNS].update_one(
                {"_id": key},
                {
                    "$set": {"payload": payload or {}, "created_at": utcnow()},
                },
                upsert=True,
            )
        except PyMongoError as error:
            raise DataStoreError(str(error)) from error

    # --------------------------------------------------------------- queries
    def _base_filter(
        self,
        *,
        days: int | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        channel: int | None = None,
    ) -> dict[str, Any]:
        query: dict[str, Any] = {}
        if start or end:
            window: dict[str, Any] = {}
            if start:
                window["$gte"] = start
            if end:
                window["$lt"] = end
            query["created_at"] = window
        elif days:
            now = utcnow()
            query["created_at"] = {"$gte": now - timedelta(days=max(1, int(days)))}
        if channel is not None:
            if getattr(self.settings, "is_camera_allowed", lambda c: True)(channel):
                query["channel"] = int(channel)
            else:
                query["channel"] = -1
        else:
            allowed = getattr(self.settings, "allowed_cameras", None)
            if allowed:
                query["channel"] = {"$in": list(allowed)}
        return query

    def list_interactions(
        self,
        limit: int = 50,
        days: int | None = None,
        channel: int | None = None,
        interaction_type: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        query = self._base_filter(days=days, start=start, end=end, channel=channel)
        if interaction_type:
            query["interaction_type"] = interaction_type.upper()
        try:
            collection = self.db[SHOP_INTERACTIONS]
            total = collection.count_documents(query)
            cursor = (
                collection.find(query)
                .sort("created_at", DESCENDING)
                .limit(max(1, min(limit, 1000)))
            )
            items = [Interaction.from_document(doc).to_api() for doc in cursor]
            return items, total
        except PyMongoError as error:
            raise DataStoreError(str(error)) from error

    def get_interaction(self, interaction_id: str) -> dict[str, Any] | None:
        identifier = object_id(interaction_id)
        if identifier is None:
            return None
        try:
            document = self.db[SHOP_INTERACTIONS].find_one({"_id": identifier})
            return Interaction.from_document(document).to_api() if document else None
        except PyMongoError as error:
            raise DataStoreError(str(error)) from error

    def list_entries(
        self,
        limit: int = 50,
        days: int | None = None,
        channel: int | None = None,
        after_hours: bool | None = None,
        false_alarm: bool | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        query = self._base_filter(days=days, start=start, end=end, channel=channel)
        if after_hours is not None:
            query["after_hours"] = after_hours
        if false_alarm is not None:
            query["false_alarm"] = false_alarm
        try:
            collection = self.db[WAREHOUSE_ENTRIES]
            total = collection.count_documents(query)
            cursor = (
                collection.find(query)
                .sort("created_at", DESCENDING)
                .limit(max(1, min(limit, 1000)))
            )
            items = [Entry.from_document(doc).to_api() for doc in cursor]
            return items, total
        except PyMongoError as error:
            raise DataStoreError(str(error)) from error

    def get_entry(self, entry_id: str) -> dict[str, Any] | None:
        identifier = object_id(entry_id)
        if identifier is None:
            return None
        try:
            document = self.db[WAREHOUSE_ENTRIES].find_one({"_id": identifier})
            return Entry.from_document(document).to_api() if document else None
        except PyMongoError as error:
            raise DataStoreError(str(error)) from error

    def list_alerts(
        self,
        limit: int = 50,
        days: int | None = None,
        acknowledged: bool | None = None,
        severity: str | None = None,
        alert_type: str | None = None,
        channel: int | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        query = self._base_filter(days=days, start=start, end=end, channel=channel)
        allowed = getattr(self.settings, "allowed_cameras", None)
        if channel is None and allowed:
            query.pop("channel", None)
            query["$or"] = [
                {"channel": {"$in": list(allowed)}},
                {"channel": None},
                {"channel": {"$exists": False}},
            ]
        if acknowledged is not None:
            query["acknowledged"] = acknowledged
        if severity:
            query["severity"] = severity
        if alert_type:
            query["type"] = alert_type
        try:
            collection = self.db[ALERTS]
            total = collection.count_documents(query)
            cursor = (
                collection.find(query)
                .sort("created_at", DESCENDING)
                .limit(max(1, min(limit, 500)))
            )
            items = [Alert.from_document(doc).to_api() for doc in cursor]
            return items, total
        except PyMongoError as error:
            raise DataStoreError(str(error)) from error

    def pending_alerts(self, limit: int = 100) -> tuple[list[dict[str, Any]], int]:
        return self.list_alerts(limit=limit, acknowledged=False)

    def get_alert(self, alert_id: str) -> dict[str, Any] | None:
        identifier = object_id(alert_id)
        if identifier is None:
            return None
        try:
            document = self.db[ALERTS].find_one({"_id": identifier})
            return Alert.from_document(document).to_api() if document else None
        except PyMongoError as error:
            raise DataStoreError(str(error)) from error

    # ------------------------------------------------------------- analytics
    def _fetch_range(
        self,
        collection: str,
        start: datetime,
        end: datetime,
    ) -> list[dict[str, Any]]:
        try:
            cursor = (
                self.db[collection]
                .find({"created_at": {"$gte": start, "$lt": end}})
                .sort("created_at", 1)
                .limit(self.MAX_ANALYTICS_ROWS)
            )
            return list(cursor)
        except PyMongoError as error:
            raise DataStoreError(str(error)) from error

    @staticmethod
    def _peak_hour(hours: Counter) -> int | None:
        if not hours:
            return None
        return max(hours.items(), key=lambda item: (item[1], -item[0]))[0]

    @staticmethod
    def _hourly(hours: Counter) -> list[dict[str, Any]]:
        return [{"hour": hour, "count": hours.get(hour, 0)} for hour in range(24)]

    def _build_daily(
        self,
        label: str,
        interactions: list[Interaction],
        entries: list[Entry],
        alerts: list[Alert],
    ) -> DailyReport:
        total = len(interactions)
        successful = sum(1 for item in interactions if item.interaction_type == "SUCCESSFUL")
        missed = sum(1 for item in interactions if item.interaction_type == "MISSED")
        unattended = sum(1 for item in interactions if item.interaction_type == "UNATTENDED")
        customers = sum(item.customer_count or (1 if item.person_count else 0) for item in interactions)
        presence = sum(1 for item in interactions if item.shopkeeper_present)
        response_times = [
            item.response_time_seconds
            for item in interactions
            if item.response_time_seconds is not None
        ]
        hours: Counter = Counter()
        for item in interactions:
            local = to_local(item.created_at)
            if local:
                hours[local.hour] += 1

        return DailyReport(
            date=label,
            shop_id=interactions[0].shop_id if interactions else self.settings.shop_id,
            total_interactions=total,
            total_customers=customers,
            successful_greetings=successful,
            missed_greetings=missed,
            unattended_customers=unattended,
            greeting_rate=round(successful / total, 4) if total else 0.0,
            peak_hour=self._peak_hour(hours),
            avg_response_time=round(sum(response_times) / len(response_times), 2)
            if response_times
            else 0.0,
            shopkeeper_presence_rate=round(presence / total, 4) if total else 0.0,
            warehouse_entries=len(entries),
            after_hours_entries=sum(1 for item in entries if item.after_hours),
            unauthorized_entries=sum(1 for item in entries if not item.authorized),
            false_alarm_entries=sum(1 for item in entries if item.false_alarm),
            alerts=len(alerts),
            unacknowledged_alerts=sum(1 for item in alerts if not item.acknowledged),
        )

    def _daily_models(
        self,
        start: date,
        end: date,
    ) -> list[DailyReport]:
        start_utc, _ = day_bounds(start)
        _, end_utc = day_bounds(end)
        allowed = set(getattr(self.settings, "allowed_cameras", []))
        interactions = [
            Interaction.from_document(doc)
            for doc in self._fetch_range(SHOP_INTERACTIONS, start_utc, end_utc)
            if not allowed or doc.get("channel") in allowed
        ]
        entries = [
            Entry.from_document(doc)
            for doc in self._fetch_range(WAREHOUSE_ENTRIES, start_utc, end_utc)
            if not allowed or doc.get("channel") in allowed
        ]
        alerts = [
            Alert.from_document(doc)
            for doc in self._fetch_range(ALERTS, start_utc, end_utc)
            if not allowed or doc.get("channel") is None or doc.get("channel") in allowed
        ]

        labels = date_series_between(start, end)
        grouped_interactions: dict[str, list[Interaction]] = {label: [] for label in labels}
        grouped_entries: dict[str, list[Entry]] = {label: [] for label in labels}
        grouped_alerts: dict[str, list[Alert]] = {label: [] for label in labels}

        for item in interactions:
            label = local_date(item.created_at).isoformat()
            if label in grouped_interactions:
                grouped_interactions[label].append(item)
        for item in entries:
            label = local_date(item.created_at).isoformat()
            if label in grouped_entries:
                grouped_entries[label].append(item)
        for item in alerts:
            label = local_date(item.created_at).isoformat()
            if label in grouped_alerts:
                grouped_alerts[label].append(item)

        return [
            self._build_daily(
                label,
                grouped_interactions[label],
                grouped_entries[label],
                grouped_alerts[label],
            )
            for label in labels
        ]

    def daily_report(self, target: date | str | None = None) -> DailyReport:
        target_date = parse_date(target) if isinstance(target, str) else (target or local_date())
        reports = self._daily_models(target_date, target_date)
        return reports[0]

    def summary(
        self,
        days: int = 7,
        start: date | None = None,
        end: date | None = None,
    ) -> dict[str, Any]:
        start_utc, end_utc = range_bounds(days=days, start=start, end=end)
        start_date = to_local(start_utc).date()
        end_date = (to_local(end_utc) - timedelta(microseconds=1)).date()
        reports = self._daily_models(start_date, end_date)

        total_interactions = sum(item.total_interactions for item in reports)
        successful = sum(item.successful_greetings for item in reports)
        missed = sum(item.missed_greetings for item in reports)
        unattended = sum(item.unattended_customers for item in reports)
        customers = sum(item.total_customers for item in reports)
        presence_rates = [item.shopkeeper_presence_rate for item in reports if item.total_interactions]
        response_values = [
            item.avg_response_time for item in reports if item.avg_response_time
        ]
        total_entries = sum(item.warehouse_entries for item in reports)
        after_hours = sum(item.after_hours_entries for item in reports)
        unauthorized = sum(item.unauthorized_entries for item in reports)
        false_alarms = sum(item.false_alarm_entries for item in reports)
        total_alerts = sum(item.alerts for item in reports)
        unacknowledged = sum(item.unacknowledged_alerts for item in reports)

        allowed = set(getattr(self.settings, "allowed_cameras", []))
        alert_docs = [
            Alert.from_document(doc)
            for doc in self._fetch_range(ALERTS, start_utc, end_utc)
            if not allowed or doc.get("channel") is None or doc.get("channel") in allowed
        ]
        severity = Counter(item.severity for item in alert_docs)

        return {
            "range": {
                "days": len(reports),
                "since": start_utc.isoformat(),
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
            },
            "shop": {
                "total_interactions": total_interactions,
                "total_customers": customers,
                "greeted": successful,
                "successful_greetings": successful,
                "missed": missed,
                "missed_greetings": missed,
                "unattended": unattended,
                "greeting_rate": round(successful / total_interactions, 4)
                if total_interactions
                else 0.0,
                "avg_response_time": round(sum(response_values) / len(response_values), 2)
                if response_values
                else 0.0,
                "shopkeeper_presence_rate": round(
                    sum(presence_rates) / len(presence_rates), 4
                )
                if presence_rates
                else 0.0,
                "peak_hour": self._peak_hour(
                    Counter(
                        {
                            report.peak_hour: report.total_interactions
                            for report in reports
                            if report.peak_hour is not None
                        }
                    )
                ),
            },
            "warehouse": {
                "total_entries": total_entries,
                "unauthorized_entries": unauthorized,
                "after_hours_entries": after_hours,
                "false_alarms": false_alarms,
                "false_alarm_rate": round(false_alarms / total_entries, 4)
                if total_entries
                else 0.0,
            },
            "alerts": {
                "total": total_alerts,
                "unacknowledged": unacknowledged,
                "by_severity": {
                    "critical": severity.get("critical", 0),
                    "warning": severity.get("warning", 0),
                    "info": severity.get("info", 0),
                },
            },
            "daily": [
                {
                    "date": report.date,
                    "interactions": report.total_interactions,
                    "customers": report.total_customers,
                    "greeted": report.successful_greetings,
                    "missed": report.missed_greetings,
                    "unattended": report.unattended_customers,
                    "greeting_rate": report.greeting_rate,
                    "entries": report.warehouse_entries,
                    "after_hours": report.after_hours_entries,
                    "alerts": report.alerts,
                }
                for report in reports
            ],
        }

    def interaction_stats(self, days: int = 1) -> dict[str, Any]:
        start_utc, end_utc = range_bounds(days=days)
        interactions = [
            Interaction.from_document(doc)
            for doc in self._fetch_range(SHOP_INTERACTIONS, start_utc, end_utc)
        ]
        hours: Counter = Counter()
        for item in interactions:
            local = to_local(item.created_at)
            if local:
                hours[local.hour] += 1
        total = len(interactions)
        successful = sum(1 for item in interactions if item.interaction_type == "SUCCESSFUL")
        missed = sum(1 for item in interactions if item.interaction_type == "MISSED")
        unattended = sum(1 for item in interactions if item.interaction_type == "UNATTENDED")
        response_times = [
            item.response_time_seconds
            for item in interactions
            if item.response_time_seconds is not None
        ]
        presence = sum(1 for item in interactions if item.shopkeeper_present)
        return {
            "range": {"days": max(1, int(days))},
            "total_interactions": total,
            "total_customers": sum(
                item.customer_count or (1 if item.person_count else 0)
                for item in interactions
            ),
            "successful_greetings": successful,
            "missed_greetings": missed,
            "unattended_customers": unattended,
            "greeting_rate": round(successful / total, 4) if total else 0.0,
            "avg_response_time": round(sum(response_times) / len(response_times), 2)
            if response_times
            else 0.0,
            "shopkeeper_presence_rate": round(presence / total, 4) if total else 0.0,
            "peak_hour": self._peak_hour(hours),
            "hourly": self._hourly(hours),
        }

    def entry_stats(self, days: int = 1) -> dict[str, Any]:
        start_utc, end_utc = range_bounds(days=days)
        entries = [
            Entry.from_document(doc)
            for doc in self._fetch_range(WAREHOUSE_ENTRIES, start_utc, end_utc)
        ]
        hours: Counter = Counter()
        locations: Counter = Counter()
        for item in entries:
            local = to_local(item.created_at)
            if local:
                hours[local.hour] += 1
            locations[item.entry_location or "main_gate"] += 1
        total = len(entries)
        false_alarms = sum(1 for item in entries if item.false_alarm)
        return {
            "range": {"days": max(1, int(days))},
            "total_entries": total,
            "after_hours_entries": sum(1 for item in entries if item.after_hours),
            "unauthorized_entries": sum(1 for item in entries if not item.authorized),
            "false_alarms": false_alarms,
            "false_alarm_rate": round(false_alarms / total, 4) if total else 0.0,
            "peak_hour": self._peak_hour(hours),
            "hourly": self._hourly(hours),
            "by_location": [
                {"location": name, "count": count}
                for name, count in locations.most_common()
            ],
        }

    def weekly_report(self, start: date, end: date) -> dict[str, Any]:
        reports = self._daily_models(start, end)
        return self._period_report("weekly", start, end, reports)

    def monthly_report(self, start: date, end: date) -> dict[str, Any]:
        reports = self._daily_models(start, end)
        return self._period_report("monthly", start, end, reports)

    def _period_report(
        self,
        kind: str,
        start: date,
        end: date,
        reports: list[DailyReport],
    ) -> dict[str, Any]:
        total_interactions = sum(item.total_interactions for item in reports)
        successful = sum(item.successful_greetings for item in reports)
        total_entries = sum(item.warehouse_entries for item in reports)
        false_alarms = sum(item.false_alarm_entries for item in reports)
        return {
            "period": kind,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "totals": {
                "total_interactions": total_interactions,
                "total_customers": sum(item.total_customers for item in reports),
                "successful_greetings": successful,
                "missed_greetings": sum(item.missed_greetings for item in reports),
                "unattended_customers": sum(item.unattended_customers for item in reports),
                "greeting_rate": round(successful / total_interactions, 4)
                if total_interactions
                else 0.0,
                "warehouse_entries": total_entries,
                "after_hours_entries": sum(
                    item.after_hours_entries for item in reports
                ),
                "unauthorized_entries": sum(
                    item.unauthorized_entries for item in reports
                ),
                "false_alarms": false_alarms,
                "false_alarm_rate": round(false_alarms / total_entries, 4)
                if total_entries
                else 0.0,
                "alerts": sum(item.alerts for item in reports),
                "unacknowledged_alerts": sum(
                    item.unacknowledged_alerts for item in reports
                ),
            },
            "daily": [item.to_api() for item in reports],
        }

    def counts(self) -> dict[str, int]:
        try:
            return {
                "interactions": self.db[SHOP_INTERACTIONS].count_documents({}),
                "entries": self.db[WAREHOUSE_ENTRIES].count_documents({}),
                "alerts": self.db[ALERTS].count_documents({}),
                "daily_reports": self.db[DAILY_REPORTS].count_documents({}),
            }
        except PyMongoError as error:
            raise DataStoreError(str(error)) from error

    def latest_timestamp(self, collection: str) -> datetime | None:
        try:
            document = self.db[collection].find_one(
                {}, sort=[("created_at", DESCENDING)]
            )
            return document.get("created_at") if document else None
        except PyMongoError:
            return None
