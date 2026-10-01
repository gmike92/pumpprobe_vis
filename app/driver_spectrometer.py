"""
driver_spectrometer.py
======================
Hardware layer for Ocean Optics / Ocean Insight spectrometers.

Two hardware backends are supported, tried in this order:

1. **OceanDirect** (Ocean Insight's official SDK) — preferred. It talks through
   Ocean's own USB driver, so it coexists with OceanView and it is the only way
   to reach newer models such as the **OceanNR** (USB VID 0x0999), which
   python-seabreeze does not support.
2. **seabreeze** (`pyseabreeze`/`cseabreeze`) — works for older units (e.g. the
   Flame-S) *provided* the device is bound to a libusb-compatible driver. NOTE:
   installing OceanView rebinds those devices to "Ocean Optics ... (WinUSB)",
   after which pyseabreeze raises `USBError(13, Access denied)`.

Plus a **simulation mode** so the GUI is fully usable with nothing plugged in.

Mirrors the style of ``driver_lockin.py``: a singleton so every widget talks to
the same device handle, and a re-entrant lock so acquisition can run on a worker
thread while the GUI reads metadata. Unlike the Thorlabs APT stage, USB
spectrometer access is thread-tolerant (the stage is NOT — see stage_widget).
"""

import threading

import numpy as np

# --- Backend 1: OceanDirect SDK (supports both old and new Ocean devices) --- #
try:
    from oceandirect.OceanDirectAPI import OceanDirectAPI
    _OD_OK = True
    print("[Spec] OceanDirect SDK available")
except Exception as _e:
    OceanDirectAPI = None
    _OD_OK = False

# --- Backend 2: seabreeze --------------------------------------------------- #
# seabreeze.use() must be called BEFORE importing seabreeze.spectrometers.
_BACKEND = None
try:
    import seabreeze
    for _cand in ("pyseabreeze", "cseabreeze"):
        try:
            seabreeze.use(_cand)
            from seabreeze.spectrometers import Spectrometer, list_devices
            _BACKEND = _cand
            break
        except Exception:
            continue
    if _BACKEND is None:
        raise ImportError("no working seabreeze backend")
    _SEABREEZE_OK = True
    print(f"[Spec] seabreeze backend: {_BACKEND}")
except Exception as _e:      # pragma: no cover - only when seabreeze missing
    Spectrometer = None
    list_devices = None
    _SEABREEZE_OK = False
    print(f"[Spec] seabreeze not available: {_e}")


def _try(obj, *names, default=None):
    """Call the first existing zero-arg method from ``names`` on ``obj``.

    The OceanDirect Python API differs slightly between SDK releases, so every
    call here is probed defensively rather than assumed.
    """
    for n in names:
        fn = getattr(obj, n, None)
        if callable(fn):
            try:
                return fn()
            except Exception:
                continue
        elif fn is not None:
            return fn
    return default


