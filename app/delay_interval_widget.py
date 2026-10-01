"""
delay_interval_widget.py
========================
Multi-zone delay-scan point generator with an arbitrary number of zones and an
fs / mm unit toggle (as in the reference GUI).

Cleaned from ``sub_interval_lw.py`` of
https://github.com/gmike92/Labview-pumprobepython, but the fixed 3 zones are
replaced by an add/remove list so a scan can have as many contiguous zones as
needed (dense near t0, progressively coarser in the tail).

Units: values may be entered in **fs** (femtoseconds of pump-probe delay) or in
**mm** (of stage travel). ``get_scan_points_fs()`` always returns fs; the scan
worker turns fs into an absolute stage position via the stage's own
``fs_to_mm`` (which knows about single/double pass). The mm<->fs factor used for
*display* is supplied by the scan widget from the selected stage via
``set_mm_per_fs()``, so the mm shown matches the stage that will actually move.

Each zone's Start is chained to the previous zone's Stop (read-only) so the
zones stay contiguous; only the very first Start is freely editable.
"""

from pyqtgraph.Qt import QtWidgets
import numpy as np

# mm of stage travel per fs of delay, double-pass (retro) default.
# = c/2 with c = 0.000299792458 mm/fs. Overridden per-stage via set_mm_per_fs().
_DEFAULT_MM_PER_FS = 0.000299792458 / 2.0


class _ZoneRow:
    """One (start, stop, step) row in the zone table."""

    def __init__(self, start, stop, step, first=False):
        self.first = first
        self.start = self._spin(start, read_only=not first)
        self.stop = self._spin(stop)
        self.step = self._spin(step, is_step=True)
        # read-only label showing the step in the *other* unit (fs<->mm)
        self.info = QtWidgets.QLabel("")
        self.info.setStyleSheet("color: #9aa0a6;")

    @staticmethod
    def _spin(value, is_step=False, read_only=False):
        sp = QtWidgets.QDoubleSpinBox()
        # generous limits so the spinbox never caps a full-travel scan
        # (1e7 fs = 10 ns, well beyond any real delay stage)
        sp.setRange(1e-5 if is_step else -1e7, 1e7)
        sp.setDecimals(1)
        sp.setMinimumWidth(96)   # room for a signed 6-decimal mm value
        sp.setValue(value)
        if read_only:
            sp.setReadOnly(True)
            sp.setButtonSymbols(QtWidgets.QAbstractSpinBox.ButtonSymbols.NoButtons)
            sp.setStyleSheet("color: #888;")
        return sp

    def widgets(self):
        return (self.start, self.stop, self.step)


