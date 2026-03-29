"""
Hook Analyzer — hook_analyzer.py
Analysiert Hook-Performance (erste 3 Sekunden) aller Ads.

Naming-Convention: H01_S1 x MTA_S1 x CTA01
Hook-ID = H\d{2}_S\d+[a-z]?  z.B. H01_S1, H02_S2a

Wird von weekly_review.py importiert.
"""

import json
import logging
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

from meta_uploader import meta_request, AD_ACCOUNT_ID
from settings_manager import settings, BASE_DIR

logger = logging.getLogger(__name__)

HOOK_HISTORY_FILE = BASE_DIR / "logs" / "hook_history.json"

# ── Schwellwerte (aus Settings, mit Defaults) ─────────────────────────────────
def _hr_min()  -> float: return float(settings.get("kpi.hook_rate_min",  0.25))
def _ctr_min() -> float: return float(settings.get("kpi.ctr_min",        0.01))
def _cpm_max() -> float: return float(settings.get("kpi.cpm_malus_threshold", 18.0))

GRADE_THRESHOLDS = {
    "A": 0.75,
    "B": 0.50,
    "C": 0.30,
}  # D = alles darunter


# ── Naming-Parser ─────────────────────────────────────────────────────────────

def parse_hook_id(ad_name: str) -> str:
    """
    Extrahiert Hook-ID aus Ad-Name.
    Beispiel: "H01_S1 x MTA_S2 x CTA02" → "H01_S1"
    Gibt "" zurück wenn kein Match.
    """
    m = re.search(r"H\d{2}_S\d+[a-z]?", ad_name, re.IGNORECASE)
    return m.group(0).upper() if m else ""


# ── KPI-Abruf ─────────────────────────────────────────────────────────────────

def get_all_active_ad_ids() -> list[dict]:
    """Gibt alle aktiven Ads als [{"ad_id": ..., "name": ...}] zurück."""
    from optimizer import load_state
    state = load_state()
    return [
        {"ad_id": a["ad_id"], "name": a["name"]}
        for a in state.get("active_ads", [])
        if a.get("status") in ("active", "freeze")
    ]


def fetch_hook_kpis(ad_id: str, days: int = 7) -> dict | None:
    """
    Ruft Hook-spezifische KPIs ab.
    Felder: impressions, 3s-views, CTR, CPM, Hook Rate.
    """
    today = datetime.now(timezone.utc).date()
    since = (today - timedelta(days=days)).isoformat()
    until = today.isoformat()

    try:
        data = meta_request(
            "GET", f"{ad_id}/insights",
            params={
                "fields": (
                    "impressions,inline_link_clicks,"
                    "video_3_sec_watched_actions,"
                    "cpm,spend,reach"
                ),
                "time_range": json.dumps({"since": since, "until": until}),
                "level": "ad",
            },
        )
    except Exception as e:
        logger.error(f"Hook-KPI-Abruf für {ad_id} fehlgeschlagen: {e}")
        return None

    rows = data.get("data", [])
    if not rows:
        return None

    row         = rows[0]
    impressions = int(row.get("impressions", 0))
    clicks      = int(row.get("inline_link_clicks", 0))
    cpm         = float(row.get("cpm", 0.0))
    spend       = float(row.get("spend", 0.0))
    reach       = int(row.get("reach", 0))

    views_3s = sum(
        int(a.get("value", 0))
        for a in row.get("video_3_sec_watched_actions", [])
    )

    hook_rate  = views_3s / impressions if impressions > 0 else 0.0
    ctr        = clicks   / impressions if impressions > 0 else 0.0
    frequency  = impressions / reach    if reach > 0 else 0.0

    return {
        "impressions": impressions,
        "views_3s":    views_3s,
        "clicks":      clicks,
        "hook_rate":   hook_rate,
        "ctr_link":    ctr,
        "cpm":         cpm,
        "spend":       spend,
        "frequency":   frequency,
    }


# ── Score ─────────────────────────────────────────────────────────────────────

def score_hook(kpis: dict) -> dict:
    """
    Berechnet Hook-Score 0.0–1.0.

    Hook Rate   50%  — Ziel: >= hook_rate_min (25%)
    CTR         30%  — Ziel: >= ctr_min (1%)
    CPM         20%  — Malus wenn > cpm_max (18€)

    Returns: {"score": float, "grade": "A/B/C/D", "score_pct": int,
              "confidence": "low/medium/high"}
    """
    hr    = float(kpis.get("hook_rate", 0.0))
    ctr   = float(kpis.get("ctr_link",  0.0))
    cpm   = float(kpis.get("cpm",       0.0))
    imp   = int(kpis.get("impressions", 0))

    hr_norm  = min(hr  / max(_hr_min(), 0.001), 2.0) / 2.0
    ctr_norm = min(ctr / max(_ctr_min(), 0.0001), 2.0) / 2.0
    cpm_norm = max(0.0, 1.0 - cpm / (_cpm_max() * 2))

    score = round(hr_norm * 0.50 + ctr_norm * 0.30 + cpm_norm * 0.20, 4)

    if score >= GRADE_THRESHOLDS["A"]:
        grade = "A"
    elif score >= GRADE_THRESHOLDS["B"]:
        grade = "B"
    elif score >= GRADE_THRESHOLDS["C"]:
        grade = "C"
    else:
        grade = "D"

    confidence = "low" if imp < 500 else ("medium" if imp < 2000 else "high")

    return {
        "score":      score,
        "grade":      grade,
        "score_pct":  int(score * 100),
        "confidence": confidence,
    }


