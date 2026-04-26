"""SOUNDERpy Flask Blueprint for VILE dashboard.

Routes:
    GET /api/sounderpy/skewt?station=<code>&cycle=<cycle>&fh=<fh>
    GET /api/sounderpy/hodograph?station=<code>&cycle=<cycle>&fh=<fh>
    GET /api/sounderpy/raob?station=<id>&date=<YYYYmmdd>&hour=<HH>
    GET /api/sounderpy/cache/status
"""
import os
import glob
import json
import tempfile
import sys
from datetime import datetime, timezone, timedelta

from flask import Blueprint, request, send_file, jsonify, make_response, render_template_string

# ensure metar-automation/scripts is on path for sounderpy_params
_METAR_SCRIPTS = os.path.join(os.path.dirname(__file__), "..", "metar-automation", "scripts")
if _METAR_SCRIPTS not in sys.path:
    sys.path.insert(0, os.path.abspath(_METAR_SCRIPTS))

from sounderpy_params import get_params, read_params_cache, PARAMS_CACHE_DIR
from sounderpy_renderer import (
    profile_json_to_sounderpy,
    raob_to_sounderpy,
    render_skewt_png,
    render_hodograph_png,
    get_cached_png,
)

bp = Blueprint("sounderpy", __name__)

NAM_PROFILES_DIR = "/mnt/d/weather_data/nam_grib2/profiles"
CACHE_DIR = "/mnt/d/weather_data/nam_grib2/profiles/skewt_cache"
PARAMS_CACHE_DIR = os.path.join(NAM_PROFILES_DIR, "params_cache")
CRON_JOB_ID = "079a48432fc5"
_DAEMON_STATE_FILE = "/tmp/sounderpy_daemon_state.json"


def _next_nam_prerender_time(now: datetime = None) -> datetime:
    """Return the next scheduled daemon run (30 minutes past cycle hour)."""
    if now is None:
        now = datetime.now(timezone.utc)
    run_hours = [3, 9, 15, 21]
    for h in run_hours:
        candidate = now.replace(hour=h, minute=30, second=0, microsecond=0)
        if candidate <= now:
            continue
        return candidate
    # Next day, first cycle
    tomorrow = now + timedelta(days=1)
    return tomorrow.replace(hour=run_hours[0], minute=30, second=0, microsecond=0)


def _scan_daemon_status() -> dict:
    """Derive daemon status from cache directories and optional state file."""
    # Try to read explicit state file written by daemon
    state = {}
    if os.path.exists(_DAEMON_STATE_FILE):
        try:
            with open(_DAEMON_STATE_FILE) as f:
                state = json.load(f)
        except Exception:
            pass

    # Derive latest cycle from cache dirs
    all_cycles = set()
    newest_mtime = 0.0
    newest_cycle = None

    for base_dir in [CACHE_DIR, PARAMS_CACHE_DIR]:
        if not os.path.isdir(base_dir):
            continue
        for entry in os.listdir(base_dir):
            edir = os.path.join(base_dir, entry)
            if not os.path.isdir(edir):
                continue
            # entry like 20260424_06z
            if len(entry) == 12 and entry[8] == "_" and entry.endswith("z"):
                all_cycles.add(entry)
                try:
                    mtime = os.path.getmtime(edir)
                    if mtime > newest_mtime:
                        newest_mtime = mtime
                        newest_cycle = entry
                except OSError:
                    pass

    if state.get("last_run_cycle"):
        last_run_cycle = state["last_run_cycle"]
        last_run_ts = state.get("last_run_timestamp")
    elif newest_cycle:
        last_run_cycle = newest_cycle
        last_run_ts = datetime.utcfromtimestamp(newest_mtime).isoformat() + "Z"
    else:
        last_run_cycle = None
        last_run_ts = None

    # Count cache items
    png_total = 0
    png_bytes = 0
    params_total = 0
    params_bytes = 0

    for cycle in all_cycles:
        cdir = os.path.join(CACHE_DIR, cycle)
        if os.path.isdir(cdir):
            for f in os.listdir(cdir):
                if f.endswith(".png"):
                    png_total += 1
                    try:
                        png_bytes += os.path.getsize(os.path.join(cdir, f))
                    except OSError:
                        pass
        pdir = os.path.join(PARAMS_CACHE_DIR, cycle)
        if os.path.isdir(pdir):
            for f in os.listdir(pdir):
                if f.endswith(".json"):
                    params_total += 1
                    try:
                        params_bytes += os.path.getsize(os.path.join(pdir, f))
                    except OSError:
                        pass

    next_run = _next_nam_prerender_time()

    return {
        "last_run_cycle": last_run_cycle,
        "last_run_timestamp": last_run_ts,
        "status": "ok" if last_run_cycle else "not_run",
        "cache_mb": round(png_bytes / 1048576, 2),
        "params_cached_count": params_total,
        "pngs_cached_count": png_total,
        "next_scheduled_run": next_run.isoformat().replace("+00:00", "Z"),
        "cron_job_id": CRON_JOB_ID,
    }

