"""
scan_widget.py
==============
Lock-in pump-probe delay scan.

Drives ONE Thorlabs delay stage across a set of delay points while reading the
SR865A lock-in at each point, and plots signal (with error bars) vs delay in fs.
Cleaned/adapted from ``sub_lockin_pumpprobe_lw.py`` of
https://github.com/gmike92/Labview-pumprobepython to use this app's own
``ThorlabsDelayStage`` and ``LockInDriver`` (buffered capture -> error bars).

The scan runs as a MAIN-THREAD state machine (not a worker thread): the Thorlabs
APT DLL is thread-affine and crashes if driven from a background thread while it
was opened on the GUI thread. Per point:
    1. target_mm = stage.zero_position_mm ± stage.fs_to_mm(delay_fs)
    2. stage.move_to(target, wait=False)   -> poll is_moving() via QTimer
    3. settle (auto = a few time-constants, or manual) via singleShot
    4. read the lock-in on a short worker thread (VISA is thread-tolerant; the
       stage is NOT) -> X/Y/R/theta mean+std
    5. record point -> live plot + incremental CSV append -> next point
"""

import os
import csv
import json
from datetime import datetime

import numpy as np
from pyqtgraph.Qt import QtCore, QtWidgets
import pyqtgraph as pg

from driver_lockin import LockInDriver, CHANNEL_NAMES
from delay_interval_widget import DelayIntervalWidget
from metadata_widget import MetadataWidget
import config_store


# map GUI channel code -> key in the driver's buffered/snapshot dict
_CH_KEY = {0: "X", 1: "Y", 2: "R", 3: "theta"}


class _LockinReadTask(QtCore.QThread):
    """Read one or more lock-ins off the GUI thread (VISA is thread-tolerant,
    unlike the stage). Emits ``{dev_id: snapshot}`` when done. Multiple lock-ins
    are read back-to-back in this one worker."""
    got = QtCore.pyqtSignal(object)

    def __init__(self, lockins, n_avg):
        super().__init__()
        # lockins: list of (dev_id, driver)
        self.lockins = list(lockins)
        self.n_avg = int(n_avg)

    def run(self):
        out = {}
        for dev_id, drv in self.lockins:
            try:
                out[dev_id] = drv.read_buffered(n_samples=self.n_avg)
            except Exception as e:
                out[dev_id] = {"_error": str(e)}
        self.got.emit(out)


# distinct plot colours per lock-in id
_LOCKIN_COLORS = {"A": "#FF7043", "B": "#26C6DA", "C": "#AB47BC", "D": "#9CCC65"}


