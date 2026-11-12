"""Environment-driven application settings.

All values are loaded once from ``backend/.env`` (or the process environment).
Nothing here talks to external services, so importing this module is safe.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = BASE_DIR / ".env"

load_dotenv(dotenv_path=ENV_FILE)


def _str(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _list(name: str, default: str = "") -> list[str]:
    raw = os.getenv(name, default) or ""
    return [item.strip() for item in raw.split(",") if item.strip()]


def _upload_dir() -> Path:
    raw = _str("UPLOAD_DIR")
    if not raw:
        return BASE_DIR / "static" / "snapshots"
    path = Path(raw)
    return path if path.is_absolute() else BASE_DIR / path


DEFAULT_ALLOWED_CAMERAS = [2, 4, 5, 7, 8, 9, 11]


def _int_list(name: str, default: str = "") -> list[int]:
    raw = os.getenv(name, default) or ""
    result = []
    for item in raw.split(","):
        item = item.strip()
        if item:
            try:
                result.append(int(item))
            except ValueError:
                pass
    return result if result else list(DEFAULT_ALLOWED_CAMERAS)

DEFAULT_RTSP_TEMPLATE = (
    "rtsp://{user}:{password}@{host}:{port}/cam/realmonitor"
    "?channel={channel}&subtype={subtype}"
)


@dataclass(frozen=True)
class Settings:
    """Runtime configuration for the surveillance API."""

    env: str = field(default_factory=lambda: _str("FLASK_ENV", "development"))
    secret_key: str = field(default_factory=lambda: _str("SECRET_KEY", "change-me"))
    host: str = field(default_factory=lambda: _str("HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _int("PORT", 5000))
    debug: bool = field(default_factory=lambda: _bool("FLASK_DEBUG", False))
    cors_origins: list[str] = field(
        default_factory=lambda: _list(
            "CORS_ORIGINS",
            "http://localhost:5173,http://127.0.0.1:5173,http://localhost:4173",
        )
    )
    public_base_url: str = field(
        default_factory=lambda: _str("PUBLIC_BASE_URL", "http://localhost:5000")
    )

    # MongoDB
    mongo_uri: str = field(
        default_factory=lambda: _str("MONGODB_URI", "mongodb://localhost:27017")
    )
    mongo_db: str = field(default_factory=lambda: _str("MONGODB_DB", "surveillance"))

    # Camera / NVR
    camera_host: str = field(default_factory=lambda: _str("CAMERA_HOST"))
    camera_port: int = field(default_factory=lambda: _int("CAMERA_PORT", 554))
    camera_user: str = field(default_factory=lambda: _str("CAMERA_USER", "admin"))
    camera_password: str = field(default_factory=lambda: _str("CAMERA_PASSWORD"))
    allowed_cameras: list[int] = field(
        default_factory=lambda: _int_list("ALLOWED_CAMERAS", "2,4,5,7,8,9,11")
    )
    camera_channels: int = field(default_factory=lambda: _int("CAMERA_CHANNELS", 16))
    camera_subtype: int = field(default_factory=lambda: _int("CAMERA_SUBTYPE", 0))
    camera_stream_url: str = field(default_factory=lambda: _str("CAMERA_STREAM_URL"))
    camera_rtsp_template: str = field(
        default_factory=lambda: _str("CAMERA_RTSP_TEMPLATE", DEFAULT_RTSP_TEMPLATE)
    )
    camera_fps: int = field(default_factory=lambda: _int("CAMERA_FPS", 12))
    frame_width: int = field(default_factory=lambda: _int("FRAME_WIDTH", 1280))
    analysis_width: int = field(default_factory=lambda: _int("ANALYSIS_WIDTH", 640))
    frame_buffer_seconds: float = field(
        default_factory=lambda: _float("FRAME_BUFFER_SECONDS", 5.0)
    )
    jpeg_quality: int = field(default_factory=lambda: _int("JPEG_QUALITY", 80))
    camera_open_timeout_ms: int = field(
        default_factory=lambda: _int("CAMERA_OPEN_TIMEOUT_MS", 8000)
    )
    camera_read_timeout_ms: int = field(
        default_factory=lambda: _int("CAMERA_READ_TIMEOUT_MS", 8000)
    )

    # Analysis behaviour
    analysis_interval_seconds: float = field(
        default_factory=lambda: _float("ANALYSIS_INTERVAL_SECONDS", 3.0)
    )
    greeting_window_seconds: float = field(
        default_factory=lambda: _float("GREETING_WINDOW_SECONDS", 8.0)
    )
    interaction_cooldown_seconds: float = field(
        default_factory=lambda: _float("INTERACTION_COOLDOWN_SECONDS", 20.0)
    )
    alert_cooldown_seconds: float = field(
        default_factory=lambda: _float("ALERT_COOLDOWN_SECONDS", 120.0)
    )
    shop_open_time: str = field(default_factory=lambda: _str("SHOP_OPEN_TIME", "06:00"))
    shop_close_time: str = field(default_factory=lambda: _str("SHOP_CLOSE_TIME", "21:00"))
    after_hours_start: str = field(
        default_factory=lambda: _str("WAREHOUSE_AFTER_HOURS_START", "21:00")
    )
    after_hours_end: str = field(
        default_factory=lambda: _str("WAREHOUSE_AFTER_HOURS_END", "11:00")
    )
    shop_id: str = field(default_factory=lambda: _str("SHOP_ID", "shop_001"))
    shop_name: str = field(default_factory=lambda: _str("SHOP_NAME", "Main Shop"))
    warehouse_id: str = field(
        default_factory=lambda: _str("WAREHOUSE_ID", "warehouse_001")
    )
    warehouse_name: str = field(
        default_factory=lambda: _str("WAREHOUSE_NAME", "Main Warehouse")
    )
    entry_location: str = field(
        default_factory=lambda: _str("WAREHOUSE_ENTRY_LOCATION", "main_gate")
    )
    unattended_alert_seconds: float = field(
        default_factory=lambda: _float("UNATTENDED_ALERT_SECONDS", 10.0)
    )
    unattended_detection_enabled: bool = field(
        default_factory=lambda: _bool("UNATTENDED_DETECTION_ENABLED", False)
    )
    shopkeeper_zone: str = field(
        default_factory=lambda: _str("SHOPKEEPER_ZONE")
    )
    staff_presence_seconds: float = field(
        default_factory=lambda: _float("STAFF_PRESENCE_SECONDS", 20.0)
    )
    background_jobs_enabled: bool = field(
        default_factory=lambda: _bool("BACKGROUND_JOBS_ENABLED", True)
    )
    upload_dir: Path = field(default_factory=_upload_dir)

    # AWS Rekognition
    aws_access_key_id: str = field(
        default_factory=lambda: _str("AWS_ACCESS_KEY_ID")
    )
    aws_secret_access_key: str = field(
        default_factory=lambda: _str("AWS_SECRET_ACCESS_KEY")
    )
    aws_region: str = field(default_factory=lambda: _str("AWS_REGION", "ap-south-1"))
    rekognition_enabled: bool = field(
        default_factory=lambda: _bool("VISION_ENABLED", True)
        if _str("VISION_ENABLED")
        else _bool("REKOGNITION_ENABLED", True)
    )
    rekognition_min_confidence: float = field(
        default_factory=lambda: _float("VISION_MIN_CONFIDENCE", 70.0)
        if _str("VISION_MIN_CONFIDENCE")
        else _float("REKOGNITION_MIN_CONFIDENCE", 70.0)
    )

    # Google Cloud (kept for Firebase push only if needed)
    google_credentials: str = field(
        default_factory=lambda: _str("GOOGLE_APPLICATION_CREDENTIALS")
    )
    google_project: str = field(default_factory=lambda: _str("GOOGLE_CLOUD_PROJECT"))

    # OpenRouter
    openrouter_api_key: str = field(
        default_factory=lambda: _str("OPENROUTER_API_KEY")
    )
    openrouter_base_url: str = field(
        default_factory=lambda: _str(
            "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
        )
    )
    openrouter_model: str = field(
        default_factory=lambda: _str("OPENROUTER_MODEL", "openai/gpt-4o-mini")
    )
    openrouter_site_url: str = field(
        default_factory=lambda: _str("OPENROUTER_SITE_URL", "http://localhost:5173")
    )

    # Alerts
    smtp_host: str = field(default_factory=lambda: _str("SMTP_HOST"))
    smtp_port: int = field(default_factory=lambda: _int("SMTP_PORT", 587))
    smtp_secure: bool = field(default_factory=lambda: _bool("SMTP_SECURE", False))
    smtp_user: str = field(default_factory=lambda: _str("SMTP_USER"))
    smtp_pass: str = field(default_factory=lambda: _str("SMTP_PASS"))
    mail_from: str = field(default_factory=lambda: _str("MAIL_FROM"))
    owner_name: str = field(default_factory=lambda: _str("OWNER_NAME", "Store Owner"))
    owner_email: str = field(
        default_factory=lambda: _str("OWNER_EMAIL", "admin123@gmail.com")
    )
    owner_auth_email: str = field(
        default_factory=lambda: _str("AUTH_EMAIL", "admin123@gmail.com")
    )
    notification_email: str = field(
        default_factory=lambda: _str("NOTIFICATION_EMAIL", "alerts@client.com")
    )
    secondary_email: str = field(default_factory=lambda: _str("SECONDARY_EMAIL"))
    owner_username: str = field(default_factory=lambda: _str("OWNER_USERNAME", "admin"))
    owner_password: str = field(default_factory=lambda: _str("OWNER_PASSWORD", "admin098"))

    # Cloudinary
    cloudinary_cloud_name: str = field(
        default_factory=lambda: _str("CLOUDINARY_CLOUD_NAME")
    )
    cloudinary_api_key: str = field(
        default_factory=lambda: _str("CLOUDINARY_API_KEY")
    )
    cloudinary_api_secret: str = field(
        default_factory=lambda: _str("CLOUDINARY_API_SECRET")
    )
    cloudinary_folder: str = field(
        default_factory=lambda: _str("CLOUDINARY_FOLDER", "sentinel-ai/snapshots")
    )

    # Escalation timings (seconds after the alert is raised)
    escalation_enabled: bool = field(
        default_factory=lambda: _bool("ESCALATION_ENABLED", False)
    )
    escalation_email_after: int = field(
        default_factory=lambda: _int("ESCALATION_EMAIL_AFTER", 300)
    )
    escalation_secondary_after: int = field(
        default_factory=lambda: _int("ESCALATION_SECONDARY_AFTER", 900)
    )

    # Firebase Cloud Messaging push (optional)
    firebase_project_id: str = field(
        default_factory=lambda: _str("FIREBASE_PROJECT_ID")
    )
    firebase_device_token: str = field(
        default_factory=lambda: _str("FIREBASE_DEVICE_TOKEN")
    )
    push_enabled: bool = field(default_factory=lambda: _bool("PUSH_ENABLED", True))

    # Scheduled reports
    daily_report_enabled: bool = field(
        default_factory=lambda: _bool("DAILY_REPORT_ENABLED", True)
    )
    daily_report_time: str = field(
        default_factory=lambda: _str("DAILY_REPORT_TIME", "21:00")
    )
    weekly_report_enabled: bool = field(
        default_factory=lambda: _bool("WEEKLY_REPORT_ENABLED", True)
    )
    weekly_report_day: str = field(
        default_factory=lambda: _str("WEEKLY_REPORT_DAY", "monday").lower()
    )
    vip_list: str = field(default_factory=lambda: _str("VIP_LIST", ""))

    greeting_phrases: list[str] = field(
        default_factory=lambda: _list(
            "GREETING_PHRASES",
            "hello,hi,hey,good morning,good afternoon,good evening,"
            "welcome,assalam o alaikum,assalamualaikum,salam,how can i help",
        )
    )

    def rtsp_url(self, channel: int = 1) -> str:
        """Build a properly escaped RTSP URL for the requested channel."""

        if self.camera_stream_url:
            return self.camera_stream_url

        if not self.camera_host:
            raise ValueError(
                "Camera is not configured. Set CAMERA_HOST (and credentials) in .env."
            )

        return self.camera_rtsp_template.format(
            user=quote(self.camera_user, safe=""),
            password=quote(self.camera_password, safe=""),
            host=self.camera_host,
            port=self.camera_port,
            channel=channel,
            subtype=self.camera_subtype,
        )

    @property
    def vision_configured(self) -> bool:
        return bool(self.google_credentials) and self.vision_enabled

    @property
    def openrouter_configured(self) -> bool:
        return bool(self.openrouter_api_key)

    @property
    def vision_configured(self) -> bool:
        return bool(
            self.aws_access_key_id
            and self.aws_secret_access_key
            and self.rekognition_enabled
        )

    @property
    def push_configured(self) -> bool:
        return bool(
            self.push_enabled
            and self.firebase_project_id
            and self.firebase_device_token
            and self.google_credentials
        )

    @property
    def cloudinary_configured(self) -> bool:
        return bool(
            self.cloudinary_cloud_name
            and self.cloudinary_api_key
            and self.cloudinary_api_secret
        )

    @property
    def email_configured(self) -> bool:
        return bool(self.smtp_host and self.mail_from)

    def is_camera_allowed(self, channel: int | None) -> bool:
        if channel is None:
            return False
        try:
            return int(channel) in self.allowed_cameras
        except (TypeError, ValueError):
            return False

    def ensure_directories(self) -> None:
        self.upload_dir.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    return settings
