"""
thorlabs_stage.py
=================
Driver for a Thorlabs Kinesis / APT translation stage used as an optical
delay line.

Adapted from ``stage_delay.py`` of
https://github.com/gmike92/Labview-pumprobepython
but converted from a SINGLETON to a normal class so that TWO independent
stages (pump delay + probe delay) can be instantiated at the same time,
each addressed by its own serial number.

Connection is attempted in this order (first that works wins):
    1. thorlabs_apt  (APT.dll)          -- e.g. BBD/BSC brushless controllers
    2. pylablib      (Kinesis)          -- KDC101, K-Cube, etc.
    3. pythonnet      (.NET Kinesis CLI) -- fallback

Physics (retro-reflector / double pass):
    delay (fs) = 2 * distance (mm) / c      with c = 0.000299792458 mm/fs
"""

import os
import sys
import time

SPEED_OF_LIGHT_MM_FS = 0.000299792458   # mm per femtosecond

# The Thorlabs APT DLL (`thorlabs_apt`) SEGFAULTS the whole process on this setup
# — even a bare `apt.list_available_devices()` crashes with 0xC0000005, which a
# Python try/except cannot catch. pylablib/Kinesis, by contrast, enumerates and
# drives ALL the Thorlabs motor controllers (brushed/brushless/APT-protocol) via
# the FTDI layer. So APT is DISABLED by default: never imported, never called.
# Only flip this to True on a machine where thorlabs_apt is known to be stable.
_USE_APT = False


# Controller model -> typical encoder counts/mm for its usual delay-line stage.
# The controller reports its own model reliably; the physical stage often can't
# be read (e.g. KBD101), so this is a best-guess default the user can override.
# Values from the Thorlabs / pylablib scale table.
_CONTROLLER_COUNTS_PER_MM = {
    "KBD101": 2000.0,     # K-Cube brushless DC -> DDSM50 / DDSM100
    "KBD201": 2000.0,
    "BBD":    20000.0,    # benchtop brushless (BBD10x/20x/30x) -> DDS220/300/600
    "KDC101": 34304.0,    # K-Cube brushed DC -> MTS/Z8, Z8xx
    "TDC001": 34304.0,    # T-Cube brushed DC -> Z8xx
}


def _counts_for_model(model):
    """Best-guess counts/mm for a controller model string, or None if unknown."""
    if not model:
        return None
    m = str(model).upper().strip()
    if m in _CONTROLLER_COUNTS_PER_MM:
        return _CONTROLLER_COUNTS_PER_MM[m]
    for prefix, val in _CONTROLLER_COUNTS_PER_MM.items():
        if m.startswith(prefix):
            return val
    return None


