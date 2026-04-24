"""SOUNDERpy Flask Blueprint for VILE dashboard.

Routes:
    GET /api/sounderpy/skewt?station=<code>&cycle=<cycle>&fh=<fh>
    GET /api/sounderpy/hodograph?station=<code>&cycle=<cycle>&fh=<fh>
    GET /api/sounderpy/raob?station=<id>&date=<YYYYmmdd>&hour=<HH>
"""
import os
import glob
import json
import tempfile
from datetime import datetime, timezone

from flask import Blueprint, request, send_file, jsonify

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
        return send_file(
            cached,
            mimetype="image/png",
            max_age=86400,
            etag=True,
            conditional=True,
        )

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

    return send_file(
        output_path,
        mimetype="image/png",
        max_age=86400,
        etag=True,
        conditional=True,
    )


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
        return send_file(
            cached,
            mimetype="image/png",
            max_age=86400,
            etag=True,
            conditional=True,
        )

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

    return send_file(
        output_path,
        mimetype="image/png",
        max_age=86400,
        etag=True,
        conditional=True,
    )


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
