"""Alert listing, acknowledgement and delivery routes."""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from . import get_alert_system, get_store

bp = Blueprint("alerts", __name__, url_prefix="/api/alerts")


def _parse_bool(value):
    if value is None or value == "":
        return None
    return value.strip().lower() in {"1", "true", "yes"}


@bp.get("")
def list_alerts():
    limit = request.args.get("limit", default=50, type=int) or 50
    days = request.args.get("days", type=int)
    acknowledged = _parse_bool(request.args.get("acknowledged"))
    severity = (request.args.get("severity") or "").upper() or None
    alert_type = (request.args.get("type") or request.args.get("alert_type") or "").upper() or None
    channel = request.args.get("channel", type=int)

    alerts, total = get_store().list_alerts(
        limit=limit,
        days=days,
        acknowledged=acknowledged,
        severity=severity,
        alert_type=alert_type,
        channel=channel,
    )
    return jsonify({"alerts": alerts, "total": total, "limit": limit})


@bp.get("/pending")
def pending_alerts():
    limit = request.args.get("limit", default=100, type=int) or 100
    alerts, total = get_store().pending_alerts(limit=limit)
    return jsonify({"alerts": alerts, "total": total})


@bp.get("/<alert_id>")
def get_alert(alert_id: str):
    alert = get_store().get_alert(alert_id)
    if alert is None:
        return jsonify({"error": "Alert not found"}), 404
    return jsonify(alert)


@bp.post("/<alert_id>/acknowledge")
def acknowledge(alert_id: str):
    if not get_alert_system().acknowledge(alert_id):
        return jsonify({"error": "Alert not found"}), 404
    alert = get_store().get_alert(alert_id) or {}
    return jsonify(
        {
            "id": alert_id,
            "acknowledged": True,
            "acknowledged_at": alert.get("acknowledged_at"),
            "alert": alert,
        }
    )


@bp.post("/send")
def send_alert():
    payload = request.get_json(silent=True) or {}
    channel = payload.get("channel")
    try:
        channel = int(channel) if channel is not None else None
    except (TypeError, ValueError):
        return jsonify({"error": "channel must be an integer"}), 400

    return jsonify(get_alert_system().send_notification(channel))