class ThorlabsDelayStage:
    """One Thorlabs delay stage. Instantiate once per physical stage."""

    def __init__(self, name="stage", serial=None, double_pass=True,
                 counts_per_mm=20000):
        self.name = name
        self.serial = str(serial) if serial else None

        self.is_connected = False
        self.is_homed = False
        self._position_mm = 0.0

        self._stage = None
        self._stage_type = None          # 'apt' | 'kinesis' | 'pythonnet'

        # Optical-path options
        self.double_pass = double_pass   # retro-reflector -> factor 2
        self.zero_position_mm = 0.0      # user-defined t0 (mm) for this stage

        # Unit scaling for the kinesis "raw count" API
        self.COUNTS_PER_MM = counts_per_mm

    # ------------------------------------------------------------------ #
    # Discovery
    # ------------------------------------------------------------------ #
    @staticmethod
    def list_devices():
        """Return a list of (serial, backend) tuples for all found stages.

        Uses pylablib/Kinesis only — the APT DLL crashes the process (see
        _USE_APT). pylablib enumerates every Thorlabs motor controller.
        """
        found = []
        if _USE_APT:                       # off by default — APT segfaults
            try:
                import thorlabs_apt as apt
                for d in apt.list_available_devices():
                    found.append((str(d[1]), "apt"))
            except Exception:
                pass
        try:
            from pylablib.devices import Thorlabs
            for d in Thorlabs.list_kinesis_devices():
                sn = str(d[0])
                if sn not in [f[0] for f in found]:
                    found.append((sn, "kinesis"))
        except Exception as e:
            print(f"[stage] Kinesis enumeration failed: {e}")
        return found

    # ------------------------------------------------------------------ #
    # Connection
    # ------------------------------------------------------------------ #
    def connect(self, serial=None):
        if self.is_connected:
            print(f"[{self.name}] already connected ({self.serial})")
            return True
        if serial:
            self.serial = str(serial)

        # Kinesis (pylablib) is the primary backend. APT is skipped unless
        # explicitly re-enabled, because thorlabs_apt segfaults the process.
        if _USE_APT and self._connect_apt():
            return True
        if self._connect_kinesis():
            return True
        print(f"[{self.name}] Kinesis failed, trying pythonnet...")
        return self._connect_pythonnet()

    def _connect_apt(self):
        try:
            # Make Thorlabs APT DLLs discoverable if present in common places.
            apt_dll_paths = [
                r"C:\Users\mguizzardi\Desktop\Camera python\TWINS FILE\Twins\APT dll",
                r"C:\Program Files\Thorlabs\Kinesis",
                os.path.join(os.path.dirname(__file__), "APT dll"),
            ]
            for p in apt_dll_paths:
                if os.path.exists(p):
                    if p not in sys.path:
                        sys.path.insert(0, p)
                    if hasattr(os, "add_dll_directory"):
                        try:
                            os.add_dll_directory(p)
                        except Exception:
                            pass
                    os.environ["PATH"] = p + os.pathsep + os.environ["PATH"]

            import thorlabs_apt as apt
            devices = apt.list_available_devices()
            print(f"[{self.name}] APT devices: {devices}")
            if not devices:
                return False

            sn = int(self.serial) if self.serial else devices[0][1]
            self.serial = str(sn)
            self._stage = apt.Motor(sn)
            self._stage_type = "apt"
            self.is_connected = True
            print(f"[{self.name}] Connected via APT: {sn}")
            return True
        except ImportError:
            return False
        except Exception as e:
            print(f"[{self.name}] APT connection error: {e}")
            return False

    def _connect_kinesis(self):
        try:
            from pylablib.devices import Thorlabs
            devices = Thorlabs.list_kinesis_devices()
            print(f"[{self.name}] Kinesis devices: {devices}")
            if not devices and not self.serial:
                return False

            sn = self.serial if self.serial else devices[0][0]
            self.serial = str(sn)
            try:
                self._stage = Thorlabs.KinesisMotor(sn)
            except Exception:
                try:
                    self._stage = Thorlabs.KinesisMotor(sn, is_rack_system=False)
                except Exception:
                    self._stage = Thorlabs.KinesisMotor(sn, is_rack_system=True)
            self._stage_type = "kinesis"
            self.is_connected = True
            print(f"[{self.name}] Connected via Kinesis: {sn}")
            # Diagnostics: report what pylablib actually returns, so the raw
            # counts->mm scaling (currently the hardcoded COUNTS_PER_MM) can be
            # verified against the real device instead of guessed.
            self._log_kinesis_units()
            return True
        except ImportError:
            return False
        except Exception as e:
            print(f"[{self.name}] Kinesis connection error: {e}")
            return False

    def _log_kinesis_units(self):
        """Print pylablib's native scale/units + raw position for a kinesis
        motor. Purely diagnostic — helps decide the correct counts->mm scale."""
        st = self._stage
        for name in ("get_scale", "get_scale_units", "get_stage"):
            fn = getattr(st, name, None)
            if callable(fn):
                try:
                    print(f"[{self.name}] kinesis {name}(): {fn()}")
                except Exception as e:
                    print(f"[{self.name}] kinesis {name}() err: {e}")
        try:
            raw = st.get_position()
            print(f"[{self.name}] kinesis raw get_position(): {raw}  "
                  f"(/{self.COUNTS_PER_MM} -> {raw / self.COUNTS_PER_MM:.6f} mm as configured)")
        except Exception as e:
            print(f"[{self.name}] kinesis get_position() err: {e}")

    def _connect_pythonnet(self):
        try:
            import clr
            kinesis_path = r"C:\Program Files\Thorlabs\Kinesis"
            if kinesis_path not in sys.path:
                sys.path.append(kinesis_path)
            clr.AddReference("Thorlabs.MotionControl.DeviceManagerCLI")
            clr.AddReference("Thorlabs.MotionControl.GenericMotorCLI")
            clr.AddReference("Thorlabs.MotionControl.KCube.DCServoCLI")
            from Thorlabs.MotionControl.DeviceManagerCLI import DeviceManagerCLI
            from Thorlabs.MotionControl.KCube.DCServoCLI import KCubeDCServo

            DeviceManagerCLI.BuildDeviceList()
            devices = DeviceManagerCLI.GetDeviceList()
            if devices.Count == 0:
                return False
            sn = self.serial if self.serial else str(devices[0])
            self.serial = str(sn)
            self._stage = KCubeDCServo.CreateKCubeDCServo(sn)
            self._stage.Connect(sn)
            time.sleep(0.5)
            self._stage.StartPolling(250)
            time.sleep(0.5)
            self._stage.EnableDevice()
            self._stage_type = "pythonnet"
            self.is_connected = True
            print(f"[{self.name}] Connected via pythonnet: {sn}")
            return True
        except Exception as e:
            print(f"[{self.name}] pythonnet connection error: {e}")
            return False

    def disconnect(self):
        if not self.is_connected:
            return True
        try:
            if self._stage is not None:
                if hasattr(self._stage, "close"):
                    self._stage.close()
                elif hasattr(self._stage, "Disconnect"):
                    try:
                        self._stage.StopPolling()
                    except Exception:
                        pass
                    self._stage.Disconnect()
        except Exception as e:
            print(f"[{self.name}] disconnect error: {e}")
        self._stage = None
        self.is_connected = False
        self.is_homed = False
        print(f"[{self.name}] disconnected")
        return True

    # ------------------------------------------------------------------ #
    # Homing
    # ------------------------------------------------------------------ #
    def home(self, timeout_s=60.0, wait=True):
        """Home the stage. wait=False issues the command and returns immediately
        (poll is_moving() to detect completion) so the GUI thread never blocks."""
        if not self.is_connected:
            print(f"[{self.name}] not connected")
            return False
        try:
            print(f"[{self.name}] homing (wait={wait})...")
            if self._stage_type == "kinesis":
                try:
                    self._stage.home(sync=wait, timeout=timeout_s)
                except TypeError:
                    self._stage.home()
            elif self._stage_type == "apt":
                self._stage.move_home(blocking=wait)
            elif hasattr(self._stage, "Home"):
                self._stage.Home(int(timeout_s * 1000))  # pythonnet: blocking only
            if wait:
                self.is_homed = True
                self._position_mm = self.get_position()
                print(f"[{self.name}] homed")
            return True
        except Exception as e:
            print(f"[{self.name}] homing failed: {e}")
            return False

    # ------------------------------------------------------------------ #
    # Movement
    # ------------------------------------------------------------------ #
    def move_to(self, position_mm, wait=True, timeout_s=60.0):
        if not self.is_connected:
            print(f"[{self.name}] not connected")
            return False
        try:
            # A move to (essentially) the current position is a no-op — some APT
            # controllers reject a zero-distance move with "Invalid error code".
            # 50 nm is far below one fs of delay (~150 nm), so real points move.
            if abs(position_mm - self.get_position()) < 5e-5:
                return True
            if self._stage_type == "kinesis":
                self._stage.move_to(position_mm * self.COUNTS_PER_MM)
                if wait:
                    self.wait_for_stop(timeout_s)
            elif self._stage_type == "apt":
                self._stage.move_to(float(position_mm), blocking=wait)
            elif hasattr(self._stage, "MoveTo"):
                self._stage.MoveTo(self._mm_to_device_units(position_mm),
                                   int(timeout_s * 1000))
            else:
                print(f"[{self.name}] no move method")
                return False
            self._position_mm = self.get_position()
            return True
        except Exception as e:
            print(f"[{self.name}] move failed: {e}")
            return False

    def move_relative(self, distance_mm, wait=True, timeout_s=60.0):
        if not self.is_connected:
            return False
        try:
            if self._stage_type == "kinesis":
                self._stage.move_by(distance_mm * self.COUNTS_PER_MM)
                if wait:
                    self.wait_for_stop(timeout_s)
                self._position_mm = self.get_position()
                return True
            # APT/pythonnet: relative move_by is unreliable on some controllers
            # ("Invalid error code") — route through the absolute move instead.
            return self.move_to(self.get_position() + distance_mm, wait, timeout_s)
        except Exception as e:
            print(f"[{self.name}] relative move failed: {e}")
            return False

    def move_relative_fs(self, delta_fs, wait=True):
        return self.move_relative(self.fs_to_mm(delta_fs), wait)

    def wait_for_stop(self, timeout_s=60.0):
        if not self.is_connected:
            return True
        t0 = time.time()
        try:
            if hasattr(self._stage, "wait_move"):
                self._stage.wait_move(timeout=timeout_s)
                return True
            while self.is_moving():
                if time.time() - t0 > timeout_s:
                    print(f"[{self.name}] wait timeout")
                    return False
                time.sleep(0.02)
            return True
        except Exception:
            return True

    def is_moving(self):
        if not self.is_connected:
            return False
        try:
            st = self._stage
            if self._stage_type == "apt":
                # thorlabs_apt Motor exposes is_in_motion (no is_moving method)
                return bool(getattr(st, "is_in_motion", False))
            if hasattr(st, "is_moving"):
                return bool(st.is_moving())
            if hasattr(st, "IsMoving"):
                return bool(st.IsMoving)
        except Exception:
            pass
        return False

    # ------------------------------------------------------------------ #
    # Position
    # ------------------------------------------------------------------ #
    def get_position(self):
        if not self.is_connected:
            return self._position_mm
        try:
            if self._stage_type == "kinesis":
                pos = self._stage.get_position() / self.COUNTS_PER_MM
            elif self._stage_type == "apt":
                pos = self._stage.position
            elif hasattr(self._stage, "Position"):
                pos = self._device_units_to_mm(self._stage.Position)
            else:
                pos = self._position_mm
            self._position_mm = float(pos)
        except Exception as e:
            print(f"[{self.name}] position read failed: {e}")
        return self._position_mm

    def get_position_fs(self):
        return self.mm_to_fs(self.get_position() - self.zero_position_mm)

    # ------------------------------------------------------------------ #
    # Auto-detection of the encoder scale
    # ------------------------------------------------------------------ #
    def get_controller_model(self):
        """Controller model string (e.g. 'KBD101'), or '' if unavailable."""
        if not self.is_connected or self._stage is None:
            return ""
        try:
            if self._stage_type == "kinesis":
                info = self._stage.get_device_info()
                return str(getattr(info, "model_no", "") or "").strip()
        except Exception as e:
            print(f"[{self.name}] model query failed: {e}")
        return ""

    def detect_counts_per_mm(self):
        """Best-guess (counts_per_mm, model) from the controller model.
        Returns (None, model) when the model is unknown."""
        model = self.get_controller_model()
        return _counts_for_model(model), model

    # ------------------------------------------------------------------ #
    # Unit conversions
    # ------------------------------------------------------------------ #
    def mm_to_fs(self, distance_mm):
        factor = 2.0 if self.double_pass else 1.0
        return distance_mm * factor / SPEED_OF_LIGHT_MM_FS

    def fs_to_mm(self, time_fs):
        factor = 2.0 if self.double_pass else 1.0
        return time_fs * SPEED_OF_LIGHT_MM_FS / factor

    def _mm_to_device_units(self, mm, counts_per_mm=34304):
        return int(mm * counts_per_mm)

    def _device_units_to_mm(self, counts, counts_per_mm=34304):
        return counts / counts_per_mm


if __name__ == "__main__":
    print("Discoverable Thorlabs stages:", ThorlabsDelayStage.list_devices())
    s = ThorlabsDelayStage("test")
    print(f"1 mm = {s.mm_to_fs(1.0):.1f} fs   |   100 fs = {s.fs_to_mm(100):.6f} mm")
