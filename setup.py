#!/usr/bin/env python3
"""
Setup-Script: Erstellt alle benötigten Ordner und prüft die .env Datei.
Einmalig ausführen: python setup.py
"""

import os
import sys
from pathlib import Path

print("🍷 Meta Upload Pipeline — Setup\n")

# ─── Ordner erstellen ─────────────────────────────────────────────────────────
dirs = ["output", "approved", "uploaded", "failed", "skipped", "logs",
        ".claude/skills/meta-ads-upload"]

for d in dirs:
    Path(d).mkdir(parents=True, exist_ok=True)
    print(f"  ✅ {d}/")

print()

# ─── .env prüfen ─────────────────────────────────────────────────────────────
env_path = Path(".env")
template_path = Path(".env.template")

if not env_path.exists():
    if template_path.exists():
        import shutil
        shutil.copy(template_path, env_path)
        print("📝 .env aus Template erstellt — bitte ausfüllen!")
    else:
        print("⚠️  .env fehlt — bitte .env.template kopieren und ausfüllen")
    sys.exit(1)

from dotenv import load_dotenv
load_dotenv()

required = {
    "META_ACCESS_TOKEN":  "Meta System User Token",
    "META_AD_ACCOUNT_ID": "Meta Ad Account ID (act_XXXXXXX)",
    "META_PAGE_ID":       "Facebook Page ID",
    "TELEGRAM_BOT_TOKEN": "Telegram Bot Token (von @BotFather)",
    "TELEGRAM_CHAT_ID":   "Telegram Chat ID",
}

missing = []
for key, label in required.items():
    val = os.getenv(key, "")
    if not val or "dein" in val or "XXXXXXXXX" in val:
        print(f"  ❌ {key} — {label} fehlt noch")
        missing.append(key)
    else:
        preview = val[:8] + "..." if len(val) > 8 else val
        print(f"  ✅ {key} = {preview}")

print()

if missing:
    print(f"❗ {len(missing)} Wert(e) in .env noch nicht gesetzt.")
    print("   Bitte .env ausfüllen und setup.py erneut starten.\n")
    sys.exit(1)

# ─── Meta Token testen ───────────────────────────────────────────────────────
print("🔍 Meta-Token testen...")
import requests

token    = os.getenv("META_ACCESS_TOKEN")
resp     = requests.get(f"https://graph.facebook.com/v21.0/me?access_token={token}", timeout=10)
data     = resp.json()

if "error" in data:
    print(f"  ❌ Token ungültig: {data['error']['message']}")
    sys.exit(1)

print(f"  ✅ Token OK — System User: {data.get('name', 'Unbekannt')}")

print()
print("✅ Setup abgeschlossen! Starte mit:")
print("   python upload_bot.py     ← Telegram Bot")
print("   python meta_uploader.py  ← Direkt (ohne Bot)")
print()
print("   Oder in Claude Code: /meta-ads-upload")
