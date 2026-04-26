# NWS Dashboard — Project Log

## 2026-04-14 — Collapsible Sections Added

Previous session crashed mid-task, but the collapsible section changes were already written to `app.py` and survived the crash. Confirmed working via browser inspection.

### What changed

**CSS** (lines 2347-2360):
- `.collapsible-toggle` — click target with hover highlight (`rgba(56,189,248,0.08)`)
- `.collapse-chevron` — ▾ character, rotates -90deg when collapsed
- `.collapsible-body` — smooth `max-height` + `opacity` + `padding` transition (0.35s ease)
- `.collapsible-body.collapsed` — `max-height: 0`, `opacity: 0`, zero padding

**JS** (lines 2674-2680):
- `toggleCollapse(bodyId, headEl)` — toggles `.collapsed` on body and `.rotated` on chevron

**HTML targets**:
- CONUS SYNOPTIC SUMMARY panel (line 2530) — `onclick="toggleCollapse('synoptic-body', this)"`
- NAM 12KM GRIB2 DOWNLOAD panel (line 2556) — `onclick="toggleCollapse('nam-body', this)"`

### Status
- Flask running clean (PID 25654), all API endpoints returning 200
- Dashboard serving at localhost:5000
- Collapsible sections functional on both panels

## 2026-04-26 — VILE SOUNDERpy Custom Plots Phase 4 Complete

All 5 phases of the custom Skew-T/hodograph fork are now locked. No further edits expected to `custom_skewt.py`.

### What changed (Session 11)

- **Full QA cycle:** Both `qa_runner.py` (synthetic) and `qa_runner_real_venv.py` (OUN RAOB) pass with exit code 0
- **Numerical regression:** All verified params match baseline to 0.00 deviation:
  - SBCAPE 644.49, MUCAPE 644.49, MLCAPE 425.90, MLCIN -269.90, DCAPE 1093.67, Γ₀₋₃ 4.99, Γ₃₋₆ 7.90
- **Vision check:** No visual regressions. CAPE/CIN shading visible. No bold/shouting text. No clipping.
- **Dashboard snapshot:** Copies placed in `nws_dashboard/test_output/`
  - `custom_skewt_v11.png`, `custom_hodo_v11.png`, `custom_hodo_sr_v11.png`

### Session resume prompt

Updated: `/mnt/d/session_9_resume_prompt.md` — all phases marked COMPLETE.

### Status
- `custom_skewt.py` locked. Defer `custom_hodo.py` enhancements until Skew-T integration into dashboard begins.

## 2026-04-15 — cfgrib Wind Data Bug Fix

All isobaric wind components (u_ms, v_ms) were `None` in 20260415 12z profiles. Without wind data, BTW (Best Transfer Winds) couldn't compute, so no threat icons rendered.

### Root cause

cfgrib has a silent failure mode: opening `paramId=[131,132]` (u+v together) from NAM GRIB2 files returns an empty dataset when isobaric level dimensions conflict. Most variables have 39 pressure levels; u/v have 42. cfgrib tries to merge them into one dataset, can't reconcile the coordinate, and silently returns zero data variables. No error, no warning.

### Fix in `nam_profile_extractor.py`

- **DATASET_QUERIES**: Split `"isobaric_uv": {paramId: [131,132]}` into two entries:
  - `"isobaric_u": {paramId: 131}`
  - `"isobaric_v": {paramId: 132}`
- **Extraction logic (~line 498)**: Read u from `datasets["isobaric_u"]` and v from `datasets["isobaric_v"]` instead of both from the combined `datasets["isobaric_uv"]`
- Re-ran extraction on 20260415 12z cycle — all profiles regenerated with wind data across 39 levels
- Restarted Flask dashboard

### Status
- Wind data flows again, BTW computes, threat icons render
- Full change-log entry at `~/.hermes/change-logs/nam-threats/2026-04-15.md`

## 2026-04-17 — WX Events live-source-path handoff

Investigated the current `/events` implementation to avoid patching the wrong app again.

### Confirmed live paths
- Live events page is served from `http://localhost:5000/events`
- Live Flask source is `/home/progged-ish/nws_dashboard/app.py`
- Event database in use is `/home/progged-ish/wx_events/wx_events.db`
- Legacy standalone `wx_events` app is not the active web source for the current dashboard page

### User-requested pending changes
- Add `24h` and `48h` options to the events time filter
- Display wind magnitudes in `KT` on the web page
- Re-label wind event types by knot thresholds for both convective and non-convective winds:
  - `25–34 KT` → `Advisory`
  - `35–49 KT` → `Air Force Moderate`
  - `50+ KT` → `Air Force Severe`

### Current state
- Root cause of repeated recap loop was context churn before code patching began
- No live code changes applied yet for the requested `/events` UI/type updates
- Next session should patch `nws_dashboard/app.py`, then verify against live `/api/events` and `/events` on port 5000
- Dashboard PID file currently records `8050` in `/home/progged-ish/nws_dashboard/dashboard.pid`