"""
Web-Dashboard — dashboard.py
Passwort-geschütztes Flask-Dashboard für Meta Upload Pipeline.
Start lokal: python dashboard.py
Render:      gunicorn dashboard:app
"""

import os
import json
import logging
from pathlib import Path
from datetime import datetime, timedelta
from functools import wraps

from flask import (
    Flask, render_template_string, request,
    redirect, url_for, session, jsonify, flash
)
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv

load_dotenv()

from settings_manager import settings, BASE_DIR
import safety_monitor

# ── App Setup ─────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key        = os.getenv("FLASK_SECRET_KEY", os.urandom(24).hex())
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=8)

DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "weingut2024")

# Login-Versuch-Tracking (in-memory, reicht für Single-Instance)
_login_attempts: dict = {}  # ip -> (count, last_attempt)

logger = logging.getLogger(__name__)

# ── Auth ──────────────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


def check_password(plain: str) -> bool:
    stored = settings.get("dashboard.password_hash", "")
    if stored:
        return check_password_hash(stored, plain)
    return plain == DASHBOARD_PASSWORD


def is_locked_out(ip: str) -> bool:
    if ip not in _login_attempts:
        return False
    count, last = _login_attempts[ip]
    lockout = int(settings.get("dashboard.login_lockout_minutes", 15))
    if count >= int(settings.get("dashboard.login_attempts_max", 5)):
        if (datetime.now() - last).seconds < lockout * 60:
            return True
        else:
            del _login_attempts[ip]
    return False


def record_failed_login(ip: str):
    count, _ = _login_attempts.get(ip, (0, datetime.now()))
    _login_attempts[ip] = (count + 1, datetime.now())


# ── Daten-Helfer ──────────────────────────────────────────────────────────────

def get_upload_log() -> list:
    log_file = BASE_DIR / "logs" / "upload_log.json"
    if log_file.exists():
        try:
            data = json.loads(log_file.read_text(encoding="utf-8"))
            uploads = data.get("uploads", [])
            return sorted(uploads, key=lambda x: x.get("timestamp", ""), reverse=True)
        except Exception:
            return []
    return []


def get_queue() -> list:
    queue_file = BASE_DIR / "logs" / "upload_queue.json"
    if queue_file.exists():
        try:
            data = json.loads(queue_file.read_text(encoding="utf-8"))
            return [q for q in data if q.get("status") == "pending"]
        except Exception:
            return []
    return []


def get_uploads_today() -> int:
    today = datetime.now().date().isoformat()
    return sum(
        1 for u in get_upload_log()
        if u.get("date") == today and u.get("status") == "success"
    )


def get_approved_count() -> int:
    approved_dir = BASE_DIR / "approved"
    if approved_dir.exists():
        return len(list(approved_dir.glob("*.mp4"))) + len(list(approved_dir.glob("*.MP4")))
    return 0


def get_api_calls_last_hour() -> int:
    api_file = BASE_DIR / "logs" / "api_calls.json"
    if api_file.exists():
        try:
            import time
            data = json.loads(api_file.read_text(encoding="utf-8"))
            cutoff = time.time() - 3600
            return sum(
                1 for c in data.get("calls", [])
                if datetime.fromisoformat(c).timestamp() > cutoff
            )
        except Exception:
            return 0
    return 0


def get_status() -> dict:
    uploads_today = get_uploads_today()
    daily_limit   = settings.get_daily_limit()
    return {
        "uploads_today":        uploads_today,
        "daily_limit":          daily_limit,
        "uploads_percent":      int(uploads_today / max(daily_limit, 1) * 100),
        "api_calls":            get_api_calls_last_hour(),
        "api_limit":            int(settings.get("safety.max_api_calls_per_hour", 200)),
        "api_percent":          int(get_api_calls_last_hour() / max(int(settings.get("safety.max_api_calls_per_hour", 200)), 1) * 100),
        "queue_pending":        len(get_queue()),
        "approved_pending":     get_approved_count(),
        "is_paused":            settings.is_paused(),
        "pause_reason":         settings.get("safety.pause_reason", ""),
        "warmup_mode":          settings.get("safety.warmup_mode", False),
        "health":               safety_monitor.get_health(),
        "health_emoji":         safety_monitor.get_status_emoji(),
        "within_upload_window": settings.is_within_upload_window(),
    }