# ── Aggregation per Hook-ID ────────────────────────────────────────────────────

def aggregate_by_hook(ads_kpis: list[dict]) -> dict:
    """
    ads_kpis: [{"name": "H01_S1 x ...", "kpis": {...}}]
    Returns: {"H01_S1": {"kpis_avg": ..., "ad_count": ..., "score": ...}, ...}
    """
    buckets: dict[str, list] = {}

    for item in ads_kpis:
        hook_id = parse_hook_id(item.get("name", ""))
        if not hook_id:
            continue
        buckets.setdefault(hook_id, []).append(item["kpis"])

    result: dict[str, dict] = {}
    for hook_id, kpis_list in buckets.items():
        n = len(kpis_list)

        def avg(key: str) -> float:
            vals = [k.get(key, 0.0) for k in kpis_list if k]
            return sum(vals) / len(vals) if vals else 0.0

        aggregated = {
            "impressions": int(sum(k.get("impressions", 0) for k in kpis_list)),
            "views_3s":    int(sum(k.get("views_3s", 0)    for k in kpis_list)),
            "clicks":      int(sum(k.get("clicks", 0)      for k in kpis_list)),
            "hook_rate":   avg("hook_rate"),
            "ctr_link":    avg("ctr_link"),
            "cpm":         avg("cpm"),
            "spend":       sum(k.get("spend", 0.0) for k in kpis_list),
            "frequency":   avg("frequency"),
        }
        scored = score_hook(aggregated)
        result[hook_id] = {
            "ad_count":   n,
            "kpis":       aggregated,
            "score":      scored["score"],
            "grade":      scored["grade"],
            "score_pct":  scored["score_pct"],
            "confidence": scored["confidence"],
        }

    return result


# ── History ───────────────────────────────────────────────────────────────────

def _load_history() -> dict:
    if HOOK_HISTORY_FILE.exists():
        try:
            return json.loads(HOOK_HISTORY_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_history(h: dict) -> None:
    HOOK_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    HOOK_HISTORY_FILE.write_text(json.dumps(h, indent=2, ensure_ascii=False), encoding="utf-8")


def _get_week_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-W%W")


def _get_hook_trend(hook_id: str, current_score: float) -> str:
    hist = _load_history()
    weeks = sorted(hist.keys())
    for week in reversed(weeks[:-1]):  # Vorwoche
        entry = hist[week].get(hook_id)
        if entry:
            prev = entry.get("score", 0)
            delta = (current_score - prev) / max(prev, 0.001) * 100
            if delta > 5:
                return f"↑ +{delta:.0f}%"
            elif delta < -5:
                return f"↓ {delta:.0f}%"
            else:
                return "→"
    return "–"


def _save_week_results(results: dict) -> None:
    hist     = _load_history()
    week_key = _get_week_key()
    hist[week_key] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        **results,
    }
    # Max 12 Wochen behalten
    weeks = sorted(hist.keys())
    for old in weeks[:-12]:
        del hist[old]
    _save_history(hist)


# ── Hauptfunktion ─────────────────────────────────────────────────────────────

def run_analysis(days: int = 7) -> dict:
    """
    Analysiert alle aktiven Ads nach Hook-ID.
    Returns dict: {"H01_S1": {"kpis": {...}, "score": ..., "grade": ..., "trend": ...}, ...}
    Wird von weekly_review.py aufgerufen.
    """
    ads  = get_all_active_ad_ids()
    logger.info(f"Hook-Analyse: {len(ads)} Ads, letzte {days} Tage")

    ads_kpis: list[dict] = []
    for ad in ads:
        kpis = fetch_hook_kpis(ad["ad_id"], days=days)
        if kpis:
            ads_kpis.append({"name": ad["name"], "kpis": kpis})

    aggregated = aggregate_by_hook(ads_kpis)

    # Trends hinzufügen
    for hook_id, data in aggregated.items():
        data["trend"] = _get_hook_trend(hook_id, data["score"])

    _save_week_results({"hooks": aggregated})
    logger.info(f"Hook-Analyse abgeschlossen: {len(aggregated)} Hook-IDs")
    return aggregated
