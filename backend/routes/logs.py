"""Legacy interaction/entry/report routes kept for compatibility.

The newer API lives at ``/api/interactions``, ``/api/entries`` and
``/api/reports``; these aliases keep older dashboards working.
"""

from __future__ import annotations

from datetime import datetime

from flask import Blueprint, Response, jsonify, request

from . import get_report_scheduler, get_reports, get_store

bp = Blueprint("logs", __name__, url_prefix="/api/logs")


def _days_param(default: int = 7, maximum: int = 365) -> int:
    days = request.args.get("days", default=default, type=int) or default
    return max(1, min(days, maximum))


@bp.get("/interactions")
def interactions():
    limit = request.args.get("limit", default=50, type=int)
    channel = request.args.get("channel", type=int)
    interaction_type = (
        request.args.get("type") or request.args.get("interaction_type") or ""
    ).upper() or None
    items, total = get_store().list_interactions(
        limit=limit or 50,
        days=request.args.get("days", type=int),
        channel=channel,
        interaction_type=interaction_type,
    )
    return jsonify({"interactions": items, "total": total})


@bp.get("/entries")
def entries():
    limit = request.args.get("limit", default=50, type=int)
    channel = request.args.get("channel", type=int)
    items, total = get_store().list_entries(
        limit=limit or 50,
        days=request.args.get("days", type=int),
        channel=channel,
    )
    return jsonify({"entries": items, "total": total})


@bp.get("/reports/summary")
@bp.get("/report")
def summary():
    return jsonify(get_reports().summary(days=_days_param()))


@bp.get("/reports/export")
def export_csv():
    content = get_reports().export_csv(days=_days_param())
    filename = f"sentinel-report-{datetime.now().strftime('%Y%m%d-%H%M')}.csv"
    return Response(
        "\ufeff" + content,
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@bp.post("/reports/send")
def send_report():
    payload = request.get_json(silent=True) or {}
    kind = str(payload.get("kind") or "daily").lower()
    if kind not in {"daily", "weekly"}:
        return jsonify({"error": "kind must be 'daily' or 'weekly'"}), 400
    return jsonify(
        get_report_scheduler().send_now(
            kind, payload.get("date") or payload.get("value")
        )
    )
