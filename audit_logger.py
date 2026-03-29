"""
Audit Logger — audit_logger.py
Vollständiges CSV-Audit-Log aller System-Aktionen + State-Backup.
"""

import csv
import json
import shutil
import logging
from datetime import datetime, timezone
from pathlib import Path

from settings_manager import BASE_DIR

logger = logging.getLogger(__name__)

LOG_DIR    = BASE_DIR / "logs"
AUDIT_FILE = LOG_DIR / "audit.csv"
BACKUP_DIR = BASE_DIR / "backups"

FIELDNAMES = [
    "timestamp", "actor", "action",
    "entity_type", "entity_id", "details", "dry_run",
]


class AuditLogger:
    def __init__(self):
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        if not AUDIT_FILE.exists():
            with open(AUDIT_FILE, "w", newline="", encoding="utf-8") as f:
                csv.DictWriter(f, fieldnames=FIELDNAMES).writeheader()

    def log(
        self,
        actor: str,
        action: str,
        entity_type: str = "",
        entity_id: str = "",
        details: str = "",
        dry_run: bool = False,
    ) -> None:
        row = {
            "timestamp":   datetime.now(timezone.utc).isoformat(),
            "actor":       actor,
            "action":      action,
            "entity_type": entity_type,
            "entity_id":   entity_id,
            "details":     str(details)[:500],
            "dry_run":     dry_run,
        }
        try:
            with open(AUDIT_FILE, "a", newline="", encoding="utf-8") as f:
                csv.DictWriter(f, fieldnames=FIELDNAMES).writerow(row)
        except Exception as e:
            logger.error(f"Audit-Log Schreibfehler: {e}")

    def backup_state(self, state_file: Path) -> Path | None:
        """
        Sichert ad_state.json mit Timestamp-Suffix.
        Behält maximal 30 Backups (älteste werden gelöscht).
        """
        if not state_file.exists():
            return None
        try:
            BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            ts   = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            dest = BACKUP_DIR / f"ad_state_{ts}.json"
            shutil.copy2(state_file, dest)

            backups = sorted(BACKUP_DIR.glob("ad_state_*.json"))
            for old in backups[:-30]:
                old.unlink(missing_ok=True)

            logger.info(f"💾 State gesichert: {dest.name}")
            self.log("system", "state_backup", "file", dest.name)
            return dest
        except Exception as e:
            logger.error(f"State-Backup fehlgeschlagen: {e}")
            return None

    def read_recent(self, n: int = 50) -> list[dict]:
        """Gibt die letzten N Audit-Einträge zurück."""
        try:
            with open(AUDIT_FILE, newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
            return rows[-n:]
        except Exception:
            return []


# Globale Singleton-Instanz
audit = AuditLogger()
