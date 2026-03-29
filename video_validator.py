"""
Video Validator — video_validator.py
Qualitätskontrolle vor Upload: Auflösung, Codec, Länge, Duplikate, Ad-Text-Policy.

Meta Alkohol-Policy: keine Health-Claims, kein "bio"/"organic" ohne Zertifizierung.
"""

import hashlib
import json
import logging
import subprocess
from pathlib import Path

from settings_manager import settings, BASE_DIR

logger = logging.getLogger(__name__)

HASH_DB_FILE = BASE_DIR / "logs" / "video_hashes.json"

# ── Verbotene Wörter (Meta Alkohol-Policy + Irreführungsverbot DE) ────────────
FORBIDDEN_WORDS = [
    "bio",               # nur mit Zertifizierung erlaubt
    "biodynamisch",
    "organic",
    "biologisch",
    "bekömmlich",
    "verträglich",
    "naturbelassen",
    "natürlich hergestellt",
    "gesund",
    "gut für",
    "heilend",
    "heilwirkung",
    "antioxidant",
    "medizinisch",
    "therapie",
    "krebsvorbeugend",
]

WARNING_WORDS = [
    "nachhaltig",   # nur mit nachweisbarer Grundlage
    "natürlich",    # vage claim
    "vegan",        # ok aber Zertifizierung empfohlen
    "fair",
]


# ── ffprobe Helper ────────────────────────────────────────────────────────────

def _get_video_info(path: Path) -> dict:
    """Ruft Metadaten via ffprobe ab. Gibt {} zurück wenn nicht verfügbar."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "quiet",
                "-print_format", "json",
                "-show_streams", "-show_format",
                str(path),
            ],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            return json.loads(result.stdout)
        logger.warning(f"ffprobe Fehler: {result.stderr[:200]}")
    except FileNotFoundError:
        logger.warning("ffprobe nicht installiert — Video-Metadaten-Check übersprungen")
    except Exception as e:
        logger.warning(f"ffprobe fehlgeschlagen für {path.name}: {e}")
    return {}


# ── Video-Validator ───────────────────────────────────────────────────────────

def validate_video(filepath: str) -> dict:
    """
    Prüft ein Video auf Meta-Anforderungen via ffprobe.

    Pflicht (valid=False bei Fehler):
      - Auflösung: exakt 1080×1920
      - Dateigröße: < 4 GB
      - Länge: 3–60 Sekunden
      - Codec: H.264
      - Framerate: 24–30 fps

    Returns: {"valid": bool, "errors": list[str], "warnings": list[str]}
    """
    path   = Path(filepath)
    errors : list[str] = []
    warnings: list[str] = []

    if not path.exists():
        return {"valid": False, "errors": [f"Datei nicht gefunden: {path}"], "warnings": []}

    # Dateigröße
    size_gb = path.stat().st_size / (1024 ** 3)
    if size_gb >= 4.0:
        errors.append(f"Datei zu groß: {size_gb:.2f} GB (Max: 4 GB)")

    info = _get_video_info(path)
    if not info:
        warnings.append("ffprobe nicht verfügbar — Codec/Auflösung/Länge nicht geprüft")
        return {"valid": len(errors) == 0, "errors": errors, "warnings": warnings}

    streams      = info.get("streams", [])
    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    fmt          = info.get("format", {})

    if video_stream:
        # Auflösung
        w = int(video_stream.get("width",  0))
        h = int(video_stream.get("height", 0))
        if w != 1080 or h != 1920:
            errors.append(
                f"Falsche Auflösung: {w}×{h} — Meta erwartet exakt 1080×1920 (9:16)"
            )

        # Codec
        codec = video_stream.get("codec_name", "").lower()
        if codec not in ("h264", "avc", "avc1"):
            errors.append(
                f"Codec '{codec}' nicht unterstützt — H.264 (h264) erforderlich"
            )

        # Framerate
        fps_raw = video_stream.get("r_frame_rate", "0/1")
        try:
            num, den = (int(x) for x in fps_raw.split("/"))
            fps = num / den if den else 0
            if not (24 <= fps <= 30):
                warnings.append(f"Framerate {fps:.1f} fps — empfohlen: 24–30 fps")
        except Exception:
            warnings.append(f"Framerate konnte nicht ermittelt werden ({fps_raw})")

    # Länge
    try:
        duration = float(fmt.get("duration", 0))
        if duration < 3:
            errors.append(f"Video zu kurz: {duration:.1f}s (Min: 3s)")
        elif duration > 60:
            errors.append(f"Video zu lang: {duration:.1f}s (Max: 60s)")
    except (ValueError, TypeError):
        warnings.append("Videolänge konnte nicht ermittelt werden")

    if errors:
        logger.warning(f"❌ {path.name}: {errors}")
    if warnings:
        logger.info(f"⚠️  {path.name}: {warnings}")

    return {"valid": len(errors) == 0, "errors": errors, "warnings": warnings}


# ── Duplikat-Erkennung (MD5) ──────────────────────────────────────────────────

def _load_hash_db() -> dict:
    if HASH_DB_FILE.exists():
        try:
            return json.loads(HASH_DB_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_hash_db(db: dict) -> None:
    HASH_DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    HASH_DB_FILE.write_text(json.dumps(db, indent=2), encoding="utf-8")


def _md5(path: Path, chunk: int = 8192) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        while buf := f.read(chunk):
            h.update(buf)
    return h.hexdigest()


def check_duplicate(filepath: str, state: dict) -> bool:
    """
    Prüft ob das Video bereits hochgeladen wurde (MD5-Hash).
    Registriert neue Hashes automatisch.
    Returns True wenn Duplikat.
    """
    path = Path(filepath)
    db   = _load_hash_db()
    h    = _md5(path)

    # Auch state.active_ads Hashes prüfen (in-memory)
    state_hashes = {a.get("md5") for a in state.get("active_ads", []) if a.get("md5")}
    if h in db or h in state_hashes:
        existing = db.get(h, "bereits hochgeladen")
        logger.warning(f"🔁 Duplikat: '{path.name}' ist identisch mit '{existing}'")
        return True

    db[h] = path.name
    _save_hash_db(db)
    return False


# ── Ad-Text Policy-Check ──────────────────────────────────────────────────────

def check_ad_text(text: str) -> dict:
    """
    Prüft Ad-Text auf Meta-Policy-Verstöße (Alkohol + DE-Recht).

    Returns: {"valid": bool, "found_words": list[str], "warnings": list[str]}
    """
    lower      = text.lower()
    found      : list[str] = []
    warn_found : list[str] = []

    for w in FORBIDDEN_WORDS:
        if w in lower:
            found.append(w)

    for w in WARNING_WORDS:
        if w in lower:
            warn_found.append(w)

    if found:
        logger.warning(f"🚫 Policy-Verstoß im Ad-Text — verbotene Wörter: {found}")
    if warn_found:
        logger.info(f"⚠️  Risikobegriffe im Ad-Text: {warn_found}")

    return {
        "valid":      len(found) == 0,
        "found_words": found,
        "warnings":   warn_found,
    }
