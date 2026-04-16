#!/usr/bin/env python3
"""NWS Operations Dashboard — modern dark-mode HUD for AFD + NAM monitoring."""

from flask import Flask, render_template_string, jsonify, request
import requests
import os
import glob
import json
import re
import threading
import time
import logging
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# wx_events integration
sys.path.insert(0, "/home/progged-ish/wx_events")
from db_schema import DB_PATH as WX_DB_PATH

# ── Configuration ──────────────────────────────────────────────────────────────

NAM_DATA_DIR = "/mnt/d/weather_data/nam_grib2"
NAM_LOG_FILE = "/home/progged-ish/metar-automation/logs/nam_download.log"
NAM_CHECKPOINT = os.path.join(NAM_DATA_DIR, ".nam_download_checkpoint")
TOTAL_FORECAST_HOURS = 29  # f00-f84 every 3h
RETENTION_DAYS = 2

AFD_OFFICES = {
    "TWC": {"name": "Tucson, AZ",  "cwa": "TWC", "wfo": "KTWC"},
    "OKX": {"name": "New York, NY", "cwa": "OKX", "wfo": "KOKX"},
    "PDX": {"name": "Portland, OR", "cwa": "PQR", "wfo": "KPDX"},
    "LAX": {"name": "Los Angeles, CA", "cwa": "LOX", "wfo": "KLOX"},
    "DEN": {"name": "Denver, CO",  "cwa": "BOU", "wfo": "KDEN"},
}

LOG_DIR = "/var/log/nws_dashboard"
Path(LOG_DIR).mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, "dashboard.log")),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ── State Cache ───────────────────────────────────────────────────────────────

_cache = {
    "afd": {office: {"text": None, "issued": None, "status": "pending", "updated": None}
            for office in AFD_OFFICES},
    "nam": {"status": "pending", "files": [], "cycle": None, "date": None,
            "completion": 0, "total_size_mb": 0, "updated": None,
            "checkpoint_hours": [], "last_log_lines": []},
}


# ── AFD Fetchers ───────────────────────────────────────────────────────────────

def fetch_afd(office: str) -> dict:
    """Fetch latest AFD for a single office via NWS API with fallback."""
    info = AFD_OFFICES.get(office, {})
    cwa = info.get("cwa", office)
    wfo = info.get("wfo", f"K{office}")
    now_iso = datetime.now(timezone.utc).isoformat()

    # Strategy 1: products/types/AFD/locations/{CWA}
    try:
        url = f"https://api.weather.gov/products/types/AFD/locations/{cwa}"
        resp = requests.get(url, headers={
            "User-Agent": "NWS-Dashboard/2.0",
            "Accept": "application/ld+json",
        }, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        products = data.get("@graph", data.get("features", []))
        if products:
            latest = products[0]
            product_url = latest.get("@id", latest.get("id"))
            if product_url and not product_url.startswith("http"):
                product_url = f"https://api.weather.gov{product_url}"
            resp2 = requests.get(product_url, headers={
                "User-Agent": "NWS-Dashboard/2.0",
                "Accept": "application/ld+json",
            }, timeout=15)
            resp2.raise_for_status()
            result = resp2.json()
            return {
                "text": result.get("productText", ""),
                "issued": result.get("issuanceTime", ""),
                "status": "ok",
                "updated": now_iso,
            }
    except Exception as e:
        logger.debug(f"AFD strategy 1 failed for {office}: {e}")

    # Strategy 2: /products?office={CWA} — broader search
    try:
        url = f"https://api.weather.gov/products?office={cwa}&type=AFD&limit=1"
        resp = requests.get(url, headers={
            "User-Agent": "NWS-Dashboard/2.0",
            "Accept": "application/ld+json",
        }, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        products = data.get("@graph", data.get("features", []))
        if products:
            latest = products[0]
            product_url = latest.get("@id", latest.get("id"))
            if product_url and not product_url.startswith("http"):
                product_url = f"https://api.weather.gov{product_url}"
            resp2 = requests.get(product_url, headers={
                "User-Agent": "NWS-Dashboard/2.0",
                "Accept": "application/ld+json",
            }, timeout=15)
            resp2.raise_for_status()
            result = resp2.json()
            return {
                "text": result.get("productText", ""),
                "issued": result.get("issuanceTime", ""),
                "status": "ok",
                "updated": now_iso,
            }
    except Exception as e:
        logger.debug(f"AFD strategy 2 failed for {office}: {e}")

    return {"text": None, "issued": None, "status": "no_data", "updated": now_iso}


def refresh_all_afds():
    """Refresh AFD data for all offices."""
    for office in AFD_OFFICES:
        _cache["afd"][office] = fetch_afd(office)


# ── NAM Data Scanner ──────────────────────────────────────────────────────────

def scan_nam_data():
    """Scan the NAM GRIB2 directory and checkpoint for current status."""
    result = {
        "status": "ok",
        "files": [],
        "cycle": None,
        "date": None,
        "completion": 0,
        "total_size_mb": 0,
        "updated": datetime.now(timezone.utc).isoformat(),
        "checkpoint_hours": [],
        "last_log_lines": [],
    }

    # Read checkpoint
    checkpoint_hours = []
    if os.path.exists(NAM_CHECKPOINT):
        try:
            with open(NAM_CHECKPOINT) as f:
                for line in f:
                    h = line.strip()
                    if h.isdigit():
                        checkpoint_hours.append(int(h))
        except Exception:
            pass
    result["checkpoint_hours"] = sorted(checkpoint_hours)

    # Scan GRIB2 files
    if not os.path.exists(NAM_DATA_DIR):
        result["status"] = "no_directory"
        return result

    grib_files = sorted(glob.glob(os.path.join(NAM_DATA_DIR, "nam.*.grib2")))
    file_entries = []
    total_size = 0
    current_cycle = None
    current_date = None

    # Pre-scan to find the latest cycle (most recent date + cycle hour)
    cycle_set = set()
    for fpath in grib_files:
        fname = os.path.basename(fpath)
        parts = fname.split(".")
        fdate = parts[1] if len(parts) > 1 else None
        fcycle = parts[2] if len(parts) > 2 else None
        if fdate and fcycle:
            cycle_set.add((fdate, fcycle))
    if cycle_set:
        # Sort by (date, cycle) to find latest
        latest_date, latest_cycle = sorted(cycle_set)[-1]
        current_date = latest_date
        current_cycle = latest_cycle

    for fpath in grib_files:
        fname = os.path.basename(fpath)
        fsize = os.path.getsize(fpath)
        total_size += fsize

        # Parse cycle from filename: nam.20260405.t12z.awphys00.tm00.grib2
        parts = fname.split(".")
        fdate = parts[1] if len(parts) > 1 else None
        fcycle = parts[2] if len(parts) > 2 else None
        fhour = None
        for p in parts:
            if p.startswith("awphys"):
                fhour = p.replace("awphys", "")
                break

        # Calculate forecast valid time: cycle_run + fhour
        valid_utc = None
        if fdate and fcycle and fhour is not None:
            try:
                cycle_hr = int(fcycle.replace("t", "").replace("z", ""))
                fh = int(fhour)
                run_dt = datetime(int(fdate[:4]), int(fdate[4:6]), int(fdate[6:8]),
                                 cycle_hr, tzinfo=timezone.utc)
                valid_dt = run_dt + timedelta(hours=fh)
                valid_utc = valid_dt.strftime("%Y-%m-%d %HZ")
            except Exception:
                pass

        # Determine file status
        fh_int = int(fhour) if fhour is not None else -1
        in_checkpoint = fh_int in checkpoint_hours
        is_latest_cycle = (fdate == current_date and fcycle == current_cycle)
        if is_latest_cycle:
            file_status = "completed"  # file exists on disk = completed
        else:
            file_status = "old"  # from a previous cycle

        file_entries.append({
            "name": fname,
            "size_mb": round(fsize / (1024 * 1024), 1),
            "date": fdate,
            "cycle": fcycle,
            "hour": fhour,
            "valid_utc": valid_utc,
            "file_status": file_status,
        })

    # ── Check profiles extraction status ──
    profiles_status = {"extracted_cycle": None, "extracted_date": None, "processed": False}
    profiled_hours = set()  # forecast hours that have been profiled
    info_files = sorted(glob.glob(os.path.join(NAM_DATA_DIR, "profiles", "nam_*_info.json")))
    if info_files:
        latest_info = info_files[-1]
        # nam_20260414_06z_info.json → date=20260414, cycle=06
        try:
            basename = os.path.basename(latest_info)
            parts = basename.replace("nam_", "").replace("_info.json", "").split("_")
            p_date = parts[0]  # 20260414
            p_cycle = parts[1]  # 06z or 06
            p_cycle_hr = p_cycle.replace("z", "")
            profiles_status["extracted_date"] = p_date
            profiles_status["extracted_cycle"] = f"t{p_cycle_hr}z"
            # Check if the extracted cycle matches the latest downloaded cycle
            if current_date and current_cycle:
                profiles_status["processed"] = (p_date == current_date and
                                                 f"t{p_cycle_hr}z" == current_cycle)
            # Load profiled forecast hours from info.json
            with open(latest_info) as Inf:
                _info = json.load(Inf)
            profiled_hours = set(_info.get("forecast_hours", []))
            profiles_status["num_stations"] = _info.get("num_stations", 0)
            profiles_status["forecast_hours_count"] = len(profiled_hours)
            profiles_status["processing_time_s"] = _info.get("processing_time_s", 0)
            profiles_status["generated_utc"] = _info.get("generated_utc", "")
        except Exception:
            pass
    # Count actual profile files on disk for the extracted cycle
    if profiles_status.get("extracted_date") and profiles_status.get("extracted_cycle"):
        _cycle_tag = f"{profiles_status['extracted_date']}_{profiles_status['extracted_cycle'].lstrip('t')}"
        _prof_files = glob.glob(os.path.join(NAM_DATA_DIR, "profiles", f"nam_{_cycle_tag}_*.json"))
        profiles_status["profile_files_on_disk"] = len([f for f in _prof_files if "_info" not in f])
    result["profiles_status"] = profiles_status

    # Build full forecast hour map (f00-f84 every 3h) for the latest cycle
    expected_hours = list(range(0, 87, 3))  # 0,3,6,...,84
    if current_date and current_cycle:
        cycle_hr = int(current_cycle.replace("t", "").replace("z", ""))
        run_dt = datetime(int(current_date[:4]), int(current_date[4:6]), int(current_date[6:8]),
                         cycle_hr, tzinfo=timezone.utc)
        latest_pattern = f"nam.{current_date}.{current_cycle}.awphys"
        existing_hours = {int(f["hour"]) for f in file_entries
                        if latest_pattern in f["name"] and f["size_mb"] > 0.1}

        timeline = []
        # Always show profiled indicator when profile data exists (even if cycle doesn't match)
        profiled_for_cycle = profiled_hours
        for fh in expected_hours:
            valid_dt = run_dt + timedelta(hours=fh)
            fh_str = f"{fh:02d}" if fh < 100 else str(fh)
            if fh in existing_hours:
                timeline.append({
                    "hour": fh_str,
                    "valid_utc": valid_dt.strftime("%Y-%m-%d %HZ"),
                    "status": "completed",
                    "profiled": fh in profiled_for_cycle,
                })
            elif fh in checkpoint_hours:
                timeline.append({
                    "hour": fh_str,
                    "valid_utc": valid_dt.strftime("%Y-%m-%d %HZ"),
                    "status": "downloading",
                    "profiled": False,
                })
            else:
                timeline.append({
                    "hour": fh_str,
                    "valid_utc": valid_dt.strftime("%Y-%m-%d %HZ"),
                    "status": "missing",
                    "profiled": False,
                })
        result["timeline"] = timeline
        result["completion"] = round((len(existing_hours) / TOTAL_FORECAST_HOURS) * 100, 1)
    else:
        result["timeline"] = []

    result["files"] = file_entries
    result["total_size_mb"] = round(total_size / (1024 * 1024), 1)
    result["cycle"] = current_cycle
    result["date"] = current_date
    result["available_cycles"] = sorted(cycle_set) if cycle_set else []

    # ── Determine expected next cycle ──
    # NAM cycles: 00, 06, 12, 18Z. Available ~3 hours after cycle start.
    # Cron runs at 03, 09, 15, 21 UTC (aligned with NAM release + delay)
    NAM_CYCLES = [0, 6, 12, 18]
    now_utc = datetime.now(timezone.utc)
    today_str = now_utc.strftime("%Y%m%d")
    current_hr = now_utc.hour
    # Find last expected cycle (available 3h after cycle, so 06Z available at ~09Z)
    expected_cycle = None
    for c in reversed(NAM_CYCLES):
        if current_hr >= c + 3:  # cycle data should be available by now
            expected_cycle = f"t{c:02d}z"
            break
    if expected_cycle is None and current_hr < 3:
        # Before 03Z: previous day's 18Z is the latest expected
        expected_cycle = "t18z"
        today_str = (now_utc - timedelta(days=1)).strftime("%Y%m%d")
    result["expected_cycle"] = expected_cycle
    result["expected_date"] = today_str
    # Flag if we're behind
    if current_date and current_cycle:
        latest_on_disk = (current_date, current_cycle)
        expected = (today_str, expected_cycle) if expected_cycle else (current_date, current_cycle)
        if latest_on_disk < expected:
            result["cycle_lag"] = True
        else:
            result["cycle_lag"] = False
    else:
        result["cycle_lag"] = True

    # Read last N lines of download log
    if os.path.exists(NAM_LOG_FILE):
        try:
            with open(NAM_LOG_FILE) as f:
                lines = f.readlines()
            result["last_log_lines"] = [l.strip() for l in lines[-20:]]
        except Exception:
            pass

    # Determine stale data
    if current_date:
        try:
            cycle_dt = datetime.strptime(current_date, "%Y%m%d")
            age_days = (datetime.now() - cycle_dt).days
            if age_days > 1:
                result["status"] = "stale"
            elif len(file_entries) == 0:
                result["status"] = "empty"
        except Exception:
            pass

    # ── Check pipeline log for last run time ──
    pipeline_log = "/home/progged-ish/metar-automation/logs/nam_pipeline.log"
    result["last_pipeline_run"] = None
    result["pipeline_status"] = "never_run"
    if os.path.exists(pipeline_log):
        try:
            with open(pipeline_log) as f:
                lines = f.readlines()
            # Find last "Pipeline Start" or "Pipeline Complete" line
            last_start = None
            last_complete = None
            for line in lines:
                if "Pipeline Start" in line:
                    # [2026-04-14 03:02:17 UTC] === NAM Pipeline Start ===
                    ts_match = re.search(r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) UTC\]', line)
                    if ts_match:
                        last_start = ts_match.group(1)
                if "Pipeline Complete" in line or "Pipeline exits" in line:
                    ts_match = re.search(r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) UTC\]', line)
                    if ts_match:
                        last_complete = ts_match.group(1)
            if last_complete:
                result["last_pipeline_run"] = last_complete + "Z"
                # Check if pipeline completed normally or was up-to-date
                if "up-to-date" in lines[-1].lower() or "Pipeline exits" in lines[-1]:
                    result["pipeline_status"] = "up_to_date"
                else:
                    result["pipeline_status"] = "completed"
            elif last_start:
                result["last_pipeline_run"] = last_start + "Z"
                result["pipeline_status"] = "running"
        except Exception:
            pass

    return result


# ── Background Refresh ────────────────────────────────────────────────────────

def background_refresh():
    """Periodically refresh all data sources."""
    while True:
        try:
            refresh_all_afds()
        except Exception as e:
            logger.error(f"AFD refresh failed: {e}")
        try:
            _cache["nam"] = scan_nam_data()
        except Exception as e:
            logger.error(f"NAM scan failed: {e}")
        time.sleep(120)  # 2-minute refresh


# ── Routes ─────────────────────────────────────────────────────────────────────



@app.route("/")
def index():
    return render_template_string(DASHBOARD_HTML)


@app.route("/api/afd/<office>")
def api_afd(office):
    if office not in AFD_OFFICES:
        return jsonify({"error": f"Unknown office: {office}"}), 404
    return jsonify({**_cache["afd"][office], "office": office})


@app.route("/api/nam")
def api_nam():
    return jsonify(_cache["nam"])


@app.route("/api/nam/trigger", methods=["POST"])
def api_nam_trigger():
    """Trigger a NAM download via the smart downloader."""
    try:
        import subprocess
        subprocess.Popen(
            ["python3", "/home/progged-ish/metar-automation/scripts/grib2_download/nam_smart_downloader.py"],
            stdout=open("/dev/null", "w"),
            stderr=open("/dev/null", "w"),
        )
        return jsonify({"status": "triggered", "message": "NAM download started in background"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/status")
def api_status():
    afd_ok = sum(1 for v in _cache["afd"].values() if v["status"] == "ok")
    return jsonify({
        "afd_offices_ok": f"{afd_ok}/{len(AFD_OFFICES)}",
        "nam_status": _cache["nam"]["status"],
        "nam_completion": _cache["nam"]["completion"],
        "last_update": _cache["nam"]["updated"],
    })


# ── Scraper & Cron Status ────────────────────────────────────────────────────

SCRAPER_DATA_DIR = "/home/progged-ish/projects/shanes-scraper/data"
SCRAPER_CRON_SCHEDULE = "30 12 * * *"  # 12:30 UTC daily

@app.route("/api/scraper-status")
def api_scraper_status():
    """Return Shane's scraper last run, next scheduled run, and status."""
    result = {
        "last_run": None,
        "last_run_status": "never",
        "next_run": None,
        "cron_schedule": SCRAPER_CRON_SCHEDULE,
        "output_file": None,
    }

    # Read job_status.json for last run time
    job_status_path = os.path.join(SCRAPER_DATA_DIR, "job_status.json")
    if os.path.exists(job_status_path):
        try:
            with open(job_status_path) as f:
                status = json.load(f)
            # {"Shanes Scraper": "13-Apr-2026 16:29Z"}
            for key, val in status.items():
                result["last_run"] = val
                result["last_run_status"] = "completed"
        except Exception:
            pass

    # Check output file mtime as backup timestamp (prefer .txt over .html for v9+)
    txt_path = os.path.join(SCRAPER_DATA_DIR, "Shane_Synoptic_Summary.txt")
    html_path = os.path.join(SCRAPER_DATA_DIR, "Shane_Synoptic_Summary.html")
    best_mtime = 0
    for _p in (txt_path, html_path):
        if os.path.exists(_p):
            try:
                _mt = os.path.getmtime(_p)
                if _mt > best_mtime:
                    best_mtime = _mt
            except Exception:
                pass
    if best_mtime > 0:
        try:
            mtime_dt = datetime.fromtimestamp(best_mtime, tz=timezone.utc)
            result["output_file"] = mtime_dt.strftime("%d-%b-%Y %HZ")
            # Use file mtime if job_status is missing
            if not result["last_run"]:
                result["last_run"] = result["output_file"]
                result["last_run_status"] = "completed"
        except Exception:
            pass

    # Calculate next run time from cron schedule
    # Cron: "30 12 * * *" = 12:30 UTC daily
    now = datetime.now(timezone.utc)
    schedule_parts = SCRAPER_CRON_SCHEDULE.split()
    sched_min = int(schedule_parts[0])
    sched_hr = int(schedule_parts[1])
    next_run = now.replace(hour=sched_hr, minute=sched_min, second=0, microsecond=0)
    if next_run <= now:
        next_run += timedelta(days=1)
    result["next_run"] = next_run.strftime("%Y-%m-%d %HZ")
    result["next_run_iso"] = next_run.isoformat()

    return result


# ── Ops Status Bar ────────────────────────────────────────────────────────────

@app.route("/api/ops-status")
def api_ops_status():
    """Combined ops status for the top-of-page status bar.

    Returns: NAM download cycle, profiles (threats) cycle, scraper last run,
    and process health for Flask + scraper.
    """
    nam = _cache.get("nam", {})
    prof = nam.get("profiles_status", {})

    # Flask health — just confirm we're responding (always true if this returns)
    flask_health = "up"

    # Scraper health — check latest output file (.txt or .html) and is recent (< 24h)
    scraper_health = "unknown"
    scraper_age_h = None
    txt_path = os.path.join(SCRAPER_DATA_DIR, "Shane_Synoptic_Summary.txt")
    html_path = os.path.join(SCRAPER_DATA_DIR, "Shane_Synoptic_Summary.html")
    best_mtime = 0
    for _p in (txt_path, html_path):
        if os.path.exists(_p):
            try:
                _mt = os.path.getmtime(_p)
                if _mt > best_mtime:
                    best_mtime = _mt
            except Exception:
                pass
    if best_mtime > 0:
        age_h = (time.time() - best_mtime) / 3600
        scraper_age_h = round(age_h, 1)
        scraper_health = "ok" if age_h < 24 else "stale"
    else:
        scraper_health = "no_data"

    # NAM download cycle label
    nam_cycle = nam.get("cycle") or "—"
    nam_date = nam.get("date") or "—"
    nam_completion = nam.get("completion", 0)
    nam_lag = nam.get("cycle_lag", True)

    # Profiles / threats active cycle
    threats_cycle = prof.get("extracted_cycle") or "—"
    threats_date = prof.get("extracted_date") or "—"
    threats_current = prof.get("processed", False)

    # Scraper last run
    job_status_path = os.path.join(SCRAPER_DATA_DIR, "job_status.json")
    scraper_last_run = None
    if os.path.exists(job_status_path):
        try:
            with open(job_status_path) as f:
                status = json.load(f)
            for key, val in status.items():
                scraper_last_run = val
                break
        except Exception:
            pass
    if not scraper_last_run:
        # Check both .txt and .html for fallback timestamp
        for _p in (txt_path, html_path):
            if os.path.exists(_p):
                try:
                    mtime = os.path.getmtime(_p)
                    if not scraper_last_run or mtime > (datetime.strptime(scraper_last_run, "%d-%b-%Y %HZ").replace(tzinfo=timezone.utc).timestamp() if scraper_last_run else 0):
                        scraper_last_run = datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%d-%b-%Y %HZ")
                except Exception:
                    pass

    # Pipeline last run
    pipeline_last = nam.get("last_pipeline_run")
    pipeline_status = nam.get("pipeline_status", "never_run")

    return jsonify({
        "nam": {
            "cycle": nam_cycle,
            "date": nam_date,
            "completion": nam_completion,
            "lag": nam_lag,
        },
        "threats": {
            "cycle": threats_cycle,
            "date": threats_date,
            "current": threats_current,
            "stale": not threats_current,
            "num_stations": prof.get("num_stations", 0),
            "forecast_hours_count": prof.get("forecast_hours_count", 0),
            "profile_files_on_disk": prof.get("profile_files_on_disk", 0),
        },
        "scraper": {
            "last_run": scraper_last_run,
            "health": scraper_health,
            "stale": scraper_health in ("stale", "no_data"),
            "age_h": scraper_age_h,
        },
        "pipeline": {
            "last_run": pipeline_last,
            "status": pipeline_status,
            "stale": pipeline_status in ("never_run",),
        },
        "flask": {
            "health": flask_health,
        },
    })


# ── Run-Now Endpoints (background trigger) ──────────────────────────────────

_scraper_running = threading.Event()
_pipeline_running = threading.Event()

SCRAPER_SCRIPT = "/home/progged-ish/projects/shanes-scraper/shanes_nws_scraper_v9.py"
SCRAPER_WORKDIR = "/home/progged-ish/projects/shanes-scraper"
PIPELINE_SCRIPT = "/home/progged-ish/metar-automation/nam_profile_extractor.py"
PIPELINE_WORKDIR = "/home/progged-ish/metar-automation"

@app.route("/api/run-scraper", methods=["POST"])
def api_run_scraper():
    """Trigger the AFD scraper in the background. Refuses if already running."""
    if _scraper_running.is_set():
        return jsonify({"status": "already_running"}), 409
    _scraper_running.set()

    def _run():
        try:
            subprocess.Popen(
                ["python3", SCRAPER_SCRIPT],
                cwd=SCRAPER_WORKDIR,
                stdout=open("/tmp/scraper_run.log", "w"),
                stderr=subprocess.STDOUT,
            )
        finally:
            _scraper_running.clear()

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"status": "started"})


