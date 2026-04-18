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
  <a href="/kpi" {% if active=='kpi' %}class="active"{% endif %}>📊 KPI-Analyse</a>
  <a href="/texte" {% if active=='texte' %}class="active"{% endif %}>✍️ Ad-Texte</a>
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

            # Kampagnen-Einstellungen
            active_campaign = request.form.get("active_campaign", "")
            if active_campaign:
                settings.set("campaigns.active", active_campaign, "dashboard")
            try:
                aov = int(request.form.get("average_order_value", 80))
                settings.set("campaigns.average_order_value", aov, "dashboard")
            except (ValueError, TypeError):
                pass
            # Kampagnen-IDs + per-campaign Budget/Limits
            campaigns_list = settings.get("campaigns.list", [])
            for camp in campaigns_list:
                cid = camp["id"]
                for field, key, typ in [
                    (f"meta_campaign_id_{cid}", "meta_campaign_id", str),
                    (f"daily_budget_{cid}",     "daily_budget",     str),
                    (f"budget_per_ad_{cid}",    "budget_per_ad_eur", float),
                    (f"warmup_budget_{cid}",    "warmup_budget_eur", float),
                    (f"spend_cap_{cid}",        "spend_cap_eur",    float),
                    (f"min_roas_{cid}",         "min_roas",         float),
                    (f"max_active_{cid}",       "max_active",       int),
                ]:
                    val = request.form.get(field, "").strip()
                    if val:
                        try:
                            camp[key] = typ(val)
                        except (ValueError, TypeError):
                            errors.append(f"{field}: ungültiger Wert")
            settings.set("campaigns.list", campaigns_list, "dashboard")

            # Optimizer-Logik
            for key_path, field_name in [
                ("optimizer.freeze_days",           "freeze_days"),
                ("optimizer.max_active_ads",        "max_active_ads"),
                ("optimizer.batch_size",            "batch_size"),
                ("optimizer.bottom_n",              "bottom_n"),
                ("optimizer.scale_after_cycles",    "scale_after_cycles"),
                ("optimizer.min_impressions",       "min_impressions"),
                ("optimizer.conversions_trigger",   "conversions_trigger"),
                ("optimizer.upload_delay_seconds",  "upload_delay_seconds"),
            ]:
                val = request.form.get(field_name)
                if val is not None:
                    ok, msg = settings.validate_and_set(key_path, val, "dashboard")
                    if not ok:
                        errors.append(f"{field_name}: {msg}")

            # KPI-Bewertung
            for key_path, field_name in [
                ("kpi.hook_rate_min",       "hook_rate_min"),
                ("kpi.ctr_min",             "ctr_min"),
                ("kpi.cpm_malus_threshold", "cpm_malus_threshold"),
            ]:
                val = request.form.get(field_name)
                if val is not None:
                    ok, msg = settings.validate_and_set(key_path, val, "dashboard")
                    if not ok:
                        errors.append(f"{field_name}: {msg}")
            for key_path, field_name in [
                ("kpi.hook_rate_weight", "hook_rate_weight"),
                ("kpi.ctr_weight",       "ctr_weight"),
                ("kpi.cpm_weight",       "cpm_weight"),
            ]:
                val = request.form.get(field_name)
                if val is not None:
                    try:
                        settings.set(key_path, float(val), "dashboard")
                    except (ValueError, TypeError):
                        errors.append(f"{field_name}: muss eine Zahl sein")

            # Budget & Skalierung
            for key_path, field_name in [
                ("budget.budget_per_ad_eur",    "budget_per_ad_eur"),
                ("budget.warmup_budget_eur",    "warmup_budget_eur"),
                ("budget.warmup_hours",         "warmup_hours"),
                ("budget.scale_budget_eur",     "scale_budget_eur"),
                ("budget.daily_spend_cap_eur",  "daily_spend_cap_eur"),
                ("budget.max_scale_multiplier", "max_scale_multiplier"),
            ]:
                val = request.form.get(field_name)
                if val is not None:
                    ok, msg = settings.validate_and_set(key_path, val, "dashboard")
                    if not ok:
                        errors.append(f"{field_name}: {msg}")

            # ROAS & Schutz
            for key_path, field_name in [
                ("roas.min_roas",                    "min_roas"),
                ("roas.roas_pause_days",             "roas_pause_days"),
                ("guard.anomaly_cpm_factor",         "anomaly_cpm_factor"),
                ("guard.max_rejections_24h",         "max_rejections_24h"),
                ("guard.max_api_errors_before_pause","max_api_errors_before_pause"),
            ]:
                val = request.form.get(field_name)
                if val is not None:
                    ok, msg = settings.validate_and_set(key_path, val, "dashboard")
                    if not ok:
                        errors.append(f"{field_name}: {msg}")

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
    sf  = s.get("safety", {})
    sc  = s.get("schedule", {})
    sg  = s.get("geo_exclusion", {})
    sop = s.get("optimizer", {})
    skp = s.get("kpi", {})
    sbg = s.get("budget", {})
    srs = s.get("roas", {})
    sgu = s.get("guard", {})
    scampaigns  = s.get("campaigns", {})
    campaigns_list  = scampaigns.get("list", [])
    active_campaign = scampaigns.get("active", "testing_inhouse")

    # Pre-compute campaign cards to avoid nested f-string (Python 3.11 incompatible)
    _campaign_cards = ""
    for _c in campaigns_list:
        _campaign_cards += (
            f'<div class="card" style="background:#f8f9fc;padding:16px">'
            f'<div style="font-weight:600;margin-bottom:10px">{_c["name"]}</div>'
            f'<div class="field"><label>Meta Kampagnen-ID</label>'
            f'<input type="text" name="meta_campaign_id_{_c["id"]}" value="{_c.get("meta_campaign_id", "")}" placeholder="123456789"></div>'
            f'<div class="field"><label>Tagesbudget gesamt (EUR)</label>'
            f'<input type="text" name="daily_budget_{_c["id"]}" value="{_c.get("daily_budget", "10")}" placeholder="10"></div>'
            f'<div class="field"><label>Budget pro Ad / Tag (EUR)</label>'
            f'<input type="number" name="budget_per_ad_{_c["id"]}" value="{_c.get("budget_per_ad_eur", 5.0)}" min="1" max="500" step="0.5"></div>'
            f'<div class="field"><label>Warmup-Budget neue Ads (EUR)</label>'
            f'<input type="number" name="warmup_budget_{_c["id"]}" value="{_c.get("warmup_budget_eur", 3.0)}" min="1" max="100" step="0.5"></div>'
            f'<div class="field"><label>Spend-Cap / Tag (EUR)</label>'
            f'<input type="number" name="spend_cap_{_c["id"]}" value="{_c.get("spend_cap_eur", 100.0)}" min="1" max="10000"></div>'
            f'<div class="field"><label>Mindest-ROAS</label>'
            f'<input type="number" name="min_roas_{_c["id"]}" value="{_c.get("min_roas", 2.0)}" min="0" max="20" step="0.1"></div>'
            f'<div class="field"><label>Max. aktive Ads</label>'
            f'<input type="number" name="max_active_{_c["id"]}" value="{_c.get("max_active", 15)}" min="1" max="50"></div>'
            f'</div>'
        )

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

      <div class="card">
        <div class="section-title">🎯 Kampagnen-Einstellungen</div>
        <div class="grid">
          <div class="field">
            <label>Aktive Kampagne</label>
            <select name="active_campaign">
              {''.join(f'<option value="{c["id"]}" {"selected" if c["id"] == active_campaign else ""}>{c["name"]} — {c["description"]}</option>' for c in campaigns_list)}
            </select>
          </div>
          <div class="field">
            <label>Ø Bestellwert (AOV in EUR)</label>
            <input type="number" name="average_order_value" value="{scampaigns.get('average_order_value', 80)}" min="1" max="9999">
          </div>
        </div>
        <div style="background:#f0f4ff;border:1px solid #c7d2fe;border-radius:6px;padding:12px;margin:12px 0;font-size:.85em;color:#555">
          ℹ️ Die aktive Kampagne bestimmt, welcher Meta-Kampagne neue Videos automatisch zugeordnet werden.
          Meta Kampagnen-IDs eingetragen → werden beim Upload direkt genutzt.
        </div>
        <div class="section-title" style="margin-top:8px">Meta Kampagnen-IDs &amp; Budgets</div>
        <div class="grid">
          {_campaign_cards}
        </div>
      </div>

      <div class="card">
        <div class="section-title">📍 Geo-Ausschluss (kein Targeting im Heimatort)</div>
        <div class="toggle">
          <input type="checkbox" name="geo_exclusion_enabled" id="geo_exclusion_enabled" {checked(sg.get('enabled', False))}>
          <label for="geo_exclusion_enabled">Bestimmten Umkreis von Werbung ausschließen</label>
        </div>
        <div style="background:#fff8e1;border:1px solid #ffe082;border-radius:6px;padding:12px;margin:8px 0;font-size:.85em;color:#555">
          ℹ️ Mit dieser Einstellung werden keine Anzeigen an Personen in deiner Nähe ausgespielt.
          Wird beim Erstellen von Ad Sets automatisch als Geo-Ausschluss übergeben.
        </div>
        <div class="grid">
          <div class="field">
            <label>Ortsname</label>
            <input type="text" name="geo_location_name" value="{sg.get('location_name', 'Gundersheim')}" placeholder="Gundersheim">
          </div>
          <div class="field">
            <label>PLZ</label>
            <input type="text" name="geo_zip_code" value="{sg.get('zip_code', '67598')}" placeholder="67598">
          </div>
          <div class="field">
            <label>Land (ISO-Code)</label>
            <input type="text" name="geo_country" value="{sg.get('country', 'DE')}" placeholder="DE" style="max-width:80px">
          </div>
          <div class="field">
            <label>Radius (km, 1–80)</label>
            <input type="number" name="geo_radius_km" value="{sg.get('radius_km', 25)}" min="1" max="80">
          </div>
          <div class="field">
            <label>Breitengrad (Latitude)</label>
            <input type="text" name="geo_latitude" value="{sg.get('latitude', 49.7153)}" placeholder="49.7153">
          </div>
          <div class="field">
            <label>Längengrad (Longitude)</label>
            <input type="text" name="geo_longitude" value="{sg.get('longitude', 8.2175)}" placeholder="8.2175">
          </div>
        </div>
      </div>

      <div class="card">
        <div class="section-title">⚙️ Optimizer-Logik</div>
        <div class="grid">
          <div class="field"><label>Freeze-Tage (1–30)</label>
            <input type="number" name="freeze_days" value="{sop.get('freeze_days', 7)}" min="1" max="30"></div>
          <div class="field"><label>Max. aktive Ads gesamt</label>
            <input type="number" name="max_active_ads" value="{sop.get('max_active_ads', 15)}" min="1" max="50"></div>
          <div class="field"><label>Batch-Größe (Ads pro Lauf)</label>
            <input type="number" name="batch_size" value="{sop.get('batch_size', 5)}" min="1" max="10"></div>
          <div class="field"><label>Bottom-N pausieren pro Rotation</label>
            <input type="number" name="bottom_n" value="{sop.get('bottom_n', 3)}" min="1" max="10"></div>
          <div class="field"><label>Zyklen bis Scale-Alert</label>
            <input type="number" name="scale_after_cycles" value="{sop.get('scale_after_cycles', 3)}" min="1" max="20"></div>
          <div class="field"><label>Min. Impressionen für Auswertung</label>
            <input type="number" name="min_impressions" value="{sop.get('min_impressions', 500)}" min="50" max="5000"></div>
          <div class="field"><label>Conversion-Trigger (Freeze-Früh-Ausstieg)</label>
            <input type="number" name="conversions_trigger" value="{sop.get('conversions_trigger', 50)}" min="1" max="500"></div>
          <div class="field"><label>Delay zwischen Uploads (Sek)</label>
            <input type="number" name="upload_delay_seconds" value="{sop.get('upload_delay_seconds', 45)}" min="10" max="300"></div>
        </div>
      </div>

      <div class="card">
        <div class="section-title">📊 KPI-Bewertung</div>
        <div style="background:#f0f4ff;border:1px solid #c7d2fe;border-radius:6px;padding:10px;margin-bottom:12px;font-size:.85em;color:#555">
          Gewichtungen sollten zusammen 1.0 (100%) ergeben.
        </div>
        <div class="grid">
          <div class="field"><label>Hook Rate — Gewichtung (z.B. 0.40)</label>
            <input type="text" name="hook_rate_weight" value="{skp.get('hook_rate_weight', 0.40)}"></div>
          <div class="field"><label>CTR — Gewichtung (z.B. 0.40)</label>
            <input type="text" name="ctr_weight" value="{skp.get('ctr_weight', 0.40)}"></div>
          <div class="field"><label>CPM — Gewichtung (z.B. 0.20)</label>
            <input type="text" name="cpm_weight" value="{skp.get('cpm_weight', 0.20)}"></div>
          <div class="field"><label>Hook Rate Mindestwert (z.B. 0.25 = 25%)</label>
            <input type="text" name="hook_rate_min" value="{skp.get('hook_rate_min', 0.25)}"></div>
          <div class="field"><label>CTR Mindestwert (z.B. 0.01 = 1%)</label>
            <input type="text" name="ctr_min" value="{skp.get('ctr_min', 0.01)}"></div>
          <div class="field"><label>CPM Malus-Schwelle (EUR)</label>
            <input type="number" name="cpm_malus_threshold" value="{skp.get('cpm_malus_threshold', 18.0)}" min="1" max="100"></div>
        </div>
      </div>

      <div class="card">
        <div class="section-title">💰 Budget & Skalierung (globale Defaults)</div>
        <div class="grid">
          <div class="field"><label>Budget pro Ad nach Warmup (EUR)</label>
            <input type="number" name="budget_per_ad_eur" value="{sbg.get('budget_per_ad_eur', 5.0)}" min="1" max="100" step="0.5"></div>
          <div class="field"><label>Warmup-Budget neue Ads (EUR)</label>
            <input type="number" name="warmup_budget_eur" value="{sbg.get('warmup_budget_eur', 3.0)}" min="1" max="50" step="0.5"></div>
          <div class="field"><label>Warmup-Dauer (Stunden)</label>
            <input type="number" name="warmup_hours" value="{sbg.get('warmup_hours', 48)}" min="1" max="168"></div>
          <div class="field"><label>Scale-Budget Top-Ads (EUR)</label>
            <input type="number" name="scale_budget_eur" value="{sbg.get('scale_budget_eur', 10.0)}" min="1" max="500" step="0.5"></div>
          <div class="field"><label>Tägliches Spend-Cap gesamt (EUR)</label>
            <input type="number" name="daily_spend_cap_eur" value="{sbg.get('daily_spend_cap_eur', 100.0)}" min="1" max="10000"></div>
          <div class="field"><label>Max. Budget-Erhöhung Faktor (z.B. 2.0 = 2×)</label>
            <input type="number" name="max_scale_multiplier" value="{sbg.get('max_scale_multiplier', 2.0)}" min="1" max="10" step="0.1"></div>
        </div>
      </div>

      <div class="card">
        <div class="section-title">📉 ROAS & Account-Schutz</div>
        <div class="grid">
          <div class="field"><label>Mindest-ROAS (unter diesem Wert pausieren)</label>
            <input type="number" name="min_roas" value="{srs.get('min_roas', 2.0)}" min="0" max="20" step="0.1"></div>
          <div class="field"><label>ROAS-Prüfzeitraum (Tage)</label>
            <input type="number" name="roas_pause_days" value="{srs.get('roas_pause_days', 3)}" min="1" max="14"></div>
          <div class="field"><label>CPM-Anomalie-Faktor (z.B. 10 = 10× Ø)</label>
            <input type="number" name="anomaly_cpm_factor" value="{sgu.get('anomaly_cpm_factor', 10.0)}" min="2" max="50" step="0.5"></div>
          <div class="field"><label>Max. Ablehnungen in 24h bis Pause</label>
            <input type="number" name="max_rejections_24h" value="{sgu.get('max_rejections_24h', 2)}" min="1" max="10"></div>
          <div class="field"><label>Max. API-Fehler bis Pipeline-Pause</label>
            <input type="number" name="max_api_errors_before_pause" value="{sgu.get('max_api_errors_before_pause', 3)}" min="1" max="10"></div>
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


