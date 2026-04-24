#!/usr/bin/env python3
"""SOUNDERpy rendering module for VILE dashboard.

Provides adapters + renderers for NAM model profiles and RAOB observed soundings.
Produces Skew-T/Log-P diagrams and hodographs as PNG images.

Requirements:
    Set MPLBACKEND=Agg before import in headless environments.
    Import specific modules: from sounderpy.plot import SkewT, Hodograph
                             from sounderpy.calc import Parcel
    Do NOT import bare `sounderpy` — its __init__.py runs test code on import.
"""
import os
import json
import glob
import math
from typing import Optional

import numpy as np

# Force Agg backend before matplotlib is touched
os.environ.setdefault("MPLBACKEND", "Agg")
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from sounderpy.plot import SkewT, Hodograph
from sounderpy.calc import Parcel

# ═══════════════════════════════════════════════════════════════
# DARK THEME — matches VILE dashboard style
# ═══════════════════════════════════════════════════════════════
_DARK_RC = {
    "figure.facecolor": "#0d1117",
    "axes.facecolor": "#0d1117",
    "axes.edgecolor": "#8b949e",
    "axes.labelcolor": "#c9d1d9",
    "text.color": "#c9d1d9",
    "xtick.color": "#8b949e",
    "ytick.color": "#8b949e",
    "grid.color": "#30363d",
    "grid.linewidth": 0.6,
}

# ═══════════════════════════════════════════════════════════════
# ① Bolton 1980 dewpoint
# ═══════════════════════════════════════════════════════════════
def dewpoint_from_rh(temp_C: float, rh_pct: float) -> float:
    """Compute dewpoint from temperature (°C) and RH (%) using Bolton 1980.
    Avoids the crude Td ≈ T - (100-RH)/5 approximation (errors 2-10°C).
    """
    es = 6.112 * math.exp((17.67 * temp_C) / (temp_C + 243.5))
    e = es * (rh_pct / 100.0)
    ln_e = math.log(e / 6.112)
    return (243.5 * ln_e) / (17.67 - ln_e)

# ═══════════════════════════════════════════════════════════════
# ① NAM profile JSON → SOUNDERpy clean_data
# ═══════════════════════════════════════════════════════════════
def profile_json_to_sounderpy(station_data: dict, fhour_key: str) -> dict:
    """Convert VILE NAM profile JSON to SOUNDERpy clean_data dict.

    Returns dict with keys: p, t, td, u, v  (all lists, pressure-sorted descending).
    Surface values are prepended to level arrays.
    """
    fh = station_data.get("forecast_hours", {}).get(fhour_key)
    if fh is None:
        raise KeyError(f"forecast hour {fhour_key} not found for station {station_data.get('code')}")

    surface = fh.get("surface", {})
    levels = fh.get("levels", [])

    # Surface values
    sfc_p = surface.get("pressure_hPa")
    sfc_t = surface.get("temp_C")
    sfc_u = surface.get("u10_ms")
    sfc_v = surface.get("v10_ms")
    sfc_rh = surface.get("rh_pct")

    # Fallback: use first level for surface wind / RH if missing
    first_lvl = levels[0] if levels else None
    if sfc_u is None and first_lvl:
        sfc_u = first_lvl.get("u_ms", 0.0)
    if sfc_v is None and first_lvl:
        sfc_v = first_lvl.get("v_ms", 0.0)
    if sfc_rh is None and first_lvl:
        sfc_rh = first_lvl.get("rh_pct", 50.0)

    sfc_td = dewpoint_from_rh(sfc_t, sfc_rh) if sfc_t is not None and sfc_rh is not None else None

    # Build arrays, prepending surface
    p = []
    t = []
    td = []
    u = []
    v = []

    if sfc_p is not None and sfc_t is not None:
        p.append(float(sfc_p))
        t.append(float(sfc_t))
        td.append(float(sfc_td) if sfc_td is not None else float(sfc_t))
        u.append(float(sfc_u) if sfc_u is not None else 0.0)
        v.append(float(sfc_v) if sfc_v is not None else 0.0)

    for lev in levels:
        lp = lev.get("hPa")
        lt = lev.get("temp_C")
        if lp is None or lt is None:
            continue
        p.append(float(lp))
        t.append(float(lt))
        rh = lev.get("rh_pct", 50.0)
        td.append(dewpoint_from_rh(lt, rh))
        u.append(float(lev.get("u_ms", 0.0)))
        v.append(float(lev.get("v_ms", 0.0)))

    clean_data = {
        "p": np.array(p),
        "t": np.array(t),
        "td": np.array(td),
        "u": np.array(u),
        "v": np.array(v),
    }
    return clean_data

