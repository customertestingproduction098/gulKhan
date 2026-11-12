"""Sentinel AI surveillance API.

Run locally:
    python app.py

Production (Render):
    gunicorn --workers 1 --threads 8 --bind 0.0.0.0:$PORT app:app

Keep one worker: analysis threads, camera connections and the event broker live
in-process.
"""

from __future__ import annotations

import atexit
import json
import logging
import queue

from flask import Flask, jsonify, request
from flask_cors import CORS
from werkzeug.exceptions import HTTPException

from config.settings import get_settings
from routes.alert import bp as alerts_bp
from routes.auth import bp as auth_bp
from routes.camera import bp as camera_bp
from routes.entries import bp as entries_bp
from routes.events import bp as events_bp
from routes.interactions import bp as interactions_bp
from routes.logs import bp as logs_bp
from routes.reports import bp as reports_bp
from routes.settings import bp as settings_bp
from services.alert_system import AlertSystem
from services.camera_processor import CameraManager, SurveillanceProcessor
from services.escalation import EscalationManager
from services.events import EventBroker
from services.mongodb_handler import DataStoreError, MongoHandler
from services.report_scheduler import ReportScheduler
from services.reports import ReportService
from services.settings_service import SettingsService
from utils.helpers import to_iso, utcnow

VERSION = "2.0.0"


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("werkzeug").setLevel(logging.WARNING)


def create_app() -> Flask:
    configure_logging()
    settings = get_settings()

    app = Flask(
        __name__,
        static_folder="static",
        static_url_path="/static",
    )
    app.config["SECRET_KEY"] = settings.secret_key
    app.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024

    CORS(
        app,
        resources={
            r"/api/*": {
                "origins": settings.cors_origins or "*",
                "methods": ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
                "allow_headers": ["Content-Type", "Authorization"],
                "max_age": 86400,
            }
        },
        supports_credentials=False,
    )

    @app.before_request
    def answer_cors_preflight():
        """Return 200 for every /api preflight so browsers never see a CORS failure."""
        if request.method == "OPTIONS" and request.path.startswith("/api/"):
            return app.make_default_options_response()

    store = MongoHandler(settings)
    events = EventBroker()
    camera_manager = CameraManager(settings)
    owner_settings = SettingsService(store, settings, events)
    alert_system = AlertSystem(store, settings, owner_settings, events)
    processor = SurveillanceProcessor(
        store=store,
        manager=camera_manager,
        alerts=alert_system,
        settings=settings,
        owner_settings=owner_settings,
        events=events,
    )
    reports = ReportService(store, alert_system, owner_settings, settings)
    escalation = EscalationManager(
        store, alert_system, owner_settings, events, settings
    )
    report_scheduler = ReportScheduler(reports, owner_settings, events, settings)

    app.extensions["store"] = store
    app.extensions["events"] = events
    app.extensions["camera_manager"] = camera_manager
    app.extensions["alert_system"] = alert_system
    app.extensions["owner_settings"] = owner_settings
    app.extensions["processor"] = processor
    app.extensions["reports"] = reports
    app.extensions["escalation"] = escalation
    app.extensions["report_scheduler"] = report_scheduler
    app.extensions["settings"] = settings

    for blueprint in (
        auth_bp,
        camera_bp,
        interactions_bp,
        entries_bp,
        alerts_bp,
        reports_bp,
        settings_bp,
        events_bp,
        logs_bp,
    ):
        app.register_blueprint(blueprint)

    # Optional WebSocket transport (SSE remains the default live feed).
    try:
        from flask_sock import Sock

        sock = Sock(app)
        app.extensions["flask_sock"] = sock
    except ImportError:  # pragma: no cover - optional dependency
        sock = None

    if sock is not None:

        @sock.route("/api/events/ws")
        def events_websocket(ws):
            token, channel = events.subscribe()
            try:
                ws.send(json.dumps({"type": "connected", "at": to_iso(utcnow())}))
                while True:
                    try:
                        event = channel.get(timeout=15)
                        ws.send(json.dumps(event, default=str))
                    except queue.Empty:
                        ws.send(
                            json.dumps(
                                {
                                    "type": "heartbeat",
                                    "at": to_iso(utcnow()),
                                }
                            )
                        )
            except Exception:  # noqa: BLE001 - client disconnected
                pass
            finally:
                events.unsubscribe(token)

    @app.get("/")
    def index():
        return jsonify(
            {
                "service": "Sentinel AI surveillance API",
                "version": VERSION,
                "docs": "See README.md in the repository root",
                "endpoints": {
                    "health": "/api/health",
                    "camera_status": "/api/camera/status",
                    "stream": "/api/camera/<channel>/stream",
                    "interactions": "/api/interactions",
                    "entries": "/api/entries",
                    "alerts": "/api/alerts",
                    "settings": "/api/settings",
                    "report_summary": "/api/reports/summary?days=7",
                    "report_daily": "/api/reports/daily",
                    "report_weekly": "/api/reports/weekly",
                    "report_pdf": "/api/reports/export/pdf",
                    "events": "/api/events",
                },
            }
        )

    @app.get("/api/health")
    def health():
        mongo = store.health()
        features = owner_settings.features()
        return jsonify(
            {
                "status": "ok",
                "service": "sentinel-ai-api",
                "version": VERSION,
                "time": to_iso(utcnow()),
                "mongo": mongo,
                "camera_configured": bool(settings.camera_host or settings.camera_stream_url),
                "monitoring": processor.monitoring,
                "mode": processor.mode,
                "background_jobs": {
                    "enabled": settings.background_jobs_enabled,
                    "escalation": escalation.running,
                    "report_scheduler": report_scheduler.running,
                },
                "features": features,
                "live_events": {
                    "sse": "/api/events",
                    "websocket": "/api/events/ws" if sock is not None else None,
                    "subscribers": events.subscriber_count,
                },
            }
        )

    @app.errorhandler(DataStoreError)
    def handle_datastore_error(error: DataStoreError):
        return (
            jsonify(
                {
                    "error": "Database unavailable",
                    "message": str(error),
                    "hint": "Start MongoDB or update MONGODB_URI in backend/.env",
                }
            ),
            503,
        )

    @app.errorhandler(HTTPException)
    def handle_http_error(error: HTTPException):
        if request.path.startswith("/api/"):
            return jsonify({"error": error.name, "message": error.description}), error.code
        return error

    @app.errorhandler(Exception)
    def handle_unexpected_error(error: Exception):
        app.logger.exception("Unhandled error: %s", error)
        store.log("error", "Unhandled API error", {"error": str(error), "path": request.path})
        if request.path.startswith("/api/"):
            return jsonify({"error": "Internal server error"}), 500
        return error

    @app.after_request
    def security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if request.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    if settings.background_jobs_enabled:
        escalation.start()
        report_scheduler.start()

    def shutdown() -> None:
        try:
            report_scheduler.stop()
            escalation.stop()
            processor.shutdown()
        finally:
            store.close()

    atexit.register(shutdown)

    return app


app = create_app()
settings = get_settings()


if __name__ == "__main__":
    print("=" * 72)
    print("Sentinel AI surveillance API")
    print("=" * 72)
    print(f"  API      : http://localhost:{settings.port}/api/health")
    print(f"  Database : {settings.mongo_db} @ {settings.mongo_uri}")
    print(f"  Camera   : {'configured' if (settings.camera_host or settings.camera_stream_url) else 'NOT configured'}")
    print("=" * 72)

    app.run(
        host=settings.host,
        port=settings.port,
        debug=settings.debug,
        threaded=True,
        use_reloader=False,
    )
