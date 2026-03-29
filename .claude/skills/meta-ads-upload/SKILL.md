# /meta-ads-upload — Meta Ads Video Upload Skill

## Was dieser Skill tut

Dieses Skill steuert den kompletten Upload-Workflow für Meta Ads Videos.
Es ist **getrennt** von der Render-Pipeline und kann unabhängig laufen.

## Phasen

### Phase 1: Voraussetzungen prüfen
- `.env` auf vollständigkeit prüfen: META_ACCESS_TOKEN, META_AD_ACCOUNT_ID, META_PAGE_ID, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
- Ordner sicherstellen: `output/`, `approved/`, `uploaded/`, `failed/`, `skipped/`, `logs/`
- Dependencies installieren falls nötig: `pip install -r requirements.txt`
- Meta-Token testen via Graph API: `GET /me?access_token=...`

### Phase 2: Videos prüfen
- Alle MP4-Dateien im `output/` Ordner auflisten
- Dateigröße, Auflösung und Format anzeigen
- Warnen wenn Dateien > 4GB (Meta-Limit)
- Warnen wenn Format nicht 9:16, 1:1 oder 16:9

### Phase 3: Upload-Bot starten
- `upload_bot.py` starten
- Bestätigen dass Bot läuft
- Dem User erklären:
  - Telegram öffnen
  - `/prüfen` schicken um Videos zu sehen
  - Mit ✅/❌ Buttons entscheiden
  - `/hochladen` um Upload zu starten

### Phase 4: Status überwachen
- `logs/upload_log.json` auslesen und Fortschritt anzeigen
- Fehler aus `logs/meta_uploader.log` anzeigen
- Auf Abschluss warten und Zusammenfassung geben

## Nutzung

```
/meta-ads-upload
```

Optionale Argumente:
- `/meta-ads-upload --nur-status` — Nur Upload-Stand anzeigen ohne Bot zu starten
- `/meta-ads-upload --queue-anzeigen` — Warteschlange für morgen anzeigen

## Sicherheitsregeln (NICHT UMGEHEN)

Diese Regeln schützen den Meta Ad Account vor Sperrung:

1. **Max 5 Uploads pro Tag** — konfiguriebar in .env (MAX_UPLOADS_PER_DAY)
2. **2–3 Minuten Delay** zwischen Uploads — zufällig, nie gleich
3. **Sequenziell** — immer ein Video nach dem anderen, nie parallel
4. **Exponential Backoff** bei Rate-Limit-Fehlern: 1→2→4→8 Minuten
5. **Alles PAUSED** auf Meta — manuell aktivieren im Ads Manager
6. **Max 200 API-Calls/Stunde** — Verlangsamung ab 180
7. **Nie automatisch abgelehnte Anzeigen erneut einreichen**

## Dateistruktur

```
meta_upload_pipeline/
├── meta_uploader.py      # Kern-Upload-Logik
├── upload_bot.py         # Telegram Bot
├── requirements.txt      # Dependencies
├── .env                  # Secrets (nie committen)
├── output/               # Videos von Render-Pipeline
├── approved/             # Freigegeben, bereit zum Upload
├── uploaded/             # Erfolgreich hochgeladen
├── failed/               # Fehlgeschlagene Uploads
├── skipped/              # Vom User übersprungen
└── logs/
    ├── upload_log.json   # Alle Uploads
    ├── api_calls.json    # API-Call-Tracker
    ├── upload_queue.json # Queue für morgen
    └── meta_uploader.log # Detailliertes Log
```

## Troubleshooting

**Token-Fehler:**
Teste mit: `curl "https://graph.facebook.com/v21.0/me?access_token=TOKEN"`

**Upload schlägt fehl:**
- Prüfe ob Video H.264 kodiert ist
- Prüfe Dateigröße (max 4GB)
- Schaue in `logs/meta_uploader.log`

**Bot antwortet nicht:**
- Prüfe TELEGRAM_BOT_TOKEN in .env
- Prüfe ob Bot bereits läuft (nur eine Instanz)

**Tages-Limit erreicht:**
Videos sind in `logs/upload_queue.json` für morgen gespeichert.
Morgen früh `/meta-ads-upload` erneut starten.