@app.route("/api/run-pipeline", methods=["POST"])
def api_run_pipeline():
    """Trigger the NAM profile extraction pipeline in the background."""
    if _pipeline_running.is_set():
        return jsonify({"status": "already_running"}), 409
    _pipeline_running.set()

    def _run():
        try:
            subprocess.Popen(
                ["python3", PIPELINE_SCRIPT],
                cwd=PIPELINE_WORKDIR,
                stdout=open("/tmp/pipeline_run.log", "w"),
                stderr=subprocess.STDOUT,
            )
        finally:
            _pipeline_running.clear()

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"status": "started"})


# ── Scraper AFD JSON Endpoint ─────────────────────────────────────────────────

@app.route("/api/afd-scraper")
def api_afd_scraper():
    """Serve the latest AFD JSON from Shane's scraper.

    Returns the full afd_latest.json with AI summaries, keywords,
    raw discussions, and CONUS summary. Query ?office=PQR to get
    a single office. Query ?field=ai_summary to get just one field.
    """
    json_path = os.path.join(SCRAPER_DATA_DIR, "afd_latest.json")
    if not os.path.exists(json_path):
        return jsonify({"error": "No scraper data available yet", "status": "no_data"}), 404

    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return jsonify({"error": f"Failed to read scraper data: {e}"}), 500

    # Optional: filter to single office
    office = request.args.get("office", "").upper()
    if office and office in data.get("offices", {}):
        single = data["offices"][office]
        single["generated_at"] = data.get("generated_at", "")
        single["version"] = data.get("version", "")
        field = request.args.get("field")
        if field and field in single:
            return jsonify({field: single[field], "office": office})
        return jsonify(single)

    # Optional: return just one field across all offices
    field = request.args.get("field")
    if field and field in data:
        return jsonify({field: data[field]})

    return jsonify(data)


@app.route("/api/afd-scraper/offices")
def api_afd_scraper_offices():
    """Return just the list of available offices from the latest scraper run."""
    json_path = os.path.join(SCRAPER_DATA_DIR, "afd_latest.json")
    if not os.path.exists(json_path):
        return jsonify({"error": "No scraper data available", "offices": []}), 404

    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return jsonify({"error": str(e), "offices": []}), 500

    offices = data.get("offices", {})
    summary = {
        "generated_at": data.get("generated_at", ""),
        "version": data.get("version", ""),
        "office_count": len(offices),
        "offices": {k: {"state": v.get("state", ""), "state_abbrev": v.get("state_abbrev", ""), "url": v.get("url", ""), "fetched_at": v.get("fetched_at", "")} for k, v in offices.items()},
    }
    return jsonify(summary)


# ── Synoptic Scraper Integration ────────────────────────────────────────────────

SYNOPTIC_HTML_PATH = os.path.join(SCRAPER_DATA_DIR, "Shane_Synoptic_Summary.txt")

@app.route("/synoptic")
def synoptic_page():
    """Serve Shane's full synoptic scraper HTML (V9)."""
    if not os.path.exists(SYNOPTIC_HTML_PATH):
        return render_template_string('''
<!DOCTYPE html><html><head><meta charset="UTF-8">
<title>Synoptic — No Data</title>
<style>
  body { font-family: 'JetBrains Mono', monospace; background: #0a0e17; color: #e2e8f0;
         display: flex; align-items: center; justify-content: center; min-height: 100vh; }
  .msg { text-align: center; }
  h1 { color: #f87171; font-size: 1.2em; letter-spacing: 2px; }
  p { color: #64748b; font-size: 0.85em; margin-top: 8px; }
  a { color: #38bdf8; text-decoration: none; }
</style></head><body>
<div class="msg">
  <h1>☤ NO SYNOPTIC DATA</h1>
  <p>Run the scraper first, then refresh.</p>
  <p style="margin-top:16px"><a href="/">← Back to Operations</a></p>
</div>
</body></html>
'''), 404
    with open(SYNOPTIC_HTML_PATH, "r", encoding="utf-8") as f:
        html_content = f.read()
    return html_content


@app.route("/api/synoptic/summary")
def api_synoptic_summary():
    """Return CONUS summary + regional narratives from latest scraper run.

    Lightweight endpoint for the dashboard widget — avoids loading full 1.3MB HTML.
    """
    json_path = os.path.join(SCRAPER_DATA_DIR, "afd_latest.json")
    if not os.path.exists(json_path):
        return jsonify({"error": "No scraper data", "status": "no_data"}), 404
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    offices = data.get("offices", {})
    # Count offices with AI summaries
    ai_count = sum(1 for v in offices.values() if v.get("ai_summary"))

    return jsonify({
        "execution_time": data.get("execution_time", ""),
        "conus_summary": data.get("conus_summary", ""),
        "regional_narratives": data.get("regional_narratives", {}),
        "keyword_counts": data.get("keyword_counts", {}),
        "office_count": len(offices),
        "ai_summary_count": ai_count,
    })


NAM_PROFILES_DIR = os.path.join(NAM_DATA_DIR, "profiles")
_profiles_cache = {"stations": [], "info": {}, "loaded": None, "info_path": None}


def _load_profiles_cache(force=False):
    """Load and cache station list from profile JSONs (expensive on NTFS).
    Only reloads if the latest info file has changed since last load."""
    if not os.path.exists(NAM_PROFILES_DIR):
        return
    info_files = sorted(glob.glob(os.path.join(NAM_PROFILES_DIR, "nam_*_info.json")))
    if not info_files:
        return
    latest_info = info_files[-1]
    # Skip reload if same info file as last time (unless forced)
    if not force and _profiles_cache["info_path"] == latest_info:
        return
    try:
        with open(latest_info) as f:
            info = json.load(f)
    except Exception:
        return
    cycle_label = info.get("cycle", "unknown")
    station_files = glob.glob(os.path.join(NAM_PROFILES_DIR, f"nam_{cycle_label}_*.json"))
    stations = []
    for sf in station_files:
        if "_info" in sf:
            continue
        try:
            with open(sf) as f:
                stn = json.load(f)
            stations.append({
                "code": stn["code"],
                "name": stn["name"],
                "lat": stn["lat"],
                "lon": stn["lon"],
                "zone": stn.get("zone", ""),
                "num_fhours": len(stn.get("forecast_hours", {})),
            })
        except Exception:
            continue
    stations.sort(key=lambda s: s["code"])
    _profiles_cache["stations"] = stations
    _profiles_cache["info"] = info
    _profiles_cache["loaded"] = datetime.now(timezone.utc).isoformat()
    _profiles_cache["info_path"] = latest_info
    logger.info(f"Profiles cache updated: {len(stations)} stations, cycle={info.get('cycle')}")


@app.route("/api/nam/profiles")
def api_nam_profiles():
    """Return cycle info + station list for the latest NAM profile extraction."""
    # Reload if info file changed (new cycle available)
    _load_profiles_cache(force=False)
    if not _profiles_cache["info"]:
        logger.warning("Profiles cache empty after load attempt")
        return jsonify({"error": "No profile extractions found"}), 404
    logger.info(f"Profiles cache: info_path={_profiles_cache.get('info_path')}, cycle={_profiles_cache['info'].get('cycle')}")
    result = {"info": _profiles_cache["info"], "stations": _profiles_cache["stations"]}
    logger.info(f"Profiles API returning: {len(result['stations'])} stations, cycle={result['info'].get('cycle')}")
    return jsonify(result)


@app.route("/api/nam/profiles/<station>")
def api_nam_profile_station(station):
    """Return full sounding data for a specific station."""
    if not os.path.exists(NAM_PROFILES_DIR):
        return jsonify({"error": "No profiles directory found"}), 404
    # Find latest info file to determine cycle label
    info_files = sorted(glob.glob(os.path.join(NAM_PROFILES_DIR, "nam_*_info.json")))
    if not info_files:
        return jsonify({"error": "No profile extractions found"}), 404
    with open(info_files[-1]) as f:
        info = json.load(f)
    cycle_label = info.get("cycle", "unknown")
    fname = f"nam_{cycle_label}_{station}.json"
    fpath = os.path.join(NAM_PROFILES_DIR, fname)
    if not os.path.exists(fpath):
        return jsonify({"error": f"Station {station} not found"}), 404
    with open(fpath) as f:
        return json.load(f)


@app.route("/skewt")
def skewt_page():
    return render_template_string(SKEWT_HTML)


# ── Threats API ─────────────────────────────────────────────────────────────────

# BTW target AGL layers (label, target_agl_meters)
# 7 layers per HAVOC Best Transfer Winds: 0-1kft, 0-2kft, ... 0-9kft AGL
# Within each layer, BTW = vector-mean of u,v interpolated at regular
# sub-layer intervals from sfc to the AGL ceiling.
BTW_AGL_LAYERS = [
    ("010", 304.8),   # 0-1000ft AGL  = 0-304.8m
    ("020", 609.6),   # 0-2000ft AGL  = 0-609.6m
    ("030", 914.4),   # 0-3000ft AGL  = 0-914.4m
    ("040", 1219.2),  # 0-4000ft AGL  = 0-1219.2m
    ("050", 1524.0),  # 0-5000ft AGL  = 0-1524.0m
    ("070", 2133.6),  # 0-7000ft AGL  = 0-2133.6m
    ("090", 2743.2),  # 0-9000ft AGL  = 0-2743.2m
]

# Sub-layer vertical resolution for interpolation (every 100m AGL)
# 100m gives good sampling even for the thinnest layer (010 = 0-304.8m)
# while keeping compute reasonable for the deepest (090 = 0-2743.2m)
BTW_SUB_LAYER_M = 100


def _interp_wind_at_agl(levels: list[dict], elevation: float, target_agl: float) -> tuple | None:
    """Linearly interpolate u,v at a precise AGL altitude (meters).
    
    Uses geopotential height_m from isobaric levels, converted to AGL
    by subtracting station elevation. Finds bracketing levels and 
    linearly interpolates u_ms and v_ms.
    
    Returns (u_ms, v_ms) or None if bracketing levels unavailable.
    """
    target_msl = target_agl + elevation
    
    # Find two levels bracketing the target height
    below = None
    above = None
    for lvl in levels:
        h = lvl.get("height_m")
        if h is None:
            continue
        if h <= target_msl:
            below = lvl
        elif above is None and h > target_msl:
            above = lvl
            break
    
    if below is None and above is None:
        return None
    
    if below is None:
        # Target below lowest level — use lowest level directly
        above_ = above if above else levels[0]
        u = above_.get("u_ms")
        v = above_.get("v_ms")
        return (u, v) if u is not None and v is not None else None
    
    if above is None:
        # Target above highest level — use highest level directly
        u = below.get("u_ms")
        v = below.get("v_ms")
        return (u, v) if u is not None and v is not None else None
    
    h_lo = below.get("height_m")
    h_hi = above.get("height_m")
    if h_lo is None or h_hi is None or h_hi == h_lo:
        return None
    
    u_lo, v_lo = below.get("u_ms"), below.get("v_ms")
    u_hi, v_hi = above.get("u_ms"), above.get("v_ms")
    if any(x is None for x in (u_lo, v_lo, u_hi, v_hi)):
        return None
    
    frac = (target_msl - h_lo) / (h_hi - h_lo)
    frac = max(0.0, min(1.0, frac))  # clamp
    u = u_lo + frac * (u_hi - u_lo)
    v = v_lo + frac * (v_hi - v_lo)
    return (u, v)


def _interp_temp_at_agl(levels: list[dict], elevation: float, target_agl: float) -> float | None:
    """Linearly interpolate temperature (°C) at a precise AGL altitude (meters).
    
    Returns temp_C or None if bracketing levels unavailable.
    """
    target_msl = target_agl + elevation
    
    below = None
    above = None
    for lvl in levels:
        h = lvl.get("height_m")
        if h is None:
            continue
        if h <= target_msl:
            below = lvl
        elif above is None and h > target_msl:
            above = lvl
            break
    
    if below is None and above is None:
        return None
    
    if below is None:
        above_ = above if above else levels[0]
        return above_.get("temp_C")
    
    if above is None:
        return below.get("temp_C")
    
    h_lo = below.get("height_m")
    h_hi = above.get("height_m")
    t_lo = below.get("temp_C")
    t_hi = above.get("temp_C")
    if any(x is None for x in (h_lo, h_hi, t_lo, t_hi)) or h_hi == h_lo:
        return None
    
    frac = (target_msl - h_lo) / (h_hi - h_lo)
    frac = max(0.0, min(1.0, frac))
    return t_lo + frac * (t_hi - t_lo)


# ── Downward Forcing (HAVOC) ──────────────────────────────────────────────────

# Downward Forcing alpha: controls how quickly DF% saturates toward 100%
# At alpha=0.3: +5 Pa/s → 78%, +10 Pa/s → 95%
DF_ALPHA = 0.3

# Station-specific BTW depth caps at 100% DF
# Default: levels 010-050 (0-5 kft AGL)
# Downslope: levels 010-090 (0-9 kft AGL)
DOWNSLOPE_STATIONS = {"KRNO", "KRTS"}
LEE_SIDE_STATIONS = {"KDEN"}

def _compute_downward_forcing(omega_500: float | None, alpha: float = DF_ALPHA) -> float | None:
    """Compute HAVOC Downward Forcing percentage from 500 hPa omega.
    
    Zero-floor exponential: any omega ≤ 0 → 0% (no downward forcing, PVA regime).
    Positive omega (subsidence) maps to DF% = 100 × (1 − e^(−α × omega)).
    
    Returns DF% as 0-100 float, or None if omega_500 unavailable.
    """
    if omega_500 is None:
        return None
    if omega_500 <= 0:
        return 0.0
    import math
    df_pct = 100.0 * (1.0 - math.exp(-alpha * omega_500))
    return round(df_pct, 1)


def _get_btw_depth_stations(station_code: str) -> str:
    """Return BTW depth category for determining max BTW at 100% DF.
    
    'downslope' (010-090): KRNO, KRTS — Sierra Nevada lee-side
    'lee_side' (010-060): KDEN — Rocky Mtn downslope (intermediate)
    'standard' (010-050): all others
    """
    if station_code in DOWNSLOPE_STATIONS:
        return "downslope"
    if station_code in LEE_SIDE_STATIONS:
        return "lee_side"
    return "standard"


def _compute_btw_for_station(profile: dict) -> dict:
    """Compute Best Transfer Winds (BTW) for AGL layers from a station profile.
    
    HAVOC Mode 1 — Momentum Transfer: check winds at every level in the
    lower 5000 ft + 7000 and 9000 ft AGL. Vector-mean of u,v interpolated
    at regular sub-layer intervals from sfc to each AGL ceiling.
    
    Returns per-forecast-hour dict with BTW layers + surface gust + PBL wind.
    """
    elevation = profile.get("elevation_m") or profile.get("forecast_hours", {}).get("f00", {}).get("surface", {}).get("elevation_m", 0) or 0
    result = {"station": profile["code"], "zone": profile.get("zone", ""),
              "name": profile.get("name", ""), "elevation_m": elevation}
    fhours = {}
    
    for fh_key, fh_data in profile.get("forecast_hours", {}).items():
        levels = fh_data.get("levels", [])
        surface = fh_data.get("surface", {})
        pbl = fh_data.get("pbl", {})
        qg = fh_data.get("qg_fields", {})
        
        fhour = {"gust_kt": surface.get("gust_kt"), "pbl_height_m": pbl.get("pbl_height_m")}
        
        # ── Extract 500 hPa omega and compute Downward Forcing % ──
        omega_500 = None
        for lvl in levels:
            if lvl.get("hPa") == 500.0:
                omega_500 = lvl.get("omega_Pas")
                break
        df_pct = _compute_downward_forcing(omega_500)
        fhour["df_pct"] = df_pct
        fhour["omega_500_Pas"] = omega_500
        
        # Also extract QG fields for this fhour (div, absvort at 5 levels)
        fhour["qg_fields"] = qg
        
        # ── Compute BTW for each AGL layer ──
        sfc_temp = surface.get("temp_C")  # for lapse rate calc
        
        for label, max_agl in BTW_AGL_LAYERS:
            u_sum, v_sum, n = 0.0, 0.0, 0
            # Interpolate at every BTW_SUB_LAYER_M from sfc up to max_agl
            agl = 0.0
            while agl <= max_agl:
                if agl == 0.0:
                    # Surface: use 10m wind from surface data if available,
                    # otherwise nearest isobaric level (typically 1000 hPa)
                    sfc_u = surface.get("u10_ms")
                    sfc_v = surface.get("v10_ms")
                    if sfc_u is None or sfc_v is None:
                        # Fallback to lowest isobaric level
                        for lvl in levels:
                            if lvl.get("u_ms") is not None and lvl.get("v_ms") is not None:
                                sfc_u, sfc_v = lvl["u_ms"], lvl["v_ms"]
                                break
                    if sfc_u is not None and sfc_v is not None:
                        u_sum += sfc_u
                        v_sum += sfc_v
                        n += 1
                else:
                    interp = _interp_wind_at_agl(levels, elevation, agl)
                    if interp is not None:
                        u_sum += interp[0]
                        v_sum += interp[1]
                        n += 1
                agl += BTW_SUB_LAYER_M
            
            if n > 0:
                import math as _m
                u_mean = u_sum / n
                v_mean = v_sum / n
                spd_ms = _m.sqrt(u_mean**2 + v_mean**2)
                dir_deg = (270 - _m.degrees(_m.atan2(v_mean, u_mean))) % 360
                fhour[f"btw_{label}_spd_kt"] = round(spd_ms * 1.94384, 0)
                fhour[f"btw_{label}_dir"] = round(dir_deg, 0)
                fhour[f"btw_{label}_u_ms"] = round(u_mean, 2)
                fhour[f"btw_{label}_v_ms"] = round(v_mean, 2)
            
            # Lapse rate: surface to this AGL ceiling (°C/km)
            # Positive = normal (temp drops with height), negative = inversion
            if sfc_temp is not None and max_agl > 0:
                temp_at_top = _interp_temp_at_agl(levels, elevation, max_agl)
                if temp_at_top is not None:
                    height_km = max_agl / 1000.0
                    lapse = (sfc_temp - temp_at_top) / height_km  # °C/km
                    fhour[f"btw_{label}_lapse_C_km"] = round(lapse, 1)
        
        # ── DF%-weighted BTW surface wind estimate ──
        # At DF% ≈ 0% (lift): average BTW across all levels 010-090
        # At DF% ≈ 100% (subsidence): MAX BTW in depth column (station-dependent)
        # Between: linear blend of mean and max
        depth_type = _get_btw_depth_stations(profile["code"])
        if depth_type == "downslope":
            btw_max_labels = ["010", "020", "030", "040", "050", "070", "090"]
        elif depth_type == "lee_side":
            btw_max_labels = ["010", "020", "030", "040", "050", "070"]  # KDEN: 0-7kft
        else:
            btw_max_labels = ["010", "020", "030", "040", "050"]
        
        btw_mean_labels = ["010", "020", "030", "040", "050", "070", "090"]
        
        # Collect BTW speeds for mean calculation (all levels)
        mean_spds = [fhour.get(f"btw_{lbl}_spd_kt") for lbl in btw_mean_labels
                      if fhour.get(f"btw_{lbl}_spd_kt") is not None]
        # Collect BTW speeds for max calculation (depth-dependent)
        max_spds = [fhour.get(f"btw_{lbl}_spd_kt") for lbl in btw_max_labels
                     if fhour.get(f"btw_{lbl}_spd_kt") is not None]
        
        if mean_spds and max_spds and df_pct is not None:
            import math as _m2
            avg_btw = sum(mean_spds) / len(mean_spds)
            peak_btw = max(max_spds)
            # Weighted blend: low DF → average, high DF → max
            df_frac = df_pct / 100.0
            btw_surface_kt = (1.0 - df_frac) * avg_btw + df_frac * peak_btw
            # Find the direction of the peak BTW level for direction assignment
            peak_label = btw_max_labels[max_spds.index(peak_btw)]
            fhour["btw_surface_kt"] = round(btw_surface_kt, 0)
            fhour["btw_surface_dir"] = fhour.get(f"btw_{peak_label}_dir")
            fhour["btw_mean_kt"] = round(avg_btw, 0)
            fhour["btw_peak_kt"] = round(peak_btw, 0)
            fhour["btw_depth_type"] = depth_type
        
        # PBL-level wind (already has speed/dir from extractor)
        fhour["pbl_wind_spd_kt"] = pbl.get("pbl_wind_spd_kt")
        fhour["pbl_wind_dir"] = pbl.get("pbl_wind_dir")
        
        fhours[fh_key] = fhour
    
    result["forecast_hours"] = fhours
    return result


@app.route("/api/nam/threats")
def api_nam_threats():
    """Return BTW + gust data for all stations, organized by zone."""
    if not os.path.exists(NAM_PROFILES_DIR):
        return jsonify({"error": "No profiles directory found"}), 404
    info_files = sorted(glob.glob(os.path.join(NAM_PROFILES_DIR, "nam_*_info.json")))
    if not info_files:
        return jsonify({"error": "No profile extractions found"}), 404
    with open(info_files[-1]) as f:
        info = json.load(f)
    cycle_label = info.get("cycle", "unknown")
    
    stations = []
    zone_order = {"North": 0, "Central": 1, "South": 2, "SOUTHCOM": 3}
    
    for sf in glob.glob(os.path.join(NAM_PROFILES_DIR, f"nam_{cycle_label}_*.json")):
        if "_info" in sf:
            continue
        try:
            with open(sf) as f:
                profile = json.load(f)
            btw = _compute_btw_for_station(profile)
            stations.append(btw)
        except Exception:
            continue
    
    stations.sort(key=lambda s: (zone_order.get(s["zone"], 99), s["station"]))
    
    zones = {}
    for s in stations:
        z = s["zone"] or "Unknown"
        if z not in zones:
            zones[z] = []
        zones[z].append(s)
    
    return jsonify({"info": info, "zones": zones})


