"""Camera preview, monitoring control and transcript ingestion routes."""

from __future__ import annotations

import time
from io import BytesIO

from flask import Blueprint, Response, jsonify, request, send_file

from config.settings import get_settings
from services.camera_processor import MODE_SHOP, VALID_MODES
from utils.helpers import is_after_hours

from . import (
    get_camera_manager,
    get_owner_settings,
    get_processor,
    get_store,
)

bp = Blueprint("camera", __name__, url_prefix="/api/camera")

MJPEG_BOUNDARY = "frame"


def _camera_statuses() -> list[dict]:
    settings = get_settings()
    owner = get_owner_settings()
    processor = get_processor()
    active_chs = set(getattr(processor, "active_channels", []) or [])
    configured = {camera["channel"]: camera for camera in owner.cameras()}
    statuses = {item["channel"]: item for item in get_camera_manager().status()}
    payload = []
    for channel in settings.allowed_cameras:
        camera = configured.get(channel, {})
        status = statuses.get(channel, {})
        payload.append(
            {
                "channel": channel,
                "name": camera.get("name") or f"Camera {channel:02d}",
                "enabled": bool(camera.get("enabled", True)),
                "connected": bool(status.get("connected")),
                "monitoring": channel in active_chs,
                "fps": status.get("fps", 0.0),
                "last_frame_at": status.get("last_frame_at"),
                "buffered_frames": status.get("buffered_frames", 0),
                "error": status.get("error"),
            }
        )
    return payload


@bp.get("/status")
def camera_status():
    settings = get_settings()
    processor = get_processor()
    owner = get_owner_settings()

    payload = processor.status()
    after_hours = owner.after_hours()
    payload.update(
        {
            "total_channels": len(settings.allowed_cameras),
            "allowed_channels": settings.allowed_cameras,
            "cameras": _camera_statuses(),
            "shop_hours": owner.shop_hours(),
            "after_hours": {
                "start": after_hours["start"],
                "end": after_hours["end"],
                "active": is_after_hours(after_hours["start"], after_hours["end"]),
            },
            "features": owner.features(),
        }
    )
    return jsonify(payload)


@bp.get("/<int:channel>/status")
def channel_status(channel: int):
    settings = get_settings()
    if not settings.is_camera_allowed(channel):
        return jsonify({"error": f"Invalid camera channel {channel}. Allowed: {settings.allowed_cameras}"}), 404

    owner = get_owner_settings()
    if not owner.camera_enabled(channel):
        return jsonify({"error": f"Camera {channel} is disabled", "channel": channel}), 404

    grabber = get_camera_manager().peek(channel)
    processor = get_processor()
    return jsonify(
        {
            "channel": channel,
            "name": next(
                (c["name"] for c in owner.cameras() if c["channel"] == channel),
                f"Camera {channel}",
            ),
            "camera": grabber.status() if grabber else None,
            "monitoring": processor.monitoring and (channel in getattr(processor, "active_channels", []) or processor.channel == channel),
            "mode": processor.mode if (channel in getattr(processor, "active_channels", []) or processor.channel == channel) else None,
        }
    )


@bp.get("/stream/<int:channel>")
@bp.get("/<int:channel>/stream")
def stream(channel: int):
    settings = get_settings()
    if not settings.is_camera_allowed(channel):
        return jsonify({"error": f"Camera {channel} is not authorized"}), 404
    if not get_owner_settings().camera_enabled(channel):
        return jsonify({"error": f"Camera {channel} is disabled", "channel": channel}), 404

    manager = get_camera_manager()
    grabber = manager.get(channel)

    if not grabber.wait_for_frame(timeout=8.0):
        grabber_status = grabber.status()
        return (
            jsonify(
                {
                    "error": "Camera stream unavailable",
                    "detail": grabber_status.get("error"),
                    "channel": channel,
                }
            ),
            503,
        )

    fps = max(1, min(settings.camera_fps, 25))
    frame_delay = 1.0 / fps

    def generate():
        last_revision = -1
        try:
            while True:
                grabber.touch()
                revision = grabber.revision
                jpeg = grabber.read_jpeg()
                if jpeg is None:
                    time.sleep(0.25)
                    continue
                if revision == last_revision:
                    time.sleep(frame_delay)
                    continue
                last_revision = revision
                yield (
                    b"--" + MJPEG_BOUNDARY.encode() + b"\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    b"Cache-Control: no-cache\r\n\r\n" + jpeg + b"\r\n"
                )
                time.sleep(frame_delay)
        except GeneratorExit:
            pass
        finally:
            manager.release_idle(max_idle_seconds=120)

    return Response(
        generate(),
        mimetype=f"multipart/x-mixed-replace; boundary={MJPEG_BOUNDARY}",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Connection": "close",
            "X-Accel-Buffering": "no",
        },
    )


