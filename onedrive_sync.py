"""
onedrive_sync.py - Laedt Videos aus geteilten OneDrive-Ordnern herunter.

Unterstuetzt mehrere Share-URLs (kommagetrennt in ONEDRIVE_SHARE_URL).
Methoden:
  1. Graph API (anonym, fuer Business-OneDrive)
  2. OneDrive Personal API via authkey (fuer private/consumer OneDrive)
  3. HTML-Scraping als letzter Fallback
"""

import os
import re
import json
import base64
import logging
import requests
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

_RAW_URLS     = os.getenv("ONEDRIVE_SHARE_URL", "")
ONEDRIVE_URLS = [u.strip() for u in _RAW_URLS.split(",") if u.strip()]

DATA_DIR      = Path(os.getenv("DATA_DIR", "/tmp"))
PROCESSED     = DATA_DIR / "onedrive_processed.json"
DOWNLOAD_DIR  = DATA_DIR / "downloads"

VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _encode_share_url(url: str) -> str:
    encoded = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
    return f"u!{encoded}"


def _resolve_url(url: str) -> str:
    """Loest 1drv.ms Kurzlink via GET auf (HEAD folgt Redirects nicht immer korrekt)."""
    try:
        r = requests.get(
            url, headers=_HEADERS,
            allow_redirects=True, timeout=15,
            stream=True  # kein Body herunterladen
        )
        r.close()
        resolved = r.url
        if resolved != url:
            logger.debug(f"Redirect: {url[:50]} -> {resolved[:80]}")
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


# ── Methode 1: Microsoft Graph API ───────────────────────────────────────────

def _list_via_graph(url: str) -> list | None:
    """Versucht Dateiliste via Graph API (funktioniert bei Business-OneDrive)."""
    share_id = _encode_share_url(url)
    api_url  = f"https://graph.microsoft.com/v1.0/shares/{share_id}/driveItem/children"
    try:
        r = requests.get(api_url, headers=_HEADERS, timeout=30)
        if r.status_code in (401, 403):
            return None
        r.raise_for_status()
        items = r.json().get("value", [])
        logger.info(f"Graph API: {len(items)} Eintraege")
        return items
    except requests.HTTPError:
        return None
    except Exception as e:
        logger.error(f"Graph API Fehler: {e}")
        return None


# ── Methode 2: OneDrive Personal API mit authkey ──────────────────────────────

def _list_via_personal_api(resolved_url: str) -> list | None:
    """
    Nutzt die OneDrive Personal (consumer) API.
    Funktioniert wenn resolved_url onedrive.live.com mit authkey+resid+cid enthaelt.
    """
    parsed  = urlparse(resolved_url)
    params  = parse_qs(parsed.query)

    authkey = params.get("authkey", [None])[0]
    resid   = params.get("resid",   [None])[0]
    cid     = params.get("cid",     [None])[0]

    if not all([authkey, resid, cid]):
        logger.info(f"Personal API: fehlende Parameter — authkey={authkey}, cid={cid}, resid={resid}")
        return None

    # Versuche mehrere Endpunkte
    endpoints = [
        f"https://api.onedrive.com/v1.0/drives/{cid}/items/{resid}/children",
        f"https://onedrive.live.com/children?authkey={authkey}&cid={cid}&id={resid}&resid={resid}",
    ]
    for endpoint in endpoints:
        try:
            api_params = {"authkey": authkey} if "api.onedrive.com" in endpoint else {}
            r = requests.get(endpoint, params=api_params, headers=_HEADERS, timeout=30)
            if r.status_code == 200:
                data  = r.json()
                items = data.get("value", data.get("data", []))
                if isinstance(items, list):
                    logger.info(f"Personal API ({endpoint[:40]}): {len(items)} Eintraege")
                    return items
        except Exception as e:
            logger.debug(f"Personal API Versuch fehlgeschlagen: {e}")
            continue

    return None


# ── Methode 3: Graph API mit aufgeloester URL ─────────────────────────────────

def _list_via_graph_resolved(resolved_url: str) -> list | None:
    """Versucht Graph API mit der aufgeloesten onedrive.live.com URL."""
    if "onedrive.live.com" not in resolved_url and "1drv.ms" in resolved_url:
        return None
    return _list_via_graph(resolved_url)


# ── Methode 4: HTML-Scraping (Fallback) ───────────────────────────────────────

def _list_via_html(url: str) -> list:
    """Sucht direkte Video-Download-Links im HTML (Fallback, selten erfolgreich)."""
    try:
        r = requests.get(url, headers=_HEADERS, allow_redirects=True, timeout=30)
        dl_links = re.findall(
            r'"(https://[^"]*\.(?:mp4|mov|avi|mkv|webm)[^"]*)"',
            r.text, re.IGNORECASE
        )
        items = []
        for link in dl_links:
            name = Path(link.split("?")[0]).name or "video.mp4"
            items.append({"name": name, "id": name,
                          "@microsoft.graph.downloadUrl": link})
        if items:
            logger.info(f"HTML-Scraping: {len(items)} Videos")
        else:
            logger.warning(f"HTML-Scraping: keine Videos (Status {r.status_code})")
        return items
    except Exception as e:
        logger.error(f"HTML-Scraping Fehler: {e}")
        return []


# ── Hauptfunktion: Dateiliste ─────────────────────────────────────────────────

def list_onedrive_files(share_url: str = None) -> list:
    """
    Listet Videos in einem OneDrive-Share.
    Probiert alle Methoden der Reihe nach.
    """
    urls      = [share_url] if share_url else ONEDRIVE_URLS
    all_items = []

    for url in urls:
        resolved = _resolve_url(url)
        logger.info(f"Resolved URL: {resolved[:200]}")

        # 1. Graph API mit Original-URL
        items = _list_via_graph(url)

        # 2. Graph API mit aufgeloester URL
        if items is None and resolved != url:
            items = _list_via_graph_resolved(resolved)

        # 3. OneDrive Personal API (authkey aus URL)
        if items is None:
            items = _list_via_personal_api(resolved)

        # 4. HTML-Scraping
        if items is None:
            logger.info("Alle API-Methoden fehlgeschlagen, versuche HTML-Scraping...")
            items = _list_via_html(resolved)

        videos = [
            i for i in (items or [])
            if Path(i.get("name", "")).suffix.lower() in VIDEO_EXTENSIONS
        ]
        logger.info(f"Share {url[:50]}: {len(videos)} Video(s)")
        all_items.extend(videos)

    return all_items


# ── Download ──────────────────────────────────────────────────────────────────

def _get_download_url(item: dict) -> str | None:
    return (
        item.get("@microsoft.graph.downloadUrl")
        or item.get("@content.downloadUrl")
        or item.get("downloadUrl")
    )


def download_file(item: dict) -> Path | None:
    url  = _get_download_url(item)
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
    """Laedt neue Videos herunter. Gibt Liste der Pfade zurueck."""
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
        logger.info(f"OneDrive Sync: {len(new_paths)} neue Video(s)")
    else:
        logger.info("OneDrive Sync: keine neuen Videos")
    return new_paths
