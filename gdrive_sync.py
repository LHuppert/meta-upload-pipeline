"""
gdrive_sync.py - Laedt Videos aus oeffentlichen Google Drive Ordnern herunter.

Benoetigt:
  GOOGLE_API_KEY        - Google Cloud API Key (Drive API aktiviert)
  GOOGLE_DRIVE_FOLDER_IDS - kommagetrennte Folder-IDs aus den Share-Links
                            z.B. aus https://drive.google.com/drive/folders/{ID}
"""

import os
import json
import logging
import requests
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

GOOGLE_API_KEY  = os.getenv("GOOGLE_API_KEY", "")
_RAW_IDS        = os.getenv("GOOGLE_DRIVE_FOLDER_IDS", "")
FOLDER_IDS      = [i.strip() for i in _RAW_IDS.split(",") if i.strip()]

DATA_DIR        = Path(os.getenv("DATA_DIR", "/tmp"))
PROCESSED       = DATA_DIR / "gdrive_processed.json"
DOWNLOAD_DIR    = DATA_DIR / "downloads"

VIDEO_MIMETYPES = {
    "video/mp4", "video/quicktime", "video/x-msvideo",
    "video/x-matroska", "video/webm", "video/mpeg",
}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".mpeg"}

_HEADERS = {"User-Agent": "Mozilla/5.0"}


# ── Hilfsfunktionen ───────────────────────────────────────────────────────────

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


def extract_folder_id(url_or_id: str) -> str:
    """Extrahiert Folder-ID aus Google Drive URL oder gibt ID direkt zurueck."""
    if "drive.google.com" in url_or_id:
        # https://drive.google.com/drive/folders/{ID}?usp=sharing
        parts = url_or_id.split("/folders/")
        if len(parts) > 1:
            return parts[1].split("?")[0].split("&")[0].strip()
    return url_or_id.strip()


# ── Dateiliste ────────────────────────────────────────────────────────────────

def list_drive_files(folder_id: str) -> list:
    """Listet alle Videos in einem oeffentlichen Google Drive Ordner."""
    if not GOOGLE_API_KEY:
        logger.error("GOOGLE_API_KEY nicht gesetzt")
        return []

    fid    = extract_folder_id(folder_id)
    params = {
        "q":       f"'{fid}' in parents and trashed=false",
        "key":     GOOGLE_API_KEY,
        "fields":  "files(id,name,mimeType,size)",
        "pageSize": 100,
    }
    try:
        r = requests.get(
            "https://www.googleapis.com/drive/v3/files",
            params=params, headers=_HEADERS, timeout=30
        )
        if r.status_code == 403:
            logger.error(f"Google Drive 403 — API Key ungueltig oder Drive API nicht aktiviert")
            return []
        if r.status_code == 404:
            logger.error(f"Google Drive 404 — Ordner nicht gefunden: {fid}")
            return []
        r.raise_for_status()
        files = r.json().get("files", [])
        videos = [
            f for f in files
            if f.get("mimeType") in VIDEO_MIMETYPES
            or Path(f.get("name", "")).suffix.lower() in VIDEO_EXTENSIONS
        ]
        logger.info(f"Google Drive Ordner {fid}: {len(videos)} Video(s) von {len(files)} Dateien")
        return videos
    except Exception as e:
        logger.error(f"Google Drive Fehler beim Auflisten ({fid}): {e}")
        return []


# ── Download ──────────────────────────────────────────────────────────────────

def download_drive_file(file_info: dict) -> Path | None:
    """Laedt eine einzelne Datei aus Google Drive herunter."""
    file_id  = file_info.get("id")
    filename = file_info.get("name", f"{file_id}.mp4")

    if not file_id:
        return None

    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    dest = DOWNLOAD_DIR / filename

    # Google Drive Download-URL fuer grosse Dateien
    download_url = (
        f"https://drive.google.com/uc"
        f"?export=download&id={file_id}&confirm=t&key={GOOGLE_API_KEY}"
    )

    try:
        logger.info(f"Lade herunter: {filename}")
        session = requests.Session()

        # Erster Request — Google fragt bei grossen Dateien nach Bestaetigung
        r = session.get(download_url, headers=_HEADERS, stream=True, timeout=60)

        # Falls Redirect zu Viren-Warnung, direkt bestaetigen
        if "drive.google.com/uc" in r.url and "confirm" not in r.url:
            r = session.get(r.url + "&confirm=t", headers=_HEADERS, stream=True, timeout=60)

        r.raise_for_status()

        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=65536):
                if chunk:
                    f.write(chunk)

        size_mb = dest.stat().st_size / 1024 / 1024
        if size_mb < 0.01:
            logger.warning(f"Datei zu klein ({size_mb:.2f} MB) — vermutlich kein echter Download")
            dest.unlink()
            return None

        logger.info(f"Heruntergeladen: {filename} ({size_mb:.1f} MB)")
        return dest

    except Exception as e:
        logger.error(f"Download-Fehler {filename}: {e}")
        if dest.exists():
            dest.unlink()
        return None


# ── Sync ──────────────────────────────────────────────────────────────────────

MAX_DOWNLOADS_PER_SYNC = 5  # Max Videos pro Sync-Lauf (verhindert /tmp Overflow)

def sync_gdrive() -> list:
    """Laedt neue Videos aus allen konfigurierten Google Drive Ordnern."""
    if not GOOGLE_API_KEY:
        logger.warning("GOOGLE_API_KEY nicht gesetzt — Google Drive Sync deaktiviert")
        return []
    if not FOLDER_IDS:
        logger.warning("GOOGLE_DRIVE_FOLDER_IDS nicht gesetzt")
        return []

    processed = _load_processed()
    new_paths = []

    for folder_id in FOLDER_IDS:
        if len(new_paths) >= MAX_DOWNLOADS_PER_SYNC:
            break
        files = list_drive_files(folder_id)
        for f in files:
            if len(new_paths) >= MAX_DOWNLOADS_PER_SYNC:
                logger.info(f"Max {MAX_DOWNLOADS_PER_SYNC} Downloads erreicht — Rest beim nächsten Sync")
                break
            fid = f.get("id")
            if fid in processed:
                continue
            path = download_drive_file(f)
            if path:
                new_paths.append(path)
                processed.add(fid)

    if new_paths:
        _save_processed(processed)
        logger.info(f"Google Drive Sync: {len(new_paths)} neue Video(s) heruntergeladen")
    else:
        logger.info("Google Drive Sync: keine neuen Videos")

    return new_paths
