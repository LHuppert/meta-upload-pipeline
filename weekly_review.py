"""
Weekly Review — weekly_review.py
Wöchentliches KPI-Review: 7 Analyse-Module, 4 Telegram-Nachrichten.

Naming-Convention: H01_S1 x MTA_S2 x CTA01
  Hook:  H\\d{2}_S\\d+[a-z]?
  MCAT:  MTA_S\\d+[a-z]?
  CTA:   CTA\\d{2,3}

Aufruf:
  python weekly_review.py --run
  python weekly_review.py --dry-run
  python weekly_review.py --history 4
  python weekly_review.py --module hook

Wird montags 08:00 UTC via APScheduler in main.py aufgerufen.
"""

import argparse
import json
import logging
import os
import re
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import requests as _http

from hook_analyzer import run_analysis as analyze_hooks, parse_hook_id
from meta_uploader import meta_request, AD_ACCOUNT_ID
from optimizer import load_state
from settings_manager import settings, BASE_DIR

logger = logging.getLogger(__name__)

# ── Konfiguration ──────────────────────────────────────────────────────────────

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID",   "")

HISTORY_FILE = BASE_DIR / "logs" / "weekly_history.json"
MAX_HISTORY_WEEKS = 12
TELEGRAM_PAUSE_SECONDS = 2


# ── Naming-Parser ──────────────────────────────────────────────────────────────

def parse_mcat_id(ad_name: str) -> str:
    """
    Extrahiert MCAT-ID aus Ad-Name.
    "H01_S1 x MTA_S2 x CTA02" → "MTA_S2"
    """
    m = re.search(r"MTA_S\d+[a-z]?", ad_name, re.IGNORECASE)
    return m.group(0).upper() if m else ""


def parse_cta_id(ad_name: str) -> str:
    """
    Extrahiert CTA-ID aus Ad-Name.
    "H01_S1 x MTA_S2 x CTA02" → "CTA02"
    """
    m = re.search(r"CTA\d{2,3}", ad_name, re.IGNORECASE)
    return m.group(0).upper() if m else ""


def parse_combo_key(ad_name: str) -> str:
    """Hook × MCAT × CTA als Kombi-Key. Leer wenn unvollständig."""
    h = parse_hook_id(ad_name)
    m = parse_mcat_id(ad_name)
    c = parse_cta_id(ad_name)
    if h and m and c:
        return f"{h} × {m} × {c}"
    return ""


# ── KPI-Abruf ─────────────────────────────────────────────────────────────────

def _date_range(days: int) -> tuple[str, str]:
    today = datetime.now(timezone.utc).date()
    since = (today - timedelta(days=days)).isoformat()
    return since, today.isoformat()


def _fetch_insights(ad_id: str, days: int = 7,
                    extra_fields: str = "",
                    breakdown: str = "") -> list[dict]:
    """
    Ruft Insights für eine Ad ab.
    Gibt data-Liste zurück (leer bei Fehler).
    """
    since, until = _date_range(days)
    fields = "impressions,inline_link_clicks,video_3_sec_watched_actions,cpm,spend,reach,actions"
    if extra_fields:
        fields += f",{extra_fields}"

    params: dict = {
        "fields":     fields,
        "time_range": json.dumps({"since": since, "until": until}),
        "level":      "ad",
    }
    if breakdown:
        params["breakdowns"] = breakdown

    try:
        data = meta_request("GET", f"{ad_id}/insights", params=params)
        return data.get("data", [])
    except Exception as e:
        logger.warning(f"Insights-Abruf fehlgeschlagen ({ad_id}): {e}")
        return []


def _extract_kpis(row: dict) -> dict:
    """Extrahiert KPI-Dict aus einem Insights-Row."""
    impressions = int(row.get("impressions", 0))
    clicks      = int(row.get("inline_link_clicks", 0))
    cpm         = float(row.get("cpm", 0.0))
    spend       = float(row.get("spend", 0.0))
    reach       = int(row.get("reach", 0))

    views_3s = sum(
        int(a.get("value", 0))
        for a in row.get("video_3_sec_watched_actions", [])
    )

    conversions = sum(
        int(a.get("value", 0))
        for a in row.get("actions", [])
        if a.get("action_type") == "offsite_conversion.fb_pixel_purchase"
    )

    hook_rate = views_3s / impressions if impressions > 0 else 0.0
    ctr_link  = clicks   / impressions if impressions > 0 else 0.0
    frequency = impressions / reach    if reach > 0 else 0.0
    roas      = (conversions * float(settings.get("campaigns.average_order_value", 80))) / spend \
                if spend > 0 else 0.0

    return {
        "impressions": impressions,
        "views_3s":    views_3s,
        "clicks":      clicks,
        "conversions": conversions,
        "hook_rate":   hook_rate,
        "ctr_link":    ctr_link,
        "cpm":         cpm,
        "spend":       spend,
        "frequency":   frequency,
        "roas":        roas,
    }


