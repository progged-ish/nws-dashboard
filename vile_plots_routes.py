"""VILE Custom SOUNDERpy Plot Routes

Blueprint: vile_custom_plots
Routes (all GET):
    /vile/custom-skewt-hodo?station=<code>&cycle=<cycle>&fh=<fh>&sm=<motion>&sr=<0|1>
    /vile/custom-skewt-hodo/api/skewt?station=<code>&cycle=<cycle>&fh=<fh>&sm=<motion>
    /vile/custom-skewt-hodo/api/hodo?station=<code>&cycle=<cycle>&fh=<fh>&sm=<motion>&sr=<0|1>
"""

import os
import sys
import json
import glob
from io import BytesIO
from datetime import datetime, timezone

from flask import (
    Blueprint, request, send_file, make_response, render_template_string, jsonify
)

# ── Ensure custom modules on path ────────────────────────────────
_CUSTOM_MODS = os.path.join(os.path.dirname(os.path.dirname(__file__)), "projects", "vile-sounderpy-custom")
if _CUSTOM_MODS not in sys.path:
    sys.path.insert(0, _CUSTOM_MODS)

from vile_plots import render_vile_plots, nam_json_to_clean_data

bp = Blueprint("vile_custom_plots", __name__)

NAM_PROFILES_DIR = "/mnt/d/weather_data/nam_grib2/profiles"


def _resolve_cycle(cycle_arg: str | None) -> str:
    if cycle_arg:
        return cycle_arg
    info_files = sorted(glob.glob(os.path.join(NAM_PROFILES_DIR, "nam_*_info.json")))
    if not info_files:
        return ""
    latest = info_files[-1]
    basename = os.path.basename(latest)
    parts = basename.replace("nam_", "").replace("_info.json", "").split("_")
    return "_".join(parts)


def _load_station(station_code: str, cycle: str) -> dict:
    fname = f"nam_{cycle}_{station_code}.json"
    fpath = os.path.join(NAM_PROFILES_DIR, fname)
    if not os.path.exists(fpath):
        raise FileNotFoundError(f"{fname} not found")
    with open(fpath) as f:
        return json.load(f)


# ═══════════════════════════════════════════════════════════════
# ⑴ PNG endpoints
# ═══════════════════════════════════════════════════════════════

@bp.route("/vile/custom-skewt-hodo/api/skewt")
def api_custom_skewt():
    station = request.args.get("station", "").strip().upper()
    cycle_arg = request.args.get("cycle", "").strip()
    fh = request.args.get("fh", "f00").strip().lower()
    sm = request.args.get("sm", "right_moving").strip().lower()

    if not station:
        return "Missing 'station' parameter", 400
    if sm not in {"right_moving", "left_moving", "mean_wind"}:
        sm = "right_moving"

    cycle = _resolve_cycle(cycle_arg if cycle_arg else None)
    if not cycle:
        return "No profile cycles available", 404

    try:
        station_data = _load_station(station, cycle)
    except FileNotFoundError:
        return f"Station {station} not found for cycle {cycle}", 404

    try:
        clean_data = nam_json_to_clean_data(station_data, fh)
    except KeyError as e:
        return str(e), 404

    skewt_buf, _ = render_vile_plots(clean_data, storm_motion=sm)

    response = make_response(send_file(
        skewt_buf,
        mimetype="image/png",
        max_age=3600,
        etag=True,
        conditional=True,
    ))
    response.headers["Cache-Control"] = "public, max-age=3600"
    return response


@bp.route("/vile/custom-skewt-hodo/api/hodo")
def api_custom_hodo():
    station = request.args.get("station", "").strip().upper()
    cycle_arg = request.args.get("cycle", "").strip()
    fh = request.args.get("fh", "f00").strip().lower()
    sm = request.args.get("sm", "right_moving").strip().lower()
    sr = request.args.get("sr", "0").strip() == "1"

    if not station:
        return "Missing 'station' parameter", 400
    if sm not in {"right_moving", "left_moving", "mean_wind"}:
        sm = "right_moving"

    cycle = _resolve_cycle(cycle_arg if cycle_arg else None)
    if not cycle:
        return "No profile cycles available", 404

    try:
        station_data = _load_station(station, cycle)
    except FileNotFoundError:
        return f"Station {station} not found for cycle {cycle}", 404

    try:
        clean_data = nam_json_to_clean_data(station_data, fh)
    except KeyError as e:
        return str(e), 404

    _, hodo_buf = render_vile_plots(clean_data, storm_motion=sm, sr_hodo=sr)

    response = make_response(send_file(
        hodo_buf,
        mimetype="image/png",
        max_age=3600,
        etag=True,
        conditional=True,
    ))
    response.headers["Cache-Control"] = "public, max-age=3600"
    return response


