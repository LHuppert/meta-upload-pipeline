"""
Telegram Bot — Reports & Ad-Texte

Commands:
/start /hilfe       — Befehlsübersicht
/status             — Aktueller Stand
/health             — Account Health
/report             — Sofort-Bericht senden
/pause [Grund]      — Bot pausieren
/fortsetzen         — Pause aufheben
/einstellungen      — Aktuelle Einstellungen anzeigen
/set KEY WERT       — Einstellung ändern
/dashboard          — Dashboard-URL anzeigen
/videos             — Alle Google Drive Videos anzeigen
/texte [Nr]         — Ad-Texte für Video generieren
"""

import os
import asyncio
import logging
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from settings_manager import settings, BASE_DIR
import safety_monitor

load_dotenv()

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

# ─── Config ───────────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = int(os.getenv("TELEGRAM_CHAT_ID", "0"))

OUTPUT_DIR = BASE_DIR / os.getenv("OUTPUT_DIR", "output")

DASHBOARD_URL = os.getenv("DASHBOARD_URL", "http://localhost:5000")


# ─── Hilfsfunktionen ─────────────────────────────────────────────────────────

async def send_safe(context, text: str, **kwargs):
    try:
        return await context.bot.send_message(chat_id=CHAT_ID, text=text, **kwargs)
    except Exception as e:
        logger.error(f"Telegram-Fehler: {e}")


def get_output_videos() -> list:
    OUTPUT_DIR.mkdir(exist_ok=True)
    return sorted(OUTPUT_DIR.glob("*.mp4")) + sorted(OUTPUT_DIR.glob("*.MP4"))


def build_status_text() -> str:
    health = safety_monitor.get_status_emoji()
    paused = settings.is_paused()
    text = f"📊 *Status — Weingut Huppert*\n\n"
    text += f"Account Health: {health}\n"
    if paused:
        reason = settings.get("safety.pause_reason", "")
        text += f"\n🛑 *PAUSE AKTIV*\n{reason}"
    return text


# ─── Command Handler ──────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🍷 *Meta Bot — Weingut Huppert*\n\n"
        "*Ad-Texte:*\n"
        "/videos — Alle Google Drive Videos anzeigen\n"
        "/texte [Nr] — Ad-Texte für Video generieren\n\n"
        "*Info & Reports:*\n"
        "/kpi — Heutige KPIs (Spend, CTR, aktive Ads)\n"
        "/status — Aktueller Stand\n"
        "/health — Account Health\n"
        "/report — Tagesbericht mit Claude-Analyse\n\n"
        "*Steuerung:*\n"
        "/pause [Grund] — Bot pausieren\n"
        "/fortsetzen — Pause aufheben\n"
        "/einstellungen — Einstellungen anzeigen\n"
        "/set KEY WERT — Einstellung ändern\n"
        "/dashboard — Dashboard-Link\n",
        parse_mode="Markdown"
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    videos_ready = len(get_output_videos())
    text = build_status_text()
    text += f"\n\nVideos im output/ Ordner: {videos_ready}"
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    summary = safety_monitor.get_summary()
    health  = safety_monitor.get_health()
    text = f"*Account Health*\n\n{summary}"
    if health.get("auto_paused_at"):
        text += f"\n\n🛑 Auto-Pause aktiv seit: {health['auto_paused_at'][:16]}"
        text += f"\nNutze /fortsetzen wenn das Problem behoben ist."
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_daily_report(context)