# ── KPI & Ad-Texte Helfer ─────────────────────────────────────────────────────

def fetch_meta_kpis(date_preset: str = "last_7d") -> tuple:
    """Holt Kampagnen-KPIs von Meta Marketing API. Gibt (data, error) zurück."""
    import requests as _req
    token   = os.getenv("META_ACCESS_TOKEN", "")
    account = os.getenv("META_AD_ACCOUNT_ID", "").lstrip("act_")
    if not token or not account:
        return None, "META_ACCESS_TOKEN oder META_AD_ACCOUNT_ID nicht gesetzt."
    url = f"https://graph.facebook.com/v18.0/act_{account}/insights"
    params = {
        "fields":      "campaign_name,impressions,clicks,ctr,cpm,spend,reach,frequency,actions",
        "date_preset": date_preset,
        "level":       "campaign",
        "access_token": token,
    }
    try:
        r = _req.get(url, params=params, timeout=20)
        data = r.json()
        if "error" in data:
            return None, data["error"].get("message", str(data["error"]))
        return data.get("data", []), None
    except Exception as e:
        return None, str(e)


def analyze_kpis_with_claude(kpis: list) -> str:
    """Lässt Claude die KPI-Daten analysieren und Empfehlungen geben."""
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        return "⚠️ Anthropic API Key nicht gesetzt — keine AI-Analyse verfügbar."
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        kpi_text = json.dumps(kpis, indent=2, ensure_ascii=False)
        response = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=900,
            messages=[{"role": "user", "content": f"""Du bist ein Meta Ads Experte für Weingut Huppert (Rheinhessen).
Analysiere diese Kampagnen-KPIs der letzten Tage und gib konkrete Handlungsempfehlungen auf Deutsch.

KPI-Daten:
{kpi_text}

Antworte in diesem Format:
**Zusammenfassung** (2-3 Sätze)

**Was gut läuft ✅**
- ...

**Was optimiert werden sollte ⚠️**
- ...

**Konkrete Empfehlungen 🎯**
- ...
"""}]
        )
        return response.content[0].text
    except Exception as e:
        return f"Fehler bei Claude-Analyse: {e}"