@bp.get("/snapshot/<int:channel>")
@bp.get("/<int:channel>/snapshot")
def snapshot(channel: int):
    settings = get_settings()
    if not settings.is_camera_allowed(channel):
        return jsonify({"error": f"Camera {channel} is not authorized"}), 404
    if not get_owner_settings().camera_enabled(channel):
        return jsonify({"error": f"Camera {channel} is disabled", "channel": channel}), 404

    grabber = get_camera_manager().get(channel)
    if not grabber.wait_for_frame(timeout=8.0):
        return jsonify({"error": "Camera stream unavailable", "channel": channel}), 503

    jpeg = grabber.read_jpeg()
    if not jpeg:
        return jsonify({"error": "No frame available"}), 503

    return send_file(BytesIO(jpeg), mimetype="image/jpeg", max_age=0)


@bp.post("/monitor/start")
def start_monitor():
    payload = request.get_json(silent=True) or {}
    settings = get_settings()
    default_channel = settings.allowed_cameras[0] if settings.allowed_cameras else 2
    channel = payload.get("channel", default_channel)
    mode = payload.get("mode", MODE_SHOP)

    try:
        channel = int(channel)
    except (TypeError, ValueError):
        return jsonify({"error": "channel must be an integer"}), 400

    if mode not in VALID_MODES:
        return jsonify({"error": f"mode must be one of {sorted(VALID_MODES)}"}), 400
    if not settings.is_camera_allowed(channel):
        return jsonify({"error": f"channel must be one of {settings.allowed_cameras}"}), 400

    try:
        status = get_processor().start(channel, mode)
    except ValueError as error:
        return jsonify({"error": str(error)}), 400

    return jsonify(status)


@bp.post("/monitor/stop")
def stop_monitor():
    return jsonify(get_processor().stop())


@bp.post("/<int:channel>/process")
def process_channel(channel: int):
    settings = get_settings()
    if not settings.is_camera_allowed(channel):
        return jsonify({"error": f"Camera {channel} is not authorized. Allowed: {settings.allowed_cameras}"}), 400
    payload = request.get_json(silent=True) or {}
    mode = payload.get("mode", request.args.get("mode", MODE_SHOP))
    if mode not in VALID_MODES:
        return jsonify({"error": f"mode must be one of {sorted(VALID_MODES)}"}), 400

    try:
        return jsonify(get_processor().start(channel, mode))
    except ValueError as error:
        return jsonify({"error": str(error)}), 400


@bp.post("/<int:channel>/inspect")
@bp.post("/inspect/<int:channel>")
def inspect_channel(channel: int):
    settings = get_settings()
    if not settings.is_camera_allowed(channel):
        return jsonify({"error": f"Camera {channel} is not authorized. Allowed: {settings.allowed_cameras}"}), 400
    try:
        return jsonify(get_processor().inspect_frame(channel))
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    except RuntimeError as error:
        return jsonify({"error": str(error)}), 503


@bp.post("/audio")
def ingest_audio():
    """Accept a transcript and feed it to the greeting analyser.

    JSON body: ``{"transcript": "...", "confidence": 0.0}``. Audio capture and
    speech-to-text live outside this backend; this endpoint is the integration
    point for any external transcriber.
    """

    payload = request.get_json(silent=True) or {}
    transcript = (payload.get("transcript") or "").strip()
    if not transcript:
        return jsonify({"error": "Provide a non-empty 'transcript'."}), 400

    try:
        speech_confidence = float(payload.get("confidence") or 0.0)
    except (TypeError, ValueError):
        speech_confidence = 0.0

    processor = get_processor()
    outcome = processor.record_speech(transcript, speech_confidence)

    store = get_store()
    store.log(
        "info",
        "Speech transcript received",
        {
            "channel": processor.channel,
            "transcript": transcript[:300],
            "greeting_detected": outcome.get("greeting_detected"),
        },
    )

    return jsonify(
        {
            "transcript": transcript,
            "confidence": speech_confidence,
            "error": None,
            **outcome,
        }
    )
