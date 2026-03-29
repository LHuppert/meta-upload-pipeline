"""
Ad Optimizer — optimizer.py
Batch-Upload + 7-Tage-Freeze Testing-Strategie für Meta Ads.

Täglich via Cron aufrufen: python optimizer.py
Dry-run:                   python optimizer.py --dry-run
Status anzeigen:           python optimizer.py --status
"""

import os
import json
import logging
import argparse
import requests
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

from meta_uploader import (
    meta_request,
    upload_video,
    wait_for_video_ready,
    get_excluded_geo_locations,
    AD_ACCOUNT_ID,
    PAGE_ID,
)
from settings_manager import settings, BASE_DIR

# ── Logging ───────────────────────────────────────────────────────────────────
log_dir = BASE_DIR / "logs"
log_dir.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger(__name__)
if not logger.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        handlers=[
            logging.FileHandler(log_dir / "optimizer.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID    = os.getenv("TELEGRAM_CHAT_ID", "")
SHOP_LINK  = os.getenv("SHOP_LINK", "https://terrapretawein.de")
PIXEL_ID   = os.getenv("META_PIXEL_ID", "")        # Meta Pixel ID für Conversion-Tracking

FREEZE_DAYS            = 7
MAX_ACTIVE             = 15
BATCH_SIZE             = 5
BUDGET_PER_AD_CENTS    = 500   # 5 € in Euro-Cents (Meta erwartet kleinste Währungseinheit)
BOTTOM_N               = 3
SCALE_AFTER_ROTATIONS  = 3
MIN_IMPRESSIONS        = 500

HOOK_RATE_WEIGHT    = 0.40
CTR_WEIGHT          = 0.40
CPM_WEIGHT          = 0.20
HOOK_RATE_MIN       = 0.25   # 25 %
CTR_MIN             = 0.01   # 1 %
CPM_MALUS_THRESHOLD = 18.0   # €

AD_STATE_FILE = BASE_DIR / "ad_state.json"
DOWNLOADS_DIR = BASE_DIR / "downloads"
APPROVED_DIR  = BASE_DIR / "approved"

_DEFAULT_STATE: dict = {
    "active_ads":       [],
    "waiting_list":     [],
    "rotation_count":   0,
    "last_rotation":    None,
    "scale_alert_sent": False,
}


# ── State I/O ─────────────────────────────────────────────────────────────────

def load_state() -> dict:
    """Liest ad_state.json. Erstellt Datei mit Defaults falls nicht vorhanden."""
    if AD_STATE_FILE.exists():
        try:
            raw   = json.loads(AD_STATE_FILE.read_text(encoding="utf-8"))
            state = deepcopy(_DEFAULT_STATE)
            state.update(raw)
            return state
        except Exception as e:
            logger.error(f"ad_state.json unlesbar: {e} — starte mit leeren Defaults")
    return deepcopy(_DEFAULT_STATE)


def save_state(state: dict) -> None:
    """Schreibt State atomar via Temp-Datei in ad_state.json."""
    AD_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = AD_STATE_FILE.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(state, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    tmp.replace(AD_STATE_FILE)


# ── Freeze-Check ──────────────────────────────────────────────────────────────

def check_freeze(ad: dict) -> bool:
    """
    Gibt True zurück wenn die Freeze-Periode vorbei ist.
    Bedingung: created_at + FREEZE_DAYS ≤ jetzt (UTC).
    """
    try:
        created = datetime.fromisoformat(ad["created_at"])
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) >= created + timedelta(days=FREEZE_DAYS)
    except (KeyError, ValueError) as e:
        logger.warning(f"Freeze-Check für Ad '{ad.get('ad_id', '?')}' fehlgeschlagen: {e}")
        return False


CONVERSION_EARLY_EXIT = 50   # Anzahl Conversions für frühzeitigen Freeze-Ausstieg


def is_ready_for_evaluation(ad: dict, kpis: dict) -> bool:
    """
    Ad ist auswertbar wenn EINE der Bedingungen erfüllt ist:

    1. Zeit-Trigger  : 7 Tage seit created_at vergangen (check_freeze)
    2. Conversion-Trigger : >= 50 Purchase-Conversions erreicht → früher Ausstieg

    Die 500-Impressionen-Mindestgrenze bleibt als Sicherheitsnetz bestehen
    und wird separat geprüft — verhindert Auswertung bei statistisch
    bedeutungslosen Daten (z.B. 50 Conversions bei 200 Impressionen).
    """
    freeze_over         = check_freeze(ad)
    enough_conversions  = kpis.get("conversions", 0) >= CONVERSION_EARLY_EXIT
    return freeze_over or enough_conversions


# ── KPI-Abruf ─────────────────────────────────────────────────────────────────

def fetch_ad_insights(ad_id: str, days: int = 7) -> dict | None:
    """
    Ruft KPI-Daten einer Ad über die Meta Insights API ab.
    Zeitraum: letzte N Tage ab heute (UTC).
    Gibt None zurück bei API-Fehler oder fehlenden Daten.
    """
    today = datetime.now(timezone.utc).date()
    since = (today - timedelta(days=days)).isoformat()
    until = today.isoformat()

    try:
        data = meta_request(
            "GET",
            f"{ad_id}/insights",
            params={
                "fields": (
                    "impressions,"
                    "inline_link_clicks,"
                    "video_3_sec_watched_actions,"
                    "actions,"
                    "cpm,"
                    "spend"
                ),
                "time_range": json.dumps({"since": since, "until": until}),
                "level": "ad",
            },
        )
    except Exception as e:
        logger.error(f"Insights-Abruf für Ad {ad_id} fehlgeschlagen: {e}")
        return None

    rows = data.get("data", [])
    if not rows:
        logger.info(f"Keine Insight-Daten für Ad {ad_id} im Zeitraum {since}–{until}")
        return None

    row         = rows[0]
    impressions = int(row.get("impressions", 0))
    clicks      = int(row.get("inline_link_clicks", 0))
    cpm         = float(row.get("cpm", 0.0))

    # 3-Sekunden-Views aus dem Actions-Array extrahieren
    views_3s = 0
    for action in row.get("video_3_sec_watched_actions", []):
        views_3s += int(action.get("value", 0))

    # Purchase-Conversions aus dem actions-Array extrahieren
    conversions = 0
    for action in row.get("actions", []):
        if action.get("action_type") == "offsite_conversion.fb_pixel_purchase":
            conversions += int(action.get("value", 0))

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
    }


# ── Score-Berechnung ──────────────────────────────────────────────────────────

def calculate_score(kpis: dict) -> float:
    """
    Berechnet einen KPI-Score zwischen 0.0 und 1.0 (höher = besser).

    Hook Rate (3s-Views / Impressionen)  — 40 % Gewichtung, Ziel >= 25 %
    CTR Link (Klicks / Impressionen)     — 40 % Gewichtung, Ziel >= 1 %
    CPM                                  — 20 % Gewichtung, Malus wenn > 18 €

    Normalisierung: 1.0 bei 2× Zielwert (linear skaliert, gecappt bei 2×).
    CPM: 0 € → 1.0, 18 € → 0.5, 36 € → 0.0 (linear invertiert).
    """
    hook_rate = float(kpis.get("hook_rate", 0.0))
    ctr       = float(kpis.get("ctr_link",  0.0))
    cpm       = float(kpis.get("cpm",       0.0))

    hook_norm = min(hook_rate / HOOK_RATE_MIN, 2.0) / 2.0
    ctr_norm  = min(ctr / CTR_MIN,             2.0) / 2.0
    cpm_norm  = max(0.0, 1.0 - cpm / (CPM_MALUS_THRESHOLD * 2))

    score = (
        hook_norm * HOOK_RATE_WEIGHT +
        ctr_norm  * CTR_WEIGHT       +
        cpm_norm  * CPM_WEIGHT
    )
    return round(score, 4)


# ── Meta Ad erstellen ─────────────────────────────────────────────────────────

def _get_active_campaign_id() -> str:
    """Liest die Meta Kampagnen-ID der aktiven Kampagne aus den Settings."""
    active_id = settings.get("campaigns.active", "testing_inhouse")
    for camp in settings.get("campaigns.list", []):
        if camp["id"] == active_id:
            return camp.get("meta_campaign_id", "")
    return ""


def _create_meta_ad(video_id: str, ad_name: str, dry_run: bool = False) -> str:
    """
    Erstellt einen vollständigen Meta Ad:
      1. AdCreative (mit Video + CTA)
      2. AdSet (5 € Tagesbudget, Targeting DE + optionaler Geo-Ausschluss)
      3. Ad (verknüpft Creative mit AdSet)

    Gibt die Ad-ID zurück. Wirft RuntimeError bei Fehler.
    """
    campaign_id = _get_active_campaign_id()
    if not campaign_id:
        raise RuntimeError(
            "Keine Meta Kampagnen-ID konfiguriert. "
            "Bitte im Dashboard → Kampagnen-Einstellungen eintragen."
        )

    if dry_run:
        fake_id = f"dry_{video_id[:8]}"
        logger.info(f"[DRY-RUN] Würde Ad '{ad_name}' (Video {video_id}) erstellen → {fake_id}")
        return fake_id

    # 1. AdCreative
    creative_resp = meta_request(
        "POST",
        f"{AD_ACCOUNT_ID}/adcreatives",
        data={
            "name": f"Creative — {ad_name}",
            "object_story_spec": json.dumps({
                "page_id": PAGE_ID,
                "video_data": {
                    "video_id":       video_id,
                    "call_to_action": {
                        "type":  "SHOP_NOW",
                        "value": {"link": SHOP_LINK},
                    },
                },
            }),
        },
    )
    creative_id = creative_resp.get("id")
    if not creative_id:
        raise RuntimeError(f"AdCreative nicht erstellt: {creative_resp}")
    logger.info(f"   AdCreative: {creative_id}")

    # 2. Targeting (DE + optionaler Geo-Ausschluss aus Settings)
    targeting: dict = {"geo_locations": {"countries": ["DE"]}}
    excluded = get_excluded_geo_locations()
    if excluded:
        targeting["excluded_geo_locations"] = excluded

    # Conversion-Ziel: OFFSITE_CONVERSIONS + PURCHASE damit Meta so schnell wie
    # möglich 50 Conversion-Events sammelt und die Lernphase verlässt.
    # LOWEST_COST_WITHOUT_CAP = kein Bid-Cap → Meta kann Budget frei einsetzen
    # um Conversions zu maximieren.
    # Falls kein Pixel konfiguriert → Fallback auf LINK_CLICKS.
    if PIXEL_ID:
        optimization_goal = "OFFSITE_CONVERSIONS"
        promoted_object   = json.dumps({
            "pixel_id":          PIXEL_ID,
            "custom_event_type": "PURCHASE",
        })
    else:
        optimization_goal = "LINK_CLICKS"
        promoted_object   = None
        logger.warning(
            "META_PIXEL_ID nicht gesetzt — Fallback auf LINK_CLICKS. "
            "Pixel-ID im Dashboard oder .env eintragen für Conversion-Optimierung."
        )

    adset_data: dict = {
        "name":              f"AdSet — {ad_name}",
        "campaign_id":       campaign_id,
        "daily_budget":      BUDGET_PER_AD_CENTS,
        "billing_event":     "IMPRESSIONS",
        "optimization_goal": optimization_goal,
        "bid_strategy":      "LOWEST_COST_WITHOUT_CAP",
        "targeting":         json.dumps(targeting),
        "status":            "ACTIVE",
    }
    if promoted_object:
        adset_data["promoted_object"] = promoted_object

    adset_resp = meta_request(
        "POST",
        f"{AD_ACCOUNT_ID}/adsets",
        data=adset_data,
    )
    adset_id = adset_resp.get("id")
    if not adset_id:
        raise RuntimeError(f"AdSet nicht erstellt: {adset_resp}")
    logger.info(f"   AdSet: {adset_id}")

    # 3. Ad
    ad_resp = meta_request(
        "POST",
        f"{AD_ACCOUNT_ID}/ads",
        data={
            "name":     ad_name,
            "adset_id": adset_id,
            "creative": json.dumps({"creative_id": creative_id}),
            "status":   "ACTIVE",
        },
    )
    ad_id = ad_resp.get("id")
    if not ad_id:
        raise RuntimeError(f"Ad nicht erstellt: {ad_resp}")

    logger.info(f"   ✅ Ad erstellt: {ad_id}")
    return ad_id


# ── Batch-Upload ──────────────────────────────────────────────────────────────

def upload_batch(ads: list, batch_num: int, state: dict, dry_run: bool = False) -> list:
    """
    Lädt bis zu BATCH_SIZE Ads hoch und erstellt die entsprechenden Meta Ads.

    ads       — Liste von Video-Dateipfaden (str oder Path)
    batch_num — Batch-Nummer für State-Tracking
    state     — wird direkt mutiert (active_ads wird befüllt)

    Gibt Liste der erstellten Ad-Dicts zurück.
    Fehler bei einzelnen Ads werden geloggt und übersprungen (kein Abbruch).
    """
    created = []

    for video_path in ads[:BATCH_SIZE]:
        video_path = Path(video_path)
        if not video_path.exists():
            logger.warning(f"Video nicht gefunden: {video_path} — übersprungen")
            continue

        ad_name = video_path.stem
        logger.info(f"📤 Lade hoch: {video_path.name} (Batch {batch_num})")

        try:
            if dry_run:
                video_id = f"dry_vid_{video_path.stem[:10]}"
                logger.info(f"[DRY-RUN] Überspringe Video-Upload für {video_path.name}")
            else:
                video_id = upload_video(video_path)
                wait_for_video_ready(video_id)

            ad_id = _create_meta_ad(video_id, ad_name, dry_run=dry_run)

            ad_entry = {
                "ad_id":      ad_id,
                "name":       ad_name,
                "filename":   video_path.name,
                "video_id":   video_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "status":     "freeze",
                "batch":      batch_num,
            }
            state["active_ads"].append(ad_entry)
            created.append(ad_entry)

            unfreeze = (datetime.now(timezone.utc) + timedelta(days=FREEZE_DAYS)).date()
            logger.info(
                f"✅ '{ad_name}' → Ad {ad_id} | "
                f"Freeze bis {unfreeze} | Batch {batch_num}"
            )

        except Exception as e:
            logger.error(f"❌ Upload fehlgeschlagen für {video_path.name}: {e}")

    return created


# ── Rotation ──────────────────────────────────────────────────────────────────

def run_rotation(state: dict, dry_run: bool = False) -> dict:
    """
    Täglich ausführen:
    1. Ads deren Freeze vorbei ist auf 'active' setzen
    2. Auswertbare Ads (active, >= MIN_IMPRESSIONS) bewerten
    3. Bottom BOTTOM_N pausieren (in Meta + State)
    4. BOTTOM_N neue Ads aus waiting_list nachladen
    5. rotation_count erhöhen

    Gibt aktualisierten State zurück.
    Bei API-Fehler: Ad NICHT abschalten, nur loggen und überspringen.
    """
    # Schritt 1: KPIs für alle Freeze-Ads abrufen und Ausstieg prüfen
    # Trigger: (a) 7 Tage vorbei  ODER  (b) >= 50 Conversions (früher Ausstieg)
    newly_unfrozen = []
    for ad in state["active_ads"]:
        if ad["status"] != "freeze":
            continue
        # Erst Zeit-Trigger (billig, kein API-Call)
        if check_freeze(ad):
            ad["status"] = "active"
            newly_unfrozen.append(f"{ad['name']} (Zeit-Trigger)")
            continue
        # Conversion-Trigger: KPIs holen und auf >= 50 Conversions prüfen
        kpis_check = fetch_ad_insights(ad["ad_id"])
        if kpis_check and is_ready_for_evaluation(ad, kpis_check):
            ad["status"] = "active"
            conv = kpis_check.get("conversions", 0)
            newly_unfrozen.append(f"{ad['name']} (Conversion-Trigger: {conv} Conversions)")

    if newly_unfrozen:
        logger.info(f"🔓 Aus Freeze entlassen ({len(newly_unfrozen)}): {newly_unfrozen}")

    # Schritt 2: Auswertbare Ads bestimmen
    evaluable = [a for a in state["active_ads"] if a["status"] == "active"]
    if not evaluable:
        logger.info("Keine auswertbaren Ads vorhanden — Rotation übersprungen")
        return state

    # Schritt 3: KPIs abrufen + Score berechnen
    scored = []
    for ad in evaluable:
        kpis = fetch_ad_insights(ad["ad_id"])
        if kpis is None:
            logger.warning(f"Keine KPI-Daten für '{ad['name']}' — übersprungen")
            continue
        # 500-Impressionen-Mindestgrenze als Sicherheitsnetz
        if kpis["impressions"] < MIN_IMPRESSIONS:
            logger.info(
                f"'{ad['name']}': {kpis['impressions']} Impressionen "
                f"(< {MIN_IMPRESSIONS} Minimum) — noch nicht genug Daten"
            )
            continue

        score = calculate_score(kpis)
        scored.append({"ad": ad, "score": score, "kpis": kpis})
        logger.info(
            f"   '{ad['name']}': Score={score:.4f} | "
            f"Hook={kpis['hook_rate']:.1%} | CTR={kpis['ctr_link']:.2%} | "
            f"CPM={kpis['cpm']:.2f}€ | Imp={kpis['impressions']} | "
            f"Conv={kpis['conversions']}"
        )

    if len(scored) < BOTTOM_N:
        logger.info(
            f"Nur {len(scored)} Ads mit ausreichend Daten "
            f"(brauche ≥{BOTTOM_N} für Rotation) — übersprungen"
        )
        return state

    # Schritt 4: Bottom N pausieren
    scored.sort(key=lambda x: x["score"])
    to_pause = scored[:BOTTOM_N]

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
                # Nicht abschalten bei Fehler — nur loggen
                logger.error(
                    f"Fehler beim Pausieren von Ad {ad['ad_id']} in Meta: {e} "
                    f"— Status im State auf 'paused' gesetzt, Meta-Status unverändert"
                )
        else:
            logger.info(f"[DRY-RUN] Würde Ad {ad['ad_id']} in Meta pausieren")

    # Schritt 5: Neue Ads aus Warteliste nachladen
    slots   = len(to_pause)
    refill  = state.get("waiting_list", [])[:slots]
    state["waiting_list"] = state.get("waiting_list", [])[slots:]

    if refill:
        next_batch = max((a["batch"] for a in state["active_ads"]), default=0) + 1
        logger.info(f"📦 Lade {len(refill)} neue Ad(s) nach (Batch {next_batch})")
        upload_batch(refill, next_batch, state, dry_run=dry_run)
    else:
        logger.info("Warteliste leer — keine neuen Ads nachladen")

    # Schritt 6: Counter
    state["rotation_count"] = state.get("rotation_count", 0) + 1
    state["last_rotation"]  = datetime.now(timezone.utc).isoformat()
    logger.info(f"🔄 Rotation #{state['rotation_count']} abgeschlossen")

    return state


# ── Skalierungs-Alert ─────────────────────────────────────────────────────────

def send_scale_alert(top_ads: list) -> None:
    """
    Sendet Telegram-Nachricht nach SCALE_AFTER_ROTATIONS abgeschlossenen Zyklen.
    top_ads — Liste von dicts mit 'name', 'score', 'kpis'
    """
    if not BOT_TOKEN or not CHAT_ID:
        logger.warning("Telegram nicht konfiguriert — Scale-Alert nicht gesendet")
        return

    lines = [f"🏆 *{SCALE_AFTER_ROTATIONS} Zyklen abgeschlossen — Bereit für Scale?*\n\n"]
    lines.append("*Top 3 Ads:*\n")
    for i, item in enumerate(top_ads[:3], start=1):
        kpis = item.get("kpis", {})
        lines.append(
            f"{i}\\. *{item['name']}*\n"
            f"   Score: `{item['score']:.2f}` | "
            f"Hook: `{kpis.get('hook_rate', 0):.1%}` | "
            f"CTR: `{kpis.get('ctr_link', 0):.2%}` | "
            f"CPM: `{kpis.get('cpm', 0):.2f}€`\n"
        )
    lines.append("\nBereit für Scale? Budgets manuell im Meta Ads Manager erhöhen.")

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={
                "chat_id":    CHAT_ID,
                "text":       "".join(lines),
                "parse_mode": "MarkdownV2",
            },
            timeout=15,
        )
        if resp.ok:
            logger.info("📨 Scale-Alert an Telegram gesendet")
        else:
            logger.error(f"Telegram Scale-Alert fehlgeschlagen: {resp.status_code} {resp.text}")
    except Exception as e:
        logger.error(f"Scale-Alert konnte nicht gesendet werden: {e}")


