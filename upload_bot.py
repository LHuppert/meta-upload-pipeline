"""
Telegram Bot — Auto-Upload + OneDrive Sync + Reports

Workflow:
1. OneDrive-Ordner wird alle 15 Min geprueft
2. Neue Videos werden automatisch zu Meta hochgeladen (zufaellige Reihenfolge)
3. Nach jedem Upload: Telegram-Benachrichtigung mit Dateiname + Meta-ID
4. Du kannst das Video in Meta Ads Manager pruefen und bei Bedarf loeschen

Commands:
/start /hilfe       — Befehlsübersicht
/status             — Heutiger Stand
/queue              — Warteschlange
/health             — Account Health
/report             — Sofort-Bericht senden
/pause [Grund]      — Uploads sofort pausieren
/fortsetzen         — Pause aufheben
/einstellungen      — Aktuelle Einstellungen anzeigen
/set KEY WERT       — Einstellung ändern
/dashboard          — Dashboard-URL anzeigen
/sync               — OneDrive jetzt manuell pruefen
"""

import os
import json
import asyncio
import logging
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from meta_uploader import run_upload_batch, get_uploads_today, get_upload_log
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

OUTPUT_DIR   = BASE_DIR / os.getenv("OUTPUT_DIR",   "output")
APPROVED_DIR = BASE_DIR / os.getenv("APPROVED_DIR", "approved")
SKIPPED_DIR  = BASE_DIR / os.getenv("SKIPPED_DIR",  "skipped")

PENDING_FILE   = BASE_DIR / "logs" / "pending_approval.json"
DECISIONS_FILE = BASE_DIR / "logs" / "approval_decisions.json"

DASHBOARD_URL = os.getenv("DASHBOARD_URL", "http://localhost:5000")


# ─── State ────────────────────────────────────────────────────────────────────

def load_pending() -> dict:
    if PENDING_FILE.exists():
        return json.loads(PENDING_FILE.read_text(encoding="utf-8"))
    return {"batch": [], "decisions": {}, "batch_id": None}


def save_pending(data: dict):
    PENDING_FILE.parent.mkdir(exist_ok=True)
    PENDING_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def log_decision(filename: str, decision: str, note: str = ""):
    log = []
    if DECISIONS_FILE.exists():
        log = json.loads(DECISIONS_FILE.read_text(encoding="utf-8"))
    log.append({
        "timestamp": datetime.now().isoformat(),
        "filename":  filename,
        "decision":  decision,
        "note":      note,
    })
    DECISIONS_FILE.write_text(json.dumps(log, indent=2, ensure_ascii=False), encoding="utf-8")


# ─── Hilfsfunktionen ─────────────────────────────────────────────────────────

async def send_safe(context, text: str, **kwargs):
    try:
        return await context.bot.send_message(chat_id=CHAT_ID, text=text, **kwargs)
    except Exception as e:
        logger.error(f"Telegram-Fehler: {e}")


def get_output_videos() -> list:
    OUTPUT_DIR.mkdir(exist_ok=True)
    return sorted(OUTPUT_DIR.glob("*.mp4")) + sorted(OUTPUT_DIR.glob("*.MP4"))


def get_decision_keyboard(filename: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Freigeben",    callback_data=f"approve|{filename}"),
        InlineKeyboardButton("❌ Überspringen", callback_data=f"skip|{filename}"),
    ]])


def get_batch_keyboard(batch_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Alle freigeben",  callback_data=f"approve_all|{batch_id}"),
            InlineKeyboardButton("📋 Einzeln prüfen",  callback_data=f"review_single|{batch_id}"),
        ],
        [
            InlineKeyboardButton("📤 Jetzt hochladen", callback_data=f"upload_now|{batch_id}"),
        ]
    ])


def build_status_text() -> str:
    uploads_today = get_uploads_today()
    daily_limit   = settings.get_daily_limit()
    remaining     = daily_limit - uploads_today
    api_calls     = 0
    try:
        from meta_uploader import get_api_calls_last_hour
        api_calls = get_api_calls_last_hour()
    except Exception:
        pass

    api_limit = int(settings.get("safety.max_api_calls_per_hour", 200))
    paused    = settings.is_paused()
    health    = safety_monitor.get_status_emoji()
    window_ok = settings.is_within_upload_window()
    warmup    = settings.get("safety.warmup_mode", False)

    text = f"📊 *Upload-Status*\n\n"
    text += f"Heute hochgeladen: {uploads_today}/{daily_limit}\n"
    text += f"Noch möglich heute: {remaining}\n"
    text += f"API Calls (letzte Std): {api_calls}/{api_limit}\n"
    text += f"Account Health: {health}\n"
    text += f"Upload-Fenster: {'✅ OK' if window_ok else '🚫 Gesperrt'}\n"
    if warmup:
        text += f"⚡ Warmup-Modus aktiv\n"
    if paused:
        reason = settings.get("safety.pause_reason", "")
        text += f"\n🛑 *PAUSE AKTIV*\n{reason}"
    return text