# ═══════════════════════════════════════════════════════════════
# ② RAOB observed sounding → SOUNDERpy clean_data
# ═══════════════════════════════════════════════════════════════
def raob_to_sounderpy(station_id: str, year: str, month: str, day: str, hour: str) -> dict:
    """Fetch observed RAOB sounding via SOUNDERpy fetch_obs() and convert to clean_data.

    Station IDs are 3-letter RAOB codes (DTX, MPX) — NOT 4-letter ICAO.
    fetch_obs takes 7 positional args: station, year, month, day, hour, hush, clean_it.
    """
    from sounderpy.obs_data import fetch_obs

    raw = fetch_obs(station_id, year, month, day, hour, "now", True)

    # fetch_obs returns dict with keys: p, z, T, Td, u, v, site_info, titles
    # p = hectopascal, T/Td = degree_Celsius, u/v = knots, z = meters
    if isinstance(raw, dict) and "p" in raw and "T" in raw:
        p = raw["p"]
        t = raw["T"]
        td = raw["Td"]
        u = raw["u"]
        v = raw["v"]

        def _to_numpy(val):
            if hasattr(val, "magnitude"):
                val = val.magnitude
            arr = np.array(val)
            if hasattr(arr, "filled"):
                arr = arr.filled(np.nan)
            return arr

        # Convert wind from knots to m/s for consistency with NAM adapter
        KT_TO_MS = 0.514444
        return {
            "p": _to_numpy(p),
            "t": _to_numpy(t),
            "td": _to_numpy(td),
            "u": _to_numpy(u) * KT_TO_MS,
            "v": _to_numpy(v) * KT_TO_MS,
        }

    raise ValueError(f"Unexpected RAOB data structure from fetch_obs: {type(raw)}")

# ═══════════════════════════════════════════════════════════════
# ③ Skew-T renderer
# ═══════════════════════════════════════════════════════════════
def render_skewt_png(
    clean_data: dict,
    station_code: str,
    station_name: str,
    valid_time: str,
    elevation_m: float = 0.0,
    output_path: str = None,
    figsize: tuple = (11, 9),
) -> str:
    """Generate publication-quality Skew-T/Log-P PNG using SOUNDERpy.

    Returns output_path.
    """
    p = clean_data["p"]
    t = clean_data["t"]
    td = clean_data["td"]
    u = clean_data.get("u")
    v = clean_data.get("v")

    with plt.rc_context(_DARK_RC):
        fig = plt.figure(figsize=figsize)
        skewt = SkewT(fig=fig)
        fig.patch.set_facecolor("#0d1117")

        # Temperature profile (red)
        skewt.plot(p, t, color="tab:red", linewidth=2, label="Temperature")
        # Dewpoint profile (green)
        skewt.plot(p, td, color="tab:green", linewidth=2, label="Dewpoint")

        # Reference lines
        skewt.plot_dry_adiabats()
        skewt.plot_moist_adiabats()
        skewt.plot_mixing_lines()

        # Wind barbs
        if u is not None and v is not None and len(u) == len(p):
            skewt.plot_barbs(p, u, v)

        # Parcel trace + CAPE/CIN shading
        if len(p) >= 2 and not np.isnan(p[0]) and not np.isnan(t[0]) and not np.isnan(td[0]):
            try:
                parcel = Parcel(
                    pbot=float(p[0]),
                    ptop=float(p[-1]),
                    pres=float(p[0]),
                    tmpc=float(t[0]),
                    dwpc=float(td[0]),
                )
                if hasattr(parcel, "ttrace") and parcel.ttrace is not None:
                    import numpy.ma as ma
                    pt_arr = np.array(parcel.ttrace)
                    pp_arr = np.array(parcel.ptrace)
                    if hasattr(pt_arr, "filled"):
                        pt_arr = pt_arr.filled(np.nan)
                        pp_arr = pp_arr.filled(np.nan)

                    # Filter out NaN for plotting
                    mask = ~(np.isnan(pt_arr) | np.isnan(pp_arr))
                    if mask.sum() > 1:
                        skewt.ax.plot(pp_arr[mask], pt_arr[mask],
                                      color="tab:orange", linewidth=2.5, linestyle="--", label="Parcel")

                        # Try shading
                        try:
                            skewt.shade_cape(p, t, pt_arr)
                            skewt.shade_cin(p, t, pt_arr, dewpoint=td)
                        except Exception:
                            pass

                        # Annotation
                        cape = parcel.bplus if hasattr(parcel, "bplus") and parcel.bplus is not None else "N/A"
                        cin = parcel.bminus if hasattr(parcel, "bminus") and parcel.bminus is not None else "N/A"
                        ann_text = f"CAPE: {cape} J/kg\nCIN: {cin} J/kg"
                        skewt.ax.annotate(
                            ann_text,
                            xy=(0.02, 0.95),
                            xycoords="axes fraction",
                            fontsize=10,
                            color="#c9d1d9",
                            verticalalignment="top",
                            fontfamily="monospace",
                            bbox=dict(boxstyle="round,pad=0.3", facecolor="#161b22", edgecolor="#30363d", alpha=0.9),
                        )
            except Exception:
                pass

        # Title
        title = f"{station_code} / {station_name} — {valid_time}  |  Elev: {elevation_m:.0f} m"
        skewt.ax.set_title(title, fontsize=12, color="#c9d1d9", pad=10)
        skewt.ax.legend(loc="upper right", fontsize=8, facecolor="#161b22", edgecolor="#30363d")

        fig = skewt.ax.figure
        fig.tight_layout()

        if output_path:
            fig.savefig(output_path, dpi=150, bbox_inches="tight", facecolor="auto")
            plt.close(fig)
        else:
            plt.close(fig)

    return output_path

