"""
onedrive_sync.py - Laedt Videos aus geteilten OneDrive-Ordnern herunter.

Unterstuetzt mehrere Share-URLs (kommagetrennt in ONEDRIVE_SHARE_URL).
Methoden: Graph API (anonym) -> HTML-Scraping als Fallback.
"""

import os
import re
import json
import base64
import logging
import requests
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

# Kommagetrennte Liste von Share-URLs unterstuetzt
_RAW_URLS        = os.getenv("ONEDRIVE_SHARE_URL", "")
ONEDRIVE_URLS    = [u.strip() for u in _RAW_URLS.split(",") if u.strip()]

DATA_DIR         = Path(os.getenv("DATA_DIR", "/tmp"))
PROCESSED        = DATA_DIR / "onedrive_processed.json"
DOWNLOAD_DIR     = DATA_DIR / "downloads"

VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}


# ── Hilfsfunktionen ───────────────────────────────────────────────────────────

def _encode_share_url(url: str) -> str:
    encoded = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
    return f"u!{encoded}"


def _resolve_url(url: str) -> str:
    """Loest 1drv.ms Kurzlink zur echten OneDrive-URL auf."""
    try:
        r = requests.head(url, headers=_HEADERS, allow_redirects=True, timeout=15)
        resolved = r.url
        if resolved != url:
            logger.debug(f"URL aufgeloest: {url[:60]}... -> {resolved[:80]}...")
        return resolved
    except Exception:
        return url


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


# ── Datei-Listing ─────────────────────────────────────────────────────────────

def _list_via_graph(url: str) -> list | None:
    """
    Versucht Dateien ueber Microsoft Graph API abzurufen.
    Gibt None zurueck wenn 401/403 (Auth erforderlich).
    """
    share_id = _encode_share_url(url)
    api_url  = f"https://graph.microsoft.com/v1.0/shares/{share_id}/driveItem/children"
    try:
        r = requests.get(api_url, headers=_HEADERS, timeout=30)
        if r.status_code in (401, 403):
            logger.debug(f"Graph API: {r.status_code} fuer {url[:60]}")
            return None
        r.raise_for_status()
        items = r.json().get("value", [])
        logger.info(f"Graph API: {len(items)} Eintraege gefunden")
        return items
    except requests.HTTPError:
        return None
    except Exception as e:
        logger.error(f"Graph API Fehler: {e}")
        return None


def _extract_download_url(item: dict) -> str | None:
    """Extrahiert Download-URL aus einem Graph-API oder HTML-Item."""
    return (
        item.get("@microsoft.graph.downloadUrl")
        or item.get("@content.downloadUrl")
        or item.get("downloadUrl")
    )


def _list_via_html(url: str) -> list:
    """
    Fallback: Parst die OneDrive-Share-Seite auf eingebettete JSON-Dateidaten.
    Funktioniert fuer 'Jeder mit dem Link' Shares.
    """
    try:
        r = requests.get(url, headers=_HEADERS, allow_redirects=True, timeout=30)
        text = r.text

        items = []

        # Methode A: Suche nach "FileLeafRef" / "ServerRelativeUrl" (SharePoint-style)
        # Methode B: Suche nach dem eingebetteten JSON-Blob mit "items"
        # Methode C: Suche nach directUrl / downloadUrl Feldern

        # Versuche JSON-Blob zu finden (OneDrive Personal bettet Daten als JS-Variable ein)
        patterns = [
            r'"items"\s*:\s*(\[(?:[^[\]]*|\[(?:[^[\]]*|\[[^\[\]]*\])*\])*\])',
            r'\"files\"\s*:\s*(\[.*?\])',
        ]
        for pat in patterns:
            m = re.search(pat, text, re.DOTALL)
            if m:
                try:
                    raw = json.loads(m.group(1))
                    for f in raw:
                        name = f.get("name") or f.get("fileName") or ""
                        dl   = (f.get("@microsoft.graph.downloadUrl")
                                or f.get("downloadUrl")
                                or f.get("url") or "")
                        if name and dl and Path(name).suffix.lower() in VIDEO_EXTENSIONS:
                            items.append({
                                "name": name,
                                "id":   f.get("id", name),
                                "@microsoft.graph.downloadUrl": dl,
                            })
                    if items:
                        logger.info(f"HTML-Scraping: {len(items)} Videos gefunden")
                        return items
                except json.JSONDecodeError:
                    continue

        # Methode D: direkte Download-Links im HTML suchen
        dl_links = re.findall(
            r'"(https://[^"]*\.(?:mp4|mov|avi|mkv)[^"]*)"',
            text, re.IGNORECASE
        )
        for link in dl_links:
            name = Path(link.split("?")[0]).name or "video.mp4"
            items.append({
                "name": name,
                "id":   name,
                "@microsoft.graph.downloadUrl": link,
            })
        if items:
            logger.info(f"HTML-Scraping (direct links): {len(items)} Videos")
        else:
            logger.warning(f"HTML-Scraping: keine Videos auf Seite gefunden. Status: {r.status_code}")
        return items

    except Exception as e:
        logger.error(f"HTML-Scraping Fehler: {e}")
        return []


def list_onedrive_files(share_url: str = None) -> list:
    """
    Listet Videos in einem OneDrive-Share.
    Versucht zuerst Graph API, dann HTML-Scraping.
    """
    urls = [share_url] if share_url else ONEDRIVE_URLS
    all_items = []

    for url in urls:
        # 1. Kurzlink aufloesen
        resolved = _resolve_url(url)

        # 2. Graph API versuchen (mit Original- UND aufgeloester URL)
        items = _list_via_graph(url)
        if items is None and resolved != url:
            items = _list_via_graph(resolved)

        # 3. Fallback: HTML-Scraping
        if items is None:
            logger.info(f"Graph API nicht verfuegbar, versuche HTML-Scraping...")
            items = _list_via_html(resolved)

        videos = [
            i for i in (items or [])
            if Path(i.get("name", "")).suffix.lower() in VIDEO_EXTENSIONS
        ]
        logger.info(f"Share {url[:50]}: {len(videos)} Video(s) gefunden")
        all_items.extend(videos)

    return all_items


# ── Download ──────────────────────────────────────────────────────────────────

def download_file(item: dict) -> Path | None:
    url  = _extract_download_url(item)
    name = item.get("name", "video.mp4")
    if not url:
        logger.warning(f"Kein Download-URL fuer {name}")
        return None
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    dest = DOWNLOAD_DIR / name
    try:
        logger.info(f"Lade herunter: {name}")
        r = requests.get(url, headers=_HEADERS, stream=True, timeout=300)
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=65536):
                f.write(chunk)
        size_mb = dest.stat().st_size / 1024 / 1024
        logger.info(f"Heruntergeladen: {name} ({size_mb:.1f} MB)")
        return dest
    except Exception as e:
        logger.error(f"Download-Fehler {name}: {e}")
        if dest.exists():
            dest.unlink()
        return None


# ── Sync ──────────────────────────────────────────────────────────────────────

def sync_onedrive() -> list:
    """Laedt neue Videos herunter. Gibt Liste der heruntergeladenen Pfade zurueck."""
    if not ONEDRIVE_URLS:
        logger.warning("ONEDRIVE_SHARE_URL nicht gesetzt")
        return []
    processed = _load_processed()
    files     = list_onedrive_files()
    new_paths = []
    for item in files:
        file_id = item.get("id") or item.get("name")
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
