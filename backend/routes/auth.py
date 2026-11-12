"""Owner authentication routes."""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from . import get_owner_settings

bp = Blueprint("auth", __name__, url_prefix="/api/auth")


@bp.post("/login")
def login():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400

    identifier = payload.get("email") or payload.get("username") or ""
    password = payload.get("password") or ""

    owner = get_owner_settings().verify_login(identifier, password)
    if owner is None:
        return jsonify({"error": "Invalid email or password"}), 401

    return jsonify({"owner": owner})
