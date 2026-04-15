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

BRAND_CONTEXT = """Du schreibst Meta Ads fuer Weingut Terra Preta Huppert aus Gundersheim, Rheinhessen.

MARKENIDENTITAET:
Ein familiengefuehrtes Weingut mit klarer Haltung: Terra Preta, geschlossene Kreislaeufe, echte Nachhaltigkeit, charakterstarke Weine, keine Massenware.

DER KERN-USP: TERRA PRETA
Aus Rebschnitt wird Pflanzenkohle. Aus Pflanzenkohle und Kompost wird Terra Preta (Schwarzerde). Aus gesunden Boeden werden Weine mit mehr Tiefe und Charakter.
Terra Preta muss IMMER konkret sein: Pflanzenkohle, Rebschnitt, Kompost, Kreislauf, Bodenleben, Wasserhaltefahigkeit, CO2-Bindung.
Nie als Deko-Begriff verwenden — immer Wirkung nennen.

TONALIT AET:
- Direkt, ehrlich, greifbar, selbstbewusst, manchmal provokant — aber nicht arrogant
- Praegnant und merkfahig — kurze klare Saetze, starke Kontraste
- Nach Arbeit und Substanz, nicht nach Werbeagentur
- Nachhaltigkeit und Geschmack/Charakter immer zusammen denken

LIEBLINGSFORMULIERUNGEN:
- "Keine Massenware. Kein Greenwashing."
- "Schwarze Erde, goldener Wein."
- "Mit Haltung gemacht."
- "Das ist kein normaler Wein."
- "Nachhaltigkeit ist fuer uns kein Zusatz, sondern Teil des Geschmacks."

VERBOTEN:
- Generisches Weinmarketing ohne Terra-Preta-Bezug
- Greenwashing-Floskeln: "umweltfreundlich", "naturnah", "bewusst geniessen" ohne konkreten Bezug
- Luxusworte ohne Substanz: "exquisit", "raffiniert", "edle Komposition"
- Weiche Formulierungen: "wir versuchen", "wir moechten", "wir glauben"
- "Mit Liebe gemacht" ohne Spezifisches dahinter
- Moralischer Zeigefinger ohne Produktnutzen
- Romantische Naturbilder ohne konkrete Substanz
- Bio, biodynamisch, organic, Demeter, Bioland (nicht zertifiziert!)

FUER META ADS/HOOKS gilt besonders:
Aufmerksamkeit in 1-2 Saetzen: provokant, kurz, kontrastreich, klarer Nutzen ODER starke Haltung.
Besser schwach/stark-Beispiele:
SCHWACH: "Wir setzen auf Nachhaltigkeit und achten auf einen verantwortungsvollen Umgang mit der Natur."
STARK: "Aus altem Rebschnitt machen wir Pflanzenkohle — fuer Boeden, die mehr koennen, und Weine, die mehr zeigen."
SCHWACH: "Unsere Weine stehen fuer Qualitaet und Genuss."
STARK: "Keine Massenware. Kein Greenwashing. Weine mit Haltung, Tiefe und eigener Handschrift."

Sprache: Deutsch."""