def generate_ad_texts_for_video(video_name: str, extra_info: str = "") -> dict:
    """Generiert Meta Ad Texte per Claude."""
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        return {"error": "Anthropic API Key fehlt", "primary_text": "", "headline": "", "description": ""}
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=600,
            system="Du erstellst Meta Ad Texte für Weingut Huppert, Gundersheim (Rheinhessen). Ton: authentisch, warm, einladend. Sprache: Deutsch.",
            messages=[{"role": "user", "content": f"""Erstelle Meta Ad Texte für dieses Video:
Video: {video_name}
{f"Zusatzinfo: {extra_info}" if extra_info else ""}

Antworte NUR mit diesem JSON (kein Markdown, keine Erklärung):
{{"primary_text": "max 125 Zeichen, 1-2 Sätze", "headline": "max 40 Zeichen", "description": "max 30 Zeichen", "cta": "SHOP_NOW oder LEARN_MORE"}}"""}]
        )
        raw = response.content[0].text.strip()
        return json.loads(raw)
    except Exception as e:
        return {"error": str(e), "primary_text": "", "headline": "", "description": ""}


# ── KPI-Analyse Seite ──────────────────────────────────────────────────────────

@app.route("/kpi")
@login_required
def kpi_page():
    preset   = request.args.get("preset", "last_7d")
    kpis, err = fetch_meta_kpis(preset)
    analyse   = ""

    preset_labels = {
        "last_7d":  "Letzte 7 Tage",
        "last_14d": "Letzte 14 Tage",
        "last_30d": "Letzte 30 Tage",
        "this_month": "Dieser Monat",
    }

    # Tabellen-Zeilen
    rows = ""
    if kpis:
        for k in kpis:
            conv = sum(int(a.get("value", 0)) for a in k.get("actions", []) if a.get("action_type") == "purchase")
            ctr  = float(k.get("ctr", 0)) * 100
            rows += f"""<tr>
              <td style="font-weight:600">{k.get('campaign_name','—')}</td>
              <td>{int(k.get('impressions', 0)):,}</td>
              <td>{int(k.get('clicks', 0)):,}</td>
              <td>{ctr:.2f}%</td>
              <td>EUR {float(k.get('cpm', 0)):.2f}</td>
              <td>EUR {float(k.get('spend', 0)):.2f}</td>
              <td>{conv}</td>
            </tr>"""
        analyse = analyze_kpis_with_claude(kpis)

    error_html  = f'<div class="alert alert-danger">❌ {err}</div>' if err else ""
    table_html  = f"""<table>
      <thead><tr>
        <th>Kampagne</th><th>Impressionen</th><th>Klicks</th>
        <th>CTR</th><th>CPM</th><th>Spend</th><th>Käufe</th>
      </tr></thead>
      <tbody>{rows if rows else '<tr><td colspan="7" style="text-align:center;color:#999">Keine Daten</td></tr>'}</tbody>
    </table>""" if not err else ""

    analyse_html = ""
    if analyse:
        import re
        # Einfaches Markdown → HTML
        html_analyse = analyse.replace("**", "<strong>", 1)
        parts = analyse.split("**")
        html_analyse = ""
        for i, p in enumerate(parts):
            if i % 2 == 1:
                html_analyse += f"<strong>{p}</strong>"
            else:
                html_analyse += p.replace("\n- ", "\n• ").replace("\n", "<br>")
        analyse_html = f"""<div class="card" style="margin-top:20px">
          <div class="card" style="background:#f0f4ff;border:1px solid #c7d2fe;padding:16px;border-radius:8px;line-height:1.7">
            {html_analyse}
          </div>
        </div>"""

    preset_btns = "".join(
        f'<a href="/kpi?preset={p}" class="btn btn-sm {"btn-primary" if preset==p else ""}" style="margin-right:6px">{label}</a>'
        for p, label in preset_labels.items()
    )

    content = f"""
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:20px;flex-wrap:wrap;gap:10px">
      <h1 style="font-size:1.4em">📊 KPI-Analyse — {preset_labels.get(preset, preset)}</h1>
      <div>{preset_btns}</div>
    </div>
    {error_html}
    <div class="card">
      <h2>Kampagnen-Performance</h2>
      {table_html}
    </div>
    {analyse_html}
    """
    return render_page(content, active="kpi")


