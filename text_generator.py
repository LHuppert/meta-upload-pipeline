"""
text_generator.py — Generiert Meta Ad Texte per Claude API.
"""
import os
import json
import logging
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

BRAND_CONTEXT = """Du arbeitest fuer Weingut Huppert aus Gundersheim, Rheinhessen (Deutschland).
Das Weingut produziert hochwertige Weine und erstellt Videoanzeigen fuer Meta Ads (Facebook/Instagram).
Zielgruppe: Weinliebhaber und Genussmenschen im deutschsprachigen Raum.
Ton: authentisch, warm, einladend.
Sprache: Deutsch."""


def generate_ad_texts(video_filename: str, extra_context: str = "") -> dict:
    if not ANTHROPIC_API_KEY:
        logger.warning("ANTHROPIC_API_KEY nicht gesetzt — verwende Standard-Texte")
        return _default_texts()
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        prompt = f"""Erstelle Meta Ad Texte fuer ein Weingut-Video.
Dateiname: {video_filename}
{f"Zusatzinfo: {extra_context}" if extra_context else ""}

Erstelle exakt diese 3 Felder:
- primary_text: Haupttext (1-2 Saetze, max. 125 Zeichen)
- headline: Ueberschrift (max. 40 Zeichen)
- description: Kurzbeschreibung (max. 30 Zeichen)

Antworte NUR mit diesem JSON (kein Markdown):
{{"primary_text": "...", "headline": "...", "description": "..."}}"""

        response = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=512,
            system=BRAND_CONTEXT,
            messages=[{"role": "user", "content": prompt}]
        )
        text = response.content[0].text.strip()
        data = json.loads(text)
        result = {
            "primary_text": data.get("primary_text", "")[:500],
            "headline":     data.get("headline", "")[:40],
            "description":  data.get("description", "")[:30],
        }
        logger.info(f"Ad-Texte generiert fuer {video_filename}")
        return result
    except Exception as e:
        logger.error(f"Fehler bei Textgenerierung: {e}")
        return _default_texts()


def generate_and_save(video_path) -> dict:
    video_path = Path(video_path)
    texts = generate_ad_texts(video_path.name)
    texts_file = video_path.with_suffix(".texts.json")
    try:
        texts_file.write_text(json.dumps(texts, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.warning(f"Konnte Texte nicht speichern: {e}")
    return texts


def load_texts_for_video(video_path) -> dict:
    video_path = Path(video_path)
    texts_file = video_path.with_suffix(".texts.json")
    if texts_file.exists():
        try:
            return json.loads(texts_file.read_text(encoding="utf-8"))
        except Exception:
            pass
    return None


def _default_texts() -> dict:
    return {
        "primary_text": "Entdecken Sie unsere handgemachten Weine aus Rheinhessen.",
        "headline":     "Weingut Huppert",
        "description":  "Jetzt entdecken",
    }