async def cmd_kpi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Zeigt heutige Meta Ads KPIs: aktive Anzeigen, Spend, Impressionen, CTR, CPM."""
    token   = os.getenv("META_ACCESS_TOKEN", "")
    account = os.getenv("META_AD_ACCOUNT_ID", "").lstrip("act_")
    if not token or not account:
        await update.message.reply_text("❌ META_ACCESS_TOKEN oder META_AD_ACCOUNT_ID fehlt in .env")
        return

    try:
        import requests as _req

        # Heutige Account-Insights
        r_insights = _req.get(
            f"https://graph.facebook.com/v18.0/act_{account}/insights",
            params={
                "fields":       "impressions,clicks,ctr,cpm,spend,actions",
                "date_preset":  "today",
                "level":        "account",
                "access_token": token,
            },
            timeout=15,
        )
        ins = r_insights.json().get("data", [{}])[0] if r_insights.json().get("data") else {}

        spend       = float(ins.get("spend", 0))
        impressions = int(ins.get("impressions", 0))
        clicks      = int(ins.get("clicks", 0))
        ctr         = float(ins.get("ctr", 0)) * 100
        cpm         = float(ins.get("cpm", 0))
        purchases   = sum(int(a.get("value", 0)) for a in ins.get("actions", []) if a.get("action_type") == "purchase")
        cpc         = (spend / clicks) if clicks > 0 else 0

        # Aktive Anzeigen zählen
        r_ads = _req.get(
            f"https://graph.facebook.com/v18.0/act_{account}/ads",
            params={
                "fields":        "id",
                "effective_status": '["ACTIVE"]',
                "limit":         200,
                "access_token":  token,
            },
            timeout=15,
        )
        active_ads = len(r_ads.json().get("data", []))

        text = (
            f"📊 *Meta Ads — Heute*\n\n"
            f"🟢 Aktive Anzeigen: *{active_ads}*\n\n"
            f"💰 Spend: *EUR {spend:.2f}*\n"
            f"👁 Impressionen: *{impressions:,}*\n"
            f"🖱 Klicks: *{clicks:,}*\n"
            f"📈 CTR: *{ctr:.2f}%*\n"
            f"💶 CPM: *EUR {cpm:.2f}*\n"
            f"💵 CPC: *EUR {cpc:.2f}*\n"
            f"🛒 Käufe: *{purchases}*\n"
        )
        if spend == 0 and impressions == 0:
            text += "\n_Noch keine Daten für heute — zu früh oder keine aktiven Kampagnen._"

    except Exception as e:
        text = f"❌ Fehler beim KPI-Abruf: {e}"

    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reason = " ".join(context.args) if context.args else "Manuell pausiert via Telegram"
    settings.enable_pause(reason, by="telegram")
    await update.message.reply_text(
        f"🛑 *Pause aktiviert*\n\nGrund: {reason}\n\nNutze /fortsetzen um wieder zu starten.",
        parse_mode="Markdown"
    )


async def cmd_fortsetzen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not settings.is_paused():
        await update.message.reply_text("▶️ Bot ist nicht pausiert.")
        return
    settings.disable_pause(by="telegram")
    await update.message.reply_text(
        "▶️ *Pause aufgehoben*",
        parse_mode="Markdown"
    )


async def cmd_einstellungen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sf = settings.get("safety", {})
    sc = settings.get("schedule", {})
    text = (
        f"⚙️ *Aktuelle Einstellungen*\n\n"
        f"*Safety:*\n"
        f"Max API Calls/h: `{sf.get('max_api_calls_per_hour', 200)}`\n"
        f"Warmup-Modus: `{'AN' if sf.get('warmup_mode') else 'AUS'}`\n\n"
        f"*Reports:*\n"
        f"Tagesbericht: `{'AN' if sc.get('daily_report_enabled') else 'AUS'}` um `{sc.get('daily_report_time','08:00')}`\n"
        f"Wochenbericht: `{'AN' if sc.get('weekly_report_enabled') else 'AUS'}`\n\n"
        f"Ändern mit `/set KEY WERT`\n"
        f"Beispiel: `/set report_time 09:00`"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


# Mapping: kurze Namen → volle settings-Pfade
SET_ALIASES = {
    "api_limit":        "safety.max_api_calls_per_hour",
    "report_time":      "schedule.daily_report_time",
}
SET_BOOLS = {
    "warmup":           "safety.warmup_mode",
    "daily_report":     "schedule.daily_report_enabled",
    "weekly_report":    "schedule.weekly_report_enabled",
}


async def cmd_set(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        keys = list(SET_ALIASES.keys()) + [k + " on/off" for k in SET_BOOLS.keys()]
        await update.message.reply_text(
            f"Verwendung: `/set KEY WERT`\n\n"
            f"Verfügbare Keys:\n" +
            "\n".join(f"• `{k}`" for k in keys),
            parse_mode="Markdown"
        )
        return

    key   = context.args[0].lower()
    value = context.args[1]

    if key in SET_BOOLS:
        key_path = SET_BOOLS[key]
        bool_val = value.lower() in ("on", "an", "true", "1", "ja", "yes")
        settings.set(key_path, bool_val, "telegram")
        await update.message.reply_text(
            f"✅ `{key}` → `{'AN' if bool_val else 'AUS'}`",
            parse_mode="Markdown"
        )
        return

    if key in SET_ALIASES:
        key_path = SET_ALIASES[key]
        ok, msg = settings.validate_and_set(key_path, value, "telegram")
        if ok:
            await update.message.reply_text(
                f"✅ `{key}` → `{value}`", parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(f"❌ Fehler: {msg}")
        return

    await update.message.reply_text(f"❌ Unbekannter Key: `{key}`\nNutze /einstellungen für die Liste.", parse_mode="Markdown")


async def cmd_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🖥️ *Dashboard*\n\n"
        f"URL: `{DASHBOARD_URL}`\n\n"
        f"Passwort: In deiner `.env` unter `DASHBOARD_PASSWORD`",
        parse_mode="Markdown"
    )


# ─── Scheduled Reports ────────────────────────────────────────────────────────

async def send_daily_report(context):
    if not settings.get("schedule.daily_report_enabled", True):
        return

    today = datetime.now().date().isoformat()

    # ── Meta KPIs abrufen ──────────────────────────────────────────────────────
    kpi_text  = ""
    kpi_lines = ""
    try:
        import requests as _req
        token   = os.getenv("META_ACCESS_TOKEN", "")
        account = os.getenv("META_AD_ACCOUNT_ID", "").lstrip("act_")
        if token and account:
            r = _req.get(
                f"https://graph.facebook.com/v18.0/act_{account}/insights",
                params={
                    "fields":      "campaign_name,impressions,clicks,ctr,cpm,spend,actions",
                    "date_preset": "last_7d",
                    "level":       "campaign",
                    "access_token": token,
                },
                timeout=15,
            )
            data = r.json().get("data", [])
            for k in data:
                conv = sum(int(a.get("value", 0)) for a in k.get("actions", []) if a.get("action_type") == "purchase")
                ctr  = float(k.get("ctr", 0)) * 100
                kpi_lines += (
                    f"• {k.get('campaign_name','?')[:30]}: "
                    f"{int(k.get('impressions',0)):,} Imp | "
                    f"CTR {ctr:.1f}% | "
                    f"CPM EUR {float(k.get('cpm',0)):.2f} | "
                    f"Spend EUR {float(k.get('spend',0)):.2f} | "
                    f"{conv} Käufe\n"
                )
            kpi_text = "\n".join([
                f"campaign_name: {k.get('campaign_name')}, impressions: {k.get('impressions')}, "
                f"clicks: {k.get('clicks')}, ctr: {float(k.get('ctr',0))*100:.2f}%, "
                f"cpm: {k.get('cpm')}, spend: {k.get('spend')}"
                for k in data
            ])
    except Exception as e:
        logger.warning(f"KPI-Abruf fehlgeschlagen: {e}")

    # ── Claude-Empfehlungen ────────────────────────────────────────────────────
    empfehlungen = ""
    if kpi_text:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))
            resp = client.messages.create(
                model="claude-opus-4-6",
                max_tokens=600,
                messages=[{"role": "user", "content":
                    f"Du bist Meta Ads Experte für Weingut Huppert (Wein, Rheinhessen). "
                    f"Basierend auf diesen KPIs der letzten 7 Tage:\n{kpi_text}\n\n"
                    f"WICHTIG: ROAS ist nicht zuverlässig verfügbar. Basiere deine Analyse "
                    f"ausschließlich auf: CTR, CPM, CPC, Spend, Impressionen, Klicks und Käufe (actions/purchase). "
                    f"Gib 3-5 konkrete Handlungsempfehlungen für HEUTE auf Deutsch. "
                    f"Kurz und direkt, nur Bullet-Liste, kein Intro, kein ROAS erwähnen."
                }]
            )
            empfehlungen = resp.content[0].text.strip()
        except Exception as e:
            empfehlungen = f"(Analyse nicht verfügbar: {e})"

    # ── Nachricht zusammenbauen ────────────────────────────────────────────────
    text = f"📊 *Tagesbericht — {today}*\n\n"

    if kpi_lines:
        text += f"*Kampagnen (letzte 7 Tage):*\n{kpi_lines}\n"

    if empfehlungen:
        text += f"*🎯 Was heute zu tun ist:*\n{empfehlungen}\n"
    elif not kpi_lines:
        text += "_Keine Meta KPI-Daten verfügbar — Token prüfen._\n"

    await context.bot.send_message(chat_id=CHAT_ID, text=text, parse_mode="Markdown")


async def send_weekly_report(context):
    if not settings.get("schedule.weekly_report_enabled", True):
        return

    text = (
        f"📈 *Wochenbericht — {datetime.now().strftime('%d.%m.%Y')}*\n\n"
        f"Account Health: {safety_monitor.get_status_emoji()}\n\n"
        f"Details im Tagesbericht oder /health"
    )
    await context.bot.send_message(chat_id=CHAT_ID, text=text, parse_mode="Markdown")


# ─── Weekly Review Job ───────────────────────────────────────────────────────

async def _run_weekly_review_job() -> None:
    """Führt weekly_review.run_weekly_review() im Hintergrund aus."""
    try:
        from weekly_review import run_weekly_review
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, run_weekly_review)
        logger.info("Weekly Review abgeschlossen")
    except Exception as e:
        logger.error(f"Weekly Review Fehler: {e}")


async def _error_handler(update, context) -> None:
    """Sendet ungefangene Bot-Fehler per Telegram."""
    err = context.error
    err_str = str(err)
    if "Conflict" in err_str and "getUpdates" in err_str:
        logger.warning(f"Bot-Konflikt (Deploy-Artefakt, ignoriert): {err}")
        return
    logger.error(f"Bot-Fehler: {err}", exc_info=err)
    try:
        await context.bot.send_message(
            chat_id=CHAT_ID,
            text=f"🚨 *Bot-Fehler*\n\n`{err}`",
            parse_mode="Markdown"
        )
    except Exception:
        pass


# ─── Google Drive Video-Liste & Ad-Texte ──────────────────────────────────────

# Gecachte Video-Liste (wird bei /videos neu geladen)
_gdrive_cache: list = []


def _esc(text) -> str:
    """Telegram Markdown V1: Sonderzeichen aus dynamischem Inhalt entfernen."""
    s = str(text) if text else "—"
    for ch in ["*", "_", "`", "["]:
        s = s.replace(ch, "")
    return s


async def cmd_videos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Zeigt alle Videos in Google Drive mit Nummern."""
    global _gdrive_cache
    if update.message.chat_id != CHAT_ID:
        return
    await update.message.reply_text("☁️ Lade Video-Liste aus Google Drive...")
    try:
        from gdrive_sync import list_drive_files
        import os as _os
        folder_ids = _os.getenv("GOOGLE_DRIVE_FOLDER_IDS", "").split(",")
        all_files = []
        for fid in folder_ids:
            fid = fid.strip()
            if fid:
                all_files.extend(list_drive_files(fid))
        video_files = [f for f in all_files if "video" in f.get("mimeType", "").lower()
                       or f.get("name", "").lower().endswith((".mp4", ".mov", ".avi"))]
        if not video_files:
            await update.message.reply_text("❌ Keine Videos in Google Drive gefunden.")
            return
        _gdrive_cache = video_files
        lines = ["📁 *Videos in Google Drive:*\n"]
        for i, f in enumerate(video_files, 1):
            size_mb = int(f.get("size", 0)) / 1024 / 1024
            lines.append(f"{i}. `{f['name']}` ({size_mb:.0f} MB)")
        lines.append("\n✍️ Tippe `/texte [Nummer]` für Ad-Texte\nBeispiel: `/texte 3`")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Fehler: {e}")