# ── Ad-Texte Generator ─────────────────────────────────────────────────────────

# ── Ad-Setup: Background-Tasks ──────────────────────────────────────────────────

import threading as _threading
import uuid as _uuid

_analysis_tasks: dict = {}   # task_id -> {"status": "pending"|"done"|"error", "result": ...}
_gdrive_video_cache: list = []
_gdrive_cache_lock = _threading.Lock()


def _load_gdrive_videos() -> list:
    """Lädt Video-Liste aus Google Drive (alle Ordner)."""
    global _gdrive_video_cache
    try:
        from gdrive_sync import list_drive_files
        folder_ids = os.getenv("GOOGLE_DRIVE_FOLDER_IDS", "").split(",")
        all_files = []
        for fid in folder_ids:
            fid = fid.strip()
            if fid:
                all_files.extend(list_drive_files(fid))
        videos = [f for f in all_files
                  if "video" in f.get("mimeType", "").lower()
                  or f.get("name", "").lower().endswith((".mp4", ".mov", ".avi", ".mkv"))]
        with _gdrive_cache_lock:
            _gdrive_video_cache = videos
        return videos
    except Exception as e:
        logger.error(f"Google Drive Video-Liste Fehler: {e}")
        return []


def _find_video_by_nr(nr: int, videos: list) -> dict | None:
    import re
    for f in videos:
        name = f.get("name", "")
        m = re.match(r'^0*(\d+)', name)
        if m and int(m.group(1)) == nr:
            return f
    return None