def _get_all_ads_with_kpis(days: int = 7) -> list[dict]:
    """
    Gibt alle aktiven/freeze-Ads mit KPIs zurück.
    [{name, ad_id, kpis}]
    """
    state = load_state()
    ads   = [
        a for a in state.get("active_ads", [])
        if a.get("status") in ("active", "freeze")
    ]

    result = []
    for ad in ads:
        rows = _fetch_insights(ad["ad_id"], days=days)
        if not rows:
            continue
        kpis = _extract_kpis(rows[0])
        if kpis["impressions"] < 100:
            continue
        result.append({
            "ad_id": ad["ad_id"],
            "name":  ad.get("name", ""),
            "kpis":  kpis,
        })

    logger.info(f"Weekly Review: {len(result)} Ads mit ausreichend Daten")
    return result


# ── Modul 1: Hook-Analyse ─────────────────────────────────────────────────────

def module_hook(days: int = 7) -> dict:
    """
    Delegiert an hook_analyzer.run_analysis().
    Gibt {hook_id: {kpis, score, grade, trend}} zurück.
    """
    return analyze_hooks(days=days)


# ── Modul 2: MCAT-Analyse ─────────────────────────────────────────────────────

def score_mcat(kpis: dict) -> dict:
    """
    MCAT-Score: Engagement-Qualität.
    Hook Rate 40%, CTR 30%, Conversion Rate 30%.
    """
    hr   = float(kpis.get("hook_rate",   0.0))
    ctr  = float(kpis.get("ctr_link",    0.0))
    conv = float(kpis.get("conversions", 0))
    imp  = int(kpis.get("impressions",   0))

    hr_min  = float(settings.get("kpi.hook_rate_min", 0.25))
    ctr_min = float(settings.get("kpi.ctr_min",       0.01))

    hr_norm  = min(hr  / max(hr_min,  0.001), 2.0) / 2.0
    ctr_norm = min(ctr / max(ctr_min, 0.001), 2.0) / 2.0

    # Conv Rate (relativ zu Impressions)
    conv_rate = conv / imp if imp > 0 else 0.0
    conv_norm = min(conv_rate / 0.001, 2.0) / 2.0  # Ziel: 0.1% Conversion Rate

    score = round(hr_norm * 0.40 + ctr_norm * 0.30 + conv_norm * 0.30, 4)

    if score >= 0.75:
        grade = "A"
    elif score >= 0.50:
        grade = "B"
    elif score >= 0.30:
        grade = "C"
    else:
        grade = "D"

    confidence = "low" if imp < 500 else ("medium" if imp < 2000 else "high")
    return {"score": score, "grade": grade, "score_pct": int(score * 100), "confidence": confidence}


def module_mcat(ads_kpis: list[dict]) -> dict:
    """
    Aggregiert KPIs nach MCAT-ID, berechnet Drop-Off-Punkt.
    Returns {mcat_id: {kpis, score, grade, drop_off}}
    """
    buckets: dict[str, list] = defaultdict(list)
    for item in ads_kpis:
        mcat_id = parse_mcat_id(item.get("name", ""))
        if mcat_id:
            buckets[mcat_id].append(item["kpis"])

    result = {}
    for mcat_id, kpis_list in buckets.items():
        n = len(kpis_list)

        def avg(key: str) -> float:
            vals = [k.get(key, 0.0) for k in kpis_list if k]
            return sum(vals) / len(vals) if vals else 0.0

        aggregated = {
            "impressions": int(sum(k.get("impressions", 0)  for k in kpis_list)),
            "views_3s":    int(sum(k.get("views_3s", 0)     for k in kpis_list)),
            "clicks":      int(sum(k.get("clicks", 0)       for k in kpis_list)),
            "conversions": int(sum(k.get("conversions", 0)  for k in kpis_list)),
            "hook_rate":   avg("hook_rate"),
            "ctr_link":    avg("ctr_link"),
            "cpm":         avg("cpm"),
            "spend":       sum(k.get("spend", 0.0)          for k in kpis_list),
            "frequency":   avg("frequency"),
            "roas":        avg("roas"),
        }

        scored = score_mcat(aggregated)

        # Drop-Off: Wo verliert man den Viewer?
        imp = aggregated["impressions"]
        v3s = aggregated["views_3s"]
        clk = aggregated["clicks"]
        conv = aggregated["conversions"]

        if imp > 0 and v3s > 0:
            if v3s / imp < 0.25:
                drop_off = "Hook (3s < 25%)"
            elif clk / imp < 0.01:
                drop_off = "Video-Body (CTR < 1%)"
            elif conv < 1:
                drop_off = "Landing Page (0 Conv.)"
            else:
                drop_off = "OK"
        else:
            drop_off = "Zu wenig Daten"

        result[mcat_id] = {
            "ad_count":   n,
            "kpis":       aggregated,
            "score":      scored["score"],
            "grade":      scored["grade"],
            "score_pct":  scored["score_pct"],
            "confidence": scored["confidence"],
            "drop_off":   drop_off,
        }

    return result


