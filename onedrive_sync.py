"""
onedrive_sync.py — Laedt Videos aus einem geteilten OneDrive-Ordner herunter.

Kein Azure-Login noetig — nur ein oeffentlicher "Jeder mit dem Link"-Share-Link.
"""

import os
import json
import base64
import logging
import requests
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

ONEDRIVE_SHARE_URL = os.getenv("ONEDRIVE_SHARE_URL", "")
DATA_DIR    = Path(os.getenv("DATA_DIR", "/tmp"))
PROCESSED   = DATA_DIR / "onedrive_processed.json"
DOWNLOAD_DIR = DATA_DIR / "downloads"

VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}


def _encode_share_url(url: str) -> str:
    encoded = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
    return f"u!{encoded}"


def _load_processed() -> set:
    try:
        if PROCESSED.exists():
            return set(json.loads(PROCESSED.read_text(encoding="utf-8")))
    except Exception:
        pass
    return set()


def _save_processed(ids: set):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED.write_text(json.dumps(list(ids), indent=2), encoding="utf-8")


def list_onedrive_files() -> list:
    if not ONEDRIVE_SHARE_URL:
        logger.warning("ONEDRIVE_SHARE_URL nicht gesetzt")
        return []
    try:
        share_id = _encode_share_url(ONEDRIVE_SHARE_URL)
        url = f"https://graph.microsoft.com/v1.0/shares/{share_id}/driveItem/children"
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        items = resp.json().get("value", [])
        return [
            item for item in items
            if Path(item.get("name", "")).suffix.lower() in VIDEO_EXTENSIONS
        ]
    except Exception as e:
        logger.error(f"OneDrive Fehler beim Auflisten: {e}")
        return []


def download_file(item: dict) -> Path | None:
    url  = item.get("@microsoft.graph.downloadUrl")
    name = item.get("name", "video.mp4")
    if not url:
        return None
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    dest = DOWNLOAD_DIR / name
    try:
        logger.info(f"Lade herunter: {name}")
        resp = requests.get(url, stream=True, timeout=300)
        resp.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                f.write(chunk)
        size_mb = dest.stat().st_size / 1024 / 1024
        logger.info(f"Heruntergeladen: {name} ({size_mb:.1f} MB)")
        return dest
    except Exception as e:
        logger.error(f"Download-Fehler {name}: {e}")
        if dest.exists():
            dest.unlink()
        return None


def sync_onedrive() -> list:
    """Laedt neue Videos herunter. Gibt Liste der heruntergeladenen Pfade zurueck."""
    if not ONEDRIVE_SHARE_URL:
        return []
    processed = _load_processed()
    files = list_onedrive_files()
    new_paths = []
    for item in files:
        file_id = item.get("id", item.get("name"))
        if file_id in processed:
            continue
        path = download_file(item)
        if path:
            new_paths.append(path)
            processed.add(file_id)
    if new_paths:
        _save_processed(processed)
        logger.info(f"OneDrive Sync: {len(new_paths)} neue Video(s) heruntergeladen")
    return new_paths
