"""Server-Sent Events feed for live dashboard updates."""

from __future__ import annotations

import json
import queue

from flask import Blueprint, Response, jsonify

from . import get_escalation, get_events, get_report_scheduler

bp = Blueprint("events", __name__, url_prefix="/api/events")

HEARTBEAT_SECONDS = 15.0


@bp.get("")
def stream_events():
    broker = get_events()
    token, channel = broker.subscribe()

    def generate():
        try:
            yield "retry: 3000\n\n"
            while True:
                try:
                    event = channel.get(timeout=HEARTBEAT_SECONDS)
                    payload = json.dumps(event.get("data") or {}, default=str)
                    yield (
                        f"event: {event.get('type', 'message')}\n"
                        f"data: {payload}\n"
                        f"id: {event.get('at', '')}\n\n"
                    )
                except queue.Empty:
                    yield ": keep-alive\n\n"
        except GeneratorExit:
            pass
        finally:
            broker.unsubscribe(token)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@bp.get("/status")
def events_status():
    return jsonify(
        {
            "subscribers": get_events().subscriber_count,
            "published": get_events().published_count,
            "escalation": get_escalation().status(),
            "scheduler": get_report_scheduler().status(),
        }
    )
