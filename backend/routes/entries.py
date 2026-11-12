"""Warehouse entry routes."""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from utils.helpers import day_bounds, parse_date

from . import get_events, get_store

bp = Blueprint("entries", __name__, url_prefix="/api/entries")


def _bool_param(name: str):
    raw = request.args.get(name)
    if raw is None or raw == "":
        return None
    return raw.strip().lower() in {"1", "true", "yes"}


def _date_range():
    start = request.args.get("start") or request.args.get("date")
    end = request.args.get("end") or start
    if not start:
        return None, None
    start_date = parse_date(start)
    end_date = parse_date(end)
    if end_date < start_date:
        raise ValueError("end must be on or after start")
    start_utc, _ = day_bounds(start_date)
    _, end_utc = day_bounds(end_date)
    return start_utc, end_utc


@bp.get("")
def list_entries():
    limit = request.args.get("limit", default=50, type=int) or 50
    channel = request.args.get("channel", type=int)
    try:
        start, end = _date_range()
    except ValueError as error:
        return jsonify({"error": str(error)}), 400

    days = None if start or end else request.args.get("days", type=int)
    items, total = get_store().list_entries(
        limit=limit,
        days=days,
        channel=channel,
        after_hours=_bool_param("after_hours"),
        false_alarm=_bool_param("false_alarm"),
        start=start,
        end=end,
    )
    return jsonify({"entries": items, "total": total, "limit": limit})


@bp.get("/stats")
def entry_stats():
    days = request.args.get("days", default=1, type=int) or 1
    return jsonify(get_store().entry_stats(days=max(1, min(days, 365))))


@bp.get("/<entry_id>")
def get_entry(entry_id: str):
    item = get_store().get_entry(entry_id)
    if item is None:
        return jsonify({"error": "Entry not found"}), 404
    return jsonify(item)


@bp.post("/<entry_id>/false-alarm")
def mark_false_alarm(entry_id: str):
    payload = request.get_json(silent=True) or {}
    value = payload.get("false_alarm", True)
    if not get_store().set_entry_false_alarm(entry_id, bool(value)):
        return jsonify({"error": "Entry not found"}), 404

    item = get_store().get_entry(entry_id)
    get_events().publish("entry.updated", {"entry": item})
    return jsonify({"id": entry_id, "false_alarm": bool(value), "entry": item})