# ── Templates ─────────────────────────────────────────────────────────────────

BASE_HTML = """<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>🍷 Meta Upload Dashboard</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f4f6f9;color:#333;min-height:100vh}
  nav{background:#1a1a2e;color:#fff;padding:14px 24px;display:flex;align-items:center;gap:20px;flex-wrap:wrap}
  nav .logo{font-size:1.2em;font-weight:700;margin-right:auto}
  nav a{color:#aab4d4;text-decoration:none;padding:6px 12px;border-radius:6px;font-size:.9em}
  nav a:hover,nav a.active{background:#16213e;color:#fff}
  .container{max-width:1100px;margin:30px auto;padding:0 20px}
  .card{background:#fff;border-radius:12px;padding:24px;margin-bottom:20px;box-shadow:0 2px 8px rgba(0,0,0,.08)}
  .card h2{font-size:1em;text-transform:uppercase;letter-spacing:.05em;color:#666;margin-bottom:16px}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:16px}
  .stat{background:#f8f9fc;border-radius:10px;padding:18px;text-align:center}
  .stat .value{font-size:2.2em;font-weight:700;color:#1a1a2e}
  .stat .label{font-size:.8em;color:#888;margin-top:4px}
  .progress{background:#e9ecef;border-radius:99px;height:8px;margin-top:10px;overflow:hidden}
  .progress-bar{height:100%;border-radius:99px;transition:width .3s}
  .bar-green{background:#28a745}.bar-yellow{background:#ffc107}.bar-red{background:#dc3545}
  .badge{display:inline-block;padding:4px 10px;border-radius:99px;font-size:.8em;font-weight:600}
  .badge-green{background:#d4edda;color:#155724}
  .badge-yellow{background:#fff3cd;color:#856404}
  .badge-red{background:#f8d7da;color:#721c24}
  .badge-blue{background:#cce5ff;color:#004085}
  .btn{display:inline-block;padding:10px 20px;border-radius:8px;border:none;cursor:pointer;font-size:.9em;font-weight:600;text-decoration:none;transition:.2s}
  .btn-primary{background:#1a1a2e;color:#fff}.btn-primary:hover{background:#16213e}
  .btn-danger{background:#dc3545;color:#fff}.btn-danger:hover{background:#c82333}
  .btn-success{background:#28a745;color:#fff}.btn-success:hover{background:#218838}
  .btn-warning{background:#ffc107;color:#333}.btn-warning:hover{background:#e0a800}
  .btn-sm{padding:6px 12px;font-size:.8em}
  table{width:100%;border-collapse:collapse;font-size:.9em}
  th{text-align:left;padding:10px 12px;border-bottom:2px solid #e9ecef;color:#666;font-size:.8em;text-transform:uppercase}
  td{padding:10px 12px;border-bottom:1px solid #f0f0f0}
  tr:last-child td{border-bottom:none}
  .status-ok{color:#28a745;font-weight:600}.status-err{color:#dc3545;font-weight:600}
  .alert{padding:12px 16px;border-radius:8px;margin-bottom:16px;font-size:.9em}
  .alert-success{background:#d4edda;color:#155724;border:1px solid #c3e6cb}
  .alert-danger{background:#f8d7da;color:#721c24;border:1px solid #f5c6cb}
  .alert-warning{background:#fff3cd;color:#856404;border:1px solid #ffeeba}
  form .field{margin-bottom:16px}
  form label{display:block;font-size:.85em;font-weight:600;color:#555;margin-bottom:6px}
  form input[type=text],form input[type=password],form input[type=number],form select{
    width:100%;padding:10px 12px;border:1px solid #ddd;border-radius:8px;font-size:.95em}
  form input:focus,form select:focus{outline:none;border-color:#1a1a2e;box-shadow:0 0 0 3px rgba(26,26,46,.1)}
  .section-title{font-size:.8em;text-transform:uppercase;letter-spacing:.1em;color:#999;border-bottom:1px solid #eee;padding-bottom:8px;margin:24px 0 16px}
  .toggle{display:flex;align-items:center;gap:10px;margin-bottom:12px}
  .toggle input[type=checkbox]{width:18px;height:18px;cursor:pointer}
  .pause-banner{background:#dc3545;color:#fff;padding:14px 24px;text-align:center;font-weight:600}
  @media(max-width:600px){.grid{grid-template-columns:1fr}.container{padding:0 12px}}
</style>
</head>
<body>
{% if paused %}
<div class="pause-banner">🛑 PAUSE AKTIV: {{ pause_reason }}</div>
{% endif %}
<nav>
  <span class="logo">🍷 Meta Upload</span>
  <a href="/dashboard" {% if active=='dashboard' %}class="active"{% endif %}>Dashboard</a>
  <a href="/einstellungen" {% if active=='settings' %}class="active"{% endif %}>Einstellungen</a>
  <a href="/logs" {% if active=='logs' %}class="active"{% endif %}>Logs</a>
  <a href="/logout" style="margin-left:auto;color:#f88">Abmelden</a>
</nav>
<div class="container">
{% with messages = get_flashed_messages(with_categories=true) %}
  {% for cat, msg in messages %}
    <div class="alert alert-{{ cat }}">{{ msg }}</div>
  {% endfor %}
{% endwith %}
{{ content | safe }}
</div>
<script>
// Auto-Refresh Status alle 30 Sekunden
{% if auto_refresh %}
setInterval(() => {
  fetch('/api/status').then(r => r.json()).then(d => {
    const ud = document.getElementById('uploads-today');
    if(ud) ud.textContent = d.uploads_today + '/' + d.daily_limit;
    const ap = document.getElementById('api-calls');
    if(ap) ap.textContent = d.api_calls + '/' + d.api_limit;
    const ql = document.getElementById('queue-count');
    if(ql) ql.textContent = d.queue_pending;
  }).catch(()=>{});
}, 30000);
{% endif %}

// Pause-Toggle
function togglePause() {
  const btn = document.getElementById('pause-btn');
  if(!btn) return;
  fetch('/api/pause', {method:'POST', headers:{'Content-Type':'application/json'},
    body:JSON.stringify({reason:'Manuell über Dashboard'})})
  .then(r=>r.json()).then(d => { location.reload(); }).catch(()=>{});
}
</script>
</body>
</html>"""