# ── Warteliste befüllen ───────────────────────────────────────────────────────

def _refresh_waiting_list(state: dict) -> None:
    """
    Scannt downloads/ und approved/ nach neuen .mp4-Dateien.
    Bereits hochgeladene oder bereits gelistete Dateien werden übersprungen.
    """
    known: set[str] = {a["filename"] for a in state["active_ads"]}
    known.update(Path(p).name for p in state.get("waiting_list", []))

    new_videos: list[str] = []
    for directory in (DOWNLOADS_DIR, APPROVED_DIR):
        if not directory.exists():
            continue
        for f in sorted(directory.glob("*.[Mm][Pp]4")):
            if f.name not in known:
                new_videos.append(str(f))
                known.add(f.name)

    if new_videos:
        state.setdefault("waiting_list", []).extend(new_videos)
        logger.info(f"📋 {len(new_videos)} neue Video(s) zur Warteliste hinzugefügt")


# ── Hauptfunktion ─────────────────────────────────────────────────────────────

def run_daily(dry_run: bool = False) -> None:
    """
    Hauptfunktion — täglich via Cron aufrufen.

    Ablauf:
    1. State laden
    2. Warteliste mit neuen Videos aus downloads/ und approved/ befüllen
    3. Batch-Upload wenn aktive Ads < MAX_ACTIVE und Warteliste nicht leer
    4. Rotation ausführen (Freeze-Check → KPI-Auswertung → Bottom pausieren → neue nachladen)
    5. Scale-Alert falls SCALE_AFTER_ROTATIONS erreicht
    6. State speichern
    """
    prefix = "[DRY-RUN] " if dry_run else ""
    logger.info("=" * 60)
    logger.info(
        f"{prefix}run_daily() — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    )

    state = load_state()
    _refresh_waiting_list(state)

    running = sum(1 for a in state["active_ads"] if a["status"] in ("freeze", "active"))
    logger.info(
        f"Aktive Ads: {running}/{MAX_ACTIVE} | "
        f"Warteliste: {len(state.get('waiting_list', []))} | "
        f"Rotations: {state.get('rotation_count', 0)}"
    )

    # Batch-Upload wenn Platz frei
    slots_free = MAX_ACTIVE - running
    if slots_free > 0 and state.get("waiting_list"):
        n_upload            = min(BATCH_SIZE, slots_free)
        to_upload           = state["waiting_list"][:n_upload]
        state["waiting_list"] = state["waiting_list"][n_upload:]
        next_batch          = max((a["batch"] for a in state["active_ads"]), default=0) + 1

        logger.info(f"{prefix}Starte Batch {next_batch} mit {len(to_upload)} Video(s)")
        created = upload_batch(to_upload, next_batch, state, dry_run=dry_run)
        logger.info(f"{prefix}Batch {next_batch}: {len(created)} Ad(s) erstellt")
    elif slots_free <= 0:
        logger.info(f"Limit erreicht ({MAX_ACTIVE} Ads aktiv) — kein Upload heute")
    else:
        logger.info("Warteliste leer — kein Upload heute")

    # Rotation
    state = run_rotation(state, dry_run=dry_run)

    # Scale-Alert nach N Zyklen (einmalig)
    if (
        state["rotation_count"] >= SCALE_AFTER_ROTATIONS
        and not state.get("scale_alert_sent", False)
    ):
        evaluable = [a for a in state["active_ads"] if a["status"] == "active"]
        top_scored: list[dict] = []
        for ad in evaluable:
            kpis = fetch_ad_insights(ad["ad_id"])
            if kpis and kpis["impressions"] >= MIN_IMPRESSIONS:
                top_scored.append({
                    "name":  ad["name"],
                    "score": calculate_score(kpis),
                    "kpis":  kpis,
                })
        top_scored.sort(key=lambda x: x["score"], reverse=True)

        if not dry_run:
            send_scale_alert(top_scored)
            state["scale_alert_sent"] = True
        else:
            names = [t["name"] for t in top_scored[:3]]
            logger.info(f"[DRY-RUN] Würde Scale-Alert senden — Top 3: {names}")

    save_state(state)
    logger.info(f"{prefix}run_daily() abgeschlossen — State gespeichert")


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Meta Ad Optimizer — Batch-Upload + 7-Tage-Freeze Testing"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Zeigt was passieren würde, ohne etwas in Meta oder State zu ändern",
    )
    parser.add_argument(
        "--status", action="store_true",
        help="Gibt den aktuellen ad_state.json aus und beendet",
    )
    args = parser.parse_args()

    if args.status:
        state = load_state()
        active  = [a for a in state["active_ads"] if a["status"] in ("freeze", "active")]
        paused  = [a for a in state["active_ads"] if a["status"] == "paused"]
        waiting = state.get("waiting_list", [])
        print(f"\n📊 Optimizer Status")
        print(f"   Aktiv/Freeze : {len(active)}/{MAX_ACTIVE}")
        print(f"   Pausiert     : {len(paused)}")
        print(f"   Warteliste   : {len(waiting)}")
        print(f"   Rotations    : {state.get('rotation_count', 0)}")
        print(f"   Letzter Lauf : {state.get('last_rotation', '—')}\n")
        print(json.dumps(state, indent=2, ensure_ascii=False, default=str))
    else:
        run_daily(dry_run=args.dry_run)
