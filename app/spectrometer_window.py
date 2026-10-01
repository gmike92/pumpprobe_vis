"""
spectrometer_window.py
======================
Standalone window for an Ocean Optics spectrometer: live spectrum, background /
probe-reference capture, and a pump-probe **map** (a spectrum at every delay
stage position) displayed as ΔT/T.

    ΔT/T = (spectrum - reference) / (reference - background)

where *background* is the dark spectrum (no light) and *reference* is a separate
probe-only measurement (pump blocked). They are deliberately two different
measurements.

Threading: spectra are acquired on a short worker thread (seabreeze/USB is
thread-tolerant), but the delay stage is driven from the MAIN thread only — the
Thorlabs APT DLL is thread-affine (see stage_widget.py). The map scan is
therefore the same main-thread state machine used by the lock-in scan.
"""

import os
import json
from datetime import datetime

import numpy as np
from pyqtgraph.Qt import QtCore, QtWidgets
import pyqtgraph as pg

from driver_spectrometer import SpectrometerDriver, delta_t_over_t
from delay_interval_widget import DelayIntervalWidget
from metadata_widget import MetadataWidget
import style
import config_store


class _SpecReadTask(QtCore.QThread):
    """Acquire one (averaged) spectrum off the GUI thread."""
    got = QtCore.pyqtSignal(object)

    def __init__(self, driver, averages):
        super().__init__()
        self.driver = driver
        self.averages = int(averages)

    def run(self):
        try:
            data = self.driver.get_spectrum(averages=self.averages)
        except Exception as e:
            print(f"[Spec] acquire failed: {e}")
            data = np.array([])
        self.got.emit(data)