# ── Modul 3: CTA-Analyse ──────────────────────────────────────────────────────

def score_cta(kpis: dict) -> dict:
    """
    CTA-Score: Click-to-Conversion-Fokus.
    CTR 50%, Conv/Click 30%, CPM 20%.
    """
    ctr       = float(kpis.get("ctr_link",    0.0))
    conv      = float(kpis.get("conversions", 0))
    clicks    = float(kpis.get("clicks",      0))
    cpm       = float(kpis.get("cpm",         0.0))
    cpm_max   = float(settings.get("kpi.cpm_malus_threshold", 18.0))
    ctr_min   = float(settings.get("kpi.ctr_min", 0.01))
    imp       = int(kpis.get("impressions", 0))

    ctr_norm  = min(ctr / max(ctr_min, 0.0001), 2.0) / 2.0
    c2c       = conv / max(clicks, 1)   # Click-to-Conversion Rate
    c2c_norm  = min(c2c / 0.02, 2.0) / 2.0  # Ziel: 2% c2c
    cpm_norm  = max(0.0, 1.0 - cpm / (cpm_max * 2))

    score = round(ctr_norm * 0.50 + c2c_norm * 0.30 + cpm_norm * 0.20, 4)

    if score >= 0.75:
        grade = "A"
    elif score >= 0.50:
        grade = "B"
    elif score >= 0.30:
        grade = "C"
    else:
        grade = "D"

    confidence = "low" if imp < 500 else ("medium" if imp < 2000 else "high")
    return {"score": score, "grade": grade, "score_pct": int(score * 100), "confidence": confidence}


def module_cta(ads_kpis: list[dict]) -> dict:
    """
    Aggregiert KPIs nach CTA-ID.
    Returns {cta_id: {kpis, score, grade, click_to_conv}}
    """
    buckets: dict[str, list] = defaultdict(list)
    for item in ads_kpis:
        cta_id = parse_cta_id(item.get("name", ""))
        if cta_id:
            buckets[cta_id].append(item["kpis"])

    result = {}
    for cta_id, kpis_list in buckets.items():
        n = len(kpis_list)

        def avg(key: str) -> float:
            vals = [k.get(key, 0.0) for k in kpis_list if k]
            return sum(vals) / len(vals) if vals else 0.0

        aggregated = {
            "impressions": int(sum(k.get("impressions", 0)  for k in kpis_list)),
            "clicks":      int(sum(k.get("clicks", 0)       for k in kpis_list)),
            "conversions": int(sum(k.get("conversions", 0)  for k in kpis_list)),
            "hook_rate":   avg("hook_rate"),
            "ctr_link":    avg("ctr_link"),
            "cpm":         avg("cpm"),
            "spend":       sum(k.get("spend", 0.0)          for k in kpis_list),
            "roas":        avg("roas"),
        }

        scored = score_cta(aggregated)
        clicks = aggregated["clicks"]
        conv   = aggregated["conversions"]
        click_to_conv = f"{conv / max(clicks, 1) * 100:.1f}%"

        result[cta_id] = {
            "ad_count":      n,
            "kpis":          aggregated,
            "score":         scored["score"],
            "grade":         scored["grade"],
            "score_pct":     scored["score_pct"],
            "confidence":    scored["confidence"],
            "click_to_conv": click_to_conv,
        }

    return result


# ── Modul 4: Kombinations-Analyse ─────────────────────────────────────────────