# ═══════════════════════════════════════════════════════════════
# ④ Hodograph renderer
# ═══════════════════════════════════════════════════════════════
def render_hodograph_png(
    clean_data: dict,
    station_code: str,
    station_name: str,
    valid_time: str,
    output_path: str = None,
    figsize: tuple = (9, 9),
    component_range: float = 80,
) -> str:
    """Generate hodograph PNG using SOUNDERpy.  Returns output_path."""
    u = clean_data.get("u")
    v = clean_data.get("v")
    p = clean_data.get("p")

    if u is None or v is None or len(u) < 2:
        # Create a placeholder with warning
        with plt.rc_context(_DARK_RC):
            fig, ax = plt.subplots(figsize=figsize)
            ax.set_facecolor("#0d1117")
            ax.text(0.5, 0.5, "Insufficient wind data for hodograph",
                    ha="center", va="center", transform=ax.transAxes, color="#c9d1d9", fontsize=12)
            ax.set_title(f"{station_code} / {station_name} — {valid_time}", fontsize=12, color="#c9d1d9")
            if output_path:
                fig.savefig(output_path, dpi=150, bbox_inches="tight", facecolor="auto")
                plt.close(fig)
            return output_path

    with plt.rc_context(_DARK_RC):
        fig, ax = plt.subplots(figsize=figsize)
        ax.set_facecolor("#0d1117")

        hodo = Hodograph(ax=ax, component_range=component_range)
        hodo.plot(u, v, color="tab:cyan", linewidth=2)

        # Mark surface point
        ax.scatter([u[0]], [v[0]], s=100, c="yellow", zorder=10, label="Surface", edgecolors="black")

        # Add grid
        hodo.add_grid()

        # Color-mapped by pressure if available
        if p is not None and len(p) == len(u):
            try:
                hodo.plot_colormapped(u, v, p, cmap="viridis_r")
            except Exception:
                pass

        ax.set_title(f"Hodograph — {station_code} / {station_name}\n{valid_time}",
                     fontsize=12, color="#c9d1d9")
        ax.legend(loc="upper right", fontsize=8, facecolor="#161b22", edgecolor="#30363d")
        fig.tight_layout()

        if output_path:
            fig.savefig(output_path, dpi=150, bbox_inches="tight", facecolor="auto")
            plt.close(fig)
        else:
            plt.close(fig)

    return output_path