def _resolve_cycle(cycle_arg: str | None) -> str:
    """Return the latest cycle label if none provided."""
    if cycle_arg:
        return cycle_arg
    info_files = sorted(glob.glob(os.path.join(NAM_PROFILES_DIR, "nam_*_info.json")))
    if not info_files:
        return ""
    latest = info_files[-1]
    basename = os.path.basename(latest)
    # nam_20260415_18z_info.json -> 20260415_18z
    parts = basename.replace("nam_", "").replace("_info.json", "").split("_")
    return "_".join(parts)


def _station_json_path(station_code: str, cycle: str) -> str | None:
    fname = f"nam_{cycle}_{station_code}.json"
    fpath = os.path.join(NAM_PROFILES_DIR, fname)
    return fpath if os.path.exists(fpath) else None


def _load_station_data(station_code: str, cycle: str) -> dict:
    """Load raw station JSON; raise 404-friendly exceptions."""
    fpath = _station_json_path(station_code, cycle)
    if not fpath:
        raise FileNotFoundError(f"Station {station_code} not found for cycle {cycle}")
    with open(fpath) as f:
        return json.load(f)


def _json_requested() -> bool:
    accept = request.headers.get("Accept", "")
    return "application/json" in accept


@bp.route("/api/sounderpy/skewt")
def api_sounderpy_skewt():
    station = request.args.get("station", "").strip().upper()
    cycle_arg = request.args.get("cycle", "").strip()
    fh = request.args.get("fh", "f00").strip().lower()

    if not station:
        if _json_requested():
            return jsonify({"error": "Missing 'station' parameter"}), 400
        return "Missing 'station' parameter", 400

    cycle = _resolve_cycle(cycle_arg if cycle_arg else None)
    if not cycle:
        if _json_requested():
            return jsonify({"error": "No profile cycles available"}), 404
        return "No profile cycles available", 404

    # Cache lookup
    cached = get_cached_png(station, fh, cycle, "skewt", CACHE_DIR)
    if cached:
        resp = make_response(
            send_file(
                cached,
                mimetype="image/png",
                max_age=86400,
                etag=True,
                conditional=True,
            )
        )
        resp.headers["X-Cache"] = "HIT"
        return resp

    # On-demand render
    try:
        station_data = _load_station_data(station, cycle)
    except FileNotFoundError as exc:
        if _json_requested():
            return jsonify({"error": str(exc)}), 404
        return str(exc), 404

    name = station_data.get("name", station)
    elevation = station_data.get("elevation_m", 0.0)
    fh_data = station_data.get("forecast_hours", {}).get(fh, {})
    valid_time = fh_data.get("valid_time", "")

    try:
        clean_data = profile_json_to_sounderpy(station_data, fh)
    except Exception as exc:
        if _json_requested():
            return jsonify({"error": f"Data conversion failed: {exc}"}), 500
        return f"Data conversion failed: {exc}", 500

    cycle_cache = os.path.join(CACHE_DIR, cycle)
    os.makedirs(cycle_cache, exist_ok=True)
    output_path = os.path.join(cycle_cache, f"skewt_{station}_{fh}.png")

    try:
        render_skewt_png(clean_data, station, name, valid_time, elevation, output_path)
    except Exception as exc:
        if _json_requested():
            return jsonify({"error": f"Render failed: {exc}"}), 500
        return f"Render failed: {exc}", 500

    resp = make_response(
        send_file(
            output_path,
            mimetype="image/png",
            max_age=86400,
            etag=True,
            conditional=True,
        )
    )
    resp.headers["X-Cache"] = "MISS"
    return resp


