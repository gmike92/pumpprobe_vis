"""
metadata_widget.py
==================
Experiment-metadata entry panel.

Free-form-ish fields describing the measurement conditions (pump power,
wavelengths, sample, notes, ...). ``get_metadata()`` returns a plain dict that
the scan widget merges with auto-captured hardware settings and writes into the
saved ``.npz`` and a JSON sidecar, so every saved scan is self-describing.
"""

from pyqtgraph.Qt import QtWidgets


class MetadataWidget(QtWidgets.QGroupBox):
    """Panel of experiment metadata fields."""

    def __init__(self, title="Experiment Metadata"):
        super().__init__(title)
        form = QtWidgets.QFormLayout(self)
        form.setContentsMargins(6, 6, 6, 6)

        self.txt_sample = QtWidgets.QLineEdit("sample")
        form.addRow("Sample name:", self.txt_sample)

        self.spin_pump_power = QtWidgets.QDoubleSpinBox()
        self.spin_pump_power.setRange(0.0, 1e6)
        self.spin_pump_power.setDecimals(3)
        self.spin_pump_power.setSuffix(" mW")
        form.addRow("Pump power:", self.spin_pump_power)

        self.spin_pump_wl = QtWidgets.QDoubleSpinBox()
        self.spin_pump_wl.setRange(0.0, 1e6)
        self.spin_pump_wl.setDecimals(1)
        self.spin_pump_wl.setSuffix(" nm")
        form.addRow("Pump wavelength:", self.spin_pump_wl)

        self.spin_probe_wl = QtWidgets.QDoubleSpinBox()
        self.spin_probe_wl.setRange(0.0, 1e6)
        self.spin_probe_wl.setDecimals(1)
        self.spin_probe_wl.setSuffix(" nm")
        form.addRow("Probe wavelength:", self.spin_probe_wl)

        self.spin_chopper = QtWidgets.QDoubleSpinBox()
        self.spin_chopper.setRange(0.0, 1e6)
        self.spin_chopper.setDecimals(2)
        self.spin_chopper.setSuffix(" Hz")
        form.addRow("Chopper/mod freq:", self.spin_chopper)

        self.spin_temp = QtWidgets.QDoubleSpinBox()
        self.spin_temp.setRange(0.0, 1e5)
        self.spin_temp.setDecimals(2)
        self.spin_temp.setSuffix(" K")
        self.spin_temp.setValue(295.0)
        form.addRow("Temperature:", self.spin_temp)

        self.txt_notes = QtWidgets.QPlainTextEdit()
        self.txt_notes.setPlaceholderText("Free-text notes...")
        self.txt_notes.setMaximumHeight(70)
        form.addRow("Notes:", self.txt_notes)

    def sample_name(self):
        return self.txt_sample.text().strip() or "sample"

    def get_metadata(self):
        """Return metadata as a plain dict (str/float values)."""
        return {
            "sample_name": self.sample_name(),
            "pump_power_mW": self.spin_pump_power.value(),
            "pump_wavelength_nm": self.spin_pump_wl.value(),
            "probe_wavelength_nm": self.spin_probe_wl.value(),
            "chopper_freq_Hz": self.spin_chopper.value(),
            "temperature_K": self.spin_temp.value(),
            "notes": self.txt_notes.toPlainText().strip(),
        }