def _extract_hook_type(filename: str) -> str:
    """Extrahiert den Hook-Typ aus dem Dateinamen.
    Format: 001_H10_MCAT_04_CTA_5.mp4 → 'H10'
    Gibt '' zurück wenn kein Hook-Typ gefunden.
    """
    import re
    # Suche nach H01-H99 Muster im Dateinamen
    m = re.search(r'_(H\d{2})_', filename, re.IGNORECASE)
    if m:
        return m.group(1).upper()
    # Fallback: suche ohne führende Null
    m = re.search(r'_(H\d+)_', filename, re.IGNORECASE)
    if m:
        return m.group(1).upper()
    return ""


CAMPAIGN_NAME = "Testing Inhouse"


def _run_analysis_task(task_id: str, video: dict):
    """Läuft in Background-Thread: Video herunterladen → analysieren → Ergebnis speichern."""
    from gdrive_sync import download_drive_file
    from text_generator import analyze_video_and_generate_setup, generate_full_ad_setup
    video_name = video.get("name", "video.mp4")
    try:
        _analysis_tasks[task_id]["status"] = "downloading"
        tmp_path = download_drive_file(video)
        if tmp_path and tmp_path.exists():
            _analysis_tasks[task_id]["status"] = "analyzing"
            result = analyze_video_and_generate_setup(str(tmp_path), video_name)
            try:
                tmp_path.unlink()
            except Exception:
                pass
        else:
            _analysis_tasks[task_id]["status"] = "analyzing"
            result = generate_full_ad_setup(video_name)
        _analysis_tasks[task_id].update({"status": "done", "result": result, "video_name": video_name,
                                          "size_mb": int(video.get("size", 0)) / 1024 / 1024})
    except Exception as e:
        _analysis_tasks[task_id].update({"status": "error", "error": str(e)})


@app.route("/api/videos")
@login_required
def api_videos():
    """Gibt gecachte oder frisch geladene Video-Liste zurück."""
    refresh = request.args.get("refresh", "0") == "1"
    with _gdrive_cache_lock:
        cached = list(_gdrive_video_cache)
    if not cached or refresh:
        cached = _load_gdrive_videos()
    import re
    result = []
    for f in cached:
        name = f.get("name", "")
        m = re.match(r'^0*(\d+)', name)
        nr = int(m.group(1)) if m else None
        result.append({"id": f.get("id"), "name": name,
                        "nr": nr, "size_mb": round(int(f.get("size", 0)) / 1024 / 1024, 1)})
    result.sort(key=lambda x: x["nr"] if x["nr"] is not None else 9999)
    return jsonify({"videos": result, "count": len(result)})


@app.route("/api/analyze", methods=["POST"])
@login_required
def api_analyze_start():
    """Startet Analyse-Task für Video-Nummer. Gibt task_id zurück."""
    data = request.get_json(silent=True) or {}
    nr = data.get("nr")
    try:
        nr = int(nr)
    except (TypeError, ValueError):
        return jsonify({"error": "Ungültige Nummer"}), 400

    with _gdrive_cache_lock:
        cached = list(_gdrive_video_cache)
    if not cached:
        cached = _load_gdrive_videos()

    video = _find_video_by_nr(nr, cached)
    if not video:
        # Cache leeren und nochmal
        cached = _load_gdrive_videos()
        video = _find_video_by_nr(nr, cached)
    if not video:
        return jsonify({"error": f"Kein Video mit Nummer {nr} gefunden"}), 404

    hook_type = _extract_hook_type(video.get("name", ""))
    task_id = _uuid.uuid4().hex
    _analysis_tasks[task_id] = {
        "status":     "pending",
        "nr":         nr,
        "video_name": video.get("name"),
        "hook_type":  hook_type,
        "kampagne":   CAMPAIGN_NAME,
        "adset":      hook_type if hook_type else "Unbekannt",
    }
    t = _threading.Thread(target=_run_analysis_task, args=(task_id, video), daemon=True)
    t.start()
    return jsonify({"task_id": task_id, "video_name": video.get("name"),
                    "size_mb": round(int(video.get("size", 0)) / 1024 / 1024, 1),
                    "hook_type": hook_type})


@app.route("/api/analyze/<task_id>")
@login_required
def api_analyze_status(task_id):
    """Gibt Status/Ergebnis eines Analyse-Tasks zurück."""
    task = _analysis_tasks.get(task_id)
    if not task:
        return jsonify({"error": "Task nicht gefunden"}), 404
    return jsonify(task)


