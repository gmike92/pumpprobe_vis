"""
driver_lockin.py
================
Hardware layer for the Stanford Research SR865A lock-in amplifier.

Based on the ``driver_lockin.py`` from
https://github.com/gmike92/Labview-pumprobepython
extended with:
  * buffered capture (mean + std error bars), matching the workflow in the
    lab's own SR865A.py
  * simple SNAP-style snapshot reads (X, Y, R, theta together)
  * getters/setters for time constant, sensitivity, frequency, harmonic

Uses QCoDeS (same dependency as the original repo). No GUI code here.
"""

import time
import threading
import numpy as np

try:
    from qcodes.instrument_drivers.stanford_research import SR865A
    from qcodes.instrument import Instrument
    _QCODES_OK = True
except Exception as _e:          # pragma: no cover - only when qcodes missing
    SR865A = None
    Instrument = None
    _QCODES_OK = False
    print(f"[LockIn] qcodes not available: {_e}")


# Channel codes used by the GUI combo box
CH_X, CH_Y, CH_R, CH_THETA = 0, 1, 2, 3
CHANNEL_NAMES = {CH_X: "X", CH_Y: "Y", CH_R: "R", CH_THETA: "theta"}


class LockInDriver:
    """
    Driver for one SR865A lock-in amplifier.

    Multiple lock-ins are supported: instances are keyed by ``dev_id`` (e.g.
    "A", "B"), so ``LockInDriver("A")`` and ``LockInDriver("B")`` are distinct
    handles that every widget for that id shares. Each connects to its own VISA
    resource and gets its own QCoDeS instrument name (``lockin_A`` ...), so two
    can run at once. ``LockInDriver()`` with no id maps to "A" (back-compat).
    """

    _instances = {}

    def __new__(cls, dev_id="A"):
        dev_id = str(dev_id)
        inst = cls._instances.get(dev_id)
        if inst is None:
            inst = super(LockInDriver, cls).__new__(cls)
            inst.dev_id = dev_id
            inst.name = f"lockin_{dev_id}"      # QCoDeS instrument name
            inst.lockin = None
            inst.is_connected = False
            inst.port = None
            # Re-entrant: read_buffered() may call read_snapshot() as a fallback
            # while already holding the lock — a plain Lock would deadlock.
            inst._lock = threading.RLock()
            inst._buffer_config = "X,Y,R,T"
            # Buffered capture is UNRELIABLE on this SR865A (serial 004418): it
            # returns malformed values and corrupts the device I/O so that the
            # following reads fail with VI_ERROR_INP_PROT_VIOL. So it's OFF by
            # default — reads use simple SNAP-style snapshots, which are robust.
            # Flip to True only on an instrument where buffered capture works.
            inst._buffer_ok = False
            cls._instances[dev_id] = inst
        return inst

    def __init__(self, dev_id="A"):
        # everything is set up in __new__ (registry); nothing to do here
        pass

    # ------------------------------------------------------------------ #
    # Connection
    # ------------------------------------------------------------------ #
    @staticmethod
    def _raw_clear(resource_str):
        """Open a raw pyvisa session and fully recover the SR865A before QCoDeS
        talks to it: stop any running buffer capture, VISA device-clear, flush
        pending output, *CLS. Fixes both INP_PROT_VIOL and the capture-stuck
        timeout on the QCoDeS init's first query."""
        try:
            from pyvisa import ResourceManager
            rm = ResourceManager()
            inst = rm.open_resource(resource_str)
            try:
                inst.timeout = 3000
                inst.read_termination = "\n"
                inst.write_termination = "\n"
                inst.clear()                       # VISA device clear (IN/OUT)
                for cmd in ("CAPTURESTOP", "*CLS"):  # stop a running capture
                    try:
                        inst.write(cmd)
                    except Exception:
                        pass
                inst.clear()
                try:
                    idn = inst.query("*IDN?")       # re-sync + confirm response
                    if "SR865" not in idn:
                        print(f"[LockIn] unexpected *IDN?: {idn!r}")
                except Exception:
                    pass
            finally:
                inst.close()
        except Exception as e:
            print(f"[LockIn] pre-connect device recovery skipped: {e}")

    def connect(self, resource_str="USB0::0xB506::0x2000::004418::INSTR"):
        """Connect to the SR865A over the given VISA resource string."""
        if not _QCODES_OK:
            print("[LockIn] Cannot connect: qcodes not installed.")
            return False

        with self._lock:
            if self.is_connected and self.port == resource_str:
                return True

            try:
                # If a QCoDeS instrument with our name already exists, close it
                # (it may be in a bad/locked state) before creating a new one.
                try:
                    old = Instrument.find_instrument(self.name)
                    print(f"[LockIn {self.dev_id}] Closing existing "
                          f"'{self.name}' instrument...")
                    if hasattr(old, "visa_handle"):
                        try:
                            old.visa_handle.close()
                        except Exception:
                            pass
                    old.close()
                    if self.name in Instrument._all_instruments:
                        del Instrument._all_instruments[self.name]
                except KeyError:
                    pass

                # Clear the device's I/O state with a raw VISA session BEFORE
                # QCoDeS talks to it. The SR865A can be left mid-transfer (e.g.
                # after a crash), and the QCoDeS constructor's first query
                # ('CAPTURERATEMAX?') then fails with VI_ERROR_INP_PROT_VIOL.
                # A device clear + *CLS resets it so construction succeeds.
                self._raw_clear(resource_str)

                print(f"[LockIn {self.dev_id}] Connecting via QCoDeS to {resource_str}...")
                self.lockin = SR865A(self.name, resource_str)
                self.port = resource_str
                self.is_connected = True

                # Robustness config
                if hasattr(self.lockin, "visa_handle"):
                    self.lockin.visa_handle.timeout = 20000
                    self.lockin.visa_handle.read_termination = "\n"
                    self.lockin.visa_handle.write_termination = "\n"
                    self.lockin.visa_handle.clear()

                try:
                    self.lockin.write("*CLS")
                except Exception:
                    pass

                print(f"[LockIn] Connected: {self.lockin.get_idn()}")
                return True

            except Exception as e:
                print(f"[LockIn] Connection failed: {e}")
                self.is_connected = False
                return False

    def disconnect(self):
        with self._lock:
            if self.lockin is not None:
                try:
                    self.lockin.close()
                except Exception:
                    pass
            self.lockin = None
            self.is_connected = False
            self.port = None

    @staticmethod
    def list_resources():
        """Return VISA resources that reply with an SR865A *IDN?."""
        found = []
        try:
            from pyvisa import ResourceManager
            rm = ResourceManager()
            for res in rm.list_resources():
                try:
                    inst = rm.open_resource(res)
                    if "SR865" in inst.query("*IDN?"):
                        found.append(res)
                    inst.close()
                except Exception:
                    pass
        except Exception as e:
            print(f"[LockIn] VISA scan failed: {e}")
        return found

    # ------------------------------------------------------------------ #
    # Parameter get / set
    # ------------------------------------------------------------------ #
    def get_time_constant(self):
        with self._lock:
            if not self.is_connected:
                return 0.1
            try:
                return float(self.lockin.time_constant())
            except Exception:
                return 0.1

    def set_time_constant(self, val):
        with self._lock:
            if not self.is_connected:
                return
            try:
                self.lockin.time_constant(val)
                print(f"[LockIn] Time constant -> {val} s")
            except Exception as e:
                print(f"[LockIn] Error setting TC: {e}")

    def get_sensitivity(self):
        with self._lock:
            if not self.is_connected:
                return None
            try:
                return float(self.lockin.sensitivity())
            except Exception:
                return None

    def set_sensitivity(self, val):
        with self._lock:
            if not self.is_connected:
                return
            try:
                self.lockin.sensitivity(val)
            except Exception as e:
                print(f"[LockIn] Error setting sensitivity: {e}")

    def get_frequency(self):
        with self._lock:
            if not self.is_connected:
                return None
            try:
                return float(self.lockin.frequency())
            except Exception:
                return None

    def set_harmonic(self, harm=1):
        with self._lock:
            if not self.is_connected:
                return
            try:
                self.lockin.harmonic(int(harm))
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # Reads
    # ------------------------------------------------------------------ #
    def read_value(self, channel_code=CH_R, samples=1):
        """Read a single channel (0=X,1=Y,2=R,3=theta), averaged over samples."""
        with self._lock:
            if not self.is_connected:
                return 0.0
            try:
                vals = []
                for _ in range(max(1, samples)):
                    if channel_code == CH_X:
                        vals.append(self.lockin.X())
                    elif channel_code == CH_Y:
                        vals.append(self.lockin.Y())
                    elif channel_code == CH_R:
                        vals.append(self.lockin.R())
                    elif channel_code == CH_THETA:
                        vals.append(self.lockin.P())
                    else:
                        vals.append(0.0)
                return float(np.mean(vals))
            except Exception as e:
                print(f"[LockIn] Read error: {e}")
                return 0.0

    def read_snapshot(self, samples=1):
        """
        Read X, Y, R, theta (averaged over ``samples`` immediate reads).

        Returns a dict of mean values; std is 0 for samples==1.
        Robust fallback when the data buffer is not usable.
        """
        with self._lock:
            out = {"X": 0.0, "Y": 0.0, "R": 0.0, "theta": 0.0,
                   "X_std": 0.0, "Y_std": 0.0, "R_std": 0.0, "theta_std": 0.0}
            if not self.is_connected:
                return out
            try:
                buf = {"X": [], "Y": [], "R": [], "theta": []}
                for _ in range(max(1, samples)):
                    buf["X"].append(self.lockin.X())
                    buf["Y"].append(self.lockin.Y())
                    buf["R"].append(self.lockin.R())
                    buf["theta"].append(self.lockin.P())
                for k, v in buf.items():
                    out[k] = float(np.mean(v))
                    out[k + "_std"] = float(np.std(v))
            except Exception as e:
                print(f"[LockIn] Snapshot error: {e}")
            return out

    def read_buffered(self, n_samples=64, max_rate=True):
        """
        Capture ``n_samples`` from the SR865A data buffer and return
        mean + std for X, Y, R, theta.

        This averages real hardware samples over a short window, giving lower
        noise and an error estimate. Falls back to read_snapshot() on failure.
        """
        with self._lock:
            out = {"X": 0.0, "Y": 0.0, "R": 0.0, "theta": 0.0,
                   "X_std": 0.0, "Y_std": 0.0, "R_std": 0.0, "theta_std": 0.0,
                   "n": 0}
            if not self.is_connected:
                return out

            # If buffered capture has failed before on this instrument, go
            # straight to the reliable snapshot average (no repeated warnings).
            if self._buffer_ok:
                try:
                    n = int(max(1, n_samples))
                    self.lockin.buffer.capture_config(self._buffer_config)  # 'X,Y,R,T'
                    if max_rate:
                        self.lockin.buffer.set_capture_rate_to_maximum()
                    data = self.lockin.buffer.capture_samples(n)  # dict of arrays
                    keymap = {"X": "X", "Y": "Y", "R": "R", "T": "theta"}
                    for src, dst in keymap.items():
                        if src in data:
                            arr = np.asarray(data[src], dtype=float)
                            if arr.size:
                                out[dst] = float(np.mean(arr))
                                out[dst + "_std"] = float(np.std(arr))
                                out["n"] = int(arr.size)
                    if out["n"] > 0:
                        return out
                    raise ValueError("empty capture")
                except Exception as e:
                    self._buffer_ok = False
                    print(f"[LockIn] Buffered capture unavailable ({e}); "
                          f"using snapshot averaging for the rest of the session.")

            # Reliable fallback: N immediate snapshots -> mean + std.
            # (Re-acquires the RLock; safe because it is re-entrant.)
            snap = self.read_snapshot(samples=max(4, min(64, int(n_samples))))
            snap["n"] = 0
            return snap

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def settle(self, seconds):
        """Blocking wait (used by the scan worker after a stage move)."""
        if seconds and seconds > 0:
            time.sleep(seconds)
