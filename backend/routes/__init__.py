"""Flask blueprints and shared extension accessors."""

from flask import current_app


def get_store():
    return current_app.extensions["store"]


def get_camera_manager():
    return current_app.extensions["camera_manager"]


def get_processor():
    return current_app.extensions["processor"]


def get_alert_system():
    return current_app.extensions["alert_system"]


def get_owner_settings():
    return current_app.extensions["owner_settings"]


def get_events():
    return current_app.extensions["events"]


def get_reports():
    return current_app.extensions["reports"]


def get_escalation():
    return current_app.extensions["escalation"]


def get_report_scheduler():
    return current_app.extensions["report_scheduler"]


__all__ = [
    "get_alert_system",
    "get_camera_manager",
    "get_escalation",
    "get_events",
    "get_owner_settings",
    "get_processor",
    "get_report_scheduler",
    "get_reports",
    "get_store",
]