@bp.route("/api/sounderpy/hodograph")
def api_sounderpy_hodograph():
    station = request.args.get("station", "").strip().upper()
    cycle_arg = request.args.get("cycle", "").strip()
    fh = request.args.get("fh", "f00").strip().lower()

    if not station:
        if _json_requested():
            return jsonify({"error": "Missing 'station' parameter"}), 400
        return "Missing 'station' parameter", 400

    cycle = _resolve_cycle(cycle_arg if cycle_arg else None)
    if not cycle:
        if _json_requested():
            return jsonify({"error": "No profile cycles available"}), 404
        return "No profile cycles available", 404

    cached = get_cached_png(station, fh, cycle, "hodograph", CACHE_DIR)
    if cached:
        resp = make_response(
            send_file(
                cached,
                mimetype="image/png",
                max_age=86400,
                etag=True,
                conditional=True,
            )
        )
        resp.headers["X-Cache"] = "HIT"
        return resp

    try:
        station_data = _load_station_data(station, cycle)
    except FileNotFoundError as exc:
        if _json_requested():
            return jsonify({"error": str(exc)}), 404
        return str(exc), 404

    name = station_data.get("name", station)
    elevation = station_data.get("elevation_m", 0.0)
    fh_data = station_data.get("forecast_hours", {}).get(fh, {})
    valid_time = fh_data.get("valid_time", "")

    try:
        clean_data = profile_json_to_sounderpy(station_data, fh)
    except Exception as exc:
        if _json_requested():
            return jsonify({"error": f"Data conversion failed: {exc}"}), 500
        return f"Data conversion failed: {exc}", 500

    cycle_cache = os.path.join(CACHE_DIR, cycle)
    os.makedirs(cycle_cache, exist_ok=True)
    output_path = os.path.join(cycle_cache, f"hodograph_{station}_{fh}.png")

    try:
        render_hodograph_png(clean_data, station, name, valid_time, output_path)
    except Exception as exc:
        if _json_requested():
            return jsonify({"error": f"Render failed: {exc}"}), 500
        return f"Render failed: {exc}", 500

    resp = make_response(
        send_file(
            output_path,
            mimetype="image/png",
            max_age=86400,
            etag=True,
            conditional=True,
        )
    )
    resp.headers["X-Cache"] = "MISS"
    return resp


@bp.route("/api/sounderpy/raob")
def api_sounderpy_raob():
    station = request.args.get("station", "").strip().upper()
    date = request.args.get("date", "").strip()
    hour = request.args.get("hour", "").strip()

    if not station:
        if _json_requested():
            return jsonify({"error": "Missing 'station' parameter"}), 400
        return "Missing 'station' parameter", 400
    if not date or len(date) != 8 or not date.isdigit():
        if _json_requested():
            return jsonify({"error": "Invalid 'date' (expected YYYYmmdd)"}), 400
        return "Invalid 'date' (expected YYYYmmdd)", 400
    if not hour or len(hour) != 2 or not hour.isdigit():
        if _json_requested():
            return jsonify({"error": "Invalid 'hour' (expected HH)"}), 400
        return "Invalid 'hour' (expected HH)", 400

    year, month, day = date[:4], date[4:6], date[6:8]

    try:
        clean_data = raob_to_sounderpy(station, year, month, day, hour)
    except Exception as exc:
        if _json_requested():
            return jsonify({"error": f"RAOB fetch failed: {exc}"}), 500
        return f"RAOB fetch failed: {exc}", 500

    tmpdir = tempfile.mkdtemp(prefix="sounderpy_raob_")
    skewt_path = os.path.join(tmpdir, f"raob_{station}_{date}_{hour}_skewt.png")
    hodo_path = os.path.join(tmpdir, f"raob_{station}_{date}_{hour}_hodo.png")

    try:
        render_skewt_png(clean_data, station, f"RAOB {station}", f"{date} {hour}Z", 0.0, skewt_path)
        render_hodograph_png(clean_data, station, f"RAOB {station}", f"{date} {hour}Z", hodo_path)
    except Exception as exc:
        if _json_requested():
            return jsonify({"error": f"Render failed: {exc}"}), 500
        return f"Render failed: {exc}", 500

    # For simplicity in the RAOB route, return the Skew-T PNG by default.
    # A future enhancement could zip both or serve based on ?type= parameter.
    return send_file(
        skewt_path,
        mimetype="image/png",
        max_age=3600,
        etag=True,
        conditional=True,
    )