# ═══════════════════════════════════════════════════════════════
# ⑤ Pre-render pipeline
# ═══════════════════════════════════════════════════════════════
def pre_render_cycle(
    profile_dir: str,
    cache_dir: str,
    cycle: str,
) -> dict:
    """Iterate stations + forecast hours, render Skew-T + hodograph PNGs.

    profile_dir: directory containing profile JSON files.
    cache_dir: base cache directory; cycle subdir is appended.
    cycle: cycle identifier, e.g. "20260411_18Z".

    Returns {rendered: int, cached: int, errors: int, details: list}.
    """
    cycle_cache = os.path.join(cache_dir, cycle)
    os.makedirs(cycle_cache, exist_ok=True)

    pattern = os.path.join(profile_dir, f"nam_{cycle}_*.json")
    files = sorted(glob.glob(pattern))

    rendered = 0
    cached = 0
    errors = 0
    details = []

    for filepath in files:
        basename = os.path.basename(filepath)
        try:
            with open(filepath) as f:
                station_data = json.load(f)
        except json.JSONDecodeError as e:
            errors += 1
            details.append({"file": basename, "error": str(e)})
            continue

        code = station_data.get("code", "???")
        name = station_data.get("name", code)
        elevation_m = station_data.get("elevation_m", 0.0)
        forecast_hours = station_data.get("forecast_hours", {})

        for fh_key, fh_data in forecast_hours.items():
            valid_time = fh_data.get("valid_time", "")

            # Filenames
            skewt_name = f"skewt_{code}_{fh_key}.png"
            hodo_name = f"hodograph_{code}_{fh_key}.png"
            skewt_path = os.path.join(cycle_cache, skewt_name)
            hodo_path = os.path.join(cycle_cache, hodo_name)

            try:
                clean_data = profile_json_to_sounderpy(station_data, fh_key)
            except Exception as e:
                errors += 1
                details.append({"station": code, "fhour": fh_key, "error": str(e)})
                continue

            # Skew-T
            try:
                if os.path.exists(skewt_path):
                    cached += 1
                else:
                    render_skewt_png(clean_data, code, name, valid_time, elevation_m, skewt_path)
                    rendered += 1
            except Exception as e:
                errors += 1
                details.append({"station": code, "fhour": fh_key, "type": "skewt", "error": str(e)})

            # Hodograph
            try:
                if os.path.exists(hodo_path):
                    cached += 1
                else:
                    render_hodograph_png(clean_data, code, name, valid_time, hodo_path)
                    rendered += 1
            except Exception as e:
                errors += 1
                details.append({"station": code, "fhour": fh_key, "type": "hodograph", "error": str(e)})

    summary = {
        "rendered": rendered,
        "cached": cached,
        "errors": errors,
        "details": details,
        "cycle_cache": cycle_cache,
    }
    return summary

# ═══════════════════════════════════════════════════════════════
# ⑥ Cache lookup
# ═══════════════════════════════════════════════════════════════
def get_cached_png(
    station_code: str,
    fhour: str,
    cycle: str,
    image_type: str,  # "skewt" or "hodograph"
    cache_dir: str,
) -> Optional[str]:
    """Return cached PNG path if it exists, else None."""
    fname = f"{image_type}_{station_code}_{fhour}.png"
    path = os.path.join(cache_dir, cycle, fname)
    return path if os.path.isfile(path) else None

# ═══════════════════════════════════════════════════════════════
# One-off render from profile JSON path (convenience)
# ═══════════════════════════════════════════════════════════════
def render_profile_json(
    json_path: str,
    fhour_key: str = "f00",
    output_dir: Optional[str] = None,
) -> dict:
    """Convenience: load a single profile JSON and render both images.
    Returns {skewt_path, hodograph_path}.
    """
    with open(json_path) as f:
        station_data = json.load(f)

    clean_data = profile_json_to_sounderpy(station_data, fhour_key)
    code = station_data.get("code", "UNK")
    name = station_data.get("name", code)
    elevation_m = station_data.get("elevation_m", 0.0)
    fh_data = station_data.get("forecast_hours", {}).get(fhour_key, {})
    valid_time = fh_data.get("valid_time", "")

    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
        skewt_path = os.path.join(output_dir, f"skewt_{code}_{fhour_key}.png")
        hodo_path = os.path.join(output_dir, f"hodograph_{code}_{fhour_key}.png")
        render_skewt_png(clean_data, code, name, valid_time, elevation_m, skewt_path)
        render_hodograph_png(clean_data, code, name, valid_time, hodo_path)
        return {"skewt_path": skewt_path, "hodograph_path": hodo_path}
    else:
        skewt_path = f"skewt_{code}_{fhour_key}.png"
        hodo_path = f"hodograph_{code}_{fhour_key}.png"
        render_skewt_png(clean_data, code, name, valid_time, elevation_m, skewt_path)
        render_hodograph_png(clean_data, code, name, valid_time, hodo_path)
        return {"skewt_path": skewt_path, "hodograph_path": hodo_path}