@app.route("/texte")
@login_required
def texte_page():
    content = """
    <style>
      .video-list{max-height:320px;overflow-y:auto;border:1px solid #e9ecef;border-radius:8px;background:#fff}
      .video-item{padding:10px 14px;cursor:pointer;border-bottom:1px solid #f5f5f5;display:flex;align-items:center;gap:10px;transition:.15s}
      .video-item:hover{background:#f0f4ff}
      .video-item.active{background:#1a1a2e;color:#fff}
      .video-item .nr{font-weight:700;color:#1a1a2e;min-width:36px;font-size:1.05em}
      .video-item.active .nr{color:#aab4d4}
      .video-item .name{font-size:.85em;color:#555;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
      .video-item.active .name{color:#ccc}
      .video-item .size{font-size:.75em;color:#999;white-space:nowrap}
      .copy-field{background:#f8f9fc;border-radius:8px;padding:14px;margin-bottom:10px;border:1px solid #e9ecef}
      .copy-field .field-label{font-size:.75em;font-weight:700;color:#666;text-transform:uppercase;letter-spacing:.05em;margin-bottom:6px}
      .copy-field .field-value{font-size:.95em;line-height:1.5;color:#222;margin-bottom:8px;word-break:break-word}
      .copy-btn{padding:4px 12px;font-size:.8em;border:1px solid #ddd;border-radius:6px;background:#fff;cursor:pointer;transition:.15s}
      .copy-btn:hover{background:#1a1a2e;color:#fff;border-color:#1a1a2e}
      .section-divider{font-size:.75em;font-weight:700;text-transform:uppercase;letter-spacing:.1em;color:#999;padding:12px 0 6px;border-top:1px solid #eee;margin-top:8px}
      #result-panel{display:none}
      #loading-bar{display:none;text-align:center;padding:30px 0;color:#666}
      .spinner{display:inline-block;width:28px;height:28px;border:3px solid #e9ecef;border-top-color:#1a1a2e;border-radius:50%;animation:spin .8s linear infinite;margin-right:10px;vertical-align:middle}
      @keyframes spin{to{transform:rotate(360deg)}}
      .video-inhalt-box{background:#fffbe6;border:1px solid #ffe082;border-radius:8px;padding:12px 14px;margin-bottom:12px;font-size:.9em;color:#555;line-height:1.6}
      .tag{display:inline-block;padding:3px 8px;border-radius:4px;font-size:.8em;background:#e3e8ff;color:#1a1a2e;margin:2px}
    </style>

    <h1 style="font-size:1.4em;margin-bottom:6px">✍️ Ads Manager Setup</h1>
    <p style="color:#888;font-size:.9em;margin-bottom:20px">Video-Nummer eingeben → Claude analysiert Inhalt → fertige Texte + Kampagnen-Einstellungen</p>

    <div style="display:grid;grid-template-columns:1fr 1.4fr;gap:20px;align-items:start">

      <!-- Linke Spalte: Input + Video-Liste -->
      <div>
        <div class="card" style="padding:18px">
          <div style="display:flex;gap:10px;margin-bottom:16px">
            <input type="number" id="nr-input" placeholder="Nr. eingeben (z.B. 42)"
              style="flex:1;padding:10px 14px;border:2px solid #1a1a2e;border-radius:8px;font-size:1.1em;font-weight:600"
              min="1" max="999">
            <button onclick="analyzeNr()" class="btn btn-primary" style="padding:10px 18px;font-size:1em">
              🔍 Analysieren
            </button>
          </div>
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
            <span style="font-size:.8em;color:#888" id="video-count">Lade Videos...</span>
            <button onclick="loadVideos(true)" class="btn btn-sm" style="background:#e9ecef;font-size:.75em">↺ Aktualisieren</button>
          </div>
          <div class="video-list" id="video-list">
            <div style="padding:20px;text-align:center;color:#aaa;font-size:.9em">Wird geladen...</div>
          </div>
        </div>
      </div>

      <!-- Rechte Spalte: Ergebnis -->
      <div>
        <div class="card" id="placeholder-panel" style="padding:30px;text-align:center;color:#aaa">
          <div style="font-size:2em;margin-bottom:10px">🍷</div>
          <div>Video-Nummer links eingeben<br>oder Video aus der Liste wählen</div>
        </div>

        <div id="loading-bar" class="card" style="padding:30px;text-align:center">
          <div><span class="spinner"></span> <span id="loading-text">Lade Video...</span></div>
          <div style="font-size:.85em;color:#aaa;margin-top:8px" id="loading-file"></div>
        </div>

        <div id="result-panel" class="card" style="padding:18px">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
            <div>
              <strong id="res-nr" style="font-size:1.1em"></strong>
              <span id="res-filename" style="font-size:.85em;color:#888;margin-left:8px"></span>
            </div>
            <button onclick="copyAll()" class="btn btn-sm btn-success">📋 Alles kopieren</button>
          </div>

          <div id="video-inhalt-wrap" style="display:none">
            <div class="video-inhalt-box" id="res-video-inhalt"></div>
          </div>

          <div class="section-divider">🗂️ Kampagnen-Struktur (Meta Ads Manager)</div>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:4px">
            <div class="copy-field" style="margin-bottom:0;border-left:3px solid #1a1a2e">
              <div class="field-label">Kampagne</div>
              <div class="field-value" id="res-kampagne" style="font-weight:700;color:#1a1a2e"></div>
              <button class="copy-btn" onclick="copyField('res-kampagne',this)">📋 Kopieren</button>
            </div>
            <div class="copy-field" style="margin-bottom:0;border-left:3px solid #4361ee">
              <div class="field-label">Anzeigengruppe (Ad Set)</div>
              <div class="field-value" id="res-adset" style="font-weight:700;color:#4361ee;font-size:1.1em"></div>
              <button class="copy-btn" onclick="copyField('res-adset',this)">📋 Kopieren</button>
            </div>
          </div>
          <div style="background:#f0f4ff;border:1px solid #c7d2fe;border-radius:6px;padding:8px 12px;font-size:.8em;color:#555;margin-bottom:8px">
            ℹ️ Jede Anzeigengruppe entspricht einem Hook-Typ. Alle Videos mit gleichem Hook (z.B. H10) kommen in dieselbe Gruppe.
          </div>

          <div class="section-divider">📢 Ad Texte</div>
          <div class="copy-field">
            <div class="field-label">Primary Text <span style="color:#aaa;font-weight:400">(max. 125 Zeichen)</span></div>
            <div class="field-value" id="res-primary-text"></div>
            <button class="copy-btn" onclick="copyField('res-primary-text',this)">📋 Kopieren</button>
          </div>
          <div class="copy-field">
            <div class="field-label">Headline <span style="color:#aaa;font-weight:400">(max. 40 Zeichen)</span></div>
            <div class="field-value" id="res-headline"></div>
            <button class="copy-btn" onclick="copyField('res-headline',this)">📋 Kopieren</button>
          </div>
          <div class="copy-field">
            <div class="field-label">Description <span style="color:#aaa;font-weight:400">(max. 30 Zeichen)</span></div>
            <div class="field-value" id="res-description"></div>
            <button class="copy-btn" onclick="copyField('res-description',this)">📋 Kopieren</button>
          </div>
          <div class="copy-field">
            <div class="field-label">CTA Button</div>
            <div class="field-value" id="res-cta"></div>
            <button class="copy-btn" onclick="copyField('res-cta',this)">📋 Kopieren</button>
          </div>

          <div class="section-divider">🎯 Kampagne</div>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:4px">
            <div class="copy-field" style="margin-bottom:0">
              <div class="field-label">Kampagnenziel</div>
              <div class="field-value" id="res-ziel"></div>
            </div>
            <div class="copy-field" style="margin-bottom:0">
              <div class="field-label">Optimierungsziel</div>
              <div class="field-value" id="res-opt"></div>
            </div>
            <div class="copy-field" style="margin-bottom:0">
              <div class="field-label">Gebotstrategie</div>
              <div class="field-value" id="res-gebot"></div>
            </div>
            <div class="copy-field" style="margin-bottom:0">
              <div class="field-label">Budget / Tag</div>
              <div class="field-value" id="res-budget"></div>
            </div>
          </div>
          <div class="copy-field" style="margin-top:8px">
            <div class="field-label">Laufzeit-Empfehlung</div>
            <div class="field-value" id="res-laufzeit"></div>
          </div>

          <div class="section-divider">👥 Zielgruppe</div>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">
            <div class="copy-field" style="margin-bottom:0">
              <div class="field-label">Alter</div>
              <div class="field-value" id="res-alter"></div>
            </div>
            <div class="copy-field" style="margin-bottom:0">
              <div class="field-label">Geschlecht</div>
              <div class="field-value" id="res-geschlecht"></div>
            </div>
          </div>
          <div class="copy-field" style="margin-top:8px">
            <div class="field-label">Standort</div>
            <div class="field-value" id="res-standort"></div>
          </div>
          <div class="copy-field">
            <div class="field-label">Interessen</div>
            <div id="res-interessen"></div>
          </div>

          <div class="section-divider">📱 Placements</div>
          <div id="res-placements" style="padding:4px 0 8px"></div>

          <div class="section-divider">💬 Hinweis</div>
          <div class="copy-field">
            <div class="field-value" id="res-hinweis"></div>
          </div>
        </div>
      </div>
    </div>

    <script>
    let currentTaskId = null;
    let pollInterval  = null;

    // ── Video-Liste laden ──────────────────────────────────────────────────────
    function loadVideos(refresh) {
      const url = '/api/videos' + (refresh ? '?refresh=1' : '');
      fetch(url).then(r => r.json()).then(data => {
        const list = document.getElementById('video-list');
        const cnt  = document.getElementById('video-count');
        cnt.textContent = data.count + ' Videos in Google Drive';
        if (!data.videos || !data.videos.length) {
          list.innerHTML = '<div style="padding:20px;text-align:center;color:#aaa">Keine Videos gefunden</div>';
          return;
        }
        list.innerHTML = data.videos.map(v =>
          `<div class="video-item" onclick="selectVideo(${v.nr}, this)" data-nr="${v.nr}">
             <span class="nr">${v.nr}</span>
             <span class="name">${v.name}</span>
             <span class="size">${v.size_mb} MB</span>
           </div>`
        ).join('');
      }).catch(e => {
        document.getElementById('video-list').innerHTML =
          '<div style="padding:16px;color:#dc3545;font-size:.85em">Fehler beim Laden: ' + e + '</div>';
      });
    }

    function selectVideo(nr, el) {
      document.querySelectorAll('.video-item').forEach(i => i.classList.remove('active'));
      if (el) el.classList.add('active');
      document.getElementById('nr-input').value = nr;
      analyzeNr();
    }

    function analyzeNr() {
      const nr = parseInt(document.getElementById('nr-input').value);
      if (!nr || nr < 1) return;

      // UI: Loading anzeigen
      document.getElementById('placeholder-panel').style.display = 'none';
      document.getElementById('result-panel').style.display       = 'none';
      document.getElementById('loading-bar').style.display        = 'flex';
      document.getElementById('loading-bar').style.flexDirection  = 'column';
      document.getElementById('loading-text').textContent = 'Starte Download...';
      document.getElementById('loading-file').textContent = '';

      if (pollInterval) clearInterval(pollInterval);

      fetch('/api/analyze', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({nr: nr})
      }).then(r => r.json()).then(data => {
        if (data.error) { showError(data.error); return; }
        currentTaskId = data.task_id;
        document.getElementById('loading-file').textContent =
          data.video_name + ' (' + data.size_mb.toFixed(0) + ' MB)';
        pollInterval = setInterval(pollTask, 2000);
      }).catch(e => showError(String(e)));
    }

    function pollTask() {
      if (!currentTaskId) return;
      fetch('/api/analyze/' + currentTaskId).then(r => r.json()).then(data => {
        const txt = document.getElementById('loading-text');
        if (data.status === 'downloading') txt.textContent = 'Lade Video herunter...';
        else if (data.status === 'analyzing') txt.textContent = '🤖 Claude analysiert Video-Inhalt...';
        else if (data.status === 'done') {
          clearInterval(pollInterval);
          showResult(data);
        } else if (data.status === 'error') {
          clearInterval(pollInterval);
          showError(data.error || 'Unbekannter Fehler');
        }
      }).catch(() => {});
    }

    function showResult(data) {
      document.getElementById('loading-bar').style.display  = 'none';
      document.getElementById('result-panel').style.display = 'block';
      const s = data.result || {};
      document.getElementById('res-nr').textContent       = 'Nr. ' + (data.nr || '');
      document.getElementById('res-filename').textContent = data.video_name || '';

      // Kampagnen-Struktur aus Task-Daten (nicht aus Claude-Result)
      set('res-kampagne', data.kampagne || 'Testing Inhouse');
      const hookType = data.hook_type || '';
      document.getElementById('res-adset').textContent =
        hookType ? hookType + ' — ' + hookHumanLabel(hookType) : '—';

      const vi = s.video_inhalt || '';
      if (vi) {
        document.getElementById('video-inhalt-wrap').style.display = 'block';
        document.getElementById('res-video-inhalt').textContent = vi;
      } else {
        document.getElementById('video-inhalt-wrap').style.display = 'none';
      }

      set('res-primary-text', s.primary_text);
      set('res-headline',     s.headline);
      set('res-description',  s.description);
      set('res-cta',          s.cta);
      set('res-ziel',         s.kampagnenziel);
      set('res-opt',          s.optimierungsziel);
      set('res-gebot',        s.gebotstrategie);
      set('res-budget',       s.tagesbudget_eur ? s.tagesbudget_eur + ' EUR' : '—');
      set('res-laufzeit',     s.laufzeit_empfehlung);
      set('res-alter',        s.zielgruppe_alter);
      set('res-geschlecht',   s.zielgruppe_geschlecht);
      set('res-standort',     s.zielgruppe_standort);
      set('res-hinweis',      s.hinweis);

      const interessen = s.zielgruppe_interessen || [];
      document.getElementById('res-interessen').innerHTML =
        interessen.map(i => `<span class="tag">${i}</span>`).join('') || '—';

      const placements = s.placements || [];
      document.getElementById('res-placements').innerHTML =
        placements.map(p => `<span class="tag">📱 ${p}</span>`).join('') || '—';
    }

    function showError(msg) {
      document.getElementById('loading-bar').style.display  = 'none';
      document.getElementById('placeholder-panel').style.display = 'block';
      document.getElementById('placeholder-panel').innerHTML =
        '<div style="color:#dc3545;font-size:.9em">❌ ' + msg + '</div>';
    }

    function set(id, val) {
      document.getElementById(id).textContent = val || '—';
    }

    function hookHumanLabel(hook) {
      // Lesbare Bezeichnung pro Hook-Typ (Wein-Kontext)
      const labels = {
        'H01': 'Hook 1 (Produkt-Fokus)',
        'H02': 'Hook 2 (Story/Emotion)',
        'H03': 'Hook 3 (Frage/Problem)',
        'H04': 'Hook 4 (Angebot/Preis)',
        'H05': 'Hook 5 (Social Proof)',
        'H06': 'Hook 6 (Herkunft/Region)',
        'H07': 'Hook 7 (Lifestyle)',
        'H08': 'Hook 8 (Saison/Anlass)',
        'H09': 'Hook 9 (Direktansprache)',
        'H10': 'Hook 10 (CTA-First)',
      };
      return labels[hook] || hook;
    }

    function copyField(id, btn) {
      const val = document.getElementById(id).textContent;
      navigator.clipboard.writeText(val).then(() => {
        const orig = btn.textContent;
        btn.textContent = '✅ Kopiert!';
        btn.style.background = '#28a745'; btn.style.color = '#fff';
        setTimeout(() => { btn.textContent = orig; btn.style.background = ''; btn.style.color = ''; }, 2000);
      });
    }

    function copyAll() {
      const kampagne = document.getElementById('res-kampagne').textContent;
      const adset    = document.getElementById('res-adset').textContent;
      const ids    = ['res-primary-text','res-headline','res-description','res-cta'];
      const labels = ['Primary Text','Headline','Description','CTA'];
      let text = '=== KAMPAGNEN-STRUKTUR ===\\nKampagne: ' + kampagne + '\\nAnzeigengruppe: ' + adset + '\\n\\n=== AD TEXTE ===\\n';
      ids.forEach((id,i) => { text += labels[i] + ':\\n' + document.getElementById(id).textContent + '\\n\\n'; });
      navigator.clipboard.writeText(text.trim()).then(() => alert('✅ Alles kopiert (Kampagne + Texte)!'));
    }

    // Enter-Taste im Nummer-Input
    document.getElementById('nr-input').addEventListener('keydown', e => { if (e.key === 'Enter') analyzeNr(); });

    // Videos beim Laden der Seite abrufen
    loadVideos(false);
    </script>
    """
    return render_page(content, active="texte")


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


