# Lock-In Pump-Probe — Project Memory

A running record of what this app is, how it's built, and every change made.
Keep appending to the **Change Log** at the bottom (newest first).

---

## 1. What it is

A standalone **lock-in-based pump-probe delay-scan GUI** (PyQt via `pyqtgraph`).
It drives a Thorlabs delay stage across a set of pump-probe delays while reading
an SR865A lock-in amplifier at each point, plots signal vs delay live, and saves
the data with full metadata.

It is a clean re-implementation of the **lock-in path** of the reference repo
`repo/Labview-pumprobepython-main` — the LabVIEW/camera and TWINS/k-space paths
are intentionally left out. `local_reference/` holds older lab scripts for
reference only.

---

## 2. How to run

Easiest: double-click **`run_app.bat`** in this folder (keeps a console open so
you can read the device logs and any traceback).

Otherwise, real Python here is Anaconda (the `python` on PATH is a Windows Store
stub that fails). Neither path contains spaces, so run them directly — do **not**
prefix with `&`, PowerShell rejects it with *AmpersandNotAllowed*:

```
C:\Users\mguizzardi\AppData\Local\anaconda3\python.exe C:\Users\mguizzardi\Desktop\pumpprobe_project\app\main.py
```

Dependencies (already installed): pyqtgraph 0.13.7, numpy 1.26.4, qcodes,
pyvisa (NI-VISA), thorlabs_apt, pylablib.

To screenshot the GUI headless for design review: use the **default Windows** Qt
platform (NOT `offscreen`, which can't rasterize text) and `widget.grab().save()`.

---

## 3. Module map (`app/`)

| File | Role |
|------|------|
| `main.py` | Entry point. Applies the theme, builds the window: header + two tabs — **Manual Control** (lock-in + pump & probe stage panels) and **Pump-Probe Scan** (stage selector + scan widget). Wires scan start/finish to pause the driven stage panel. |
| `style.py` | Central dark theme. `apply_theme(app)` sets font, full QSS, pyqtgraph colors. Accent constants: `BLUE/GREEN/AMBER/RED/MUTED`. |
| `driver_lockin.py` | SR865A driver (QCoDeS), singleton `LockInDriver`. `read_value`, `read_snapshot`, `read_buffered` (X/Y/R/theta mean+std). |
| `lockin_widget.py` | `LockInControlWidget`: channel select, time constant, harmonic, live monitor plot. |
| `thorlabs_stage.py` | `ThorlabsDelayStage` (one instance per physical stage). Optics-aware `mm_to_fs`/`fs_to_mm` (double-pass factor 2). Non-blocking moves + `is_moving()`. |
| `stage_widget.py` | `StageControlWidget`: device dropdown + Rescan, connect/home/jog/go-to/set-t0, live position. |
| `delay_interval_widget.py` | N-zone scan-point generator with **fs/mm unit toggle** and per-zone fs↔mm readout. |
| `metadata_widget.py` | `MetadataWidget`: sample, pump power, wavelengths, chopper, temperature, notes. |
| `scan_widget.py` | `PumpProbeScanWidget`: the delay-scan engine (main-thread state machine) + live plot + auto-save. |
| `driver_spectrometer.py` | Ocean Optics driver (`seabreeze`, **pyseabreeze** backend), singleton + simulation fallback. Also `delta_t_over_t()`. |
| `spectrometer_window.py` | Separate window: live spectrum, background/reference capture, pump-probe ΔT/T **map** (spectrum at every delay) + dynamics cut. |

---

## 4. Hardware notes / gotchas

- **SR865A lock-in** VISA address: `USB0::0xB506::0x2000::004418::INSTR`
  (serial **004418**). This is the default in `driver_lockin.py` and
  `lockin_widget.py`. The **Find** button auto-detects any SR865A.
- **Thorlabs stages** discovered on this PC: serials `94203585` (apt),
  `28252278` (apt), `27267060` (apt), `73203584` (kinesis).
- **OceanDirect SDK is NOT installed** (checked 2026-07-23). Only *OceanView*
  (Java GUI, `C:\Program Files\Ocean Optics\OceanView`) and the *OceanDirect
  Sample Pack* (example source only — no wheel/DLL) are present. To use the
  **OceanNR**, download and install the actual **OceanDirect SDK** from Ocean
  Insight, then `pip install` its Python wheel. `driver_spectrometer.py` already
  has an OceanDirect backend that activates automatically once `oceandirect`
  imports (API verified against the official sample pack).
- **Ocean Optics spectrometers**: two are plugged in, but only one is usable —
  the **Flame-S `FLMS13830`** (2048 px, 339.8–1024 nm, integration 1 ms–655 s)
  via the **`pyseabreeze`** backend. The default `cseabreeze` backend returns an
  empty device list on this PC, so `driver_spectrometer.py` selects pyseabreeze
  explicitly (must be done *before* importing `seabreeze.spectrometers`).
  The second device (**OceanNR `NR1700501`**, USB VID 0x0999) is a newer Ocean
  Insight model that python-seabreeze does not support — it needs the OceanDirect
  SDK, which is not installed. It is visible to pyusb but not to seabreeze.
- **APT DLL is DISABLED** (`thorlabs_stage._USE_APT = False`, 2026-07-28). Even a
  bare `thorlabs_apt.list_available_devices()` **segfaults the process**
  (0xC0000005 / exit -1073741819) on this PC — uncatchable from Python — and both
  stage panels enumerating it at launch was the "code keeps crashing". The app now
  uses **pylablib / Kinesis only** for both discovery and connection; pylablib sees
  every controller (brushed / brushless / APT-protocol) and connects fine. Do not
  re-enable APT unless it's proven stable. (Historical note: APT was also
  thread-affine, which is why all stage access is on the GUI/main thread with
  non-blocking + QTimer-polled moves; that design still stands.)
