"""
start.py — Startet Dashboard + Telegram Bot zusammen in einem Prozess.
Für Render Free Plan: alles in einem Web Service.
"""

import threading
import logging
import os
import sys
from apscheduler.schedulers.background import BackgroundScheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def run_onedrive_sync():
    """OneDrive + Google Drive Sync im Hintergrund — alle 5 Minuten."""
    try:
        from onedrive_sync import sync_onedrive
        from gdrive_sync import sync_gdrive
        new_videos = sync_onedrive() + sync_gdrive()
        if len(new_videos) > 0:
            logger.info(f"Sync: {len(new_videos)} neue Videos heruntergeladen — starte Upload")
            threading.Thread(target=run_meta_upload, daemon=True).start()
    except Exception as e:
        logger.error(f"OneDrive-Sync Fehler: {e}")


def run_meta_upload():
    """Meta-Upload im Hintergrund starten."""
    try:
        from meta_uploader import run_upload_batch
        logger.info("Meta-Upload gestartet...")
        run_upload_batch()
    except Exception as e:
        logger.error(f"Meta-Upload Fehler: {e}")


def run_dashboard():
    """Flask Dashboard in eigenem Thread."""
    from dashboard import app
    port = int(os.getenv("PORT", 5000))
    logger.info(f"Dashboard startet auf Port {port}")
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


def run_bot():
    """Telegram Bot in eigenem Thread — eigener asyncio Event Loop."""
    import asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        from upload_bot import main
        logger.info("Telegram Bot startet...")
        main()
    except Exception as e:
        logger.error(f"Bot Fehler: {e}")
    finally:
        loop.close()


if __name__ == "__main__":
    logger.info("Weingut Huppert — Meta Upload Pipeline startet")

    # OneDrive-Sync alle 5 Minuten
    if os.getenv("ONEDRIVE_SHARE_URL"):
        sync_scheduler = BackgroundScheduler()
        sync_scheduler.add_job(run_onedrive_sync, "interval", minutes=5, id="onedrive_sync")
        sync_scheduler.start()
        logger.info("OneDrive-Sync Scheduler gestartet (alle 5 Minuten)")
        run_onedrive_sync()  # Sofort beim Start einmal ausführen

    # Bot in Hintergrund-Thread
    bot_thread = threading.Thread(target=run_bot, daemon=True, name="TelegramBot")
    bot_thread.start()
    logger.info("Telegram Bot Thread gestartet")

    # Dashboard im Hauptthread (Render erwartet einen laufenden Web-Prozess)
    run_dashboard()
