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