def generate_ad_texts(video_filename: str, extra_context: str = "") -> dict:
    if not ANTHROPIC_API_KEY:
        logger.warning("ANTHROPIC_API_KEY nicht gesetzt — verwende Standard-Texte")
        return _default_texts()
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        import re as _re
        hook_match = _re.search(r'H(\d+)', video_filename, _re.IGNORECASE)
        hook_info = f"Hook-Typ H{hook_match.group(1)}" if hook_match else ""

        prompt = f"""Erstelle Meta Ad Texte fuer ein Werbevideo von Terra Preta Weingut Huppert.
Dateiname: {video_filename}
{f"Hook-Typ: {hook_info}" if hook_info else ""}
{f"Zusatzinfo: {extra_context}" if extra_context else ""}

Pflichtanforderungen:
- Terra Preta, Pflanzenkohle ODER Kreislauf muss konkret vorkommen
- Kein generisches Weinmarketing (keine leeren Qualitaets- oder Genuss-Phrasen)
- Kurze, kontrastreiche Saetze — Hook-Logik, kein Loblied

Antworte NUR mit diesem JSON (kein Markdown):
{{"primary_text": "max. 125 Zeichen, provokant und direkt", "headline": "max. 40 Zeichen, praegnant", "description": "max. 30 Zeichen"}}"""

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
        import re as _re
        hook_match = _re.search(r'H(\d+)', video_filename, _re.IGNORECASE)
        hook_info = f"Hook-Typ H{hook_match.group(1)}" if hook_match else ""

        prompt = f"""Du erstellst ein komplettes Meta Ads Setup fuer Terra Preta Weingut Huppert (Gundersheim, Rheinhessen).
Video: {video_filename}
{f"Hook-Typ: {hook_info}" if hook_info else ""}
{f"Zusatzinfo: {extra_context}" if extra_context else ""}

PFLICHT fuer primary_text und headline:
- Terra Preta, Pflanzenkohle, Kreislauf oder Bodenleben MUSS konkret benannt werden
- Kein generisches Weinmarketing — kein "Qualitaet", "Genuss", "fuer jeden Anlass"
- Ton: direkt, kantig, selbstbewusst — nach Marke, nicht nach Agentur
- Kontraste nutzen: "Keine Massenware.", "Schwarze Erde, goldener Wein.", "Mit Haltung gemacht."
- Nachhaltigkeit und Weincharakter zusammen denken

Antworte NUR mit diesem JSON (kein Markdown, keine Erklaerung):
{{
  "primary_text": "max. 125 Zeichen, provokant/direkt, Terra-Preta-Bezug zwingend",
  "headline": "max. 40 Zeichen, praegnant und merkfaehig",
  "description": "max. 30 Zeichen",
  "cta": "SHOP_NOW oder LEARN_MORE oder WATCH_MORE",
  "kampagnenziel": "VIDEO_VIEWS oder CONVERSIONS oder REACH",
  "zielgruppe_alter": "30-65",
  "zielgruppe_geschlecht": "ALL",
  "zielgruppe_interessen": ["Wein", "Nachhaltigkeit", "Genuss", "Rheinhessen"],
  "zielgruppe_standort": "Deutschland (Geo-Ausschluss: 40km um PLZ 67598)",
  "placements": ["Facebook Feed", "Instagram Reels", "Stories"],
  "optimierungsziel": "THRUPLAY oder LINK_CLICKS",
  "gebotstrategie": "LOWEST_COST",
  "tagesbudget_eur": 5,
  "laufzeit_empfehlung": "7 Tage testen",
  "hinweis": "Konkreter Tipp fuer diesen Video-Typ — was macht ihn wirkungsvoll oder wo liegt das Risiko"
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


def _get_ffmpeg() -> str:
    """Gibt den Pfad zum ffmpeg-Binary zurück (imageio-ffmpeg oder System)."""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def _get_ffprobe() -> str:
    """Gibt den Pfad zum ffprobe-Binary zurück."""
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        return exe.replace("ffmpeg", "ffprobe")
    except Exception:
        return "ffprobe"


def _get_video_duration(video_path: str) -> float:
    """Gibt Video-Dauer in Sekunden zurück."""
    import subprocess
    try:
        probe = subprocess.run(
            [_get_ffmpeg(), "-i", video_path, "-f", "null", "-"],
            capture_output=True, text=True, timeout=30
        )
        # Dauer aus stderr parsen: "Duration: 00:01:23.45"
        for line in probe.stderr.splitlines():
            if "Duration:" in line:
                parts = line.split("Duration:")[1].split(",")[0].strip()
                h, m, s = parts.split(":")
                return int(h) * 3600 + int(m) * 60 + float(s)
    except Exception:
        pass
    return 30.0


def _extract_frames(video_path: str, num_frames: int = 10) -> list:
    """Extrahiert gleichmäßig verteilte Frames als base64-JPEG-Liste."""
    import subprocess, base64, tempfile
    ffmpeg = _get_ffmpeg()
    frames = []
    try:
        duration = _get_video_duration(video_path)
        with tempfile.TemporaryDirectory() as tmpdir:
            for i in range(num_frames):
                t = duration * (i + 1) / (num_frames + 1)
                frame_path = f"{tmpdir}/frame_{i:02d}.jpg"
                subprocess.run(
                    [ffmpeg, "-ss", str(t), "-i", video_path,
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
    ffmpeg = _get_ffmpeg()
    transcript = ""
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            audio_path = f"{tmpdir}/audio.wav"
            subprocess.run(
                [ffmpeg, "-i", video_path, "-vn", "-acodec", "pcm_s16le",
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

    import re as _re2
    hook_match2 = _re2.search(r'H(\d+)', video_name, _re2.IGNORECASE)
    hook_info2 = f"Hook-Typ H{hook_match2.group(1)}" if hook_match2 else ""

    content.append({
        "type": "text",
        "text": f"""Das sind {len(frames)} Screenshots aus einem Werbevideo von Terra Preta Weingut Huppert (Gundersheim, Rheinhessen).