# ─── Command Handler ──────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🍷 *Meta Upload Bot — Weingut Huppert*\n\n"
        "*Upload-Workflow:*\n"
        "/prufen — Videos freigeben\n"
        "/hochladen — Freigegebene Videos hochladen\n"
        "/queue — Warteschlange\n\n"
        "*Info & Reports:*\n"
        "/status — Aktueller Stand\n"
        "/health — Account Health\n"
        "/report — Sofort-Bericht\n\n"
        "*Ad-Texte:*\n"
        "/videos — Alle Google Drive Videos anzeigen\n"
        "/texte [Nr] — Ad-Texte für Video generieren\n\n"
        "*Steuerung:*\n"
        "/pause [Grund] — Uploads pausieren\n"
        "/fortsetzen — Pause aufheben\n"
        "/einstellungen — Einstellungen anzeigen\n"
        "/set KEY WERT — Einstellung ändern\n"
        "/dashboard — Dashboard-Link\n",
        parse_mode="Markdown"
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    videos_ready  = len(get_output_videos())
    pending       = load_pending()
    approved_count = sum(1 for v in pending.get("decisions", {}).values() if v == "approve")

    text = build_status_text()
    text += f"\n\nVideos im output/ Ordner: {videos_ready}"
    text += f"\nFreigegeben (noch nicht hochgeladen): {approved_count}"
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
    # Health-Monitor zurücksetzen bei manuellem Fortsetzen
    await update.message.reply_text(
        "▶️ *Pause aufgehoben*\n\nUploads werden beim nächsten /hochladen fortgesetzt.",
        parse_mode="Markdown"
    )


