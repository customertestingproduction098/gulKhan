"""Small, dependency-light helpers used across the API."""

from __future__ import annotations

import os
import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bson import ObjectId

UTC = timezone.utc
DEFAULT_TIMEZONE = "Asia/Karachi"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def utcnow() -> datetime:
    """Timezone-aware UTC timestamp used for all persisted records."""

    return datetime.now(tz=UTC)


def app_tz():
    """Configured operating timezone (Pakistan by default)."""

    name = (os.getenv("APP_TIMEZONE") or DEFAULT_TIMEZONE).strip()
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        # Windows installs may not ship IANA data; Pakistan has a fixed +05:00 offset.
        if name in {"Asia/Karachi", "Asia/Kolkata", "Asia/Calcutta"}:
            return timezone(timedelta(hours=5))
        return UTC


def current_time() -> datetime:
    """Wall-clock time in the operating timezone, used for business-hours decisions."""

    return datetime.now(tz=app_tz())


def local_tz():
    """Timezone used for date grouping and business-hours decisions."""

    return app_tz()


def local_now() -> datetime:
    return datetime.now(tz=local_tz())



def local_date(value: datetime | None = None) -> date:
    moment = value or utcnow()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(local_tz()).date()


def to_local(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(local_tz())


def to_iso(value: Any) -> str | None:
    """Render a datetime (or ISO-ish string) as an ISO-8601 UTC string."""

    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, date) and not isinstance(value, datetime):
        return datetime.combine(value, time.min, tzinfo=UTC).isoformat()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).isoformat()
    return str(value)


def parse_hhmm(value: str, default: time) -> time:
    """Parse ``HH:MM`` strings without raising on bad input."""

    try:
        hours, minutes = value.strip().split(":")
        return time(hour=int(hours), minute=int(minutes))
    except (AttributeError, ValueError):
        return default


def normalize_hhmm(value: Any, default: str = "00:00") -> str | None:
    """Return a strict ``HH:MM`` string, or ``None`` for invalid input."""

    if not isinstance(value, str):
        return None
    try:
        hours_text, minutes_text = value.strip().split(":")
        hours, minutes = int(hours_text), int(minutes_text)
    except (AttributeError, ValueError):
        return None
    if not (0 <= hours <= 23 and 0 <= minutes <= 59):
        return None
    return f"{hours:02d}:{minutes:02d}"


def is_between(now: time, start: time, end: time) -> bool:
    """Return True when ``now`` falls inside a window that may cross midnight."""

    if start <= end:
        return start <= now < end
    return now >= start or now < end


def is_after_hours(start: str, end: str, now: datetime | None = None) -> bool:
    """Check whether the current local time is outside working hours."""

    moment = (now or current_time()).time()
    start_time = parse_hhmm(start, time(21, 0))
    end_time = parse_hhmm(end, time(11, 0))
    return is_between(moment, start_time, end_time)


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_int(value: Any, default: int = 0, minimum: int | None = None, maximum: int | None = None) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    if minimum is not None:
        number = max(minimum, number)
    if maximum is not None:
        number = min(maximum, number)
    return number


def object_id(value: str | ObjectId | None) -> ObjectId | None:
    """Best-effort ObjectId conversion for route parameters."""

    if isinstance(value, ObjectId):
        return value
    if isinstance(value, str) and ObjectId.is_valid(value):
        return ObjectId(value)
    return None


def serialize_doc(document: Any) -> Any:
    """Recursively convert Mongo values into JSON-safe primitives."""

    if document is None:
        return None
    if isinstance(document, ObjectId):
        return str(document)
    if isinstance(document, datetime):
        return to_iso(document)
    if isinstance(document, dict):
        return {key: serialize_doc(value) for key, value in document.items()}
    if isinstance(document, (list, tuple)):
        return [serialize_doc(item) for item in document]
    return document


def since_days(days: int) -> datetime:
    """UTC timestamp ``days`` in the past (minimum one day)."""

    return utcnow() - timedelta(days=max(1, int(days)))


def date_series(days: int) -> list[str]:
    """Local-date labels for the last ``days`` days, oldest first."""

    today = local_date()
    return [
        (today - timedelta(days=offset)).isoformat()
        for offset in range(max(1, int(days)) - 1, -1, -1)
    ]


def date_series_between(start: date, end: date) -> list[str]:
    labels: list[str] = []
    cursor = start
    while cursor <= end:
        labels.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return labels


def parse_date(value: str | None, default: date | None = None) -> date:
    """Parse ``YYYY-MM-DD`` (or today/yesterday) into a local date."""

    if not value:
        return default or local_date()
    normalized = value.strip().lower()
    if normalized in {"today", "now"}:
        return local_date()
    if normalized == "yesterday":
        return local_date() - timedelta(days=1)
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError:
        return default or local_date()


def day_bounds(target: date) -> tuple[datetime, datetime]:
    """UTC bounds for a local calendar day (end exclusive)."""

    zone = local_tz()
    start_local = datetime.combine(target, time.min, tzinfo=zone)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


def range_bounds(days: int | None = None, start: date | None = None, end: date | None = None) -> tuple[datetime, datetime]:
    """UTC bounds for a local date range. End is inclusive calendar day."""

    end_date = end or local_date()
    start_date = start or (end_date - timedelta(days=max(1, days or 7) - 1))
    if start_date > end_date:
        start_date, end_date = end_date, start_date
    start_utc, _ = day_bounds(start_date)
    _, end_utc = day_bounds(end_date)
    return start_utc, end_utc


def parse_week(value: str | None) -> tuple[date, date]:
    """Parse an ISO week (``2026-W43``) or any date inside that week."""

    today = local_date()
    if not value:
        iso = today.isocalendar()
        start = date.fromisocalendar(iso.year, iso.week, 1)
        return start, start + timedelta(days=6)

    raw = value.strip()
    match = re.match(r"^(\d{4})-?W(\d{1,2})$", raw, re.IGNORECASE)
    if match:
        year, week = int(match.group(1)), int(match.group(2))
        try:
            start = date.fromisocalendar(year, week, 1)
            return start, start + timedelta(days=6)
        except ValueError:
            pass

    try:
        anchor = datetime.strptime(raw, "%Y-%m-%d").date()
        start = anchor - timedelta(days=anchor.weekday())
        return start, start + timedelta(days=6)
    except ValueError:
        iso = today.isocalendar()
        start = date.fromisocalendar(iso.year, iso.week, 1)
        return start, start + timedelta(days=6)


def parse_month(value: str | None) -> tuple[date, date]:
    """Parse ``YYYY-MM`` into the first and last local date of the month."""

    today = local_date()
    year, month = today.year, today.month
    if value:
        match = re.match(r"^(\d{4})-(\d{1,2})$", value.strip())
        if match:
            year, month = int(match.group(1)), int(match.group(2))
    if not 1 <= month <= 12:
        year, month = today.year, today.month
    start = date(year, month, 1)
    if month == 12:
        end = date(year, 12, 31)
    else:
        end = date(year, month + 1, 1) - timedelta(days=1)
    return start, end


def iso_week_label(start: date) -> str:
    iso = start.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def is_valid_email(value: str) -> bool:
    return bool(EMAIL_RE.match((value or "").strip()))