def module_combos(ads_kpis: list[dict]) -> dict:
    """
    Analysiert Hook × MCAT × CTA Kombinationen.
    Findet Synergy-Pairs: welche Hook+MCAT oder Hook+CTA Kombis überdurchschnittlich sind.
    """
    buckets: dict[str, list] = defaultdict(list)
    for item in ads_kpis:
        combo = parse_combo_key(item.get("name", ""))
        if combo:
            buckets[combo].append(item["kpis"])

    if not buckets:
        return {"combos": {}, "synergy_pairs": []}

    combo_scores = {}
    for combo, kpis_list in buckets.items():
        def avg(key: str) -> float:
            vals = [k.get(key, 0.0) for k in kpis_list if k]
            return sum(vals) / len(vals) if vals else 0.0

        agg = {
            "impressions": int(sum(k.get("impressions", 0) for k in kpis_list)),
            "hook_rate":   avg("hook_rate"),
            "ctr_link":    avg("ctr_link"),
            "cpm":         avg("cpm"),
            "roas":        avg("roas"),
            "conversions": int(sum(k.get("conversions", 0) for k in kpis_list)),
        }

        # Kombinierter Score: Hook Rate 40%, CTR 40%, ROAS 20%
        hr_min  = float(settings.get("kpi.hook_rate_min", 0.25))
        ctr_min = float(settings.get("kpi.ctr_min", 0.01))
        hr_norm   = min(agg["hook_rate"] / max(hr_min, 0.001), 2.0) / 2.0
        ctr_norm  = min(agg["ctr_link"]  / max(ctr_min, 0.001), 2.0) / 2.0
        roas_norm = min(agg["roas"] / 2.0, 1.0) * 0.5  # 2.0 ROAS = Ziel

        score = round(hr_norm * 0.40 + ctr_norm * 0.40 + roas_norm * 0.20, 4)
        combo_scores[combo] = {
            "ad_count":    len(kpis_list),
            "kpis":        agg,
            "score":       score,
            "score_pct":   int(score * 100),
        }

    # Synergy-Pairs: Kombis mit Score > Median + 15%
    scores = [v["score"] for v in combo_scores.values()]
    if scores:
        median   = sorted(scores)[len(scores) // 2]
        threshold = median * 1.15
        synergy_pairs = [
            k for k, v in combo_scores.items()
            if v["score"] >= threshold and v["kpis"]["impressions"] >= 200
        ]
    else:
        synergy_pairs = []

    # Sortiert nach Score absteigend
    sorted_combos = dict(
        sorted(combo_scores.items(), key=lambda x: x[1]["score"], reverse=True)
    )

    return {"combos": sorted_combos, "synergy_pairs": synergy_pairs}


# ── Modul 5: Budget-Effizienz ─────────────────────────────────────────────────

def module_budget(ads_kpis: list[dict], prev_week_data: Optional[dict] = None) -> dict:
    """
    Analysiert Spend-Effizienz: CPA, ROAS, Budget-Verteilung.
    Vergleicht mit Vorwoche wenn vorhanden.
    """
    if not ads_kpis:
        return {}

    total_spend = sum(a["kpis"].get("spend", 0.0)     for a in ads_kpis)
    total_conv  = sum(a["kpis"].get("conversions", 0) for a in ads_kpis)
    total_clicks = sum(a["kpis"].get("clicks", 0)     for a in ads_kpis)
    total_imp   = sum(a["kpis"].get("impressions", 0) for a in ads_kpis)

    cpa     = total_spend / max(total_conv, 1)
    avg_cpm = total_spend / max(total_imp, 1) * 1000
    aov     = float(settings.get("campaigns.average_order_value", 80))
    roas    = (total_conv * aov) / max(total_spend, 0.01)

    # Top 3 nach Effizienz (ROAS)
    top3_efficiency = sorted(
        [a for a in ads_kpis if a["kpis"].get("impressions", 0) >= 200],
        key=lambda x: x["kpis"].get("roas", 0.0),
        reverse=True,
    )[:3]

    # Wasted Spend: Ads mit ROAS < 1.0 und Spend > 1€
    wasted = [
        a for a in ads_kpis
        if a["kpis"].get("roas", 0.0) < 1.0 and a["kpis"].get("spend", 0.0) > 1.0
    ]

    # Effizienz-Trend vs. Vorwoche
    efficiency_trend = "–"
    if prev_week_data and "budget" in prev_week_data:
        prev = prev_week_data["budget"]
        prev_roas = prev.get("roas", 0.0)
        if prev_roas > 0:
            delta = (roas - prev_roas) / prev_roas * 100
            if delta > 5:
                efficiency_trend = f"↑ +{delta:.0f}%"
            elif delta < -5:
                efficiency_trend = f"↓ {delta:.0f}%"
            else:
                efficiency_trend = "→"

    return {
        "total_spend":       round(total_spend, 2),
        "total_conversions": total_conv,
        "cpa":               round(cpa, 2),
        "avg_cpm":           round(avg_cpm, 2),
        "roas":              round(roas, 2),
        "efficiency_trend":  efficiency_trend,
        "ad_count":          len(ads_kpis),
        "top3_efficiency":   [a["name"] for a in top3_efficiency],
        "wasted_spend_ads":  len(wasted),
        "wasted_spend_eur":  round(sum(a["kpis"].get("spend", 0) for a in wasted), 2),
    }


# ── Modul 6: Creative Fatigue ─────────────────────────────────────────────────

def module_fatigue(ads_kpis: list[dict]) -> dict:
    """
    Identifiziert Ads mit Frequency-Erschöpfung oder CTR/Hook-Decay.
    """
    freq_max = float(settings.get("optimizer.creative_fatigue_freq", 3.0))

    high_freq   = []
    ctr_decay   = []
    hook_decay  = []
    healthy     = []

    hr_min  = float(settings.get("kpi.hook_rate_min", 0.25))
    ctr_min = float(settings.get("kpi.ctr_min",       0.01))

    for ad in ads_kpis:
        kpis = ad["kpis"]
        freq = float(kpis.get("frequency", 0.0))
        ctr  = float(kpis.get("ctr_link",  0.0))
        hr   = float(kpis.get("hook_rate", 0.0))
        name = ad.get("name", ad.get("ad_id", ""))

        fatigued = False
        if freq > freq_max:
            high_freq.append({"name": name, "frequency": round(freq, 1)})
            fatigued = True
        if ctr < ctr_min * 0.5:  # CTR unter 50% des Minimums = Decay
            ctr_decay.append({"name": name, "ctr": f"{ctr*100:.2f}%"})
            fatigued = True
        if hr < hr_min * 0.5:    # Hook Rate unter 50% des Minimums = Decay
            hook_decay.append({"name": name, "hook_rate": f"{hr*100:.1f}%"})
            fatigued = True
        if not fatigued:
            healthy.append(name)

    return {
        "total_ads":    len(ads_kpis),
        "healthy":      len(healthy),
        "high_freq":    high_freq,
        "ctr_decay":    ctr_decay,
        "hook_decay":   hook_decay,
        "fatigue_rate": f"{(len(ads_kpis) - len(healthy)) / max(len(ads_kpis), 1) * 100:.0f}%",
    }


# ── Modul 7: Wochentags-Analyse ───────────────────────────────────────────────

_DOW_MAP = {
    "1": "Mo", "2": "Di", "3": "Mi", "4": "Do",
    "5": "Fr", "6": "Sa", "7": "So",
}


def module_dayparting(days: int = 7) -> dict:
    """
    Ruft Insights mit breakdown=day_of_week ab.
    Identifiziert beste/schlechteste Tage nach CTR und Hook Rate.
    """
    state = load_state()
    ads   = [
        a for a in state.get("active_ads", [])
        if a.get("status") in ("active", "freeze")
    ]

    day_buckets: dict[str, list] = defaultdict(list)

    for ad in ads[:10]:  # Max 10 Ads für Rate-Limits
        rows = _fetch_insights(ad["ad_id"], days=days, breakdown="day_of_week")
        for row in rows:
            dow = str(row.get("day_of_week", ""))
            if dow:
                day_buckets[dow].append(_extract_kpis(row))

    if not day_buckets:
        return {"note": "Keine Wochentags-Daten verfügbar"}

    day_stats = {}
    for dow, kpis_list in day_buckets.items():
        def avg(key: str) -> float:
            vals = [k.get(key, 0.0) for k in kpis_list if k]
            return sum(vals) / len(vals) if vals else 0.0

        day_stats[_DOW_MAP.get(dow, dow)] = {
            "impressions": int(sum(k.get("impressions", 0) for k in kpis_list)),
            "hook_rate":   round(avg("hook_rate") * 100, 1),
            "ctr":         round(avg("ctr_link")  * 100, 2),
            "cpm":         round(avg("cpm"),             2),
            "spend":       round(sum(k.get("spend", 0.0) for k in kpis_list), 2),
        }

    # Bester / schlechtester Tag nach CTR
    days_sorted = sorted(day_stats.items(), key=lambda x: x[1]["ctr"], reverse=True)
    best_day  = days_sorted[0][0]  if days_sorted else "–"
    worst_day = days_sorted[-1][0] if days_sorted else "–"

    return {
        "by_day":    day_stats,
        "best_day":  best_day,
        "worst_day": worst_day,
    }


# ── History ───────────────────────────────────────────────────────────────────

def _load_history() -> dict:
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_history(h: dict) -> None:
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_FILE.write_text(json.dumps(h, indent=2, ensure_ascii=False), encoding="utf-8")


def _get_week_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-W%W")


def _get_prev_week_data() -> Optional[dict]:
    hist  = _load_history()
    weeks = sorted(hist.keys())
    if len(weeks) >= 2:
        return hist[weeks[-2]]
    return None


def _save_week(data: dict) -> None:
    hist     = _load_history()
    week_key = _get_week_key()
    hist[week_key] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        **data,
    }
    weeks = sorted(hist.keys())
    for old in weeks[:-MAX_HISTORY_WEEKS]:
        del hist[old]
    _save_history(hist)


