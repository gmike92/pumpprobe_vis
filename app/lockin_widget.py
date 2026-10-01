"""
lockin_widget.py
================
Lock-in control GUI for the SR865A.

This is the lock-in GUI taken from
https://github.com/gmike92/Labview-pumprobepython  (sub_lockin_lw.py),
lightly extended to expose the sensitivity/frequency readback and the
buffered-capture sample count used by the scan. Nothing else from that repo
(camera / TWINS / k-space) is included.
"""

from pyqtgraph.Qt import QtCore, QtWidgets
import pyqtgraph as pg

from driver_lockin import LockInDriver, CH_R, CH_THETA


class LockInControlWidget(QtWidgets.QWidget):
    """Sub-UI for controlling the SR865A lock-in amplifier."""

    def __init__(self, dev_id="A", resource="USB0::0xB506::0x2000::004418::INSTR"):
        super().__init__()
        self.dev_id = str(dev_id)
        self.lockin = LockInDriver(self.dev_id)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(9)

        # --- Output channel selector ---
        h1 = QtWidgets.QHBoxLayout()
        h1.addWidget(QtWidgets.QLabel("Scan chan:"))
        self.combo = QtWidgets.QComboBox()
        self.combo.addItems(["X (0)", "Y (1)", "R (2)", "Theta (3)"])
        self.combo.setCurrentIndex(2)  # R by default
        h1.addWidget(self.combo)
        layout.addLayout(h1)

        # --- Connection row ---
        h2 = QtWidgets.QHBoxLayout()
        self.lbl_status = QtWidgets.QLabel("Not Conn")
        h2.addWidget(self.lbl_status)
        self.txt_resource = QtWidgets.QLineEdit(resource)
        self.btn_scan = QtWidgets.QPushButton("Find")
        self.btn_scan.setToolTip("Scan VISA bus for an SR865A")
        self.btn_scan.clicked.connect(self.scan_bus)
        h2.addWidget(self.btn_scan)
        self.btn_connect = QtWidgets.QPushButton("Connect")
        self.btn_connect.clicked.connect(self.connect_lockin)
        h2.addWidget(self.btn_connect)
        layout.addLayout(h2)

        # --- Harmonic ---
        h_harm = QtWidgets.QHBoxLayout()
        h_harm.addWidget(QtWidgets.QLabel("Harm:"))
        self.spin_harm = QtWidgets.QSpinBox()
        self.spin_harm.setRange(1, 99)
        self.spin_harm.setValue(1)
        self.spin_harm.valueChanged.connect(self.set_harmonic)
        h_harm.addWidget(self.spin_harm)
        h_harm.addWidget(QtWidgets.QLabel("Freq:"))
        self.lbl_freq = QtWidgets.QLabel("-")
        h_harm.addWidget(self.lbl_freq)
        h_harm.addStretch()
        layout.addLayout(h_harm)

        # --- Time constant ---
        h_tc = QtWidgets.QHBoxLayout()
        h_tc.addWidget(QtWidgets.QLabel("TC:"))
        self.combo_tc = QtWidgets.QComboBox()
        self.tc_values = [
            1e-6, 3e-6, 10e-6, 30e-6, 100e-6, 300e-6,
            1e-3, 3e-3, 10e-3, 30e-3, 100e-3, 300e-3,
            1.0, 3.0, 10.0, 30.0]
        self.tc_labels = [
            "1 us", "3 us", "10 us", "30 us", "100 us", "300 us",
            "1 ms", "3 ms", "10 ms", "30 ms", "100 ms", "300 ms",
            "1 s", "3 s", "10 s", "30 s"]
        self.combo_tc.addItems(self.tc_labels)
        self.combo_tc.setCurrentIndex(9)  # 30 ms
        self.combo_tc.currentIndexChanged.connect(self.set_time_constant)
        h_tc.addWidget(self.combo_tc)
        layout.addLayout(h_tc)

        self.lbl_sens = QtWidgets.QLabel("Sens: -")
        layout.addWidget(self.lbl_sens)

        # --- Live monitor ---
        h_live = QtWidgets.QHBoxLayout()
        self.chk_live = QtWidgets.QCheckBox("Live Monitor")
        self.chk_live.stateChanged.connect(self.toggle_live)
        h_live.addWidget(self.chk_live)
        self.lbl_live_val = QtWidgets.QLabel("R: - | Th: -")
        h_live.addWidget(self.lbl_live_val)
        layout.addLayout(h_live)

        self.timer_live = QtCore.QTimer()
        self.timer_live.setInterval(300)
        self.timer_live.timeout.connect(self.update_live_readout)

        self.plot_live = pg.PlotWidget(title="Live Monitor (R)")
        self.plot_live.setLabel("left", "Magnitude", "V")
        self.plot_live.setLabel("bottom", "Time steps")
        self.plot_live.showGrid(x=True, y=True, alpha=0.25)
        self.plot_live.setMinimumHeight(220)
        self.curve_live = self.plot_live.plot(pen=pg.mkPen("#ffb74d", width=2))
        layout.addWidget(self.plot_live)

        self.live_data = []
        self.update_info()

    # ------------------------------------------------------------------ #
    def scan_bus(self):
        res = LockInDriver.list_resources()
        if res:
            self.txt_resource.setText(res[0])
            self.lbl_status.setText(f"Found {len(res)}")
        else:
            self.lbl_status.setText("None found")

    def connect_lockin(self):
        res = self.txt_resource.text().strip()
        if self.lockin.connect(res):
            self.lbl_status.setText("Connected")
            self.update_info()
        else:
            self.lbl_status.setText("Conn Failed")

    def update_info(self):
        if self.lockin.is_connected:
            try:
                val = self.lockin.get_time_constant()
                idx = min(range(len(self.tc_values)),
                          key=lambda i: abs(self.tc_values[i] - val))
                self.combo_tc.blockSignals(True)
                self.combo_tc.setCurrentIndex(idx)
                self.combo_tc.blockSignals(False)
            except Exception:
                pass
            sens = self.lockin.get_sensitivity()
            self.lbl_sens.setText(f"Sens: {sens:.2e} V" if sens else "Sens: -")
            freq = self.lockin.get_frequency()
            self.lbl_freq.setText(f"{freq:.3f} Hz" if freq else "-")
            self.lbl_status.setText("Connected")
        else:
            self.lbl_status.setText("Not Conn")
            self.lbl_sens.setText("Sens: -")

    def set_harmonic(self, val):
        self.lockin.set_harmonic(val)

    def set_time_constant(self, idx):
        if 0 <= idx < len(self.tc_values):
            self.lockin.set_time_constant(self.tc_values[idx])

    def get_time_constant(self):
        return self.lockin.get_time_constant()

    def get_channel_code(self):
        return self.combo.currentIndex()

    def get_channel_name(self):
        return self.combo.currentText().split(" ")[0]

    # ------------------------------------------------------------------ #
    def toggle_live(self, _state):
        if self.chk_live.isChecked():
            self.live_data = []
            self.curve_live.setData([])
            self.timer_live.start()
        else:
            self.timer_live.stop()
            self.lbl_live_val.setText("R: - | Th: -")

    def update_live_readout(self):
        if not self.lockin.is_connected:
            return
        try:
            r = self.lockin.read_value(CH_R, samples=1)
            th = self.lockin.read_value(CH_THETA, samples=1)
            self.lbl_live_val.setText(f"R: {r:.4e} V | Th: {th:.2f} deg")
            self.live_data.append(r)
            if len(self.live_data) > 300:
                self.live_data.pop(0)
            self.curve_live.setData(self.live_data)
        except Exception:
            pass
