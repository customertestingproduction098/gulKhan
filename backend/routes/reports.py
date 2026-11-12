"""Reporting, export and delivery routes."""

from __future__ import annotations

from datetime import datetime
from io import BytesIO

from flask import Blueprint, Response, jsonify, request, send_file

from utils.helpers import parse_date

from . import get_report_scheduler, get_reports, get_store

bp = Blueprint("reports", __name__, url_prefix="/api/reports")


def _range_args():
    days = request.args.get("days", default=7, type=int) or 7
    start = request.args.get("start")
    end = request.args.get("end")
    start_date = parse_date(start) if start else None
    end_date = parse_date(end) if end else None
    if start_date and end_date and end_date < start_date:
        raise ValueError("end must be on or after start")
    return max(1, min(days, 365)), start_date, end_date


@bp.get("/summary")
def summary():
    try:
        days, start, end = _range_args()
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    return jsonify(get_reports().summary(days=days, start=start, end=end))


@bp.get("/daily")
def daily():
    try:
        return jsonify(
            get_reports().daily(
                request.args.get("date"),
                persist=request.args.get("persist", "").lower() in {"1", "true"},
            )
        )
    except ValueError as error:
        return jsonify({"error": str(error)}), 400


@bp.get("/weekly")
def weekly():
    try:
        return jsonify(
            get_reports().weekly(
                request.args.get("week"),
                persist=request.args.get("persist", "").lower() in {"1", "true"},
            )
        )
    except ValueError as error:
        return jsonify({"error": str(error)}), 400


@bp.get("/monthly")
def monthly():
    try:
        return jsonify(
            get_reports().monthly(
                request.args.get("month"),
                persist=request.args.get("persist", "").lower() in {"1", "true"},
            )
        )
    except ValueError as error:
        return jsonify({"error": str(error)}), 400


@bp.get("/history")
def history():
    limit = request.args.get("limit", default=30, type=int) or 30
    reports = get_store().list_daily_reports(limit=max(1, min(limit, 180)))
    return jsonify({"reports": reports, "total": len(reports)})


@bp.get("/export/csv")
@bp.get("/export.csv")
def export_csv():
    try:
        days, start, end = _range_args()
        content = get_reports().export_csv(days=days, start=start, end=end)
    except ValueError as error:
        return jsonify({"error": str(error)}), 400

    filename = f"sentinel-report-{datetime.now().strftime('%Y%m%d-%H%M')}.csv"
    return Response(
        "\ufeff" + content,
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@bp.get("/export/pdf")
@bp.get("/export.pdf")
def export_pdf():
    try:
        days, start, end = _range_args()
        content = get_reports().export_pdf(days=days, start=start, end=end)
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    except RuntimeError as error:
        return jsonify({"error": str(error)}), 503

    filename = f"sentinel-report-{datetime.now().strftime('%Y%m%d-%H%M')}.pdf"
    return send_file(
        BytesIO(content),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=filename,
        max_age=0,
    )


@bp.post("/send")
def send_report():
    payload = request.get_json(silent=True) or {}
    kind = str(payload.get("kind") or "daily").strip().lower()
    if kind not in {"daily", "weekly"}:
        return jsonify({"error": "kind must be 'daily' or 'weekly'"}), 400

    result = get_report_scheduler().send_now(kind, payload.get("value") or payload.get("date"))
    return jsonify(result)