@bp.route("/api/sounderpy/cache/status")
def api_sounderpy_cache_status():
    """Return cache status for the newest or requested cycle."""
    cycle_arg = request.args.get("cycle", "").strip()
    cycle = cycle_arg if cycle_arg else _resolve_cycle(None)
    if not cycle:
        return jsonify(
            {
                "cycle": None,
                "station_count": 0,
                "fhours_rendered": 0,
                "total_pngs": 0,
                "cache_mb": 0.0,
                "oldest_cached": None,
            }
        ), 404

    cycle_cache = os.path.join(CACHE_DIR, cycle)
    station_files = {}
    fhours_set = set()
    total_bytes = 0
    oldest_ts = None

    if os.path.isdir(cycle_cache):
        for fname in os.listdir(cycle_cache):
            if not fname.endswith(".png"):
                continue
            # skewt_STATION_fXX.png  or  hodograph_STATION_fXX.png
            parts = fname.split("_")
            if len(parts) < 3:
                continue
            stn = parts[1]
            fh_part = parts[-1].replace(".png", "")
            station_files.setdefault(stn, []).append(fh_part)
            fhours_set.add(fh_part)
            fpath = os.path.join(cycle_cache, fname)
            try:
                total_bytes += os.path.getsize(fpath)
                mtime = os.path.getmtime(fpath)
                if oldest_ts is None or mtime < oldest_ts:
                    oldest_ts = mtime
            except OSError:
                pass

    # Separate K-stations from military / non-standard prefixes
    k_stations = {s for s in station_files if s.startswith("K")}
    # "complete" = at least 50 PNGs (29 fhours x 2 images minus some tolerance)
    k_complete = [s for s in k_stations if len(station_files.get(s, [])) >= 50]
    k_stations_partial = [s for s in k_stations if 0 < len(station_files.get(s, [])) < 50]

    station_count = len(station_files)
    k_station_count = len(k_stations)

    fhours_rendered = len(fhours_set)
    total_pngs = sum(len(v) for v in station_files.values())
    cache_mb = round(total_bytes / 1048576, 2) if total_bytes else 0.0
    oldest_cached = (
        datetime.utcfromtimestamp(oldest_ts).isoformat() + "Z" if oldest_ts else None
    )

    # ── Params cache stats ──
    params_cache_cycle = os.path.join(PARAMS_CACHE_DIR, cycle)
    params_files = 0
    params_bytes = 0
    if os.path.isdir(params_cache_cycle):
        for fname in os.listdir(params_cache_cycle):
            if not fname.endswith(".json"):
                continue
            params_files += 1
            fpath = os.path.join(params_cache_cycle, fname)
            try:
                params_bytes += os.path.getsize(fpath)
            except OSError:
                pass

    params_cache_mb = round(params_bytes / 1048576, 2) if params_bytes else 0.0

    return jsonify(
        {
            "cycle": cycle,
            "station_count": station_count,
            "k_station_count": k_station_count,
            "k_stations_complete": len(k_complete),
            "k_stations_partial": k_stations_partial,
            "fhours_rendered": fhours_rendered,
            "total_pngs": total_pngs,
            "cache_mb": cache_mb,
            "oldest_cached": oldest_cached,
            "params_cached_count": params_files,
            "params_cache_mb": params_cache_mb,
        }
    )


@bp.route("/api/nam/params")
def api_nam_params():
    station = request.args.get("station", "").strip().upper()
    cycle_arg = request.args.get("cycle", "").strip()
    fh = request.args.get("fh", "").strip().lower()

    if not station:
        return jsonify({"error": "Missing 'station' parameter"}), 400

    cycle = _resolve_cycle(cycle_arg if cycle_arg else None)
    if not cycle:
        return jsonify({"error": "No profile cycles available"}), 404

    # Cache-first: check params JSON on disk before computing
    cached_params = read_params_cache(station, cycle)
    if cached_params is not None:
        result = cached_params.get(fh, cached_params) if fh else cached_params
        resp = jsonify(result)
        resp.headers["X-Cache"] = "HIT"
        return resp

    result = get_params(station, cycle, fh if fh else None)
    resp = jsonify(result)
    resp.headers["X-Cache"] = "MISS"
    return resp