class PumpProbeScanWidget(QtWidgets.QWidget):
    """Delay-scan control + live plot. Needs a connected delay stage + lock-in."""

    # Let the main window pause the stage panel's polling while a scan runs, so
    # only the worker thread talks to the stage.
    scan_started = QtCore.pyqtSignal()
    scan_finished = QtCore.pyqtSignal()

    def __init__(self, delay_stage=None, lockin_widget=None,
                 lockin_widgets=None, parent=None):
        super().__init__(parent)
        self.delay_stage = delay_stage
        # one or more lock-in control panels (for channel + time constant + which
        # are connected). Accept a list, or a single widget for back-compat.
        if lockin_widgets is not None:
            self.lockin_widgets = list(lockin_widgets)
        elif lockin_widget is not None:
            self.lockin_widgets = [lockin_widget]
        else:
            self.lockin_widgets = []
        # lock-ins active in the current run: list of dicts (set at start_scan)
        self._scan_lockins = []
        self.curves = {}     # dev_id -> PlotDataItem
        self.errs = {}       # dev_id -> ErrorBarItem
        # scan state-machine
        self._scanning = False
        self._paused = False
        self._paused_idle = False   # True only while halted between points
        self._scan_index = 0
        self._scan_move_polls = 0
        self._reader = None
        self._reset_data()
        self._setup_ui()

        # timer that polls the stage during a scan move (main thread)
        self._scan_move_timer = QtCore.QTimer()
        self._scan_move_timer.setInterval(80)
        self._scan_move_timer.timeout.connect(self._scan_poll_move)

        self.set_delay_stage(delay_stage)

    def set_delay_stage(self, stage):
        """Point the scan (and the mm/fs display) at a specific delay stage."""
        self.delay_stage = stage
        if stage is not None:
            # mm of travel per fs of delay for THIS stage (double-pass aware)
            self.interval.set_mm_per_fs(stage.fs_to_mm(1.0))
            # adopt this stage's saved/current t0 (if connected)
            if getattr(stage, "is_connected", False):
                self.spin_zero.setValue(stage.zero_position_mm)

    def _reset_data(self):
        self.d_delay_fs = []
        self.d_target_mm = []
        self.d_actual_mm = []      # actual stage position (mm)
        # per-lock-in channel data: dev_id -> {"X":[], "Y":[], ...}
        self.d_lockin = {}
        self._csv_path = None
        self._npz_path = None
        self._json_path = None
        self._run_base = None
        self._scan_points = []
        self._scan_plan = []

    def _connected_lockins(self):
        """Lock-in panels that are currently connected."""
        return [w for w in self.lockin_widgets if w.lockin.is_connected]

    # ------------------------------------------------------------------ #
    def _setup_ui(self):
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        # ---- Left: controls (scrollable) ----
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setMinimumWidth(480)
        scroll.setMaximumWidth(560)
        ctrl = QtWidgets.QWidget()
        cl = QtWidgets.QVBoxLayout(ctrl)
        cl.setContentsMargins(2, 2, 8, 2)
        cl.setSpacing(12)

        # Metadata (sample, pump power, wavelengths, notes, ...)
        self.meta = MetadataWidget()
        cl.addWidget(self.meta)

        # Intervals
        self.interval = DelayIntervalWidget()
        cl.addWidget(self.interval)

        # Config
        gb = QtWidgets.QGroupBox("Scan Config")
        gcl = QtWidgets.QVBoxLayout(gb)

        self.chk_probe = QtWidgets.QCheckBox("Probe on stage (invert axis)")
        self.chk_probe.setToolTip(
            "Unchecked: stage carries the pump, +delay -> +mm.\n"
            "Checked: stage carries the probe, +delay -> -mm.")
        gcl.addWidget(self.chk_probe)

        h_zero = QtWidgets.QHBoxLayout()
        h_zero.addWidget(QtWidgets.QLabel("Zero t0 (mm):"))
        self.spin_zero = QtWidgets.QDoubleSpinBox()
        self.spin_zero.setRange(-500.0, 500.0)
        self.spin_zero.setDecimals(4)
        self.spin_zero.setToolTip(
            "Stage position that corresponds to 0 fs delay. "
            "Delay points are taken relative to this.")
        h_zero.addWidget(self.spin_zero)
        self.btn_use_current = QtWidgets.QPushButton("Set = current")
        self.btn_use_current.setToolTip("Capture the stage's current position as zero (t0)")
        self.btn_use_current.clicked.connect(self._use_current_zero)
        h_zero.addWidget(self.btn_use_current)
        gcl.addLayout(h_zero)

        # Live readout of where the stage is relative to zero
        self.lbl_live_delay = QtWidgets.QLabel("Stage: --  |  delay: -- fs")
        self.lbl_live_delay.setStyleSheet(
            "color: #4aa3ff; font-weight: 600; font-size: 15px; padding: 2px 0;")
        gcl.addWidget(self.lbl_live_delay)

        h_avg = QtWidgets.QHBoxLayout()
        h_avg.addWidget(QtWidgets.QLabel("Samples/point:"))
        self.spin_navg = QtWidgets.QSpinBox()
        self.spin_navg.setRange(1, 4096)
        self.spin_navg.setValue(64)
        h_avg.addWidget(self.spin_navg)
        gcl.addLayout(h_avg)

        self.chk_auto_wait = QtWidgets.QCheckBox("Auto settle (3 x time-const)")
        self.chk_auto_wait.setChecked(True)
        self.chk_auto_wait.stateChanged.connect(self._update_wait_label)
        gcl.addWidget(self.chk_auto_wait)

        h_wait = QtWidgets.QHBoxLayout()
        h_wait.addWidget(QtWidgets.QLabel("Manual settle (s):"))
        self.spin_wait = QtWidgets.QDoubleSpinBox()
        self.spin_wait.setRange(0.0, 60.0)
        self.spin_wait.setDecimals(3)
        self.spin_wait.setValue(0.1)
        self.spin_wait.valueChanged.connect(self._update_wait_label)
        h_wait.addWidget(self.spin_wait)
        gcl.addLayout(h_wait)

        self.lbl_wait = QtWidgets.QLabel("Settle: -")
        gcl.addWidget(self.lbl_wait)

        cl.addWidget(gb)

        # Actions
        g_run = QtWidgets.QGroupBox("Run")
        grl = QtWidgets.QVBoxLayout(g_run)

        self.btn_run = QtWidgets.QPushButton("Run Scan")
        self.btn_run.setStyleSheet(
            "QPushButton { background-color: #4caf82; color: #10151b; font-weight: 700; "
            "font-size: 15px; padding: 11px; border: none; border-radius: 6px; }"
            "QPushButton:hover { background-color: #5cc093; }"
            "QPushButton:disabled { background-color: #2f4a3e; color: #789; }")
        self.btn_run.clicked.connect(self.start_scan)
        grl.addWidget(self.btn_run)

        h_ps = QtWidgets.QHBoxLayout()
        self.btn_pause = QtWidgets.QPushButton("Pause")
        self.btn_pause.setToolTip(
            "Pause the scan at the current point (finishes the point in progress). "
            "Press again to resume where it left off.")
        self.btn_pause.setStyleSheet(
            "QPushButton { background-color: #ffb74d; color: #10151b; font-weight: 700; "
            "font-size: 15px; padding: 11px; border: none; border-radius: 6px; }"
            "QPushButton:hover { background-color: #ffc46b; }"
            "QPushButton:disabled { background-color: #4a412e; color: #987; }")
        self.btn_pause.clicked.connect(self.toggle_pause)
        self.btn_pause.setEnabled(False)
        h_ps.addWidget(self.btn_pause)

        self.btn_stop = QtWidgets.QPushButton("Stop")
        self.btn_stop.setStyleSheet(
            "QPushButton { background-color: #ef5350; color: #fff; font-weight: 700; "
            "font-size: 15px; padding: 11px; border: none; border-radius: 6px; }"
            "QPushButton:hover { background-color: #f4655f; }"
            "QPushButton:disabled { background-color: #4a2f2e; color: #987; }")
        self.btn_stop.clicked.connect(self.stop_scan)
        self.btn_stop.setEnabled(False)
        h_ps.addWidget(self.btn_stop)
        grl.addLayout(h_ps)

        self.progress = QtWidgets.QProgressBar()
        grl.addWidget(self.progress)

        self.lbl_status = QtWidgets.QLabel("Status: Ready")
        self.lbl_status.setStyleSheet("color: #4caf82; font-weight: 600;")
        self.lbl_status.setWordWrap(True)
        grl.addWidget(self.lbl_status)

        self.btn_save = QtWidgets.QPushButton("Save a copy as...")
        self.btn_save.setToolTip(
            "Each scan is auto-saved on completion. Use this to save an extra "
            "copy to another location.")
        self.btn_save.clicked.connect(self.save_data)
        self.btn_save.setEnabled(False)
        grl.addWidget(self.btn_save)

        cl.addWidget(g_run)
        cl.addStretch()
        scroll.setWidget(ctrl)
        layout.addWidget(scroll)

        # ---- Right: plot ----
        self.plot = pg.PlotWidget(title="Pump-Probe Trace (Lock-In)")
        self.plot.setLabel("bottom", "Delay", "fs")
        self.plot.setLabel("left", "Signal", "V")
        # fs is already the right magnitude — don't render 3000 fs as "3 kfs"
        self.plot.getAxis("bottom").enableAutoSIPrefix(False)
        self.plot.showGrid(x=True, y=True, alpha=0.3)
        self.plot.addLegend(offset=(-10, 10))
        # one curve + error bars per lock-in are created at scan start
        layout.addWidget(self.plot, stretch=1)

        self._update_wait_label()

        # Live stage/delay readout timer
        self._live_timer = QtCore.QTimer()
        self._live_timer.setInterval(400)
        self._live_timer.timeout.connect(self._update_live_delay)
        self._live_timer.start()

    def _update_live_delay(self):
        st = self.delay_stage
        if st is None or not st.is_connected:
            self.lbl_live_delay.setText("Stage: --  |  delay: -- fs")
            return
        pos = st.get_position()
        diff_mm = pos - self.spin_zero.value()
        delay_fs = st.mm_to_fs(diff_mm)
        if self.chk_probe.isChecked():
            delay_fs = -delay_fs
        self.lbl_live_delay.setText(
            f"Stage: {pos:.4f} mm  |  delay: {delay_fs:+.0f} fs")

    # ------------------------------------------------------------------ #
    def _use_current_zero(self):
        if self.delay_stage and self.delay_stage.is_connected:
            pos = self.delay_stage.get_position()
            self.spin_zero.setValue(pos)
            self.delay_stage.zero_position_mm = pos
            # persist per-stage so it survives restarts
            config_store.set_stage_zero(self.delay_stage.serial, pos)

    def set_zero_mm(self, value):
        """Reflect an externally-set t0 (e.g. restored/saved from a stage panel)."""
        self.spin_zero.setValue(float(value))

    def _max_time_constant(self):
        """Longest time constant across the connected lock-ins (0 if none)."""
        tcs = [w.lockin.get_time_constant() for w in self._connected_lockins()]
        return max(tcs) if tcs else 0.0

    def _get_wait_time(self):
        if self.chk_auto_wait.isChecked():
            tc = self._max_time_constant()
            return 3.0 * tc if tc > 0 else 0.1
        return self.spin_wait.value()

    def _update_wait_label(self):
        auto = self.chk_auto_wait.isChecked()
        self.spin_wait.setEnabled(not auto)
        if auto:
            tc = self._max_time_constant()
            if tc > 0:
                self.lbl_wait.setText(
                    f"Settle: {3.0 * tc:.3f} s (3x max TC, TC={tc:.3f}s)")
            else:
                self.lbl_wait.setText("Settle: auto (connect lock-in)")
        else:
            self.lbl_wait.setText(f"Settle: {self.spin_wait.value():.3f} s (manual)")

    # ------------------------------------------------------------------ #
    def start_scan(self):
        if self.delay_stage is None or not self.delay_stage.is_connected:
            self.lbl_status.setText("Status: Connect the delay stage first!")
            return
        connected = self._connected_lockins()
        if not connected:
            self.lbl_status.setText("Status: Connect at least one lock-in first!")
            return

        # Snapshot the lock-ins driving THIS run (id, driver, channel, colour).
        self._scan_lockins = [
            {"id": w.dev_id, "widget": w, "drv": w.lockin,
             "chan": w.get_channel_code(),
             "chan_name": CHANNEL_NAMES.get(w.get_channel_code(), "R"),
             "color": _LOCKIN_COLORS.get(w.dev_id, "#FF7043")}
            for w in connected]

        # Sync the stage's t0 to the spinbox, then build the scan plan. In mm
        # mode the interval values are ABSOLUTE stage positions; in fs mode they
        # are delays relative to t0. get_scan_plan() resolves both to concrete
        # (target_mm, delay_fs) pairs.
        self.delay_stage.zero_position_mm = self.spin_zero.value()
        plan = self.interval.get_scan_plan(
            self.delay_stage, self.spin_zero.value(),
            invert_stage=self.chk_probe.isChecked())
        if len(plan) == 0:
            self.lbl_status.setText("Status: No scan points — check intervals.")
            return
        points = np.array([d for (_t, d) in plan])   # delay axis (fs)

        self._reset_data()
        # per-lock-in storage
        self.d_lockin = {L["id"]: {k: [] for k in
                                   ("X", "Y", "R", "theta", "X_std", "Y_std", "R_std")}
                         for L in self._scan_lockins}
        self._scan_plan = plan
        self._scan_points = points
        self._scan_start_time = datetime.now()

        # (re)build one curve + error-bar item per lock-in in this run
        self._rebuild_curves()

        # Open incremental CSV + write the metadata sidecar up front
        # (so conditions are captured even if the scan crashes mid-way)
        self._open_csv()
        self._write_metadata_json(completed=False)

        self.btn_run.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.btn_pause.setEnabled(True)
        self.btn_pause.setText("Pause")
        self.btn_save.setEnabled(False)
        self.progress.setMaximum(len(points))
        self.progress.setValue(0)
        names = ", ".join(f"{L['id']}:{L['chan_name']}" for L in self._scan_lockins)
        self.lbl_status.setText(
            f"Status: scanning {len(points)} points with lock-in(s) {names}")
        self.plot.setLabel("left", "Signal", "V")

        # Configure the state machine
        self._scan_index = 0
        self._scanning = True
        self._paused = False
        self._scan_sign = -1.0 if self.chk_probe.isChecked() else 1.0
        self._scan_zero_mm = self.delay_stage.zero_position_mm
        self._scan_settle_s = self._get_wait_time()
        self._scan_navg = self.spin_navg.value()

        # Take the stage: stop our own polling and the stage panel's polling.
        self._live_timer.stop()
        self.scan_started.emit()

        QtCore.QTimer.singleShot(0, self._scan_move)

    def stop_scan(self):
        if self._scanning:
            self._scanning = False
            self._paused = False
            self.lbl_status.setText("Status: stopping...")
            self._scan_move_timer.stop()
            # let any in-flight lock-in read finish, then finalize
            if self._reader is not None and self._reader.isRunning():
                return
            self._scan_finish(completed=False)

    def toggle_pause(self):
        """Pause the scan at the current point, or resume from where it stopped.
        Pausing takes effect at the next point boundary — the point in progress
        (move + settle + read) always completes so no point is left half-done."""
        if not self._scanning:
            return
        self._paused = not self._paused
        if self._paused:
            self.btn_pause.setText("Resume")
            self.lbl_status.setText(
                f"Status: pausing after point {self._scan_index + 1}...")
        else:
            self.btn_pause.setText("Pause")
            self.lbl_status.setText("Status: resuming...")
            # Only kick off the next point if we're actually halted between
            # points; if a point is still in flight, its record step continues.
            if self._paused_idle:
                self._paused_idle = False
                QtCore.QTimer.singleShot(0, self._scan_move)

    # -- state machine ------------------------------------------------------ #
    def _scan_move(self):
        """Step 1: move the stage (non-blocking) to the next point."""
        if not self._scanning:
            return
        self._paused_idle = False              # a point cycle is now active
        if self._scan_index >= len(self._scan_plan):
            self._scan_finish(completed=True)
            return
        target_mm, delay_fs = self._scan_plan[self._scan_index]
        self._scan_target_mm = target_mm
        self.lbl_status.setText(
            f"Status: point {self._scan_index + 1}/{len(self._scan_plan)} — "
            f"{delay_fs:+.0f} fs  →  {target_mm:.4f} mm")
        try:
            self.delay_stage.move_to(target_mm, wait=False)
        except Exception as e:
            print(f"[Scan] move failed: {e}")
        self._scan_move_polls = 0
        self._scan_move_timer.start()

    def _scan_poll_move(self):
        """Step 2: poll until the stage stops, then settle + read."""
        if not self._scanning:
            self._scan_move_timer.stop()
            return
        self._scan_move_polls += 1
        try:
            pos = self.delay_stage.get_position()
            self.lbl_live_delay.setText(f"Stage: {pos:.4f} mm  (moving)")
        except Exception:
            pass
        if self._scan_move_polls < 3:
            return  # grace so motion starts
        timed_out = self._scan_move_polls > 900   # ~72 s
        if not self.delay_stage.is_moving() or timed_out:
            self._scan_move_timer.stop()
            settle_ms = int(max(0.0, self._scan_settle_s) * 1000)
            QtCore.QTimer.singleShot(settle_ms, self._scan_read)

    def _rebuild_curves(self):
        """Create one plot curve + error-bar item per lock-in in the run."""
        for c in self.curves.values():
            self.plot.removeItem(c)
        for e in self.errs.values():
            self.plot.removeItem(e)
        # drop stale legend entries
        try:
            self.plot.plotItem.legend.clear()
        except Exception:
            pass
        self.curves, self.errs = {}, {}
        multi = len(self._scan_lockins) > 1
        for L in self._scan_lockins:
            color = L["color"]
            label = (f"Lock-in {L['id']} ({L['chan_name']})" if multi
                     else f"Signal ({L['chan_name']})")
            self.errs[L["id"]] = pg.ErrorBarItem(pen=pg.mkPen(color, width=1))
            self.plot.addItem(self.errs[L["id"]])
            self.curves[L["id"]] = self.plot.plot(
                pen=pg.mkPen(color, width=2), symbol="o", symbolSize=5,
                symbolBrush=color, name=label)

    def _scan_read(self):
        """Step 3: read the lock-in(s) on a short worker thread (VISA is safe)."""
        if not self._scanning:
            return
        self._reader = _LockinReadTask(
            [(L["id"], L["drv"]) for L in self._scan_lockins], self._scan_navg)
        self._reader.got.connect(self._scan_record)
        self._reader.start()

    def _scan_record(self, snaps):
        """Step 4: store the point (all lock-ins), update plot + CSV, advance.
        ``snaps`` is {dev_id: snapshot-dict}."""
        # even if the user pressed Stop during the read, record this last point
        delay_fs = self._scan_plan[self._scan_index][1]
        try:
            actual_mm = self.delay_stage.get_position()
        except Exception:
            actual_mm = self._scan_target_mm
        self.d_delay_fs.append(delay_fs)
        self.d_target_mm.append(self._scan_target_mm)
        self.d_actual_mm.append(actual_mm)

        row = [f"{delay_fs:.3f}", f"{actual_mm:.5f}"]
        x = np.array(self.d_delay_fs)
        for L in self._scan_lockins:
            snap = snaps.get(L["id"], {}) or {}
            d = self.d_lockin[L["id"]]
            for k in ("X", "Y", "R", "theta"):
                d[k].append(float(snap.get(k, 0.0)))
            for k in ("X_std", "Y_std", "R_std"):
                d[k].append(float(snap.get(k, 0.0)))
            row += [f"{d['X'][-1]:.8e}", f"{d['Y'][-1]:.8e}",
                    f"{d['R'][-1]:.8e}", f"{d['theta'][-1]:.4f}"]

            # live plot of this lock-in's selected channel + error bars
            chan = L["chan_name"]
            y = np.array(d[chan])
            std = (np.array(d[chan + "_std"]) if chan != "theta"
                   else np.zeros(len(y)))
            self.curves[L["id"]].setData(x, y)
            self.errs[L["id"]].setData(x=x, y=y, height=2.0 * std, beam=0.0)

        self.lbl_live_delay.setText(
            f"Stage: {actual_mm:.4f} mm  |  delay: {delay_fs:+.0f} fs")

        if self._csv_path:
            with open(self._csv_path, "a", newline="") as f:
                csv.writer(f).writerow(row)

        self._scan_index += 1
        self.progress.setValue(self._scan_index)

        if not self._scanning:                 # Stop was pressed during the read
            self._scan_finish(completed=False)
        elif self._paused:                     # Pause pressed — hold at this point
            self._paused_idle = True
            self.lbl_status.setText(
                f"Status: PAUSED at point {self._scan_index}/"
                f"{len(self._scan_points)} — press Resume to continue")
        else:
            QtCore.QTimer.singleShot(0, self._scan_move)

    def _scan_finish(self, completed):
        self._scanning = False
        self._paused = False
        self._paused_idle = False
        self._scan_move_timer.stop()
        self.btn_run.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.btn_pause.setEnabled(False)
        self.btn_pause.setText("Pause")
        self.btn_save.setEnabled(len(self.d_delay_fs) > 0)

        # Give the stage back: resume our polling and the stage panel's.
        self.scan_finished.emit()
        if self.delay_stage is not None and self.delay_stage.is_connected:
            self._live_timer.start()

        saved = ""
        if self.d_delay_fs:
            self._write_metadata_json(completed=completed)
            self._auto_save_npz()
            saved = f" — saved {os.path.basename(self._npz_path)}"
        word = "complete" if completed else "aborted (partial saved)"
        self.lbl_status.setText(f"Status: scan {word}{saved}")

    # ------------------------------------------------------------------ #
    def _default_dir(self):
        base = r"D:\pumpprobedata"
        now = datetime.now()
        target = os.path.join(base, now.strftime("%Y"), now.strftime("%m"),
                              now.strftime("%d"))
        try:
            os.makedirs(target, exist_ok=True)
            return target
        except Exception:
            return os.getcwd()

    def _base_name(self):
        if not getattr(self, "_run_base", None):
            name = self.meta.sample_name()
            name = "".join(c for c in name if c.isalnum() or c in " -_").strip()
            self._run_base = f"{name or 'sample'}_lockin_{datetime.now().strftime('%H%M%S')}"
        return self._run_base

    def _open_csv(self):
        self._run_base = None   # fresh base name per run
        base = self._base_name()
        self._csv_path = os.path.join(self._default_dir(), base + ".csv")
        self._npz_path = os.path.join(self._default_dir(), base + ".npz")
        self._json_path = os.path.join(self._default_dir(), base + ".json")
        header = ["delay_fs", "position_mm"]
        for L in self._scan_lockins:
            i = L["id"]
            header += [f"X_{i}", f"Y_{i}", f"R_{i}", f"theta_{i}"]
        with open(self._csv_path, "w", newline="") as f:
            csv.writer(f).writerow(header)

    def _collect_metadata(self, completed=False):
        """Merge user metadata + auto-captured hardware/scan settings."""
        md = dict(self.meta.get_metadata())
        md.update({
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "zero_mm": self.spin_zero.value(),
            "probe_on_stage": self.chk_probe.isChecked(),
            "samples_per_point": self.spin_navg.value(),
            "settle_s": self._get_wait_time(),
            "n_points_planned": int(len(getattr(self, "_scan_points", []))),
            "n_points_acquired": len(self.d_delay_fs),
            "zones": self.interval.get_zone_config(),
            "completed": bool(completed),
            "csv_file": os.path.basename(self._csv_path) if self._csv_path else "",
        })
        # Per-lock-in id/channel/hardware state (best effort)
        lockins = []
        for L in self._scan_lockins:
            entry = {"id": L["id"], "channel": L["chan_name"]}
            drv = L["drv"]
            if drv.is_connected:
                entry["time_constant_s"] = drv.get_time_constant()
                entry["sensitivity_V"] = drv.get_sensitivity()
                entry["frequency_Hz"] = drv.get_frequency()
                entry["resource"] = drv.port
            lockins.append(entry)
        md["lockins"] = lockins
        md["n_lockins"] = len(lockins)
        if getattr(self, "_scan_start_time", None):
            md["scan_start"] = self._scan_start_time.isoformat(timespec="seconds")
        return md

    def _write_metadata_json(self, completed=False):
        if not self._json_path:
            return
        try:
            with open(self._json_path, "w") as f:
                json.dump(self._collect_metadata(completed), f, indent=2)
        except Exception as e:
            print(f"[Scan] metadata write failed: {e}")

    def _data_arrays(self):
        arrays = dict(
            delay_fs=np.array(self.d_delay_fs),
            position_mm=np.array(self.d_actual_mm),   # actual stage position
            target_mm=np.array(self.d_target_mm),     # commanded position
        )
        # per-lock-in channels, suffixed by id: X_A, Y_A, ..., X_B, ...
        for dev_id, d in self.d_lockin.items():
            for k in ("X", "Y", "R", "theta", "X_std", "Y_std", "R_std"):
                key = (f"theta_deg_{dev_id}" if k == "theta" else f"{k}_{dev_id}")
                arrays[key] = np.array(d[k])
        return arrays

    def _auto_save_npz(self):
        try:
            np.savez(self._npz_path,
                     metadata_json=json.dumps(self._collect_metadata(True)),
                     **self._data_arrays())
        except Exception as e:
            print(f"[Scan] auto-save failed: {e}")

    def save_data(self):
        """Manual 'save a copy as...' (auto-save already happened on finish)."""
        if not self.d_delay_fs:
            return
        init = os.path.join(self._default_dir(), self._base_name() + ".npz")
        fname, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save a copy", init, "NPZ Files (*.npz)")
        if not fname:
            return
        np.savez(fname,
                 metadata_json=json.dumps(self._collect_metadata(True)),
                 **self._data_arrays())
        self.lbl_status.setText(f"Status: copy saved {os.path.basename(fname)}")

    def closeEvent(self, event):
        self._scanning = False
        self._scan_move_timer.stop()
        if self._reader is not None and self._reader.isRunning():
            self._reader.wait(2000)
        event.accept()