class DelayIntervalWidget(QtWidgets.QWidget):
    """N-zone delay interval configuration with fs/mm toggle. Emits fs points."""

    def __init__(self):
        super().__init__()
        self.zones = []
        self.current_unit = "fs"
        self.mm_per_fs = _DEFAULT_MM_PER_FS
        self._setup_ui()

    def _setup_ui(self):
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # --- Units row ---
        h_units = QtWidgets.QHBoxLayout()
        h_units.addWidget(QtWidgets.QLabel("Units:"))
        self.combo_units = QtWidgets.QComboBox()
        self.combo_units.addItems(["fs", "mm"])
        self.combo_units.currentTextChanged.connect(self._on_unit_changed)
        h_units.addWidget(self.combo_units)
        h_units.addStretch()
        layout.addLayout(h_units)

        self.gb = QtWidgets.QGroupBox("Scan Intervals (fs)")
        gbl = QtWidgets.QVBoxLayout(self.gb)

        self.grid = QtWidgets.QGridLayout()
        self.grid.setContentsMargins(5, 5, 5, 5)
        self.grid.addWidget(QtWidgets.QLabel(""), 0, 0)
        self.lbl_start = QtWidgets.QLabel("Start")
        self.lbl_stop = QtWidgets.QLabel("Stop")
        self.lbl_step = QtWidgets.QLabel("Step")
        self.lbl_conv = QtWidgets.QLabel("step in fs")
        self.lbl_conv.setStyleSheet("color: #9aa0a6;")
        self.grid.addWidget(self.lbl_start, 0, 1)
        self.grid.addWidget(self.lbl_stop, 0, 2)
        self.grid.addWidget(self.lbl_step, 0, 3)
        self.grid.addWidget(self.lbl_conv, 0, 4)
        self.grid.setColumnStretch(4, 1)
        gbl.addLayout(self.grid)

        h_btn = QtWidgets.QHBoxLayout()
        self.btn_add = QtWidgets.QPushButton("+ Add zone")
        self.btn_add.clicked.connect(self.add_zone)
        self.btn_del = QtWidgets.QPushButton("- Remove zone")
        self.btn_del.clicked.connect(self.remove_zone)
        h_btn.addWidget(self.btn_add)
        h_btn.addWidget(self.btn_del)
        gbl.addLayout(h_btn)

        layout.addWidget(self.gb)

        self.lbl_count = QtWidgets.QLabel("Points: --")
        self.lbl_count.setStyleSheet("font-weight: bold;")
        layout.addWidget(self.lbl_count)

        # Sensible defaults (fs): one fine zone, then a coarser one
        self.add_zone(start=-1000.0, stop=0.0, step=10.0)
        self.add_zone(stop=2000.0, step=20.0)

    # ------------------------------------------------------------------ #
    # Stage-aware conversion factor
    # ------------------------------------------------------------------ #
    def set_mm_per_fs(self, mm_per_fs):
        """Set the mm-of-travel per fs-of-delay used for the mm display."""
        if mm_per_fs and mm_per_fs > 0:
            self.mm_per_fs = float(mm_per_fs)

    def _to_fs(self, value):
        """Convert a spinbox value (in current unit) to fs."""
        return value / self.mm_per_fs if self.current_unit == "mm" else value

    # ------------------------------------------------------------------ #
    # Zones
    # ------------------------------------------------------------------ #
    def add_zone(self, start=None, stop=None, step=None):
        first = len(self.zones) == 0
        if start is None:
            start = self.zones[-1].stop.value() if self.zones else 0.0
        if stop is None:
            stop = start + (1000.0 if self.current_unit == "fs" else 0.15)
        if step is None:
            step = 100.0 if self.current_unit == "fs" else 0.015

        zone = _ZoneRow(start, stop, step, first=first)
        self._apply_unit_to_row(zone)
        row = len(self.zones) + 1  # +1 for header row
        self.grid.addWidget(QtWidgets.QLabel(f"Zone {row}:"), row, 0)
        self.grid.addWidget(zone.start, row, 1)
        self.grid.addWidget(zone.stop, row, 2)
        self.grid.addWidget(zone.step, row, 3)
        self.grid.addWidget(zone.info, row, 4)

        for sp in zone.widgets():
            sp.valueChanged.connect(self._on_change)
        self.zones.append(zone)
        self._on_change()

    def remove_zone(self):
        if len(self.zones) <= 1:
            return  # always keep at least one zone
        self.zones.pop()
        row = len(self.zones) + 1
        for col in range(5):
            item = self.grid.itemAtPosition(row, col)
            if item and item.widget():
                item.widget().setParent(None)
        self._on_change()

    def _on_change(self):
        self.update_continuity()
        self._update_conversions()
        self._update_count()

    def _update_conversions(self):
        """Show each zone's step converted to the other unit (fs<->mm)."""
        for zone in self.zones:
            step = zone.step.value()
            if self.current_unit == "mm":
                fs = step / self.mm_per_fs if self.mm_per_fs else 0.0
                zone.info.setText(f"= {fs:,.1f} fs")
            else:
                zone.info.setText(f"= {step * self.mm_per_fs:.6f} mm")

    def update_continuity(self):
        """Chain each zone's Start to the previous zone's Stop."""
        for i in range(1, len(self.zones)):
            prev_stop = self.zones[i - 1].stop.value()
            sp = self.zones[i].start
            sp.blockSignals(True)
            sp.setValue(prev_stop)
            sp.blockSignals(False)

    # ------------------------------------------------------------------ #
    # Units
    # ------------------------------------------------------------------ #
    def _apply_unit_to_row(self, zone):
        """Set spin decimals/step size appropriate to the current unit."""
        if self.current_unit == "mm":
            dec, single = 6, 0.0001
        else:
            dec, single = 1, 10.0
        for sp in zone.widgets():
            sp.setDecimals(dec)
            sp.setSingleStep(single)

    def _on_unit_changed(self, new_unit):
        if new_unit == self.current_unit:
            return
        # factor to convert current displayed values -> new unit
        if new_unit == "mm":      # fs -> mm
            factor = self.mm_per_fs
        else:                     # mm -> fs
            factor = 1.0 / self.mm_per_fs
        self.current_unit = new_unit

        for zone in self.zones:
            # Read values BEFORE changing decimals (lowering decimals would
            # round a small mm value to 0 and lose the conversion).
            vals = [sp.value() for sp in zone.widgets()]
            self._apply_unit_to_row(zone)
            for sp, v in zip(zone.widgets(), vals):
                sp.blockSignals(True)
                sp.setValue(v * factor)
                sp.blockSignals(False)

        self.gb.setTitle(f"Scan Intervals ({new_unit})")
        for lbl, base in ((self.lbl_start, "Start"), (self.lbl_stop, "Stop"),
                          (self.lbl_step, "Step")):
            lbl.setText(f"{base} ({new_unit})")
        self.lbl_conv.setText("step in fs" if new_unit == "mm" else "step in mm")
        self._on_change()

    # ------------------------------------------------------------------ #
    # Output
    # ------------------------------------------------------------------ #
    def is_absolute_mm(self):
        """True when the zone values are ABSOLUTE stage positions (mm mode)."""
        return self.current_unit == "mm"

    def _raw_points(self):
        """Sorted, de-duplicated zone points in the CURRENT display unit
        (fs = delay relative to t0; mm = absolute stage position)."""
        points = []
        for zone in self.zones:
            start, stop, step = (zone.start.value(), zone.stop.value(),
                                 zone.step.value())
            if step <= 0 or stop == start:
                continue
            n = int(round(abs(stop - start) / step))
            if n <= 0:
                continue
            points.extend(np.linspace(start, stop, n + 1).tolist())
        if not points:
            return np.array([])
        return np.unique(np.round(points, 6))

    def get_scan_points_fs(self):
        """Delay points in fs (only meaningful in fs mode; used for counting)."""
        pts = self._raw_points()
        if pts.size == 0:
            return pts
        return pts / self.mm_per_fs if self.current_unit == "mm" else pts

    def get_scan_plan(self, stage, zero_mm, invert_stage=False):
        """Return the scan as a list of ``(target_mm, delay_fs)`` tuples,
        honouring the current unit's meaning:

        * **fs mode** — values are delays relative to t0; the stage target is
          ``t0 ± fs_to_mm(delay)``.
        * **mm mode** — values are ABSOLUTE stage positions; the stage goes there
          exactly and the delay is computed as the travel away from t0.
        """
        sign = -1.0 if invert_stage else 1.0
        pts = self._raw_points()
        plan = []
        if self.current_unit == "mm":
            for pos in pts:                       # pos = absolute stage mm
                delay_fs = sign * stage.mm_to_fs(pos - zero_mm)
                plan.append((float(pos), float(delay_fs)))
        else:
            for delay_fs in pts:                  # delay relative to t0
                target = zero_mm + sign * stage.fs_to_mm(delay_fs)
                plan.append((float(target), float(delay_fs)))
        return plan

    def get_zone_config(self):
        """Return the zone table (canonical fs) + the current display unit."""
        return {
            "unit": self.current_unit,
            "mm_per_fs": self.mm_per_fs,
            "zones_fs": [{"start_fs": self._to_fs(z.start.value()),
                          "stop_fs": self._to_fs(z.stop.value()),
                          "step_fs": self._to_fs(z.step.value())}
                         for z in self.zones],
        }

    def set_zone_config(self, config):
        """Rebuild the zone table from get_zone_config() output (fs canonical)."""
        # Work in fs while rebuilding, then restore the saved display unit.
        self.combo_units.setCurrentText("fs")
        zones = config.get("zones_fs", []) if isinstance(config, dict) else []
        while len(self.zones) > 1:
            self.remove_zone()
        if zones:
            first = zones[0]
            self.zones[0].start.setValue(first.get("start_fs", -1000.0))
            self.zones[0].stop.setValue(first.get("stop_fs", 0.0))
            self.zones[0].step.setValue(first.get("step_fs", 10.0))
            for z in zones[1:]:
                self.add_zone(z.get("start_fs"), z.get("stop_fs"), z.get("step_fs"))
        if isinstance(config, dict) and config.get("unit") == "mm":
            self.combo_units.setCurrentText("mm")
        self._on_change()

    def _update_count(self):
        pts = self._raw_points()
        n = len(pts)
        if n == 0:
            self.lbl_count.setText("Points: 0")
        elif self.current_unit == "mm":
            self.lbl_count.setText(
                f"Points: {n}  |  stage {pts[0]:.4f} … {pts[-1]:.4f} mm (absolute)")
        else:
            self.lbl_count.setText(
                f"Points: {n}  |  delay {pts[0]:+.0f} … {pts[-1]:+.0f} fs (from t0)")