# ── Telegram ──────────────────────────────────────────────────────────────────

def _send_telegram(text: str, dry_run: bool = False) -> bool:
    if dry_run:
        logger.info(f"[DRY-RUN] Telegram:\n{text}")
        return True
    if not BOT_TOKEN or not CHAT_ID:
        logger.warning("Telegram nicht konfiguriert")
        return False
    try:
        resp = _http.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={
                "chat_id":                  CHAT_ID,
                "text":                     text,
                "parse_mode":               "Markdown",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        if not resp.ok:
            logger.error(f"Telegram fehlgeschlagen: {resp.status_code} {resp.text[:200]}")
            return False
        return True
    except Exception as e:
        logger.error(f"Telegram-Fehler: {e}")
        return False


def _grade_emoji(grade: str) -> str:
    return {"A": "🟢", "B": "🟡", "C": "🟠", "D": "🔴"}.get(grade, "⚪")


def _trend_fmt(trend: str) -> str:
    if "↑" in trend:
        return f"_{trend}_"
    if "↓" in trend:
        return f"_{trend}_"
    return trend


# ── Report-Nachrichten ────────────────────────────────────────────────────────

def _build_msg1_overview(results: dict, week_key: str) -> str:
    """Nachricht 1: Übersicht + Hook-Analyse Top 3."""
    budget  = results.get("budget", {})
    hooks   = results.get("hook",   {})

    spend   = budget.get("total_spend",       0.0)
    conv    = budget.get("total_conversions", 0)
    roas    = budget.get("roas",              0.0)
    trend   = budget.get("efficiency_trend",  "–")
    ads_n   = budget.get("ad_count",          0)

    lines = [
        f"*📊 Weekly Review — {week_key}*",
        f"",
        f"*Woche auf einen Blick*",
        f"• Aktive Ads: {ads_n}",
        f"• Gesamtausgaben: {spend:.2f}€",
        f"• Conversions: {conv}",
        f"• ROAS: {roas:.2f}  {_trend_fmt(trend)}",
        f"",
        f"*🎣 Hook-Analyse — Top 3*",
    ]

    if hooks:
        top3 = sorted(hooks.items(), key=lambda x: x[1].get("score", 0), reverse=True)[:3]
        for i, (hid, hdata) in enumerate(top3, 1):
            grade = hdata.get("grade", "?")
            pct   = hdata.get("score_pct", 0)
            hr    = hdata.get("kpis", {}).get("hook_rate", 0) * 100
            trend_h = hdata.get("trend", "–")
            lines.append(
                f"{i}. `{hid}` — {_grade_emoji(grade)} {grade} ({pct}%)  "
                f"HR: {hr:.1f}%  {_trend_fmt(trend_h)}"
            )
        if len(hooks) > 3:
            lines.append(f"_...und {len(hooks) - 3} weitere Hooks_")
    else:
        lines.append("_Keine Hook-Daten verfügbar_")

    return "\n".join(lines)


def _build_msg2_mcat_cta(results: dict) -> str:
    """Nachricht 2: MCAT + CTA Analyse."""
    mcats = results.get("mcat", {})
    ctas  = results.get("cta",  {})

    lines = ["*📐 MCAT & CTA Analyse*", ""]

    # MCAT
    lines.append("*Middle Content*")
    if mcats:
        for mid, mdata in sorted(mcats.items(), key=lambda x: x[1]["score"], reverse=True):
            grade    = mdata.get("grade", "?")
            pct      = mdata.get("score_pct", 0)
            drop_off = mdata.get("drop_off", "")
            hr       = mdata.get("kpis", {}).get("hook_rate", 0) * 100
            ctr      = mdata.get("kpis", {}).get("ctr_link",  0) * 100
            lines.append(
                f"• `{mid}` — {_grade_emoji(grade)} {grade} ({pct}%)  "
                f"HR: {hr:.1f}%  CTR: {ctr:.2f}%"
            )
            if drop_off and drop_off != "OK":
                lines.append(f"  ⚠️ Drop-Off: _{drop_off}_")
    else:
        lines.append("_Keine MCAT-Daten_")

    lines.append("")

    # CTA
    lines.append("*Call to Action*")
    if ctas:
        for cid, cdata in sorted(ctas.items(), key=lambda x: x[1]["score"], reverse=True):
            grade = cdata.get("grade", "?")
            pct   = cdata.get("score_pct", 0)
            c2c   = cdata.get("click_to_conv", "–")
            ctr   = cdata.get("kpis", {}).get("ctr_link", 0) * 100
            lines.append(
                f"• `{cid}` — {_grade_emoji(grade)} {grade} ({pct}%)  "
                f"CTR: {ctr:.2f}%  C2C: {c2c}"
            )
    else:
        lines.append("_Keine CTA-Daten_")

    return "\n".join(lines)


def _build_msg3_combos_budget(results: dict) -> str:
    """Nachricht 3: Kombinationen + Budget-Effizienz."""
    combos  = results.get("combos",  {})
    budget  = results.get("budget",  {})

    lines = ["*🔀 Kombinations-Analyse*", ""]

    combo_data   = combos.get("combos",       {})
    synergy_pairs = combos.get("synergy_pairs", [])

    if combo_data:
        top3 = list(combo_data.items())[:3]
        for combo, cdata in top3:
            pct  = cdata.get("score_pct", 0)
            conv = cdata.get("kpis", {}).get("conversions", 0)
            lines.append(f"• `{combo}`  Score: {pct}%  Conv: {conv}")
        if synergy_pairs:
            lines.append(f"")
            lines.append(f"✨ *Synergy-Pairs* ({len(synergy_pairs)}):")
            for sp in synergy_pairs[:3]:
                lines.append(f"  → `{sp}`")
    else:
        lines.append("_Keine Kombinations-Daten_")

    lines += ["", "*💰 Budget-Effizienz*", ""]

    cpa       = budget.get("cpa",              0.0)
    avg_cpm   = budget.get("avg_cpm",          0.0)
    wasted_n  = budget.get("wasted_spend_ads", 0)
    wasted_e  = budget.get("wasted_spend_eur", 0.0)
    top3_eff  = budget.get("top3_efficiency",  [])

    lines.append(f"• CPA: {cpa:.2f}€  |  Ø CPM: {avg_cpm:.2f}€")
    if wasted_n:
        lines.append(f"• ⚠️ Wasted Spend: {wasted_e:.2f}€ ({wasted_n} Ads mit ROAS < 1)")

    if top3_eff:
        lines.append("• Top 3 ROAS-Ads:")
        for name in top3_eff:
            lines.append(f"  → _{name[:50]}_")

    return "\n".join(lines)


def _build_msg4_fatigue_dayparting(results: dict) -> str:
    """Nachricht 4: Creative Fatigue + Wochentags-Analyse + Empfehlungen."""
    fatigue    = results.get("fatigue",    {})
    dayparting = results.get("dayparting", {})

    lines = ["*😴 Creative Fatigue*", ""]

    fatigue_rate = fatigue.get("fatigue_rate", "0%")
    healthy      = fatigue.get("healthy",      0)
    total_ads    = fatigue.get("total_ads",    0)
    high_freq    = fatigue.get("high_freq",    [])
    ctr_decay    = fatigue.get("ctr_decay",    [])
    hook_decay   = fatigue.get("hook_decay",   [])

    lines.append(f"• {healthy}/{total_ads} Ads gesund  |  Fatigue-Rate: {fatigue_rate}")

    if high_freq:
        lines.append(f"• Hohe Frequency ({len(high_freq)}):")
        for ad in high_freq[:3]:
            lines.append(f"  → _{ad['name'][:40]}_ (Freq: {ad['frequency']})")

    if ctr_decay:
        lines.append(f"• CTR-Decay ({len(ctr_decay)}):")
        for ad in ctr_decay[:2]:
            lines.append(f"  → _{ad['name'][:40]}_ ({ad['ctr']})")

    if hook_decay:
        lines.append(f"• Hook-Decay ({len(hook_decay)}):")
        for ad in hook_decay[:2]:
            lines.append(f"  → _{ad['name'][:40]}_ ({ad['hook_rate']})")

    lines += ["", "*📅 Wochentags-Analyse*", ""]

    by_day    = dayparting.get("by_day",    {})
    best_day  = dayparting.get("best_day",  "–")
    worst_day = dayparting.get("worst_day", "–")

    if by_day:
        lines.append(f"• Bester Tag:      {best_day}")
        lines.append(f"• Schlechtster Tag: {worst_day}")
        lines.append("")
        lines.append("```")
        lines.append("Tag | CTR    | HR     | CPM")
        lines.append("----|--------|--------|------")
        for day, stats in sorted(by_day.items()):
            lines.append(
                f"{day:3} | {stats['ctr']:5.2f}% | "
                f"{stats['hook_rate']:5.1f}% | {stats['cpm']:5.2f}€"
            )
        lines.append("```")
    else:
        lines.append("_Keine Wochentags-Daten_")

    # Empfehlungen
    recommendations = _build_recommendations(results)
    if recommendations:
        lines += ["", "*💡 Empfehlungen für nächste Woche*"]
        for rec in recommendations:
            lines.append(f"• {rec}")

    return "\n".join(lines)


def _build_recommendations(results: dict) -> list[str]:
    """Generiert automatische Empfehlungen basierend auf allen Modulen."""
    recs  = []
    hooks = results.get("hook",    {})
    budget = results.get("budget", {})
    fatigue = results.get("fatigue", {})

    # Hook-Empfehlungen
    if hooks:
        d_hooks = [hid for hid, h in hooks.items() if h.get("grade") == "D"]
        a_hooks = [hid for hid, h in hooks.items() if h.get("grade") == "A"]
        if d_hooks:
            recs.append(f"Hook(s) austauschen: {', '.join(d_hooks[:3])}")
        if a_hooks:
            recs.append(f"Bewährte Hooks weiter testen: {', '.join(a_hooks[:3])}")

    # Budget-Empfehlungen
    wasted = budget.get("wasted_spend_eur", 0.0)
    roas   = budget.get("roas", 0.0)
    if wasted > 5.0:
        recs.append(f"{wasted:.2f}€ Wasted Spend identifiziert — schwache Ads pausieren")
    if roas < float(settings.get("roas.min_roas", 2.0)):
        recs.append(f"ROAS {roas:.2f} unter Ziel — Budgets reduzieren und Creative testen")

    # Fatigue-Empfehlungen
    high_freq_count = len(fatigue.get("high_freq", []))
    if high_freq_count > 0:
        recs.append(f"{high_freq_count} Ads mit hoher Frequency — neue Creatives einplanen")

    return recs


# ── Haupt-Run ─────────────────────────────────────────────────────────────────

def run_weekly_review(days: int = 7, dry_run: bool = False,
                      module_filter: Optional[str] = None) -> dict:
    """
    Führt das vollständige Weekly Review durch.
    Returns das vollständige Results-Dict.
    """
    week_key = _get_week_key()
    logger.info(f"Weekly Review gestartet — {week_key} (days={days}, dry_run={dry_run})")

    ads_kpis    = _get_all_ads_with_kpis(days=days)
    prev_data   = _get_prev_week_data()

    run_all = module_filter is None

    results: dict = {}

    if run_all or module_filter == "hook":
        logger.info("Modul: Hook-Analyse")
        results["hook"] = module_hook(days=days)

    if run_all or module_filter == "mcat":
        logger.info("Modul: MCAT-Analyse")
        results["mcat"] = module_mcat(ads_kpis)

    if run_all or module_filter == "cta":
        logger.info("Modul: CTA-Analyse")
        results["cta"] = module_cta(ads_kpis)

    if run_all or module_filter == "combos":
        logger.info("Modul: Kombinations-Analyse")
        results["combos"] = module_combos(ads_kpis)

    if run_all or module_filter == "budget":
        logger.info("Modul: Budget-Effizienz")
        results["budget"] = module_budget(ads_kpis, prev_data)

    if run_all or module_filter == "fatigue":
        logger.info("Modul: Creative Fatigue")
        results["fatigue"] = module_fatigue(ads_kpis)

    if run_all or module_filter == "dayparting":
        logger.info("Modul: Wochentags-Analyse")
        results["dayparting"] = module_dayparting(days=days)

    # History speichern
    if run_all and not dry_run:
        _save_week(results)
        logger.info(f"Weekly Review in History gespeichert: {week_key}")

    # 4 Telegram-Nachrichten senden
    if run_all:
        messages = [
            _build_msg1_overview(results, week_key),
            _build_msg2_mcat_cta(results),
            _build_msg3_combos_budget(results),
            _build_msg4_fatigue_dayparting(results),
        ]
        for i, msg in enumerate(messages, 1):
            logger.info(f"Sende Telegram-Nachricht {i}/4")
            _send_telegram(msg, dry_run=dry_run)
            if i < len(messages):
                time.sleep(TELEGRAM_PAUSE_SECONDS)

    logger.info("Weekly Review abgeschlossen")
    return results


# ── CLI ───────────────────────────────────────────────────────────────────────

def _print_history(n: int) -> None:
    hist = _load_history()
    weeks = sorted(hist.keys(), reverse=True)[:n]
    if not weeks:
        print("Keine History vorhanden.")
        return
    for wk in weeks:
        entry   = hist[wk]
        ts      = entry.get("generated_at", "?")[:10]
        budget  = entry.get("budget", {})
        spend   = budget.get("total_spend",       "?")
        conv    = budget.get("total_conversions",  "?")
        roas    = budget.get("roas",               "?")
        hooks   = entry.get("hook",               {})
        top_h   = max(hooks.items(), key=lambda x: x[1].get("score", 0))[0] \
                  if hooks else "–"
        print(f"{wk} ({ts}) | Spend: {spend}€ | Conv: {conv} | ROAS: {roas} | Top-Hook: {top_h}")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(description="Weekly Review — Terra Preta Meta Pipeline")
    parser.add_argument("--run",      action="store_true", help="Review jetzt ausführen")
    parser.add_argument("--dry-run",  action="store_true", help="Kein Telegram, kein History-Save")
    parser.add_argument("--history",  type=int, metavar="N", help="Letzte N Wochen anzeigen")
    parser.add_argument("--module",   type=str,
                        choices=["hook","mcat","cta","combos","budget","fatigue","dayparting"],
                        help="Nur ein Modul ausführen")
    parser.add_argument("--days",     type=int, default=7, help="Zeitraum in Tagen (default: 7)")
    args = parser.parse_args()

    if args.history:
        _print_history(args.history)
        return

    if args.run or args.dry_run or args.module:
        results = run_weekly_review(
            days=args.days,
            dry_run=args.dry_run,
            module_filter=args.module,
        )
        if args.module:
            print(json.dumps(results.get(args.module, {}), indent=2, ensure_ascii=False))
        else:
            print(f"Weekly Review abgeschlossen. {len(results)} Module ausgeführt.")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