# ═══════════════════════════════════════════════════════════════
# ⑵ Combined dashboard page
# ═══════════════════════════════════════════════════════════════

@bp.route("/vile/custom-skewt-hodo")
def vile_custom_skewt_hodo_page():
    station = request.args.get("station", "KBOI").strip().upper()
    cycle_arg = request.args.get("cycle", "").strip()
    fh = request.args.get("fh", "f00").strip().lower()
    sm = request.args.get("sm", "right_moving").strip().lower()
    sr = request.args.get("sr", "").strip() == "1"

    if sm not in {"right_moving", "left_moving", "mean_wind"}:
        sm = "right_moving"

    cycle = _resolve_cycle(cycle_arg if cycle_arg else None)
    # Default to latest cycle if still empty
    if not cycle:
        cycle = _resolve_cycle(None)

    # Build query-string for the PNG endpoints
    qs_params = f"station={station}&cycle={cycle}&fh={fh}&sm={sm}"
    qs_params_hodo = qs_params
    if sr:
        qs_params_hodo += "&sr=1"

    station_name = station
    valid_label = f"{cycle} / {fh}"
    try:
        sd = _load_station(station, cycle)
        station_name = sd.get("name", station)
        fh_data = sd.get("forecast_hours", {}).get(fh, {})
        vt = fh_data.get("valid_time", "")
        if vt:
            try:
                dt = datetime.fromisoformat(vt.replace("Z", "+00:00"))
                valid_label = dt.strftime("%d %b %Y %HZ")
            except Exception:
                valid_label = vt
    except FileNotFoundError:
        pass

    return render_template_string(
        _COMBINED_HTML,
        station=station,
        station_name=station_name,
        cycle=cycle,
        fh=fh,
        sm=sm,
        sr=sr,
        qs_params=qs_params,
        qs_params_hodo=qs_params_hodo,
        valid_label=valid_label,
    )


# ═══════════════════════════════════════════════════════════════
# ⑶ Health check
# ═══════════════════════════════════════════════════════════════

@bp.route("/vile/custom-skewt-hodo/health")
def health():
    return jsonify({"status": "ok", "module": "vile_custom_plots"})


# ═══════════════════════════════════════════════════════════════
# DARK HTML TEMPLATE  (side-by-side desktop, stacked mobile)
# ═══════════════════════════════════════════════════════════════

