# Pump-Probe VIS

A standalone GUI for **lock-in pump-probe delay scans** and **spectrometer-based
pump-probe ΔT/T maps**, controlling a Thorlabs delay stage, up to two Stanford
Research **SR865A** lock-in amplifiers, and an Ocean Optics / Ocean Insight
spectrometer.

Built with PyQt + [pyqtgraph](https://www.pyqtgraph.org/). The hardware layer is
a clean re-implementation of the lock-in path of
[gmike92/Labview-pumprobepython](https://github.com/gmike92/Labview-pumprobepython).

## Features

- **Two independent delay stages** (pump + probe), discovered via Kinesis.
  - Per-stage **t0 (zero)** and **encoder counts/mm** that are **auto-detected**
    from the controller model and **persist across sessions** (keyed by serial).
  - Non-blocking homing / moves (GUI stays responsive), *Go to t0*.
- **Up to two SR865A lock-ins**, connected and **acquired simultaneously**.
  - Live monitor, selectable channel (X / Y / R / θ), time-constant readout.
- **Lock-in pump-probe scan**
  - Multi-zone delay intervals in **fs** (relative to t0) or **mm** (absolute
    stage position), with a live fs↔mm readout.
  - Live plot (one trace per lock-in), **Pause/Resume**, **Stop**.
  - Every point saves **position + X, Y, R, θ** for each lock-in; auto-saved as
    CSV + NPZ + a JSON metadata sidecar.
- **Spectrometer pump-probe map** (separate window)
  - Live spectrum, adjustable **integration time** and **averaging**.
  - **Background** (dark) and **probe Reference** capture; dark auto-removed from
    the live view; adjustable **wavelength range (ROI)**.
  - Takes a spectrum at every delay → **ΔT/T map** (delay × wavelength) with a
    draggable wavelength cursor + dynamics cut. Switchable **ΔT/T | ΔT | T**.
  - `ΔT/T = (spectrum − reference) / (reference − background)`.

## Requirements

- **Python 3.10+** (developed on 3.12 / Anaconda).
- `pip install -r requirements.txt`
- A **VISA** backend (e.g. NI-VISA) for the SR865A lock-in.
- Thorlabs **Kinesis** installed (pylablib talks to it).
- For the spectrometer: python-seabreeze with the **pyseabreeze** backend
  (uses pyusb). Newer Ocean Insight models need the OceanDirect SDK.

## Run

```
python app/main.py
```

On Windows you can also double-click **`run_app.bat`** (edit the Python path
inside it if needed) — it keeps a console open so you can read the device logs.

## Layout

| File | Role |
|------|------|
| `app/main.py` | Entry point; Manual Control + Pump-Probe Scan tabs. |
| `app/style.py` | Dark theme. |
| `app/driver_lockin.py` | SR865A driver (QCoDeS), multi-device. |
| `app/lockin_widget.py` | Lock-in control panel. |
| `app/thorlabs_stage.py` | Delay-stage driver (Kinesis). |
| `app/stage_widget.py` | Stage control panel (connect/home/jog/t0/calibration). |
| `app/delay_interval_widget.py` | Multi-zone fs/mm scan-point generator. |
| `app/metadata_widget.py` | Experiment metadata entry. |
| `app/scan_widget.py` | Lock-in delay-scan engine + live plot + saving. |
| `app/driver_spectrometer.py` | Ocean Optics driver (seabreeze / OceanDirect). |
| `app/spectrometer_window.py` | Spectrometer live view + ΔT/T map. |
| `app/config_store.py` | Persistent per-stage settings (t0, counts/mm). |

## Notes

- Measurement data and the machine-specific `pumpprobe_config.json` are
  git-ignored.
- See `MEMORY.md` for a detailed change log and hardware/implementation notes.
