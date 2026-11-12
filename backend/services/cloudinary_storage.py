"""Cloudinary image storage service for surveillance snapshots.

Provides robust upload of OpenCV frames and images to Cloudinary,
returning public HTTPS URLs and falling back safely if upload fails.
"""

from __future__ import annotations

import io
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from config.settings import Settings, get_settings

logger = logging.getLogger(__name__)

_configured_cloud_name: str | None = None


def _init_cloudinary(settings: Settings) -> bool:
    global _configured_cloud_name
    if not settings.cloudinary_configured:
        return False

    if _configured_cloud_name != settings.cloudinary_cloud_name:
        try:
            import cloudinary

            cloudinary.config(
                cloud_name=settings.cloudinary_cloud_name,
                api_key=settings.cloudinary_api_key,
                api_secret=settings.cloudinary_api_secret,
                secure=True,
            )
            _configured_cloud_name = settings.cloudinary_cloud_name
            logger.info(
                "Cloudinary initialized for cloud: %s",
                settings.cloudinary_cloud_name,
            )
        except Exception as error:  # noqa: BLE001
            logger.error("Failed to initialize Cloudinary: %s", error)
            return False
    return True


class CloudinaryStorage:
    """Uploads snapshots and frames to Cloudinary cloud storage."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._available = _init_cloudinary(self.settings)

    @property
    def available(self) -> bool:
        if not self.settings.cloudinary_configured:
            return False
        if not self._available:
            self._available = _init_cloudinary(self.settings)
        return self._available

    def upload_frame(
        self,
        frame: np.ndarray,
        prefix: str = "snapshot",
        quality: int | None = None,
    ) -> str | None:
        """Encode an OpenCV BGR frame to JPEG and upload to Cloudinary.

        Returns the secure HTTPS URL, or None on failure.
        """
        if frame is None or not self.available:
            return None

        try:
            jpeg_quality = quality or getattr(self.settings, "jpeg_quality", 85)
            success, buffer = cv2.imencode(
                ".jpg",
                frame,
                [cv2.IMWRITE_JPEG_QUALITY, int(jpeg_quality)],
            )
            if not success or buffer is None:
                logger.warning("Failed to encode frame to JPEG for Cloudinary upload")
                return None

            return self.upload_bytes(
                buffer.tobytes(),
                prefix=prefix,
            )
        except Exception as error:  # noqa: BLE001
            logger.error("Cloudinary frame upload error: %s", error)
            return None

    def upload_bytes(
        self,
        image_bytes: bytes,
        prefix: str = "snapshot",
    ) -> str | None:
        """Upload raw image bytes (JPEG/PNG) to Cloudinary."""
        if not image_bytes or not self.available:
            return None

        try:
            import cloudinary.uploader

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            public_id = f"{prefix}_{timestamp}"
            folder = getattr(self.settings, "cloudinary_folder", "sentinel-ai/snapshots")

            response: dict[str, Any] = cloudinary.uploader.upload(
                io.BytesIO(image_bytes),
                folder=folder,
                public_id=public_id,
                resource_type="image",
                overwrite=True,
            )
            secure_url = response.get("secure_url")
            if secure_url:
                logger.info("Uploaded snapshot to Cloudinary: %s", secure_url)
                return secure_url
            logger.warning("Cloudinary response missing secure_url: %s", response)
            return None
        except Exception as error:  # noqa: BLE001
            logger.error("Failed to upload image to Cloudinary: %s", error)
            return None

    def upload_file(
        self,
        file_path: str | Path,
        prefix: str = "snapshot",
    ) -> str | None:
        """Upload an image file from local disk to Cloudinary."""
        path = Path(file_path)
        if not path.is_file() or not self.available:
            return None

        try:
            with open(path, "rb") as f:
                return self.upload_bytes(f.read(), prefix=prefix)
        except Exception as error:  # noqa: BLE001
            logger.error("Failed reading file for Cloudinary upload: %s", error)
            return None


_default_cloudinary: CloudinaryStorage | None = None


def get_cloudinary_storage() -> CloudinaryStorage:
    global _default_cloudinary
    if _default_cloudinary is None:
        _default_cloudinary = CloudinaryStorage()
    return _default_cloudinary
