"""
Production Guard — production_guard.py
Sicherheits-Layer: Account-Health, Spend-Cap, Anomalie, Rejections, Fatigue.

Alle Schwellenwerte kommen aus settings_manager — im Dashboard konfigurierbar.
"""

import json
import logging
import os
import requests as _http
from datetime import datetime, timezone, timedelta
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from meta_uploader import meta_request, AD_ACCOUNT_ID
from settings_manager import settings, BASE_DIR

logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

CPM_HISTORY_FILE = BASE_DIR / "logs" / "cpm_history.json"

# Meta Account Status: 1=ACTIVE, 9=in_grace_period (noch ok)
HEALTHY_STATUSES = {1, 9}

NEGATIVE_KEYWORDS = [
    "schlecht", "schrecklich", "ekelhaft", "betrug", "lüge", "fake",
    "abzocke", "nie wieder", "enttäuscht", "katastrophe", "unverschämt",
    "terrible", "awful", "scam", "fraud", "worst",
]


# ── Telegram Alert ────────────────────────────────────────────────────────────

def send_alert(message: str, level: str = "warning") -> None:
    """Sendet Telegram-Alert. Fehler werden nur geloggt."""
    if not BOT_TOKEN or not CHAT_ID:
        logger.warning(f"Telegram nicht konfiguriert — Alert: {message[:100]}")
        return
    emoji = {"warning": "⚠️", "error": "🚨", "info": "ℹ️"}.get(level, "⚠️")
    try:
        resp = _http.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={
                "chat_id":    CHAT_ID,
                "text":       f"{emoji} *Production Guard*\n\n{message}",
                "parse_mode": "Markdown",
            },
            timeout=10,
        )
        if not resp.ok:
            logger.error(f"Telegram-Alert fehlgeschlagen: {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        logger.error(f"Telegram-Alert Fehler: {e}")


# ── Account Health ────────────────────────────────────────────────────────────

def check_account_health() -> dict:
    """
    Ruft Account-Status von Meta ab.
    Returns {"healthy": bool, "reason": str|None}
    Bei API-Fehler: healthy=True (fail-open — nie blockieren bei Netzwerkproblem).
    """
    try:
        data   = meta_request("GET", AD_ACCOUNT_ID,
                              params={"fields": "account_status,disable_reason,name"})
        status = int(data.get("account_status", 1))
        if status not in HEALTHY_STATUSES:
            reason = f"account_status={status}, disable_reason={data.get('disable_reason')}"
            logger.error(f"🚨 Account-Problem: {reason}")
            send_alert(f"Account-Problem erkannt!\n{reason}", "error")
            return {"healthy": False, "reason": reason}
        logger.info(f"✅ Account-Health OK (Status {status})")
        return {"healthy": True, "reason": None}
    except Exception as e:
        logger.warning(f"Account-Health-Check fehlgeschlagen: {e} — fail-open")
        return {"healthy": True, "reason": None}


# ── Spend Cap ─────────────────────────────────────────────────────────────────

def get_todays_spend_eur() -> float:
    """Ruft heutigen Account-Spend von Meta ab (EUR)."""
    today = datetime.now(timezone.utc).date().isoformat()
    try:
        data = meta_request(
            "GET", f"{AD_ACCOUNT_ID}/insights",
            params={
                "fields":     "spend",
                "time_range": json.dumps({"since": today, "until": today}),
                "level":      "account",
            },
        )
        rows = data.get("data", [])
        return float(rows[0].get("spend", 0.0)) if rows else 0.0
    except Exception as e:
        logger.warning(f"Spend-Abruf fehlgeschlagen: {e}")
        return 0.0


def check_daily_spend_cap(state: dict) -> bool:
    """
    Prüft ob das tägliche Spend-Cap erreicht wurde.
    Bei Überschreitung: alle Ads pausieren, pipeline_paused setzen.
    Returns True wenn Cap erreicht (= Pipeline soll stoppen).
    """
    cap   = float(settings.get("budget.daily_spend_cap_eur", 100.0))
    spend = get_todays_spend_eur()
    state["daily_spend_eur"] = spend
    logger.info(f"💸 Tages-Spend: {spend:.2f}€ / {cap:.2f}€ Cap")

    if spend >= cap:
        msg = f"Tägliches Spend-Cap erreicht: {spend:.2f}€ / {cap:.2f}€ — Pipeline pausiert"
        logger.warning(f"🛑 {msg}")
        state["pipeline_paused"] = True
        state["pause_reason"]    = "daily_spend_cap_reached"
        send_alert(msg, "warning")
        return True

    # Frühwarnung bei 80 %
    if spend >= cap * 0.8:
        send_alert(
            f"Spend-Warnung: {spend:.2f}€ — noch {cap - spend:.2f}€ bis Limit",
            "warning",
        )
    return False


# ── CPM Anomalie-Erkennung ────────────────────────────────────────────────────

def _load_cpm_history() -> dict:
    if CPM_HISTORY_FILE.exists():
        try:
            return json.loads(CPM_HISTORY_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_cpm_history(h: dict) -> None:
    CPM_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    CPM_HISTORY_FILE.write_text(json.dumps(h, indent=2), encoding="utf-8")


def record_cpm(ad_id: str, cpm: float) -> None:
    hist = _load_cpm_history()
    hist.setdefault(ad_id, []).append({
        "date": datetime.now(timezone.utc).date().isoformat(),
        "cpm":  cpm,
    })
    hist[ad_id] = hist[ad_id][-14:]
    _save_cpm_history(hist)


def check_anomaly(kpis: dict, ad_id: str) -> bool:
    """
    True wenn CPM > anomaly_cpm_factor × historischer Durchschnitt.
    Braucht mind. 3 Datenpunkte. Bei Fehler: False (kein falsches Pausieren).
    """
    factor  = float(settings.get("guard.anomaly_cpm_factor", 10.0))
    hist    = _load_cpm_history().get(ad_id, [])
    cur_cpm = float(kpis.get("cpm", 0.0))

    if len(hist) < 3:
        return False

    avg = sum(h["cpm"] for h in hist[:-1]) / len(hist[:-1])
    if avg <= 0:
        return False

    ratio = cur_cpm / avg
    if ratio > factor:
        msg = (
            f"CPM-Anomalie: Ad `{ad_id}`\n"
            f"CPM: {cur_cpm:.2f}€ ({ratio:.1f}× Ø {avg:.2f}€)\n"
            f"Ad wurde automatisch pausiert."
        )
        logger.warning(f"🔴 {msg}")
        send_alert(msg, "error")
        return True
    return False


# ── Creative Fatigue ──────────────────────────────────────────────────────────

def check_creative_fatigue(ad_id: str, kpis: dict) -> tuple[bool, float]:
    """
    Prüft Frequency aus KPI-Dict (Frequency muss im kpis-Dict übergeben werden).
    Returns (fatigued, frequency).
    """
    freq_max  = float(settings.get("optimizer.creative_fatigue_freq", 3.0))
    frequency = float(kpis.get("frequency", 0.0))
    fatigued  = frequency > freq_max

    if fatigued:
        logger.info(
            f"😴 Creative Fatigue: Ad {ad_id} — "
            f"Frequency {frequency:.1f} > {freq_max}"
        )
        send_alert(
            f"Creative Fatigue: Ad `{ad_id}`\n"
            f"Frequency {frequency:.1f} überschreitet Schwelle {freq_max}\n"
            f"Ad pausiert.",
            "warning",
        )
    return fatigued, frequency


# ── Rejection Handling ────────────────────────────────────────────────────────

def handle_rejection(ad_id: str, state: dict) -> None:
    """
    Verarbeitet eine Meta-Ablehnung.
    Erhöht rejection_counter_24h. Bei >= MAX: Pipeline pausieren.
    """
    max_rej = int(settings.get("safety.max_rejections_24h", 2))

    # Per-Ad Zähler
    for ad in state.get("active_ads", []):
        if ad.get("ad_id") == ad_id:
            ad["rejection_count"] = ad.get("rejection_count", 0) + 1
            break

    state["rejection_counter_24h"] = state.get("rejection_counter_24h", 0) + 1
    count = state["rejection_counter_24h"]
    logger.warning(f"🚫 Ablehnung: Ad {ad_id} — {count} in 24h")

    if count == max_rej:
        msg = f"⚠️ {max_rej} Ablehnungen in 24h — Pipeline pausiert zur Prüfung\nAd: `{ad_id}`"
        send_alert(msg, "warning")
        state["pipeline_paused"] = True
        state["pause_reason"]    = "rejection_limit_reached"

    elif count > max_rej:
        msg = f"🚨 {count} Ablehnungen — manuelle Prüfung erforderlich\nLetzte Ad: `{ad_id}`"
        send_alert(msg, "error")


def reset_rejection_counter(state: dict) -> None:
    """
    Setzt rejection_counter_24h täglich um 00:00 UTC zurück.
    """
    now   = datetime.now(timezone.utc)
    last  = state.get("last_rejection_reset")

    if last:
        try:
            last_dt = datetime.fromisoformat(last)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            if last_dt.date() >= now.date():
                return  # Heute schon zurückgesetzt
        except Exception:
            pass

    state["rejection_counter_24h"] = 0
    state["last_rejection_reset"]  = now.isoformat()
    logger.info("🔄 Rejection-Counter auf 0 zurückgesetzt (neuer Tag)")


# ── Kommentar-Monitor ─────────────────────────────────────────────────────────

def check_ad_comments(ad_id: str) -> list[str]:
    """
    Prüft Post unter der Ad auf negative Kommentare.
    Returns Liste negativer Kommentar-Texte. Bei Fehler: [].
    """
    try:
        ad_data = meta_request("GET", ad_id,
                               params={"fields": "effective_object_story_id"})
        post_id = ad_data.get("effective_object_story_id", "")
        if not post_id:
            return []

        resp = meta_request("GET", f"{post_id}/comments",
                            params={"fields": "message,created_time", "limit": "50"})
        negative = [
            c.get("message", "")
            for c in resp.get("data", [])
            if any(kw in c.get("message", "").lower() for kw in NEGATIVE_KEYWORDS)
        ]
        if negative:
            send_alert(
                f"Negative Kommentare bei Ad `{ad_id}`\n"
                f"Anzahl: {len(negative)}\n"
                f"Beispiel: \"{negative[0][:100]}\"",
                "warning",
            )
        return negative
    except Exception as e:
        logger.warning(f"Kommentar-Check für {ad_id} fehlgeschlagen: {e}")
        return []


# ── ROAS Check ────────────────────────────────────────────────────────────────

def check_roas(kpis_history: list, state: dict) -> bool:
    """
    Prüft ob ROAS die letzten ROAS_PAUSE_DAYS Tage unter MIN_ROAS lag.
    kpis_history: liste von {"date": ..., "roas": float}
    Returns True wenn Pause empfohlen (= Pipeline soll stoppen).
    """
    min_roas    = float(settings.get("roas.min_roas",       2.0))
    pause_days  = int(settings.get("roas.roas_pause_days",  3))

    if len(kpis_history) < pause_days:
        return False

    recent = kpis_history[-pause_days:]
    below  = [r for r in recent if float(r.get("roas", 999)) < min_roas]

    if len(below) >= pause_days:
        avg  = sum(r["roas"] for r in below) / len(below)
        msg  = (
            f"ROAS unter {min_roas} für {pause_days} aufeinanderfolgende Tage\n"
            f"Ø ROAS: {avg:.2f} — alle Ads pausiert"
        )
        logger.warning(f"📉 {msg}")
        state["pipeline_paused"] = True
        state["pause_reason"]    = "roas_below_threshold"
        send_alert(msg, "error")
        return True
    return False


# ── Fehler-Eskalator ──────────────────────────────────────────────────────────

class ErrorEscalator:
    """Zählt API-Fehler und sendet Alert bei >= max_errors."""

    def __init__(self):
        self._count   = 0
        self._alerted = False

    @property
    def _max(self) -> int:
        return int(settings.get("safety.max_api_errors_before_pause", 3))

    def record(self, context: str, error: Exception, state: dict | None = None) -> None:
        self._count += 1
        logger.error(f"API-Fehler #{self._count}/{self._max}: {context} — {error}")
        if self._count >= self._max and not self._alerted:
            send_alert(
                f"🚨 {self._max} fehlgeschlagene API-Calls!\n"
                f"Letzter Fehler: {context}\n`{error}`",
                "error",
            )
            self._alerted = True
            if state is not None:
                state["pipeline_paused"] = True
                state["pause_reason"]    = f"api_error_limit_{context[:50]}"

    def reset(self) -> None:
        self._count   = 0
        self._alerted = False


# Globale Instanz
error_escalator = ErrorEscalator()
