"""
Zentrale Einstellungsverwaltung — settings_manager.py
Alle anderen Module importieren von hier.
Thread-safe JSON-basierter Config-Store.
"""

import json
import os
import threading
from pathlib import Path
from datetime import datetime

BASE_DIR      = Path(os.getenv("DATA_DIR", "."))
SETTINGS_FILE = BASE_DIR / "settings.json"

DEFAULTS = {
    "safety": {
        "max_uploads_per_day":      5,
        "min_delay_seconds":        120,
        "max_delay_seconds":        180,
        "max_api_calls_per_hour":   200,
        "slowdown_threshold":       180,
        "max_retries":              3,
        "backoff_sequence":         [60, 120, 240, 480],
        "pause_mode":               False,
        "pause_reason":             "",
        "max_errors_before_pause":  3,
        "upload_window_enabled":    False,
        "upload_window_start":      "08:00",
        "upload_window_end":        "21:00",
        "warmup_mode":              False,
        "warmup_uploads_per_day":   2,
    },
    "schedule": {
        "daily_report_enabled":  True,
        "daily_report_time":     "08:00",
        "weekly_report_enabled": True,
        "weekly_report_day":     "monday",
    },
    "notifications": {
        "on_upload_complete":  True,
        "on_error":            True,
        "on_limit_reached":    True,
        "on_auto_pause":       True,
    },
    "dashboard": {
        "password_hash":            "",
        "session_lifetime_hours":   8,
        "login_attempts_max":       5,
        "login_lockout_minutes":    15,
    },
    "meta": {
        "api_version": "v21.0",
    },
    "campaigns": {
        "active": "testing_inhouse",
        "average_order_value": 80,
        "list": [
            {"id": "testing_inhouse", "name": "Testing Inhouse",  "description": "Eigene Videos — neues Weinpaket testen", "meta_campaign_id": "", "daily_budget": "10"},
            {"id": "testing_cutter",  "name": "Testing Cutter",   "description": "Cutter-Videos testen",                   "meta_campaign_id": "", "daily_budget": "10"},
            {"id": "scaling",         "name": "Scaling",          "description": "Top-Performer skalieren",                "meta_campaign_id": "", "daily_budget": "50"},
        ],
    },
    "geo_exclusion": {
        "enabled":       False,
        "location_name": "Gundersheim",
        "zip_code":      "67598",
        "latitude":      49.7153,
        "longitude":     8.2175,
        "radius_km":     25,
        "country":       "DE",
    },
    "updated_at":  None,
    "updated_by":  None,
}


class SettingsManager:
    """Thread-sicherer Singleton für alle Einstellungen."""

    def __init__(self, settings_file: Path = SETTINGS_FILE):
        self._file = settings_file
        self._lock = threading.Lock()
        self._ensure_file_exists()

    def _ensure_file_exists(self):
        if not self._file.exists():
            self._write(self._deep_merge(DEFAULTS, {}))

    def _read(self) -> dict:
        try:
            return json.loads(self._file.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _write(self, data: dict):
        self._file.parent.mkdir(parents=True, exist_ok=True)
        self._file.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8"
        )

    def _deep_merge(self, base: dict, override: dict) -> dict:
        result = dict(base)
        for k, v in override.items():
            if k in result and isinstance(result[k], dict) and isinstance(v, dict):
                result[k] = self._deep_merge(result[k], v)
            else:
                result[k] = v
        return result

    def _load(self) -> dict:
        raw = self._read()
        return self._deep_merge(DEFAULTS, raw)

    # ── Public API ──────────────────────────────────────────────────────────

    def get_all(self) -> dict:
        with self._lock:
            return self._load()

    def get(self, key_path: str, default=None):
        """Liest einen Wert. key_path z.B. 'safety.max_uploads_per_day'"""
        with self._lock:
            data = self._load()
            keys = key_path.split(".")
            cur = data
            for k in keys:
                if isinstance(cur, dict) and k in cur:
                    cur = cur[k]
                else:
                    return default
            return cur

    def set(self, key_path: str, value, updated_by: str = "system"):
        """Schreibt einen Wert. key_path z.B. 'safety.max_uploads_per_day'"""
        with self._lock:
            data = self._load()
            keys = key_path.split(".")
            cur = data
            for k in keys[:-1]:
                cur = cur.setdefault(k, {})
            cur[keys[-1]] = value
            data["updated_at"] = datetime.now().isoformat()
            data["updated_by"] = updated_by
            self._write(data)

    def update_section(self, section: str, values: dict, updated_by: str = "system"):
        """Aktualisiert mehrere Werte in einer Sektion."""
        with self._lock:
            data = self._load()
            if section not in data:
                data[section] = {}
            data[section].update(values)
            data["updated_at"] = datetime.now().isoformat()
            data["updated_by"] = updated_by
            self._write(data)

    # ── Häufig genutzte Shortcuts ───────────────────────────────────────────

    def is_paused(self) -> bool:
        return bool(self.get("safety.pause_mode", False))

    def enable_pause(self, reason: str = "Manuell pausiert", by: str = "system"):
        self.set("safety.pause_mode",   True,   by)
        self.set("safety.pause_reason", reason, by)

    def disable_pause(self, by: str = "system"):
        self.set("safety.pause_mode",   False, by)
        self.set("safety.pause_reason", "",    by)

    def get_daily_limit(self) -> int:
        if self.get("safety.warmup_mode", False):
            return int(self.get("safety.warmup_uploads_per_day", 2))
        return int(self.get("safety.max_uploads_per_day", 5))

    def is_within_upload_window(self) -> bool:
        """Prüft ob gerade im erlaubten Zeitfenster."""
        if not self.get("safety.upload_window_enabled", False):
            return True
        try:
            now    = datetime.now().strftime("%H:%M")
            start  = self.get("safety.upload_window_start", "08:00")
            end    = self.get("safety.upload_window_end",   "21:00")
            return start <= now <= end
        except Exception:
            return True

    def validate_and_set(self, key_path: str, value, updated_by: str = "system") -> tuple[bool, str]:
        """Setzt Wert nach Validierung. Gibt (ok, fehlermeldung) zurück."""
        RULES = {
            "safety.max_uploads_per_day":    (1, 20,  int),
            "safety.warmup_uploads_per_day": (1, 10,  int),
            "safety.min_delay_seconds":      (30, 600, int),
            "safety.max_delay_seconds":      (60, 900, int),
            "safety.max_api_calls_per_hour": (50, 500, int),
            "safety.slowdown_threshold":     (50, 499, int),
            "safety.max_errors_before_pause":(1, 10,  int),
        }
        if key_path in RULES:
            min_v, max_v, typ = RULES[key_path]
            try:
                value = typ(value)
            except (ValueError, TypeError):
                return False, f"Wert muss eine Zahl sein."
            if not (min_v <= value <= max_v):
                return False, f"Wert muss zwischen {min_v} und {max_v} liegen."
        self.set(key_path, value, updated_by)
        return True, ""


# ── Globale Singleton-Instanz ────────────────────────────────────────────────
settings = SettingsManager()
