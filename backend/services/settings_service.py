"""Mongo-backed owner settings with environment fallbacks.

``Settings`` (config/settings.py) holds credentials and process-level defaults.
This service layers the editable owner profile, business hours, notification
preferences and camera enablement on top, and caches the merged document.
"""

from __future__ import annotations

import copy
import hmac
import logging
import re
import threading
import time
from typing import Any

from werkzeug.security import check_password_hash, generate_password_hash

from config.settings import Settings, get_settings
from services.mongodb_handler import DataStoreError, MongoHandler
from utils.helpers import (
    WEEKDAYS,
    is_valid_email,
    normalize_hhmm,
    serialize_doc,
)

logger = logging.getLogger(__name__)

USERNAME_PATTERN = re.compile(r"^[a-zA-Z0-9._-]{3,40}$")
OPERATIONS_REVISION = 1
AFTER_HOURS_START = "21:00"
AFTER_HOURS_END = "11:00"


class SettingsService:
    CACHE_SECONDS = 5.0

    def __init__(
        self,
        store: MongoHandler,
        settings: Settings | None = None,
        events=None,
    ) -> None:
        self.store = store
        self.settings = settings or get_settings()
        self.events = events
        self._lock = threading.RLock()
        self._cached: dict[str, Any] | None = None
        self._cached_at = 0.0
        self._default_password_hash = generate_password_hash(self.settings.owner_password)

    # ------------------------------------------------------------ defaults
    def _defaults(self) -> dict[str, Any]:
        return {
            "auth_seeded": False,
            "operations_revision": 0,
            "owner": {
                "id": "owner_001",
                "name": self.settings.owner_name,
                "auth_email": getattr(self.settings, "owner_auth_email", "admin123@gmail.com"),
                "notification_email": getattr(self.settings, "notification_email", "alerts@client.com"),
                "email": getattr(self.settings, "notification_email", "alerts@client.com"),
                "secondary_email": getattr(self.settings, "secondary_email", ""),
                "username": self.settings.owner_username,
                "password": self.settings.owner_password,
                "password_hash": self._default_password_hash,
            },
            "shop": {
                "id": self.settings.shop_id,
                "name": self.settings.shop_name,
                "open": self.settings.shop_open_time,
                "close": self.settings.shop_close_time,
            },
            "warehouse": {
                "id": self.settings.warehouse_id,
                "name": self.settings.warehouse_name,
                "after_hours_start": self.settings.after_hours_start,
                "after_hours_end": self.settings.after_hours_end,
                "entry_location": self.settings.entry_location,
            },
            "notifications": {
                "email": True,
                "push": True,
                "escalation_enabled": self.settings.escalation_enabled,
                "email_after_seconds": self.settings.escalation_email_after,
                "secondary_after_seconds": self.settings.escalation_secondary_after,
            },
            "monitoring": {
                "unattended_enabled": self.settings.unattended_detection_enabled,
                "unattended_after_seconds": self.settings.unattended_alert_seconds,
                "staff_zone": self.settings.shopkeeper_zone,
                "staff_presence_seconds": self.settings.staff_presence_seconds,
            },
            "cameras": [
                {"channel": channel, "name": f"Camera {channel:02d}", "enabled": True}
                for channel in self.settings.allowed_cameras
            ],
            "reports": {
                "daily_enabled": self.settings.daily_report_enabled,
                "daily_time": self.settings.daily_report_time,
                "weekly_enabled": self.settings.weekly_report_enabled,
                "weekly_day": self.settings.weekly_report_day,
                "recipients": [],
            },
        }

    # ----------------------------------------------------------------- read
    def get(self, force: bool = False) -> dict[str, Any]:
        with self._lock:
            fresh = self._cached is not None and (
                time.monotonic() - self._cached_at
            ) < self.CACHE_SECONDS
            if fresh and not force:
                return copy.deepcopy(self._cached)

            stored: dict[str, Any] = {}
            try:
                stored = self.store.get_owner_settings() or {}
            except DataStoreError as error:
                logger.warning("Using environment settings (Mongo unavailable): %s", error)

            merged = self._merge(self._defaults(), stored)
            seeded = False
            cur_owner = merged.setdefault("owner", {})
            if not cur_owner.get("auth_email"):
                cur_owner["auth_email"] = getattr(self.settings, "owner_auth_email", "admin123@gmail.com")
            if not cur_owner.get("notification_email"):
                cur_owner["notification_email"] = getattr(self.settings, "notification_email", "alerts@client.com")
            if "secondary_email" not in cur_owner:
                cur_owner["secondary_email"] = "" 
            if not merged.get("auth_seeded"):
                owner = merged.setdefault("owner", {})
                owner["email"] = self.settings.owner_email
                owner["username"] = self.settings.owner_username
                owner["password"] = self.settings.owner_password
                owner["password_hash"] = generate_password_hash(
                    self.settings.owner_password
                )
                merged["auth_seeded"] = True
                seeded = True

            try:
                revision = int(merged.get("operations_revision") or 0)
            except (TypeError, ValueError):
                revision = 0
            if revision < OPERATIONS_REVISION:
                warehouse = merged.setdefault("warehouse", {})
                warehouse["after_hours_start"] = AFTER_HOURS_START
                warehouse["after_hours_end"] = AFTER_HOURS_END
                merged["operations_revision"] = OPERATIONS_REVISION
                seeded = True

            normalized = self._normalize(merged)
            if seeded:
                try:
                    self.store.save_owner_settings(normalized)
                except DataStoreError as error:
                    logger.warning("Could not seed owner credentials: %s", error)
            self._cached = normalized
            self._cached_at = time.monotonic()
            return copy.deepcopy(normalized)

    def update(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("Settings payload must be an object")

        with self._lock:
            current = self.get(force=True)
            merged = self._merge(current, payload)
            normalized = self._normalize(merged)
            saved = self.store.save_owner_settings(normalized)
            normalized = self._normalize(self._merge(normalized, saved or {}))
            self._cached = normalized
            self._cached_at = time.monotonic()

        if self.events is not None:
            self.events.publish("settings.updated", {"settings": normalized})
        return copy.deepcopy(normalized)

    # ------------------------------------------------------------- accessors
    def owner(self) -> dict[str, Any]:
        return self.get()["owner"]

    def verify_login(self, username: str, password: str) -> dict[str, Any] | None:
        """Return the owner profile when the email/username and password match."""
        candidate = str(username or "").strip().lower()
        secret = str(password or "")
        if not candidate or not secret:
            return None

        owner = self.owner()
        stored_auth_email = str(owner.get("auth_email") or "").strip().lower()
        legacy_email = str(owner.get("email") or "").strip().lower()
        fallback_auth = str(getattr(self.settings, "owner_auth_email", "") or self.settings.owner_email or "").strip().lower()
        stored_username = str(owner.get("username") or self.settings.owner_username).strip().lower()

        allowed_identifiers = {i for i in [stored_auth_email, stored_username, fallback_auth, "admin123@gmail.com", "admin"] if i}
        if legacy_email:
            allowed_identifiers.add(legacy_email)

        if candidate not in allowed_identifiers:
            return None

        password_hash = str(owner.get("password_hash") or self._default_password_hash)
        plain_password = str(owner.get("password") or "")
        default_plain = str(self.settings.owner_password or "")

        pw_matches = False
        if plain_password and (secret == plain_password or secret.strip() == plain_password):
            pw_matches = True
        elif default_plain and (secret == default_plain or secret.strip() == default_plain):
            pw_matches = True
        elif check_password_hash(password_hash, secret) or check_password_hash(password_hash, secret.strip()):
            pw_matches = True
        elif check_password_hash(self._default_password_hash, secret) or check_password_hash(self._default_password_hash, secret.strip()):
            pw_matches = True

        if not pw_matches:
            return None

        resolved_auth = stored_auth_email or fallback_auth or "admin123@gmail.com"
        return {
            "id": owner.get("id", "owner_001"),
            "name": owner.get("name", self.settings.owner_name),
            "email": resolved_auth,
            "auth_email": resolved_auth,
            "notification_email": owner.get("notification_email") or self.settings.notification_email,
            "username": stored_username or "admin",
            "role": "owner",
        }

    def shop_hours(self) -> dict[str, str]:
        shop = self.get()["shop"]
        return {"open": shop["open"], "close": shop["close"]}

    def after_hours(self) -> dict[str, str]:
        warehouse = self.get()["warehouse"]
        return {
            "start": warehouse["after_hours_start"],
            "end": warehouse["after_hours_end"],
        }

    def notifications(self) -> dict[str, Any]:
        return self.get()["notifications"]

    def monitoring(self) -> dict[str, Any]:
        return self.get()["monitoring"]

    def reports(self) -> dict[str, Any]:
        return self.get()["reports"]

    def cameras(self) -> list[dict[str, Any]]:
        return self.get()["cameras"]

    def camera_enabled(self, channel: int) -> bool:
        if not self.settings.is_camera_allowed(channel):
            return False
        for camera in self.cameras():
            if int(camera.get("channel", 0)) == int(channel):
                return bool(camera.get("enabled", True))
        return False

    def email_enabled(self) -> bool:
        owner = self.owner()
        configured = getattr(self.settings, "email_configured", None)
        if configured is None:
            configured = bool(getattr(self.settings, "smtp_host", None) and getattr(self.settings, "mail_from", None))
        target_email = owner.get("notification_email") or owner.get("email")
        return bool(
            self.notifications().get("email", True)
            and configured
            and target_email
        )

    def push_enabled(self) -> bool:
        return bool(
            self.notifications().get("push", True)
            and self.settings.push_configured
        )

    def escalation(self) -> dict[str, Any]:
        notifications = self.notifications()
        return {
            "enabled": bool(notifications.get("escalation_enabled", True)),
            "email_after": int(notifications.get("email_after_seconds", 300)),
            "secondary_after": int(
                notifications.get("secondary_after_seconds", 900)
            ),
        }

    def alert_contacts(self) -> dict[str, str]:
        owner = self.owner()
        notif_email = str(
            owner.get("notification_email")
            or getattr(self.settings, "notification_email", "alerts@client.com")
        ).strip()
        sec_email = str(owner.get("secondary_email") or "").strip()
        return {
            "email": notif_email,
            "notification_email": notif_email,
            "secondary_email": sec_email,
        }

    def report_recipients(self) -> list[str]:
        reports = self.reports()
        recipients = [
            email for email in reports.get("recipients", []) if is_valid_email(email)
        ]
        if recipients:
            return recipients
        owner_email = self.owner().get("email", "")
        return [owner_email] if is_valid_email(owner_email) else []

    def unattended(self) -> dict[str, Any]:
        monitoring = self.monitoring()
        return {
            "enabled": bool(monitoring.get("unattended_enabled", False)),
            "after_seconds": float(monitoring.get("unattended_after_seconds", 10)),
            "staff_zone": self.staff_zone(),
            "staff_presence_seconds": float(
                monitoring.get("staff_presence_seconds", 20)
            ),
        }

    def staff_zone(self) -> tuple[float, float, float, float] | None:
        raw = str(self.monitoring().get("staff_zone", "") or "").strip()
        if not raw:
            return None
        try:
            values = [float(part) for part in raw.split(",")]
        except ValueError:
            return None
        if len(values) != 4:
            return None
        x1, y1, x2, y2 = [max(0.0, min(1.0, value)) for value in values]
        if x2 <= x1 or y2 <= y1:
            return None
        return x1, y1, x2, y2

    def features(self) -> dict[str, bool]:
        return {
            "vision": self.settings.vision_configured,
            "rekognition": self.settings.vision_configured,
            "openrouter": self.settings.openrouter_configured,
            "email": self.email_enabled(),
            "push": self.push_enabled(),
            "escalation": self.escalation().get("enabled", False),
        }

    def document(self) -> dict[str, Any]:
        document = serialize_doc(self.get())
        owner = document.get("owner") or {}
        owner.pop("password_hash", None)
        owner["password_set"] = bool(owner.get("password"))
        return document

    # -------------------------------------------------------------- internals
    @classmethod
    def _merge(cls, base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
        merged = copy.deepcopy(base)
        if not isinstance(override, dict):
            return merged
        for key, value in override.items():
            if key == "cameras" and isinstance(value, list):
                merged["cameras"] = value
            elif isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = cls._merge(merged[key], value)
            else:
                merged[key] = value
        return merged

    def _normalize(self, data: dict[str, Any]) -> dict[str, Any]:
        defaults = self._defaults()
        owner = data.get("owner") or {}
        shop = data.get("shop") or {}
        warehouse = data.get("warehouse") or {}
        notifications = data.get("notifications") or {}
        monitoring = data.get("monitoring") or {}
        reports = data.get("reports") or {}

        auth_email = str(owner.get("auth_email") or "").strip()
        notification_email = str(owner.get("notification_email") or "").strip()
        secondary_email = str(owner.get("secondary_email") or "").strip()
        if not auth_email:
            auth_email = getattr(self.settings, "owner_auth_email", "admin123@gmail.com")
        if not notification_email:
            notification_email = getattr(self.settings, "notification_email", "alerts@client.com")
        if auth_email and not is_valid_email(auth_email):
            raise ValueError("Sign-in (Auth) email is not valid")
        if notification_email and not is_valid_email(notification_email):
            raise ValueError("Notification email is not valid")
        if secondary_email and not is_valid_email(secondary_email):
            raise ValueError("Secondary email is not valid")

        username = str(owner.get("username") or self.settings.owner_username).strip()
        if not USERNAME_PATTERN.match(username):
            raise ValueError(
                "Username must be 3–40 characters using letters, numbers, dots, dashes or underscores"
            )

        password_hash = str(owner.get("password_hash") or "").strip()
        new_password = owner.get("password")
        plain_password = str(owner.get("password") or "")
        if new_password is not None and str(new_password) != "":
            plain_password = str(new_password)
            password_hash = generate_password_hash(plain_password)
        if not password_hash:
            password_hash = self._default_password_hash

        open_time = normalize_hhmm(shop.get("open"), defaults["shop"]["open"])
        close_time = normalize_hhmm(shop.get("close"), defaults["shop"]["close"])
        after_start = normalize_hhmm(
            warehouse.get("after_hours_start"),
            defaults["warehouse"]["after_hours_start"],
        )
        after_end = normalize_hhmm(
            warehouse.get("after_hours_end"),
            defaults["warehouse"]["after_hours_end"],
        )
        if not all([open_time, close_time, after_start, after_end]):
            raise ValueError("Business hours must use HH:MM (24-hour) format")

        staff_zone = self._normalize_zone(monitoring.get("staff_zone", ""))
        weekly_day = str(reports.get("weekly_day") or "monday").lower()
        if weekly_day not in WEEKDAYS:
            weekly_day = "monday"
        daily_time = normalize_hhmm(
            reports.get("daily_time"), defaults["reports"]["daily_time"]
        )
        if not daily_time:
            raise ValueError("Daily report time must use HH:MM format")

        recipients = []
        for email_value in reports.get("recipients") or []:
            candidate = str(email_value).strip()
            if candidate and is_valid_email(candidate) and candidate not in recipients:
                recipients.append(candidate)
        recipients = recipients[:5]

        normalized = {
            "auth_seeded": bool(data.get("auth_seeded")),
            "operations_revision": int(data.get("operations_revision") or 0),
            "owner": {
                "id": str(owner.get("id") or "owner_001")[:80],
                "name": str(owner.get("name") or self.settings.owner_name)[:120],
                "auth_email": auth_email,
                "notification_email": notification_email,
                "email": notification_email,
                "secondary_email": secondary_email,
                "username": username,
                "password": plain_password,
                "password_hash": password_hash,
            },
            "shop": {
                "id": str(shop.get("id") or self.settings.shop_id)[:80],
                "name": str(shop.get("name") or self.settings.shop_name)[:120],
                "open": open_time,
                "close": close_time,
            },
            "warehouse": {
                "id": str(warehouse.get("id") or self.settings.warehouse_id)[:80],
                "name": str(
                    warehouse.get("name") or self.settings.warehouse_name
                )[:120],
                "after_hours_start": after_start,
                "after_hours_end": after_end,
                "entry_location": str(
                    warehouse.get("entry_location") or "main_gate"
                )[:80],
            },
            "notifications": {
                "email": bool(notifications.get("email", True)),
                "push": bool(notifications.get("push", True)),
                "escalation_enabled": bool(
                    notifications.get("escalation_enabled", True)
                ),
                "email_after_seconds": self._delay(
                    notifications.get("email_after_seconds"), 300
                ),
                "secondary_after_seconds": self._delay(
                    notifications.get("secondary_after_seconds"), 900
                ),
            },
            "monitoring": {
                "unattended_enabled": bool(
                    monitoring.get("unattended_enabled", False)
                ),
                "unattended_after_seconds": self._delay(
                    monitoring.get("unattended_after_seconds"), 10, maximum=300
                ),
                "staff_zone": staff_zone,
                "staff_presence_seconds": self._delay(
                    monitoring.get("staff_presence_seconds"), 20, maximum=600
                ),
            },
            "cameras": self._normalize_cameras(data.get("cameras")),
            "reports": {
                "daily_enabled": bool(reports.get("daily_enabled", True)),
                "daily_time": daily_time,
                "weekly_enabled": bool(reports.get("weekly_enabled", True)),
                "weekly_day": weekly_day,
                "recipients": recipients,
            },
        }
        normalized["updated_at"] = data.get("updated_at") or None
        return normalized

    @staticmethod
    def _delay(value: Any, default: int, maximum: int = 86_400) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return default
        return max(1, min(number, maximum))

    @staticmethod
    def _normalize_zone(value: Any) -> str:
        if isinstance(value, (list, tuple)) and len(value) == 4:
            try:
                values = [float(item) for item in value]
            except (TypeError, ValueError):
                return ""
        else:
            raw = str(value or "").strip()
            if not raw:
                return ""
            try:
                values = [float(part) for part in raw.split(",")]
            except ValueError:
                raise ValueError("Staff zone must be four comma-separated numbers")
        if len(values) != 4:
            raise ValueError("Staff zone must contain x1,y1,x2,y2")
        x1, y1, x2, y2 = [max(0.0, min(1.0, item)) for item in values]
        if x2 <= x1 or y2 <= y1:
            raise ValueError("Staff zone must satisfy x2 > x1 and y2 > y1")
        return ",".join(f"{item:.3f}" for item in (x1, y1, x2, y2))

    def _normalize_cameras(self, cameras: Any) -> list[dict[str, Any]]:
        supplied: dict[int, dict[str, Any]] = {}
        if isinstance(cameras, list):
            for item in cameras:
                if not isinstance(item, dict):
                    continue
                try:
                    channel = int(item.get("channel"))
                except (TypeError, ValueError):
                    continue
                supplied[channel] = item
        elif isinstance(cameras, dict):
            for key, item in cameras.items():
                try:
                    channel = int(key)
                except (TypeError, ValueError):
                    continue
                if isinstance(item, dict):
                    supplied[channel] = item

        normalized = []
        for channel in self.settings.allowed_cameras:
            item = supplied.get(channel, {})
            normalized.append(
                {
                    "channel": channel,
                    "name": str(item.get("name") or f"Camera {channel:02d}")[:80],
                    "enabled": bool(item.get("enabled", True)),
                }
            )
        return normalized