# ── Daemon Status Endpoint ─────────────────────────────────────────────────
@bp.route("/api/sounderpy/daemon/status")
def api_sounderpy_daemon_status():
    """Return global daemon status derived from cache directories."""
    return jsonify(_scan_daemon_status())


# ── Sounderpy Monitoring HUD ─────────────────────────────────────────────────
_SOUNDERPY_HUD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>SOUNDERpy Cache HUD</title>
    <style>
        :root { --accent:#e94560; --accent2:#4ecca3; --warn:#f4d03f; --bg:#0d1117; --card:#161b22; --text:#c9d1d9; --muted:#8b949e; }
        * { margin:0; padding:0; box-sizing:border-box; font-family:'Segoe UI',Roboto,Helvetica,Arial,sans-serif; }
        body { background:var(--bg); color:var(--text); min-height:100vh; padding:20px; }
        .container { max-width:1000px; margin:0 auto; }
        h1 { font-weight:300; font-size:1.8rem; margin-bottom:4px; }
        .subtitle { color:var(--muted); font-size:0.85rem; margin-bottom:24px; }
        .grid { display:grid; gap:16px; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); }
        .card { background:var(--card); border:1px solid #30363d; border-radius:10px; padding:18px; }
        .card h3 { font-size:0.75rem; text-transform:uppercase; color:var(--muted); letter-spacing:0.05em; margin-bottom:8px; }
        .metric { font-size:1.5rem; font-weight:600; }
        .badge { display:inline-block; padding:2px 10px; border-radius:12px; font-size:0.75rem; font-weight:600; }
        .badge.ok { background:rgba(78,204,163,0.15); color:var(--accent2); }
        .badge.warn { background:rgba(244,208,63,0.15); color:var(--warn); }
        .badge.bad { background:rgba(233,69,96,0.15); color:var(--accent); }
        .progress-bg { width:100%; height:8px; background:#30363d; border-radius:4px; margin-top:10px; overflow:hidden; }
        .progress-fill { height:100%; border-radius:4px; transition:width 0.4s ease; }
        .table { width:100%; border-collapse:collapse; margin-top:12px; font-size:0.88rem; }
        .table th { text-align:left; color:var(--muted); font-weight:500; padding:6px 0; border-bottom:1px solid #30363d; }
        .table td { padding:6px 0; border-bottom:1px solid #21262d; }
        .refresh { cursor:pointer; background:none; border:1px solid #30363d; color:var(--muted); padding:6px 14px; border-radius:6px; font-size:0.8rem; margin-top:20px; transition:0.2s; }
        .refresh:hover { border-color:var(--accent); color:var(--accent); }
        .header-row { display:flex; justify-content:space-between; align-items:center; margin-bottom:8px; }
        .nav { color:var(--muted); font-size:0.75rem; margin-bottom:16px; }
        .nav a { color:#58a6ff; text-decoration:none; }
        @media (max-width:600px) { .grid { grid-template-columns:1fr; } }
    </style>
</head>
<body>
    <div class="container">
        <div class="nav"><a href="/">back to dashboard</a></div>
        <div class="header-row">
            <div>
                <h1>SOUNDERpy Cache Monitor</h1>
                <div class="subtitle">NAM Profile Pre-render Pipeline</div>
            </div>
            <span id="cacheBadge" class="badge bad">checking...</span>
        </div>
        
        <div class="grid">
            <div class="card">
                <h3>Current Cycle</h3>
                <div id="currentCycle" class="metric">--</div>
            </div>
            <div class="card">
                <h3>PNG Cache</h3>
                <div id="pngCacheMB" class="metric">0 MB</div>
                <div class="progress-bg"><div id="pngBar" class="progress-fill" style="width:0%;background:var(--accent2)"></div></div>
            </div>
            <div class="card">
                <h3>Params Cache</h3>
                <div id="paramsCacheMB" class="metric">0 MB</div>
            </div>
            <div class="card">
                <h3>Station Coverage</h3>
                <div id="stationCoverage" class="metric">0 / 0</div>
                <div class="progress-bg"><div id="stationBar" class="progress-fill" style="width:0%;background:var(--warn)"></div></div>
            </div>
            <div class="card">
                <h3>Daemon Last Run</h3>
                <div id="lastRun" class="metric">--</div>
                <div id="lastRunCycle" style="color:var(--muted);font-size:0.8rem;margin-top:4px;"></div>
            </div>
            <div class="card">
                <h3>Next Scheduled</h3>
                <div id="nextRun" class="metric">--</div>
            </div>
        </div>

        <div class="card" style="margin-top:16px;">
            <h3>Cycle History</h3>
            <table class="table">
                <thead><tr><th>Cycle</th><th>PNG files</th><th>Params files</th><th>age</th></tr></thead>
                <tbody id="cycleTbody"></tbody>
            </table>
        </div>

        <button class="refresh" onclick="loadStats()">refresh</button>
    </div>

    <script>
    const TOTAL_STATIONS = 75;
    const WARN_SIZE_MB = 5120;   // 5 GB

    function timeAgo(ts) {
        const d = new Date(ts);
        if(isNaN(d)) return 'n/a';
        const sec = Math.floor((Date.now()-d)/1000);
        if(sec<60) return sec+'s ago';
        if(sec<3600) return Math.floor(sec/60)+'m ago';
        if(sec<86400) return Math.floor(sec/3600)+'h ago';
        return Math.floor(sec/86400)+'d ago';
    }

    function countdown(ts) {
        const d = new Date(ts);
        if(isNaN(d)) return '--';
        const sec = Math.floor((d-Date.now())/1000);
        if(sec<0) return 'overdue';
        const h = Math.floor(sec/3600), m = Math.floor((sec%3600)/60);
        return h+'h '+m+'m';
    }

    async function loadStats() {
        const res = await fetch('/api/sounderpy/daemon/status');
        const d = await res.json();

        document.getElementById('currentCycle').textContent = d.last_run_cycle || 'none';
        document.getElementById('pngCacheMB').textContent = d.cache_mb + ' MB';
        document.getElementById('paramsCacheMB').textContent = d.params_cached_count + ' files';

        const cov = Math.min((d.params_cached_count / TOTAL_STATIONS)*100, 100);
        document.getElementById('stationCoverage').textContent = d.params_cached_count + ' / ' + TOTAL_STATIONS;
        document.getElementById('stationBar').style.width = cov + '%';

        const warn = d.cache_mb > WARN_SIZE_MB;
        const bar = document.getElementById('pngBar');
        let pct = Math.min((d.cache_mb / WARN_SIZE_MB)*100, 100);
        bar.style.width = pct + '%';
        bar.style.background = warn ? 'var(--accent)' : 'var(--accent2)';

        document.getElementById('lastRun').textContent = d.last_run_timestamp ? timeAgo(d.last_run_timestamp) : 'never';
        document.getElementById('lastRunCycle').textContent = d.last_run_cycle || '';
        document.getElementById('nextRun').textContent = countdown(d.next_scheduled_run);

        // Health badge
        const badge = document.getElementById('cacheBadge');
        if(!d.last_run_cycle) { badge.textContent='cold'; badge.className='badge bad'; }
        else {
            const h = (Date.now()-new Date(d.last_run_timestamp))/3600000;
            if(h < 6) { badge.textContent='healthy'; badge.className='badge ok'; }
            else if(h < 12){ badge.textContent='stale'; badge.className='badge warn'; }
            else { badge.textContent='cold'; badge.className='badge bad'; }
        }

        // Cycle history
        const tbody = document.getElementById('cycleTbody');
        tbody.innerHTML = '';
        // Populate via a secondary endpoint? For now, just show latest.
        if(d.last_run_cycle) {
            tbody.innerHTML = '<tr><td>'+d.last_run_cycle+'</td><td>'+d.pngs_cached_count+'</td><td>'+d.params_cached_count+'</td><td>'+timeAgo(d.last_run_timestamp)+'</td></tr>';
        }
    }
    loadStats();
    setInterval(loadStats, 30000);
    </script>
</body>
</html>"""


@bp.route("/sounderpy/hud")
def sounderpy_hud():
    return render_template_string(_SOUNDERPY_HUD_HTML)

