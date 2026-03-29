"""
Ad Optimizer — optimizer.py
Zentrales Steuerungssystem der Meta Ad Pipeline.

Täglich via Cron:   python optimizer.py
Dry-run:            python optimizer.py --dry-run
Status:             python optimizer.py --status
Rotation erzwingen: python optimizer.py --force-rotation
Pipeline freigeben: python optimizer.py --unpause
Warteliste:         python optimizer.py --add-to-waitlist <filepath> [name]
"""

import argparse
import json
import logging
import os
import time
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests as _http
from dotenv import load_dotenv

load_dotenv()

from meta_uploader import (
    meta_request, upload_video, wait_for_video_ready,
    get_excluded_geo_locations, AD_ACCOUNT_ID, PAGE_ID,
)
from settings_manager import settings, BASE_DIR
from audit_logger import audit
from video_validator import validate_video, check_duplicate, check_ad_text
from production_guard import (
    check_account_health, check_daily_spend_cap, check_roas,
    check_anomaly, check_creative_fatigue, handle_rejection,
    reset_rejection_counter, record_cpm, error_escalator, send_alert,
)

# ── Logging ───────────────────────────────────────────────────────────────────
_log_dir = BASE_DIR / "logs"
_log_dir.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger(__name__)
if not logger.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        handlers=[
            logging.FileHandler(_log_dir / "optimizer.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )

# ── Env ───────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
SHOP_LINK = os.getenv("SHOP_LINK", "https://terrapretawein.de")
PIXEL_ID  = os.getenv("META_PIXEL_ID", "")

# ── Pfade ──────────────────────────────────────────────────────────────────────
AD_STATE_FILE = BASE_DIR / "ad_state.json"
DOWNLOADS_DIR = BASE_DIR / "downloads"
APPROVED_DIR  = BASE_DIR / "approved"

# ── Settings-Shortcuts (immer live aus settings_manager) ──────────────────────
def _s(key: str, default):
    return settings.get(key, default)

def _cs(key: str, default=None):
    """Campaign-spezifischer Wert (aktive Kampagne) oder globaler Default."""
    return settings.get_campaign_setting(key, default=default)

# ── State-Default ─────────────────────────────────────────────────────────────
_STATE_DEFAULT = {
    "active_ads":             [],
    "waiting_list":           [],
    "rotation_count":         0,
    "last_rotation":          None,
    "scale_alert_sent":       False,
    "daily_spend_eur":        0.0,
    "rejection_counter_24h":  0,
    "last_rejection_reset":   None,
    "pipeline_paused":        False,
    "pause_reason":           None,
    "roas_history":           [],
    "last_report_date":       None,
}


# ═══════════════════════════════════════════════════════════════════════════════
# 1. STATE-MANAGEMENT
# ═══════════════════════════════════════════════════════════════════════════════

def load_state() -> dict:
    """Lädt ad_state.json; legt Datei mit Defaults an falls nicht vorhanden."""
    if AD_STATE_FILE.exists():
        try:
            raw   = json.loads(AD_STATE_FILE.read_text(encoding="utf-8"))
            state = deepcopy(_STATE_DEFAULT)
            state.update(raw)
            return state
        except Exception as e:
            logger.error(f"ad_state.json unlesbar: {e} — nutze Defaults")
    return deepcopy(_STATE_DEFAULT)


def save_state(state: dict) -> None:
    """Atomar schreiben via Temp-Datei."""
    AD_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = AD_STATE_FILE.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(state, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    tmp.replace(AD_STATE_FILE)


def backup_state() -> None:
    """Sichert ad_state.json mit Timestamp in backups/."""
    audit.backup_state(AD_STATE_FILE)


# ═══════════════════════════════════════════════════════════════════════════════
# 3a. FREEZE & EVALUATION
# ═══════════════════════════════════════════════════════════════════════════════

def check_freeze(ad: dict) -> bool:
    """True wenn created_at + freeze_days ≤ jetzt (UTC)."""
    freeze_days = int(_s("optimizer.freeze_days", 7))
    try:
        created = datetime.fromisoformat(ad["created_at"])
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) >= created + timedelta(days=freeze_days)
    except (KeyError, ValueError) as e:
        logger.warning(f"check_freeze für '{ad.get('ad_id', '?')}': {e}")
        return False


def is_ready_for_evaluation(ad: dict, kpis: dict) -> bool:
    """
    True wenn:
      (check_freeze ODER conversions >= conversions_trigger)
      UND impressions >= min_impressions (Sicherheitsnetz, immer erforderlich)
    """
    min_imp    = int(_s("optimizer.min_impressions",    500))
    conv_trig  = int(_s("optimizer.conversions_trigger", 50))
    imps       = int(kpis.get("impressions", 0))

    if imps < min_imp:
        return False  # Sicherheitsnetz: nie auswerten ohne ausreichend Daten

    return check_freeze(ad) or kpis.get("conversions", 0) >= conv_trig


# ═══════════════════════════════════════════════════════════════════════════════
# 3b. KPI-SCORE
# ═══════════════════════════════════════════════════════════════════════════════

def calculate_score(kpis: dict) -> float:
    """
    Gewichteter Score 0.0–1.0. Gibt 0.0 zurück wenn Mindest-KPIs nicht erreicht.

    Hook Rate (3s/Imp)  40%  Ziel >= hook_rate_min (25%)
    CTR Link            40%  Ziel >= ctr_min (1%)
    CPM                 20%  Malus wenn > cpm_malus_threshold (18€)
    """
    hr_w   = float(_s("kpi.hook_rate_weight",    0.40))
    ctr_w  = float(_s("kpi.ctr_weight",          0.40))
    cpm_w  = float(_s("kpi.cpm_weight",          0.20))
    hr_min = float(_s("kpi.hook_rate_min",        0.25))
    ct_min = float(_s("kpi.ctr_min",             0.01))
    cpm_th = float(_s("kpi.cpm_malus_threshold", 18.0))

    hook_rate = float(kpis.get("hook_rate", 0.0))
    ctr       = float(kpis.get("ctr_link",  0.0))
    cpm       = float(kpis.get("cpm",       0.0))

    # Score = 0 wenn beide Hauptmetriken unter Schwelle
    if hook_rate < hr_min and ctr < ct_min:
        return 0.0

    hook_norm = min(hook_rate / hr_min, 2.0) / 2.0
    ctr_norm  = min(ctr / ct_min,       2.0) / 2.0
    cpm_norm  = max(0.0, 1.0 - cpm / (cpm_th * 2))

    return round(hook_norm * hr_w + ctr_norm * ctr_w + cpm_norm * cpm_w, 4)


# ═══════════════════════════════════════════════════════════════════════════════
# KPI-ABRUF
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_ad_kpis(ad_id: str, days: int = 7) -> dict | None:
    """
    Ruft KPIs einer Ad für die letzten N Tage ab.
    Gibt None bei API-Fehler zurück (Ad bleibt aktiv — nie abschalten bei Fehler).
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
                    "actions,cpm,spend,frequency"
                ),
                "time_range": json.dumps({"since": since, "until": until}),
                "level": "ad",
            },
        )
    except Exception as e:
        error_escalator.record(f"fetch_kpis/{ad_id}", e)
        logger.error(f"KPI-Abruf fehlgeschlagen für {ad_id}: {e}")
        return None

    rows = data.get("data", [])
    if not rows:
        return None

    row         = rows[0]
    impressions = int(row.get("impressions", 0))
    clicks      = int(row.get("inline_link_clicks", 0))
    cpm         = float(row.get("cpm", 0.0))
    frequency   = float(row.get("frequency", 0.0))

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
    ctr       = clicks  / impressions  if impressions > 0 else 0.0

    return {
        "impressions": impressions,
        "views_3s":    views_3s,
        "clicks":      clicks,
        "conversions": conversions,
        "hook_rate":   hook_rate,
        "ctr_link":    ctr,
        "cpm":         cpm,
        "frequency":   frequency,
        "spend":       float(row.get("spend", 0.0)),
    }


# ═══════════════════════════════════════════════════════════════════════════════
# META AD ERSTELLEN
# ═══════════════════════════════════════════════════════════════════════════════

def _get_active_campaign() -> dict:
    return settings.get_campaign()


def _create_meta_ad(
    video_id: str,
    ad_name: str,
    budget_cents: int,
    dry_run: bool = False,
) -> dict:
    """
    Erstellt AdCreative + AdSet + Ad.
    Returns {"ad_id": str, "adset_id": str}
    Targeting: DE, Alter 30–65, Geo-Ausschluss aus Settings.
    """
    camp = _get_active_campaign()
    campaign_id = camp.get("meta_campaign_id", "")
    if not campaign_id:
        raise RuntimeError(
            "Keine Meta Kampagnen-ID — bitte im Dashboard → Kampagnen eintragen."
        )

    if dry_run:
        fake_ad    = f"dry_ad_{video_id[:8]}"
        fake_adset = f"dry_adset_{video_id[:8]}"
        logger.info(f"[DRY-RUN] Ad '{ad_name}' → {fake_ad} / AdSet {fake_adset}")
        return {"ad_id": fake_ad, "adset_id": fake_adset}

    # 1. AdCreative
    creative_r = meta_request(
        "POST", f"{AD_ACCOUNT_ID}/adcreatives",
        data={
            "name": f"Creative — {ad_name}",
            "object_story_spec": json.dumps({
                "page_id": PAGE_ID,
                "video_data": {
                    "video_id":       video_id,
                    "call_to_action": {"type": "SHOP_NOW", "value": {"link": SHOP_LINK}},
                },
            }),
        },
    )
    creative_id = creative_r.get("id")
    if not creative_id:
        raise RuntimeError(f"AdCreative nicht erstellt: {creative_r}")

    # 2. Targeting: DE, Alter 30–65, optionaler Geo-Ausschluss
    targeting: dict = {
        "geo_locations":  {"countries": ["DE"]},
        "age_min": 30,
        "age_max": 65,
    }
    excluded = get_excluded_geo_locations()
    if excluded:
        targeting["excluded_geo_locations"] = excluded

    # Conversion-Ziel: OFFSITE_CONVERSIONS wenn Pixel konfiguriert
    if PIXEL_ID:
        optimization_goal = "OFFSITE_CONVERSIONS"
        promoted_object   = json.dumps({
            "pixel_id": PIXEL_ID, "custom_event_type": "PURCHASE",
        })
    else:
        optimization_goal = "LINK_CLICKS"
        promoted_object   = None

    adset_data: dict = {
        "name":              f"AdSet — {ad_name}",
        "campaign_id":       campaign_id,
        "daily_budget":      budget_cents,
        "billing_event":     "IMPRESSIONS",
        "optimization_goal": optimization_goal,
        "bid_strategy":      "LOWEST_COST_WITHOUT_CAP",
        "targeting":         json.dumps(targeting),
        "status":            "ACTIVE",
    }
    if promoted_object:
        adset_data["promoted_object"] = promoted_object

    adset_r  = meta_request("POST", f"{AD_ACCOUNT_ID}/adsets", data=adset_data)
    adset_id = adset_r.get("id")
    if not adset_id:
        raise RuntimeError(f"AdSet nicht erstellt: {adset_r}")

    # 3. Ad
    ad_r   = meta_request("POST", f"{AD_ACCOUNT_ID}/ads", data={
        "name":     ad_name,
        "adset_id": adset_id,
        "creative": json.dumps({"creative_id": creative_id}),
        "status":   "ACTIVE",
    })
    ad_id  = ad_r.get("id")
    if not ad_id:
        raise RuntimeError(f"Ad nicht erstellt: {ad_r}")

    logger.info(f"   ✅ Ad {ad_id} / AdSet {adset_id} / Creative {creative_id}")
    return {"ad_id": ad_id, "adset_id": adset_id}


# ═══════════════════════════════════════════════════════════════════════════════
# 2. UPLOAD-LOGIK (BATCH)
# ═══════════════════════════════════════════════════════════════════════════════

def upload_batch(ads: list, state: dict, dry_run: bool = False) -> list:
    """
    Lädt bis zu batch_size Ads hoch.

    Ablauf je Video:
    1. Account-Health prüfen
    2. Pipeline-Pause prüfen
    3. Duplicate-Check (MD5)
    4. Video-Validator (ffprobe)
    5. Ad-Text Policy-Check
    6. Upload + AdCreative/AdSet/Ad erstellen
    7. Warmup-Budget (3€) — nach warmup_hours auf 5€ erhöht
    8. 45s Delay zwischen Uploads
    """
    batch_size    = int(_s("optimizer.batch_size", 5))
    delay_s       = int(_s("optimizer.upload_delay_seconds", 45))
    warmup_eur    = float(_cs("warmup_budget_eur") or _s("budget.warmup_budget_eur", 3.0))
    warmup_cents  = int(warmup_eur * 100)

    created = []

    for i, video_path in enumerate(ads[:batch_size]):
        video_path = Path(video_path)

        # Pipeline-Pause
        if state.get("pipeline_paused"):
            logger.warning(
                f"Pipeline pausiert ({state.get('pause_reason')}) — Upload abgebrochen"
            )
            break

        # Account-Health (jedes Mal prüfen um frühzeitig zu stoppen)
        health = check_account_health()
        if not health["healthy"]:
            state["pipeline_paused"] = True
            state["pause_reason"]    = f"account_unhealthy: {health['reason']}"
            break

        if not video_path.exists():
            logger.warning(f"Video nicht gefunden: {video_path}")
            continue

        # Duplicate-Check
        if check_duplicate(str(video_path), state):
            audit.log("optimizer", "upload_skipped_duplicate", "video", video_path.name,
                      dry_run=dry_run)
            continue

        # Video-Validator
        val = validate_video(str(video_path))
        if not val["valid"]:
            logger.warning(f"Video ungültig: {video_path.name} — {val['errors']}")
            audit.log("optimizer", "upload_skipped_invalid", "video", video_path.name,
                      str(val["errors"]), dry_run=dry_run)
            continue

        # Ad-Text Policy (falls Texte vorhanden)
        try:
            from text_generator import load_texts_for_video, generate_and_save
            texts = load_texts_for_video(video_path) or generate_and_save(video_path)
            if texts:
                combined = " ".join(texts.values())
                policy   = check_ad_text(combined)
                if not policy["valid"]:
                    logger.warning(
                        f"Policy-Verstoß in Ad-Text für {video_path.name}: "
                        f"{policy['found_words']}"
                    )
                    audit.log("optimizer", "upload_skipped_policy", "video",
                              video_path.name, str(policy["found_words"]), dry_run=dry_run)
                    continue
        except Exception as e:
            logger.warning(f"Text-Check übersprungen: {e}")
            texts = {}

        ad_name = video_path.stem
        logger.info(f"📤 Upload: {video_path.name} (Warmup-Budget: {warmup_eur:.0f}€)")

        try:
            if dry_run:
                video_id = f"dry_vid_{video_path.stem[:10]}"
            else:
                video_id = upload_video(video_path)
                wait_for_video_ready(video_id)

            meta_ids = _create_meta_ad(video_id, ad_name, warmup_cents, dry_run=dry_run)

            ad_entry = {
                "ad_id":             meta_ids["ad_id"],
                "adset_id":          meta_ids["adset_id"],
                "name":              ad_name,
                "filename":          video_path.name,
                "video_id":          video_id,
                "created_at":        datetime.now(timezone.utc).isoformat(),
                "status":            "freeze",
                "batch":             max((a["batch"] for a in state["active_ads"]), default=0) + 1,
                "budget_eur":        warmup_eur,
                "rejection_count":   0,
                "impressions_total": 0,
                "conversions_total": 0,
            }
            state["active_ads"].append(ad_entry)
            created.append(ad_entry)
            save_state(state)

            freeze_until = (
                datetime.now(timezone.utc) + timedelta(days=int(_s("optimizer.freeze_days", 7)))
            ).date()
            logger.info(
                f"✅ '{ad_name}' hochgeladen — Ad {meta_ids['ad_id']} | "
                f"Freeze bis {freeze_until}"
            )
            audit.log("optimizer", "ad_uploaded", "ad", meta_ids["ad_id"],
                      f"file={video_path.name}", dry_run=dry_run)

        except Exception as e:
            error_escalator.record(f"upload/{video_path.name}", e, state)
            logger.error(f"❌ Upload fehlgeschlagen für {video_path.name}: {e}")
            audit.log("optimizer", "upload_failed", "video", video_path.name,
                      str(e)[:200], dry_run=dry_run)
            continue

        # Rate-Limiting: 45s zwischen Uploads
        if i < len(ads[:batch_size]) - 1:
            if not dry_run:
                logger.info(f"⏳ {delay_s}s Pause (Rate-Limiting)...")
                time.sleep(delay_s)

    return created


# ═══════════════════════════════════════════════════════════════════════════════
# WARMUP-BUDGET UPGRADE (3€ → 5€ nach warmup_hours)
# ═══════════════════════════════════════════════════════════════════════════════

def _apply_budget_upgrades(state: dict, dry_run: bool = False) -> None:
    """Erhöht Budget von Warmup auf Vollbudget für Ads die warm-up abgeschlossen haben."""
    warmup_h      = int(_s("budget.warmup_hours",    48))
    warmup_eur    = float(_cs("warmup_budget_eur") or _s("budget.warmup_budget_eur", 3.0))
    full_eur      = float(_cs("budget_per_ad_eur")  or _s("budget.budget_per_ad_eur", 5.0))
    full_cents    = int(full_eur * 100)

    for ad in state["active_ads"]:
        if ad.get("budget_eur", warmup_eur) >= full_eur:
            continue  # Bereits auf Vollbudget
        if ad["status"] not in ("freeze", "active"):
            continue

        try:
            created = datetime.fromisoformat(ad["created_at"])
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            age_h = (datetime.now(timezone.utc) - created).total_seconds() / 3600
        except Exception:
            continue

        if age_h < warmup_h:
            continue

        logger.info(
            f"📈 Budget-Upgrade: '{ad['name']}' "
            f"{warmup_eur:.0f}€ → {full_eur:.0f}€/Tag"
        )
        ad["budget_eur"] = full_eur
        if not dry_run:
            adset_id = ad.get("adset_id")
            if adset_id:
                try:
                    meta_request("POST", adset_id, data={"daily_budget": full_cents})
                    audit.log("optimizer", "budget_upgraded", "adset", adset_id,
                              f"{warmup_eur}€→{full_eur}€")
                except Exception as e:
                    logger.error(f"Budget-Upgrade für '{ad['name']}' fehlgeschlagen: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# SCALE-BUDGET UPGRADE (nach scale_after_cycles auf scale_budget_eur)
# ═══════════════════════════════════════════════════════════════════════════════

def _apply_scale_budget(ad: dict, dry_run: bool = False) -> None:
    """Setzt Budget einer Top-Ad auf scale_budget_eur."""
    scale_eur   = float(_cs("budget_per_ad_eur") or _s("budget.scale_budget_eur", 10.0))
    scale_cents = int(scale_eur * 100)
    # Safety: nie mehr als max_scale_multiplier × aktuelles Budget
    max_mult    = float(_s("budget.max_scale_multiplier", 2.0))
    current     = float(ad.get("budget_eur", 5.0))
    allowed_eur = current * max_mult
    target_eur  = min(scale_eur, allowed_eur)
    target_cent = int(target_eur * 100)

    ad["budget_eur"] = target_eur
    if not dry_run:
        adset_id = ad.get("adset_id")
        if adset_id:
            try:
                meta_request("POST", adset_id, data={"daily_budget": target_cent})
                audit.log("optimizer", "budget_scaled", "adset", adset_id,
                          f"{current}€→{target_eur}€")
            except Exception as e:
                logger.error(f"Scale-Budget für '{ad['name']}' fehlgeschlagen: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# 3c. ROTATION
# ═══════════════════════════════════════════════════════════════════════════════

def run_rotation(state: dict, force: bool = False, dry_run: bool = False) -> None:
    """
    Täglich: KPIs abrufen → Fatigue prüfen → Bottom N pausieren → neue nachladen.

    1. KPIs aller aktiven Ads abrufen
    2. Creative-Fatigue und Anomalien prüfen
    3. Freeze-Überprüfung (Zeit- oder Conversion-Trigger)
    4. Auswertbare Ads bewerten (calculate_score)
    5. Bottom BOTTOM_N pausieren
    6. BOTTOM_N neue Ads aus waiting_list nachladen
    7. rotation_count inkrementieren, State speichern
    8. Scale-Alert nach scale_after_cycles
    """
    bottom_n           = int(_s("optimizer.bottom_n",          3))
    scale_after_cycles = int(_s("optimizer.scale_after_cycles", 3))

    evaluable_ads   : list[dict] = []
    scored          : list[dict] = []

    # Schritt 1–3: KPIs + Freeze/Fatigue/Anomalie für alle aktiven Ads
    for ad in state["active_ads"]:
        if ad["status"] == "paused":
            continue

        kpis = fetch_ad_kpis(ad["ad_id"])
        if kpis is None:
            logger.warning(f"Keine KPI-Daten für '{ad['name']}' — übersprungen")
            continue

        # Totals akkumulieren
        ad["impressions_total"] = ad.get("impressions_total", 0) + kpis["impressions"]
        ad["conversions_total"] = ad.get("conversions_total", 0) + kpis["conversions"]

        record_cpm(ad["ad_id"], kpis["cpm"])

        # Anomalie
        if check_anomaly(kpis, ad["ad_id"]) and not dry_run:
            ad["status"] = "paused"
            try:
                meta_request("POST", ad["ad_id"], data={"status": "PAUSED"})
            except Exception as e:
                logger.error(f"Anomalie-Pause für {ad['ad_id']} fehlgeschlagen: {e}")
            audit.log("optimizer", "ad_paused_anomaly", "ad", ad["ad_id"], dry_run=dry_run)
            continue

        # Creative Fatigue
        fatigued, freq = check_creative_fatigue(ad["ad_id"], kpis)
        if fatigued and not dry_run:
            ad["status"] = "paused"
            try:
                meta_request("POST", ad["ad_id"], data={"status": "PAUSED"})
            except Exception as e:
                logger.error(f"Fatigue-Pause für {ad['ad_id']} fehlgeschlagen: {e}")
            audit.log("optimizer", "ad_paused_fatigue", "ad", ad["ad_id"],
                      f"freq={freq:.1f}", dry_run=dry_run)
            continue

        # Freeze → active prüfen
        if ad["status"] == "freeze":
            if is_ready_for_evaluation(ad, kpis) or force:
                ad["status"] = "active"
                trigger = "force" if force else (
                    "conversion" if kpis.get("conversions", 0) >= int(_s("optimizer.conversions_trigger", 50))
                    else "time"
                )
                logger.info(f"🔓 '{ad['name']}' → active ({trigger}-trigger)")
            else:
                continue  # Noch in Freeze

        if ad["status"] == "active":
            evaluable_ads.append({"ad": ad, "kpis": kpis})

    # Schritt 4: Score berechnen
    for item in evaluable_ads:
        score = calculate_score(item["kpis"])
        scored.append({**item, "score": score})
        kpis = item["kpis"]
        logger.info(
            f"   '{item['ad']['name']}': Score={score:.4f} | "
            f"Hook={kpis['hook_rate']:.1%} CTR={kpis['ctr_link']:.2%} "
            f"CPM={kpis['cpm']:.2f}€ Imp={kpis['impressions']} Conv={kpis['conversions']}"
        )

    if len(scored) < bottom_n:
        logger.info(
            f"Nur {len(scored)} auswertbare Ads "
            f"(brauche ≥{bottom_n}) — Rotation übersprungen"
        )
        return

    # Schritt 5: Bottom N pausieren
    scored.sort(key=lambda x: x["score"])
    to_pause = scored[:bottom_n]

    for item in to_pause:
        ad = item["ad"]
        logger.info(
            f"⏸️  Pausiere: '{ad['name']}' "
            f"(Score={item['score']:.4f}, Imp={item['kpis']['impressions']})"
        )
        ad["status"] = "paused"
        if not dry_run:
            try:
                meta_request("POST", ad["ad_id"], data={"status": "PAUSED"})
            except Exception as e:
                # Nie abschalten bei API-Fehler — nur loggen
                logger.error(
                    f"Pause von {ad['ad_id']} in Meta fehlgeschlagen: {e} "
                    f"— State auf 'paused' gesetzt"
                )
        audit.log("optimizer", "ad_paused_rotation", "ad", ad["ad_id"],
                  f"score={item['score']:.4f}", dry_run=dry_run)

    # Schritt 6: Neue Ads nachladen
    refill = state.get("waiting_list", [])[:bottom_n]
    state["waiting_list"] = state.get("waiting_list", [])[bottom_n:]
    if refill:
        logger.info(f"📦 Lade {len(refill)} neue Ad(s) aus Warteliste nach")
        upload_batch(refill, state, dry_run=dry_run)

    # Schritt 7: Zähler + State
    state["rotation_count"] = state.get("rotation_count", 0) + 1
    state["last_rotation"]  = datetime.now(timezone.utc).isoformat()
    logger.info(f"🔄 Rotation #{state['rotation_count']} abgeschlossen")
    audit.log("optimizer", "rotation_completed", details=f"#{state['rotation_count']}")
    save_state(state)

    # Schritt 8: Scale-Alert
    if (
        state["rotation_count"] >= scale_after_cycles
        and not state.get("scale_alert_sent", False)
    ):
        top3 = [
            {"name": i["ad"]["name"], "score": i["score"], "kpis": i["kpis"]}
            for i in sorted(scored, key=lambda x: x["score"], reverse=True)[:3]
        ]
        if not dry_run:
            send_scale_alert(top3, state)
            state["scale_alert_sent"] = True
        else:
            logger.info(f"[DRY-RUN] Würde Scale-Alert senden: {[t['name'] for t in top3]}")


# ═══════════════════════════════════════════════════════════════════════════════
# 4. BUDGET-SCHUTZ
# ═══════════════════════════════════════════════════════════════════════════════

def check_budget_scaling(new_budget_eur: float, yesterday_spend_eur: float) -> float:
    """Limitiert neues Budget auf max. yesterday_spend × max_scale_multiplier."""
    mult    = float(_s("budget.max_scale_multiplier", 2.0))
    allowed = yesterday_spend_eur * mult
    if new_budget_eur > allowed:
        logger.warning(
            f"Budget-Skalierung begrenzt: {new_budget_eur}€ → {allowed:.2f}€ "
            f"(Max {mult}× gestriges Spend {yesterday_spend_eur}€)"
        )
        return allowed
    return new_budget_eur


# ═══════════════════════════════════════════════════════════════════════════════
# 7. MONITORING & ALERTS
# ═══════════════════════════════════════════════════════════════════════════════

def _tg(text: str) -> None:
    """Telegram-Nachricht senden."""
    if not BOT_TOKEN or not CHAT_ID:
        return
    try:
        _http.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception as e:
        logger.error(f"Telegram-Fehler: {e}")


def send_scale_alert(top_ads: list, state: dict) -> None:
    """Telegram-Alert nach scale_after_cycles Zyklen mit Top 3 Ads."""
    scale_eur = float(_s("budget.scale_budget_eur", 10.0))
    lines = [
        f"🏆 *{_s('optimizer.scale_after_cycles', 3)} Rotations-Zyklen abgeschlossen!*\n\n"
        f"*Top 3 Ads:*\n"
    ]
    for i, ad in enumerate(top_ads[:3], 1):
        k = ad.get("kpis", {})
        lines.append(
            f"{i}\\. *{ad['name']}*\n"
            f"   Hook: `{k.get('hook_rate', 0):.1%}` | "
            f"CTR: `{k.get('ctr_link', 0):.2%}` | "
            f"CPM: `{k.get('cpm', 0):.2f}€`\n"
        )
    lines.append(f"\nBereit für Scale auf {scale_eur:.0f}€/Tag?")

    # Scale-Budget auf Top-Ads anwenden
    for top_ad_info in top_ads[:3]:
        for ad in state.get("active_ads", []):
            if ad["name"] == top_ad_info["name"] and ad["status"] == "active":
                _apply_scale_budget(ad)

    _tg("".join(lines).replace(".", "\\.").replace("-", "\\-").replace("!", "\\!"))


def send_daily_report(state: dict) -> None:
    """Tages-Report: aktive Ads, Budget, Top-KPIs, Pipeline-Status."""
    active  = [a for a in state["active_ads"] if a["status"] in ("freeze", "active")]
    frozen  = [a for a in active if a["status"] == "freeze"]
    paused  = [a for a in state["active_ads"] if a["status"] == "paused"]

    status_line = (
        f"🛑 PAUSIERT ({state.get('pause_reason', '?')})"
        if state.get("pipeline_paused")
        else "✅ Aktiv"
    )

    lines = [
        f"📊 *Tages-Report — {datetime.now(timezone.utc).strftime('%d.%m.%Y')}*\n\n",
        f"Pipeline: {status_line}\n",
        f"Aktive Ads: {len(active)} (davon {len(frozen)} in Freeze)\n",
        f"Pausierte Ads: {len(paused)}\n",
        f"Warteliste: {len(state.get('waiting_list', []))}\n",
        f"Rotations: {state.get('rotation_count', 0)}\n",
        f"Tages-Spend: {state.get('daily_spend_eur', 0):.2f}€\n",
    ]

    # Top 3 nach letzten KPIs (nur aus active)
    scored = []
    for ad in [a for a in active if a["status"] == "active"]:
        kpis = fetch_ad_kpis(ad["ad_id"], days=1)
        if kpis and kpis["impressions"] > 0:
            scored.append({"name": ad["name"], "score": calculate_score(kpis), "kpis": kpis})
    scored.sort(key=lambda x: x["score"], reverse=True)

    if scored:
        lines.append("\n*Top 3 heute:*\n")
        for i, s in enumerate(scored[:3], 1):
            k = s["kpis"]
            lines.append(
                f"{i}. {s['name']} — "
                f"Hook {k['hook_rate']:.1%} | CTR {k['ctr_link']:.2%} | "
                f"CPM {k['cpm']:.2f}€\n"
            )

    _tg("".join(lines))
    state["last_report_date"] = datetime.now(timezone.utc).date().isoformat()


def _should_send_report(state: dict) -> bool:
    """True wenn es nach 08:00 UTC ist und heute noch kein Report gesendet wurde."""
    now = datetime.now(timezone.utc)
    if now.hour < 8:
        return False
    last = state.get("last_report_date")
    return last != now.date().isoformat()


# ═══════════════════════════════════════════════════════════════════════════════
# WARTELISTE BEFÜLLEN
# ═══════════════════════════════════════════════════════════════════════════════

def _refresh_waiting_list(state: dict) -> None:
    known: set[str] = {a["filename"] for a in state["active_ads"]}
    known.update(Path(p).name for p in state.get("waiting_list", []))

    new_vids: list[str] = []
    for d in (DOWNLOADS_DIR, APPROVED_DIR):
        if d.exists():
            for f in sorted(d.glob("*.[Mm][Pp]4")):
                if f.name not in known:
                    new_vids.append(str(f))
                    known.add(f.name)

    if new_vids:
        state.setdefault("waiting_list", []).extend(new_vids)
        logger.info(f"📋 {len(new_vids)} Video(s) zur Warteliste hinzugefügt")


# ═══════════════════════════════════════════════════════════════════════════════
# 8. HAUPTFUNKTION
# ═══════════════════════════════════════════════════════════════════════════════

def run_daily(dry_run: bool = False, force_rotation: bool = False) -> None:
    """
    Tägliche Hauptfunktion:
    1. Rejection-Counter zurücksetzen (neuer Tag)
    2. State-Backup
    3. Account-Health prüfen
    4. Spend-Cap prüfen
    5. Pipeline-Pause prüfen (Erinnerung senden, abbrechen)
    6. Warmup-Budget-Upgrades (48h → 5€)
    7. Rotation (wenn nötig)
    8. Neue Ads hochladen (wenn Platz frei)
    9. ROAS prüfen
    10. Tages-Report (08:00 UTC)
    11. State speichern
    """
    prefix = "[DRY-RUN] " if dry_run else ""
    logger.info("=" * 60)
    logger.info(f"{prefix}run_daily — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    error_escalator.reset()

    state = load_state()

    # 1. Rejection-Counter zurücksetzen (täglich)
    reset_rejection_counter(state)

    # 2. State-Backup
    if not dry_run:
        backup_state()

    # 3. Account-Health
    health = check_account_health()
    if not health["healthy"]:
        state["pipeline_paused"] = True
        state["pause_reason"]    = f"account_unhealthy: {health['reason']}"
        save_state(state)
        return

    # 4. Spend-Cap
    if check_daily_spend_cap(state):
        save_state(state)
        return

    # 5. Pipeline-Pause-Check
    if state.get("pipeline_paused"):
        reason = state.get("pause_reason", "unbekannt")
        logger.warning(f"🛑 Pipeline pausiert: {reason} — Erinnerung gesendet")
        send_alert(
            f"Pipeline ist pausiert!\n"
            f"Grund: {reason}\n"
            f"Freigabe: `optimizer.py --unpause`",
            "warning",
        )
        if _should_send_report(state):
            send_daily_report(state)
        save_state(state)
        return

    _refresh_waiting_list(state)

    # 6. Warmup-Budget-Upgrade
    _apply_budget_upgrades(state, dry_run=dry_run)

    # 7. Rotation
    max_active   = int(_cs("max_active") or _s("optimizer.max_active_ads", 15))
    running      = sum(1 for a in state["active_ads"] if a["status"] in ("freeze", "active"))
    last_rot     = state.get("last_rotation")
    rot_due      = True  # Täglich rotieren wenn auswertbare Ads vorhanden
    if last_rot:
        try:
            last_dt = datetime.fromisoformat(last_rot)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            rot_due = (datetime.now(timezone.utc) - last_dt).total_seconds() >= 82800  # ~23h
        except Exception:
            pass

    if rot_due or force_rotation:
        run_rotation(state, force=force_rotation, dry_run=dry_run)
        running = sum(1 for a in state["active_ads"] if a["status"] in ("freeze", "active"))

    # 8. Neue Ads hochladen
    slots_free = max_active - running
    if slots_free > 0 and state.get("waiting_list"):
        batch_size  = int(_s("optimizer.batch_size", 5))
        n_upload    = min(batch_size, slots_free)
        to_upload   = state["waiting_list"][:n_upload]
        state["waiting_list"] = state["waiting_list"][n_upload:]
        logger.info(f"{prefix}Lade {n_upload} neue Ad(s) hoch ({running}/{max_active} aktiv)")
        created = upload_batch(to_upload, state, dry_run=dry_run)
        logger.info(f"{prefix}{len(created)} Ad(s) erstellt")
    elif slots_free <= 0:
        logger.info(f"Limit erreicht ({max_active} Ads aktiv) — kein Upload")
    else:
        logger.info("Warteliste leer — kein Upload")

    # 9. ROAS prüfen
    if check_roas(state.get("roas_history", []), state):
        save_state(state)
        return

    # 10. Tages-Report
    if _should_send_report(state) and not dry_run:
        send_daily_report(state)

    save_state(state)
    logger.info(f"{prefix}run_daily abgeschlossen")


# ═══════════════════════════════════════════════════════════════════════════════
# 9. CLI-INTERFACE
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Meta Ad Optimizer — Terra Preta Wein"
    )
    parser.add_argument("--dry-run",        action="store_true",
                        help="Zeigt geplante Aktionen ohne Ausführung")
    parser.add_argument("--status",         action="store_true",
                        help="State als Tabelle ausgeben und beenden")
    parser.add_argument("--force-rotation", action="store_true",
                        help="Rotation erzwingen (ignoriert Freeze-Check)")
    parser.add_argument("--unpause",        action="store_true",
                        help="pipeline_paused auf False setzen")
    parser.add_argument("--add-to-waitlist", nargs="+", metavar="FILEPATH",
                        help="Video(s) zur Warteliste hinzufügen")
    args = parser.parse_args()

    if args.status:
        s       = load_state()
        active  = [a for a in s["active_ads"] if a["status"] in ("freeze", "active")]
        frozen  = [a for a in active if a["status"] == "freeze"]
        paused  = [a for a in s["active_ads"] if a["status"] == "paused"]
        print(f"\n{'='*50}")
        print(f"  Meta Ad Optimizer — Status")
        print(f"{'='*50}")
        print(f"  Pipeline:      {'🛑 PAUSIERT (' + s.get('pause_reason','?') + ')' if s.get('pipeline_paused') else '✅ Aktiv'}")
        print(f"  Aktiv/Freeze:  {len(active)} ({len(frozen)} in Freeze)")
        print(f"  Pausiert:      {len(paused)}")
        print(f"  Warteliste:    {len(s.get('waiting_list', []))}")
        print(f"  Rotations:     {s.get('rotation_count', 0)}")
        print(f"  Letzter Lauf:  {s.get('last_rotation', '—')}")
        print(f"  Tages-Spend:   {s.get('daily_spend_eur', 0):.2f}€")
        print(f"{'='*50}\n")
        if active:
            print(f"  {'Name':<30} {'Status':<8} {'Budget':<8} {'Conv':<6}")
            print(f"  {'-'*56}")
            for a in s["active_ads"]:
                print(
                    f"  {a['name'][:30]:<30} {a['status']:<8} "
                    f"{a.get('budget_eur', '?'):<8} {a.get('conversions_total', 0):<6}"
                )
        print()

    elif args.unpause:
        s = load_state()
        s["pipeline_paused"] = False
        s["pause_reason"]    = None
        save_state(s)
        audit.log("cli", "pipeline_unpaused")
        print("✅ Pipeline freigegeben")

    elif args.add_to_waitlist:
        s = load_state()
        added = 0
        for fp in args.add_to_waitlist:
            p = Path(fp)
            if p.exists():
                if str(p) not in s["waiting_list"]:
                    s["waiting_list"].append(str(p))
                    added += 1
                    audit.log("cli", "added_to_waitlist", "video", p.name)
            else:
                print(f"⚠️  Nicht gefunden: {fp}")
        save_state(s)
        print(f"✅ {added} Video(s) zur Warteliste hinzugefügt")

    else:
        run_daily(dry_run=args.dry_run, force_rotation=args.force_rotation)