class SpectrometerDriver:
    """Singleton driver for one Ocean Optics spectrometer."""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(SpectrometerDriver, cls).__new__(cls)
            cls._instance.spec = None
            cls._instance.is_connected = False
            cls._instance.simulated = False
            cls._instance.model = ""
            cls._instance.serial = ""
            cls._instance._lock = threading.RLock()
            cls._instance._integration_us = 10000
            cls._instance._wavelengths = None
            # which backend the open device uses: 'oceandirect'|'seabreeze'|'sim'
            cls._instance.backend = ""
            cls._instance._od_api = None      # OceanDirectAPI instance
            cls._instance._od_dev = None      # open OceanDirect device
            cls._instance._od_id = None
            cls._instance._od_scans_avg = 1   # hardware averaging currently set
            cls._instance._index = {}         # serial -> (backend, device_id)
            # simulation only: lets the map scan produce delay-dependent data
            cls._instance.sim_delay_fs = 0.0
        return cls._instance

    # ------------------------------------------------------------------ #
    # Discovery / connection
    # ------------------------------------------------------------------ #
    def _od_open_api(self):
        """Lazily create the OceanDirect API object (one per process)."""
        if self._od_api is None and _OD_OK:
            self._od_api = OceanDirectAPI()
        return self._od_api

    def list_devices(self):
        """Return [(serial, model), ...] across every available backend."""
        found = []
        self._index = {}

        # --- OceanDirect first (covers the newer models too) ---
        if _OD_OK:
            try:
                api = self._od_open_api()
                _try(api, "find_usb_devices", "find_devices")
                ids = _try(api, "get_device_ids", default=[]) or []
                for did in ids:
                    serial, model = str(did), "Ocean device"
                    try:
                        dev = api.open_device(did)
                        serial = str(_try(dev, "get_serial_number", default=did))
                        model = str(_try(dev, "get_model", "get_device_type",
                                         default="Ocean device"))
                        try:
                            api.close_device(did)
                        except Exception:
                            _try(dev, "close_device")
                    except Exception as e:
                        print(f"[Spec] OceanDirect probe of {did} failed: {e}")
                    found.append((serial, model))
                    self._index[serial] = ("oceandirect", did)
            except Exception as e:
                print(f"[Spec] OceanDirect scan failed: {e}")

        # --- then seabreeze, skipping serials OceanDirect already claimed ---
        if _SEABREEZE_OK:
            try:
                for d in list_devices():
                    try:
                        serial, model = str(d.serial_number), str(d.model)
                    except Exception:
                        serial, model = str(d), "?"
                    if serial in self._index:
                        continue
                    found.append((serial, model))
                    self._index[serial] = ("seabreeze", None)
            except Exception as e:
                print(f"[Spec] seabreeze scan failed: {e}")
        return found

    def connect(self, serial=None, simulate_if_absent=True):
        """Connect to the given serial (or the first device found)."""
        with self._lock:
            if self.is_connected:
                return True
            if not self._index:
                self.list_devices()
            if serial is None and self._index:
                serial = next(iter(self._index))
            backend = self._index.get(str(serial), (None, None))[0] if serial else None

            # --- OceanDirect ---
            if backend == "oceandirect" or (backend is None and _OD_OK and serial is None):
                if self._connect_oceandirect(serial):
                    return True

            # --- seabreeze ---
            if backend in (None, "seabreeze") and _SEABREEZE_OK:
                try:
                    if serial:
                        self.spec = Spectrometer.from_serial_number(str(serial))
                    else:
                        devs = list_devices()
                        if not devs:
                            raise RuntimeError("no spectrometer found")
                        self.spec = Spectrometer(devs[0])
                    self.model = str(self.spec.model)
                    self.serial = str(self.spec.serial_number)
                    self._wavelengths = np.asarray(self.spec.wavelengths(), float)
                    self.backend = "seabreeze"
                    self.set_integration_time_ms(self._integration_us / 1000.0)
                    self.is_connected = True
                    self.simulated = False
                    print(f"[Spec] connected (seabreeze): {self.model} "
                          f"({self.serial}), {len(self._wavelengths)} px")
                    return True
                except Exception as e:
                    print(f"[Spec] seabreeze connection failed: {e}")

            if simulate_if_absent:
                # Simulated device so the GUI/scan can be exercised offline
                self._wavelengths = np.linspace(400.0, 1000.0, 2048)
                self.model = "SIMULATED"
                self.serial = "SIM-0001"
                self.backend = "sim"
                self.is_connected = True
                self.simulated = True
                print("[Spec] no hardware — running in SIMULATION mode")
                return True
            return False

    def _connect_oceandirect(self, serial=None):
        """Open a device through the OceanDirect SDK."""
        try:
            api = self._od_open_api()
            if api is None:
                return False
            did = self._index.get(str(serial), (None, None))[1] if serial else None
            if did is None:
                _try(api, "find_usb_devices", "find_devices")
                ids = _try(api, "get_device_ids", default=[]) or []
                if not ids:
                    return False
                did = ids[0]
            dev = api.open_device(did)
            self._od_dev, self._od_id = dev, did
            self._od_scans_avg = -1     # unknown -> force a set on first use
            self.serial = str(_try(dev, "get_serial_number", default=did))
            self.model = str(_try(dev, "get_model", "get_device_type",
                                  default="Ocean device"))
            wl = _try(dev, "get_wavelengths", default=None)
            self._wavelengths = np.asarray(wl, float) if wl is not None else None
            if self._wavelengths is None or self._wavelengths.size == 0:
                raise RuntimeError("no wavelength calibration returned")
            self.backend = "oceandirect"
            self.set_integration_time_ms(self._integration_us / 1000.0)
            self.is_connected = True
            self.simulated = False
            print(f"[Spec] connected (OceanDirect): {self.model} "
                  f"({self.serial}), {len(self._wavelengths)} px")
            return True
        except Exception as e:
            print(f"[Spec] OceanDirect connection failed: {e}")
            self._od_dev = self._od_id = None
            return False

    def disconnect(self):
        with self._lock:
            if self.backend == "oceandirect" and self._od_dev is not None:
                try:
                    if self._od_api is not None and self._od_id is not None:
                        self._od_api.close_device(self._od_id)
                    else:
                        _try(self._od_dev, "close_device")
                except Exception:
                    pass
            elif self.spec is not None:
                try:
                    self.spec.close()
                except Exception:
                    pass
            self.spec = None
            self._od_dev = self._od_id = None
            self.is_connected = False
            self.simulated = False
            self.backend = ""
            print("[Spec] disconnected")
            return True

    # ------------------------------------------------------------------ #
    # Settings
    # ------------------------------------------------------------------ #
    def integration_limits_ms(self):
        """(min, max) integration time in ms."""
        with self._lock:
            if self.is_connected and self.backend == "oceandirect":
                lo = _try(self._od_dev, "get_integration_time_minimum",
                          "get_minimum_integration_time", default=1000)
                hi = _try(self._od_dev, "get_integration_time_maximum",
                          "get_maximum_integration_time", default=10_000_000)
                try:
                    return float(lo) / 1000.0, float(hi) / 1000.0
                except Exception:
                    pass
            elif self.is_connected and self.backend == "seabreeze":
                try:
                    lo, hi = self.spec.integration_time_micros_limits
                    return lo / 1000.0, hi / 1000.0
                except Exception:
                    pass
            return 1.0, 10000.0

    def set_integration_time_ms(self, ms):
        with self._lock:
            us = int(max(1.0, float(ms)) * 1000)
            self._integration_us = us
            if self.is_connected and not self.simulated:
                try:
                    if self.backend == "oceandirect":
                        self._od_dev.set_integration_time(us)
                    else:
                        self.spec.integration_time_micros(us)
                except Exception as e:
                    print(f"[Spec] set integration time failed: {e}")
            return self._integration_us / 1000.0

    def get_integration_time_ms(self):
        return self._integration_us / 1000.0

    def wavelengths(self):
        with self._lock:
            if self._wavelengths is None:
                return np.array([])
            return self._wavelengths.copy()

    # ------------------------------------------------------------------ #
    # Acquisition
    # ------------------------------------------------------------------ #
    def get_spectrum(self, averages=1, correct_dark=False, correct_nonlinearity=False):
        """Acquire a spectrum, averaged over ``averages`` scans.

        OceanDirect can average in hardware (``set_scans_to_average``), which is
        faster and lower-noise than summing N separate USB reads, so use it when
        available and fall back to software averaging otherwise.
        """
        with self._lock:
            if not self.is_connected:
                return np.array([])
            n = max(1, int(averages))

            if n > 1 and self.backend == "oceandirect" and self._od_dev is not None:
                try:
                    if self._od_scans_avg != n:
                        self._od_dev.set_scans_to_average(n)
                        self._od_scans_avg = n
                    return self._single(correct_dark, correct_nonlinearity)
                except Exception as e:
                    print(f"[Spec] hardware averaging unavailable ({e}); "
                          f"using software averaging")
                    self._od_scans_avg = -1     # don't retry every acquisition

            acc = None
            for _ in range(n):
                s = self._single(correct_dark, correct_nonlinearity)
                acc = s if acc is None else acc + s
            return acc / float(n)

    def _single(self, correct_dark=False, correct_nonlinearity=False):
        if self.simulated:
            return self._simulated_spectrum()
        try:
            if self.backend == "oceandirect":
                data = _try(self._od_dev, "get_formatted_spectrum",
                            "get_spectrum", default=None)
                if data is None:
                    raise RuntimeError("no spectrum returned")
                return np.asarray(data, dtype=float)
            return np.asarray(
                self.spec.intensities(correct_dark_counts=correct_dark,
                                      correct_nonlinearity=correct_nonlinearity),
                dtype=float)
        except Exception as e:
            print(f"[Spec] read failed: {e}")
            return np.zeros_like(self._wavelengths)

    def _simulated_spectrum(self):
        """A broadband probe with a delay-dependent transient bleach at 600 nm,
        so the pump-probe map has believable structure when testing offline."""
        wl = self._wavelengths
        # lamp-like envelope + dark offset + shot noise
        probe = 12000.0 * np.exp(-0.5 * ((wl - 700.0) / 180.0) ** 2) + 500.0
        t = float(self.sim_delay_fs)
        # transient: rises at t0, decays with ~800 fs constant
        amp = 0.0 if t < 0 else 0.06 * np.exp(-t / 800.0)
        feature = amp * np.exp(-0.5 * ((wl - 600.0) / 25.0) ** 2)
        signal = probe * (1.0 - feature)
        noise = np.random.normal(0.0, 8.0, size=wl.shape)
        return signal + noise


# ---------------------------------------------------------------------- #
# Analysis helper
# ---------------------------------------------------------------------- #
def delta_t_over_t(spectrum, reference, background=None, floor=1.0):
    """
    ΔT/T for a pump-probe spectrum.

        T_pumped   = spectrum  - background      (pump ON)
        T_probe    = reference - background      (pump OFF, probe reference)
        ΔT/T       = (T_pumped - T_probe) / T_probe
                   = (spectrum - reference) / (reference - background)

    ``background`` is the dark spectrum (no light). ``reference`` is the
    probe-only spectrum — deliberately a *different* measurement than the
    background. Pixels whose reference transmission is below ``floor`` counts
    are returned as 0 to avoid dividing by noise.
    """
    spectrum = np.asarray(spectrum, float)
    reference = np.asarray(reference, float)
    bg = np.zeros_like(spectrum) if background is None else np.asarray(background, float)
    t_probe = reference - bg
    num = spectrum - reference
    out = np.zeros_like(spectrum)
    valid = np.abs(t_probe) > float(floor)
    out[valid] = num[valid] / t_probe[valid]
    return out
