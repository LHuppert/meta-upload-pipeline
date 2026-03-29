"""
text_generator.py — Generiert Meta Ad Texte per Claude API.

Für jedes Video werden 3 Felder generiert:
- Primary Text:  Haupttext der Anzeige (max. 125 Zeichen empfohlen)
- Headline:      Überschrift (max. 40 Zeichen)
- Description:   Kurzbeschreibung (max. 30 Zeichen)

Der Dateiname des Videos wird als Kontext verwendet.
Bsp: "004_H01_MCAT_03_CTA_3.mp4" → Weingut-Anzeige mit Weinberg-Motiv
"""

import os
import json
import logging
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# ── Marken-Kontext (einmal definiert, immer verwendet) ────────────────────────

SYSTEM_PROMPT = """Du bist Texter für Weingut Huppert, ein familiengeführtes Weingut
aus Gundersheim im Rheinhessen (Deutschland). Das Weingut produziert hochwertige Weine
und schaltet Videoanzeigen auf Facebook und Instagram.

Zielgruppe: Weinliebhaber und Genussmenschen im deutschsprachigen Raum, 30–65 Jahre.
Ton: authentisch, warm, einladend — nicht übertrieben werblich.
Sprache: Deutsch.

Deine Aufgabe: Generiere präzise Meta Ad Texte basierend auf dem Dateinamen des Videos."""


def generate_ad_texts(video_filename: str, extra_context: str = "") -> dict:
    """
    Generiert Primary Text, Headline und Description für eine Meta-Videoanzeige.

    Args:
        video_filename:  Dateiname des Videos (z.B. "004_H01_MCAT_03_CTA_3.mp4")
        extra_context:   Optionaler zusätzlicher Kontext (z.B. "Weinlese-Saison")

    Returns:
        dict mit keys: primary_text, headline, description
    """
    if not ANTHROPIC_API_KEY:
        logger.warning("ANTHROPIC_API_KEY nicht gesetzt — verwende Standard-Texte")
        return _default_texts()

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

        context_part = f"\nZusätzlicher Kontext: {extra_context}" if extra_context else ""

        prompt = f"""Erstelle Meta Ad Texte für dieses Weingut-Video.

Dateiname: {video_filename}{context_part}

Leite aus dem Dateinamen ab, worum es im Video geht (z.B. CTA = Call-to-Action,
H01 = erstes Hauptvideo, MCAT = Meta Category, etc.) und schreibe passende Texte.

Erstelle GENAU diese 3 Felder:
- primary_text: 1–2 Sätze, emotional und ansprechend, max. 125 Zeichen
- headline: Prägnante Überschrift, max. 40 Zeichen
- description: Sehr kurz, handlungsauffordernd, max. 30 Zeichen

Antworte NUR mit diesem JSON (kein Markdown, keine Erklärung):
{{"primary_text": "...", "headline": "...", "description": "..."}}"""

        response = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=512,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}]
        )

        raw = response.content[0].text.strip()

        # JSON parsen (robuste Variante)
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]

        data = json.loads(raw.strip())

        result = {
            "primary_text": data.get("primary_text", "")[:500],
            "headline":     data.get("headline", "")[:100],
            "description":  data.get("description", "")[:100],
        }

        logger.info(
            f"✅ Ad-Texte generiert für {video_filename}\n"
            f"   Primary: {result['primary_text']}\n"
            f"   Headline: {result['headline']}\n"
            f"   Description: {result['description']}"
        )
        return result

    except json.JSONDecodeError as e:
        logger.error(f"JSON-Parse-Fehler bei Textgenerierung: {e} — Antwort: {raw!r}")
        return _default_texts()
    except Exception as e:
        logger.error(f"Fehler bei Textgenerierung: {e}")
        return _default_texts()


def _default_texts() -> dict:
    """Fallback-Texte wenn Claude API nicht verfügbar."""
    return {
        "primary_text": "Entdecken Sie unsere handgemachten Weine aus dem Herzen Rheinhessens.",
        "headline":     "Weingut Huppert",
        "description":  "Jetzt entdecken",
    }


def generate_and_save(video_path: Path) -> dict:
    """
    Generiert Texte für ein Video und speichert sie als JSON-Datei neben dem Video.
    Gibt die generierten Texte zurück.
    """
    texts = generate_ad_texts(video_path.name)

    texts_file = video_path.with_suffix(".texts.json")
    texts_file.write_text(
        json.dumps(texts, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )
    logger.info(f"💾 Texte gespeichert: {texts_file.name}")
    return texts


def load_texts_for_video(video_path: Path) -> dict | None:
    """
    Lädt vorhandene Texte für ein Video.
    Gibt None zurück wenn keine Datei gefunden.
    """
    texts_file = video_path.with_suffix(".texts.json")
    if texts_file.exists():
        try:
            return json.loads(texts_file.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


if __name__ == "__main__":
    # Test
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    test_videos = [
        "004_H01_MCAT_03_CTA_3.mp4",
        "009_H01_MCAT_04_CTA_5.mp4",
        "Weingut_Huppert_Herbst_2024.mp4",
    ]
    for v in test_videos:
        print(f"\n--- {v} ---")
        texts = generate_ad_texts(v)
        for k, val in texts.items():
            print(f"  {k}: {val}")