class SpectrometerWindow(QtWidgets.QMainWindow):
    """Separate window: live spectrum + pump-probe ΔT/T map."""

    def __init__(self, stage_widgets=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Ocean Optics Spectrometer — Pump-Probe Map")
        self.setMinimumSize(1280, 820)

        self.spec = SpectrometerDriver()
        self.stage_widgets = list(stage_widgets or [])
        self._reader = None
        self._live_worker = None     # persistent live-view acquisition thread
        self._mask = None            # cached ROI mask
        self._mask_x = np.array([])  # cached ROI wavelength axis

        self.background = None      # dark spectrum
        self.reference = None       # probe-only spectrum
        self._last_live = None      # most recent live spectrum (raw, full length)

        # map-scan state machine
        self._scanning = False
        self._paused = False
        self._paused_idle = False
        self._scan_index = 0
        self._scan_move_polls = 0
        self._scan_stage_widget = None
        self._reset_map()

        self._build_ui()

        self._live_timer = QtCore.QTimer()
        # Poll often and let _live_tick drop frames while a read is in flight:
        # the rate then follows the integration time instead of a fixed 120 ms
        # ceiling (which left the device idle most of each cycle and felt laggy).
        # ~40 Hz is an upper bound on redraws, plenty for a live view.
        self._live_timer.setInterval(25)
        self._live_timer.timeout.connect(self._live_tick)

        self._scan_move_timer = QtCore.QTimer()
        self._scan_move_timer.setInterval(80)
        self._scan_move_timer.timeout.connect(self._scan_poll_move)

        self._refresh_enabled()
        QtCore.QTimer.singleShot(150, self.scan_devices)

    # ------------------------------------------------------------------ #
    def _reset_map(self):
        self.map_delays = []        # fs
        self.map_positions = []     # mm
        self.map_raw = []           # list of raw spectra (ROI-cropped) — the
                                    # single source of truth; ΔT/T, ΔT and T are
                                    # all derived from this on demand
        self._scan_mask = None      # wavelength ROI frozen for the run
        self._scan_wl = np.array([])
        self._scan_ref = None       # reference cropped to the ROI
        self._scan_bg = None        # background cropped to the ROI
        self._csv_path = None
        self._npz_path = None
        self._json_path = None
        self._run_base = None
        self._scan_points = []
        self._scan_plan = []

    # ------------------------------------------------------------------ #
    # UI
    # ------------------------------------------------------------------ #
    def _build_ui(self):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        root = QtWidgets.QHBoxLayout(central)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(12)

        # ---------------- Left: controls ----------------
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setMinimumWidth(470)
        scroll.setMaximumWidth(560)
        ctrl = QtWidgets.QWidget()
        cl = QtWidgets.QVBoxLayout(ctrl)
        cl.setContentsMargins(2, 2, 8, 2)
        cl.setSpacing(12)

        # --- Device ---
        gb_dev = QtWidgets.QGroupBox("Spectrometer")
        dv = QtWidgets.QVBoxLayout(gb_dev)
        h = QtWidgets.QHBoxLayout()
        h.addWidget(QtWidgets.QLabel("Device:"))
        self.combo_dev = QtWidgets.QComboBox()
        self.combo_dev.setMinimumWidth(150)
        h.addWidget(self.combo_dev, 1)
        self.btn_rescan = QtWidgets.QPushButton("Rescan")
        self.btn_rescan.clicked.connect(self.scan_devices)
        h.addWidget(self.btn_rescan)
        dv.addLayout(h)
        h2 = QtWidgets.QHBoxLayout()
        self.btn_connect = QtWidgets.QPushButton("Connect")
        self.btn_connect.clicked.connect(self.toggle_connection)
        h2.addWidget(self.btn_connect)
        self.chk_sim = QtWidgets.QCheckBox("Simulate if absent")
        self.chk_sim.setToolTip("Run with a synthetic spectrum when no hardware is present")
        h2.addWidget(self.chk_sim)
        dv.addLayout(h2)
        self.lbl_dev_status = QtWidgets.QLabel("Not connected")
        self.lbl_dev_status.setStyleSheet(f"color: {style.RED}; font-weight: 600;")
        dv.addWidget(self.lbl_dev_status)
        cl.addWidget(gb_dev)

        # --- Acquisition ---
        gb_acq = QtWidgets.QGroupBox("Acquisition")
        af = QtWidgets.QFormLayout(gb_acq)
        self.spin_int = QtWidgets.QDoubleSpinBox()
        self.spin_int.setRange(1.0, 60000.0)
        self.spin_int.setDecimals(1)
        self.spin_int.setValue(20.0)
        self.spin_int.setSuffix(" ms")
        self.spin_int.valueChanged.connect(self._apply_integration)
        af.addRow("Integration time:", self.spin_int)
        self.spin_avg = QtWidgets.QSpinBox()
        self.spin_avg.setRange(1, 500)
        self.spin_avg.setValue(3)
        af.addRow("Averages/spectrum:", self.spin_avg)
        self.chk_live = QtWidgets.QCheckBox("Live view")
        self.chk_live.stateChanged.connect(self._toggle_live)
        af.addRow(self.chk_live)
        cl.addWidget(gb_acq)

        # --- Wavelength range (ROI) ---
        gb_wl = QtWidgets.QGroupBox("Wavelength Range")
        wv = QtWidgets.QVBoxLayout(gb_wl)
        hw = QtWidgets.QHBoxLayout()
        hw.addWidget(QtWidgets.QLabel("From:"))
        self.spin_wl_min = QtWidgets.QDoubleSpinBox()
        self.spin_wl_min.setRange(0.0, 1e5)
        self.spin_wl_min.setDecimals(1)
        self.spin_wl_min.setSuffix(" nm")
        self.spin_wl_min.valueChanged.connect(self._on_range_changed)
        hw.addWidget(self.spin_wl_min)
        hw.addWidget(QtWidgets.QLabel("to:"))
        self.spin_wl_max = QtWidgets.QDoubleSpinBox()
        self.spin_wl_max.setRange(0.0, 1e5)
        self.spin_wl_max.setDecimals(1)
        self.spin_wl_max.setSuffix(" nm")
        self.spin_wl_max.valueChanged.connect(self._on_range_changed)
        hw.addWidget(self.spin_wl_max)
        wv.addLayout(hw)
        self.btn_full_range = QtWidgets.QPushButton("Full detector range")
        self.btn_full_range.clicked.connect(self._set_full_range)
        wv.addWidget(self.btn_full_range)
        self.lbl_range = QtWidgets.QLabel("Range: —")
        self.lbl_range.setStyleSheet(f"color: {style.MUTED};")
        wv.addWidget(self.lbl_range)
        cl.addWidget(gb_wl)

        # --- Background / Reference ---
        gb_ref = QtWidgets.QGroupBox("Background && Reference")
        rv = QtWidgets.QVBoxLayout(gb_ref)
        self.btn_bg = QtWidgets.QPushButton("Take Background (dark)")
        self.btn_bg.setToolTip("Block ALL light, then capture the dark spectrum")
        self.btn_bg.clicked.connect(lambda: self._capture("background"))
        rv.addWidget(self.btn_bg)
        self.lbl_bg = QtWidgets.QLabel("Background: none")
        self.lbl_bg.setStyleSheet(f"color: {style.MUTED};")
        rv.addWidget(self.lbl_bg)

        self.btn_ref = QtWidgets.QPushButton("Take Reference (probe only)")
        self.btn_ref.setToolTip("Block the PUMP (probe on sample), then capture the reference")
        self.btn_ref.clicked.connect(lambda: self._capture("reference"))
        rv.addWidget(self.btn_ref)
        self.lbl_ref = QtWidgets.QLabel("Reference: none")
        self.lbl_ref.setStyleSheet(f"color: {style.MUTED};")
        rv.addWidget(self.lbl_ref)

        self.chk_sub_dark = QtWidgets.QCheckBox("Subtract dark from live view")
        self.chk_sub_dark.setToolTip(
            "Once a background is stored, show dark-corrected counts "
            "(live and reference both have the dark removed)")
        self.chk_sub_dark.setChecked(True)
        self.chk_sub_dark.stateChanged.connect(self._redraw_spectra)
        rv.addWidget(self.chk_sub_dark)

        self.btn_clear = QtWidgets.QPushButton("Clear both")
        self.btn_clear.clicked.connect(self._clear_refs)
        rv.addWidget(self.btn_clear)
        cl.addWidget(gb_ref)

        # --- Metadata ---
        self.meta = MetadataWidget()
        cl.addWidget(self.meta)

        # --- Map scan ---
        gb_map = QtWidgets.QGroupBox("Pump-Probe Map")
        mv = QtWidgets.QVBoxLayout(gb_map)
        hs = QtWidgets.QHBoxLayout()
        hs.addWidget(QtWidgets.QLabel("Stage:"))
        self.combo_stage = QtWidgets.QComboBox()
        for w in self.stage_widgets:
            self.combo_stage.addItem(w.title())
        if not self.stage_widgets:
            self.combo_stage.addItem("(no stage)")
        self.combo_stage.currentIndexChanged.connect(
            lambda _i: self._sync_zero_from_stage())
        hs.addWidget(self.combo_stage, 1)
        mv.addLayout(hs)

        hz = QtWidgets.QHBoxLayout()
        hz.addWidget(QtWidgets.QLabel("Zero t0 (mm):"))
        self.spin_zero = QtWidgets.QDoubleSpinBox()
        self.spin_zero.setRange(-500.0, 500.0)
        self.spin_zero.setDecimals(4)
        hz.addWidget(self.spin_zero)
        self.btn_zero = QtWidgets.QPushButton("Set = current")
        self.btn_zero.clicked.connect(self._use_current_zero)
        hz.addWidget(self.btn_zero)
        mv.addLayout(hz)

        self.chk_probe = QtWidgets.QCheckBox("Probe on stage (invert axis)")
        mv.addWidget(self.chk_probe)

        self.interval = DelayIntervalWidget()
        mv.addWidget(self.interval)

        hb = QtWidgets.QHBoxLayout()
        self.btn_run = QtWidgets.QPushButton("Run Map")
        self.btn_run.setStyleSheet(
            "QPushButton { background-color: #4caf82; color: #10151b; font-weight: 700;"
            " font-size: 15px; padding: 10px; border: none; border-radius: 6px; }"
            "QPushButton:disabled { background-color: #2f4a3e; color: #789; }")
        self.btn_run.clicked.connect(self.start_map)
        hb.addWidget(self.btn_run)
        self.btn_pause = QtWidgets.QPushButton("Pause")
        self.btn_pause.setStyleSheet(
            "QPushButton { background-color: #ffb74d; color: #10151b; font-weight: 700;"
            " font-size: 15px; padding: 10px; border: none; border-radius: 6px; }"
            "QPushButton:disabled { background-color: #4a412e; color: #987; }")
        self.btn_pause.clicked.connect(self.toggle_pause)
        self.btn_pause.setEnabled(False)
        hb.addWidget(self.btn_pause)
        self.btn_stop = QtWidgets.QPushButton("Stop")
        self.btn_stop.setStyleSheet(
            "QPushButton { background-color: #ef5350; color: #fff; font-weight: 700;"
            " font-size: 15px; padding: 10px; border: none; border-radius: 6px; }"
            "QPushButton:disabled { background-color: #4a2f2e; color: #987; }")
        self.btn_stop.clicked.connect(self.stop_map)
        self.btn_stop.setEnabled(False)
        hb.addWidget(self.btn_stop)
        mv.addLayout(hb)

        self.progress = QtWidgets.QProgressBar()
        mv.addWidget(self.progress)
        self.lbl_status = QtWidgets.QLabel("Status: Ready")
        self.lbl_status.setStyleSheet(f"color: {style.GREEN}; font-weight: 600;")
        self.lbl_status.setWordWrap(True)
        mv.addWidget(self.lbl_status)
        self.btn_save = QtWidgets.QPushButton("Save a copy as...")
        self.btn_save.clicked.connect(self.save_copy)
        self.btn_save.setEnabled(False)
        mv.addWidget(self.btn_save)
        cl.addWidget(gb_map)

        cl.addStretch()
        scroll.setWidget(ctrl)
        root.addWidget(scroll)

        # ---------------- Right: plots ----------------
        tabs = QtWidgets.QTabWidget()

        # Live spectrum
        self.plot_spec = pg.PlotWidget(title="Live Spectrum")
        self.plot_spec.setLabel("bottom", "Wavelength", "nm")
        self.plot_spec.setLabel("left", "Counts")
        self.plot_spec.showGrid(x=True, y=True, alpha=0.25)
        # nm/fs are already the right magnitude — don't let pyqtgraph turn
        # 600 nm into "0.6 knm" or 3000 fs into "3 kfs"
        self.plot_spec.getAxis("bottom").enableAutoSIPrefix(False)
        self.plot_spec.addLegend(offset=(-10, 10))
        # 2048-point curves refreshed several times a second: clip to the visible
        # range and auto-downsample, and skip antialiasing (which style.py turns
        # on globally) — together these keep the live view smooth.
        self.plot_spec.setClipToView(True)
        self.curve_live = self.plot_spec.plot(pen=pg.mkPen("#4aa3ff", width=1),
                                              name="live", antialias=False,
                                              autoDownsample=True)
        self.curve_bg = self.plot_spec.plot(pen=pg.mkPen("#9aa0a6", width=1,
                                                         style=QtCore.Qt.PenStyle.DashLine),
                                            name="background", antialias=False,
                                            autoDownsample=True)
        self.curve_ref = self.plot_spec.plot(pen=pg.mkPen("#4caf82", width=1),
                                             name="reference", antialias=False,
                                             autoDownsample=True)
        tabs.addTab(self.plot_spec, "Live Spectrum")

        # ΔT/T map + cut
        map_page = QtWidgets.QWidget()
        mpl = QtWidgets.QVBoxLayout(map_page)
        mpl.setContentsMargins(0, 0, 0, 0)

        # what the map/cut display — derived from the stored raw spectra, so it
        # can be switched at any time (during or long after a scan)
        hmode = QtWidgets.QHBoxLayout()
        hmode.addWidget(QtWidgets.QLabel("Map shows:"))
        self.combo_mode = QtWidgets.QComboBox()
        self.combo_mode.addItems(["ΔT/T", "ΔT", "T"])
        self.combo_mode.setToolTip(
            "ΔT/T = (spectrum - reference) / (reference - background)\n"
            "ΔT   = spectrum - reference\n"
            "T    = spectrum - background   (dark-corrected transmission)")
        self.combo_mode.setMinimumWidth(110)
        self.combo_mode.currentIndexChanged.connect(lambda _i: self._update_map())
        hmode.addWidget(self.combo_mode)
        hmode.addStretch()
        mpl.addLayout(hmode)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)

        self.plot_map = pg.PlotWidget(title="ΔT/T map")
        self.plot_map.setLabel("bottom", "Wavelength", "nm")
        self.plot_map.setLabel("left", "Delay", "fs")
        self.plot_map.getAxis("bottom").enableAutoSIPrefix(False)
        self.plot_map.getAxis("left").enableAutoSIPrefix(False)
        self.img = pg.ImageItem()
        self.plot_map.addItem(self.img)
        # diverging blue-white-red, centred on 0 — for the signed ΔT/T and ΔT
        self._lut_diverging = pg.ColorMap(
            np.array([0.0, 0.25, 0.5, 0.75, 1.0]),
            np.array([[0, 0, 180, 255], [100, 150, 255, 255],
                      [255, 255, 255, 255],
                      [255, 150, 100, 255], [180, 0, 0, 255]],
                     dtype=np.ubyte)).getLookupTable(0.0, 1.0, 256)
        # sequential (viridis-like) — for the strictly positive T
        self._lut_sequential = pg.ColorMap(
            np.array([0.0, 0.25, 0.5, 0.75, 1.0]),
            np.array([[68, 1, 84, 255], [59, 82, 139, 255],
                      [33, 145, 140, 255], [94, 201, 98, 255],
                      [253, 231, 37, 255]], dtype=np.ubyte)).getLookupTable(0.0, 1.0, 256)
        self.img.setLookupTable(self._lut_diverging)
        self.cursor_wl = pg.InfiniteLine(angle=90, movable=True,
                                         pen=pg.mkPen("#ffb74d", width=2))
        self.cursor_wl.sigPositionChanged.connect(self._update_cut)
        self.plot_map.addItem(self.cursor_wl)
        splitter.addWidget(self.plot_map)

        self.plot_cut = pg.PlotWidget(title="ΔT/T dynamics at cursor wavelength")
        self.plot_cut.setLabel("bottom", "Delay", "fs")
        self.plot_cut.setLabel("left", "ΔT/T")
        self.plot_cut.getAxis("bottom").enableAutoSIPrefix(False)
        self.plot_cut.showGrid(x=True, y=True, alpha=0.25)
        self.curve_cut = self.plot_cut.plot(pen=pg.mkPen("#ff7043", width=2),
                                            symbol="o", symbolSize=5,
                                            symbolBrush="#ff7043")
        splitter.addWidget(self.plot_cut)
        splitter.setSizes([460, 260])
        mpl.addWidget(splitter)
        tabs.addTab(map_page, "Pump-Probe Map")

        root.addWidget(tabs, 1)

    # ------------------------------------------------------------------ #
    # Device
    # ------------------------------------------------------------------ #
    def scan_devices(self):
        self.btn_rescan.setEnabled(False)
        self.lbl_dev_status.setText("Scanning...")
        self.lbl_dev_status.setStyleSheet(f"color: {style.AMBER}; font-weight: 600;")
        QtWidgets.QApplication.processEvents()
        devs = self.spec.list_devices()
        self.combo_dev.clear()
        for serial, model in devs:
            self.combo_dev.addItem(f"{model}  ({serial})", userData=serial)
        if devs:
            self.lbl_dev_status.setText(f"Found {len(devs)} spectrometer(s)")
            self.lbl_dev_status.setStyleSheet(f"color: {style.BLUE}; font-weight: 600;")
        else:
            self.lbl_dev_status.setText("None found (tick 'Simulate' to test offline)")
            self.lbl_dev_status.setStyleSheet(f"color: {style.RED}; font-weight: 600;")
        self._refresh_enabled()

    def toggle_connection(self):
        if self.spec.is_connected:
            self._live_timer.stop()
            self.chk_live.setChecked(False)
            # let an in-flight acquisition finish before closing the device
            if self._live_worker is not None and self._live_worker.isRunning():
                self._live_worker.wait(3000)
            self._live_worker = None
            self.spec.disconnect()
            self.lbl_dev_status.setText("Disconnected")
            self.lbl_dev_status.setStyleSheet(f"color: {style.MUTED}; font-weight: 600;")
        else:
            serial = self.combo_dev.currentData()
            ok = self.spec.connect(serial=serial,
                                   simulate_if_absent=self.chk_sim.isChecked())
            if ok:
                tag = " [SIMULATED]" if self.spec.simulated else ""
                self.lbl_dev_status.setText(
                    f"Connected: {self.spec.model} ({self.spec.serial}){tag}")
                self.lbl_dev_status.setStyleSheet(f"color: {style.GREEN}; font-weight: 600;")
                lo, hi = self.spec.integration_limits_ms()
                self.spin_int.setRange(lo, hi)
                self.spec.set_integration_time_ms(self.spin_int.value())
                self._set_full_range()      # default ROI = whole detector
            else:
                self.lbl_dev_status.setText("Connection failed")
                self.lbl_dev_status.setStyleSheet(f"color: {style.RED}; font-weight: 600;")
        self._refresh_enabled()

    def _apply_integration(self, val):
        if self.spec.is_connected:
            self.spec.set_integration_time_ms(val)

    def _refresh_enabled(self):
        conn = self.spec.is_connected
        scanning = self._scanning
        self.btn_connect.setText("Disconnect" if conn else "Connect")
        self.btn_connect.setEnabled(not scanning)
        self.btn_rescan.setEnabled(not conn and not scanning)
        self.combo_dev.setEnabled(not conn and not scanning)
        self.chk_sim.setEnabled(not conn and not scanning)
        for w in (self.btn_bg, self.btn_ref, self.btn_clear, self.chk_live,
                  self.chk_sub_dark, self.spin_wl_min, self.spin_wl_max,
                  self.btn_full_range):
            w.setEnabled(conn and not scanning)
        self.btn_run.setEnabled(conn and not scanning)
        self.btn_stop.setEnabled(scanning)
        self.btn_pause.setEnabled(scanning)

    # ------------------------------------------------------------------ #
    # Live view / captures
    # ------------------------------------------------------------------ #
    def _toggle_live(self):
        if self.chk_live.isChecked() and self.spec.is_connected:
            self._live_timer.start()
        else:
            self._live_timer.stop()

    def _live_tick(self):
        """Kick off one acquisition, reusing a single worker thread.

        Creating a QThread per frame (as this used to) is a big part of what made
        the live view stutter; a busy worker simply means we skip this frame.
        """
        if self._scanning or not self.spec.is_connected:
            return
        w = self._live_worker
        if w is not None and w.isRunning():
            return                       # still acquiring -> drop this frame
        if w is None:
            w = self._live_worker = _SpecReadTask(self.spec, 1)
            w.got.connect(self._on_live)   # connected once, not every frame
        w.averages = 1
        w.start()

    def _on_live(self, data):
        if data is None or len(data) == 0:
            return
        self._last_live = np.asarray(data, float)
        self._redraw_live()              # cheap: only the live curve

    # -- wavelength range (ROI) ------------------------------------------ #
    def _recompute_mask(self):
        """Cache the ROI mask + its x axis (recomputing it per frame is waste)."""
        wl = self.spec.wavelengths()
        if wl.size == 0:
            self._mask = np.zeros(0, bool)
        else:
            lo, hi = self.spin_wl_min.value(), self.spin_wl_max.value()
            if hi <= lo:
                m = np.ones(wl.size, bool)
            else:
                m = (wl >= lo) & (wl <= hi)
                if not m.any():
                    m = np.ones(wl.size, bool)
            self._mask = m
        self._mask_x = wl[self._mask] if wl.size else np.array([])
        return self._mask

    def _wl_mask(self):
        """Boolean mask selecting the pixels inside the chosen wavelength range."""
        if self._mask is None or (self.spec.wavelengths().size != self._mask.size):
            self._recompute_mask()
        return self._mask

    def _set_full_range(self):
        wl = self.spec.wavelengths()
        if wl.size == 0:
            return
        for sp, v in ((self.spin_wl_min, wl[0]), (self.spin_wl_max, wl[-1])):
            sp.blockSignals(True)
            sp.setRange(float(wl[0]), float(wl[-1]))
            sp.setValue(float(v))
            sp.blockSignals(False)
        self._on_range_changed()

    def _on_range_changed(self, *_):
        self._recompute_mask()
        m, x = self._mask, self._mask_x
        if x.size:
            self.lbl_range.setText(
                f"Range: {x[0]:.1f} – {x[-1]:.1f} nm  ({int(m.sum())} px)")
        self._redraw_spectra()

    def _redraw_live(self):
        """Per-frame path — updates ONLY the live curve. Keep this cheap: no
        axis labels, no setXRange, no redrawing the static traces."""
        if self._last_live is None or self._mask is None:
            return
        if self._last_live.size != self._mask.size:
            return
        y = self._last_live
        if self.chk_sub_dark.isChecked() and self.background is not None \
                and self.background.size == y.size:
            y = y - self.background
        self.curve_live.setData(self._mask_x, y[self._mask])

    def _redraw_spectra(self, *_):
        """Full redraw — static traces, axis label and view range. Only called
        when something actually changes (ROI, background/reference, toggle)."""
        wl = self.spec.wavelengths()
        if wl.size == 0:
            return
        if self._mask is None or self._mask.size != wl.size:
            self._recompute_mask()
        m, x = self._mask, self._mask_x
        sub = self.chk_sub_dark.isChecked() and self.background is not None
        dark = self.background if sub else None

        if self.reference is not None and self.reference.size == wl.size:
            y = self.reference - dark if sub else self.reference
            self.curve_ref.setData(x, y[m])
        else:
            self.curve_ref.setData([], [])
        # once the dark is being subtracted it would just sit at zero — hide it
        if self.background is not None and not sub:
            self.curve_bg.setData(x, self.background[m])
        else:
            self.curve_bg.setData([], [])

        self.plot_spec.setLabel(
            "left", "Counts (dark-corrected)" if sub else "Counts")
        if x.size:
            self.plot_spec.setXRange(float(x[0]), float(x[-1]), padding=0.02)
        self._redraw_live()

    def _capture(self, which):
        """Capture the background (dark) or the probe-only reference."""
        if not self.spec.is_connected:
            return
        self.lbl_status.setText(f"Status: acquiring {which}...")
        self.btn_bg.setEnabled(False)
        self.btn_ref.setEnabled(False)
        task = _SpecReadTask(self.spec, self.spin_avg.value())
        task.got.connect(lambda d: self._on_capture(which, d))
        self._capture_task = task
        task.start()

    def _on_capture(self, which, data):
        stamp = datetime.now().strftime("%H:%M:%S")
        if data is not None and len(data):
            if which == "background":
                self.background = np.asarray(data, float)
                self.lbl_bg.setText(f"Background: stored {stamp} "
                                    f"(max {self.background.max():.0f} cts)")
                self.lbl_bg.setStyleSheet(f"color: {style.GREEN};")
            else:
                self.reference = np.asarray(data, float)
                self.lbl_ref.setText(f"Reference: stored {stamp} "
                                     f"(max {self.reference.max():.0f} cts)")
                self.lbl_ref.setStyleSheet(f"color: {style.GREEN};")
        self.lbl_status.setText("Status: Ready")
        self._redraw_spectra()      # dark now removed from the live view
        self._refresh_enabled()

    def _clear_refs(self):
        self.background = None
        self.reference = None
        self.lbl_bg.setText("Background: none")
        self.lbl_bg.setStyleSheet(f"color: {style.MUTED};")
        self.lbl_ref.setText("Reference: none")
        self.lbl_ref.setStyleSheet(f"color: {style.MUTED};")
        self._redraw_spectra()

    # ------------------------------------------------------------------ #
    # Stage helpers
    # ------------------------------------------------------------------ #
    def _current_stage_widget(self):
        i = self.combo_stage.currentIndex()
        if 0 <= i < len(self.stage_widgets):
            return self.stage_widgets[i]
        return None

    def _current_stage(self):
        w = self._current_stage_widget()
        return w.stage if w is not None else None

    def _use_current_zero(self):
        st = self._current_stage()
        if st is not None and st.is_connected:
            pos = st.get_position()
            self.spin_zero.setValue(pos)
            st.zero_position_mm = pos
            config_store.set_stage_zero(st.serial, pos)   # persist per-stage

    def set_zero_mm(self, value):
        """Reflect an externally set/restored t0 into the map's t0 field."""
        self.spin_zero.setValue(float(value))

    def _sync_zero_from_stage(self):
        """Adopt the selected stage's current/saved t0."""
        st = self._current_stage()
        if st is not None and getattr(st, "is_connected", False):
            self.spin_zero.setValue(st.zero_position_mm)

    # ------------------------------------------------------------------ #
    # Map scan — main-thread state machine (stage is thread-affine)
    # ------------------------------------------------------------------ #
    def start_map(self):
        st = self._current_stage()
        if st is None or not st.is_connected:
            self.lbl_status.setText("Status: connect the delay stage first!")
            return
        if not self.spec.is_connected:
            self.lbl_status.setText("Status: connect the spectrometer first!")
            return
        if self.reference is None:
            self.lbl_status.setText("Status: take a probe Reference first (needed for ΔT/T)")
            return
        st.zero_position_mm = self.spin_zero.value()
        # mm mode = absolute stage positions, fs mode = delays relative to t0
        plan = self.interval.get_scan_plan(
            st, self.spin_zero.value(),
            invert_stage=self.chk_probe.isChecked())
        if len(plan) == 0:
            self.lbl_status.setText("Status: no scan points — check intervals.")
            return

        self.chk_live.setChecked(False)
        self._live_timer.stop()

        self._reset_map()
        self._scan_plan = plan
        self._scan_points = np.array([d for (_t, d) in plan])
        self._scan_start_time = datetime.now()
        self._scan_index = 0
        self._scanning = True
        self._paused = False
        self._paused_idle = False
        self._scan_sign = -1.0 if self.chk_probe.isChecked() else 1.0
        self._scan_zero_mm = st.zero_position_mm
        self._scan_avg = self.spin_avg.value()
        # Freeze the wavelength ROI for the whole map so every row has the same
        # length even if the user fiddles with the range mid-scan.
        self._scan_mask = self._wl_mask()
        self._scan_wl = self.spec.wavelengths()[self._scan_mask]
        self._scan_ref = self.reference[self._scan_mask]
        self._scan_bg = (self.background[self._scan_mask]
                         if self.background is not None
                         else np.zeros(self._scan_wl.size))

        self._open_files()
        self._write_metadata_json(completed=False)

        self.progress.setMaximum(len(plan))
        self.progress.setValue(0)
        self.btn_pause.setText("Pause")
        self._refresh_enabled()
        self.lbl_status.setText(f"Status: mapping {len(plan)} delays...")

        # lock the driven stage panel (only this scan drives it)
        self._scan_stage_widget = self._current_stage_widget()
        if self._scan_stage_widget is not None:
            self._scan_stage_widget.set_scan_active(True)

        QtCore.QTimer.singleShot(0, self._scan_move)

    def stop_map(self):
        if self._scanning:
            self._scanning = False
            self._paused = False
            self._scan_move_timer.stop()
            if self._reader is not None and self._reader.isRunning():
                return
            self._scan_finish(completed=False)

    def toggle_pause(self):
        if not self._scanning:
            return
        self._paused = not self._paused
        if self._paused:
            self.btn_pause.setText("Resume")
            self.lbl_status.setText("Status: pausing after this delay...")
        else:
            self.btn_pause.setText("Pause")
            self.lbl_status.setText("Status: resuming...")
            if self._paused_idle:
                self._paused_idle = False
                QtCore.QTimer.singleShot(0, self._scan_move)

    def _scan_move(self):
        if not self._scanning:
            return
        self._paused_idle = False
        if self._scan_index >= len(self._scan_plan):
            self._scan_finish(completed=True)
            return
        st = self._current_stage()
        target, delay_fs = self._scan_plan[self._scan_index]
        self._scan_target_mm = target
        self.lbl_status.setText(
            f"Status: delay {self._scan_index + 1}/{len(self._scan_plan)} "
            f"— {delay_fs:+.0f} fs  →  {target:.4f} mm")
        try:
            st.move_to(target, wait=False)
        except Exception as e:
            print(f"[SpecMap] move failed: {e}")
        self._scan_move_polls = 0
        self._scan_move_timer.start()

    def _scan_poll_move(self):
        if not self._scanning:
            self._scan_move_timer.stop()
            return
        st = self._current_stage()
        self._scan_move_polls += 1
        if self._scan_move_polls < 3:
            return
        timed_out = self._scan_move_polls > 900
        if not st.is_moving() or timed_out:
            self._scan_move_timer.stop()
            # simulation: let the fake spectrum depend on the delay
            if self.spec.simulated:
                self.spec.sim_delay_fs = float(self._scan_points[self._scan_index])
            self._reader = _SpecReadTask(self.spec, self._scan_avg)
            self._reader.got.connect(self._scan_record)
            self._reader.start()

    def _scan_record(self, data):
        st = self._current_stage()
        delay_fs = float(self._scan_plan[self._scan_index][1])
        try:
            pos = st.get_position()
        except Exception:
            pos = self._scan_target_mm
        spectrum = np.asarray(data, float)
        if spectrum.size == 0:
            spectrum = np.zeros_like(self.spec.wavelengths())

        # keep only the frozen wavelength ROI; ΔT/T, ΔT and T are derived later
        m = self._scan_mask
        self.map_delays.append(delay_fs)
        self.map_positions.append(pos)
        self.map_raw.append(spectrum[m])

        self._update_map()
        self._append_csv(delay_fs, pos, spectrum)

        self._scan_index += 1
        self.progress.setValue(self._scan_index)

        if not self._scanning:
            self._scan_finish(completed=False)
        elif self._paused:
            self._paused_idle = True
            self.lbl_status.setText(
                f"Status: PAUSED at delay {self._scan_index}/"
                f"{len(self._scan_points)} — press Resume")
        else:
            QtCore.QTimer.singleShot(0, self._scan_move)

    def _scan_finish(self, completed):
        self._scanning = False
        self._paused = False
        self._paused_idle = False
        self._scan_move_timer.stop()
        self.btn_pause.setText("Pause")
        self._refresh_enabled()
        self.btn_save.setEnabled(len(self.map_raw) > 0)

        if self._scan_stage_widget is not None:
            self._scan_stage_widget.set_scan_active(False)
            self._scan_stage_widget = None

        saved = ""
        if self.map_raw:
            self._write_metadata_json(completed=completed)
            self._save_npz(self._npz_path)
            saved = f" — saved {os.path.basename(self._npz_path)}"
        word = "complete" if completed else "aborted (partial saved)"
        self.lbl_status.setText(f"Status: map {word}{saved}")

    # ------------------------------------------------------------------ #
    # Display
    # ------------------------------------------------------------------ #
    def current_mode(self):
        return self.combo_mode.currentText()

    def _map_array(self, mode=None):
        """Build the 2-D display array from the stored raw spectra.

            ΔT/T = (spectrum - reference) / (reference - background)
            ΔT   =  spectrum - reference
            T    =  spectrum - background        (dark-corrected transmission)
        """
        mode = mode or self.current_mode()
        raw = np.asarray(self.map_raw, float)          # (n_delay, n_roi_px)
        if raw.size == 0:
            return raw
        ref = self._scan_ref
        bg = self._scan_bg if self._scan_bg is not None else np.zeros(raw.shape[1])
        if mode == "T":
            return raw - bg
        if ref is None:
            return raw - bg
        if mode == "ΔT":
            return raw - ref
        # ΔT/T — guard against dividing by probe transmission that is just noise
        t_probe = ref - bg
        out = np.zeros_like(raw)
        valid = np.abs(t_probe) > 1.0
        if valid.any():
            out[:, valid] = (raw[:, valid] - ref[valid]) / t_probe[valid]
        return out

    def _update_map(self):
        if not self.map_raw:
            return
        mode = self.current_mode()
        arr = self._map_array(mode)
        wl = self._scan_wl
        if arr.size == 0 or wl.size == 0:
            return

        if mode == "T":
            # strictly positive -> sequential scale from 0
            hi = float(np.nanmax(arr)) or 1.0
            self.img.setLookupTable(self._lut_sequential)
            levels = (0.0, hi)
        else:
            # signed -> diverging scale centred on 0
            lim = float(np.nanmax(np.abs(arr))) or 1e-6
            self.img.setLookupTable(self._lut_diverging)
            levels = (-lim, lim)

        # ImageItem is column-major by default -> transpose so x=wavelength
        self.img.setImage(arr.T, levels=levels, autoLevels=False)
        d0, d1 = self.map_delays[0], self.map_delays[-1]
        if d1 == d0:
            d1 = d0 + 1.0
        self.img.setRect(QtCore.QRectF(wl[0], d0, wl[-1] - wl[0], d1 - d0))
        self.plot_map.setTitle(f"{mode} map")
        if self.cursor_wl.value() < wl[0] or self.cursor_wl.value() > wl[-1]:
            self.cursor_wl.setValue(float(wl[len(wl) // 2]))
        self._update_cut()

    def _update_cut(self):
        if not self.map_raw:
            return
        wl = self._scan_wl
        if wl.size == 0:
            return
        mode = self.current_mode()
        idx = int(np.argmin(np.abs(wl - self.cursor_wl.value())))
        arr = self._map_array(mode)
        self.curve_cut.setData(np.asarray(self.map_delays, float), arr[:, idx])
        self.plot_cut.setTitle(f"{mode} dynamics at {wl[idx]:.1f} nm")
        self.plot_cut.setLabel("left", mode)

    # ------------------------------------------------------------------ #
    # Saving
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
        if not self._run_base:
            name = "".join(c for c in self.meta.sample_name()
                           if c.isalnum() or c in " -_").strip()
            self._run_base = f"{name or 'sample'}_specmap_{datetime.now():%H%M%S}"
        return self._run_base

    def _open_files(self):
        self._run_base = None
        base = self._base_name()
        d = self._default_dir()
        self._csv_path = os.path.join(d, base + "_raw.csv")
        self._npz_path = os.path.join(d, base + ".npz")
        self._json_path = os.path.join(d, base + ".json")
        wl = self._scan_wl
        with open(self._csv_path, "w") as f:
            f.write("delay_fs,position_mm," + ",".join(f"{w:.3f}" for w in wl) + "\n")

    def _append_csv(self, delay_fs, pos_mm, spectrum):
        if not self._csv_path:
            return
        try:
            row = ",".join(f"{v:.6g}" for v in spectrum)
            with open(self._csv_path, "a") as f:
                f.write(f"{delay_fs:.3f},{pos_mm:.5f},{row}\n")
        except Exception as e:
            print(f"[SpecMap] csv append failed: {e}")

    def _collect_metadata(self, completed=False):
        md = dict(self.meta.get_metadata())
        md.update({
            "measurement": "spectrometer_pump_probe_map",
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "spectrometer_model": self.spec.model,
            "spectrometer_serial": self.spec.serial,
            "spectrometer_backend": self.spec.backend,
            "simulated": bool(self.spec.simulated),
            "integration_time_ms": self.spec.get_integration_time_ms(),
            "averages": int(self.spin_avg.value()),
            "wavelength_range_nm": [float(self.spin_wl_min.value()),
                                    float(self.spin_wl_max.value())],
            "roi_pixels": int(len(getattr(self, "_scan_wl", []))),
            "dark_subtracted_in_view": bool(self.chk_sub_dark.isChecked()),
            "zero_mm": self.spin_zero.value(),
            "probe_on_stage": self.chk_probe.isChecked(),
            "has_background": self.background is not None,
            "has_reference": self.reference is not None,
            "n_delays_planned": int(len(self._scan_points)),
            "n_delays_acquired": len(self.map_raw),
            "display_mode": self.current_mode(),
            "zones": self.interval.get_zone_config(),
            "completed": bool(completed),
            "dtt_formula": "(spectrum - reference) / (reference - background)",
            "csv_file": os.path.basename(self._csv_path) if self._csv_path else "",
        })
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
            print(f"[SpecMap] metadata write failed: {e}")

    def _save_npz(self, path):
        try:
            full_wl = self.spec.wavelengths()
            empty = np.array([])
            np.savez_compressed(
                path,
                # ROI axis — matches spectra / dtt / dt / t and *_roi
                wavelengths_nm=getattr(self, "_scan_wl", full_wl),
                wavelengths_full_nm=full_wl,
                delay_fs=np.asarray(self.map_delays, float),
                position_mm=np.asarray(self.map_positions, float),
                spectra=np.asarray(self.map_raw, float),     # raw, (n_delay, n_px)
                # all three derived quantities, so no reprocessing is needed
                dtt=self._map_array("ΔT/T"),
                dt=self._map_array("ΔT"),
                t=self._map_array("T"),
                background_roi=(self._scan_bg if self._scan_bg is not None else empty),
                reference_roi=(self._scan_ref if self._scan_ref is not None else empty),
                background=(self.background if self.background is not None else empty),
                reference=(self.reference if self.reference is not None else empty),
                metadata_json=json.dumps(self._collect_metadata(True)),
            )
        except Exception as e:
            print(f"[SpecMap] save failed: {e}")

    def save_copy(self):
        if not self.map_raw:
            return
        init = os.path.join(self._default_dir(), self._base_name() + ".npz")
        fname, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save a copy", init, "NPZ Files (*.npz)")
        if fname:
            self._save_npz(fname)
            self.lbl_status.setText(f"Status: copy saved {os.path.basename(fname)}")

    # ------------------------------------------------------------------ #
    def showEvent(self, event):
        super().showEvent(event)
        self._sync_zero_from_stage()   # adopt the selected stage's saved t0

    def closeEvent(self, event):
        self._scanning = False
        self._live_timer.stop()
        self._scan_move_timer.stop()
        if self._reader is not None and self._reader.isRunning():
            self._reader.wait(3000)
        if self._live_worker is not None and self._live_worker.isRunning():
            self._live_worker.wait(3000)
        if self._scan_stage_widget is not None:
            self._scan_stage_widget.set_scan_active(False)
        event.accept()