@app.route("/threats")
def threats_page():
    return render_template_string(THREATS_HTML)


# ── Wiki ──────────────────────────────────────────────────────────────────────

WIKI_DIR = os.path.expanduser("~/wiki")

@app.route("/wiki")
def wiki_page():
    return render_template_string(WIKI_HTML)


# ── WX Events Full Page ───────────────────────────────────────────────────────

EVENTS_HTML = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>WX Event Tracker</title>
<style>
  :root {
    --bg-deep: #0a0e17;
    --bg-panel: #111827;
    --bg-card: #1a2332;
    --border: #2a3a4e;
    --text: #e2e8f0;
    --text-dim: #64748b;
    --accent: #38bdf8;
    --accent2: #818cf8;
    --green: #34d399;
    --red: #f87171;
    --amber: #fbbf24;
    --orange: #fb923c;
    --orange-dark: #c2410c;
    --magenta: #e879f9;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: 'JetBrains Mono', 'Fira Code', 'Cascadia Code', monospace;
    background: var(--bg-deep); color: var(--text); min-height: 100vh;
  }
  .hud { max-width: 100%; margin: 0 auto; padding: 16px; }

  /* ── Top Bar ─────────────────────── */
  .topbar {
    display: flex; align-items: center; justify-content: space-between;
    padding: 12px 20px; margin-bottom: 16px;
    background: var(--bg-panel); border: 1px solid var(--border); border-radius: 8px;
  }
  .topbar-left { display: flex; align-items: center; gap: 12px; }
  .topbar-title { font-size: 1.1em; font-weight: 700; letter-spacing: 2px; color: var(--accent); }
  .topbar-sub { font-size: 0.75em; color: var(--text-dim); letter-spacing: 1px; }
  .topbar-right { display: flex; align-items: center; gap: 16px; font-size: 0.8em; color: var(--text-dim); }
  .pulse { width: 8px; height: 8px; border-radius: 50%; display: inline-block; margin-right: 6px; }
  .pulse-ok { background: var(--green); box-shadow: 0 0 6px var(--green); }
  .nav-links { display: flex; gap: 16px; margin-top: 8px; font-size: 0.7em; letter-spacing: 1px; }
  .nav-links a { color: var(--accent); text-decoration: none; font-weight: 600; }
  .nav-links a:hover { text-decoration: underline; }
  .pulse-warn { background: var(--amber); box-shadow: 0 0 6px var(--amber); }
  .pulse-err { background: var(--red); box-shadow: 0 0 6px var(--red); }

  /* ── Ops Status Bar ──────────────── */
  .opsbar {
    display: flex; align-items: stretch; gap: 0;
    margin-bottom: 12px; border-radius: 8px; overflow: hidden;
    border: 1px solid var(--border); background: var(--bg-panel);
    font-size: 0.72em; letter-spacing: 0.5px;
  }
  .opsbar-cell {
    flex: 1; padding: 8px 14px; border-right: 1px solid var(--border);
    display: flex; flex-direction: column; gap: 2px; min-width: 0;
  }
  .opsbar-cell:last-child { border-right: none; }
  .opsbar-label {
    font-size: 0.75em; color: var(--text-dim); text-transform: uppercase;
    letter-spacing: 1.5px; font-weight: 600; white-space: nowrap;
  }
  .opsbar-value {
    font-size: 1.05em; color: var(--text); font-weight: 400;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .pill { padding: 1px 7px; border-radius: 8px; font-size: 0.85em; font-weight: 600; }
  .pill-ok { background: rgba(52,211,153,0.15); color: var(--green); }
  .pill-warn { background: rgba(251,191,36,0.15); color: var(--amber); }
  .pill-err { background: rgba(248,113,113,0.15); color: var(--red); }
  .pill-info { background: rgba(56,189,248,0.15); color: var(--accent); }

  /* ── Filters ────────────────────── */
  .filters {
    display: flex; gap: 12px; align-items: center; flex-wrap: wrap;
    padding: 10px 16px; margin-bottom: 12px;
    background: var(--bg-panel); border: 1px solid var(--border); border-radius: 8px;
    font-size: 0.75em;
  }
  .filters label { color: var(--text-dim); text-transform: uppercase; letter-spacing: 1px; }
  .filters select, .filters input {
    background: var(--bg-deep); color: var(--text);
    border: 1px solid var(--border); border-radius: 4px;
    padding: 4px 8px; font-family: inherit; font-size: 0.95em;
    cursor: pointer;
  }
  .filters select:focus, .filters input:focus {
    outline: none; border-color: var(--accent);
  }
  .filters .filter-group { display: flex; align-items: center; gap: 6px; }
  .filters .divider { color: var(--border); }

  /* ── Zone sections ──────────────── */
  .zone-section { margin-bottom: 20px; }
  .zone-header {
    display: flex; align-items: center; gap: 10px;
    padding: 8px 16px; margin-bottom: 8px;
    background: var(--bg-panel); border: 1px solid var(--border); border-radius: 6px;
    font-size: 0.85em; font-weight: 700; letter-spacing: 2px; color: var(--accent2);
  }
  .zone-count { font-size: 0.7em; color: var(--text-dim); font-weight: 400; margin-left: auto; }

  /* ── Event Table ────────────────── */
  .event-table-wrap { overflow-x: auto; }
  .event-table {
    width: 100%; border-collapse: collapse;
    font-size: 0.72em; letter-spacing: 0.3px;
  }
  .event-table th {
    background: var(--bg-panel); color: var(--text-dim);
    text-transform: uppercase; letter-spacing: 1.5px; font-weight: 600;
    padding: 8px 12px; text-align: left;
    border-bottom: 1px solid var(--border); white-space: nowrap;
    position: sticky; top: 0; z-index: 2;
  }
  .event-table td {
    padding: 7px 12px; border-bottom: 1px solid rgba(42,58,78,0.5);
    vertical-align: middle;
  }
  .event-table tr:hover td { background: rgba(56,189,248,0.04); }
  .event-table tr:last-child td { border-bottom: none; }

  .type-pill {
    display: inline-block; padding: 1px 8px; border-radius: 8px;
    font-weight: 700; font-size: 0.9em; letter-spacing: 0.5px;
  }
  .type-SNOW, .type-SNOW_SQUALL { background: rgba(56,189,248,0.2); color: var(--accent); }
  .type-WIND_GUST, .type-TSTM_WIND_GUST, .type-HIGH_WIND { background: rgba(251,191,36,0.2); color: var(--amber); }
  .type-HAIL { background: rgba(251,146,60,0.2); color: var(--orange); }
  .type-TORNADO { background: rgba(248,113,113,0.2); color: var(--red); }
  .type-FLASH_FLOOD, .type-HEAVY_RAIN, .type-FLOOD { background: rgba(56,189,248,0.15); color: var(--accent2); }
  .type-FREEZING_RAIN, .type-FREEZING_DRIZZLE { background: rgba(129,140,248,0.2); color: var(--accent2); }
  .type-BLIZZARD, .type-HEAVY_SNOW { background: rgba(232,121,249,0.2); color: var(--magenta); }
  .type-OTHER { background: rgba(100,116,139,0.2); color: var(--text-dim); }

  .mag { font-weight: 700; color: var(--text); }
  .dist { color: var(--text-dim); }
  .source-LSR { color: var(--green); }
  .source-NWS_WARN { color: var(--amber); }
  .source-METAR_PEAK { color: var(--accent); }
  .remarks { color: var(--text-dim); font-size: 0.9em; max-width: 300px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .site-id { color: var(--accent2); font-weight: 600; }
  .state-badge { color: var(--text-dim); font-size: 0.85em; }

  /* ── Type Legend ────────────────── */
  .legend {
    display: flex; gap: 12px; align-items: center; flex-wrap: wrap;
    padding: 8px 16px; margin-bottom: 12px;
    background: var(--bg-panel); border: 1px solid var(--border); border-radius: 8px;
    font-size: 0.68em; color: var(--text-dim);
  }
  .legend span { display: flex; align-items: center; gap: 5px; }
  .legend .sep { color: var(--border); margin: 0 4px; }

  /* ── Empty state ─────────────────── */
  .empty-state {
    text-align: center; padding: 40px;
    color: var(--text-dim); font-size: 0.85em;
  }
  .empty-state .icon { font-size: 2em; margin-bottom: 8px; opacity: 0.4; }

  /* ── Pagination ──────────────────── */
  .pagination { display: flex; gap: 8px; align-items: center; justify-content: center; padding: 12px; }
  .pagination button {
    background: var(--bg-panel); color: var(--text); border: 1px solid var(--border);
    border-radius: 4px; padding: 4px 12px; font-family: inherit; font-size: 0.8em;
    cursor: pointer;
  }
  .pagination button:hover:not(:disabled) { border-color: var(--accent); color: var(--accent); }
  .pagination button:disabled { opacity: 0.4; cursor: not-allowed; }
  .pagination .page-info { font-size: 0.75em; color: var(--text-dim); }

  /* ── Footer ──────────────────────── */
  .footer {
    text-align: center; padding: 12px; font-size: 0.7em; color: var(--text-dim);
    border-top: 1px solid var(--border); margin-top: 16px;
  }
</style>
</head>
<body>
<div class="hud">

  <!-- ── Top Bar ── -->
  <div class="topbar">
    <div class="topbar-left">
      <div class="topbar-title">⚡ WX EVENT TRACKER</div>
      <div class="topbar-sub">V.I.L.E. VALIDATION DB</div>
      <div class="nav-links">
        <a href="/">OPS</a>
        <a href="/skewt">SKEW-T</a>
        <a href="/threats">THREATS</a>
        <a href="/synoptic">SYNOPTIC</a>
        <a href="/events" style="color:var(--orange);font-weight:700;">EVENTS</a>
        <a href="/wiki">WIKI</a>
      </div>
    </div>
    <div class="topbar-right">
      <span><span class="pulse pulse-ok" id="statusDot"></span><span id="statusText">Connecting…</span></span>
      <span id="zuluTime">—</span>
    </div>
  </div>

  <!-- ── Ops Stats ── -->
  <div class="opsbar">
    <div class="opsbar-cell">
      <div class="opsbar-label">Total Events</div>
      <div class="opsbar-value" id="statTotal">—</div>
    </div>
    <div class="opsbar-cell">
      <div class="opsbar-label">Last 7 Days</div>
      <div class="opsbar-value" id="stat7d">—</div>
    </div>
    <div class="opsbar-cell">
      <div class="opsbar-label">Last 30 Days</div>
      <div class="opsbar-value" id="stat30d">—</div>
    </div>
    <div class="opsbar-cell">
      <div class="opsbar-label">Sites w/ Events</div>
      <div class="opsbar-value" id="statSites">—</div>
    </div>
    <div class="opsbar-cell">
      <div class="opsbar-label">Last Event</div>
      <div class="opsbar-value" id="statLastEvent">—</div>
    </div>
    <div class="opsbar-cell">
      <div class="opsbar-label">Last Ingest</div>
      <div class="opsbar-value" id="statIngest">—</div>
    </div>
  </div>

  <!-- ── Filters ── -->
  <div class="filters">
    <div class="filter-group">
      <label>Type</label>
      <select id="fType">
        <option value="ALL">ALL</option>
        <option value="SNOW">SNOW</option>
        <option value="SNOW_SQUALL">SNOW SQUALL</option>
        <option value="WIND_GUST">WIND GUST</option>
        <option value="TSTM_WIND_GUST">TSTM WIND GUST</option>
        <option value="HAIL">HAIL</option>
        <option value="TORNADO">TORNADO</option>
        <option value="FLASH_FLOOD">FLASH FLOOD</option>
        <option value="HEAVY_RAIN">HEAVY RAIN</option>
        <option value="FREEZING_RAIN">FREEZING RAIN</option>
        <option value="BLIZZARD">BLIZZARD</option>
        <option value="HIGH_WIND">HIGH WIND</option>
      </select>
    </div>
    <div class="divider">|</div>
    <div class="filter-group">
      <label>Site</label>
      <select id="fSite">
        <option value="ALL">ALL</option>
      </select>
    </div>
    <div class="divider">|</div>
    <div class="filter-group">
      <label>State</label>
      <select id="fState">
        <option value="ALL">ALL</option>
      </select>
    </div>
    <div class="divider">|</div>
    <div class="filter-group">
      <label>Start</label>
      <input type="date" id="fStartDate" style="width:130px;">
    </div>
    <div class="filter-group">
      <label>End</label>
      <input type="date" id="fEndDate" style="width:130px;">
    </div>
    <div class="divider">|</div>
    <div class="filter-group">
      <label>Dist (mi)</label>
      <input type="number" id="fMinDist" placeholder="min" min="0" max="500" style="width:65px;">
      <span style="color:var(--text-dim)">–</span>
      <input type="number" id="fMaxDist" placeholder="max" min="0" max="500" style="width:65px;">
    </div>
    <div class="divider">|</div>
    <div class="filter-group">
      <label>Sort</label>
      <select id="fSortBy">
        <option value="timestamp">Date</option>
        <option value="distance">Distance</option>
        <option value="magnitude">Magnitude</option>
        <option value="type">Type</option>
      </select>
      <select id="fSortOrder">
        <option value="desc">↓</option>
        <option value="asc">↑</option>
      </select>
    </div>
    <div class="divider">|</div>
    <div class="filter-group">
      <label>Days</label>
      <select id="fDays">
        <option value="7">7</option>
        <option value="14">14</option>
        <option value="30" selected>30</option>
        <option value="60">60</option>
        <option value="90">90</option>
        <option value="365">365</option>
        <option value="all">ALL</option>
      </select>
    </div>
    <div class="divider">|</div>
    <div class="filter-group">
      <label>Limit</label>
      <select id="fLimit">
        <option value="50">50</option>
        <option value="100" selected>100</option>
        <option value="200">200</option>
        <option value="500">500</option>
      </select>
    </div>
    <div class="divider">|</div>
    <div class="filter-group">
      <button onclick="loadEvents(0)" style="background:var(--bg-deep);color:var(--accent);border:1px solid var(--accent);border-radius:4px;padding:4px 12px;cursor:pointer;font-family:inherit;font-size:0.9em;">FILTER</button>
      <button onclick="clearFilters()" style="background:var(--bg-deep);color:var(--text-dim);border:1px solid var(--border);border-radius:4px;padding:4px 10px;cursor:pointer;font-family:inherit;font-size:0.85em;">Clear</button>
    </div>
  </div>

  <!-- ── Type Legend ── -->
  <div class="legend" id="typeLegend">
    <span><span class="type-pill type-SNOW">SN</span> Snow</span>
    <span class="sep">|</span>
    <span><span class="type-pill type-WIND_GUST">WG</span> Wind Gust</span>
    <span class="sep">|</span>
    <span><span class="type-pill type-HAIL">HL</span> Hail</span>
    <span class="sep">|</span>
    <span><span class="type-pill type-TORNADO">TO</span> Tornado</span>
    <span class="sep">|</span>
    <span><span class="type-pill type-FLASH_FLOOD">FF</span> Flash Flood</span>
    <span class="sep">|</span>
    <span><span class="type-pill type-FREEZING_RAIN">FZ</span> Freezing Rain</span>
    <span class="sep">|</span>
    <span><span class="type-pill type-BLIZZARD">BZ</span> Blizzard</span>
  </div>

  <!-- ── Event Table ── -->
  <div class="event-table-wrap">
    <table class="event-table" id="eventTable">
      <thead>
        <tr>
          <th>Timestamp (UTC)</th>
          <th>Type</th>
          <th>Mag</th>
          <th>Location</th>
          <th>Site</th>
          <th>Source</th>
          <th>METAR Match</th>
          <th>Remarks</th>
        </tr>
      </thead>
      <tbody id="eventBody">
        <tr><td colspan="8" class="empty-state"><div class="icon">◷</div>Loading events…</td></tr>
      </tbody>
    </table>
  </div>

  <!-- ── Pagination ── -->
  <div class="pagination" id="pagination"></div>

  <!-- ── Footer ── -->
  <div class="footer">
    WX Event Tracker &mdash; V.I.L.E. Validation DB &mdash;
    Data: Iowa State MESONET LSR + METAR &mdash;
    <span id="dbPath">—</span>
  </div>

</div>

<script>
const API = '';

let currentOffset = 0;
let currentTotal = 0;
let currentLimit = 100;

// ── Zulu clock ──
function updateClock() {
  const now = new Date();
  document.getElementById('zuluTime').textContent =
    now.toISOString().replace('T', ' ').substring(0, 16) + 'Z';
}
setInterval(updateClock, 1000);
updateClock();

// ── Load telemetry ──
async function loadTelemetry() {
  try {
    const r = await fetch(API + '/api/telemetry');
    const d = await r.json();
    document.getElementById('statTotal').innerHTML =
      `<span class="pill pill-info">${d.total_events.toLocaleString()}</span>`;
    document.getElementById('stat7d').textContent = d.recent_events_7d;
    document.getElementById('stat30d').textContent = d.recent_events_30d;
    document.getElementById('statSites').textContent = d.sites_with_events;
    document.getElementById('statLastEvent').textContent = d.last_event
      ? d.last_event.replace('T', ' ').substring(0, 16) + 'Z' : 'None';
    document.getElementById('statIngest').textContent = d.last_ingest_file
      ? d.last_ingest_file.replace('wx_obs_', '').replace('.json', '') + ' @ ' +
        (d.last_ingest_at ? d.last_ingest_at.substring(0, 16) : '?')
      : 'Never';

    const dot = document.getElementById('statusDot');
    const txt = document.getElementById('statusText');
    if (d.total_events > 0) {
      dot.className = 'pulse pulse-ok';
      txt.textContent = 'ONLINE';
    } else {
      dot.className = 'pulse pulse-warn';
      txt.textContent = 'NO DATA';
    }
    document.getElementById('dbPath').textContent = d.db_path || '';

    // Populate state filter
    const states = [...new Set(
      (d.type_counts || []).length ? [] : []
    )];
    // Will be populated from /api/sites
  } catch(e) {
    document.getElementById('statusText').textContent = 'ERROR';
    document.getElementById('statusDot').className = 'pulse pulse-err';
  }
}

// ── Load sites for state and site filters ──
async function loadSites() {
  try {
    const r = await fetch(API + '/api/sites');
    const sites = await r.json();
    const states = [...new Set(sites.map(s => s.state).filter(Boolean))].sort();
    const siteSel = document.getElementById('fSite');
    const stateSel = document.getElementById('fState');
    states.forEach(st => {
      const opt = document.createElement('option');
      opt.value = st; opt.textContent = st;
      stateSel.appendChild(opt);
    });
    // Populate site dropdown with all sites
    sites.forEach(s => {
      const opt = document.createElement('option');
      opt.value = s.site_id;
      opt.textContent = `${s.site_id} (${s.state})`;
      siteSel.appendChild(opt);
    });
  } catch(e) {}
}

// ── Event type CSS class ──
function typeClass(t) {
  if (!t) return 'type-OTHER';
  const m = {
    'SNOW':'SNOW','SNOW_SQUALL':'SNOW_SQUALL','BLIZZARD':'BLIZZARD','HEAVY_SNOW':'BLIZZARD',
    'WIND_GUST':'WIND_GUST','TSTM_WIND_GUST':'WIND_GUST','HIGH_WIND':'WIND_GUST',
    'HAIL':'HAIL',
    'TORNADO':'TORNADO','FUNNEL':'TORNADO',
    'FLASH_FLOOD':'FLASH_FLOOD','FLOOD':'FLASH_FLOOD','HEAVY_RAIN':'FLASH_FLOOD',
    'FREEZING_RAIN':'FREEZING_RAIN','FREEZING_DRIZZLE':'FREEZING_RAIN',
  };
  return 'type-' + (m[t] || 'OTHER');
}

// ── Format type label ──
function typeLabel(t) {
  const m = {
    'SNOW':'SN','SNOW_SQUALL':'SQ','WIND_GUST':'WG','TSTM_WIND_GUST':'TS',
    'HIGH_WIND':'HW','HAIL':'HL','TORNADO':'TO','FLASH_FLOOD':'FF',
    'FLOOD':'FL','HEAVY_RAIN':'HR','FREEZING_RAIN':'FZ','FREEZING_DRIZZLE':'FZ',
    'BLIZZARD':'BZ','HEAVY_SNOW':'HS','FUNNEL':'FC',
  };
  return m[t] || t.substring(0,2);
}

// ── Format timestamp ──
function fmtTs(ts) {
  if (!ts) return '—';
  return ts.replace('T', ' ').substring(0, 16) + 'Z';
}

// ── Format magnitude ──
function fmtMag(v, u) {
  if (v === null || v === undefined) return '—';
  if (u === 'mph') return v.toFixed(0) + ' mph';
  if (u === 'inches') return v.toFixed(2) + ' in';
  if (u === 'inches_per_hour') return v.toFixed(2) + ' in/hr';
  return v;
}

// ── Load events ──
async function loadEvents(offset) {
  currentOffset = offset;
  const type = document.getElementById('fType').value;
  const site = document.getElementById('fSite').value;
  const state = document.getElementById('fState').value;
  const days = document.getElementById('fDays').value;
  const startDate = document.getElementById('fStartDate').value;
  const endDate = document.getElementById('fEndDate').value;
  const minDist = document.getElementById('fMinDist').value;
  const maxDist = document.getElementById('fMaxDist').value;
  const sortBy = document.getElementById('fSortBy').value;
  const sortOrder = document.getElementById('fSortOrder').value;
  const limit = parseInt(document.getElementById('fLimit').value);
  currentLimit = limit;

  // Disable days filter if start_date is set (date range takes precedence)
  const effectiveDays = (startDate || endDate) ? 'all' : days;

  const params = new URLSearchParams({
    type, site, state, days: effectiveDays, limit, offset,
    start_date: startDate,
    end_date: endDate,
    min_dist: minDist,
    max_dist: maxDist,
    sort_by: sortBy,
    sort_order: sortOrder,
  });

  try {
    const r = await fetch(API + '/api/events?' + params);
    const d = await r.json();
    currentTotal = d.total;
    renderTable(d.events);
    renderPagination();
  } catch(e) {
    document.getElementById('eventBody').innerHTML =
      `<tr><td colspan="8" class="empty-state"><div class="icon">⚠</div>Failed to load events: ${e}</td></tr>`;
  }
}

// ── Clear all filters ──
function clearFilters() {
  document.getElementById('fType').value = 'ALL';
  document.getElementById('fSite').value = 'ALL';
  document.getElementById('fState').value = 'ALL';
  document.getElementById('fStartDate').value = '';
  document.getElementById('fEndDate').value = '';
  document.getElementById('fMinDist').value = '';
  document.getElementById('fMaxDist').value = '';
  document.getElementById('fSortBy').value = 'timestamp';
  document.getElementById('fSortOrder').value = 'desc';
  document.getElementById('fDays').value = '30';
  document.getElementById('fLimit').value = '100';
  loadEvents(0);
}

// ── Render table ──
function renderTable(events) {
  const tbody = document.getElementById('eventBody');
  if (!events || events.length === 0) {
    tbody.innerHTML = `<tr><td colspan="8" class="empty-state"><div class="icon">◇</div>No events match the current filters.</td></tr>`;
    return;
  }

  tbody.innerHTML = events.map(ev => {
    const cls = typeClass(ev.event_type);
    const lbl = typeLabel(ev.event_type);
    return `<tr>
      <td>${fmtTs(ev.timestamp)}</td>
      <td><span class="type-pill ${cls}">${lbl}</span>&nbsp;<span style="color:var(--text-dim);font-size:0.85em">${ev.event_type || ev.raw_type || ''}</span></td>
      <td class="mag">${fmtMag(ev.magnitude, ev.magnitude_unit)}</td>
      <td>${ev.latitude?.toFixed(3)}, ${ev.longitude?.toFixed(3)} <span class="dist">(${ev.distance_mi} mi)</span></td>
      <td><span class="site-id">${ev.site_id}</span> <span class="state-badge">${ev.state || ''}</span></td>
      <td class="source-${ev.source}">${ev.source}</td>
      <td style="color:var(--text-dim);font-size:0.88em">${fmtTs(ev.metar_timestamp)}</td>
      <td class="remarks" title="${ev.remarks || ''}">${ev.remarks || '—'}</td>
    </tr>`;
  }).join('');
}

// ── Render pagination ──
function renderPagination() {
  const div = document.getElementById('pagination');
  const total = currentTotal;
  const limit = currentLimit;
  const curPage = Math.floor(currentOffset / limit) + 1;
  const totalPages = Math.ceil(total / limit);
  const start = currentOffset + 1;
  const end = Math.min(currentOffset + limit, total);

  let html = '';
  html += `<button onclick="loadEvents(0)" ${currentOffset === 0 ? 'disabled' : ''}>«</button>`;
  html += `<button onclick="loadEvents(${currentOffset - limit})" ${currentOffset === 0 ? 'disabled' : ''}>‹</button>`;
  html += `<span class="page-info">${total === 0 ? 'No events' : `${start}–${end} of ${total.toLocaleString()}`} &nbsp;|&nbsp; Page ${curPage}/${totalPages || 1}</span>`;
  html += `<button onclick="loadEvents(${currentOffset + limit})" ${currentOffset + limit >= total ? 'disabled' : ''}>›</button>`;
  html += `<button onclick="loadEvents(${(totalPages - 1) * limit})" ${currentOffset + limit >= total ? 'disabled' : ''}>»</button>`;
  div.innerHTML = html;
}

// ── Filter change handlers ──
document.getElementById('fType').addEventListener('change', () => loadEvents(0));
document.getElementById('fSite').addEventListener('change', () => loadEvents(0));
document.getElementById('fState').addEventListener('change', () => loadEvents(0));
document.getElementById('fDays').addEventListener('change', () => loadEvents(0));
document.getElementById('fSortBy').addEventListener('change', () => loadEvents(0));
document.getElementById('fSortOrder').addEventListener('change', () => loadEvents(0));
document.getElementById('fLimit').addEventListener('change', () => loadEvents(0));

// ── Init ──
loadTelemetry();
loadSites();
loadEvents(0);

// Refresh telemetry every 60s
setInterval(loadTelemetry, 60000);
</script>
</body>
</html>
'''

@app.route("/events")
def events_page():
    return render_template_string(EVENTS_HTML)

@app.route("/api/wiki/index")
def api_wiki_index():
    """Serve the wiki index page as JSON."""
    index_path = os.path.join(WIKI_DIR, "index.md")
    if not os.path.exists(index_path):
        return jsonify({"error": "index.md not found"}), 404
    with open(index_path, "r", encoding="utf-8") as f:
        return jsonify({"content": f.read()})

@app.route("/api/wiki/page/<path:page>")
def api_wiki_page(page):
    """Serve a single wiki page by relative path (e.g., 'concepts/havoc-downward-forcing')."""
    # Only allow .md files under WIKI_DIR
    if ".." in page or page.startswith("/"):
        return jsonify({"error": "Invalid path"}), 400
    if not page.endswith(".md"):
        page += ".md"
    filepath = os.path.join(WIKI_DIR, page)
    # Prevent path traversal
    filepath = os.path.realpath(filepath)
    if not filepath.startswith(os.path.realpath(WIKI_DIR)):
        return jsonify({"error": "Invalid path"}), 400
    if not os.path.exists(filepath):
        return jsonify({"error": f"Page not found: {page}"}), 404
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()
    # List all .md files for sidebar navigation
    all_pages = []
    for root, dirs, files in os.walk(WIKI_DIR):
        for fname in files:
            if fname.endswith(".md") and fname != "log.md":
                fpath = os.path.join(root, fname)
                relpath = os.path.relpath(fpath, WIKI_DIR)
                all_pages.append(relpath)
    all_pages.sort()
    return jsonify({"content": content, "path": page, "all_pages": all_pages})


# ── WX Events API ─────────────────────────────────────────────────────────────
def _zulu_now():
    return datetime.utcnow().strftime("%Y%m%d %H%MZ")


def _wx_conn():
    """Open a connection to the WX events database."""
    import sqlite3
    conn = sqlite3.connect(str(WX_DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


@app.route("/api/telemetry")
def api_telemetry():
    """Summary stats for the dashboard header."""
    conn = _wx_conn()
    try:
        cur = conn.execute

        # Total events
        total = conn.execute("SELECT COUNT(*) as n FROM events").fetchone()["n"]

        # Counts by type (last 30 days)
        thirty_days_ago = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        type_counts = conn.execute("""
            SELECT event_type, COUNT(*) as cnt
            FROM events
            WHERE timestamp >= ?
            GROUP BY event_type
            ORDER BY cnt DESC
        """, (thirty_days_ago,)).fetchall()

        # Recent events (last 7 days)
        seven_days_ago = (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        recent = conn.execute("""
            SELECT COUNT(*) as n FROM events WHERE timestamp >= ?
        """, (seven_days_ago,)).fetchone()["n"]

        # Sites with events
        sites_with_events = conn.execute("""
            SELECT COUNT(DISTINCT site_id) as n FROM events
        """).fetchone()["n"]

        # Last event timestamp
        last_event_row = conn.execute(
            "SELECT timestamp FROM events ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        last_event = last_event_row["timestamp"] if last_event_row else None

        # Ingestion status
        last_ingest = conn.execute(
            "SELECT source_file, ingested_at FROM ingestion_log ORDER BY ingested_at DESC LIMIT 1"
        ).fetchone()

        return jsonify({
            "timestamp": _zulu_now(),
            "total_events": total,
            "recent_events_7d": recent,
            "recent_events_30d": sum(r["cnt"] for r in type_counts),
            "type_counts": [dict(r) for r in type_counts],
            "sites_with_events": sites_with_events,
            "last_event": last_event,
            "last_ingest_file": last_ingest["source_file"] if last_ingest else None,
            "last_ingest_at": last_ingest["ingested_at"] if last_ingest else None,
        })
    finally:
        conn.close()


@app.route("/api/events")
def api_events():
    """Filterable, paginated, sortable event list."""
    conn = _wx_conn()
    try:
        # ── Filters ──────────────────────────────────────────────────────────────
        event_type = request.args.get("type")
        site_id = request.args.get("site")
        state = request.args.get("state")
        days_arg = request.args.get("days", "30")
        if days_arg == "all":
            days = None  # No date cutoff
        else:
            days = int(days_arg)
        limit = int(request.args.get("limit", 200))
        offset = int(request.args.get("offset", 0))

        # Date range (overrides days if provided)
        start_date = request.args.get("start_date")  # YYYY-MM-DD
        end_date = request.args.get("end_date")      # YYYY-MM-DD

        # Distance range (miles)
        min_distance = request.args.get("min_dist")
        max_distance = request.args.get("max_dist")

        # Sort
        sort_by = request.args.get("sort_by", "timestamp")  # timestamp | distance | magnitude | type
        sort_order = request.args.get("sort_order", "desc") # asc | desc

        # Build WHERE clause
        where = []
        params = []

        # Date filtering
        if start_date:
            where.append("e.timestamp >= ?")
            params.append(start_date + "T00:00:00+00:00")
        elif days is not None:
            cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
            where.append("e.timestamp >= ?")
            params.append(cutoff)
        # If no start_date and days is None, no cutoff applied

        if end_date:
            where.append("e.timestamp <= ?")
            params.append(end_date + "T23:59:59+00:00")

        # Event type
        if event_type and event_type != "ALL":
            where.append("e.event_type = ?")
            params.append(event_type)

        # Site filter
        if site_id and site_id != "ALL":
            where.append("e.site_id = ?")
            params.append(site_id)

        # State filter
        if state and state != "ALL":
            where.append("sl.state = ?")
            params.append(state)

        # Distance range
        if min_distance and min_distance != "":
            where.append("e.distance_mi >= ?")
            params.append(float(min_distance))
        if max_distance and max_distance != "":
            where.append("e.distance_mi <= ?")
            params.append(float(max_distance))

        where_clause = " AND ".join(where)

        # ── Sort clause ────────────────────────────────────────────────────────
        sort_col_map = {
            "timestamp": "e.timestamp",
            "distance": "e.distance_mi",
            "magnitude": "e.magnitude",
            "type": "e.event_type",
        }
        sort_col = sort_col_map.get(sort_by, "e.timestamp")
        sort_dir = "DESC" if sort_order == "desc" else "ASC"
        if sort_by == "magnitude":
            sort_dir = "DESC" if sort_order == "desc" else "ASC"
            # Handle NULL magnitudes - put them last
            order_clause = f"e.magnitude IS NOT NULL, e.magnitude {sort_dir}"
        else:
            order_clause = f"{sort_col} {sort_dir}"

        # ── Count ───────────────────────────────────────────────────────────────
        total = conn.execute(f"""
            SELECT COUNT(*) as n FROM events e
            JOIN support_locations sl ON e.site_id = sl.site_id
            WHERE {where_clause}
        """, params).fetchone()["n"]

        # ── Query ──────────────────────────────────────────────────────────────
        rows = conn.execute(f"""
            SELECT
                e.id, e.timestamp, e.event_type, e.raw_type,
                e.magnitude, e.magnitude_unit,
                e.latitude, e.longitude,
                e.distance_mi, e.source,
                e.site_id, sl.name as site_name, sl.state,
                e.metar_timestamp, e.remarks
            FROM events e
            JOIN support_locations sl ON e.site_id = sl.site_id
            WHERE {where_clause}
            ORDER BY {order_clause}
            LIMIT ? OFFSET ?
        """, params + [limit, offset]).fetchall()

        events = [dict(r) for r in rows]

        return jsonify({
            "events": events,
            "total": total,
            "limit": limit,
            "offset": offset,
            "filters": {
                "type": event_type or "ALL",
                "site": site_id or "ALL",
                "state": state or "ALL",
                "days": days,
                "start_date": start_date or "",
                "end_date": end_date or "",
                "min_dist": min_distance or "",
                "max_dist": max_distance or "",
                "sort_by": sort_by,
                "sort_order": sort_order,
            }
        })
    finally:
        conn.close()


@app.route("/api/sites")
def api_sites():
    """All support locations with optional event counts."""
    conn = _wx_conn()
    try:
        rows = conn.execute("""
            SELECT
                sl.site_id, sl.name, sl.state, sl.zone,
                sl.latitude, sl.longitude,
                COUNT(e.id) as event_count
            FROM support_locations sl
            LEFT JOIN events e ON sl.site_id = e.site_id
            GROUP BY sl.site_id
            ORDER BY event_count DESC, sl.state, sl.name
        """).fetchall()

        return jsonify([dict(r) for r in rows])
    finally:
        conn.close()


@app.route("/api/summary")
def api_summary():
    """Monthly event breakdown by type for charts."""
    conn = _wx_conn()
    try:
        rows = conn.execute("""
            SELECT
                strftime('%Y-%m', timestamp) as month,
                event_type,
                COUNT(*) as cnt
            FROM events
            GROUP BY month, event_type
            ORDER BY month DESC, cnt DESC
        """).fetchall()

        return jsonify([dict(r) for r in rows])
    finally:
        conn.close()


# ── Threats Template ──────────────────────────────────────────────────────────

THREATS_HTML = r'''
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>NAM Threats Grid</title>
<style>
  :root {
    --bg-deep: #0a0e17;
    --bg-panel: #111827;
    --bg-card: #1a2332;
    --border: #2a3a4e;
    --text: #e2e8f0;
    --text-dim: #64748b;
    --accent: #38bdf8;
    --accent2: #818cf8;
    --green: #34d399;
    --red: #f87171;
    --amber: #fbbf24;
    --orange: #fb923c;
    --orange-dark: #c2410c;
    --orange-light: #fed7aa;
    --magenta: #e879f9;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: 'JetBrains Mono', 'Fira Code', 'Cascadia Code', monospace;
    background: var(--bg-deep); color: var(--text); min-height: 100vh;
  }
  .hud { max-width: 100%; margin: 0 auto; padding: 16px; }

  .topbar {
    display: flex; align-items: center; justify-content: space-between;
    padding: 12px 20px; margin-bottom: 16px;
    background: var(--bg-panel); border: 1px solid var(--border); border-radius: 8px;
  }
  .topbar-left { display: flex; align-items: center; gap: 12px; }
  .topbar-title { font-size: 1.1em; font-weight: 700; letter-spacing: 2px; color: var(--accent); }
  .topbar-sub { font-size: 0.75em; color: var(--text-dim); letter-spacing: 1px; }
  .topbar-right { display: flex; align-items: center; gap: 16px; font-size: 0.8em; color: var(--text-dim); }
  .pulse { width: 8px; height: 8px; border-radius: 50%; display: inline-block; margin-right: 6px; }
  .pulse-ok { background: var(--green); box-shadow: 0 0 6px var(--green); }

  /* ── Ops Status Bar ────────────────── */
  .opsbar {
    display: flex; align-items: stretch; gap: 0;
    margin-bottom: 12px; border-radius: 8px; overflow: hidden;
    border: 1px solid var(--border); background: var(--bg-panel);
    font-size: 0.72em; letter-spacing: 0.5px;
  }
  .opsbar-cell {
    flex: 1; padding: 8px 14px; border-right: 1px solid var(--border);
    display: flex; flex-direction: column; gap: 2px; min-width: 0;
  }
  .opsbar-cell:last-child { border-right: none; }
  .opsbar-label {
    font-size: 0.75em; color: var(--text-dim); text-transform: uppercase;
    letter-spacing: 1.5px; font-weight: 600; white-space: nowrap;
  }
  .opsbar-value {
    font-size: 1.05em; color: var(--text); font-weight: 400;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .opsbar-value .pill { padding: 1px 7px; border-radius: 8px; font-size: 0.85em; font-weight: 600; }
  .opsbar-value .pill-ok { background: rgba(52,211,153,0.15); color: var(--green); }
  .opsbar-value .pill-warn { background: rgba(251,191,36,0.15); color: var(--amber); }
  .opsbar-value .pill-err { background: rgba(248,113,113,0.15); color: var(--red); }
  .opsbar-value .pill-info { background: rgba(56,189,248,0.15); color: var(--accent); }
  .run-btn {
    display: inline-block; margin-left: 6px; padding: 1px 8px; font-size: 0.82em;
    background: rgba(248,113,113,0.18); color: var(--red); border: 1px solid var(--red);
    border-radius: 3px; cursor: pointer; letter-spacing: 0.5px; transition: background 0.2s;
  }
  .run-btn:hover { background: rgba(248,113,113,0.35); }
  .run-btn.running { background: rgba(251,191,36,0.18); color: var(--amber); border-color: var(--amber);
    animation: pulse-amber 1.2s infinite; }
  @keyframes pulse-amber { 0%,100% { opacity:1; } 50% { opacity:0.5; } }

  .nav-links a {
    color: var(--text-dim); text-decoration: none; font-size: 0.75em;
    letter-spacing: 1px; padding: 4px 10px; border: 1px solid var(--border);
    border-radius: 4px; margin-left: 8px; transition: all 0.2s;
  }
  .nav-links a:hover { color: var(--accent); border-color: var(--accent); }

  .legend {
    display: flex; gap: 16px; align-items: center; flex-wrap: wrap;
    padding: 10px 16px; margin-bottom: 12px;
    background: var(--bg-panel); border: 1px solid var(--border); border-radius: 8px;
    font-size: 0.7em; color: var(--text-dim);
  }
  .legend span { display: flex; align-items: center; gap: 6px; }
  .legend .glyph {
    width: 22px; height: 22px; border-radius: 4px; display: inline-flex;
    align-items: center; justify-content: center; font-size: 0.8em; font-weight: 700;
  }
  .legend .sep { color: var(--border); margin: 0 4px; }

  /* ── Zone sections ── */
  .zone-section { margin-bottom: 20px; }
  .zone-header {
    display: flex; align-items: center; gap: 10px;
    padding: 8px 16px; margin-bottom: 8px;
    background: var(--bg-panel); border: 1px solid var(--border); border-radius: 6px;
    font-size: 0.85em; font-weight: 700; letter-spacing: 2px; color: var(--accent2);
  }
  .zone-count { font-size: 0.7em; color: var(--text-dim); font-weight: 400; margin-left: auto; }

  /* ── Threats Grid ── */
  .threats-grid-wrap { overflow-x: auto; }
  .threats-grid {
    display: grid; gap: 1px;
    font-size: 0.68em; line-height: 1;
  }
  .threats-grid .col-label {
    padding: 6px 4px; background: var(--bg-panel); color: var(--text-dim);
    font-weight: 600; letter-spacing: 1px; text-transform: uppercase;
    text-align: center; position: sticky; top: 0; z-index: 2;
    border-bottom: 1px solid var(--border);
  }
  .threats-grid .row-label {
    padding: 6px 8px; background: var(--bg-deep); color: var(--text);
    font-weight: 600; letter-spacing: 1px; white-space: nowrap;
    border-right: 1px solid var(--border); display: flex; align-items: center;
    position: relative;
  }
  .threats-grid .row-label .stn-name {
    display: none; /* hidden, shown via tooltip */
  }
  .threats-grid .row-label .stn-tip {
    display: none; position: absolute; z-index: 100;
    top: 110%; left: 0; background: var(--bg-panel); border: 1px solid var(--border);
    border-radius: 6px; padding: 6px 10px; font-size: 0.85em; font-weight: 400;
    white-space: nowrap; pointer-events: none; color: var(--text-dim);
    box-shadow: 0 4px 20px rgba(0,0,0,0.6);
  }
  .threats-grid .row-label:hover .stn-tip { display: block; }

  /* ── Cell (glyph icon) ── */
  .cell {
    padding: 2px; background: var(--bg-deep);
    display: flex; align-items: center; justify-content: center;
    position: relative; min-height: 28px;
  }
  .glyph-icon {
    width: 22px; height: 22px; border-radius: 4px;
    display: inline-flex; align-items: center; justify-content: center;
    font-weight: 700; font-size: 0.75em; cursor: default;
    transition: transform 0.15s, box-shadow 0.15s;
  }
  .glyph-icon:hover {
    transform: scale(1.3); z-index: 10;
    box-shadow: 0 0 12px rgba(0,0,0,0.5);
  }

  /* Threshold colors */
  .nc-warn { background: var(--amber); color: #78350f; }
  .nc-svr  { background: var(--orange-dark); color: #fff; }

  /* Subdued icon: inversion blocks momentum transfer */
  .nc-warn.inv-block  { background: rgba(251,191,36,0.25); color: rgba(120,53,15,0.6); }
  .nc-svr.inv-block   { background: rgba(194,65,12,0.25);  color: rgba(255,255,255,0.45); }

  /* ── Tooltip ── */
  .cell .tip {
    display: none; position: absolute; z-index: 100;
    top: 110%; left: 50%; transform: translateX(-50%);
    background: var(--bg-panel); border: 1px solid var(--border); border-radius: 6px;
    padding: 10px 14px; font-size: 0.75em; line-height: 1.7;
    white-space: nowrap; pointer-events: none;
    box-shadow: 0 4px 20px rgba(0,0,0,0.6);
  }
  .cell .tip::after {
    content: ''; position: absolute; bottom: 100%; left: 50%;
    transform: translateX(-50%); border: 6px solid transparent;
    border-bottom-color: var(--border);
  }
  .cell .tip.flip {
    top: auto; bottom: 110%;
  }
  .cell .tip.flip::after {
    bottom: auto; top: 100%;
    border-bottom-color: transparent;
    border-top-color: var(--border);
  }
  .cell:hover .tip { display: block; }
  .tip .tip-title { font-weight: 700; color: var(--accent); letter-spacing: 1px; margin-bottom: 4px; }
  .tip .tip-layer { color: var(--text-dim); display: flex; gap: 8px; align-items: baseline; }
  .tip .tip-layer:hover { color: var(--text); }
  .tip .tip-alt { min-width: 60px; }
  .tip .tip-layer .val { color: var(--text); font-weight: 600; }
  .tip .tip-layer .dir { color: var(--text-dim); }
  .tip .tip-layer .lapse { font-size: 0.9em; margin-left: auto; }

  .loading {
    text-align: center; padding: 80px 20px;
    color: var(--text-dim); font-size: 0.85em; letter-spacing: 1px;
  }
  .error-box {
    padding: 20px; margin: 20px 0;
    background: var(--bg-panel); border: 1px solid var(--red); border-radius: 8px;
    color: var(--red); font-size: 0.8em;
  }
</style>
</head>
<body>
<div class="hud">
  <div class="topbar">
    <div class="topbar-left">
      <span class="pulse pulse-ok" id="statusDot"></span>
      <span class="topbar-title">THREATS</span>
      <span class="topbar-sub" id="cycleLabel">&mdash;</span>
    </div>
    <div class="topbar-right">
      <div class="nav-links">
        <a href="/">OPS</a>
        <a href="/skewt">SKEWT</a>
        <a href="/wiki">WIKI</a>
      </div>
      <span id="loadTime">&mdash;</span>
    </div>
  </div>

  <!-- Ops Status Bar -->
  <div class="opsbar" id="opsbar-thr">
    <div class="opsbar-cell">
      <div class="opsbar-label">☤ NAM Download</div>
      <div class="opsbar-value" id="ops-nam">&mdash;</div>
    </div>
    <div class="opsbar-cell">
      <div class="opsbar-label">⚗ Threats Active</div>
      <div class="opsbar-value" id="ops-threats">&mdash;</div>
    </div>
    <div class="opsbar-cell">
      <div class="opsbar-label">▶ Scraper</div>
      <div class="opsbar-value" id="ops-scraper">&mdash;</div>
    </div>
    <div class="opsbar-cell">
      <div class="opsbar-label">⚙ Pipeline</div>
      <div class="opsbar-value" id="ops-pipeline">&mdash;</div>
    </div>
    <div class="opsbar-cell">
      <div class="opsbar-label">⚡ Flask</div>
      <div class="opsbar-value" id="ops-flask">&mdash;</div>
    </div>
  </div>

  <div class="legend">
    <span>
      <span class="glyph nc-warn">&#x27A4;</span>
      NC Wind 35‑49kt
    </span>
    <span>
      <span class="glyph nc-svr">&#x27A4;</span>
      NC Wind &ge;50kt
    </span>
    <span class="sep">|</span>
    <span>BTW = Best Transfer Winds</span>
    <span class="sep">|</span>
    <span>DF% = Downward Forcing (subsidence mixing depth)</span>
    <span class="sep">|</span>
    <span>Layers: 1k–9k ft AGL</span>
  </div>

  <div id="content"><div class="loading">Loading threat data...</div></div>
</div>

<script>
// BTW layers: shorthand → display label (feet AGL)
const BTW_LAYERS = [
  {key:'090', label:'9,000 ft'},
  {key:'070', label:'7,000 ft'},
  {key:'050', label:'5,000 ft'},
  {key:'040', label:'4,000 ft'},
  {key:'030', label:'3,000 ft'},
  {key:'020', label:'2,000 ft'},
  {key:'010', label:'1,000 ft'},
];

function fmtDir(d) {
  if (d == null) return '\u2014';
  const dirs = ['N','NNE','NE','ENE','E','ESE','SE','SSE','S','SSW','SW','WSW','W','WNW','NW','NNW'];
  return dirs[Math.round(((d % 360) + 360) % 360 / 22.5) % 16];
}

function lapseColor(lapse) {
  // Cyan for negative (inversions), grey near 0, red approaching 9.8
  if (lapse == null) return 'var(--text-dim)';
  if (lapse < 0) {
    // Cyan: deeper inversion = more saturated cyan
    const t = Math.min(Math.abs(lapse) / 10, 1); // -10+ maps to full cyan
    const r = Math.round(0 + (0 - 0) * t);
    const g = Math.round(180 + (255 - 180) * t);
    const b = Math.round(180 + (255 - 180) * t);
    return `rgb(${r},${g},${b})`;
  }
  // Positive lapse rate: grey at 0 → red at 9.8
  const t = Math.min(lapse / 9.8, 1);
  // Grey at t=0: rgb(148,163,184), Red at t=1: rgb(248,113,113)
  const r = Math.round(148 + (248 - 148) * t);
  const g = Math.round(163 + (113 - 163) * t);
  const b = Math.round(184 + (113 - 184) * t);
  return `rgb(${r},${g},${b})`;
}

let rawData = null;

async function loadThreats() {
  const t0 = performance.now();
  try {
    const resp = await fetch('/api/nam/threats');
    if (!resp.ok) throw new Error('API returned ' + resp.status);
    rawData = await resp.json();
    const elapsed = ((performance.now() - t0) / 1000).toFixed(2);
    document.getElementById('loadTime').textContent = elapsed + 's';
    render();
  } catch (e) {
    document.getElementById('content').innerHTML =
      '<div class="error-box">Failed to load threat data: ' + e.message + '</div>';
    document.getElementById('statusDot').className = 'pulse';
    document.getElementById('statusDot').style.background = 'var(--red)';
    document.getElementById('statusDot').style.boxShadow = '0 0 6px var(--red)';
  }
}

function esc(s) {
  if (s == null) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function render() {
  if (!rawData) return;

  const cycleLabel = rawData.info?.cycle || 'unknown';
  document.getElementById('cycleLabel').textContent = cycleLabel + 'z';

  // Collect all forecast hours across all stations (3-hourly)
  const fhSet = new Set();
  for (const stations of Object.values(rawData.zones)) {
    for (const s of stations) {
      for (const fh of Object.keys(s.forecast_hours || {})) {
        fhSet.add(fh);
      }
    }
  }
  const fhours = [...fhSet].sort();
  const fhours3h = fhours.filter(fh => {
    const n = parseInt(fh.replace('f',''), 10);
    return n % 3 === 0;
  });

  // Build per-zone grids (explicit order)
  let html = '';
  const zoneOrder = ['North', 'Central', 'South', 'SOUTHCOM'];
  const orderedZones = zoneOrder.filter(z => rawData.zones[z]);
  // Also include any zones not in the predefined order (e.g. Unknown)
  for (const z of Object.keys(rawData.zones)) {
    if (!zoneOrder.includes(z)) orderedZones.push(z);
  }
  for (const zone of orderedZones) {
    const stations = rawData.zones[zone];
    const sorted = [...stations].sort((a,b) => (a.station||'').localeCompare(b.station||''));
    html += '<div class="zone-section">';
    html += '<div class="zone-header">' + esc(zone) +
            '<span class="zone-count">' + sorted.length + ' stations</span></div>';
    html += '<div class="threats-grid-wrap">';
    html += buildGrid(sorted, fhours3h);
    html += '</div></div>';
  }

  document.getElementById('content').innerHTML = html || '<div class="error-box">No threat data available</div>';

  // Tooltip flip: if opening below would clip, flip above
  document.querySelectorAll('.cell').forEach(cell => {
    cell.addEventListener('mouseenter', () => {
      const tip = cell.querySelector('.tip');
      if (!tip) return;
      const rect = cell.getBoundingClientRect();
      const tipH = tip.offsetHeight || 200;
      tip.classList.toggle('flip', rect.bottom + tipH > window.innerHeight);
    });
    cell.addEventListener('mouseleave', () => {
      const tip = cell.querySelector('.tip');
      if (tip) tip.classList.remove('flip');
    });
  });
}

function buildGrid(stations, fhours) {
  const colTemplate = '80px repeat(' + fhours.length + ', 36px)';
  let h = '<div class="threats-grid" style="grid-template-columns:' + colTemplate + '">';

  // Header row
  h += '<div class="col-label"></div>';
  const cycleDate = rawData.info?.cycle_date;
  const cycleHour = rawData.info?.cycle_hour;
  let runDt = null;
  if (cycleDate && cycleHour != null) {
    const y = parseInt(cycleDate.substring(0,4), 10);
    const m = parseInt(cycleDate.substring(4,6), 10);
    const d = parseInt(cycleDate.substring(6,8), 10);
    runDt = new Date(Date.UTC(y, m-1, d, cycleHour, 0, 0));
  }
  for (const fh of fhours) {
    const n = parseInt(fh.replace('f',''), 10);
    let label = String(n);
    if (runDt) {
      const valid = new Date(runDt.getTime() + n * 3600000);
      const hh = String(valid.getUTCHours()).padStart(2,'0');
      const dd = valid.getUTCDate();
      label = n + '<br><span style="font-size:0.8em;color:var(--text-dim)">' + hh + 'Z</span>';
    }
    h += '<div class="col-label">' + label + '</div>';
  }

  // Station rows
  for (const s of stations) {
    h += '<div class="row-label">' + esc(s.station) +
         '<span class="stn-name">' + esc(s.name) + '</span>' +
         '<span class="stn-tip">' + esc(s.name) + '</span></div>';
    for (const fh of fhours) {
      const fhData = (s.forecast_hours || {})[fh];

      // Check ALL layers — icon if ANY hits >= 35kt
      let maxSpd = 0;
      let maxLayer = null;
      for (const lyr of BTW_LAYERS) {
        const spd = fhData ? fhData['btw_' + lyr.key + '_spd_kt'] : null;
        if (spd != null && spd > maxSpd) {
          maxSpd = spd;
          maxLayer = lyr.key;
        }
      }

      if (maxSpd >= 35) {
        const cls = maxSpd >= 50 ? 'nc-svr' : 'nc-warn';
        // Check for low-level inversion (any layer <= 5000ft with negative lapse)
        const LOW_LAYERS = ['010','020','030','040','050']; // <= 5000 ft AGL
        let hasInversion = false;
        for (const lk of LOW_LAYERS) {
          const ll = fhData ? fhData['btw_' + lk + '_lapse_C_km'] : null;
          if (ll != null && ll < 0) { hasInversion = true; break; }
        }
        const invClass = hasInversion ? ' inv-block' : '';
        h += '<div class="cell">';
        h += '<span class="glyph-icon ' + cls + invClass + '">&#x27A4;</span>';
        // Tooltip: all 7 BTW layers with lapse rates
        h += '<div class="tip">';
        h += '<div class="tip-title">' + esc(s.station) + ' ' + esc(s.name) + ' f' + fh.replace('f','') + '</div>';
        for (const lyr of BTW_LAYERS) {
          const ls = fhData ? fhData['btw_' + lyr.key + '_spd_kt'] : null;
          const ld = fhData ? fhData['btw_' + lyr.key + '_dir'] : null;
          const ll = fhData ? fhData['btw_' + lyr.key + '_lapse_C_km'] : null;
          const active = ls != null && ls >= 35;
          h += '<div class="tip-layer">';
          h += '<span class="tip-alt">' + esc(lyr.label) + '</span> ';
          if (ls != null) {
            h += '<span class="val' + (active ? ' style="color:var(--orange)"' : '') + '">' +
                 Math.round(ls) + 'kt</span>';
            if (ld != null) h += ' <span class="dir">' + fmtDir(ld) + '</span>';
          } else {
            h += '<span class="val">\u2014</span>';
          }
          // Lapse rate with continuous color gradient
          if (ll != null) {
            h += ' <span class="lapse" style="color:' + lapseColor(ll) + '">' +
                 ll.toFixed(1) + '&deg;/km</span>';
          }
          h += '</div>';
        }
        // ── HAVOC summary: DF%, surface wind, depth type ──
        const dfPct = fhData ? fhData.df_pct : null;
        const sfcSpd = fhData ? fhData.btw_surface_kt : null;
        const sfcDir = fhData ? fhData.btw_surface_dir : null;
        const meanSpd = fhData ? fhData.btw_mean_kt : null;
        const peakSpd = fhData ? fhData.btw_peak_kt : null;
        const depthType = fhData ? fhData.btw_depth_type : null;
        h += '<div style="margin-top:6px;padding-top:4px;border-top:1px solid var(--border)">';
        // DF% bar
        if (dfPct != null) {
          const dfColor = dfPct >= 70 ? 'var(--red)' : dfPct >= 30 ? 'var(--amber)' : 'var(--green)';
          h += '<div class="tip-layer"><span class="tip-alt">DF%</span> <span class="val" style="color:' + dfColor + '">' + dfPct.toFixed(0) + '%</span>';
          // mini bar
          const barW = Math.round(dfPct);
          h += ' <span style="display:inline-block;width:50px;height:6px;background:var(--bg-deep);border-radius:3px;vertical-align:middle">';
          h += '<span style="display:inline-block;width:' + barW + '%;height:100%;background:' + dfColor + ';border-radius:3px"></span></span>';
          h += '</div>';
        }
        // Surface wind estimate
        if (sfcSpd != null) {
          h += '<div class="tip-layer"><span class="tip-alt">SFC</span> <span class="val" style="color:var(--orange)">' + Math.round(sfcSpd) + 'kt</span>';
          if (sfcDir != null) h += ' <span class="dir">' + fmtDir(sfcDir) + '</span>';
          h += ' <span class="lapse" style="color:var(--text-dim)">(mean ' + Math.round(meanSpd) + ' / peak ' + Math.round(peakSpd) + ')</span>';
          h += '</div>';
        }
        // Depth type label
        if (depthType) {
          const depthLabel = depthType === 'downslope' ? '0-9kft' : depthType === 'lee_side' ? '0-7kft' : '0-5kft';
          h += '<div class="tip-layer" style="color:var(--text-dim)"><span class="tip-alt">DEPTH</span> ' + depthLabel + '</div>';
        }
        h += '</div>'; // summary block
        h += '</div>'; // tip
        h += '</div>'; // cell
      } else {
        h += '<div class="cell"></div>';
      }
    }
  }

  h += '</div>';
  return h;
}

loadThreats();

// Ops status bar (threats page)
function renderOpsStatus(data) {
  if (!data) return;
  const namEl = document.getElementById("ops-nam");
  if (namEl) {
    const n = data.nam || {};
    const cycle = n.cycle || "—";
    const date = n.date || "—";
    const comp = n.completion != null ? n.completion : 0;
    const lag = n.lag;
    const pillCls = lag ? "pill-warn" : "pill-ok";
    const dateShort = date !== "—" ? date.slice(4) : date;
    namEl.innerHTML = `<span class="pill ${pillCls}">${cycle}</span> ${dateShort} <span style="color:var(--text-dim)">${comp}%</span>`;
  }
  const thrEl = document.getElementById("ops-threats");
  if (thrEl) {
    const t = data.threats || {};
    const tCycle = t.cycle || "—";
    const tDate = t.date || "—";
    const tCurrent = t.current;
    const pillCls = tCurrent ? "pill-ok" : "pill-warn";
    const dateShort = tDate !== "—" ? tDate.slice(4) : tDate;
    const label = tCurrent ? "CURRENT" : "STALE";
    let html = `<span class="pill ${pillCls}">${tCycle}</span> ${dateShort} <span style="color:var(--text-dim)">${label}</span>`;
    if (t.stale) {
      html += ` <span class="run-btn" onclick="runPipeline(this)">RUN PIPELINE</span>`;
    }
    thrEl.innerHTML = html;
  }
  const scrEl = document.getElementById("ops-scraper");
  if (scrEl) {
    const s = data.scraper || {};
    const lr = s.last_run || "—";
    const h = s.health || "unknown";
    const pillCls = h === "ok" ? "pill-ok" : h === "stale" ? "pill-warn" : "pill-err";
    let html = `<span class="pill ${pillCls}">${h.toUpperCase()}</span> ${lr}`;
    if (s.stale) {
      html += ` <span class="run-btn" onclick="runScraper(this)">RUN SCRAPER</span>`;
    }
    if (s.age_h != null) html += ` <span style="color:var(--text-dim);font-size:0.9em">${s.age_h}h ago</span>`;
    scrEl.innerHTML = html;
  }
  const pipEl = document.getElementById("ops-pipeline");
  if (pipEl) {
    const p = data.pipeline || {};
    const lr = p.last_run || "never";
    const st = p.status || "never_run";
    const pillCls = st === "completed" ? "pill-ok" : st === "running" ? "pill-warn" : st === "up_to_date" ? "pill-info" : "pill-err";
    let html = `<span class="pill ${pillCls}">${st.replace(/_/g," ").toUpperCase()}</span> ${lr}`;
    if (p.stale) {
      html += ` <span class="run-btn" onclick="runPipeline(this)">RUN PIPELINE</span>`;
    }
    pipEl.innerHTML = html;
  }
  const flEl = document.getElementById("ops-flask");
  if (flEl) {
    const f = data.flask || {};
    const h = f.health || "unknown";
    const pillCls = h === "up" ? "pill-ok" : "pill-err";
    flEl.innerHTML = `<span class="pill ${pillCls}">${h.toUpperCase()}</span> :5000`;
  }
}
function runScraper(btn) {
  if (btn.classList.contains("running")) return;
  btn.classList.add("running"); btn.textContent = "RUNNING…";
  fetch("/api/run-scraper", {method:"POST"}).then(r => {
    if (r.status === 409) { btn.textContent = "ALREADY RUNNING"; setTimeout(() => { btn.classList.remove("running"); btn.textContent = "RUN SCRAPER"; }, 3000); return; }
    setTimeout(() => { btn.classList.remove("running"); btn.textContent = "RUN SCRAPER"; }, 120000);
  }).catch(() => { btn.classList.remove("running"); btn.textContent = "RUN SCRAPER"; });
}
function runPipeline(btn) {
  if (btn.classList.contains("running")) return;
  btn.classList.add("running"); btn.textContent = "RUNNING…";
  fetch("/api/run-pipeline", {method:"POST"}).then(r => {
    if (r.status === 409) { btn.textContent = "ALREADY RUNNING"; setTimeout(() => { btn.classList.remove("running"); btn.textContent = "RUN PIPELINE"; }, 3000); return; }
    setTimeout(() => { btn.classList.remove("running"); btn.textContent = "RUN PIPELINE"; }, 300000);
  }).catch(() => { btn.classList.remove("running"); btn.textContent = "RUN PIPELINE"; });
}
fetch("/api/ops-status").then(r => r.json()).then(renderOpsStatus).catch(() => {});

// WX Events summary in ops bar
async function loadEventSummary() {
  const el = document.getElementById("ops-events");
  if (!el) return;
  try {
    const r = await fetch("/api/events/summary");
    const d = await r.json();
    const pillCls = d.recent_30d > 0 ? "pill-warn" : "pill-ok";
    el.innerHTML = `<span class="pill ${pillCls}">${d.total}</span> total &middot; <span class="pill pill-info">${d.recent_30d}</span> 30d`;
  } catch { el.textContent = "—"; }
}
loadEventSummary();
setInterval(loadEventSummary, 120000);
setInterval(() => fetch("/api/ops-status").then(r => r.json()).then(renderOpsStatus).catch(() => {}), 15000);
</script>
</body>
</html>
'''

# ── Wiki Template ──────────────────────────────────────────────────────────────

WIKI_HTML = r'''
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>V.I.L.E. Wiki — NWS Operations</title>
<script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
<style>
  :root {
    --bg-deep: #0a0e17;
    --bg-panel: #111827;
    --bg-card: #1a2332;
    --border: #2a3a4e;
    --text: #e2e8f0;
    --text-dim: #64748b;
    --accent: #38bdf8;
    --accent2: #818cf8;
    --green: #34d399;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: 'JetBrains Mono', 'Fira Code', 'Cascadia Code', monospace;
    background: var(--bg-deep); color: var(--text); min-height: 100vh;
  }

  /* Top bar */
  .topbar {
    display: flex; align-items: center; justify-content: space-between;
    padding: 12px 20px; margin-bottom: 0;
    background: var(--bg-panel); border-bottom: 1px solid var(--border);
  }
  .topbar-title { font-size: 1.1em; font-weight: 700; letter-spacing: 2px; color: var(--accent); }
  .topbar-sub { font-size: 0.7em; color: var(--text-dim); letter-spacing: 1px; margin-top: 2px; }
  .topbar-right { display: flex; align-items: center; gap: 16px; font-size: 0.72em; }
  .topbar-right a, .nav-links a {
    color: var(--accent); text-decoration: none; font-weight: 600;
    letter-spacing: 1px; padding: 2px 8px; border: 1px solid transparent;
    border-radius: 3px; transition: border-color 0.2s;
  }
  .topbar-right a:hover, .nav-links a:hover { border-color: var(--accent); }

  /* Layout */
  .wiki-layout { display: flex; min-height: calc(100vh - 52px); }

  /* Sidebar */
  .wiki-sidebar {
    width: 240px; min-width: 200px; background: var(--bg-panel);
    border-right: 1px solid var(--border); padding: 16px 0;
    overflow-y: auto; font-size: 0.72em;
  }
  .wiki-sidebar h3 {
    color: var(--accent); font-size: 0.9em; letter-spacing: 2px;
    padding: 8px 16px 6px; border-bottom: 1px solid var(--border);
    margin-bottom: 4px;
  }
  .wiki-sidebar a {
    display: block; padding: 5px 16px 5px 20px; color: var(--text-dim);
    text-decoration: none; transition: all 0.15s; border-left: 2px solid transparent;
  }
  .wiki-sidebar a:hover { color: var(--text); background: rgba(56,189,248,0.06); }
  .wiki-sidebar a.active { color: var(--accent); border-left-color: var(--accent); background: rgba(56,189,248,0.08); }
  .wiki-sidebar .section-label {
    padding: 10px 16px 3px; color: var(--accent2); font-size: 0.85em;
    letter-spacing: 1.5px; font-weight: 700; text-transform: uppercase;
  }

  /* Content */
  .wiki-content {
    flex: 1; padding: 24px 32px; overflow-y: auto;
    max-width: 900px; line-height: 1.7;
  }
  .wiki-content h1 {
    font-size: 1.6em; color: var(--accent); letter-spacing: 2px;
    border-bottom: 1px solid var(--border); padding-bottom: 8px; margin-bottom: 16px;
  }
  .wiki-content h2 {
    font-size: 1.2em; color: var(--accent2); letter-spacing: 1.5px;
    margin-top: 28px; margin-bottom: 10px;
  }
  .wiki-content h3 {
    font-size: 1.05em; color: var(--text); margin-top: 20px; margin-bottom: 8px;
  }
  .wiki-content p { margin-bottom: 12px; font-size: 0.88em; }
  .wiki-content code {
    background: rgba(56,189,248,0.1); color: var(--accent);
    padding: 1px 5px; border-radius: 3px; font-size: 0.88em;
  }
  .wiki-content pre {
    background: var(--bg-card); border: 1px solid var(--border);
    border-radius: 6px; padding: 14px; margin: 12px 0; overflow-x: auto;
    font-size: 0.78em; line-height: 1.5;
  }
  .wiki-content pre code { background: none; padding: 0; color: var(--text); }
  .wiki-content table {
    width: 100%; border-collapse: collapse; margin: 12px 0; font-size: 0.8em;
  }
  .wiki-content th {
    background: var(--bg-card); color: var(--accent); text-align: left;
    padding: 8px 10px; border: 1px solid var(--border); letter-spacing: 1px;
  }
  .wiki-content td {
    padding: 6px 10px; border: 1px solid var(--border); color: var(--text);
  }
  .wiki-content a { color: var(--accent); text-decoration: none; }
  .wiki-content a:hover { text-decoration: underline; }
  .wiki-content ul, .wiki-content ol { margin: 8px 0 12px 20px; font-size: 0.85em; }
  .wiki-content li { margin-bottom: 4px; }
  .wiki-content blockquote {
    border-left: 3px solid var(--accent2); padding: 8px 16px;
    background: rgba(129,140,248,0.05); margin: 12px 0; font-size: 0.85em;
    color: var(--text-dim);
  }
  .wiki-content hr { border: none; border-top: 1px solid var(--border); margin: 20px 0; }

  /* Breadcrumb */
  .breadcrumb { font-size: 0.7em; color: var(--text-dim); margin-bottom: 6px; letter-spacing: 0.5px; }
  .breadcrumb a { color: var(--text-dim); }
  .breadcrumb a:hover { color: var(--accent); }

  @media (max-width: 768px) {
    .wiki-sidebar { display: none; }
    .wiki-content { padding: 16px; }
  }
</style>
</head>
<body>

<div class="topbar">
  <div>
    <div class="topbar-title">⚗ V.I.L.E. WIKI</div>
    <div class="topbar-sub">25TH OWS — FORECAST APPROACH KNOWLEDGE BASE</div>
  </div>
  <div class="topbar-right">
    <a href="/">☤ OPS</a>
    <a href="/skewt">SKEW-T</a>
    <a href="/threats">THREATS</a>
    <a href="/synoptic">SYNOPTIC</a>
  </div>
</div>

<div class="wiki-layout">
  <div class="wiki-sidebar" id="sidebar">
    <h3>⚗ PAGES</h3>
    <div id="sidebar-links"></div>
  </div>
  <div class="wiki-content" id="content">
    <p style="color:var(--text-dim)">Loading wiki...</p>
  </div>
</div>

<script>
// Configure marked
marked.setOptions({
  gfm: true,
  breaks: false,
  headerIds: true,
  mangle: false,
});

const contentEl = document.getElementById('content');
const sidebarLinks = document.getElementById('sidebar-links');

let allPages = [];
let currentPage = null;

function renderSidebar() {
  let html = '';
  let currentSection = '';
  for (const p of allPages) {
    const parts = p.split('/');
    const section = parts.length > 1 ? parts[0] : 'root';
    const name = parts[parts.length - 1].replace('.md', '');
    if (section !== currentSection) {
      currentSection = section;
      const label = section === 'root' ? '' : section.charAt(0).toUpperCase() + section.slice(1);
      if (label) html += `<div class="section-label">${label}</div>`;
    }
    const active = p === currentPage ? ' active' : '';
    const title = name.replace(/-/g, ' ');
    html += `<a href="#" class="sidebar-link${active}" data-page="${p}">${title}</a>`;
  }
  sidebarLinks.innerHTML = html;
  // Bind click events
  sidebarLinks.querySelectorAll('.sidebar-link').forEach(a => {
    a.addEventListener('click', e => {
      e.preventDefault();
      loadPage(a.dataset.page);
    });
  });
}

function renderMarkdown(md, pagePath) {
  // Strip YAML frontmatter
  let clean = md.replace(/^---[\s\S]*?---\n/, '');
  // Convert [[wikilinks]] to clickable links
  clean = clean.replace(/\[\[([^\]|]+)(?:\|([^\]]+))?\]\]/g, (match, target, label) => {
    const display = label || target;
    // Try to resolve wikilink to a known page
    const targetSlug = target.toLowerCase().replace(/\s+/g, '-');
    let resolved = allPages.find(p => p.toLowerCase().includes(targetSlug) || p.toLowerCase().endsWith(targetSlug + '.md'));
    if (resolved) {
      return `<a href="#" onclick="event.preventDefault(); loadPage('${resolved}')" style="color:var(--accent2)">${display}</a>`;
    }
    return `<span style="color:var(--accent2);border-bottom:1px dashed var(--accent2);cursor:help" title="Unresolved: ${target}">${display}</span>`;
  });
  // Render breadcrumb
  const parts = pagePath.split('/');
  let breadcrumb = '<a href="#" onclick="event.preventDefault(); loadPage(\'index.md\')">Wiki</a>';
  if (parts.length > 1) {
    breadcrumb += ` / ${parts[0]}`;
  }
  breadcrumb += ` / <span style="color:var(--text)">${parts[parts.length-1].replace('.md','')}</span>`;

  contentEl.innerHTML = `<div class="breadcrumb">${breadcrumb}</div>` + marked.parse(clean);
  currentPage = pagePath;
  renderSidebar();
  contentEl.scrollTop = 0;
}

async function loadPage(page) {
  try {
    const resp = await fetch(`/api/wiki/page/${page}`);
    if (!resp.ok) throw new Error(resp.statusText);
    const data = await resp.json();
    allPages = data.all_pages || allPages;
    renderMarkdown(data.content, data.path);
    window.history.pushState({page}, '', `/wiki?page=${encodeURIComponent(page)}`);
  } catch(e) {
    contentEl.innerHTML = `<p style="color:var(--red)">Error loading page: ${e.message}</p>`;
  }
}

async function init() {
  // Check for page in URL
  const params = new URLSearchParams(window.location.search);
  const startPage = params.get('page') || 'index.md';
  await loadPage(startPage);
}

// Handle back/forward navigation
window.addEventListener('popstate', e => {
  if (e.state && e.state.page) loadPage(e.state.page);
});

init();
</script>
</body>
</html>
'''

# ── SkewT Template ──────────────────────────────────────────────────────────────

SKEWT_HTML = r'''
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>NAM SkewT Soundings</title>
<style>
  :root {
    --bg-deep: #0a0e17;
    --bg-panel: #111827;
    --bg-card: #1a2332;
    --border: #2a3a4e;
    --text: #e2e8f0;
    --text-dim: #64748b;
    --accent: #38bdf8;
    --accent2: #818cf8;
    --green: #34d399;
    --red: #f87171;
    --amber: #fbbf24;
    --orange: #fb923c;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: 'JetBrains Mono', 'Fira Code', 'Cascadia Code', monospace;
    background: var(--bg-deep); color: var(--text); min-height: 100vh;
  }
  .hud { max-width: 1400px; margin: 0 auto; padding: 16px; }
  .topbar {
    display: flex; align-items: center; justify-content: space-between;
    padding: 12px 20px; margin-bottom: 16px;
    background: var(--bg-panel); border: 1px solid var(--border); border-radius: 8px;
  }
  .topbar-left { display: flex; align-items: center; gap: 12px; }
  .topbar-title { font-size: 1.1em; font-weight: 700; letter-spacing: 2px; color: var(--accent); }
  .topbar-sub { font-size: 0.75em; color: var(--text-dim); letter-spacing: 1px; }
  .topbar-right { display: flex; align-items: center; gap: 16px; font-size: 0.8em; color: var(--text-dim); }
  .pulse { width: 8px; height: 8px; border-radius: 50%; display: inline-block; margin-right: 6px; }
  .pulse-ok { background: var(--green); box-shadow: 0 0 6px var(--green); }

  .controls {
    display: flex; gap: 12px; align-items: center; flex-wrap: wrap;
    padding: 12px 16px; margin-bottom: 16px;
    background: var(--bg-panel); border: 1px solid var(--border); border-radius: 8px;
  }
  .controls label { font-size: 0.75em; color: var(--text-dim); letter-spacing: 1px; text-transform: uppercase; }
  .controls select, .controls input {
    background: var(--bg-card); border: 1px solid var(--border); color: var(--text);
    padding: 6px 12px; border-radius: 6px; font-family: inherit; font-size: 0.8em;
  }
  .controls select:focus, .controls input:focus { border-color: var(--accent); outline: none; }
  .btn {
    padding: 6px 16px; border-radius: 6px; font-family: inherit; font-size: 0.75em;
    font-weight: 600; letter-spacing: 1px; cursor: pointer; border: 1px solid var(--border);
    background: var(--bg-card); color: var(--text-dim); transition: all 0.2s;
  }
  .btn:hover { border-color: var(--accent); color: var(--accent); }
  .btn.active { background: rgba(56,189,248,0.12); border-color: var(--accent); color: var(--accent); }

  .skewt-container {
    display: flex; gap: 16px;
  }
  .skewt-left { flex: 1; }
  .skewt-right { width: 320px; }

  .panel {
    background: var(--bg-panel); border: 1px solid var(--border); border-radius: 8px;
    overflow: hidden;
  }
  .panel-head {
    display: flex; align-items: center; justify-content: space-between;
    padding: 10px 16px; border-bottom: 1px solid var(--border);
    background: rgba(56,189,248,0.04);
  }
  .panel-head h2 { font-size: 0.85em; font-weight: 600; letter-spacing: 2px; text-transform: uppercase; color: var(--accent); }
  .panel-pill {
    font-size: 0.7em; padding: 2px 10px; border-radius: 12px;
    font-weight: 600; letter-spacing: 1px;
  }
  .pill-info { background: rgba(56,189,248,0.15); color: var(--accent); }
  .pill-ok { background: rgba(52,211,153,0.15); color: var(--green); }
  .panel-body { padding: 16px; }

  canvas { display: block; border: 1px solid var(--border); border-radius: 6px; background: #0d1117; }

  .sounding-list { max-height: 600px; overflow-y: auto; }
  .sounding-item {
    padding: 8px 12px; border-bottom: 1px solid var(--border);
    cursor: pointer; font-size: 0.78em; transition: all 0.15s;
    display: flex; justify-content: space-between; align-items: center;
  }
  .sounding-item:hover { background: rgba(56,189,248,0.06); }
  .sounding-item.active { background: rgba(56,189,248,0.12); color: var(--accent); }
  .sounding-code { font-weight: 700; letter-spacing: 1px; }
  .sounding-name { color: var(--text-dim); font-size: 0.85em; }

  .params-grid {
    display: grid; grid-template-columns: 1fr 1fr; gap: 8px;
  }
  .param-card {
    background: var(--bg-card); border: 1px solid var(--border); border-radius: 6px;
    padding: 10px 12px;
  }
  .param-label { font-size: 0.65em; color: var(--text-dim); letter-spacing: 1px; text-transform: uppercase; }
  .param-value { font-size: 1.1em; font-weight: 700; color: var(--accent); margin-top: 2px; }
  .param-unit { font-size: 0.7em; color: var(--text-dim); font-weight: 400; }

  .fh-slider {
    width: 100%; margin-top: 8px;
  }
  .fh-slider input[type=range] {
    width: 100%; background: var(--bg-card); accent-color: var(--accent);
  }
  .fh-labels {
    display: flex; justify-content: space-between; font-size: 0.65em; color: var(--text-dim);
  }

  a { color: var(--accent); text-decoration: none; }
  a:hover { text-decoration: underline; }
  .back-link { font-size: 0.8em; }
</style>
</head>
<body>
<div class="hud">
  <div class="topbar">
    <div class="topbar-left">
      <a href="/" class="back-link">◀ DASHBOARD</a>
      <span class="topbar-title">NAM SKEW-T</span>
      <span class="topbar-sub" id="cycleLabel">loading...</span>
    </div>
    <div class="topbar-right">
      <span><span class="pulse pulse-ok"></span>LIVE</span>
      <span id="clock"></span>
    </div>
  </div>

  <div class="controls">
    <div>
      <label>STATION</label><br>
      <select id="stationSelect"><option>Loading...</option></select>
    </div>
    <div>
      <label>FORECAST HOUR</label><br>
      <select id="fhourSelect"></select>
    </div>
    <div class="fh-slider">
      <input type="range" id="fhourSlider" min="0" max="84" step="3" value="0">
      <div class="fh-labels"><span>f00</span><span>f84</span></div>
    </div>
  </div>

  <div class="skewt-container">
    <div class="skewt-left">
      <div class="panel">
        <div class="panel-head">
          <h2>SOUNDING</h2>
          <span class="panel-pill pill-info" id="soundingLabel">—</span>
        </div>
        <div class="panel-body">
          <canvas id="skewtCanvas" width="750" height="600"></canvas>
        </div>
      </div>
    </div>
    <div class="skewt-right">
      <div class="panel" style="margin-bottom:16px">
        <div class="panel-head">
          <h2>PARAMETERS</h2>
          <span class="panel-pill pill-ok" id="validTime">—</span>
        </div>
        <div class="panel-body">
          <div class="params-grid" id="paramsGrid"></div>
        </div>
      </div>
      <div class="panel">
        <div class="panel-head">
          <h2>STATIONS</h2>
          <span class="panel-pill pill-info" id="stationCount">—</span>
        </div>
        <div class="panel-body sounding-list" id="stationList"></div>
      </div>
    </div>
  </div>
</div>

<script>
// ── Global State ──────────────────────────────────────────────
let profileData = null;
let stationList = [];
let cycleInfo = {};
let currentStation = null;
let currentFhour = 'f00';

// ── API Fetch ─────────────────────────────────────────────────
async function loadProfiles() {
  const resp = await fetch('/api/nam/profiles');
  const data = await resp.json();
  cycleInfo = data.info;
  stationList = data.stations;
  document.getElementById('cycleLabel').textContent = cycleInfo.cycle + ' | ' + cycleInfo.model;
  document.getElementById('stationCount').textContent = stationList.length + ' stations';

  // Populate station dropdown
  const sel = document.getElementById('stationSelect');
  sel.innerHTML = '';
  stationList.forEach(s => {
    const opt = document.createElement('option');
    opt.value = s.code;
    opt.textContent = s.code + ' — ' + s.name;
    sel.appendChild(opt);
  });

  // Populate forecast hours
  const fhSel = document.getElementById('fhourSelect');
  fhSel.innerHTML = '';
  cycleInfo.forecast_hours.forEach(fh => {
    const key = 'f' + String(fh).padStart(2, '0');
    const opt = document.createElement('option');
    opt.value = key; opt.textContent = 'f' + fh;
    fhSel.appendChild(opt);
  });

  // Station list sidebar
  const list = document.getElementById('stationList');
  list.innerHTML = '';
  stationList.forEach(s => {
    const div = document.createElement('div');
    div.className = 'sounding-item';
    div.innerHTML = '<span class="sounding-code">' + s.code + '</span><span class="sounding-name">' + s.name + '</span>';
    div.onclick = () => { selectStation(s.code); };
    list.appendChild(div);
  });

  // Pick first station
  if (stationList.length > 0) selectStation(stationList[0].code);
}

async function selectStation(code) {
  currentStation = code;
  document.getElementById('stationSelect').value = code;

  // Highlight in sidebar
  document.querySelectorAll('.sounding-item').forEach(el => {
    el.classList.toggle('active', el.querySelector('.sounding-code').textContent === code);
  });

  const resp = await fetch('/api/nam/profiles/' + code);
  profileData = await resp.json();
  selectFhour(currentFhour);
}

function selectFhour(key) {
  currentFhour = key;
  document.getElementById('fhourSelect').value = key;
  const fhNum = parseInt(key.replace('f', ''));
  document.getElementById('fhourSlider').value = fhNum;

  if (profileData && profileData.forecast_hours[key]) {
    drawSkewT(profileData.forecast_hours[key]);
    updateParams(profileData.forecast_hours[key], key);
    document.getElementById('soundingLabel').textContent =
      profileData.code + ' ' + key.toUpperCase();
  }
}

// ── Parameter Display ─────────────────────────────────────────
function updateParams(sounding, fhKey) {
  const sfc = sounding.surface;
  const levels = sounding.levels;

  // Find significant levels
  const l850 = levels.find(l => l.hPa === 850) || {};
  const l700 = levels.find(l => l.hPa === 700) || {};
  const l500 = levels.find(l => l.hPa === 500) || {};
  const l300 = levels.find(l => l.hPa === 300) || {};

  // Find max wind
  let maxWind = 0, maxWindLevel = 0;
  levels.forEach(l => {
    const wspd = l.wind_spd_ms * 1.94384; // m/s to kt
    if (wspd > maxWind) { maxWind = wspd; maxWindLevel = l.hPa; }
  });

  // Compute LCL estimate (simple: 125m per degC dewpoint depression)
  const sfcTd = sfc.temp_C - (100 - (sfc.rh_pct || (levels.length > 0 ? levels[0].rh_pct : 50))) / 5;
  const dep = sfc.temp_C - sfcTd;
  const lclHpa = Math.round(sfc.pressure_hPa - dep * 12);

  const params = [
    {label: 'SFC TEMP', value: sfc.temp_C, unit: '°C'},
    {label: 'SFC DEWP', value: (sfc.temp_C - (100 - (sfc.rh_pct || (levels.length > 0 ? levels[0].rh_pct : 50)))/5).toFixed(1), unit: '°C'},
    {label: 'SFC PRES', value: Math.round(sfc.pressure_hPa), unit: 'hPa'},
    {label: 'SFC RH', value: (sfc.rh_pct || '—'), unit: '%'},
    {label: '850mb T', value: (l850.temp_C || '—'), unit: '°C'},
    {label: '700mb T', value: (l700.temp_C || '—'), unit: '°C'},
    {label: '500mb T', value: (l500.temp_C || '—'), unit: '°C'},
    {label: '300mb T', value: (l300.temp_C || '—'), unit: '°C'},
    {label: '850mb RH', value: (l850.rh_pct || '—'), unit: '%'},
    {label: '700mb RH', value: (l700.rh_pct || '—'), unit: '%'},
    {label: 'MAX WIND', value: maxWind.toFixed(0), unit: 'kt'},
    {label: 'MAX W LVL', value: maxWindLevel, unit: 'hPa'},
  ];

  const grid = document.getElementById('paramsGrid');
  grid.innerHTML = '';
  params.forEach(p => {
    const div = document.createElement('div');
    div.className = 'param-card';
    div.innerHTML = '<div class="param-label">' + p.label + '</div>' +
      '<div class="param-value">' + p.value + ' <span class="param-unit">' + p.unit + '</span></div>';
    grid.appendChild(div);
  });

  const fhNum = parseInt(fhKey.replace('f', ''));
  if (cycleInfo.cycle_date && cycleInfo.cycle_hour !== undefined) {
    const base = new Date(cycleInfo.cycle_date.slice(0,4) + '-' +
      cycleInfo.cycle_date.slice(4,6) + '-' + cycleInfo.cycle_date.slice(6,8) + 'T' +
      String(cycleInfo.cycle_hour).padStart(2,'0') + ':00:00Z');
    const valid = new Date(base.getTime() + fhNum * 3600000);
    document.getElementById('validTime').textContent = valid.toISOString().slice(0,16) + 'Z';
  }
}

// ── SkewT Drawing ─────────────────────────────────────────────
function drawSkewT(sounding) {
  const canvas = document.getElementById('skewtCanvas');
  const ctx = canvas.getContext('2d');
  const W = canvas.width, H = canvas.height;

  // Plot area margins
  const ml = 65, mr = 40, mt = 30, mb = 50;
  const pw = W - ml - mr, ph = H - mt - mb;

  // Pressure range (log scale)
  const P_TOP = 100, P_BOT = 1050;
  function pToY(p) {
    return mt + ph * (Math.log(P_BOT) - Math.log(p)) / (Math.log(P_BOT) - Math.log(P_TOP));
  }

  // Temperature range (skewed)
  const T_MIN = -50, T_MAX = 45;
  const SKEW = 0.4; // skew factor: degC per log-pressure unit
  function tToX(t, p) {
    const skewOffset = SKEW * (Math.log(P_BOT) - Math.log(p));
    return ml + pw * ((t + skewOffset) - T_MIN) / (T_MAX - T_MIN);
  }

  // Clear
  ctx.fillStyle = '#0d1117';
  ctx.fillRect(0, 0, W, H);

  // ── Draw isobars ──
  ctx.strokeStyle = '#1e293b';
  ctx.lineWidth = 1;
  ctx.fillStyle = '#475569';
  ctx.font = '11px monospace';
  ctx.textAlign = 'right';
  [1000,850,700,500,400,300,250,200,150,100].forEach(p => {
    if (p < P_TOP || p > P_BOT) return;
    const y = pToY(p);
    ctx.beginPath(); ctx.moveTo(ml, y); ctx.lineTo(W - mr, y); ctx.stroke();
    ctx.fillText(p, ml - 6, y + 4);
  });

  // ── Draw isotherms (skewed) ──
  ctx.strokeStyle = '#1e293b';
  ctx.lineWidth = 0.5;
  for (let t = T_MIN; t <= T_MAX; t += 10) {
    ctx.beginPath();
    ctx.moveTo(tToX(t, P_BOT), pToY(P_BOT));
    ctx.lineTo(tToX(t, P_TOP), pToY(P_TOP));
    ctx.stroke();
    // Label at bottom
    ctx.fillStyle = '#475569';
    ctx.textAlign = 'center';
    ctx.fillText(t + '°', tToX(t, P_BOT), pToY(P_BOT) + 16);
  }

  // ── Draw dry adiabats ──
  ctx.strokeStyle = '#1a3329';
  ctx.lineWidth = 0.5;
  for (let theta = -30; theta <= 60; theta += 10) {
    ctx.beginPath();
    let first = true;
    for (let p = P_BOT; p >= P_TOP; p -= 20) {
      const t = theta * (p / 1000) ** 0.286 - 273.15;
      // Convert K theta to T at pressure p
      const tC = (theta + 273.15) * (p / 1000) ** 0.286 - 273.15;
      const x = tToX(tC, p), y = pToY(p);
      if (first) { ctx.moveTo(x, y); first = false; } else ctx.lineTo(x, y);
    }
    ctx.stroke();
  }

  // ── Draw temperature profile ──
  const levels = sounding.levels;
  if (!levels || levels.length === 0) return;

  // Temperature line
  ctx.strokeStyle = '#f87171';
  ctx.lineWidth = 2.5;
  ctx.beginPath();
  let first = true;
  levels.forEach(l => {
    if (l.hPa < P_TOP || l.hPa > P_BOT) return;
    const x = tToX(l.temp_C, l.hPa), y = pToY(l.hPa);
    if (first) { ctx.moveTo(x, y); first = false; } else ctx.lineTo(x, y);
  });
  ctx.stroke();

  // Dewpoint from RH: Td ≈ T - (100-RH)/5 (approximate)
  ctx.strokeStyle = '#34d399';
  ctx.lineWidth = 2.5;
  ctx.beginPath();
  first = true;
  levels.forEach(l => {
    if (l.hPa < P_TOP || l.hPa > P_BOT) return;
    const td = l.temp_C - (100 - l.rh_pct) / 5;
    const x = tToX(td, l.hPa), y = pToY(l.hPa);
    if (first) { ctx.moveTo(x, y); first = false; } else ctx.lineTo(x, y);
  });
  ctx.stroke();

  // ── Wind barbs ──
  const barbX = W - mr - 5;
  ctx.strokeStyle = '#94a3b8';
  ctx.fillStyle = '#94a3b8';
  ctx.lineWidth = 1.2;
  levels.forEach(l => {
    if (l.hPa < P_TOP || l.hPa > P_BOT) return;
    const y = pToY(l.hPa);
    const spd = l.wind_spd_ms * 1.94384; // kts
    const dir = l.wind_dir;
    drawWindBarb(ctx, barbX, y, spd, dir);
  });

  // ── Surface point ──
  const sfc = sounding.surface;
  if (sfc) {
    const sx = tToX(sfc.temp_C, sfc.pressure_hPa);
    const sy = pToY(sfc.pressure_hPa);
    ctx.fillStyle = '#f87171';
    ctx.beginPath(); ctx.arc(sx, sy, 4, 0, Math.PI * 2); ctx.fill();
    const sdx = tToX(sfc.temp_C - (100 - (sfc.rh_pct || (levels.length > 0 ? levels[0].rh_pct : 50))) / 5, sfc.pressure_hPa);
    ctx.fillStyle = '#34d399';
    ctx.beginPath(); ctx.arc(sdx, sy, 4, 0, Math.PI * 2); ctx.fill();
  }

  // ── Legend ──
  ctx.font = '12px monospace';
  ctx.fillStyle = '#f87171'; ctx.fillText('— T', 80, H - 10);
  ctx.fillStyle = '#34d399'; ctx.fillText('— Td', 140, H - 10);
  ctx.fillStyle = '#94a3b8'; ctx.fillText('☊ Wind', 200, H - 10);

  // Axis labels
  ctx.save();
  ctx.translate(14, mt + ph / 2);
  ctx.rotate(-Math.PI / 2);
  ctx.fillStyle = '#64748b';
  ctx.font = '12px monospace';
  ctx.textAlign = 'center';
  ctx.fillText('PRESSURE (hPa)', 0, 0);
  ctx.restore();

  ctx.fillStyle = '#64748b';
  ctx.textAlign = 'center';
  ctx.fillText('TEMPERATURE (°C)', ml + pw / 2, H - 4);
}

function drawWindBarb(ctx, x, y, spd_kt, dir_deg) {
  // Simplified wind barb: draw a staff with barbs for speed
  const r = 14;
  // Convert meteorological direction to math angle (from = direction wind comes from)
  const ang = (dir_deg - 180) * Math.PI / 180;
  const ex = x - r * Math.sin(ang);
  const ey = y - r * Math.cos(ang);

  ctx.beginPath();
  ctx.moveTo(x, y);
  ctx.lineTo(ex, ey);
  ctx.stroke();

  // Barbs
  const barbLen = 7;
  const halfKnots = Math.round(spd_kt / 5);
  let bx = ex, by = ey;

  // Draw 50kt pennants, 10kt long barbs, 5kt short barbs
  for (let i = 0; i < Math.floor(spd_kt / 50); i++) {
    ctx.beginPath(); ctx.moveTo(bx, by);
    ctx.lineTo(bx + barbLen * Math.cos(ang - 0.4), by + barbLen * Math.sin(ang - 0.4));
    ctx.lineTo(bx + barbLen * 0.5 * Math.cos(ang), by + barbLen * 0.5 * Math.sin(ang));
    ctx.fillStyle = '#94a3b8'; ctx.fill(); ctx.stroke();
  }
  // Simplified: just draw ticks for speed
  const ticks = Math.floor(spd_kt / 10);
  for (let i = 0; i < Math.min(ticks, 4); i++) {
    const frac = 0.3 + i * 0.2;
    const tx = ex + (x - ex) * frac;
    const ty = ey + (y - ey) * frac;
    const side = (i % 2 === 0) ? 1 : -1;
    ctx.beginPath(); ctx.moveTo(tx, ty);
    ctx.lineTo(tx + side * barbLen * Math.cos(ang - Math.PI/4), ty + side * barbLen * Math.sin(ang - Math.PI/4));
    ctx.stroke();
  }
}

// ── Event Handlers ─────────────────────────────────────────────
document.getElementById('stationSelect').addEventListener('change', e => {
  selectStation(e.target.value);
});
document.getElementById('fhourSelect').addEventListener('change', e => {
  selectFhour(e.target.value);
});
document.getElementById('fhourSlider').addEventListener('input', e => {
  const fh = parseInt(e.target.value);
  const key = 'f' + String(fh).padStart(2, '0');
  // Check if this forecast hour exists
  if (cycleInfo.forecast_hours.includes(fh)) {
    selectFhour(key);
  }
});

// Clock
function updateClock() {
  document.getElementById('clock').textContent = new Date().toISOString().slice(0,19) + 'Z';
}
setInterval(updateClock, 1000);
updateClock();

// Init
loadProfiles();
</script>
</body>
</html>
'''

DASHBOARD_HTML = r'''
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>NWS Operations Dashboard</title>
<style>
  :root {
    --bg-deep: #0a0e17;
    --bg-panel: #111827;
    --bg-card: #1a2332;
    --border: #2a3a4e;
    --text: #e2e8f0;
    --text-dim: #64748b;
    --accent: #38bdf8;
    --accent2: #818cf8;
    --green: #34d399;
    --red: #f87171;
    --amber: #fbbf24;
    --orange: #fb923c;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: 'JetBrains Mono', 'Fira Code', 'Cascadia Code', monospace;
    background: var(--bg-deep);
    color: var(--text);
    min-height: 100vh;
  }
  .hud { max-width: 1400px; margin: 0 auto; padding: 16px; }

  /* ── Top Bar ──────────────────────── */
  .topbar {
    display: flex; align-items: center; justify-content: space-between;
    padding: 12px 20px; margin-bottom: 16px;
    background: var(--bg-panel); border: 1px solid var(--border); border-radius: 8px;
  }
  .topbar-left { display: flex; align-items: center; gap: 12px; }
  .topbar-title { font-size: 1.1em; font-weight: 700; letter-spacing: 2px; color: var(--accent); }
  .topbar-sub { font-size: 0.75em; color: var(--text-dim); letter-spacing: 1px; }
  .topbar-right { display: flex; align-items: center; gap: 16px; font-size: 0.8em; color: var(--text-dim); }
  .pulse { width: 8px; height: 8px; border-radius: 50%; display: inline-block; margin-right: 6px; }
  .pulse-ok { background: var(--green); box-shadow: 0 0 6px var(--green); }
  .pulse-warn { background: var(--amber); box-shadow: 0 0 6px var(--amber); }
  .pulse-err { background: var(--red); box-shadow: 0 0 6px var(--red); }

  /* ── Ops Status Bar ────────────────── */
  .opsbar {
    display: flex; align-items: stretch; gap: 0;
    margin-bottom: 16px; border-radius: 8px; overflow: hidden;
    border: 1px solid var(--border); background: var(--bg-panel);
    font-size: 0.72em; letter-spacing: 0.5px;
  }
  .opsbar-cell {
    flex: 1; padding: 8px 14px; border-right: 1px solid var(--border);
    display: flex; flex-direction: column; gap: 2px; min-width: 0;
  }
  .opsbar-cell:last-child { border-right: none; }
  .opsbar-label {
    font-size: 0.75em; color: var(--text-dim); text-transform: uppercase;
    letter-spacing: 1.5px; font-weight: 600; white-space: nowrap;
  }
  .opsbar-value {
    font-size: 1.05em; color: var(--text); font-weight: 400;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .opsbar-value .pill { padding: 1px 7px; border-radius: 8px; font-size: 0.85em; font-weight: 600; }
  .opsbar-value .pill-ok { background: rgba(52,211,153,0.15); color: var(--green); }
  .opsbar-value .pill-warn { background: rgba(251,191,36,0.15); color: var(--amber); }
  .opsbar-value .pill-err { background: rgba(248,113,113,0.15); color: var(--red); }
  .opsbar-value .pill-info { background: rgba(56,189,248,0.15); color: var(--accent); }
  .run-btn {
    display: inline-block; margin-left: 6px; padding: 1px 8px; font-size: 0.82em;
    background: rgba(248,113,113,0.18); color: var(--red); border: 1px solid var(--red);
    border-radius: 3px; cursor: pointer; letter-spacing: 0.5px; transition: background 0.2s;
  }
  .run-btn:hover { background: rgba(248,113,113,0.35); }
  .run-btn.running { background: rgba(251,191,36,0.18); color: var(--amber); border-color: var(--amber);
    animation: pulse-amber 1.2s infinite; }
  @keyframes pulse-amber { 0%,100% { opacity:1; } 50% { opacity:0.5; } }

  /* ── Grid Layout ──────────────────── */
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  .full-width { grid-column: 1 / -1; }

  /* ── Panel ────────────────────────── */
  .panel {
    background: var(--bg-panel); border: 1px solid var(--border); border-radius: 8px;
    overflow: hidden;
  }
  .panel-head {
    display: flex; align-items: center; justify-content: space-between;
    padding: 10px 16px; border-bottom: 1px solid var(--border);
    background: rgba(56,189,248,0.04);
  }
  .panel-head h2 {
    font-size: 0.85em; font-weight: 600; letter-spacing: 2px; text-transform: uppercase;
    color: var(--accent);
  }
  .panel-pill {
    font-size: 0.7em; padding: 2px 10px; border-radius: 12px;
    font-weight: 600; letter-spacing: 1px;
  }
  .pill-ok { background: rgba(52,211,153,0.15); color: var(--green); }
  .pill-warn { background: rgba(251,191,36,0.15); color: var(--amber); }
  .pill-err { background: rgba(248,113,113,0.15); color: var(--red); }
  .pill-info { background: rgba(56,189,248,0.15); color: var(--accent); }
  .panel-body { padding: 16px; }
  .collapsible-toggle { cursor: pointer; user-select: none; }
  .collapsible-toggle:hover { background: rgba(56,189,248,0.08); }
  .collapse-chevron {
    font-size: 0.9em; color: var(--text-dim); transition: transform 0.25s ease;
    display: inline-block; width: 16px; text-align: center;
  }
  .collapsible-body {
    overflow: hidden; transition: max-height 0.35s ease, opacity 0.25s ease, padding 0.25s ease;
    max-height: 1200px; opacity: 1;
  }
  .collapsible-body.collapsed {
    max-height: 0 !important; opacity: 0; padding-top: 0 !important; padding-bottom: 0 !important;
  }
  .collapse-chevron.rotated { transform: rotate(-90deg); }

  /* ── AFD Section ──────────────────── */
  .afd-tabs { display: flex; gap: 4px; margin-bottom: 12px; flex-wrap: wrap; }
  .afd-tab {
    padding: 6px 14px; border-radius: 6px; font-size: 0.75em; font-weight: 600;
    background: var(--bg-card); border: 1px solid var(--border); color: var(--text-dim);
    cursor: pointer; transition: all 0.2s; letter-spacing: 1px;
  }
  .afd-tab:hover { border-color: var(--accent); color: var(--accent); }
  .afd-tab.active { background: rgba(56,189,248,0.12); border-color: var(--accent); color: var(--accent); }
  .afd-content {
    background: var(--bg-card); border: 1px solid var(--border); border-radius: 6px;
    padding: 16px; max-height: 400px; overflow-y: auto; font-size: 0.78em;
    line-height: 1.7; white-space: pre-wrap; color: var(--text);
  }
  .afd-meta { font-size: 0.7em; color: var(--text-dim); margin-bottom: 8px; }

  /* ── NAM Status ───────────────────── */
  .nam-stats { display: grid; grid-template-columns: repeat(5, 1fr); gap: 12px; margin-bottom: 16px; }
  .stat-box {
    background: var(--bg-card); border: 1px solid var(--border); border-radius: 6px;
    padding: 12px; text-align: center;
  }
  .stat-value { font-size: 1.5em; font-weight: 700; }
  .stat-label { font-size: 0.65em; color: var(--text-dim); letter-spacing: 1px; text-transform: uppercase; margin-top: 4px; }
  .val-green { color: var(--green); }
  .val-amber { color: var(--amber); }
  .val-red { color: var(--red); }
  .val-accent { color: var(--accent); }

  .progress-bar {
    height: 6px; background: var(--bg-card); border-radius: 3px; margin-top: 8px; overflow: hidden;
  }
  .progress-fill { height: 100%; border-radius: 3px; transition: width 0.5s ease; }
  .fill-green { background: var(--green); }
  .fill-amber { background: var(--amber); }
  .fill-red { background: var(--red); }

  .nam-timeline {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(100px, 1fr));
    gap: 6px;
    margin-top: 6px;
  }
  .nam-tile {
    display: flex; flex-direction: column; align-items: center; justify-content: center;
    padding: 8px 4px; border-radius: 6px; border: 1px solid var(--border);
    background: rgba(0,0,0,0.3); transition: all 0.25s ease;
    min-height: 58px;
  }
  .nam-tile:hover { transform: translateY(-1px); border-color: var(--accent); }
  .nam-tile-hour { font-size: 0.95em; font-weight: 700; letter-spacing: 0.5px; }
  .nam-tile-utc { font-size: 0.65em; margin-top: 3px; opacity: 0.8; }
  .profile-dot { font-size: 0.6em; color: #38bdf8; margin-top: 1px; line-height: 1; opacity: 0.9; }
  .nam-tile.size-ok .nam-tile-hour { color: var(--green); }
  .nam-tile.size-ok { border-color: rgba(74,222,128,0.3); background: rgba(74,222,128,0.06); }
  .nam-tile.size-warn .nam-tile-hour { color: var(--amber); }
  .nam-tile.size-warn { border-color: rgba(251,191,36,0.3); background: rgba(251,191,36,0.06); }
  .nam-tile.size-err .nam-tile-hour { color: var(--red); }
  .nam-tile.size-err { border-color: rgba(248,113,113,0.3); background: rgba(248,113,113,0.06); }
  .nam-tile.size-dim .nam-tile-hour { color: var(--text-dim); }
  .nam-tile.size-dim { border-color: var(--border); }
  /* timeline legend */
  .nam-legend {
    display: flex; gap: 16px; margin-top: 10px; font-size: 0.7em; color: var(--text-dim);
  }
  .nam-legend-dot {
    display: inline-block; width: 8px; height: 8px; border-radius: 2px; margin-right: 4px; vertical-align: middle;
  }

  .trigger-btn {
    background: rgba(56,189,248,0.1); border: 1px solid var(--accent); color: var(--accent);
    padding: 6px 16px; border-radius: 6px; font-size: 0.75em; font-weight: 600;
    cursor: pointer; letter-spacing: 1px; transition: all 0.2s;
  }
  .trigger-btn:hover { background: rgba(56,189,248,0.25); }
  .trigger-btn:disabled { opacity: 0.4; cursor: not-allowed; }

  /* ── Synoptic Keyword Pills ───────── */
  .syn-kw {
    font-size: 0.7em; padding: 2px 10px; border-radius: 12px;
    font-weight: 600; letter-spacing: 1px; border: 1px solid;
  }
  .syn-kw-front { background: rgba(220,38,38,0.12); border-color: rgba(220,38,38,0.4); color: #f87171; }
  .syn-kw-trough { background: rgba(249,115,22,0.12); border-color: rgba(249,115,22,0.4); color: #fb923c; }
  .syn-kw-ridge { background: rgba(52,211,153,0.12); border-color: rgba(52,211,153,0.4); color: #34d399; }
  .syn-kw-shortwave { background: rgba(245,158,11,0.12); border-color: rgba(245,158,11,0.4); color: #fbbf24; }
  .syn-kw-dryline { background: rgba(234,179,8,0.12); border-color: rgba(234,179,8,0.4); color: #eab308; }
  .syn-kw-surface-low, .syn-kw-low { background: rgba(59,130,246,0.12); border-color: rgba(59,130,246,0.4); color: #60a5fa; }
  .syn-kw-upper-low { background: rgba(139,92,246,0.12); border-color: rgba(139,92,246,0.4); color: #a78bfa; }
  .syn-kw-jet-streak, .syn-kw-jet-streak { background: rgba(56,189,248,0.12); border-color: rgba(56,189,248,0.4); color: #38bdf8; }
  .syn-kw-record-high { background: rgba(236,72,153,0.12); border-color: rgba(236,72,153,0.4); color: #ec4899; }
  .syn-kw-default { background: rgba(100,116,139,0.12); border-color: rgba(100,116,139,0.4); color: #94a3b8; }

  /* ── Log Tail ─────────────────────── */
  .log-tail {
    background: #000; border: 1px solid var(--border); border-radius: 6px;
    padding: 12px; max-height: 200px; overflow-y: auto;
    font-size: 0.7em; line-height: 1.6; color: var(--text-dim);
  }
  .log-tail .log-ok { color: var(--green); }
  .log-tail .log-err { color: var(--red); }
  .log-tail .log-warn { color: var(--amber); }

  /* ── Scrollbar ─────────────────────── */
  ::-webkit-scrollbar { width: 6px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
  ::-webkit-scrollbar-thumb:hover { background: var(--text-dim); }

  /* ── Responsive ────────────────────── */
  @media (max-width: 900px) {
    .grid { grid-template-columns: 1fr; }
    .nam-stats { grid-template-columns: repeat(3, 1fr); }
  }
</style>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;600;700&display=swap" rel="stylesheet">
</head>
<body>

<div class="hud">

  <!-- Top Bar -->
  <div class="topbar">
    <div class="topbar-left">
      <div>
        <div class="topbar-title">☤ NWS OPERATIONS</div>
        <div class="topbar-sub">25TH OWS — MONITORING DASHBOARD</div>
        <div style="display:flex;gap:10px;margin-top:4px;">
          <a href="/synoptic" style="color:var(--accent);font-size:0.7em;letter-spacing:1px;text-decoration:none;font-weight:600;">▶ SYNOPTIC</a>
          <a href="/skewt" style="color:var(--accent);font-size:0.7em;letter-spacing:1px;text-decoration:none;font-weight:600;">▶ SKEW-T</a>
          <a href="/threats" style="color:var(--accent);font-size:0.7em;letter-spacing:1px;text-decoration:none;font-weight:600;">▶ THREATS</a>
          <a href="/events" style="color:var(--orange);font-size:0.7em;letter-spacing:1px;text-decoration:none;font-weight:600;">▶ EVENTS</a>
          <a href="/wiki" style="color:var(--accent2);font-size:0.7em;letter-spacing:1px;text-decoration:none;font-weight:600;">▶ WIKI</a>
        </div>
      </div>
    </div>
    <div class="topbar-right">
      <span><span class="pulse pulse-ok" id="pulse-indicator"></span><span id="conn-status">LIVE</span></span>
      <span id="clock"></span>
    </div>
  </div>

  <!-- Ops Status Bar -->
  <div class="opsbar" id="opsbar">
    <div class="opsbar-cell">
      <div class="opsbar-label">☤ NAM Download</div>
      <div class="opsbar-value" id="ops-nam">&mdash;</div>
    </div>
    <div class="opsbar-cell">
      <div class="opsbar-label">⚗ Threats Active</div>
      <div class="opsbar-value" id="ops-threats">&mdash;</div>
    </div>
    <div class="opsbar-cell">
      <div class="opsbar-label">▶ Scraper</div>
      <div class="opsbar-value" id="ops-scraper">&mdash;</div>
    </div>
    <div class="opsbar-cell">
      <div class="opsbar-label">⚙ Pipeline</div>
      <div class="opsbar-value" id="ops-pipeline">&mdash;</div>
    </div>
    <div class="opsbar-cell">
      <div class="opsbar-label">⚡ Flask</div>
      <div class="opsbar-value" id="ops-flask">&mdash;</div>
    </div>
    <div class="opsbar-cell">
      <div class="opsbar-label">⚡ WX Events</div>
      <div class="opsbar-value" id="ops-events">&mdash;</div>
    </div>
  </div>

  <div class="grid">

    <!-- ── CONUS Synoptic Summary (full width) ──────── -->
    <div class="panel full-width">
      <div class="panel-head collapsible-toggle" onclick="toggleCollapse('synoptic-body', this)">
        <h2>CONUS SYNOPTIC SUMMARY</h2>
        <div style="display:flex;align-items:center;gap:10px;">
          <span class="panel-pill pill-info" id="syn-pill">...</span>
          <a href="/synoptic" style="color:var(--accent);font-size:0.7em;letter-spacing:1px;text-decoration:none;font-weight:600;" onclick="event.stopPropagation()">FULL PAGE ▶</a>
          <span class="collapse-chevron">▾</span>
        </div>
      </div>
      <div class="panel-body collapsible-body" id="synoptic-body">
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;">
          <div>
            <div style="font-size:0.75em;color:var(--accent);letter-spacing:1px;margin-bottom:8px;font-weight:600;">CONUS BULLETIN</div>
            <div id="syn-conus" style="font-size:0.8em;line-height:1.7;color:var(--text);max-height:200px;overflow-y:auto;background:var(--bg-card);padding:12px;border-radius:6px;border:1px solid var(--border);">Waiting for data...</div>
          </div>
          <div>
            <div style="font-size:0.75em;color:var(--accent);letter-spacing:1px;margin-bottom:8px;font-weight:600;">REGIONAL NARRATIVES</div>
            <div id="syn-regions" style="font-size:0.8em;max-height:200px;overflow-y:auto;">Waiting for data...</div>
          </div>
        </div>
        <div style="display:flex;gap:8px;margin-top:12px;flex-wrap:wrap;" id="syn-keywords"></div>
        <div style="font-size:0.65em;color:var(--text-dim);margin-top:8px;" id="syn-meta">—</div>
      </div>
    </div>

    <!-- ── NAM Download Status (full width) ──────────── -->
    <div class="panel full-width">
      <div class="panel-head collapsible-toggle" onclick="toggleCollapse('nam-body', this)">
        <h2>NAM 12KM (AWPHYS) — GRIB2 DOWNLOAD</h2>
        <div style="display:flex;align-items:center;gap:10px;">
          <span class="panel-pill" id="nam-pill">...</span>
          <span class="panel-pill" id="nam-extract-pill" style="font-size:0.65em;">...</span>
          <button class="trigger-btn" id="nam-trigger" onclick="event.stopPropagation(); triggerDownload()">▸ DOWNLOAD</button>
          <span class="collapse-chevron">▾</span>
        </div>
      </div>
      <div class="panel-body collapsible-body" id="nam-body">
        <div class="nam-stats" id="nam-stats">
          <div class="stat-box">
            <div class="stat-value val-accent" id="nam-cycle">--</div>
            <div class="stat-label">Cycle</div>
          </div>
          <div class="stat-box">
            <div class="stat-value val-green" id="nam-completion">--%</div>
            <div class="stat-label">Completion</div>
            <div class="progress-bar"><div class="progress-fill fill-green" id="nam-progress" style="width:0%"></div></div>
          </div>
          <div class="stat-box">
            <div class="stat-value val-accent" id="nam-files">--</div>
            <div class="stat-label">Files</div>
          </div>
          <div class="stat-box">
            <div class="stat-value val-accent" id="nam-size">--</div>
            <div class="stat-label">Total Size</div>
          </div>
          <div class="stat-box">
            <div class="stat-value" id="nam-profiles">--</div>
            <div class="stat-label">Profiles</div>
          </div>
          <div class="stat-box">
            <div class="stat-value" id="nam-extracted">--</div>
            <div class="stat-label">Extracted</div>
          </div>
        </div>
        <div id="nam-cycle-lag" style="display:none;font-size:0.75em;margin-bottom:8px;font-weight:600;"></div>

        <div class="nam-timeline" id="nam-timeline"></div>
        <div class="nam-legend">
          <span><span class="nam-legend-dot" style="background:var(--green)"></span> Downloaded</span>
          <span><span class="nam-legend-dot" style="background:var(--amber)"></span> Checkpoint only</span>
          <span><span class="nam-legend-dot" style="background:var(--red)"></span> Size mismatch</span>
          <span><span class="nam-legend-dot" style="background:var(--text-dim)"></span> Missing</span>
          <span style="color:#38bdf8">⚛ Profiled</span>
          <span style="color:var(--green)">⚔ Threats</span>
        </div>
      </div>
    </div>

    <!-- ── NAM + Scraper Row ──────────────────── -->
    <div class="panel">
      <div class="panel-head">
        <h2>NAM PROFILES</h2>
        <span class="panel-pill" id="nam-processed-pill">...</span>
      </div>
      <div class="panel-body">
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;">
          <div class="stat-box">
            <div class="stat-value" id="nam-dl-cycle">--</div>
            <div class="stat-label">Downloaded</div>
          </div>
          <div class="stat-box">
            <div class="stat-value" id="nam-ext-cycle">--</div>
            <div class="stat-label">Extracted</div>
          </div>
        </div>
        <div style="display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:8px;margin-top:8px;">
          <div class="stat-box">
            <div class="stat-value" id="prof-stations" style="font-size:1.1em;">--</div>
            <div class="stat-label">Stations</div>
          </div>
          <div class="stat-box">
            <div class="stat-value" id="prof-fh" style="font-size:1.1em;">--</div>
            <div class="stat-label">Fcst Hours</div>
          </div>
          <div class="stat-box">
            <div class="stat-value" id="prof-files" style="font-size:1.1em;">--</div>
            <div class="stat-label">Files</div>
          </div>
          <div class="stat-box">
            <div class="stat-value" id="prof-time" style="font-size:1.1em;">--</div>
            <div class="stat-label">Proc Time</div>
          </div>
        </div>
        <div style="margin-top:10px;font-size:0.75em;color:var(--text-dim);" id="nam-pipeline-info">—</div>
      </div>
    </div>

    <div class="panel">
      <div class="panel-head">
        <h2>AFD SCRAPER</h2>
        <span class="panel-pill" id="scraper-pill">...</span>
      </div>
      <div class="panel-body">
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;">
          <div class="stat-box">
            <div class="stat-value" id="scraper-last">--</div>
            <div class="stat-label">Last Run</div>
          </div>
          <div class="stat-box">
            <div class="stat-value val-accent" id="scraper-next">--</div>
            <div class="stat-label">Next Run</div>
          </div>
        </div>
        <div style="margin-top:10px;font-size:0.75em;color:var(--text-dim);" id="scraper-meta">Cron: —</div>
      </div>
    </div>

    <!-- ── AFD Panel ─────────────────────────────────── -->
    <div class="panel">
      <div class="panel-head">
        <h2>AREA FORECAST DISCUSSIONS</h2>
        <span class="panel-pill pill-info" id="afd-pill">5 OFFICES</span>
      </div>
      <div class="panel-body">
        <div class="afd-tabs" id="afd-tabs"></div>
        <div class="afd-meta" id="afd-meta">Select an office</div>
        <div class="afd-content" id="afd-content">Loading...</div>
      </div>
    </div>

    <!-- ── Download Log ──────────────────────────────── -->
    <div class="panel">
      <div class="panel-head">
        <h2>DOWNLOAD LOG</h2>
        <span class="panel-pill pill-info" id="log-count">0 lines</span>
      </div>
      <div class="panel-body">
        <div class="log-tail" id="log-tail">Waiting for data...</div>
      </div>
    </div>

  </div>
</div>

<script>
const OFFICES = {"TWC":"Tucson, AZ","OKX":"New York, NY","PDX":"Portland, OR","LAX":"Los Angeles, CA","DEN":"Denver, CO"};
let afdData = {};
let activeOffice = "TWC";
let downloadTriggered = false;

// ── Collapsible sections ────────────────────────────────────
function toggleCollapse(bodyId, headEl) {
  const body = document.getElementById(bodyId);
  const chevron = headEl.querySelector('.collapse-chevron');
  body.classList.toggle('collapsed');
  chevron.classList.toggle('rotated');
}

// Clock
function updateClock() {
  const now = new Date();
  document.getElementById("clock").textContent = now.toUTCString().slice(17, 25) + " Z";
}
setInterval(updateClock, 1000);
updateClock();

// AFD Tabs
function renderAfdTabs() {
  const container = document.getElementById("afd-tabs");
  container.innerHTML = "";
  for (const [code, name] of Object.entries(OFFICES)) {
    const tab = document.createElement("div");
    tab.className = "afd-tab" + (code === activeOffice ? " active" : "");
    tab.textContent = code;
    tab.title = name;
    tab.onclick = () => { activeOffice = code; renderAfdTabs(); renderAfdContent(); };
    container.appendChild(tab);
  }
}

function renderAfdContent() {
  const data = afdData[activeOffice];
  if (!data) {
    document.getElementById("afd-meta").textContent = "No data";
    document.getElementById("afd-content").textContent = "Waiting for refresh...";
    return;
  }
  const meta = document.getElementById("afd-meta");
  if (data.status === "ok") {
    meta.textContent = (OFFICES[activeOffice] || activeOffice) + " — Issued: " + (data.issued || "unknown") + " — Updated: " + (data.updated || "");
    meta.style.color = "var(--text-dim)";
  } else {
    meta.textContent = data.status;
    meta.style.color = "var(--red)";
  }
  document.getElementById("afd-content").textContent = data.text || "No discussion available.";
}

// NAM rendering
function renderNam(data) {
  // Pill
  const pill = document.getElementById("nam-pill");
  const statusMap = { ok: ["LIVE", "pill-ok"], stale: ["STALE", "pill-warn"],
                      empty: ["EMPTY", "pill-err"], no_directory: ["NO DIR", "pill-err"],
                      pending: ["...", "pill-info"] };
  const [label, cls] = statusMap[data.status] || ["UNKNOWN", "pill-err"];
  pill.textContent = label;
  pill.className = "panel-pill " + cls;

  // Stats
  document.getElementById("nam-cycle").textContent = data.cycle ? data.date + " " + data.cycle.toUpperCase() : "--";

  const pct = data.completion || 0;
  const pctEl = document.getElementById("nam-completion");
  pctEl.textContent = pct + "%";
  pctEl.className = "stat-value " + (pct >= 90 ? "val-green" : pct >= 50 ? "val-amber" : "val-red");

  const bar = document.getElementById("nam-progress");
  bar.style.width = pct + "%";
  bar.className = "progress-fill " + (pct >= 90 ? "fill-green" : pct >= 50 ? "fill-amber" : "fill-red");

  const completedCount = (data.timeline || []).filter(t => t.status === "completed").length;
  document.getElementById("nam-files").textContent = completedCount + " / 29";
  document.getElementById("nam-size").textContent = (data.total_size_mb / 1024).toFixed(1) + " GB";

  // Cycle lag warning
  const cycleLagEl = document.getElementById("nam-cycle-lag");
  if (data.cycle_lag && data.expected_cycle) {
    const expDate = data.expected_date || "";
    const expCycle = data.expected_cycle ? data.expected_cycle.toUpperCase() : "";
    cycleLagEl.textContent = "⚠ Expected: " + expDate + " " + expCycle;
    cycleLagEl.style.display = "block";
    cycleLagEl.style.color = "var(--amber)";
  } else {
    cycleLagEl.style.display = "none";
  }

  // Profiles status
  const profEl = document.getElementById("nam-profiles");
  const procPill = document.getElementById("nam-processed-pill");
  const extractPill = document.getElementById("nam-extract-pill");
  const extractedEl = document.getElementById("nam-extracted");
  const ps = data.profiles_status || {};
  const stnCount = ps.num_stations || 0;
  const fhCount = ps.forecast_hours_count || 0;
  const filesOnDisk = ps.profile_files_on_disk || 0;
  if (ps.processed) {
    profEl.textContent = "✓ " + (ps.extracted_date || "") + " " + (ps.extracted_cycle || "").toUpperCase();
    profEl.className = "stat-value val-green";
    procPill.textContent = "PROCESSED";
    procPill.className = "panel-pill pill-ok";
    extractPill.textContent = "⚔ EXTRACTED t" + (ps.extracted_cycle || "").replace("t","").toUpperCase();
    extractPill.className = "panel-pill pill-ok";
    extractedEl.textContent = stnCount + " stn · " + fhCount + " fh";
    extractedEl.className = "stat-value val-green";
  } else if (ps.extracted_date) {
    profEl.textContent = ps.extracted_date + " " + (ps.extracted_cycle || "").toUpperCase();
    profEl.className = "stat-value val-amber";
    procPill.textContent = "BEHIND";
    procPill.className = "panel-pill pill-warn";
    extractPill.textContent = "⚔ " + (ps.extracted_cycle || "").toUpperCase() + " STALE";
    extractPill.className = "panel-pill pill-warn";
    extractedEl.textContent = stnCount + " stn · " + fhCount + " fh";
    extractedEl.className = "stat-value val-amber";
  } else {
    profEl.textContent = "NONE";
    profEl.className = "stat-value val-red";
    procPill.textContent = "NO DATA";
    procPill.className = "panel-pill pill-err";
    extractPill.textContent = "NOT EXTRACTED";
    extractPill.className = "panel-pill pill-err";
    extractedEl.textContent = "—";
    extractedEl.className = "stat-value val-red";
  }

  // Profiles panel detail
  const dlCycle = data.date && data.cycle ? data.date + " " + data.cycle.toUpperCase() : "--";
  const extCycle = ps.extracted_date && ps.extracted_cycle ? ps.extracted_date + " " + ps.extracted_cycle.toUpperCase() : "--";
  document.getElementById("nam-dl-cycle").textContent = dlCycle;
  document.getElementById("nam-dl-cycle").className = "stat-value val-accent";
  document.getElementById("nam-ext-cycle").textContent = extCycle;
  document.getElementById("nam-ext-cycle").className = "stat-value " + (ps.processed ? "val-green" : "val-amber");
  // Profiles detail stats
  document.getElementById("prof-stations").textContent = stnCount || "—";
  document.getElementById("prof-fh").textContent = fhCount || "—";
  document.getElementById("prof-files").textContent = filesOnDisk || "—";
  const procTimeS = ps.processing_time_s || 0;
  document.getElementById("prof-time").textContent = procTimeS ? (procTimeS / 60).toFixed(1) + "m" : "—";

  // Pipeline info
  const pipeInfo = document.getElementById("nam-pipeline-info");
  if (data.last_pipeline_run) {
    pipeInfo.textContent = "Pipeline: " + data.pipeline_status.toUpperCase() + " at " + data.last_pipeline_run;
  } else {
    pipeInfo.textContent = "Pipeline: never run";
  }

  // Timeline grid
  const tl = document.getElementById("nam-timeline");
  const timeline = data.timeline || [];
  if (timeline.length > 0) {
    tl.innerHTML = timeline.map(t => {
      const sizeClass = t.status === "completed" ? "size-ok"
                      : t.status === "checkpoint_only" ? "size-warn"
                      : t.status === "size_mismatch" ? "size-err"
                      : "size-dim";
      const hourLabel = "f" + (t.hour || "??").padStart(2, "0");
      const utcLabel = t.valid_utc || "";
      const profDot = t.profiled ? '<div class="profile-dot" title="Profile extracted">⚛</div>' : '';
      return `<div class="nam-tile ${sizeClass}" title="${t.status}${t.profiled ? ' — profiled' : ''}">
                <div class="nam-tile-hour">${hourLabel}</div>
                ${profDot}
                <div class="nam-tile-utc">${utcLabel}</div>
              </div>`;
    }).join("");
  } else {
    tl.innerHTML = '<div style="color:var(--text-dim);padding:8px;font-size:0.8em;grid-column:1/-1;">No GRIB2 files found in /mnt/d/weather_data/nam_grib2</div>';
  }

  // Log
  const logEl = document.getElementById("log-tail");
  if (data.last_log_lines && data.last_log_lines.length > 0) {
    logEl.innerHTML = data.last_log_lines.map(l => {
      let cls = "";
      if (/fail|error|missing|timeout/i.test(l)) cls = "log-err";
      else if (/success|complete|✓/i.test(l)) cls = "log-ok";
      else if (/warning|retry|stale/i.test(l)) cls = "log-warn";
      return `<div class="${cls}">${l}</div>`;
    }).join("");
    document.getElementById("log-count").textContent = data.last_log_lines.length + " lines";
  } else {
    logEl.textContent = "No log entries yet.";
  }
}

// Trigger download
function triggerDownload() {
  if (downloadTriggered) return;
  downloadTriggered = true;
  const btn = document.getElementById("nam-trigger");
  btn.disabled = true;
  btn.textContent = "⟳ RUNNING...";
  fetch("/api/nam/trigger", {method: "POST"})
    .then(r => r.json())
    .then(d => {
      setTimeout(() => { downloadTriggered = false; btn.disabled = false; btn.textContent = "▸ DOWNLOAD"; }, 60000);
    })
    .catch(() => { downloadTriggered = false; btn.disabled = false; btn.textContent = "▸ DOWNLOAD"; });
}

// Synoptic rendering
const SYNOPTIC_KW_COLORS = {
  front: 'front', trough: 'trough', ridge: 'ridge', shortwave: 'shortwave',
  dryline: 'dryline', 'surface low': 'surface-low', low: 'low',
  'upper low': 'upper-low', 'jet streak': 'jet-streak',
  'record high': 'record-high', 'arctic': 'default'
};

function kwClass(kw) {
  const k = kw.toLowerCase().replace(/\s+/g, '-');
  for (const [key, cls] of Object.entries(SYNOPTIC_KW_COLORS)) {
    if (k.includes(key.replace(/\s+/g, '-'))) return 'syn-kw-' + cls;
  }
  return 'syn-kw-default';
}

function stripHtml(html) {
  const el = document.createElement('div');
  el.innerHTML = html;
  return el.textContent || el.innerText || '';
}

function renderSynoptic(data) {
  // Pill
  const pill = document.getElementById('syn-pill');
  if (data.office_count > 0) {
    pill.textContent = data.ai_summary_count + '/' + data.office_count + ' OFFICES';
    pill.className = 'panel-pill pill-ok';
  } else {
    pill.textContent = 'NO DATA';
    pill.className = 'panel-pill pill-err';
  }

  // CONUS summary — strip <details> wrapper if present
  const conusEl = document.getElementById('syn-conus');
  const raw = data.conus_summary || '';
  const clean = raw.replace(/<details[^>]*>/gi, '').replace(/<summary[^>]*>.*?<\/summary>/gi, '')
                   .replace(/<\/details>/gi, '').replace(/<br\s*\/?>/gi, '\n').trim();
  conusEl.innerHTML = '';
  conusEl.textContent = stripHtml(clean) || 'No CONUS summary available.';

  // Regional narratives
  const regionsEl = document.getElementById('syn-regions');
  regionsEl.innerHTML = '';
  const regions = data.regional_narratives || {};
  if (Object.keys(regions).length) {
    for (const [name, text] of Object.entries(regions)) {
      const div = document.createElement('div');
      div.style.cssText = 'background:var(--bg-card);padding:10px;border-radius:6px;border:1px solid var(--border);margin-bottom:8px;';
      const label = document.createElement('div');
      label.style.cssText = 'font-size:0.7em;color:var(--accent2);letter-spacing:1px;font-weight:600;margin-bottom:4px;';
      label.textContent = name.toUpperCase();
      const body = document.createElement('div');
      body.style.cssText = 'font-size:0.78em;line-height:1.6;color:var(--text);';
      body.textContent = stripHtml(text).substring(0, 400) + (stripHtml(text).length > 400 ? '...' : '');
      div.appendChild(label);
      div.appendChild(body);
      regionsEl.appendChild(div);
    }
  } else {
    regionsEl.textContent = 'No regional data.';
  }

  // Keyword pills
  const kwEl = document.getElementById('syn-keywords');
  kwEl.innerHTML = '';
  const kw = data.keyword_counts || {};
  const sorted = Object.entries(kw).sort((a, b) => b[1] - a[1]).slice(0, 10);
  for (const [word, count] of sorted) {
    const span = document.createElement('span');
    span.className = 'syn-kw ' + kwClass(word);
    span.textContent = word.toUpperCase() + ' ' + count;
    kwEl.appendChild(span);
  }

  // Meta
  document.getElementById('syn-meta').textContent =
    data.execution_time ? 'Generated: ' + data.execution_time : '—';
}

// Scraper status rendering
function renderScraper(data) {
  const pill = document.getElementById("scraper-pill");
  const lastEl = document.getElementById("scraper-last");
  const nextEl = document.getElementById("scraper-next");
  const metaEl = document.getElementById("scraper-meta");

  if (data.last_run) {
    pill.textContent = "RUN";
    pill.className = "panel-pill pill-ok";
    lastEl.textContent = data.last_run;
    lastEl.className = "stat-value val-green";
  } else {
    pill.textContent = "NEVER";
    pill.className = "panel-pill pill-err";
    lastEl.textContent = "Never";
    lastEl.className = "stat-value val-red";
  }

  if (data.next_run) {
    nextEl.textContent = data.next_run;
  } else {
    nextEl.textContent = "--";
  }

  metaEl.textContent = "Cron: " + (data.cron_schedule || "—");
}

// Ops status bar
function renderOpsStatus(data) {
  if (!data) return;
  // NAM download
  const namEl = document.getElementById("ops-nam");
  if (namEl) {
    const n = data.nam || {};
    const cycle = n.cycle || "—";
    const date = n.date || "—";
    const comp = n.completion != null ? n.completion : 0;
    const lag = n.lag;
    const pillCls = lag ? "pill-warn" : "pill-ok";
    const dateShort = date !== "—" ? date.slice(4) : date; // MMDD
    namEl.innerHTML = `<span class="pill ${pillCls}">${cycle}</span> ${dateShort} <span style="color:var(--text-dim)">${comp}%</span>`;
  }
  // Threats active
  const thrEl = document.getElementById("ops-threats");
  if (thrEl) {
    const t = data.threats || {};
    const tCycle = t.cycle || "—";
    const tDate = t.date || "—";
    const tCurrent = t.current;
    const pillCls = tCurrent ? "pill-ok" : "pill-warn";
    const dateShort = tDate !== "—" ? tDate.slice(4) : tDate;
    const label = tCurrent ? "CURRENT" : "STALE";
    let html = `<span class="pill ${pillCls}">${tCycle}</span> ${dateShort} <span style="color:var(--text-dim)">${label}</span>`;
    if (t.stale) {
      html += ` <span class="run-btn" onclick="runPipeline(this)">RUN PIPELINE</span>`;
    }
    thrEl.innerHTML = html;
  }
  // Scraper
  const scrEl = document.getElementById("ops-scraper");
  if (scrEl) {
    const s = data.scraper || {};
    const lr = s.last_run || "—";
    const h = s.health || "unknown";
    const pillCls = h === "ok" ? "pill-ok" : h === "stale" ? "pill-warn" : "pill-err";
    let html = `<span class="pill ${pillCls}">${h.toUpperCase()}</span> ${lr}`;
    if (s.stale) {
      html += ` <span class="run-btn" onclick="runScraper(this)">RUN SCRAPER</span>`;
    }
    if (s.age_h != null) html += ` <span style="color:var(--text-dim);font-size:0.9em">${s.age_h}h ago</span>`;
    scrEl.innerHTML = html;
  }
  // Pipeline
  const pipEl = document.getElementById("ops-pipeline");
  if (pipEl) {
    const p = data.pipeline || {};
    const lr = p.last_run || "never";
    const st = p.status || "never_run";
    const pillCls = st === "completed" ? "pill-ok" : st === "running" ? "pill-warn" : st === "up_to_date" ? "pill-info" : "pill-err";
    let html = `<span class="pill ${pillCls}">${st.replace(/_/g," ").toUpperCase()}</span> ${lr}`;
    if (p.stale) {
      html += ` <span class="run-btn" onclick="runPipeline(this)">RUN PIPELINE</span>`;
    }
    pipEl.innerHTML = html;
  }
  // Flask
  const flEl = document.getElementById("ops-flask");
  if (flEl) {
    const f = data.flask || {};
    const h = f.health || "unknown";
    const pillCls = h === "up" ? "pill-ok" : "pill-err";
    flEl.innerHTML = `<span class="pill ${pillCls}">${h.toUpperCase()}</span> :5000`;
  }
}
function runScraper(btn) {
  if (btn.classList.contains("running")) return;
  btn.classList.add("running"); btn.textContent = "RUNNING…";
  fetch("/api/run-scraper", {method:"POST"}).then(r => {
    if (r.status === 409) { btn.textContent = "ALREADY RUNNING"; setTimeout(() => { btn.classList.remove("running"); btn.textContent = "RUN SCRAPER"; }, 3000); return; }
    setTimeout(() => { btn.classList.remove("running"); btn.textContent = "RUN SCRAPER"; }, 120000);
  }).catch(() => { btn.classList.remove("running"); btn.textContent = "RUN SCRAPER"; });
}
function runPipeline(btn) {
  if (btn.classList.contains("running")) return;
  btn.classList.add("running"); btn.textContent = "RUNNING…";
  fetch("/api/run-pipeline", {method:"POST"}).then(r => {
    if (r.status === 409) { btn.textContent = "ALREADY RUNNING"; setTimeout(() => { btn.classList.remove("running"); btn.textContent = "RUN PIPELINE"; }, 3000); return; }
    setTimeout(() => { btn.classList.remove("running"); btn.textContent = "RUN PIPELINE"; }, 300000);
  }).catch(() => { btn.classList.remove("running"); btn.textContent = "RUN PIPELINE"; });
}

// Poll loop
async function refresh() {
  try {
    const [namResp, synResp, scraperResp, opsResp, ...afdResps] = await Promise.all([
      fetch("/api/nam").then(r => r.json()),
      fetch("/api/synoptic/summary").then(r => r.json()).catch(() => ({})),
      fetch("/api/scraper-status").then(r => r.json()).catch(() => ({})),
      fetch("/api/ops-status").then(r => r.json()).catch(() => ({})),
      ...Object.keys(OFFICES).map(o => fetch(`/api/afd/${o}`).then(r => r.json()).then(d => { afdData[o] = d; }))
    ]);
    renderNam(namResp);
    if (synResp.office_count) renderSynoptic(synResp);
    if (scraperResp.last_run !== undefined) renderScraper(scraperResp);
    renderOpsStatus(opsResp);
    renderAfdContent();
    document.getElementById("conn-status").textContent = "LIVE";
    document.getElementById("pulse-indicator").className = "pulse pulse-ok";
  } catch(e) {
    document.getElementById("conn-status").textContent = "OFFLINE";
    document.getElementById("pulse-indicator").className = "pulse pulse-err";
  }
}

// Boot
renderAfdTabs();
refresh();
setInterval(refresh, 15000);
</script>
</body>
</html>
'''


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Initial data load
    logger.info("NWS Operations Dashboard starting...")
    
    # Only do local NAM scan at startup (fast, no network)
    try:
        _cache["nam"] = scan_nam_data()
    except Exception as e:
        logger.error(f"Initial NAM scan failed: {e}")
    # Profiles and AFDs loaded lazily to avoid blocking server startup
    logger.info("Initial data: NAM local scan done. AFDs + profiles lazy.")

    # Background refresh thread (fetches AFDs, rechecks NAM data)
    t = threading.Thread(target=background_refresh, daemon=True)
    t.start()

    logger.info("Dashboard ready at http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False)