_COMBINED_HTML = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>V.I.L.E. Custom Sounderpy — {{ station }}</title>
<style>
:root {
  --bg-primary: #0d1117;
  --bg-elevated: #161b22;
  --bg-surface: #1c2128;
  --border: #30363d;
  --text-primary: #c9d1d9;
  --text-secondary: #8b949e;
  --accent: #58a6ff;
  --accent-hover: #79b8ff;
  --danger: #f85149;
  --success: #238636;
  --radius: 8px;
  --gap: 14px;
  --font-mono: ui-monospace,"SF Mono",SFMono-Regular,"DejaVu Sans Mono",Menlo,Consolas,monospace;
}
* { box-sizing: border-box; }
html, body {
  margin: 0; padding: 0;
  background: var(--bg-primary);
  color: var(--text-primary);
  font-family: -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
  line-height: 1.5;
  min-height: 100vh;
}
header {
  display: flex; align-items: center; justify-content: space-between;
  padding: 14px 22px;
  border-bottom: 1px solid var(--border);
  background: var(--bg-elevated);
  gap: 12px;
  flex-wrap: wrap;
}
header h1 {
  margin: 0; font-size: 1.15rem; font-weight: 600; letter-spacing: 0.3px;
  white-space: nowrap;
}
header h1 span { color: var(--accent); }
header .meta {
  font-size: 0.82rem; color: var(--text-secondary);
  font-family: var(--font-mono);
  white-space: nowrap;
}
.controls {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
}
.controls select, .controls a, .controls button {
  appearance: none;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg-surface);
  color: var(--text-primary);
  font-size: 0.82rem;
  padding: 6px 10px;
  line-height: 1;
  cursor: pointer;
  text-decoration: none;
  transition: background 0.12s, border-color 0.12s;
}
.controls select:hover, .controls a:hover, .controls button:hover {
  border-color: var(--accent);
}
.controls button.primary {
  background: var(--accent);
  color: #000;
  font-weight: 600;
  border-color: var(--accent);
}
.controls button.primary:hover {
  background: var(--accent-hover);
}
main {
  display: flex;
  flex-direction: row;
  gap: var(--gap);
  padding: var(--gap);
  height: calc(100vh - 70px);
  overflow: hidden;
}
.panel {
  flex: 1 1 0;
  min-width: 0;
  display: flex;
  flex-direction: column;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg-primary);
  overflow: hidden;
}
.panel-header {
  padding: 10px 14px;
  border-bottom: 1px solid var(--border);
  background: var(--bg-elevated);
  font-size: 0.78rem;
  color: var(--text-secondary);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  font-weight: 600;
  display: flex; justify-content: space-between; align-items: center;
}
.panel-body {
  flex: 1 1 auto;
  display: flex; align-items: center; justify-content: center;
  overflow: auto;
  position: relative;
}
.panel-body img {
  display: block;
  max-width: 100%;
  max-height: 100%;
  object-fit: contain;
}
.panel-body .placeholder {
  color: var(--text-secondary);
  font-size: 0.9rem;
  text-align: center;
  padding: 20px;
}
/* Mobile: stack vertically */
@media (max-width: 860px) {
  main { flex-direction: column; height: auto; overflow: visible; }
  .panel { min-height: 45vh; }
  header { flex-direction: column; align-items: flex-start; }
}
/* Loading spinner overlay */
.spinner {
  width: 32px; height: 32px;
  border: 3px solid var(--border);
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: spin .8s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }
</style>
</head>
<body>
<header>
  <h1>V.I.L.E. <span>Custom Sounderpy</span></h1>
  <div class="meta">{{ station }} — {{ station_name }} &nbsp;|&nbsp; {{ valid_label }}</div>
  <div class="controls">
    <select id="smSelect" title="Storm Motion" onchange="updateMotion()">
      <option value="right_moving" {% if sm == 'right_moving' %}selected{% endif %}>Right Moving</option>
      <option value="left_moving" {% if sm == 'left_moving' %}selected{% endif %}>Left Moving</option>
      <option value="mean_wind" {% if sm == 'mean_wind' %}selected{% endif %}>Mean Wind</option>
    </select>
    <label style="font-size:0.78rem;color:var(--text-secondary);cursor:pointer;display:flex;align-items:center;gap:4px;">
      <input type="checkbox" id="srCheck" {% if sr %}checked{% endif %} onchange="updateMotion()">
      SR Hodo
    </label>
    <button class="primary" onclick="updateMotion()">↻ Reload</button>
    <a href="/">← Dashboard</a>
    <a href="/threats">◀ THREATS</a>
  </div>
</header>

<main>
  <div class="panel">
    <div class="panel-header">
      <span>Skew-T / Log-P</span>
      <span style="font-size:0.68rem;color:var(--text-secondary);">Dark Mode</span>
    </div>
    <div class="panel-body" id="skewtPanel">
      <img id="skewtImg" src="/vile/custom-skewt-hodo/api/skewt?{{ qs_params }}" alt="Skew-T">
    </div>
  </div>
  <div class="panel">
    <div class="panel-header">
      <span>Hodograph</span>
      <span style="font-size:0.68rem;color:var(--text-secondary);">{% if sr %}Storm Relative{% else %}Ground Relative{% endif %}</span>
    </div>
    <div class="panel-body" id="hodoPanel">
      <img id="hodoImg" src="/vile/custom-skewt-hodo/api/hodo?{{ qs_params_hodo }}" alt="Hodograph">
    </div>
  </div>
</main>

<script>
function updateMotion() {
  const sm = document.getElementById('smSelect').value;
  const sr = document.getElementById('srCheck').checked ? 1 : 0;
  const params = new URLSearchParams(window.location.search);
  params.set('sm', sm);
  if (sr) params.set('sr', '1'); else params.delete('sr');
  window.location.search = params.toString();
}

// Add subtle error handling if images fail to load
document.getElementById('skewtImg').addEventListener('error', function() {
  const panel = document.getElementById('skewtPanel');
  panel.innerHTML = '<div class="placeholder">❌ Failed to load Skew-T image.<br>Check station / cycle / forecast hour.</div>';
});
document.getElementById('hodoImg').addEventListener('error', function() {
  const panel = document.getElementById('hodoPanel');
  panel.innerHTML = '<div class="placeholder">❌ Failed to load Hodograph image.<br>Check station / cycle / forecast hour.</div>';
});
</script>
</body>
</html>
'''