Dateiname: {video_name}
{f"Hook-Typ: {hook_info2}" if hook_info2 else ""}
{transcript_block}

Analysiere das Video exakt — was ist zu sehen, was wird gesagt — und erstelle ein Meta Ads Setup das auf dem echten Inhalt basiert.

PFLICHT fuer primary_text und headline:
- Terra Preta, Pflanzenkohle, Kreislauf oder Bodenleben MUSS vorkommen — konkret, nicht als Deko
- Kein generisches Weinmarketing, keine leeren Qualitaets- oder Genussfloskeln
- Ton: direkt, kantig, selbstbewusst — Beispiele: "Keine Massenware.", "Schwarze Erde, goldener Wein.", "Mit Haltung gemacht."
- Nachhaltigkeit und Weincharakter zusammen denken — der Boden erklaert den Geschmack

Antworte NUR mit diesem JSON (kein Markdown):
{{
  "video_inhalt": "Was ist konkret zu sehen und was wird gesagt? 2-3 praezise Saetze",
  "primary_text": "max. 125 Zeichen — provokant, direkt, Terra-Preta-Bezug zwingend",
  "headline": "max. 40 Zeichen — praegnant, merkfaehig",
  "description": "max. 30 Zeichen",
  "cta": "SHOP_NOW oder LEARN_MORE oder WATCH_MORE",
  "kampagnenziel": "VIDEO_VIEWS oder CONVERSIONS oder REACH",
  "zielgruppe_alter": "30-65",
  "zielgruppe_geschlecht": "ALL",
  "zielgruppe_interessen": ["Wein", "Nachhaltigkeit", "Genuss", "Rheinhessen"],
  "zielgruppe_standort": "Deutschland (Geo-Ausschluss: 40km um PLZ 67598)",
  "placements": ["Facebook Feed", "Instagram Reels", "Stories"],
  "optimierungsziel": "THRUPLAY oder LINK_CLICKS",
  "gebotstrategie": "LOWEST_COST",
  "tagesbudget_eur": 5,
  "laufzeit_empfehlung": "7 Tage testen",
  "hinweis": "Konkreter Tipp basierend auf dem echten Video-Inhalt — Staerke oder Risiko dieses Video-Typs"
}}"""
    })

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model="claude-opus-4-6",
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
        "primary_text": "Schwarze Erde, goldener Wein. Keine Massenware. Kein Greenwashing.",
        "headline":     "Terra Preta Weingut Huppert",
        "description":  "Weine mit Haltung",
    }
