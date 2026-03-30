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


def generate_full_ad_setup(video_filename: str, extra_context: str = "") -> dict:
    """Generiert komplettes Ad-Setup: Texte + alle Ads Manager Einstellungen."""
    if not ANTHROPIC_API_KEY:
        logger.warning("ANTHROPIC_API_KEY nicht gesetzt")
        return {"error": "API Key fehlt", **_default_texts()}
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        prompt = f"""Du erstellst ein komplettes Meta Ads Setup fuer Weingut Huppert (Gundersheim, Rheinhessen).
Video: {video_filename}
{f"Zusatzinfo: {extra_context}" if extra_context else ""}

Antworte NUR mit diesem JSON (kein Markdown, keine Erklaerung):
{{
  "primary_text": "Haupttext max. 125 Zeichen, 1-2 Saetze, einladend",
  "headline": "Ueberschrift max. 40 Zeichen",
  "description": "Kurzbeschreibung max. 30 Zeichen",
  "cta": "SHOP_NOW oder LEARN_MORE oder WATCH_MORE",
  "kampagnenziel": "z.B. VIDEO_VIEWS oder CONVERSIONS oder REACH - passendes Ziel fuer Weingut-Video",
  "zielgruppe_alter": "z.B. 30-65",
  "zielgruppe_geschlecht": "ALL oder MALE oder FEMALE",
  "zielgruppe_interessen": ["Wein", "Genuss", "..."],
  "zielgruppe_standort": "Deutschland, Oesterreich, Schweiz",
  "placements": ["Facebook Feed", "Instagram Feed", "Instagram Reels", "Stories"],
  "optimierungsziel": "z.B. THRUPLAY oder LINK_CLICKS oder IMPRESSIONS",
  "gebotstrategie": "LOWEST_COST oder COST_CAP",
  "tagesbudget_eur": 5,
  "laufzeit_empfehlung": "z.B. 7 Tage testen",
  "hinweis": "1 kurzer Hinweis was bei diesem Video-Typ besonders wichtig ist"
}}"""
        response = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=800,
            system=BRAND_CONTEXT,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = response.content[0].text.strip()
        data = json.loads(raw)
        logger.info(f"Volles Ad-Setup generiert fuer {video_filename}")
        return data
    except Exception as e:
        logger.error(f"Fehler bei vollstaendigem Ad-Setup: {e}")
        return {"error": str(e), **_default_texts()}


def _get_video_duration(video_path: str) -> float:
    """Gibt Video-Dauer in Sekunden zurück."""
    import subprocess
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", video_path],
            capture_output=True, text=True, timeout=30
        )
        return float(json.loads(probe.stdout).get("format", {}).get("duration", 30))
    except Exception:
        return 30.0


def _extract_frames(video_path: str, num_frames: int = 10) -> list:
    """Extrahiert gleichmäßig verteilte Frames als base64-JPEG-Liste."""
    import subprocess, base64, tempfile
    frames = []
    try:
        duration = _get_video_duration(video_path)
        with tempfile.TemporaryDirectory() as tmpdir:
            for i in range(num_frames):
                t = duration * (i + 1) / (num_frames + 1)
                frame_path = f"{tmpdir}/frame_{i:02d}.jpg"
                subprocess.run(
                    ["ffmpeg", "-ss", str(t), "-i", video_path,
                     "-vframes", "1", "-q:v", "3", "-vf", "scale=640:-1",
                     frame_path, "-y"],
                    capture_output=True, timeout=30
                )
                fp = Path(frame_path)
                if fp.exists() and fp.stat().st_size > 0:
                    frames.append(base64.standard_b64encode(fp.read_bytes()).decode())
    except Exception as e:
        logger.error(f"Frame-Extraktion fehlgeschlagen: {e}")
    return frames


