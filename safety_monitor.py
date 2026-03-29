"""
Account Health Monitor — safety_monitor.py
Verfolgt Fehler-Raten und schützt den Meta Ad Account vor Sperrung.
"""

import json
import logging
from pathlib import Path
from datetime import datetime, date
from settings_manager import settings, BASE_DIR

logger = logging.getLogger(__name__)

HEALTH_FILE = BASE_DIR / "logs" / "account_health.json"

DEFAULTS = {
    "consecutive_errors":  0,
    "total_errors_today":  0,
    "total_uploads_today": 0,
    "last_error_time":     None,
    "last_upload_time":    None,
    "health_status":       "green",   # green / yellow / red
    "auto_paused_at":      None,
    "date":                None,
}


def _load() -> dict:
    if HEALTH_FILE.exists():
        try:
            data = json.loads(HEALTH_FILE.read_text(encoding="utf-8"))
            # Reset wenn neuer Tag
            if data.get("date") != date.today().isoformat():
                data = dict(DEFAULTS)
                data["date"] = date.today().isoformat()
            return data
        except Exception:
            pass
    d = dict(DEFAULTS)
    d["date"] = date.today().isoformat()
    return d


def _save(data: dict):
    HEALTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    HEALTH_FILE.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )


def get_health() -> dict:
    return _load()


def _update_status(data: dict) -> str:
    errors = data["consecutive_errors"]
    if errors == 0:
        return "green"
    elif errors <= 1:
        return "yellow"
    else:
        return "red"


def record_success(filename: str = ""):
    """Erfolgreiches Upload registrieren — setzt Fehler-Zähler zurück."""
    data = _load()
    data["consecutive_errors"]  = 0
    data["total_uploads_today"] += 1
    data["last_upload_time"]    = datetime.now().isoformat()
    data["health_status"]       = "green"
    _save(data)
    logger.info(f"✅ Health: Upload erfolgreich. Consecutive Errors zurückgesetzt.")


def record_error(error_msg: str = ""):
    """Fehler registrieren. Gibt True zurück wenn Auto-Pause ausgelöst wurde."""
    data = _load()
    data["consecutive_errors"]  += 1
    data["total_errors_today"]  += 1
    data["last_error_time"]      = datetime.now().isoformat()

    max_errors = int(settings.get("safety.max_errors_before_pause", 3))
    consecutive = data["consecutive_errors"]

    if consecutive >= max_errors:
        data["health_status"]  = "red"
        data["auto_paused_at"] = datetime.now().isoformat()
        _save(data)

        reason = f"Auto-Pause: {consecutive} Fehler in Folge. Letzter Fehler: {error_msg[:100]}"
        settings.enable_pause(reason, by="safety_monitor")
        logger.error(f"🔴 Health: AUTO-PAUSE aktiviert — {reason}")
        return True

    elif consecutive >= 2:
        data["health_status"] = "yellow"
        logger.warning(f"🟡 Health: {consecutive} Fehler in Folge — erhöhter Delay empfohlen")
    else:
        data["health_status"] = "yellow"

    _save(data)
    return False


def get_recommended_delay() -> float:
    """Gibt empfohlenen Delay-Faktor zurück basierend auf Health-Status."""
    data = _load()
    errors = data["consecutive_errors"]
    if errors == 0:
        return 1.0   # Normal
    elif errors == 1:
        return 1.5   # 50% mehr Delay
    else:
        return 2.0   # Doppelter Delay


def get_status_emoji() -> str:
    data = _load()
    status = data.get("health_status", "green")
    return {"green": "🟢", "yellow": "🟡", "red": "🔴"}.get(status, "⚪")


def get_summary() -> str:
    """Kurze Text-Zusammenfassung für Telegram/Dashboard."""
    data    = _load()
    emoji   = get_status_emoji()
    status  = data.get("health_status", "green").upper()
    errors  = data.get("consecutive_errors", 0)
    uploads = data.get("total_uploads_today", 0)

    text = f"{emoji} Account Health: {status}\n"
    text += f"Uploads heute: {uploads} | Fehler heute: {data.get('total_errors_today', 0)}\n"
    if errors > 0:
        text += f"⚠️ {errors} Fehler in Folge\n"
    if data.get("auto_paused_at"):
        text += f"🛑 Auto-Pause aktiv seit: {data['auto_paused_at'][:16]}\n"
    return text