LOGIN_HTML = """
<div style="max-width:400px;margin:80px auto">
<div class="card" style="text-align:center">
  <h1 style="margin-bottom:8px;font-size:2em">🍷</h1>
  <h2 style="margin-bottom:24px;font-size:1.3em;color:#1a1a2e">Meta Upload Dashboard</h2>
  {% if error %}<div class="alert alert-danger">{{ error }}</div>{% endif %}
  <form method="POST">
    <div class="field" style="text-align:left">
      <label>Passwort</label>
      <input type="password" name="password" autofocus required placeholder="Dashboard-Passwort">
    </div>
    <button type="submit" class="btn btn-primary" style="width:100%">Anmelden</button>
  </form>
</div>
</div>
"""


def render_page(content: str, active: str = "", auto_refresh: bool = False) -> str:
    s = settings.get("safety", {})
    return render_template_string(
        BASE_HTML,
        content=content,
        active=active,
        auto_refresh=auto_refresh,
        paused=settings.is_paused(),
        pause_reason=settings.get("safety.pause_reason", ""),
    )


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return redirect(url_for("dashboard"))


@app.route("/login", methods=["GET", "POST"])
def login():
    error = ""
    ip = request.remote_addr

    if request.method == "POST":
        if is_locked_out(ip):
            error = f"Zu viele Fehlversuche. Bitte warte {settings.get('dashboard.login_lockout_minutes', 15)} Minuten."
        elif check_password(request.form.get("password", "")):
            session.permanent = True
            session["logged_in"] = True
            _login_attempts.pop(ip, None)
            return redirect(url_for("dashboard"))
        else:
            record_failed_login(ip)
            error = "Falsches Passwort."

    return render_template_string(BASE_HTML, content=LOGIN_HTML, active="login",
                                  auto_refresh=False, paused=False, pause_reason="",
                                  error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/dashboard")
@login_required
def dashboard():
    st     = get_status()
    health = st["health"]
    logs   = get_upload_log()[:10]

    # Ampel-Farbe
    h_status = health.get("health_status", "green")
    h_color  = {"green": "badge-green", "yellow": "badge-yellow", "red": "badge-red"}.get(h_status, "badge-green")
    ul_color = "bar-green" if st["uploads_percent"] < 80 else ("bar-yellow" if st["uploads_percent"] < 100 else "bar-red")
    api_color = "bar-green" if st["api_percent"] < 80 else ("bar-yellow" if st["api_percent"] < 100 else "bar-red")

    log_rows = ""
    for u in logs:
        status_cls = "status-ok" if u.get("status") == "success" else "status-err"
        status_txt = "✅ OK" if u.get("status") == "success" else "❌ Fehler"
        ts = u.get("timestamp", "")[:16].replace("T", " ")
        vid_id = u.get("meta_video_id", "—")
        err    = u.get("error", "")[:60] or "—"
        name   = u.get("filename", "—")
        log_rows += f"""<tr>
          <td>{ts}</td>
          <td>{name}</td>
          <td class="{status_cls}">{status_txt}</td>
          <td style="font-family:monospace;font-size:.8em">{vid_id}</td>
          <td style="color:#999;font-size:.8em">{err}</td>
        </tr>"""

    pause_btn_label = "▶️ Fortsetzen" if st["is_paused"] else "⏸️ Pause aktivieren"
    pause_btn_cls   = "btn-success" if st["is_paused"] else "btn-danger"

    window_info = "✅ Im Upload-Fenster" if st["within_upload_window"] else "🚫 Außerhalb Upload-Fenster"
    warmup_badge = '<span class="badge badge-blue">WARMUP MODE</span>' if st["warmup_mode"] else ""

    content = f"""
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:20px;flex-wrap:wrap;gap:10px">
      <h1 style="font-size:1.4em">Übersicht {warmup_badge}</h1>
      <div style="display:flex;gap:10px">
        <button onclick="togglePause()" id="pause-btn" class="btn {pause_btn_cls}">{pause_btn_label}</button>
      </div>
    </div>

    <div class="grid">
      <div class="stat">
        <div class="value" id="uploads-today">{st['uploads_today']}/{st['daily_limit']}</div>
        <div class="label">Uploads heute</div>
        <div class="progress"><div class="progress-bar {ul_color}" style="width:{min(st['uploads_percent'],100)}%"></div></div>
      </div>
      <div class="stat">
        <div class="value" id="api-calls">{st['api_calls']}/{st['api_limit']}</div>
        <div class="label">API Calls / Stunde</div>
        <div class="progress"><div class="progress-bar {api_color}" style="width:{min(st['api_percent'],100)}%"></div></div>
      </div>
      <div class="stat">
        <div class="value" id="queue-count">{st['queue_pending']}</div>
        <div class="label">In Queue</div>
      </div>
      <div class="stat">
        <div class="value">{st['approved_pending']}</div>
        <div class="label">Freigegeben (warten)</div>
      </div>
    </div>

    <div class="card" style="margin-top:20px">
      <h2>Account Health</h2>
      <div style="display:flex;align-items:center;gap:16px;flex-wrap:wrap">
        <span style="font-size:2em">{st['health_emoji']}</span>
        <div>
          <span class="badge {h_color}">{h_status.upper()}</span>
          <div style="font-size:.85em;color:#666;margin-top:4px">
            Aufeinanderfolgende Fehler: {health.get('consecutive_errors', 0)} |
            Fehler heute: {health.get('total_errors_today', 0)} |
            {window_info}
          </div>
        </div>
        {'<div class="alert alert-danger" style="margin:0">' + st['pause_reason'] + '</div>' if st['is_paused'] else ''}
      </div>
    </div>

    <div class="card">
      <h2>Letzte Uploads</h2>
      <table>
        <thead><tr><th>Zeit</th><th>Datei</th><th>Status</th><th>Meta Video-ID</th><th>Info</th></tr></thead>
        <tbody>{log_rows if log_rows else '<tr><td colspan="5" style="text-align:center;color:#999">Noch keine Uploads</td></tr>'}</tbody>
      </table>
    </div>
    """
    return render_page(content, active="dashboard", auto_refresh=True)


@app.route("/einstellungen", methods=["GET", "POST"])
@login_required
def einstellungen():
    if request.method == "POST":
        action = request.form.get("action", "save")

        if action == "change_password":
            new_pw = request.form.get("new_password", "").strip()
            if len(new_pw) < 6:
                flash("Passwort muss mindestens 6 Zeichen haben.", "danger")
            else:
                hashed = generate_password_hash(new_pw)
                settings.set("dashboard.password_hash", hashed, "dashboard")
                flash("Passwort erfolgreich geändert.", "success")

        elif action == "reset":
            from settings_manager import DEFAULTS
            settings.update_section("safety", DEFAULTS["safety"], "dashboard")
            flash("Einstellungen auf Standard zurückgesetzt.", "success")

        else:
            # Safety-Einstellungen speichern
            fields = {
                "safety.max_uploads_per_day":      ("max_uploads_per_day",     int),
                "safety.min_delay_seconds":         ("min_delay_seconds",        int),
                "safety.max_delay_seconds":         ("max_delay_seconds",        int),
                "safety.max_api_calls_per_hour":    ("max_api_calls_per_hour",  int),
                "safety.slowdown_threshold":        ("slowdown_threshold",       int),
                "safety.max_errors_before_pause":   ("max_errors_before_pause", int),
                "safety.warmup_uploads_per_day":    ("warmup_uploads_per_day",  int),
            }
            errors = []
            for key_path, (field_name, typ) in fields.items():
                val = request.form.get(field_name)
                if val is not None:
                    ok, msg = settings.validate_and_set(key_path, val, "dashboard")
                    if not ok:
                        errors.append(f"{field_name}: {msg}")

            # Boolean-Felder
            settings.set("safety.warmup_mode",           "warmup_mode"           in request.form, "dashboard")
            settings.set("safety.upload_window_enabled", "upload_window_enabled" in request.form, "dashboard")

            # Zeitfenster
            win_start = request.form.get("upload_window_start", "08:00")
            win_end   = request.form.get("upload_window_end",   "21:00")
            settings.set("safety.upload_window_start", win_start, "dashboard")
            settings.set("safety.upload_window_end",   win_end,   "dashboard")

            # Report-Settings
            settings.set("schedule.daily_report_enabled",  "daily_report_enabled"  in request.form, "dashboard")
            settings.set("schedule.weekly_report_enabled", "weekly_report_enabled" in request.form, "dashboard")
            rt = request.form.get("daily_report_time", "08:00")
            settings.set("schedule.daily_report_time", rt, "dashboard")

            # Geo-Ausschluss
            settings.set("geo_exclusion.enabled",       "geo_exclusion_enabled" in request.form, "dashboard")
            settings.set("geo_exclusion.location_name", request.form.get("geo_location_name", "Gundersheim"), "dashboard")
            settings.set("geo_exclusion.zip_code",      request.form.get("geo_zip_code", "67598"), "dashboard")
            settings.set("geo_exclusion.country",       request.form.get("geo_country", "DE"), "dashboard")
            try:
                settings.set("geo_exclusion.radius_km", int(request.form.get("geo_radius_km", 25)), "dashboard")
                settings.set("geo_exclusion.latitude",  float(request.form.get("geo_latitude", 49.7153)), "dashboard")
                settings.set("geo_exclusion.longitude", float(request.form.get("geo_longitude", 8.2175)), "dashboard")
            except (ValueError, TypeError):
                errors.append("Geo-Koordinaten: Ungültige Werte")

            if errors:
                flash("Fehler: " + " | ".join(errors), "danger")
            else:
                flash("Einstellungen gespeichert.", "success")

        return redirect(url_for("einstellungen"))

    s = settings.get_all()
    sf = s.get("safety", {})
    sc = s.get("schedule", {})

    def checked(val):
        return "checked" if val else ""

    content = f"""
    <h1 style="margin-bottom:24px;font-size:1.4em">Einstellungen</h1>

    <form method="POST">
      <input type="hidden" name="action" value="save">
      <div class="card">
        <div class="section-title">Safety — Upload-Limits</div>
        <div class="grid">
          <div class="field">
            <label>Max Uploads pro Tag (1–20)</label>
            <input type="number" name="max_uploads_per_day" value="{sf.get('max_uploads_per_day', 5)}" min="1" max="20">
          </div>
          <div class="field">
            <label>Min Delay zwischen Uploads (Sek, 30–600)</label>
            <input type="number" name="min_delay_seconds" value="{sf.get('min_delay_seconds', 120)}" min="30" max="600">
          </div>
          <div class="field">
            <label>Max Delay zwischen Uploads (Sek, 60–900)</label>
            <input type="number" name="max_delay_seconds" value="{sf.get('max_delay_seconds', 180)}" min="60" max="900">
          </div>
          <div class="field">
            <label>Max API Calls / Stunde (50–500)</label>
            <input type="number" name="max_api_calls_per_hour" value="{sf.get('max_api_calls_per_hour', 200)}" min="50" max="500">
          </div>
          <div class="field">
            <label>Slowdown-Schwelle API Calls (50–499)</label>
            <input type="number" name="slowdown_threshold" value="{sf.get('slowdown_threshold', 180)}" min="50" max="499">
          </div>
          <div class="field">
            <label>Fehler bis Auto-Pause (1–10)</label>
            <input type="number" name="max_errors_before_pause" value="{sf.get('max_errors_before_pause', 3)}" min="1" max="10">
          </div>
        </div>

        <div class="section-title">Warmup-Modus</div>
        <div class="toggle">
          <input type="checkbox" name="warmup_mode" id="warmup_mode" {checked(sf.get('warmup_mode', False))}>
          <label for="warmup_mode">Warmup-Modus aktiv (schonender Start für neuen Account)</label>
        </div>
        <div class="field" style="max-width:300px">
          <label>Max Uploads/Tag im Warmup (1–10)</label>
          <input type="number" name="warmup_uploads_per_day" value="{sf.get('warmup_uploads_per_day', 2)}" min="1" max="10">
        </div>

        <div class="section-title">Upload-Zeitfenster</div>
        <div class="toggle">
          <input type="checkbox" name="upload_window_enabled" id="upload_window_enabled" {checked(sf.get('upload_window_enabled', False))}>
          <label for="upload_window_enabled">Nur innerhalb bestimmter Uhrzeiten hochladen</label>
        </div>
        <div class="grid" style="max-width:500px">
          <div class="field">
            <label>Von</label>
            <input type="text" name="upload_window_start" value="{sf.get('upload_window_start', '08:00')}" placeholder="08:00">
          </div>
          <div class="field">
            <label>Bis</label>
            <input type="text" name="upload_window_end" value="{sf.get('upload_window_end', '21:00')}" placeholder="21:00">
          </div>
        </div>

        <div class="section-title">Tägliche Reports (Telegram)</div>
        <div class="toggle">
          <input type="checkbox" name="daily_report_enabled" id="daily_report_enabled" {checked(sc.get('daily_report_enabled', True))}>
          <label for="daily_report_enabled">Täglichen Bericht per Telegram senden</label>
        </div>
        <div class="toggle">
          <input type="checkbox" name="weekly_report_enabled" id="weekly_report_enabled" {checked(sc.get('weekly_report_enabled', True))}>
          <label for="weekly_report_enabled">Wöchentlichen Bericht (montags) per Telegram senden</label>
        </div>
        <div class="field" style="max-width:200px">
          <label>Uhrzeit Tagesbericht</label>
          <input type="text" name="daily_report_time" value="{sc.get('daily_report_time', '08:00')}" placeholder="08:00">
        </div>
      </div>

        <div class="section-title">📍 Geo-Ausschluss (kein Targeting im Heimatort)</div>
        <div class="toggle">
          <input type="checkbox" name="geo_exclusion_enabled" id="geo_exclusion_enabled" {checked(s.get('geo_exclusion', {{}}).get('enabled', False))}>
          <label for="geo_exclusion_enabled">Bestimmten Umkreis von Werbung ausschließen</label>
        </div>
        <div style="background:#fff8e1;border:1px solid #ffe082;border-radius:6px;padding:12px;margin:8px 0;font-size:.85em;color:#555">
          ℹ️ Mit dieser Einstellung werden keine Anzeigen an Personen in deiner Nähe ausgespielt.
          Wird beim Erstellen von Ad Sets automatisch als Geo-Ausschluss übergeben.
        </div>
        <div class="grid">
          <div class="field">
            <label>Ortsname</label>
            <input type="text" name="geo_location_name" value="{s.get('geo_exclusion', {{}}).get('location_name', 'Gundersheim')}" placeholder="Gundersheim">
          </div>
          <div class="field">
            <label>PLZ</label>
            <input type="text" name="geo_zip_code" value="{s.get('geo_exclusion', {{}}).get('zip_code', '67598')}" placeholder="67598">
          </div>
          <div class="field">
            <label>Land (ISO-Code)</label>
            <input type="text" name="geo_country" value="{s.get('geo_exclusion', {{}}).get('country', 'DE')}" placeholder="DE" style="max-width:80px">
          </div>
          <div class="field">
            <label>Radius (km, 1–80)</label>
            <input type="number" name="geo_radius_km" value="{s.get('geo_exclusion', {{}}).get('radius_km', 25)}" min="1" max="80">
          </div>
          <div class="field">
            <label>Breitengrad (Latitude)</label>
            <input type="text" name="geo_latitude" value="{s.get('geo_exclusion', {{}}).get('latitude', 49.7153)}" placeholder="49.7153">
          </div>
          <div class="field">
            <label>Längengrad (Longitude)</label>
            <input type="text" name="geo_longitude" value="{s.get('geo_exclusion', {{}}).get('longitude', 8.2175)}" placeholder="8.2175">
          </div>
        </div>

      <div style="display:flex;gap:12px;flex-wrap:wrap">
        <button type="submit" class="btn btn-primary">💾 Speichern</button>
        <button type="submit" name="action" value="reset" class="btn btn-warning"
          onclick="return confirm('Wirklich auf Standard zurücksetzen?')">↩️ Auf Standard zurücksetzen</button>
      </div>
    </form>

    <div class="card" style="margin-top:24px">
      <div class="section-title">Passwort ändern</div>
      <form method="POST" style="max-width:400px">
        <input type="hidden" name="action" value="change_password">
        <div class="field">
          <label>Neues Passwort (min. 6 Zeichen)</label>
          <input type="password" name="new_password" placeholder="Neues Passwort">
        </div>
        <button type="submit" class="btn btn-primary btn-sm">Passwort ändern</button>
      </form>
    </div>
    """
    return render_page(content, active="settings")


@app.route("/logs")
@login_required
def logs_page():
    uploads   = get_upload_log()[:50]
    filter_by = request.args.get("filter", "all")
    if filter_by == "errors":
        uploads = [u for u in uploads if u.get("status") != "success"]
    elif filter_by == "success":
        uploads = [u for u in uploads if u.get("status") == "success"]

    rows = ""
    for u in uploads:
        status_cls = "status-ok" if u.get("status") == "success" else "status-err"
        status_txt = "✅ OK" if u.get("status") == "success" else "❌ Fehler"
        ts = u.get("timestamp", "")[:16].replace("T", " ")
        rows += f"""<tr>
          <td>{u.get('date','—')}</td>
          <td>{ts.split(' ')[1] if ' ' in ts else ts}</td>
          <td>{u.get('filename','—')}</td>
          <td class="{status_cls}">{status_txt}</td>
          <td style="font-family:monospace;font-size:.8em">{u.get('meta_video_id','—')}</td>
          <td style="color:#999;font-size:.8em">{(u.get('error','') or '—')[:80]}</td>
        </tr>"""

    content = f"""
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:20px;flex-wrap:wrap;gap:10px">
      <h1 style="font-size:1.4em">Upload-Logs</h1>
      <div style="display:flex;gap:8px">
        <a href="/logs" class="btn btn-sm {'btn-primary' if filter_by=='all' else ''}">Alle</a>
        <a href="/logs?filter=success" class="btn btn-sm btn-success {'btn-primary' if filter_by=='success' else ''}">✅ Erfolge</a>
        <a href="/logs?filter=errors" class="btn btn-sm btn-danger">❌ Fehler</a>
      </div>
    </div>
    <div class="card">
      <table>
        <thead><tr><th>Datum</th><th>Uhrzeit</th><th>Datei</th><th>Status</th><th>Meta Video-ID</th><th>Fehler</th></tr></thead>
        <tbody>{rows if rows else '<tr><td colspan="6" style="text-align:center;color:#999">Keine Einträge</td></tr>'}</tbody>
      </table>
    </div>
    """
    return render_page(content, active="logs")


# ── API Endpoints ─────────────────────────────────────────────────────────────

@app.route("/api/status")
@login_required
def api_status():
    return jsonify(get_status())


@app.route("/api/health")
@login_required
def api_health():
    return jsonify({
        "health":  safety_monitor.get_health(),
        "emoji":   safety_monitor.get_status_emoji(),
        "summary": safety_monitor.get_summary(),
    })


@app.route("/api/queue")
@login_required
def api_queue():
    return jsonify({"queue": get_queue()})


@app.route("/api/pause", methods=["POST"])
@login_required
def api_pause():
    data   = request.get_json(silent=True) or {}
    reason = data.get("reason", "Manuell über Dashboard pausiert")
    if settings.is_paused():
        settings.disable_pause(by="dashboard")
        return jsonify({"status": "resumed", "paused": False})
    else:
        settings.enable_pause(reason, by="dashboard")
        return jsonify({"status": "paused", "paused": True})


@app.route("/api/settings")
@login_required
def api_settings():
    return jsonify(settings.get_all())


# ── Start ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    debug = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    print(f"🍷 Dashboard startet auf http://localhost:{port}")
    print(f"   Passwort: {DASHBOARD_PASSWORD}")
    app.run(host="0.0.0.0", port=port, debug=debug)