def _transcribe_audio(video_path: str) -> str:
    """Extrahiert Audio und transkribiert gesprochenen Text (Deutsch)."""
    import subprocess, tempfile
    transcript = ""
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            audio_path = f"{tmpdir}/audio.wav"
            # Audio extrahieren — max 60 Sekunden, mono, 16kHz für SR
            subprocess.run(
                ["ffmpeg", "-i", video_path, "-vn", "-acodec", "pcm_s16le",
                 "-ar", "16000", "-ac", "1", "-t", "60", audio_path, "-y"],
                capture_output=True, timeout=60
            )
            if not Path(audio_path).exists():
                return ""
            import speech_recognition as sr
            recognizer = sr.Recognizer()
            with sr.AudioFile(audio_path) as source:
                audio_data = recognizer.record(source)
            transcript = recognizer.recognize_google(audio_data, language="de-DE")
            logger.info(f"Transkription: {transcript[:100]}...")
    except Exception as e:
        logger.warning(f"Transkription fehlgeschlagen (kein Problem): {e}")
    return transcript


def analyze_video_and_generate_setup(video_path: str, video_name: str) -> dict:
    """Analysiert Video vollständig: 10 Frames (visuell) + Audio-Transkript → Claude."""
    if not ANTHROPIC_API_KEY:
        return {"error": "Anthropic API Key fehlt"}

    # Parallel: Frames extrahieren + Audio transkribieren
    frames    = _extract_frames(video_path, num_frames=10)
    transcript = _transcribe_audio(video_path)

    if not frames:
        logger.warning("Keine Frames — Fallback auf Dateiname-Analyse")
        return generate_full_ad_setup(video_name)

    # Claude-Anfrage aufbauen: Bilder + Text
    content = []
    for frame in frames:
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": frame}
        })

    transcript_block = f"\n\nGesprochener Text im Video (Transkript):\n\"{transcript}\"" if transcript else ""

    content.append({
        "type": "text",
        "text": f"""Das sind {len(frames)} Screenshots aus einem Werbevideo von Weingut Huppert (Gundersheim, Rheinhessen).
Dateiname: {video_name}{transcript_block}

Analysiere das Video vollständig — was zu sehen ist, was gesagt wird — und erstelle ein komplettes Meta Ads Setup das den echten Inhalt widerspiegelt.

Antworte NUR mit diesem JSON (kein Markdown):
{{
  "video_inhalt": "Was ist konkret zu sehen und was wird gesagt? 2-3 präzise Sätze",
  "primary_text": "Haupttext max. 125 Zeichen, basierend auf echtem Video-Inhalt",
  "headline": "max. 40 Zeichen",
  "description": "max. 30 Zeichen",
  "cta": "SHOP_NOW oder LEARN_MORE oder WATCH_MORE",
  "kampagnenziel": "VIDEO_VIEWS oder CONVERSIONS oder REACH",
  "zielgruppe_alter": "z.B. 30-65",
  "zielgruppe_geschlecht": "ALL oder MALE oder FEMALE",
  "zielgruppe_interessen": ["Wein", "Genuss", "..."],
  "zielgruppe_standort": "Deutschland, Österreich, Schweiz",
  "placements": ["Facebook Feed", "Instagram Reels", "Stories"],
  "optimierungsziel": "THRUPLAY oder LINK_CLICKS",
  "gebotstrategie": "LOWEST_COST",
  "tagesbudget_eur": 5,
  "laufzeit_empfehlung": "7 Tage testen",
  "hinweis": "Konkreter Tipp basierend auf dem tatsächlichen Video-Inhalt"
}}"""
    })

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=900,
            system=BRAND_CONTEXT,
            messages=[{"role": "user", "content": content}]
        )
        raw = response.content[0].text.strip()
        data = json.loads(raw)
        logger.info(f"Video-Analyse abgeschlossen: {video_name} ({len(frames)} Frames, Transkript: {'ja' if transcript else 'nein'})")
        return data
    except Exception as e:
        logger.error(f"Claude Vision Fehler: {e}")
        return generate_full_ad_setup(video_name)


def _default_texts() -> dict:
    return {
        "primary_text": "Entdecken Sie unsere handgemachten Weine aus Rheinhessen.",
        "headline":     "Weingut Huppert",
        "description":  "Jetzt entdecken",
    }