# ── Meta CAPI Webhook ─────────────────────────────────────────────────────────
# Empfängt WooCommerce Order-Webhooks und sendet Purchase an Meta CAPI.
# Öffentlich erreichbar — HMAC-SHA256-Signatur pflicht, wenn WC_WEBHOOK_SECRET
# in den Env-Vars gesetzt ist.
import hmac as _hmac_mod
import hashlib as _hashlib_mod
import base64 as _base64_mod

def _wc_webhook_valid(raw_body: bytes, received_sig: str, secret: str) -> bool:
    if not secret or not received_sig:
        return False
    mac = _hmac_mod.new(secret.encode('utf-8'), raw_body, _hashlib_mod.sha256).digest()
    expected = _base64_mod.b64encode(mac).decode('utf-8')
    return _hmac_mod.compare_digest(expected, received_sig)


@app.route("/webhook/woocommerce/order", methods=["POST"])
def webhook_woocommerce_order():
    """Empfängt WooCommerce Order-Webhooks und sendet Purchase an Meta CAPI.
    Keine Login-Pflicht (externer Aufruf), aber HMAC-Signatur-Verifikation."""
    from meta_capi import send_purchase

    raw = request.get_data() or b''
    sig = request.headers.get('X-WC-Webhook-Signature', '')
    secret = os.environ.get('WC_WEBHOOK_SECRET', '')

    if secret and not _wc_webhook_valid(raw, sig, secret):
        logger.warning(f'WC Webhook ungültige Signatur (len={len(raw)})')
        return jsonify({'ok': False, 'error': 'signature'}), 401

    try:
        order = request.get_json(silent=True) or {}
    except Exception as e:
        return jsonify({'ok': False, 'error': f'json: {e}'}), 400

    status = (order.get('status') or '').lower()
    order_id = order.get('id')
    if not order_id:
        return jsonify({'ok': False, 'error': 'no order id'}), 400

    # Nur zahlungswirksame Status feuern
    if status not in ('processing', 'completed', 'on-hold'):
        return jsonify({'ok': True, 'skipped': f'status={status}'}), 200

    result = send_purchase(order)
    if not result.get('ok'):
        logger.error(f'CAPI Order {order_id} Fehler: {result.get("error")}')
        return jsonify({'ok': False, 'order_id': order_id, **result}), 502

    logger.info(f'CAPI Purchase gesendet: Order {order_id} → {result.get("events_received")} Events')
    return jsonify({'ok': True, 'order_id': order_id, **result}), 200


@app.route("/api/capi/status", methods=["GET"])
def api_capi_status():
    from meta_capi import check_connection
    return jsonify(check_connection())


@app.route("/api/capi/test", methods=["POST"])
@login_required
def api_capi_test():
    """Feuert ein Test-Purchase (login-geschützt)."""
    from meta_capi import send_test
    return jsonify(send_test())


# ── Start ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    debug = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    print(f"🍷 Dashboard startet auf http://localhost:{port}")
    print(f"   Passwort: {DASHBOARD_PASSWORD}")
    app.run(host="0.0.0.0", port=port, debug=debug)
