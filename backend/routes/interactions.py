"""Shop interaction routes."""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from models.interaction import TYPE_MISSED, TYPE_SUCCESSFUL, TYPE_UNATTENDED
from utils.helpers import day_bounds, parse_date

from . import get_store

bp = Blueprint("interactions", __name__, url_prefix="/api/interactions")

VALID_TYPES = {TYPE_SUCCESSFUL, TYPE_MISSED, TYPE_UNATTENDED}


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
def list_interactions():
    limit = request.args.get("limit", default=50, type=int) or 50
    channel = request.args.get("channel", type=int)
    interaction_type = (
        request.args.get("type") or request.args.get("interaction_type") or ""
    ).upper() or None
    if interaction_type and interaction_type not in VALID_TYPES:
        return (
            jsonify({"error": f"type must be one of {sorted(VALID_TYPES)}"}),
            400,
        )

    try:
        start, end = _date_range()
    except ValueError as error:
        return jsonify({"error": str(error)}), 400

    days = None if start or end else request.args.get("days", type=int)
    items, total = get_store().list_interactions(
        limit=limit,
        days=days,
        channel=channel,
        interaction_type=interaction_type,
        start=start,
        end=end,
    )
    return jsonify({"interactions": items, "total": total, "limit": limit})


@bp.get("/stats")
def interaction_stats():
    days = request.args.get("days", default=1, type=int) or 1
    return jsonify(get_store().interaction_stats(days=max(1, min(days, 365))))


@bp.get("/<interaction_id>")
def get_interaction(interaction_id: str):
    item = get_store().get_interaction(interaction_id)
    if item is None:
        return jsonify({"error": "Interaction not found"}), 404
    return jsonify(item)
