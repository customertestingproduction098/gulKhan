"""Owner and monitoring settings routes."""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from . import get_owner_settings

bp = Blueprint("settings", __name__, url_prefix="/api/settings")


@bp.get("")
def get_settings():
    owner_settings = get_owner_settings()
    return jsonify(
        {
            "settings": owner_settings.document(),
            "features": owner_settings.features(),
        }
    )


@bp.put("")
@bp.patch("")
def update_settings():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400

    # Accept either the raw settings object or {"settings": {...}}.
    if isinstance(payload.get("settings"), dict):
        payload = payload["settings"]

    try:
        updated = get_owner_settings().update(payload)
    except ValueError as error:
        return jsonify({"error": str(error)}), 400

    return jsonify(
        {
            "settings": get_owner_settings().document(),
            "features": get_owner_settings().features(),
            "updated": updated,
        }
    )
