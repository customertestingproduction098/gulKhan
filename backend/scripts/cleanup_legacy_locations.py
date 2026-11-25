"""One-time idempotent MongoDB cleanup for legacy placeholder location values.

Run manually from ``backend/``:
    python scripts/cleanup_legacy_locations.py

Also invoked safely once when ``MongoHandler`` initializes indexes.
"""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path
from typing import Any

LEGACY_LOCATION_REGEX = re.compile(r"^main_gate$", re.IGNORECASE)
logger = logging.getLogger(__name__)


def sanitize_database(db: Any) -> dict[str, int]:
    """Scrub legacy placeholder locations from settings, entries, and alerts."""
    counts = {"settings": 0, "entries": 0, "alerts": 0}

    settings_doc = db["settings"].find_one({"_id": "owner"})
    if isinstance(settings_doc, dict):
        updates: dict[str, Any] = {}
        warehouse = settings_doc.get("warehouse")
        if isinstance(warehouse, dict) and LEGACY_LOCATION_REGEX.match(
            str(warehouse.get("entry_location") or "").strip()
        ):
            updates["warehouse.entry_location"] = ""

        cameras = settings_doc.get("cameras")
        if isinstance(cameras, list):
            changed = False
            cleaned_cameras = []
            for cam in cameras:
                if not isinstance(cam, dict):
                    cleaned_cameras.append(cam)
                    continue
                item = dict(cam)
                if LEGACY_LOCATION_REGEX.match(str(item.get("location") or "").strip()):
                    item["location"] = ""
                    changed = True
                if LEGACY_LOCATION_REGEX.match(str(item.get("name") or "").strip()):
                    ch = item.get("channel")
                    item["name"] = f"Camera {int(ch):02d}" if ch is not None else "Camera"
                    changed = True
                cleaned_cameras.append(item)
            if changed:
                updates["cameras"] = cleaned_cameras

        if updates:
            res = db["settings"].update_one({"_id": "owner"}, {"$set": updates})
            counts["settings"] = int(res.modified_count)

    entry_res = db["warehouse_entries"].update_many(
        {"entry_location": {"$regex": LEGACY_LOCATION_REGEX}},
        {"$set": {"entry_location": ""}},
    )
    counts["entries"] = int(entry_res.modified_count)

    alert_res = db["alerts"].update_many(
        {"metadata.entry_location": {"$regex": LEGACY_LOCATION_REGEX}},
        {"$unset": {"metadata.entry_location": ""}},
    )
    counts["alerts"] = int(alert_res.modified_count)

    if any(counts.values()):
        logger.info("Cleaned legacy location placeholders from MongoDB: %s", counts)
    return counts


def main() -> int:
    backend_dir = Path(__file__).resolve().parent.parent
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))

    from config.settings import get_settings
    from services.mongodb_handler import MongoHandler

    store = MongoHandler(get_settings())
    try:
        counts = sanitize_database(store.db)
        print(f"Cleanup complete: {counts}")
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