async def cmd_texte(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ad-Texte für ein Video generieren.
    /texte 42  → sucht Video das mit '42' beginnt (z.B. '42_Weinlese.mp4')
    /texte     → zeigt kurze Anleitung
    """
    global _gdrive_cache
    if update.message.chat_id != CHAT_ID:
        return

    args = context.args

    if not args:
        await update.message.reply_text(
            "✍️ *Ad-Texte Generator*\n\n"
            "Tippe `/texte [Nummer]` für ein Video.\n"
            "Beispiel: `/texte 42`\n\n"
            "Die Nummer entspricht dem Präfix im Dateinamen\n"
            "(z.B. `42_Weinlese.mp4` → `/texte 42`)\n\n"
            "Alle Videos anzeigen: /videos",
            parse_mode="Markdown"
        )
        return

    try:
        nr = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ Bitte eine Zahl eingeben. Beispiel: `/texte 42`", parse_mode="Markdown")
        return

    await update.message.reply_text(f"🔍 Suche Video Nr. {nr} in Google Drive...", parse_mode="Markdown")

    try:
        from gdrive_sync import list_drive_files
        import os as _os

        if not _gdrive_cache:
            folder_ids = _os.getenv("GOOGLE_DRIVE_FOLDER_IDS", "").split(",")
            all_files = []
            for fid in folder_ids:
                fid = fid.strip()
                if fid:
                    all_files.extend(list_drive_files(fid))
            _gdrive_cache = [f for f in all_files if "video" in f.get("mimeType", "").lower()
                             or f.get("name", "").lower().endswith((".mp4", ".mov", ".avi"))]

        video = None
        for f in _gdrive_cache:
            name = f.get("name", "")
            import re as _re
            m = _re.match(r'^0*(\d+)', name)
            try:
                if m and int(m.group(1)) == nr:
                    video = f
                    break
            except ValueError:
                continue

        if not video:
            _gdrive_cache = []
            folder_ids = _os.getenv("GOOGLE_DRIVE_FOLDER_IDS", "").split(",")
            all_files = []
            for fid in folder_ids:
                fid = fid.strip()
                if fid:
                    all_files.extend(list_drive_files(fid))
            _gdrive_cache = [f for f in all_files if "video" in f.get("mimeType", "").lower()
                             or f.get("name", "").lower().endswith((".mp4", ".mov", ".avi"))]
            for f in _gdrive_cache:
                name = f.get("name", "")
                prefix = name.split("_")[0].split(" ")[0].split("-")[0].strip()
                try:
                    if int(prefix) == nr:
                        video = f
                        break
                except ValueError:
                    continue

        if not video:
            await update.message.reply_text(
                f"❌ Kein Video mit Nummer *{nr}* gefunden.\n\n"
                f"Stelle sicher dass der Dateiname mit `{nr}_` oder `{nr} ` beginnt.\n"
                f"Alle Videos anzeigen: /videos",
                parse_mode="Markdown"
            )
            return

    except Exception as e:
        await update.message.reply_text(f"❌ Fehler beim Laden aus Google Drive: {e}")
        return

    video_name = video.get("name", f"{nr}.mp4")
    size_mb = int(video.get("size", 0)) / 1024 / 1024

    await update.message.reply_text(
        f"🎬 Lade Video herunter & analysiere Inhalt...\n`{video_name}`",
        parse_mode="Markdown"
    )

    try:
        from gdrive_sync import download_drive_file
        from text_generator import analyze_video_and_generate_setup, generate_full_ad_setup

        tmp_path = None
        try:
            tmp_path = download_drive_file(video)
        except Exception as e:
            logger.warning(f"Download fehlgeschlagen: {e}")

        if tmp_path and tmp_path.exists():
            await update.message.reply_text("🔍 Claude analysiert Video-Inhalt...", parse_mode="Markdown")
            s = analyze_video_and_generate_setup(str(tmp_path), video_name)
            try:
                tmp_path.unlink()
            except Exception:
                pass
        else:
            await update.message.reply_text("⚠️ Download nicht möglich — generiere auf Basis des Dateinamens...", parse_mode="Markdown")
            s = generate_full_ad_setup(video_name)

        if s.get("error"):
            await update.message.reply_text(f"❌ Fehler: {s['error']}")
            return

        interessen = _esc(", ".join(s.get("zielgruppe_interessen", [])) or "—")
        placements = _esc(", ".join(s.get("placements", [])) or "—")
        video_inhalt = _esc(s.get("video_inhalt", ""))

        msg = (
            f"✍️ *Ads Manager Setup — Nr. {nr}*\n"
            f"📁 `{video_name}` ({size_mb:.0f} MB)\n\n"
            + (f"🎬 *Video-Inhalt:*\n{video_inhalt}\n\n" if s.get("video_inhalt") else "")
            + f"━━━━ *AD TEXTE* ━━━━\n\n"
            f"*📢 Primary Text:*\n{_esc(s.get('primary_text', '—'))}\n\n"
            f"*🏷️ Headline:*\n{_esc(s.get('headline', '—'))}\n\n"
            f"*📝 Description:*\n{_esc(s.get('description', '—'))}\n\n"
            f"*🔘 CTA:* {_esc(s.get('cta', '—'))}\n\n"
            f"━━━━ *KAMPAGNE* ━━━━\n\n"
            f"🎯 Ziel: {_esc(s.get('kampagnenziel', '—'))}\n"
            f"📊 Optimierung: {_esc(s.get('optimierungsziel', '—'))}\n"
            f"💰 Gebot: {_esc(s.get('gebotstrategie', '—'))}\n"
            f"💵 Budget: *{_esc(s.get('tagesbudget_eur', '—'))} EUR/Tag*\n"
            f"📅 Laufzeit: {_esc(s.get('laufzeit_empfehlung', '—'))}\n\n"
            f"━━━━ *ZIELGRUPPE* ━━━━\n\n"
            f"👥 Alter: {_esc(s.get('zielgruppe_alter', '—'))}\n"
            f"⚥ Geschlecht: {_esc(s.get('zielgruppe_geschlecht', '—'))}\n"
            f"🌍 Standort: {_esc(s.get('zielgruppe_standort', '—'))}\n"
            f"💡 Interessen: {interessen}\n\n"
            f"━━━━ *PLACEMENTS* ━━━━\n\n"
            f"📱 {placements}\n\n"
            f"━━━━ *HINWEIS* ━━━━\n\n"
            f"💬 {_esc(s.get('hinweis', '—'))}"
        )
        try:
            await update.message.reply_text(msg, parse_mode="Markdown")
        except Exception as md_err:
            logger.warning(f"Markdown-Fehler, sende als Plaintext: {md_err}")
            plain = msg.replace("*", "").replace("`", "").replace("━", "-")
            await update.message.reply_text(plain)
    except Exception as e:
        await update.message.reply_text(f"❌ Fehler bei Textgenerierung: {e}")


# ─── Nummer-Eingabe direkt ────────────────────────────────────────────────────

async def handle_number_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Wenn der User einfach eine Zahl schreibt → Ad-Setup für dieses Video."""
    if update.message.chat_id != CHAT_ID:
        return
    text = update.message.text.strip()
    try:
        nr = int(text)
    except ValueError:
        return
    context.args = [str(nr)]
    await cmd_texte(update, context)


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    if not BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN fehlt in .env")
    if not CHAT_ID:
        raise ValueError("TELEGRAM_CHAT_ID fehlt in .env")

    scheduler = AsyncIOScheduler()

    def get_report_hour():
        t = settings.get("schedule.daily_report_time", "08:00")
        try:
            h, m = t.split(":")
            return int(h), int(m)
        except Exception:
            return 8, 0

    async def _post_init(application: Application) -> None:
        h, m = get_report_hour()
        scheduler.add_job(
            send_daily_report, "cron",
            args=[application], hour=h, minute=m, id="daily_report"
        )
        scheduler.add_job(
            send_weekly_report, "cron",
            args=[application], day_of_week="mon", hour=9, minute=0, id="weekly_report"
        )
        scheduler.add_job(
            _run_weekly_review_job, "cron",
            day_of_week="mon", hour=8, minute=0, id="weekly_review"
        )
        scheduler.start()
        logger.info(f"📅 Scheduler gestartet — Tagesbericht: {h:02d}:{m:02d}")

    app = Application.builder().token(BOT_TOKEN).post_init(_post_init).build()

    app.add_handler(CommandHandler("start",         cmd_start))
    app.add_handler(CommandHandler("hilfe",         cmd_start))
    app.add_handler(CommandHandler("status",        cmd_status))
    app.add_handler(CommandHandler("health",        cmd_health))
    app.add_handler(CommandHandler("report",        cmd_report))
    app.add_handler(CommandHandler("kpi",           cmd_kpi))
    app.add_handler(CommandHandler("pause",         cmd_pause))
    app.add_handler(CommandHandler("fortsetzen",    cmd_fortsetzen))
    app.add_handler(CommandHandler("einstellungen", cmd_einstellungen))
    app.add_handler(CommandHandler("set",           cmd_set))
    app.add_handler(CommandHandler("dashboard",     cmd_dashboard))
    app.add_handler(CommandHandler("videos",        cmd_videos))
    app.add_handler(CommandHandler("texte",         cmd_texte))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_number_input))
    app.add_error_handler(_error_handler)

    logger.info("🤖 Telegram Bot gestartet")
    app.run_polling(allowed_updates=Update.ALL_TYPES, stop_signals=None)


if __name__ == "__main__":
    main()
