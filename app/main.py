"""
main.py
=======
Lock-in pump-probe control application (entry point).

Assembles the standalone, lock-in-based pump-probe setup:
  * SR865A lock-in control        (lockin_widget.LockInControlWidget)
  * two Thorlabs delay stages      (stage_widget.StageControlWidget)
      - one carrying the pump delay line, one the probe delay line
  * a delay-scan engine            (scan_widget.PumpProbeScanWidget)

This is the clean re-implementation of the lock-in path of
https://github.com/gmike92/Labview-pumprobepython (the LabVIEW/camera and
TWINS/k-space paths are intentionally left out).

Run:
    python main.py
"""

import sys

from pyqtgraph.Qt import QtCore, QtWidgets

from lockin_widget import LockInControlWidget
from stage_widget import StageControlWidget
from scan_widget import PumpProbeScanWidget
import style


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Lock-In Pump-Probe")
        self.setMinimumSize(1320, 820)

        # --- Shared widgets / drivers ---
        # Two independent lock-ins (A defaults to serial 004418; B is entered/
        # found by the user). Both can be connected and acquired at once.
        self.lockin_a = LockInControlWidget(dev_id="A")
        self.lockin_b = LockInControlWidget(
            dev_id="B", resource="USB0::0xB506::0x2000::000000::INSTR")
        self.lockin_widget = self.lockin_a       # back-compat alias
        self.pump_stage = StageControlWidget(title="Pump delay stage")
        self.probe_stage = StageControlWidget(title="Probe delay stage")

        self._build_ui()

    def _build_ui(self):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        root = QtWidgets.QVBoxLayout(central)
        root.setContentsMargins(14, 12, 14, 14)
        root.setSpacing(10)

        # ---- Header banner ----
        h_head = QtWidgets.QHBoxLayout()
        header = QtWidgets.QLabel("Lock-In Pump-Probe")
        header.setStyleSheet(
            f"font-size: 22px; font-weight: 700; color: {style.BLUE}; padding: 2px 4px;")
        h_head.addWidget(header)
        h_head.addStretch()
        self.btn_spec = QtWidgets.QPushButton("Open Spectrometer Window")
        self.btn_spec.setToolTip(
            "Ocean Optics spectrometer: live spectrum, background/reference, "
            "and a pump-probe ΔT/T map")
        self.btn_spec.setStyleSheet(
            "QPushButton { background-color: #7e57c2; color: #fff; font-weight: 700; "
            "padding: 9px 16px; border: none; border-radius: 6px; }"
            "QPushButton:hover { background-color: #8e67d2; }")
        self.btn_spec.clicked.connect(self.open_spectrometer)
        h_head.addWidget(self.btn_spec)
        root.addLayout(h_head)

        tabs = QtWidgets.QTabWidget()
        root.addWidget(tabs)

        # ============ Tab 1: Manual control ============
        manual = QtWidgets.QWidget()
        h = QtWidgets.QHBoxLayout(manual)
        h.setContentsMargins(12, 14, 12, 12)
        h.setSpacing(14)

        # Lock-in column: two independent SR865A panels (A and B)
        lock_col = QtWidgets.QVBoxLayout()
        lock_col.setSpacing(14)
        for lbl_txt, lw in (("Lock-In A (SR865A)", self.lockin_a),
                            ("Lock-In B (SR865A)", self.lockin_b)):
            box = QtWidgets.QGroupBox(lbl_txt)
            bl = QtWidgets.QVBoxLayout(box)
            bl.setContentsMargins(12, 16, 12, 12)
            bl.addWidget(lw)
            box.setMinimumWidth(470)
            box.setMaximumWidth(560)
            lock_col.addWidget(box)
        lock_col.addStretch()
        lock_scroll = QtWidgets.QScrollArea()
        lock_scroll.setWidgetResizable(True)
        lock_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        lock_scroll.setHorizontalScrollBarPolicy(
            QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        lock_scroll.setMinimumWidth(500)
        lock_scroll.setMaximumWidth(590)
        _lc = QtWidgets.QWidget(); _lc.setLayout(lock_col)
        lock_scroll.setWidget(_lc)
        h.addWidget(lock_scroll)

        # Stages column
        stages = QtWidgets.QVBoxLayout()
        stages.setSpacing(14)
        stages.addWidget(self.pump_stage)
        stages.addWidget(self.probe_stage)
        stages.addStretch()
        h.addLayout(stages, 1)

        tabs.addTab(manual, "Manual Control")

        # ============ Tab 2: Pump-probe scan ============
        scan_tab = QtWidgets.QWidget()
        sv = QtWidgets.QVBoxLayout(scan_tab)
        sv.setContentsMargins(12, 14, 12, 12)
        sv.setSpacing(10)

        # Which stage drives the scan
        pick_box = QtWidgets.QGroupBox("Scan Stage")
        pick = QtWidgets.QHBoxLayout(pick_box)
        pick.setContentsMargins(12, 16, 12, 10)
        lbl = QtWidgets.QLabel("Delay stage driven by the scan:")
        pick.addWidget(lbl)
        self.combo_stage = QtWidgets.QComboBox()
        self.combo_stage.addItems(["Pump delay stage", "Probe delay stage"])
        self.combo_stage.setMinimumWidth(200)
        self.combo_stage.currentIndexChanged.connect(self._update_scan_stage)
        pick.addWidget(self.combo_stage)
        pick.addStretch()
        sv.addWidget(pick_box)

        self.scan_widget = PumpProbeScanWidget(
            delay_stage=self.pump_stage.stage,
            lockin_widgets=[self.lockin_a, self.lockin_b])
        self.scan_widget.scan_started.connect(self._on_scan_started)
        self.scan_widget.scan_finished.connect(self._on_scan_finished)
        sv.addWidget(self.scan_widget, 1)

        # a persisted per-stage t0 (set/restored on a stage panel) flows into the
        # scan and spectrometer t0 fields
        self.pump_stage.zero_changed.connect(
            lambda z: self._on_stage_zero(self.pump_stage, z))
        self.probe_stage.zero_changed.connect(
            lambda z: self._on_stage_zero(self.probe_stage, z))

        tabs.addTab(scan_tab, "Pump-Probe Scan")

    def open_spectrometer(self):
        """Open (or raise) the separate Ocean Optics spectrometer window."""
        from spectrometer_window import SpectrometerWindow
        if getattr(self, "spec_window", None) is None:
            self.spec_window = SpectrometerWindow(
                stage_widgets=[self.pump_stage, self.probe_stage], parent=None)
        self.spec_window.show()
        self.spec_window.raise_()
        self.spec_window.activateWindow()

    def _scan_stage_widget(self):
        return self.pump_stage if self.combo_stage.currentIndex() == 0 else self.probe_stage

    def _update_scan_stage(self, idx):
        self.scan_widget.set_delay_stage(
            self.pump_stage.stage if idx == 0 else self.probe_stage.stage)

    def _on_stage_zero(self, stage_widget, z):
        """A stage's t0 was set/restored — mirror it into the scan and the
        spectrometer t0 fields if they're driving that stage."""
        if self._scan_stage_widget() is stage_widget:
            self.scan_widget.set_zero_mm(z)
        sw = getattr(self, "spec_window", None)
        if sw is not None and sw._current_stage_widget() is stage_widget:
            sw.set_zero_mm(z)

    def _on_scan_started(self):
        # Pause the driven stage panel's polling so only the worker uses the stage
        self._scanning_widget = self._scan_stage_widget()
        self._scanning_widget.set_scan_active(True)

    def _on_scan_finished(self):
        w = getattr(self, "_scanning_widget", None)
        if w is not None:
            w.set_scan_active(False)
            self._scanning_widget = None

    def closeEvent(self, event):
        # stop the scan state machine + let any in-flight lock-in read finish
        self.scan_widget._scanning = False
        self.scan_widget._scan_move_timer.stop()
        reader = self.scan_widget._reader
        if reader is not None and reader.isRunning():
            reader.wait(2000)
        if getattr(self, "spec_window", None) is not None:
            try:
                self.spec_window.close()
                self.spec_window.spec.disconnect()
            except Exception:
                pass
        for w in (self.pump_stage, self.probe_stage):
            try:
                w._move_timer.stop()
                w.timer.stop()
                w.stage.disconnect()
            except Exception:
                pass
        for lw in (self.lockin_a, self.lockin_b):
            try:
                lw.lockin.disconnect()
            except Exception:
                pass
        event.accept()


def main():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    style.apply_theme(app)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