async def cmd_einstellungen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sf = settings.get("safety", {})
    sc = settings.get("schedule", {})
    text = (
        f"⚙️ *Aktuelle Einstellungen*\n\n"
        f"*Safety:*\n"
        f"Max Uploads/Tag: `{sf.get('max_uploads_per_day', 5)}`\n"
        f"Min Delay: `{sf.get('min_delay_seconds', 120)}s`\n"
        f"Max Delay: `{sf.get('max_delay_seconds', 180)}s`\n"
        f"Max API Calls/h: `{sf.get('max_api_calls_per_hour', 200)}`\n"
        f"Fehler bis Auto-Pause: `{sf.get('max_errors_before_pause', 3)}`\n"
        f"Warmup-Modus: `{'AN' if sf.get('warmup_mode') else 'AUS'}`\n"
        f"Upload-Fenster: `{'AN' if sf.get('upload_window_enabled') else 'AUS'}` "
        f"({sf.get('upload_window_start','08:00')}–{sf.get('upload_window_end','21:00')})\n\n"
        f"*Reports:*\n"
        f"Tagesbericht: `{'AN' if sc.get('daily_report_enabled') else 'AUS'}` um `{sc.get('daily_report_time','08:00')}`\n"
        f"Wochenbericht: `{'AN' if sc.get('weekly_report_enabled') else 'AUS'}`\n\n"
        f"Ändern mit `/set KEY WERT`\n"
        f"Beispiel: `/set max_uploads 7`"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


# Mapping: kurze Namen → volle settings-Pfade
SET_ALIASES = {
    "max_uploads":      "safety.max_uploads_per_day",
    "min_delay":        "safety.min_delay_seconds",
    "max_delay":        "safety.max_delay_seconds",
    "api_limit":        "safety.max_api_calls_per_hour",
    "slowdown":         "safety.slowdown_threshold",
    "max_errors":       "safety.max_errors_before_pause",
    "warmup_uploads":   "safety.warmup_uploads_per_day",
    "report_time":      "schedule.daily_report_time",
}
SET_BOOLS = {
    "warmup":           "safety.warmup_mode",
    "window":           "safety.upload_window_enabled",
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


async def cmd_pruefen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    videos = get_output_videos()
    if not videos:
        await update.message.reply_text(
            "📂 Keine Videos im output/ Ordner.\n\n"
            "Sobald die Render-Pipeline fertig ist, landen die Videos dort."
        )
        return

    batch_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    batch    = [v.name for v in videos]
    save_pending({"batch": batch, "decisions": {}, "batch_id": batch_id, "created": datetime.now().isoformat()})

    await send_safe(
        context,
        f"🎬 *{len(videos)} Video(s) bereit zur Freigabe*\n\n"
        f"✅ Freigeben = wird hochgeladen\n"
        f"❌ Überspringen = wird nicht hochgeladen\n\n"
        f"Oder alle auf einmal:",
        parse_mode="Markdown",
        reply_markup=get_batch_keyboard(batch_id),
    )

    for video_path in videos:
        try:
            with open(video_path, "rb") as f:
                await context.bot.send_video(
                    chat_id=CHAT_ID, video=f,
                    caption=f"📹 {video_path.name}",
                    reply_markup=get_decision_keyboard(video_path.name),
                    supports_streaming=True,
                )
            await asyncio.sleep(1.5)
        except Exception as e:
            logger.error(f"Fehler beim Senden von {video_path.name}: {e}")
            await send_safe(context, f"⚠️ Konnte {video_path.name} nicht senden: {e}")


async def cmd_hochladen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if settings.is_paused():
        reason = settings.get("safety.pause_reason", "")
        await update.message.reply_text(
            f"🛑 Uploads sind pausiert.\nGrund: {reason}\n\nNutze /fortsetzen um zu reaktivieren."
        )
        return

    if not settings.is_within_upload_window():
        sf = settings.get("safety", {})
        await update.message.reply_text(
            f"🕐 Außerhalb des Upload-Fensters.\n"
            f"Uploads erlaubt: {sf.get('upload_window_start','08:00')}–{sf.get('upload_window_end','21:00')}\n"
            f"Videos bleiben in der Queue."
        )
        return

    pending   = load_pending()
    approved  = [n for n, d in pending.get("decisions", {}).items() if d == "approve"]

    if not approved:
        await update.message.reply_text("📋 Keine freigegebenen Videos.\nNutze /prüfen um Videos freizugeben.")
        return

    uploads_today = get_uploads_today()
    daily_limit   = settings.get_daily_limit()
    remaining     = daily_limit - uploads_today

    if remaining <= 0:
        await update.message.reply_text(
            f"🛑 Tages-Limit erreicht ({daily_limit}/{daily_limit}).\n"
            f"Videos sind in der Queue für morgen."
        )
        return

    to_upload = approved[:remaining]
    queued    = approved[remaining:]

    msg = (
        f"📤 *Upload startet*\n\n"
        f"Hochladen: {len(to_upload)} Video(s)\n"
        f"Noch möglich heute: {remaining}\n"
    )
    if queued:
        msg += f"In Queue für morgen: {len(queued)}\n"
    msg += f"\nDelay: {settings.get('safety.min_delay_seconds',120)}–{settings.get('safety.max_delay_seconds',180)}s (Account-Schutz)"

    await update.message.reply_text(msg, parse_mode="Markdown")

    APPROVED_DIR.mkdir(exist_ok=True)
    video_paths = []
    for name in to_upload:
        src = OUTPUT_DIR / name
        if src.exists():
            dest = APPROVED_DIR / name
            src.rename(dest)
            video_paths.append(dest)

    asyncio.create_task(run_upload_async(context, video_paths, queued))


async def run_upload_async(context, video_paths: list, queued_names: list):
    await send_safe(context, f"⏳ Upload läuft... ({len(video_paths)} Videos)")
    try:
        results, leftover = run_upload_batch(video_paths)
        success = sum(1 for r in results if r["status"] == "success")
        failed  = sum(1 for r in results if r["status"] == "error")

        # Health-Monitor aktualisieren
        for r in results:
            if r["status"] == "success":
                safety_monitor.record_success(r.get("path", ""))
            else:
                auto_paused = safety_monitor.record_error(r.get("error", ""))
                if auto_paused and settings.get("notifications.on_auto_pause", True):
                    await send_safe(context,
                        f"🔴 *AUTO-PAUSE aktiviert!*\n\n"
                        f"Zu viele Fehler in Folge.\n"
                        f"Nutze /health für Details und /fortsetzen wenn das Problem behoben ist.",
                        parse_mode="Markdown"
                    )

        summary = (
            f"{'✅' if failed == 0 else '⚠️'} *Upload abgeschlossen*\n\n"
            f"Hochgeladen: {success}\n"
            f"Fehler: {failed}\n"
        )
        if queued_names or leftover:
            summary += f"In Queue für morgen: {len(queued_names) + len(leftover)}\n"
        summary += "\n⚠️ Alle Ads sind auf Meta *PAUSED* — bitte im Ads Manager prüfen und aktivieren."

        await send_safe(context, summary, parse_mode="Markdown")

    except Exception as e:
        await send_safe(context, f"❌ Upload-Fehler: {e}")
        safety_monitor.record_error(str(e))


async def cmd_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from meta_uploader import load_queue
    queue = [q for q in load_queue() if q["status"] == "pending"]
    if not queue:
        await update.message.reply_text("📋 Queue ist leer.")
        return

    lines = [f"📋 *{len(queue)} Video(s) in Queue:*\n"]
    for q in queue[:20]:
        name  = Path(q["path"]).name
        added = q["added"][:10]
        lines.append(f"• {name} (hinzugefügt: {added})")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ─── Callback Handler ─────────────────────────────────────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data    = query.data
    action, payload = data.split("|", 1)

    # Test-Upload Callback
    if action == "testup":
        if payload == "cancel":
            await query.edit_message_text("❌ Test-Upload abgebrochen.")
            return
        # Echten Upload starten
        await query.edit_message_text(f"⏳ Lade `{payload}` jetzt hoch...", parse_mode="Markdown")
        await run_onedrive_sync(context.application, force_filename=payload)
        return

    pending = load_pending()

    if action == "approve":
        filename = payload
        pending["decisions"][filename] = "approve"
        save_pending(pending)
        log_decision(filename, "approve")
        approved_count = sum(1 for v in pending["decisions"].values() if v == "approve")
        await query.edit_message_caption(
            f"✅ {filename} — freigegeben ({approved_count}/{len(pending['batch'])} entschieden)",
            reply_markup=None,
        )

    elif action == "skip":
        filename = payload
        pending["decisions"][filename] = "skip"
        save_pending(pending)
        log_decision(filename, "skip")
        SKIPPED_DIR.mkdir(exist_ok=True)
        src = OUTPUT_DIR / filename
        if src.exists():
            src.rename(SKIPPED_DIR / filename)
        await query.edit_message_caption(f"❌ {filename} — übersprungen", reply_markup=None)
        await check_all_decided(context, pending)

    elif action == "approve_all":
        for name in pending.get("batch", []):
            if name not in pending["decisions"]:
                pending["decisions"][name] = "approve"
                log_decision(name, "approve", "bulk")
        save_pending(pending)
        count = sum(1 for v in pending["decisions"].values() if v == "approve")
        await query.edit_message_text(
            f"✅ Alle {count} Videos freigegeben!\nNutze /hochladen um den Upload zu starten."
        )

    elif action == "review_single":
        await query.edit_message_text("📋 Prüfe die Videos oben einzeln.\nWenn fertig: /hochladen")

    elif action == "upload_now":
        await query.edit_message_text("📤 Upload wird gestartet...")
        approved = [n for n, d in pending.get("decisions", {}).items() if d == "approve"]
        if not approved:
            await send_safe(context, "Noch keine Videos freigegeben.")
            return
        APPROVED_DIR.mkdir(exist_ok=True)
        video_paths = []
        for name in approved:
            src = OUTPUT_DIR / name
            if src.exists():
                dest = APPROVED_DIR / name
                src.rename(dest)
                video_paths.append(dest)
        asyncio.create_task(run_upload_async(context, video_paths, []))


async def check_all_decided(context, pending: dict):
    total   = len(pending["batch"])
    decided = len(pending["decisions"])
    if decided < total:
        return
    approved = sum(1 for v in pending["decisions"].values() if v == "approve")
    skipped  = sum(1 for v in pending["decisions"].values() if v == "skip")
    await send_safe(
        context,
        f"📊 *Alle {total} Videos geprüft*\n\n"
        f"✅ Freigegeben: {approved}\n"
        f"❌ Übersprungen: {skipped}\n\n"
        f"Nutze /hochladen um die {approved} Videos hochzuladen.",
        parse_mode="Markdown",
    )


# ─── Video-Empfang per Telegram ───────────────────────────────────────────────

async def handle_video_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Videos die direkt per Telegram gesendet werden in output/ speichern."""
    msg = update.message
    if not msg:
        return

    # Sicherheit: nur vom autorisierten Chat
    if msg.chat_id != CHAT_ID:
        await msg.reply_text("❌ Nicht autorisiert.")
        return

    # Video oder Dokument (als Datei gesendet)
    file_obj = None
    filename = None
    if msg.video:
        file_obj = msg.video
        filename = f"video_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4"
    elif msg.document and msg.document.mime_type and "video" in msg.document.mime_type:
        file_obj = msg.document
        original = msg.document.file_name or "video.mp4"
        ext = Path(original).suffix or ".mp4"
        filename = f"video_{datetime.now().strftime('%Y%m%d_%H%M%S')}{ext}"
    else:
        return

    await msg.reply_text(f"📥 Video empfangen — wird heruntergeladen...")

    try:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        dest = OUTPUT_DIR / filename
        tg_file = await context.bot.get_file(file_obj.file_id)
        await tg_file.download_to_drive(str(dest))
        await msg.reply_text(
            f"✅ *{filename}* gespeichert!\n\n"
            f"Nutze /prüfen um das Video freizugeben und hochzuladen.",
            parse_mode="Markdown"
        )
        logger.info(f"Video empfangen und gespeichert: {filename}")
    except Exception as e:
        await msg.reply_text(f"❌ Fehler beim Speichern: {e}")
        logger.error(f"Video-Download Fehler: {e}")


# ─── Scheduled Reports ────────────────────────────────────────────────────────

async def send_daily_report(context):
    if not settings.get("schedule.daily_report_enabled", True):
        return

    uploads_today = get_uploads_today()
    daily_limit   = settings.get_daily_limit()
    health_emoji  = safety_monitor.get_status_emoji()
    health_data   = safety_monitor.get_health()
    queue_count   = 0
    try:
        from meta_uploader import load_queue
        queue_count = len([q for q in load_queue() if q["status"] == "pending"])
    except Exception:
        pass

    # Letzte 5 Uploads
    log   = get_upload_log()[:5]
    today = datetime.now().date().isoformat()
    log_lines = ""
    for u in log:
        if u.get("date") == today:
            icon = "✅" if u.get("status") == "success" else "❌"
            ts   = u.get("timestamp", "")[:16].split("T")
            time = ts[1] if len(ts) > 1 else ""
            log_lines += f"{icon} {u.get('filename','?')[:30]} {time}\n"

    text = (
        f"📊 *Tagesbericht — {today}*\n\n"
        f"Uploads heute: {uploads_today}/{daily_limit}\n"
        f"Queue: {queue_count} Videos warten\n"
        f"Account Health: {health_emoji}\n"
        f"Fehler in Folge: {health_data.get('consecutive_errors', 0)}\n"
    )
    if settings.is_paused():
        text += f"\n🛑 *PAUSE AKTIV*: {settings.get('safety.pause_reason', '')}\n"
    if log_lines:
        text += f"\n*Heutige Uploads:*\n{log_lines}"
    text += f"\nMorgen verfügbar: {daily_limit} Uploads"

    await context.bot.send_message(chat_id=CHAT_ID, text=text, parse_mode="Markdown")


async def send_weekly_report(context):
    if not settings.get("schedule.weekly_report_enabled", True):
        return

    log = get_upload_log()
    from datetime import timedelta
    week_ago = (datetime.now() - timedelta(days=7)).date().isoformat()
    week_uploads = [u for u in log if u.get("date", "") >= week_ago]
    success = sum(1 for u in week_uploads if u.get("status") == "success")
    failed  = sum(1 for u in week_uploads if u.get("status") != "success")
    total   = len(week_uploads)
    rate    = int(success / max(total, 1) * 100)

    text = (
        f"📈 *Wochenbericht*\n\n"
        f"Hochgeladen: {success} Videos\n"
        f"Fehler: {failed}\n"
        f"Erfolgsrate: {rate}%\n"
        f"Durchschnitt: {round(success/7, 1)}/Tag\n\n"
        f"Account Health: {safety_monitor.get_status_emoji()}"
    )
    await context.bot.send_message(chat_id=CHAT_ID, text=text, parse_mode="Markdown")


# ─── Weekly Review Job ───────────────────────────────────────────────────────

async def _run_weekly_review_job() -> None:
    """Führt weekly_review.run_weekly_review() im Hintergrund aus."""
    try:
        from weekly_review import run_weekly_review
        import asyncio
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, run_weekly_review)
        logger.info("Weekly Review abgeschlossen")
    except Exception as e:
        logger.error(f"Weekly Review Fehler: {e}")


async def _error_handler(update, context) -> None:
    """Sendet ungefangene Bot-Fehler per Telegram (ausser harmlose Deploy-Konflikte)."""
    err = context.error
    err_str = str(err)
    # Conflict tritt kurz beim Deploy auf wenn zwei Instanzen laufen — kein echter Fehler
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


# ─── OneDrive Auto-Upload ─────────────────────────────────────────────────────

async def run_onedrive_sync(app_or_context, force_filename: str = None):
    """Prueft OneDrive auf neue Videos und laedt sie automatisch zu Meta hoch."""
    if settings.is_paused():
        logger.info("OneDrive Sync uebersprungen — Pause aktiv")
        return

    bot = app_or_context.bot if hasattr(app_or_context, "bot") else app_or_context

    try:
        from onedrive_sync import sync_onedrive, list_onedrive_files, download_file
        from gdrive_sync import sync_gdrive
        import random as _random

        if force_filename:
            # Gezielter Download eines bestimmten Videos (für /testupload)
            files = list_onedrive_files()
            item = next((f for f in files if f.get("name") == force_filename), None)
            new_videos = [download_file(item)] if item else []
            new_videos = [v for v in new_videos if v]
        else:
            new_videos = sync_onedrive() + sync_gdrive()
    except Exception as e:
        logger.error(f"OneDrive Sync Fehler: {e}")
        try:
            await bot.send_message(
                chat_id=CHAT_ID,
                text=f"⚠️ *OneDrive Sync Fehler*\n\n`{e}`",
                parse_mode="Markdown"
            )
        except Exception:
            pass
        return

    if not new_videos:
        logger.info("OneDrive Sync: keine neuen Videos")
        return

    # Zufaellige Reihenfolge
    _random.shuffle(new_videos)
    logger.info(f"OneDrive Sync: {len(new_videos)} neue Videos → Auto-Upload startet")

    await bot.send_message(
        chat_id=CHAT_ID,
        text=f"☁️ *OneDrive Sync*: {len(new_videos)} neue Video(s) gefunden — Upload startet...",
        parse_mode="Markdown"
    )

    loop = asyncio.get_event_loop()
    results, _ = await loop.run_in_executor(None, run_upload_batch, new_videos)

    for r in results:
        filename = Path(r["path"]).name
        if r["status"] == "success":
            texts = r.get("ad_texts", {})
            pt   = texts.get("primary_text", "—")
            hl   = texts.get("headline", "—")
            desc = texts.get("description", "—")
            active_campaign = settings.get("campaigns.active", "testing_inhouse")
            campaign_list   = settings.get("campaigns.list", [])
            camp_name = next((c["name"] for c in campaign_list if c["id"] == active_campaign), active_campaign)
            msg = (
                f"✅ *Video hochgeladen!*\n\n"
                f"📁 `{filename}`\n"
                f"🆔 Meta-ID: `{r['video_id']}`\n"
                f"🎯 Kampagne: *{camp_name}*\n\n"
                f"━━━━ *Verwendete Meta Ad Texte* ━━━━\n\n"
                f"*📢 Primary Text:*\n{pt}\n\n"
                f"*🏷️ Headline:*\n{hl}\n\n"
                f"*📝 Description:*\n{desc}\n\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"Bei Bedarf im Meta Ads Manager löschen."
            )
            safety_monitor.record_success()
        else:
            msg = (
                f"❌ *Upload fehlgeschlagen*\n\n"
                f"📁 `{filename}`\n"
                f"Fehler: {r.get('error', '?')}"
            )
            safety_monitor.record_error(r.get("error", ""))

        await bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")

        # Lokale Datei nach Upload löschen — /tmp hat nur 2 GB Limit
        try:
            Path(r["path"]).unlink(missing_ok=True)
            logger.info(f"Lokale Datei gelöscht: {filename}")
        except Exception as e:
            logger.warning(f"Konnte Datei nicht löschen: {filename}: {e}")


async def cmd_sync(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manueller OneDrive Sync."""
    if update.message.chat_id != CHAT_ID:
        return
    await update.message.reply_text("☁️ OneDrive wird jetzt manuell geprüft...")
    await run_onedrive_sync(context.application)


# ─── Google Drive Video-Liste & Ad-Texte ──────────────────────────────────────

# Gecachte Video-Liste (wird bei /videos neu geladen)
_gdrive_cache: list = []


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

    # Google Drive durchsuchen nach Datei die mit der Nummer beginnt
    try:
        from gdrive_sync import list_drive_files
        import os as _os

        # Cache nutzen oder neu laden
        if not _gdrive_cache:
            folder_ids = _os.getenv("GOOGLE_DRIVE_FOLDER_IDS", "").split(",")
            all_files = []
            for fid in folder_ids:
                fid = fid.strip()
                if fid:
                    all_files.extend(list_drive_files(fid))
            _gdrive_cache = [f for f in all_files if "video" in f.get("mimeType", "").lower()
                             or f.get("name", "").lower().endswith((".mp4", ".mov", ".avi", ".mov"))]

        # Datei suchen die mit der Nummer beginnt (z.B. "42_", "42 ", "042_")
        video = None
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
            # Cache leeren und nochmal versuchen
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
        f"🤖 Generiere Setup für:\n`{video_name}`...",
        parse_mode="Markdown"
    )

    try:
        from text_generator import generate_full_ad_setup
        s = generate_full_ad_setup(video_name)

        if s.get("error"):
            await update.message.reply_text(f"❌ Fehler: {s['error']}")
            return

        interessen = ", ".join(s.get("zielgruppe_interessen", [])) or "—"
        placements = ", ".join(s.get("placements", [])) or "—"

        msg = (
            f"✍️ *Ads Manager Setup — Nr. {nr}*\n"
            f"📁 `{video_name}` ({size_mb:.0f} MB)\n\n"
            f"━━━━ *AD TEXTE* ━━━━\n\n"
            f"*📢 Primary Text:*\n{s.get('primary_text', '—')}\n\n"
            f"*🏷️ Headline:*\n{s.get('headline', '—')}\n\n"
            f"*📝 Description:*\n{s.get('description', '—')}\n\n"
            f"*🔘 CTA Button:* `{s.get('cta', '—')}`\n\n"
            f"━━━━ *KAMPAGNE* ━━━━\n\n"
            f"🎯 Kampagnenziel: `{s.get('kampagnenziel', '—')}`\n"
            f"📊 Optimierungsziel: `{s.get('optimierungsziel', '—')}`\n"
            f"💰 Gebotstrategie: `{s.get('gebotstrategie', '—')}`\n"
            f"💵 Tagesbudget: *{s.get('tagesbudget_eur', '—')} EUR*\n"
            f"📅 Laufzeit: {s.get('laufzeit_empfehlung', '—')}\n\n"
            f"━━━━ *ZIELGRUPPE* ━━━━\n\n"
            f"👥 Alter: `{s.get('zielgruppe_alter', '—')}`\n"
            f"⚥ Geschlecht: `{s.get('zielgruppe_geschlecht', '—')}`\n"
            f"🌍 Standort: {s.get('zielgruppe_standort', '—')}\n"
            f"💡 Interessen: {interessen}\n\n"
            f"━━━━ *PLACEMENTS* ━━━━\n\n"
            f"📱 {placements}\n\n"
            f"━━━━ *HINWEIS* ━━━━\n\n"
            f"💬 {s.get('hinweis', '—')}"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Fehler bei Textgenerierung: {e}")


async def cmd_testupload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Zeigt Textvorschau für ein zufälliges OneDrive-Video — ohne Upload."""
    if update.message.chat_id != CHAT_ID:
        return
    await update.message.reply_text("🧪 Analysiere OneDrive-Ordner...")
    try:
        from onedrive_sync import list_onedrive_files
        from text_generator import generate_ad_texts
        import random as _random

        files = list_onedrive_files()
        if not files:
            await update.message.reply_text("❌ Keine Videos in OneDrive gefunden.\n\nOneDrive Share-Link gesetzt? /einstellungen")
            return

        item = _random.choice(files)
        filename = item.get("name", "video.mp4")
        size_mb  = item.get("size", 0) / 1024 / 1024

        await update.message.reply_text(f"📝 KI generiert Texte für `{filename}`...", parse_mode="Markdown")
        texts = generate_ad_texts(filename)

        active_campaign = settings.get("campaigns.active", "testing_inhouse")
        campaign_list   = settings.get("campaigns.list", [])
        camp_name = next((c["name"] for c in campaign_list if c["id"] == active_campaign), active_campaign)

        msg = (
            f"🧪 *TEST-VORSCHAU* — noch nicht hochgeladen\n\n"
            f"📁 Datei: `{filename}`\n"
            f"📦 Größe: {size_mb:.1f} MB\n"
            f"🎯 Kampagne: *{camp_name}*\n\n"
            f"━━━━ *Meta Ad Texte (KI)* ━━━━\n\n"
            f"*📢 Primary Text:*\n{texts.get('primary_text', '—')}\n\n"
            f"*🏷️ Headline:*\n{texts.get('headline', '—')}\n\n"
            f"*📝 Description:*\n{texts.get('description', '—')}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Texte OK? Jetzt wirklich hochladen?"
        )

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Ja, hochladen", callback_data=f"testup|{filename}"),
            InlineKeyboardButton("❌ Abbrechen",     callback_data="testup|cancel"),
        ]])

        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=keyboard)

    except Exception as e:
        logger.error(f"testupload Fehler: {e}")
        await update.message.reply_text(f"❌ Fehler: {e}")


# ─── Nummer-Eingabe direkt ────────────────────────────────────────────────────

async def handle_number_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Wenn der User einfach eine Zahl schreibt → Ad-Setup für dieses Video."""
    if update.message.chat_id != CHAT_ID:
        return
    text = update.message.text.strip()
    try:
        nr = int(text)
    except ValueError:
        return  # Kein Zahl → ignorieren
    # Direkt wie /texte [nr] behandeln
    context.args = [str(nr)]
    await cmd_texte(update, context)


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    if not BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN fehlt in .env")
    if not CHAT_ID:
        raise ValueError("TELEGRAM_CHAT_ID fehlt in .env")

    # APScheduler — im post_init starten damit der asyncio-Loop bereits läuft
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
        # Async-Funktionen direkt übergeben — AsyncIOScheduler handled coroutines korrekt
        scheduler.add_job(
            send_daily_report, "cron",
            args=[application], hour=h, minute=m, id="daily_report"
        )
        scheduler.add_job(
            send_weekly_report, "cron",
            args=[application], day_of_week="mon", hour=9, minute=0, id="weekly_report"
        )
        scheduler.add_job(
            run_onedrive_sync, "interval",
            args=[application], minutes=15, id="onedrive_sync"
        )
        # Weekly Review jeden Montag 08:00 UTC
        scheduler.add_job(
            _run_weekly_review_job, "cron",
            day_of_week="mon", hour=8, minute=0, id="weekly_review"
        )
        scheduler.start()
        logger.info(f"📅 Scheduler gestartet — Tagesbericht: {h:02d}:{m:02d}")

    app = Application.builder().token(BOT_TOKEN).post_init(_post_init).build()

    # Commands registrieren
    app.add_handler(CommandHandler("start",        cmd_start))
    app.add_handler(CommandHandler("hilfe",        cmd_start))
    app.add_handler(CommandHandler("status",       cmd_status))
    app.add_handler(CommandHandler("health",       cmd_health))
    app.add_handler(CommandHandler("report",       cmd_report))
    app.add_handler(CommandHandler("pause",        cmd_pause))
    app.add_handler(CommandHandler("fortsetzen",   cmd_fortsetzen))
    app.add_handler(CommandHandler("einstellungen",cmd_einstellungen))
    app.add_handler(CommandHandler("set",          cmd_set))
    app.add_handler(CommandHandler("dashboard",    cmd_dashboard))
    app.add_handler(CommandHandler("prufen",       cmd_pruefen))
    app.add_handler(CommandHandler("hochladen",    cmd_hochladen))
    app.add_handler(CommandHandler("queue",        cmd_queue))
    app.add_handler(CommandHandler("sync",         cmd_sync))
    app.add_handler(CommandHandler("testupload",   cmd_testupload))
    app.add_handler(CommandHandler("videos",       cmd_videos))
    app.add_handler(CommandHandler("texte",        cmd_texte))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video_upload))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_number_input))
    app.add_error_handler(_error_handler)

    logger.info("🤖 Telegram Upload-Bot gestartet")
    logger.info("☁️ OneDrive Sync: alle 15 Minuten")
    app.run_polling(allowed_updates=Update.ALL_TYPES, stop_signals=None)


if __name__ == "__main__":
    main()