- If connects start failing with a Kinesis handshake error ("expected 0x0006,
  got 0x0491"), it's usually leftover state from a prior crashed process — close
  stray python/pythonw processes or unplug/replug the stage.
- **Lock-in buffered capture is flaky** on serial 004418 ("cannot reshape array
  of size 390 into shape (4)"). `read_buffered` latches `_buffer_ok=False` on the
  first failure and uses snapshot averaging for the rest of the session
  (X/Y/R/theta mean+std, unaffected data).
- VISA: only NI-VISA installed (no pyvisa-py); use the default ResourceManager.

---

## 5. Data output

Every scan auto-saves (no button press) to `D:\pumpprobedata\YYYY\MM\DD\`
(falls back to the app's working directory if `D:` is unavailable) as
`<sample>_lockin_<HHMMSS>.{csv,npz,json}`:

- **CSV** (appended live, crash-safe): `delay_fs, position_mm, X, Y, R, theta_deg`
- **NPZ** (at finish): the above arrays + `target_mm, X_std, Y_std, R_std,
  metadata_json`
- **JSON** sidecar: full metadata (user fields + auto lock-in TC/sensitivity/
  frequency, channel, zero, zones, timestamps, point counts). Written at scan
  start too, so conditions survive a crash.

The **Save a copy as…** button makes an extra copy elsewhere; the scan is already
saved automatically.

---

## 6. Change Log (newest first)

### 2026-07-28

- **Lock-in scan wouldn't start → fixed (lock-in connect was failing).** The
  SR865A connect threw `VI_ERROR_INP_PROT_VIOL` / then a timeout during the QCoDeS
  init's first query (`CAPTURERATEMAX?`) because the device was left mid-transfer /
  mid-buffer-capture (from an earlier crash). With no lock-in connected, the scan
  refused to start. Fixes: (1) `LockInDriver._raw_clear()` runs a raw-VISA recovery
  BEFORE QCoDeS talks to the device — `clear()` + `CAPTURESTOP` + `*CLS` + `clear()`
  + `*IDN?` re-sync (keep it MINIMAL; a flush-read loop made it worse). (2)
  **Buffered capture is now OFF by default** (`_buffer_ok = False`) — on this
  SR865A it returns malformed values and corrupts the I/O so following reads fail;
  reads now use the robust SNAP snapshot path. Verified on real 004418: connect
  0.05 s, 5 clean X/Y/R/theta reads, disconnect clean.
- **Stage counts/mm auto-detected from the controller model.** On connect, if no
  per-serial value is saved, the app reads the controller model (`get_device_info
  ().model_no`, e.g. KBD101) and maps it to a counts/mm via
  `_CONTROLLER_COUNTS_PER_MM` (KBD101/201->2000, BBD*->20000, KDC101/TDC001->34304)
  — `detect_counts_per_mm()`. The physical stage itself often can't be read
  (KBD101 reports "step"), so this is a model-based best guess. Priority on connect:
  **saved per-serial value > auto-detect > 20000 default**. An **"Auto"** button
  re-detects and saves; a source label shows "auto — KBD101 → 2000" / "saved" /
  "not recognised". Verified on the real KBD101: auto → 2000, position reads 91.85
  mm (was a wrong 9.19).
- **Stage encoder scale (counts/mm) is now per-stage & configurable** — fixes
  "go-to moves to the wrong place". The hardcoded `COUNTS_PER_MM = 20000` was
  wrong for the **KBD101 + DDSM50/DDSM100** stage, which is **2000 counts/mm**
  (10x less → moves overshot 10x; raw 183706 counts = 91.8 mm at 2000, not 9.2 mm
  at 20000). Each stage panel now has a **Stage preset dropdown** (DDSM=2000,
  DDS220/300/600=20000, MTS/Z8=34304, Z6xx=24600, Custom) + an editable
  **Counts/mm** field. The value applies to the stage and is **persisted per
  serial** (`config_store.get/set_stage_counts_per_mm`), restored on connect
  (before t0). pylablib's own scale table (kinesis.py ~line 1250) is the source
  of the preset values. Verified: preset -> move_to(10mm) sends 20000 counts,
  reads back 10 mm, persists, restores on a fresh instance.
- **Added "Go to t0" button** on each stage panel, next to "Set current as t0" —
  moves the stage to its saved t0 (non-blocking + polled, controls lock during
  the move). The `t0 = … mm` label moved to its own line.
- **Fixed hard crash ("code keeps crushing").** Root cause isolated to the
  Thorlabs **APT DLL** (`thorlabs_apt`): a bare `apt.list_available_devices()`
  segfaults the process (0xC0000005), and both stage panels called it at launch.
  Fix: `_USE_APT = False` in `thorlabs_stage.py` — APT is never imported/called;
  the app uses **pylablib/Kinesis** for discovery + connection (sees all three
  stages, connects, reads position). Verified: enumeration ×2 no longer crashes,
  full-app stability run (idle, tab-switching, open/close spectrometer ×2) exits
  cleanly. A PC restart is NOT required.

### 2026-07-27

- **Per-stage t0 (zero) now persists across restarts/days.** New `config_store.py`
  writes a JSON file `pumpprobe_config.json` at the project root (atomic writes),
  storing each stage's zero **keyed by its serial number**. Setting zero anywhere
  saves it: the stage panel's "Set current as t0" (`set_zero`), the scan's
  "Set = current", and the spectrometer map's "Set = current". On **connect**, a
  stage restores its saved t0 (`_load_saved_zero`, label shows "(restored)"). The
  stage emits `zero_changed`; main mirrors it into the scan and spectrometer t0
  fields when that stage drives them, and `set_delay_stage` / the map's stage
  combo adopt the connected stage's t0. Verified: save -> fresh app instance ->
  restored; unknown serial keeps its own value.

- **Two lock-ins supported, acquired simultaneously (incl. pump-probe).**
  `LockInDriver` changed from a singleton to a **registry keyed by dev_id**
  ("A"/"B"); each gets its own VISA resource + QCoDeS instrument name
  (`lockin_A`/`lockin_B`). `LockInDriver()` still maps to "A" (back-compat).
  `LockInControlWidget(dev_id, resource)` — main builds two panels (A defaults to
  serial 004418; B is entered/found), shown stacked in a scrollable Lock-In
  column on Manual Control. The scan takes `lockin_widgets=[A, B]`, reads every
  CONNECTED lock-in at each delay point (one worker thread reads them back-to-
  back), plots one curve per lock-in (A=orange, B=cyan) of its selected channel,
  and auto-settle uses the MAX time constant across them. Saving: CSV columns
  `X_A,Y_A,R_A,theta_A[,X_B,...]`; npz arrays suffixed by id (`R_A`, `X_B`,
  `theta_deg_B`, `*_std_*`); metadata gets a `lockins` list + `n_lockins`. Works
  with one or two connected; refuses to start if none. Data model is now
  `d_lockin[dev_id][channel]` (the old flat `d_X/d_R/...` lists are gone).

### 2026-07-25

- **Scan mm mode is now ABSOLUTE stage position** (was travel offset from t0).
  Typing `185→192` in mm mode moves the stage to exactly 185–192 mm; the fs/delay
  axis is computed as travel from t0. fs mode is unchanged (delay relative to t0).
  Implemented centrally in `DelayIntervalWidget.get_scan_plan(stage, zero, invert)`
  which returns `(target_mm, delay_fs)` pairs; both the lock-in scan and the
  spectrometer map now use it (they no longer recompute the target themselves).
  The zone count label shows the resolved range ("stage 185.0000 … 192.0000 mm
  (absolute)" or "delay … fs (from t0)"). This resolves the reported "+5 mm
  misread" — that +5 was simply t0=5 being added under the old relative semantics.
- **Kinesis position scaling flagged as suspect** — kinesis uses a hardcoded
  `COUNTS_PER_MM = 20000` (a guess). Added `_log_kinesis_units()` diagnostics at
  connect (prints pylablib `get_scale`/`get_scale_units`/`get_stage` + raw
  position) so the true scale can be verified. NOT changed blindly: pylablib's
  `scale="stage"` can report METERS, so a naive switch risks a 1000x error.
  Awaiting the console output / concrete symptom from the user.

### 2026-07-23

- **Spectrometer live view made smooth** — measured **6.5 → 30.7 fps (4.7x)**.
  The dominant cause was the live timer's fixed **120 ms** interval, which capped
  the view at ~8 fps and left the device idle most of each cycle; it is now 25 ms
  with frame-dropping (`_live_tick` skips while a read is in flight), so the rate
  follows the integration time. Also: the live worker **QThread is reused** instead
  of being recreated every frame; the ROI mask is cached (`_recompute_mask`); the
  per-frame path (`_redraw_live`) now updates **only** the live curve — the
  background/reference traces, axis label and `setXRange` moved to
  `_redraw_spectra`, which runs only when something changes (this also stopped the
  view resetting under the user's zoom every frame); and the 2048-point curves use
  `setClipToView` + `autoDownsample` with antialiasing off.
- **Spectrometer driver is now multi-backend**: tries **OceanDirect** (Ocean
  Insight SDK) first, then **seabreeze**, then simulation. `list_devices()`
  merges both backends and remembers which one owns each serial, so `connect()`
  routes correctly. The OceanDirect path uses **hardware averaging**
  (`set_scans_to_average`) when available, falling back to software averaging.
  API verified against Ocean's official Python sample pack. The backend in use is
  recorded in the saved metadata (`spectrometer_backend`).
  NOTE: `list_devices()` is now an *instance* method, not a staticmethod.
- Investigated the OceanView install: it rebinds the spectrometers to Ocean's own
  "(WinUSB)" driver, which briefly broke pyseabreeze with
  `USBError(13, Access denied)`. It recovered on its own — the Flame-S works
  again via pyseabreeze — but keep this in mind if access errors reappear.

### 2026-07-21

- **Spectrometer: ΔT/T | ΔT | T selector** — a "Map shows:" combo above the map
  switches what the map *and* the dynamics cut display. All three are derived on
  demand from the stored raw spectra, so you can switch freely during or long
  after a scan without re-measuring:
  `ΔT/T = (spec-ref)/(ref-bg)`, `ΔT = spec-ref`, `T = spec-bg`.
  Signed quantities (ΔT/T, ΔT) use a diverging blue-white-red scale centred on 0;
  the positive-only T uses a sequential viridis-like scale from 0. The npz now
  saves **all three** (`dtt`, `dt`, `t`) plus ROI-matched `background_roi` /
  `reference_roi`. `map_raw` is the single source of truth (the old `map_dtt`
  list is gone).
- **Spectrometer: dark removed from the live view** — once a Background is taken
  it is subtracted from the displayed spectra (live *and* reference), the y-axis
  reads "Counts (dark-corrected)", and the (now-zero) background trace is hidden.
  A "Subtract dark from live view" checkbox (on by default) toggles it back to
  raw counts.
- **Spectrometer: adjustable wavelength range (ROI)** — From/To spin boxes plus a
  "Full detector range" button. The ROI crops the live plot, the ΔT/T map, the
  dynamics cut, the raw CSV and the saved arrays. It is **frozen at map start** so
  every row keeps the same length. The npz stores the ROI axis as
  `wavelengths_nm` (matching `spectra`/`dtt`) and the untouched detector axis as
  `wavelengths_full_nm`; the range and pixel count go into the metadata.
- **Ocean Optics spectrometer — new separate window** (`Open Spectrometer Window`
  button in the main header). Provides:
  - device dropdown + Rescan + Connect/Disconnect, **integration time** and
    **averages** controls, and a **live spectrum** view;
  - **Take Background (dark)** and **Take Reference (probe only)** — two separate
    measurements, both overlaid on the spectrum plot and stored in the saved file;
  - **Pump-probe map**: takes a spectrum at every delay-stage position and shows
    it as a **ΔT/T map** (delay × wavelength, diverging blue-white-red colormap)
    with a draggable wavelength cursor and a **ΔT/T-vs-delay dynamics cut**;
    Run / Pause / Stop, and auto-save.
  - ΔT/T = `(spectrum - reference) / (reference - background)`; pixels whose
    reference transmission is below a floor are zeroed to avoid dividing by noise.
  - Saves `<sample>_specmap_<HHMMSS>.{npz,json,_raw.csv}` — npz holds
    `wavelengths_nm, delay_fs, position_mm, spectra (n×px), dtt (n×px),
    background, reference, metadata_json`.
  - The map reuses the same **main-thread** stage state machine (APT is
    thread-affine); only the spectrometer read runs on a worker thread.
  - Falls back to a **simulation mode** (synthetic transient at 600 nm) when no
    spectrometer is present, so the GUI can be exercised offline.
- **Axis fix** — disabled pyqtgraph's automatic SI prefixes on the wavelength and
  delay axes (they were rendering 600 nm as "0.6 knm" and 3000 fs as "3 kfs").
- **Fix crash on window close** — `main.py`'s `closeEvent` still referenced the
  old scan `worker` (removed in the main-thread-scan rewrite), raising
  `AttributeError` on exit. Now it stops the state machine and waits for the
  in-flight lock-in read (`_reader`).
- **Change the connected stage** — the stage panel's **Connect** button now toggles
  to **Disconnect** once connected. Disconnecting frees the device and re-enables
  the dropdown + Rescan so a different stage can be selected and connected. (The
  driver's `connect()` no-ops when already connected, so a disconnect was required
  to switch.) Verified on real hardware: 94203585 → disconnect → 28252278.
- **Pause/Resume during a scan** — added a Pause button next to Stop in the scan
  panel. Pausing takes effect at the next point boundary (the point in progress —
  move + settle + lock-in read — always completes, so no point is left half-done),
  then holds; Resume continues from where it stopped without re-saving or losing
  data. Guarded against a Pause-then-Resume race so it can't double-advance.

### 2026-07-17

- **Added this `MEMORY.md`** to track the project and its changes.
- **Fixed "stage connection failed" + prior "not responding" properly** by
  removing background threads for stage ops (the APT DLL is thread-affine and was
  crashing across threads). All stage access is now main-thread; long ops are
  non-blocking + QTimer-polled. The scan was rewritten from a `QThread` worker
  into a **main-thread state machine** (move → poll → settle → lock-in read on a
  short thread → record). Verified on real hardware: connect, absolute move,
  jog all work. Also: non-blocking home, APT `is_in_motion` for `is_moving`,
  relative jogs rerouted through absolute `move_to` (APT relative move throws
  "Invalid error code"), and zero-distance moves are now silent no-ops.
- **Fixed "connect button stuck disabled"** — centralized panel enable/disable in
  `_refresh_enabled()` so the launch device-scan re-enables Connect afterward.
- **Fixed lock-in deadlock** — `read_buffered`'s snapshot fallback re-acquired the
  driver's lock; changed it to an `RLock`. Buffered capture now latches off after
  the first failure and uses snapshot averaging (no per-point warning spam).
- **Save format**: scan now stores/saves **position + X, Y, R, theta** (all lock-in
  channels captured in one buffered read), not just the single selected channel.
- **Delay zones**: added an **fs/mm unit toggle** with a live per-zone conversion
  readout ("step in fs"), widened spinboxes so 6-decimal mm values are visible,
  and raised the range cap from ±1e6 to **±1e7 fs** (10 ns) so a full-travel scan
  isn't clipped. Zones are now add/remove (arbitrary count) instead of a fixed 3.
- **Metadata panel** added (sample, pump power, pump/probe wavelength, chopper
  freq, temperature, notes), saved into every scan's npz + JSON sidecar.
- **Auto-save**: every scan is written to disk automatically on completion (was
  manual-button only).
- **Aesthetic overhaul** — new `style.py` dark theme (readable contrast, rounded
  panels, styled inputs/tabs/scrollbars), header banner, bigger fonts, wider
  columns; fixed clipped tab labels and clipped scan controls.
- **Stage discovery at launch** — the app scans the bus and lists connected
  stages in a dropdown (+ Rescan), matching the FTIR app's workflow.
- **Lock-in default address** corrected to serial 004418 (was 005254 → caused
  `VI_ERROR_RSRC_NFOUND`).

### Initial build (pre-2026-07-17)

- Built the app skeleton from the reference repo's lock-in path: `driver_lockin`,
  `lockin_widget`, `thorlabs_stage`, `stage_widget`, then the scan engine and the
  main window tying lock-in + two delay stages + scan together.
