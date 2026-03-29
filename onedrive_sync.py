"""
onedrive_sync.py — Liest Videos aus einem geteilten OneDrive-Ordner
und stellt sie für den Meta-Upload bereit.

Workflow:
1. Schaut alle X Minuten in den OneDrive-Ordner
2. Lädt neue Videos herunter (die noch nicht verarbeitet wurden)
3. Legt sie in output/ ab (von dort: Bot-Freigabe → Meta-Upload)
4. Trackt verarbeitete Dateien in logs/onedrive_processed.json

Ordnerstruktur OneDrive:
  videoupload/          ← Videos hierher legen
  videoupload/hochgeladen/  ← nach Upload (manuell verschieben bis OAuth steht)
"""

import os
import json
import base64
import logging
import requests
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

ONEDRIVE_SHARE_URL = os.getenv("ONEDRIVE_SHARE_URL", "")
OUTPUT_DIR         = Path(os.getenv("DATA_DIR", ".")) / "output"
PROCESSED_FILE     = Path(os.getenv("DATA_DIR", ".")) / "logs" / "onedrive_processed.json"

GRAPH_BASE = "https://graph.microsoft.com/v1.0"


# ── Hilfsfunktionen ───────────────────────────────────────────────────────────

def encode_share_url(share_url: str) -> str:
    """OneDrive Share-URL → Graph-API Share-ID kodieren."""
    b64 = base64.b64encode(share_url.encode()).decode()
    b64_url = b64.rstrip("=").replace("+", "-").replace("/", "_")
    return f"u!{b64_url}"


def load_processed() -> set:
    if PROCESSED_FILE.exists():
        try:
            data = json.loads(PROCESSED_FILE.read_text(encoding="utf-8"))
            return set(data.get("processed", []))
        except Exception:
            return set()
    return set()


def save_processed(processed: set):
    PROCESSED_FILE.parent.mkdir(parents=True, exist_ok=True)
    PROCESSED_FILE.write_text(
        json.dumps({"processed": list(processed), "updated": datetime.now().isoformat()},
                   indent=2, ensure_ascii=False),
        encoding="utf-8"
    )


# ── Graph API ────────────────────────────────────────────────────────────────

def list_files_in_share(share_url: str) -> list:
    """Dateien im geteilten Ordner auflisten (kein Auth nötig für öffentliche Links)."""
    share_id = encode_share_url(share_url)
    url = f"{GRAPH_BASE}/shares/{share_id}/driveItem/children"
    headers = {"Accept": "application/json"}

    try:
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        items = resp.json().get("value", [])
        # Nur MP4-Dateien zurückgeben
        return [
            item for item in items
            if item.get("file") and item.get("name", "").lower().endswith(".mp4")
        ]
    except requests.RequestException as e:
        logger.error(f"OneDrive-Fehler beim Auflisten: {e}")
        return []


def download_file(item: dict, dest_path: Path) -> bool:
    """Einzelne Datei vom OneDrive herunterladen."""
    download_url = item.get("@microsoft.graph.downloadUrl") or item.get("downloadUrl")
    if not download_url:
        logger.warning(f"Kein Download-URL für {item.get('name')}")
        return False

    try:
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        logger.info(f"Lade herunter: {item['name']} ({item.get('size', 0) // 1024 // 1024} MB)")
        with requests.get(download_url, stream=True, timeout=300) as r:
            r.raise_for_status()
            with open(dest_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
        logger.info(f"✅ Heruntergeladen: {dest_path.name}")
        return True
    except Exception as e:
        logger.error(f"Download-Fehler für {item.get('name')}: {e}")
        if dest_path.exists():
            dest_path.unlink()
        return False


# ── Haupt-Sync ────────────────────────────────────────────────────────────────

def sync_onedrive() -> dict:
    """
    Prüft OneDrive-Ordner auf neue Videos und lädt sie nach output/ herunter.
    Gibt Zusammenfassung zurück: {downloaded, skipped, errors}
    """
    if not ONEDRIVE_SHARE_URL:
        logger.warning("ONEDRIVE_SHARE_URL nicht gesetzt — OneDrive-Sync übersprungen")
        return {"downloaded": 0, "skipped": 0, "errors": 0}

    processed = load_processed()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    files  = list_files_in_share(ONEDRIVE_SHARE_URL)
    result = {"downloaded": 0, "skipped": 0, "errors": 0}

    if not files:
        logger.info("OneDrive: Keine MP4-Dateien gefunden.")
        return result

    logger.info(f"OneDrive: {len(files)} MP4(s) gefunden")

    for item in files:
        name    = item.get("name", "")
        item_id = item.get("id", name)  # ID als eindeutiger Schlüssel

        if item_id in processed:
            result["skipped"] += 1
            continue

        dest = OUTPUT_DIR / name
        if dest.exists():
            # Bereits vorhanden aber noch nicht als verarbeitet markiert
            processed.add(item_id)
            result["skipped"] += 1
            continue

        ok = download_file(item, dest)
        if ok:
            processed.add(item_id)
            result["downloaded"] += 1
        else:
            result["errors"] += 1

    save_processed(processed)
    logger.info(
        f"OneDrive-Sync: {result['downloaded']} neu, "
        f"{result['skipped']} übersprungen, {result['errors']} Fehler"
    )
    return result


def mark_as_uploaded(filename: str):
    """
    Nach erfolgreichem Meta-Upload: Datei als verarbeitet markieren.
    (Für späteres Verschieben in OneDrive/hochgeladen wenn OAuth aktiv ist)
    """
    # Bereits beim Download in processed gespeichert.
    # Hier Platzhalter für spätere OAuth-Funktion: Datei in hochgeladen/ verschieben.
    logger.info(f"[OneDrive] {filename} als hochgeladen markiert")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    result = sync_onedrive()
    print(f"Sync abgeschlossen: {result}")
