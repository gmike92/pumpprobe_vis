"""
stage_widget.py
===============
GUI panel to control ONE Thorlabs delay stage (connect / home / jog / go-to /
set-t0 / live position). Two of these are placed side by side in the main app
for the pump-delay and probe-delay stages.

IMPORTANT — threading: the Thorlabs APT DLL is *thread-affine* (it crashes hard
if enumerated on one thread and opened/polled on another). So EVERY stage call
here happens on the GUI (main) thread. To stay responsive during the only truly
long operations — homing and long moves — we issue the move *non-blocking* and
watch ``is_moving()`` with a QTimer instead of blocking. Quick calls (connect,
bus scan, position reads) run inline. A delay scan also drives the stage from
the main thread (see scan_widget), never from a worker thread.
"""

from pyqtgraph.Qt import QtCore, QtWidgets
from thorlabs_stage import ThorlabsDelayStage
import config_store


class StageControlWidget(QtWidgets.QGroupBox):

    # emitted (with the new t0 in mm) when this stage's zero is set or restored,
    # so the scan / spectrometer t0 fields can follow it
    zero_changed = QtCore.pyqtSignal(float)

    def __init__(self, title="Stage", serial="", double_pass=True):
        super().__init__(title)
        self.stage = ThorlabsDelayStage(name=title, serial=serial or None,
                                        double_pass=double_pass)
        self._moving = False         # a non-blocking move/home is in progress
        self._scan_active = False    # a delay scan owns this stage
        self._move_polls = 0

        v = QtWidgets.QVBoxLayout(self)
        v.setContentsMargins(12, 16, 12, 12)
        v.setSpacing(9)

        # --- Device selection (populated by scanning the bus at launch) ---
        h_dev = QtWidgets.QHBoxLayout()
        h_dev.addWidget(QtWidgets.QLabel("Device:"))
        self.combo_dev = QtWidgets.QComboBox()
        self.combo_dev.setEditable(True)   # allow typing a serial manually too
        self.combo_dev.setMinimumWidth(180)
        h_dev.addWidget(self.combo_dev, 1)
        self.btn_rescan = QtWidgets.QPushButton("Rescan")
        self.btn_rescan.setToolTip("Scan the bus for connected Thorlabs stages")
        self.btn_rescan.clicked.connect(self.scan_devices)
        h_dev.addWidget(self.btn_rescan)
        v.addLayout(h_dev)

        # --- Connection ---
        h_con = QtWidgets.QHBoxLayout()
        self.btn_connect = QtWidgets.QPushButton("Connect")
        self.btn_connect.clicked.connect(self.connect_stage)
        h_con.addWidget(self.btn_connect)
        self.btn_home = QtWidgets.QPushButton("Home")
        self.btn_home.clicked.connect(self.home_stage)
        self.btn_home.setEnabled(False)
        h_con.addWidget(self.btn_home)
        v.addLayout(h_con)

        self.lbl_status = QtWidgets.QLabel("Not connected")
        self.lbl_status.setStyleSheet("color: #ef5350; font-weight: 600;")
        v.addWidget(self.lbl_status)

        # --- Live position ---
        self.lbl_pos = QtWidgets.QLabel("Pos: -- mm   (-- fs)")
        self.lbl_pos.setStyleSheet(
            "font-weight: 700; font-size: 16px; color: #4aa3ff; padding: 4px 0;")
        v.addWidget(self.lbl_pos)

        # --- Absolute move ---
        h_abs = QtWidgets.QHBoxLayout()
        h_abs.addWidget(QtWidgets.QLabel("Go to (mm):"))
        self.spin_abs = QtWidgets.QDoubleSpinBox()
        self.spin_abs.setRange(-500.0, 500.0)
        self.spin_abs.setDecimals(4)
        self.spin_abs.setSingleStep(0.01)
        h_abs.addWidget(self.spin_abs)
        self.btn_go = QtWidgets.QPushButton("Go")
        self.btn_go.clicked.connect(self.go_abs)
        h_abs.addWidget(self.btn_go)
        v.addLayout(h_abs)

        # --- Relative jog (fs) ---
        h_jog = QtWidgets.QHBoxLayout()
        h_jog.addWidget(QtWidgets.QLabel("Step (fs):"))
        self.spin_step = QtWidgets.QDoubleSpinBox()
        self.spin_step.setRange(0.01, 1e7)
        self.spin_step.setValue(100.0)
        h_jog.addWidget(self.spin_step)
        self.btn_neg = QtWidgets.QPushButton("- Step")
        self.btn_neg.clicked.connect(lambda: self.jog(-1))
        h_jog.addWidget(self.btn_neg)
        self.btn_pos = QtWidgets.QPushButton("+ Step")
        self.btn_pos.clicked.connect(lambda: self.jog(+1))
        h_jog.addWidget(self.btn_pos)
        v.addLayout(h_jog)

        # --- t0 / zero ---
        h_zero = QtWidgets.QHBoxLayout()
        self.btn_setzero = QtWidgets.QPushButton("Set current as t0")
        self.btn_setzero.clicked.connect(self.set_zero)
        h_zero.addWidget(self.btn_setzero)
        self.btn_gozero = QtWidgets.QPushButton("Go to t0")
        self.btn_gozero.setToolTip("Move the stage to the saved t0 position")
        self.btn_gozero.clicked.connect(self.go_to_zero)
        h_zero.addWidget(self.btn_gozero)
        v.addLayout(h_zero)

        h_zlbl = QtWidgets.QHBoxLayout()
        self.lbl_zero = QtWidgets.QLabel("t0 = 0.0000 mm")
        h_zlbl.addWidget(self.lbl_zero)
        h_zlbl.addStretch()
        v.addLayout(h_zlbl)

        # --- Encoder scale (counts per mm) — depends on the physical stage ---
        h_cal = QtWidgets.QHBoxLayout()
        h_cal.addWidget(QtWidgets.QLabel("Stage:"))
        self.combo_cal = QtWidgets.QComboBox()
        # (label, counts/mm) presets from the Thorlabs / pylablib scale table
        self._cal_presets = [
            ("DDSM50 / DDSM100  (2000/mm)", 2000.0),
            ("DDS220 / DDS300 / DDS600  (20000/mm)", 20000.0),
            ("MTS/Z8, Z8xx  (34304/mm)", 34304.0),
            ("Z6xx  (24600/mm)", 24600.0),
            ("Custom…", None),
        ]
        for label, _v in self._cal_presets:
            self.combo_cal.addItem(label)
        self.combo_cal.currentIndexChanged.connect(self._on_cal_preset)
        h_cal.addWidget(self.combo_cal, 1)
        v.addLayout(h_cal)

        h_cal2 = QtWidgets.QHBoxLayout()
        h_cal2.addWidget(QtWidgets.QLabel("Counts/mm:"))
        self.spin_cpm = QtWidgets.QDoubleSpinBox()
        self.spin_cpm.setRange(1.0, 1e8)
        self.spin_cpm.setDecimals(2)
        self.spin_cpm.setGroupSeparatorShown(True)
        self.spin_cpm.setValue(float(self.stage.COUNTS_PER_MM))
        self.spin_cpm.setToolTip(
            "Encoder counts per mm for this stage. Wrong value = go-to moves the "
            "wrong distance. Saved per stage serial. KBD101+DDSM = 2000.")
        self.spin_cpm.valueChanged.connect(self._on_cpm_changed)
        h_cal2.addWidget(self.spin_cpm, 1)
        self.btn_autocal = QtWidgets.QPushButton("Auto")
        self.btn_autocal.setToolTip("Detect counts/mm from the controller model")
        self.btn_autocal.clicked.connect(lambda: self._autodetect_counts(force=True))
        h_cal2.addWidget(self.btn_autocal)
        v.addLayout(h_cal2)
        self.lbl_cal = QtWidgets.QLabel("")
        self.lbl_cal.setStyleSheet("color: #9aa0a6; font-size: 12px;")
        v.addWidget(self.lbl_cal)
        self._sync_cal_combo(self.spin_cpm.value())   # combo matches initial value

        # options
        h_opt = QtWidgets.QHBoxLayout()
        self.chk_double = QtWidgets.QCheckBox("Double pass (retro)")
        self.chk_double.setChecked(double_pass)
        self.chk_double.stateChanged.connect(
            lambda: setattr(self.stage, "double_pass",
                            self.chk_double.isChecked()))
        h_opt.addWidget(self.chk_double)
        v.addLayout(h_opt)

        self._set_controls_enabled(False)

        # live position timer
        self.timer = QtCore.QTimer()
        self.timer.setInterval(400)
        self.timer.timeout.connect(self.refresh_position)

        # poll timer for non-blocking moves/homing
        self._move_timer = QtCore.QTimer()
        self._move_timer.setInterval(80)
        self._move_timer.timeout.connect(self._poll_move)

        # Discover connected stages shortly after the window shows, so the bus
        # scan (brief) doesn't delay the window appearing. Runs on this (main)
        # thread — APT must not be enumerated from a worker thread.
        self._initial_serial = str(serial) if serial else None
        QtCore.QTimer.singleShot(150, lambda: self.scan_devices(self._initial_serial))

    # ------------------------------------------------------------------ #
    # Non-blocking move engine — issue the move, then poll is_moving()
    # ------------------------------------------------------------------ #
    def _start_move(self, issue_fn, status, on_done=None):
        """Issue a non-blocking move/home, then watch is_moving() via a timer."""
        if not self.stage.is_connected or self._moving or self._scan_active:
            return
        self._moving = True
        self._move_polls = 0
        self._move_done_cb = on_done
        self.timer.stop()                    # our poll timer drives updates now
        self._refresh_enabled()
        self.lbl_status.setText(status)
        self.lbl_status.setStyleSheet("color: #ffb74d; font-weight: 600;")
        try:
            issue_fn()                        # non-blocking hardware command
        except Exception as e:
            print(f"[{self.stage.name}] move command failed: {e}")
            self._finish_move("Move failed", ok=False)
            return
        self._move_timer.start()

    def _poll_move(self):
        self._move_polls += 1
        try:
            self.refresh_position()           # live position while moving
        except Exception:
            pass
        # grace period (~240 ms) so motion has actually started
        if self._move_polls < 3:
            return
        timed_out = self._move_polls > 900     # ~72 s hard cap (covers homing)
        if not self.stage.is_moving() or timed_out:
            self._finish_move(
                "Move timed out" if timed_out else
                f"At {self.stage.get_position():.4f} mm",
                ok=not timed_out)

    def _finish_move(self, status, ok=True):
        self._move_timer.stop()
        self._moving = False
        self.lbl_status.setText(status)
        self.lbl_status.setStyleSheet(
            "color: %s; font-weight: 600;" % ("#4caf82" if ok else "#ef5350"))
        self._refresh_enabled()
        try:
            self.refresh_position()
        except Exception:
            pass
        if self.stage.is_connected and not self._scan_active:
            self.timer.start()
        cb, self._move_done_cb = getattr(self, "_move_done_cb", None), None
        if cb:
            cb(ok)

    def _set_controls_enabled(self, on):
        for w in (self.btn_go, self.btn_neg, self.btn_pos,
                  self.spin_abs, self.spin_step, self.btn_setzero,
                  self.btn_gozero, self.btn_autocal):
            w.setEnabled(on)

    def _refresh_enabled(self):
        """Set every control's enabled state from the current situation."""
        scan = self._scan_active
        connected = self.stage.is_connected
        moving = self._moving
        # Connect toggles to Disconnect when connected, so the stage can be freed
        # and a different device selected.
        self.btn_connect.setText("Disconnect" if connected else "Connect")
        self.btn_connect.setEnabled(not scan and not moving)
        # Rescan / device pick only make sense when not connected (and idle)
        self.btn_rescan.setEnabled(not scan and not moving and not connected)
        self.combo_dev.setEnabled(not scan and not moving and not connected)
        # Motion controls: need a connection, no scan, and not already moving
        move_ok = connected and not scan and not moving
        self._set_controls_enabled(move_ok)
        self.btn_home.setEnabled(move_ok)

    def set_scan_active(self, active):
        """Called by the main window: a delay scan owns this stage — stop polling
        and lock manual controls so only the scan (on the main thread) drives it."""
        self._scan_active = bool(active)
        if active:
            self.timer.stop()
        self._refresh_enabled()
        if not active and self.stage.is_connected and not self._moving:
            self.timer.start()

    # ------------------------------------------------------------------ #
    def scan_devices(self, preselect=None):
        """Scan the bus for Thorlabs stages and fill the dropdown (main thread)."""
        if self._moving:
            return
        preselect = preselect or self.selected_serial()
        self.btn_rescan.setEnabled(False)
        self.lbl_status.setText("Scanning for stages...")
        self.lbl_status.setStyleSheet("color: #ffb74d; font-weight: 600;")
        QtWidgets.QApplication.processEvents()
        try:
            devices = ThorlabsDelayStage.list_devices()
        except Exception as e:
            devices = []
            print(f"[{self.stage.name}] device scan failed: {e}")

        self.combo_dev.blockSignals(True)
        self.combo_dev.clear()
        for sn, backend in devices:
            self.combo_dev.addItem(f"{sn}  ({backend})", userData=str(sn))
        self.combo_dev.blockSignals(False)
        if preselect:
            idx = self.combo_dev.findData(str(preselect))
            if idx >= 0:
                self.combo_dev.setCurrentIndex(idx)
            else:
                self.combo_dev.setEditText(str(preselect))
        if devices:
            self.lbl_status.setText(f"Found {len(devices)} stage(s) — select & Connect")
            self.lbl_status.setStyleSheet("color: #4aa3ff; font-weight: 600;")
        else:
            self.lbl_status.setText("No stages found (type a serial or Rescan)")
            self.lbl_status.setStyleSheet("color: #ef5350; font-weight: 600;")
        self._refresh_enabled()

    def selected_serial(self):
        """Serial from the dropdown. Items read 'SERIAL  (backend)'; a manually
        typed serial is just the number — so the first token works for both."""
        text = self.combo_dev.currentText().strip()
        return text.split()[0] if text else ""

    def connect_stage(self):
        """Connect the selected device, or disconnect if already connected."""
        if self._moving or self._scan_active:
            return
        if self.stage.is_connected:
            self.disconnect_stage()
            return
        serial = self.selected_serial()
        self.lbl_status.setText("Connecting...")
        self.lbl_status.setStyleSheet("color: #ffb74d; font-weight: 600;")
        QtWidgets.QApplication.processEvents()
        try:
            ok = self.stage.connect(serial or None)
        except Exception as e:
            ok = False
            print(f"[{self.stage.name}] connect error: {e}")
        if ok:
            self.lbl_status.setText(f"Connected ({self.stage.serial})")
            self.lbl_status.setStyleSheet("color: #4caf82; font-weight: 600;")
            self._load_saved_counts_per_mm()   # restore scale before positions
            self._load_saved_zero()            # restore this stage's saved t0
            self._refresh_enabled()
            self.refresh_position()
            self.timer.start()
        else:
            self.lbl_status.setText("Connection failed")
            self.lbl_status.setStyleSheet("color: #ef5350; font-weight: 600;")
            self._refresh_enabled()

    def disconnect_stage(self):
        """Release the current stage so a different device can be selected."""
        if self._scan_active:
            return
        self.timer.stop()
        self._move_timer.stop()
        self._moving = False
        try:
            self.stage.disconnect()
        except Exception as e:
            print(f"[{self.stage.name}] disconnect error: {e}")
        self.lbl_status.setText("Disconnected — select a device to connect")
        self.lbl_status.setStyleSheet("color: #9aa0a6; font-weight: 600;")
        self.lbl_pos.setText("Pos: -- mm   (-- fs)")
        self._refresh_enabled()

    def home_stage(self):
        # Non-blocking home + poll (homing can take ~60 s — never block the GUI)
        def done(ok):
            if ok:
                self.stage.is_homed = True
        self._start_move(lambda: self.stage.home(wait=False),
                         "Homing (may take ~60 s)...", on_done=done)

    def go_abs(self):
        target = self.spin_abs.value()
        self._start_move(lambda: self.stage.move_to(target, wait=False),
                         f"Moving to {target:.4f} mm...")

    def jog(self, direction):
        step_fs = self.spin_step.value() * direction
        self._start_move(lambda: self.stage.move_relative_fs(step_fs, wait=False),
                         f"Jogging {step_fs:+.0f} fs...")

    def go_to_zero(self):
        """Move the stage to its saved t0 (non-blocking + polled)."""
        z = self.stage.zero_position_mm
        self._start_move(lambda: self.stage.move_to(z, wait=False),
                         f"Moving to t0 ({z:.4f} mm)...")

    # -- encoder scale (counts per mm) ---------------------------------- #
    def _on_cal_preset(self, idx):
        if 0 <= idx < len(self._cal_presets):
            val = self._cal_presets[idx][1]
            if val is not None:                 # "Custom…" leaves the field alone
                self.spin_cpm.setValue(val)

    def _sync_cal_combo(self, value):
        """Point the preset combo at the matching entry (or Custom)."""
        self.combo_cal.blockSignals(True)
        match = next((i for i, (_l, v) in enumerate(self._cal_presets)
                      if v is not None and abs(v - value) < 1e-6), None)
        self.combo_cal.setCurrentIndex(match if match is not None
                                       else len(self._cal_presets) - 1)
        self.combo_cal.blockSignals(False)

    def _on_cpm_changed(self, value):
        """Apply counts/mm to the stage and persist it (per serial)."""
        self.stage.COUNTS_PER_MM = float(value)
        self._sync_cal_combo(value)
        if self.stage.is_connected and self.stage.serial:
            config_store.set_stage_counts_per_mm(self.stage.serial, value)
        if self.stage.is_connected:
            self.refresh_position()

    def _apply_counts_per_mm(self, cpm):
        """Set the field + stage without re-triggering a save."""
        self.stage.COUNTS_PER_MM = float(cpm)
        self.spin_cpm.blockSignals(True)
        self.spin_cpm.setValue(float(cpm))
        self.spin_cpm.blockSignals(False)
        self._sync_cal_combo(float(cpm))

    def _load_saved_counts_per_mm(self):
        """On connect: a saved per-serial value wins; otherwise auto-detect from
        the controller model."""
        if config_store.has_stage_counts_per_mm(self.stage.serial):
            cpm = config_store.get_stage_counts_per_mm(
                self.stage.serial, self.stage.COUNTS_PER_MM)
            self._apply_counts_per_mm(cpm)
            self.lbl_cal.setText(f"counts/mm: saved for {self.stage.serial}")
        else:
            self._autodetect_counts(force=False)

    def _autodetect_counts(self, force=False):
        """Detect counts/mm from the controller model. ``force`` re-runs it even
        if a value is already saved (and saves the detected value)."""
        if not self.stage.is_connected:
            return
        cpm, model = self.stage.detect_counts_per_mm()
        if cpm is not None:
            self._apply_counts_per_mm(cpm)
            self.lbl_cal.setText(f"counts/mm: auto — {model} → {cpm:g}")
            self.lbl_cal.setStyleSheet("color: #4caf82; font-size: 12px;")
            if force and self.stage.serial:
                config_store.set_stage_counts_per_mm(self.stage.serial, cpm)
        else:
            self.lbl_cal.setText(
                f"counts/mm: {model or 'unknown'} not recognised — set it manually")
            self.lbl_cal.setStyleSheet("color: #ffb74d; font-size: 12px;")
        self.refresh_position()

    def set_zero(self):
        if not self.stage.is_connected:
            return
        self.stage.zero_position_mm = self.stage.get_position()
        self.lbl_zero.setText(f"t0 = {self.stage.zero_position_mm:.4f} mm")
        # Persist so this t0 survives restarts (keyed by the stage serial).
        config_store.set_stage_zero(self.stage.serial, self.stage.zero_position_mm)
        self.zero_changed.emit(self.stage.zero_position_mm)
        self.refresh_position()

    def _load_saved_zero(self):
        """On connect, restore this stage's saved t0 (if any)."""
        if config_store.has_stage_zero(self.stage.serial):
            z = config_store.get_stage_zero(self.stage.serial,
                                            self.stage.zero_position_mm)
            self.stage.zero_position_mm = z
            self.lbl_zero.setText(f"t0 = {z:.4f} mm  (restored)")
            self.zero_changed.emit(z)
        else:
            self.lbl_zero.setText(f"t0 = {self.stage.zero_position_mm:.4f} mm")

    def refresh_position(self):
        if not self.stage.is_connected:
            return
        pos_mm = self.stage.get_position()
        pos_fs = self.stage.mm_to_fs(pos_mm - self.stage.zero_position_mm)
        self.spin_abs.blockSignals(True)
        if not self.spin_abs.hasFocus():
            self.spin_abs.setValue(pos_mm)
        self.spin_abs.blockSignals(False)
        self.lbl_pos.setText(f"Pos: {pos_mm:.4f} mm   ({pos_fs:+.1f} fs)")
