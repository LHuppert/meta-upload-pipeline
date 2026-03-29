"""
Gmail-Bestelltracking — gmail_tracker.py
Zählt Bestellungen via IMAP für ROAS-Berechnung.
Bestellmails kommen an: bestellen@terrapretawein.de
Betreff: [Terra Preta Wein]: Neue Bestellung (#12345)
"""

import imaplib
import email
import os
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from email.header import decode_header

from dotenv import load_dotenv
load_dotenv()

from settings_manager import settings, BASE_DIR

logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
GMAIL_USER     = os.getenv("GMAIL_USER",     "bestellen@terrapretawein.de")
GMAIL_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")
IMAP_HOST      = "imap.gmail.com"
IMAP_PORT      = 993
ORDER_SUBJECT  = "[Terra Preta Wein]: Neue Bestellung"

ORDERS_CACHE   = BASE_DIR / "logs" / "orders_cache.json"


def _connect() -> imaplib.IMAP4_SSL | None:
    """Verbindet mit Gmail IMAP. Gibt None zurück bei Fehler."""
    if not GMAIL_PASSWORD:
        logger.warning("GMAIL_APP_PASSWORD nicht gesetzt — Bestelltracking deaktiviert.")
        return None
    try:
        mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        mail.login(GMAIL_USER, GMAIL_PASSWORD)
        return mail
    except Exception as e:
        logger.error(f"Gmail IMAP Verbindungsfehler: {e}")
        return None


def _decode_subject(raw_subject: str) -> str:
    parts = decode_header(raw_subject)
    decoded = ""
    for part, enc in parts:
        if isinstance(part, bytes):
            decoded += part.decode(enc or "utf-8", errors="replace")
        else:
            decoded += part
    return decoded


def count_orders(days: int = 1) -> int:
    """Zählt Bestellmails der letzten N Tage."""
    mail = _connect()
    if not mail:
        return _count_from_cache(days)
    try:
        mail.select("INBOX")
        since = (datetime.now() - timedelta(days=days)).strftime("%d-%b-%Y")
        _, data = mail.search(None, f'(SINCE {since} SUBJECT "{ORDER_SUBJECT}")')
        ids = data[0].split() if data[0] else []
        count = len(ids)
        logger.info(f"Bestellungen letzte {days} Tag(e): {count}")
        _update_cache(count, days)
        mail.logout()
        return count
    except Exception as e:
        logger.error(f"Fehler beim Zählen der Bestellungen: {e}")
        mail.logout()
        return _count_from_cache(days)


def count_orders_today() -> int:
    return count_orders(days=1)


def count_orders_last_7_days() -> int:
    return count_orders(days=7)


def count_orders_last_30_days() -> int:
    return count_orders(days=30)


def get_roas_estimate(days: int = 7) -> dict:
    """
    Berechnet geschätzten ROAS basierend auf Bestellanzahl und AOV.
    Gibt dict mit orders, revenue_estimate, aov zurück.
    """
    orders = count_orders(days=days)
    aov    = int(settings.get("campaigns.average_order_value", 80))
    revenue = orders * aov
    return {
        "days":             days,
        "orders":           orders,
        "aov_eur":          aov,
        "revenue_estimate": revenue,
    }


# ── Cache (Fallback wenn IMAP nicht verfügbar) ────────────────────────────────

def _update_cache(count: int, days: int):
    try:
        ORDERS_CACHE.parent.mkdir(parents=True, exist_ok=True)
        cache = _load_cache()
        cache[f"last_{days}d"] = {
            "count":     count,
            "updated_at": datetime.now().isoformat(),
        }
        ORDERS_CACHE.write_text(json.dumps(cache, indent=2), encoding="utf-8")
    except Exception:
        pass


def _load_cache() -> dict:
    try:
        if ORDERS_CACHE.exists():
            return json.loads(ORDERS_CACHE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _count_from_cache(days: int) -> int:
    cache = _load_cache()
    key   = f"last_{days}d"
    if key in cache:
        return cache[key].get("count", 0)
    return 0


def get_summary_text(days: int = 7) -> str:
    """Gibt einen formatierten Text für Telegram-Reports zurück."""
    data = get_roas_estimate(days=days)
    if not GMAIL_PASSWORD:
        return "📦 Bestelltracking: GMAIL_APP_PASSWORD nicht konfiguriert."
    return (
        f"📦 Bestellungen ({days} Tage): *{data['orders']}*\n"
        f"💰 Umsatz-Schätzung: *{data['revenue_estimate']} €* (AOV {data['aov_eur']} €)\n"
    )


# ── Direktaufruf zum Testen ───────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("Heute:", count_orders_today())
    print("7 Tage:", count_orders_last_7_days())
    print("30 Tage:", count_orders_last_30_days())
    print(get_summary_text(7))
