"""
Meta Ads Upload Pipeline
Separates Programm zum Hochladen von Videos zu Meta Ads.
Läuft unabhängig von der Render-Pipeline.

Safety-First: Verhält sich wie ein Mensch — keine Bursts,
zufällige Delays, Tages-Limits, automatischer Backoff.
"""

import os
import json
import time
import random
import logging
import requests
from datetime import datetime, date
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# ─── Logging ──────────────────────────────────────────────────────────────────
log_dir = Path("logs")
log_dir.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(log_dir / "meta_uploader.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


# ─── Config ───────────────────────────────────────────────────────────────────
META_ACCESS_TOKEN = os.getenv("META_ACCESS_TOKEN", "")
AD_ACCOUNT_ID     = os.getenv("META_AD_ACCOUNT_ID", "")   # act_XXXXXXXXX
PAGE_ID           = os.getenv("META_PAGE_ID", "")
META_API_VERSION  = "v21.0"
META_API_BASE     = f"https://graph.facebook.com/{META_API_VERSION}"

# Upload-Ordner — Videos die freigegeben wurden liegen hier
APPROVED_DIR  = Path(os.getenv("APPROVED_DIR", "approved"))
UPLOADED_DIR  = Path(os.getenv("UPLOADED_DIR", "uploaded"))
FAILED_DIR    = Path(os.getenv("FAILED_DIR",   "failed"))

# State-Dateien
UPLOAD_LOG_FILE  = Path("logs/upload_log.json")
API_CALLS_FILE   = Path("logs/api_calls.json")
QUEUE_FILE       = Path("logs/upload_queue.json")


# ─── Safety-Konstanten (aus bestehendem Projekt übernommen) ───────────────────
MAX_UPLOADS_PER_DAY    = int(os.getenv("MAX_UPLOADS_PER_DAY", "5"))
MIN_DELAY_SECONDS      = float(os.getenv("MIN_UPLOAD_DELAY", "120"))   # 2 Min
MAX_DELAY_SECONDS      = float(os.getenv("MAX_UPLOAD_DELAY", "180"))   # 3 Min
MAX_API_CALLS_PER_HOUR = int(os.getenv("MAX_API_CALLS_HOUR", "200"))
SLOWDOWN_THRESHOLD     = int(os.getenv("API_SLOWDOWN_AT", "180"))

# Exponential Backoff: 1 → 2 → 4 → 8 Minuten
BACKOFF_SEQUENCE = [60, 120, 240, 480]
MAX_RETRIES      = 3


# ─── State-Verwaltung ─────────────────────────────────────────────────────────

def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default
    return default


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def get_upload_log() -> dict:
    return load_json(UPLOAD_LOG_FILE, {"uploads": []})


def get_uploads_today() -> int:
    log = get_upload_log()
    today = date.today().isoformat()
    return sum(1 for u in log["uploads"] if u.get("date") == today and u.get("status") == "success")


def record_upload(filename: str, meta_video_id: str, status: str, error: str = ""):
    log = get_upload_log()
    log["uploads"].append({
        "date":          date.today().isoformat(),
        "timestamp":     datetime.now().isoformat(),
        "filename":      filename,
        "meta_video_id": meta_video_id,
        "status":        status,
        "error":         error,
    })
    save_json(UPLOAD_LOG_FILE, log)


# ─── API-Call-Tracker ─────────────────────────────────────────────────────────

def track_api_call():
    data = load_json(API_CALLS_FILE, {"calls": []})
    now = datetime.now().isoformat()
    data["calls"].append(now)
    # Nur letzte 60 Min behalten
    cutoff = time.time() - 3600
    data["calls"] = [c for c in data["calls"] if datetime.fromisoformat(c).timestamp() > cutoff]
    save_json(API_CALLS_FILE, data)
    return len(data["calls"])


def get_api_calls_last_hour() -> int:
    data = load_json(API_CALLS_FILE, {"calls": []})
    cutoff = time.time() - 3600
    return sum(1 for c in data["calls"] if datetime.fromisoformat(c).timestamp() > cutoff)


# ─── Human-like Delays ────────────────────────────────────────────────────────

def human_delay(min_s: float = None, max_s: float = None, label: str = ""):
    """Zufälliger Delay wie ein Mensch der zwischen Aktionen pausiert."""
    min_s = min_s or MIN_DELAY_SECONDS
    max_s = max_s or MAX_DELAY_SECONDS
    wait = random.uniform(min_s, max_s)
    if label:
        logger.info(f"⏳ {label} — warte {wait:.0f}s...")
    else:
        logger.info(f"⏳ Warte {wait:.0f}s (Human-Delay)...")
    time.sleep(wait)


def check_rate_limit():
    """Prüft API-Calls und verlangsamt oder pausiert wenn nötig."""
    calls = get_api_calls_last_hour()
    if calls >= MAX_API_CALLS_PER_HOUR:
        wait = 3600 - (time.time() % 3600) + random.uniform(60, 120)
        logger.warning(f"⚠️ API-Limit erreicht ({calls} Calls) — warte {wait:.0f}s")
        time.sleep(wait)
    elif calls >= SLOWDOWN_THRESHOLD:
        extra = random.uniform(30, 60)
        logger.info(f"🐌 Annäherung an API-Limit ({calls}/{MAX_API_CALLS_PER_HOUR}) — +{extra:.0f}s Extra-Delay")
        time.sleep(extra)


# ─── Meta Graph API Wrapper ───────────────────────────────────────────────────

def meta_request(method: str, endpoint: str, **kwargs) -> dict:
    """
    Einzelner Meta-API-Request mit:
    - Automatischem Token-Anhängen
    - Rate-Limit-Prüfung vorher
    - Exponential Backoff bei Fehlern
    - Call-Tracking
    """
    check_rate_limit()

    url = f"{META_API_BASE}/{endpoint}"
    params = kwargs.pop("params", {})
    params["access_token"] = META_ACCESS_TOKEN

    last_error = None
    for attempt, backoff in enumerate(BACKOFF_SEQUENCE[:MAX_RETRIES], start=1):
        try:
            track_api_call()
            resp = requests.request(method, url, params=params, timeout=300, **kwargs)
            data = resp.json()

            if "error" in data:
                err = data["error"]
                code = err.get("code", 0)
                msg  = err.get("message", "Unbekannter Fehler")

                # Rate-Limit-Fehler von Meta (Code 4, 17, 32, 613)
                if code in (4, 17, 32, 613) or "rate" in msg.lower():
                    logger.warning(f"🔴 Meta Rate-Limit (Code {code}) — Backoff {backoff}s (Versuch {attempt}/{MAX_RETRIES})")
                    time.sleep(backoff + random.uniform(10, 30))
                    continue

                raise RuntimeError(f"Meta API Fehler {code}: {msg}")

            return data

        except requests.exceptions.RequestException as e:
            last_error = e
            logger.warning(f"🌐 Netzwerkfehler — Backoff {backoff}s (Versuch {attempt}): {e}")
            time.sleep(backoff)

    raise RuntimeError(f"Meta API nach {MAX_RETRIES} Versuchen fehlgeschlagen: {last_error}")


# ─── Geo-Ausschluss Helper ───────────────────────────────────────────────────

def get_excluded_geo_locations() -> dict | None:
    """
    Liest Geo-Ausschluss aus settings_manager.
    Gibt einen Meta-API-kompatiblen excluded_geo_locations-Dict zurück
    oder None wenn deaktiviert.

    Wird beim Erstellen von Ad Sets als Targeting-Parameter übergeben:
    adset_params["targeting"]["excluded_geo_locations"] = get_excluded_geo_locations()
    """
    try:
        from settings_manager import settings
        geo = settings.get("geo_exclusion", {})
        if not geo.get("enabled", False):
            return None
        return {
            "custom_locations": [{
                "latitude":        float(geo.get("latitude",  49.7153)),
                "longitude":       float(geo.get("longitude", 8.2175)),
                "radius":          int(geo.get("radius_km",  25)),
                "distance_unit":   "kilometer",
            }],
            "location_types": ["home", "recent"],
        }
    except Exception as e:
        logger.warning(f"Geo-Ausschluss konnte nicht gelesen werden: {e}")
        return None


# ─── Video-Upload ─────────────────────────────────────────────────────────────

def upload_video(video_path: Path) -> str:
    """
    Lädt ein einzelnes Video zu Meta hoch.
    Gibt die Meta Video-ID zurück.

    Meta erwartet einen mehrstufigen Upload für große Dateien:
    1. Upload-Session starten
    2. Chunks hochladen
    3. Session abschließen
    """
    file_size = video_path.stat().st_size
    logger.info(f"📤 Starte Upload: {video_path.name} ({file_size / 1024 / 1024:.1f} MB)")

    # ── Schritt 1: Upload-Session starten ──
    session = meta_request(
        "POST",
        f"{AD_ACCOUNT_ID}/advideos",
        data={
            "upload_phase": "start",
            "file_size":    file_size,
        }
    )
    upload_session_id = session.get("upload_session_id")
    video_id          = session.get("video_id")
    start_offset      = int(session.get("start_offset", 0))
    end_offset        = int(session.get("end_offset", file_size))

    if not upload_session_id:
        raise RuntimeError(f"Keine upload_session_id erhalten: {session}")

    logger.info(f"   Session {upload_session_id} gestartet, Video-ID: {video_id}")

    # ── Schritt 2: Datei in Chunks hochladen ──
    with open(video_path, "rb") as f:
        while start_offset < file_size:
            chunk_size = end_offset - start_offset
            f.seek(start_offset)
            chunk = f.read(chunk_size)

            logger.info(f"   Chunk {start_offset/1024/1024:.1f}–{end_offset/1024/1024:.1f} MB hochladen...")

            chunk_resp = meta_request(
                "POST",
                f"{AD_ACCOUNT_ID}/advideos",
                data={
                    "upload_phase":     "transfer",
                    "upload_session_id": upload_session_id,
                    "start_offset":     start_offset,
                    "end_offset":       end_offset,
                },
                files={"video_file_chunk": (video_path.name, chunk, "application/octet-stream")},
            )

            start_offset = int(chunk_resp.get("start_offset", end_offset))
            end_offset   = int(chunk_resp.get("end_offset",   file_size))

            if start_offset == end_offset:
                break

    # ── Schritt 3: Upload abschließen ──
    finish = meta_request(
        "POST",
        f"{AD_ACCOUNT_ID}/advideos",
        data={
            "upload_phase":     "finish",
            "upload_session_id": upload_session_id,
        }
    )

    if not finish.get("success"):
        raise RuntimeError(f"Upload-Finish fehlgeschlagen: {finish}")

    logger.info(f"   ✅ Video hochgeladen. ID: {video_id}")
    return video_id


def wait_for_video_ready(video_id: str, max_wait: int = 600) -> bool:
    """
    Wartet bis Meta das Video fertig verarbeitet hat.
    Meta encodiert Videos asynchron — wir müssen pollen.
    """
    logger.info(f"   ⏳ Warte auf Video-Verarbeitung ({video_id})...")
    start = time.time()
    poll_interval = 15

    while time.time() - start < max_wait:
        status = meta_request(
            "GET",
            video_id,
            params={"fields": "status"}
        )
        s = status.get("status", {})
        proc = s.get("processing_progress", 0)
        vstat = s.get("video_status", "processing")

        logger.info(f"   Status: {vstat} ({proc}%)")

        if vstat == "ready":
            return True
        if vstat in ("error", "failed"):
            raise RuntimeError(f"Meta Video-Verarbeitung fehlgeschlagen: {s}")

        time.sleep(poll_interval)
        poll_interval = min(poll_interval + 5, 30)  # Langsam erhöhen

    logger.warning(f"   ⚠️ Video {video_id} noch nicht ready nach {max_wait}s — weitermachen trotzdem")
    return False


# ─── Tages-Limit-Check ───────────────────────────────────────────────────────

def check_daily_limit() -> bool:
    uploads = get_uploads_today()
    if uploads >= MAX_UPLOADS_PER_DAY:
        logger.warning(f"🛑 Tages-Limit erreicht: {uploads}/{MAX_UPLOADS_PER_DAY} Uploads heute")
        return False
    logger.info(f"📊 Uploads heute: {uploads}/{MAX_UPLOADS_PER_DAY}")
    return True


# ─── Queue-Management ─────────────────────────────────────────────────────────

def load_queue() -> list:
    return load_json(QUEUE_FILE, [])


def save_queue(queue: list):
    save_json(QUEUE_FILE, queue)


def add_to_queue(video_paths: list):
    """Fügt Videos zur Upload-Queue hinzu (für morgen wenn Limit erreicht)."""
    queue = load_queue()
    existing = {q["path"] for q in queue}
    for p in video_paths:
        if str(p) not in existing:
            queue.append({
                "path":   str(p),
                "added":  datetime.now().isoformat(),
                "status": "pending",
            })
    save_queue(queue)
    logger.info(f"📋 {len(video_paths)} Video(s) in Queue gespeichert")


def get_pending_queue() -> list:
    return [q for q in load_queue() if q["status"] == "pending"]


def mark_queue_item_done(path: str):
    queue = load_queue()
    for item in queue:
        if item["path"] == path:
            item["status"] = "uploaded"
    save_queue(queue)


# ─── Haupt-Upload-Funktion ───────────────────────────────────────────────────

def upload_single_video(video_path: Path) -> dict:
    """
    Lädt ein Video hoch und gibt Ergebnis zurück.
    Kümmert sich um Retry, Logging, Verschieben der Datei.
    """
    result = {"path": str(video_path), "video_id": None, "status": "error", "error": "",
              "ad_texts": {}}

    # Ad-Texte vor dem Upload generieren
    try:
        from text_generator import generate_and_save, load_texts_for_video
        existing = load_texts_for_video(video_path)
        if existing:
            result["ad_texts"] = existing
            logger.info(f"📝 Vorhandene Ad-Texte geladen für {video_path.name}")
        else:
            result["ad_texts"] = generate_and_save(video_path)
    except Exception as e:
        logger.warning(f"Textgenerierung fehlgeschlagen (Upload läuft trotzdem): {e}")

    try:
        video_id = upload_video(video_path)
        wait_for_video_ready(video_id)

        # Erfolgreich hochgeladen → in uploaded/ verschieben
        UPLOADED_DIR.mkdir(exist_ok=True)
        dest = UPLOADED_DIR / video_path.name
        video_path.rename(dest)

        # Texts-Datei auch verschieben falls vorhanden
        texts_file = video_path.with_suffix(".texts.json")
        if texts_file.exists():
            texts_file.rename(UPLOADED_DIR / texts_file.name)

        record_upload(video_path.name, video_id, "success")
        result["video_id"] = video_id
        result["status"]   = "success"
        logger.info(f"✅ {video_path.name} erfolgreich hochgeladen. Meta-ID: {video_id}")

    except Exception as e:
        error_msg = str(e)
        logger.error(f"❌ Upload fehlgeschlagen für {video_path.name}: {error_msg}")

        # In failed/ verschieben
        FAILED_DIR.mkdir(exist_ok=True)
        dest = FAILED_DIR / video_path.name
        try:
            video_path.rename(dest)
        except Exception:
            pass

        record_upload(video_path.name, "", "error", error_msg)
        result["error"] = error_msg

    return result


def run_upload_batch(video_paths: list) -> list:
    """
    Verarbeitet eine Liste von Videos mit Human-like Behavior:
    - Sequenziell (nie parallel)
    - Zufällige Delays zwischen Uploads
    - Tages-Limit respektieren
    - Rest in Queue für morgen
    """
    results       = []
    queued_for_later = []

    for i, video_path in enumerate(video_paths, start=1):
        video_path = Path(video_path)

        if not video_path.exists():
            logger.warning(f"⚠️ Datei nicht gefunden: {video_path} — übersprungen")
            continue

        # Tages-Limit prüfen
        if not check_daily_limit():
            remaining = video_paths[i - 1:]
            queued_for_later = remaining
            logger.info(f"📋 {len(remaining)} Video(s) für morgen in Queue gespeichert")
            add_to_queue(remaining)
            break

        logger.info(f"━━━ Video {i}/{len(video_paths)}: {video_path.name} ━━━")
        result = upload_single_video(video_path)
        results.append(result)

        # Wenn nicht das letzte Video: Human-Delay
        uploads_remaining = len(video_paths) - i
        if uploads_remaining > 0 and result["status"] == "success":
            human_delay(
                min_s=MIN_DELAY_SECONDS,
                max_s=MAX_DELAY_SECONDS,
                label=f"Pause vor nächstem Upload ({uploads_remaining} übrig)"
            )

    return results, queued_for_later


# ─── Approved-Folder watcher ─────────────────────────────────────────────────

def get_approved_videos() -> list:
    """Gibt alle MP4-Dateien aus dem approved/ Ordner zurück."""
    APPROVED_DIR.mkdir(exist_ok=True)
    videos = sorted(APPROVED_DIR.glob("*.mp4")) + sorted(APPROVED_DIR.glob("*.MP4"))
    return videos


if __name__ == "__main__":
    logger.info("🚀 Meta Uploader gestartet (standalone)")

    videos = get_approved_videos()
    if not videos:
        logger.info("📂 Keine Videos im approved/ Ordner — nichts zu tun.")
    else:
        logger.info(f"📂 {len(videos)} Video(s) im approved/ Ordner gefunden")
        results, queued = run_upload_batch(videos)

        success = sum(1 for r in results if r["status"] == "success")
        failed  = sum(1 for r in results if r["status"] == "error")
        logger.info(f"\n{'━'*40}")
        logger.info(f"✅ Fertig: {success} hochgeladen, {failed} Fehler, {len(queued)} in Queue")
