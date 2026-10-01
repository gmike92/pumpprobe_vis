"""
style.py
========
Central place for the application's look & feel: a readable dark theme, a
sensible default font, and matching pyqtgraph plot colors. Call
``apply_theme(app)`` once on the QApplication so every widget inherits it.

Palette (accessible on the #1e1e1e background):
    bg      #1e1e1e   panels    #262626   border  #3c3c3c
    text    #e8e8e8   muted     #9aa0a6
    accents blue #4aa3ff  green #4caf82  amber #ffb74d  red #ef5350
"""

from pyqtgraph.Qt import QtGui
import pyqtgraph as pg

FONT_FAMILY = "Segoe UI"
FONT_PT = 11

# named accents reused by widgets for status text etc.
BLUE = "#4aa3ff"
GREEN = "#4caf82"
AMBER = "#ffb74d"
RED = "#ef5350"
MUTED = "#9aa0a6"

QSS = """
* { outline: 0; }

QWidget {
    background-color: #1e1e1e;
    color: #e8e8e8;
    font-family: "Segoe UI";
    font-size: 14px;
}

QMainWindow, QScrollArea, QScrollArea > QWidget > QWidget {
    background-color: #1e1e1e;
}

/* ---- Group boxes ---- */
QGroupBox {
    background-color: #232323;
    border: 1px solid #3c3c3c;
    border-radius: 8px;
    margin-top: 16px;
    padding: 10px 10px 8px 10px;
    font-weight: 600;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 12px;
    padding: 2px 8px;
    color: #4aa3ff;
    background-color: transparent;
}

/* ---- Labels ---- */
QLabel { background: transparent; }

/* ---- Buttons ---- */
QPushButton {
    background-color: #333333;
    color: #e8e8e8;
    border: 1px solid #4a4a4a;
    border-radius: 6px;
    padding: 7px 14px;
    min-height: 20px;
}
QPushButton:hover { background-color: #3f3f3f; border-color: #5a5a5a; }
QPushButton:pressed { background-color: #2b2b2b; }
QPushButton:disabled { color: #6a6a6a; background-color: #262626; border-color: #383838; }

/* ---- Inputs ---- */
QLineEdit, QDoubleSpinBox, QSpinBox, QComboBox, QPlainTextEdit, QTextEdit {
    background-color: #2a2a2a;
    color: #e8e8e8;
    border: 1px solid #3c3c3c;
    border-radius: 6px;
    padding: 5px 8px;
    min-height: 22px;
    selection-background-color: #4aa3ff;
    selection-color: #10151b;
}
QLineEdit:focus, QDoubleSpinBox:focus, QSpinBox:focus, QComboBox:focus,
QPlainTextEdit:focus, QTextEdit:focus {
    border: 1px solid #4aa3ff;
}
QLineEdit:read-only { color: #9aa0a6; background-color: #242424; }

QComboBox::drop-down {
    subcontrol-origin: padding; subcontrol-position: center right;
    width: 22px; border-left: 1px solid #3c3c3c;
}
QComboBox QAbstractItemView {
    background-color: #2a2a2a; color: #e8e8e8;
    border: 1px solid #4a4a4a; selection-background-color: #4aa3ff;
    selection-color: #10151b; outline: 0;
}
QDoubleSpinBox::up-button, QSpinBox::up-button,
QDoubleSpinBox::down-button, QSpinBox::down-button {
    width: 18px; background-color: #333333; border: none;
}
QDoubleSpinBox::up-button:hover, QSpinBox::up-button:hover,
QDoubleSpinBox::down-button:hover, QSpinBox::down-button:hover {
    background-color: #444444;
}

/* ---- Check boxes ---- */
QCheckBox { spacing: 8px; padding: 2px; background: transparent; }
QCheckBox::indicator {
    width: 18px; height: 18px; border-radius: 4px;
    border: 1px solid #5a5a5a; background-color: #2a2a2a;
}
QCheckBox::indicator:checked {
    background-color: #4aa3ff; border-color: #4aa3ff;
}

/* ---- Progress bar ---- */
QProgressBar {
    border: 1px solid #3c3c3c; border-radius: 6px;
    background-color: #262626; text-align: center; min-height: 20px; color: #e8e8e8;
}
QProgressBar::chunk { background-color: #4caf82; border-radius: 5px; }

/* ---- Tabs ---- */
QTabWidget::pane { border: 1px solid #3c3c3c; border-radius: 8px; top: -1px; }
QTabBar::tab {
    background: #262626; color: #b8b8b8;
    padding: 11px 26px; margin-right: 2px; min-width: 150px;
    border: 1px solid #3c3c3c; border-bottom: none;
    border-top-left-radius: 8px; border-top-right-radius: 8px;
    font-size: 14px;
}
QTabBar::tab:selected { background: #232323; color: #4aa3ff; font-weight: 600; }
QTabBar::tab:hover { background: #2f2f2f; }

/* ---- Scrollbars ---- */
QScrollBar:vertical { background: #1e1e1e; width: 12px; margin: 0; }
QScrollBar::handle:vertical { background: #3c3c3c; border-radius: 6px; min-height: 30px; }
QScrollBar::handle:vertical:hover { background: #4a4a4a; }
QScrollBar:horizontal { background: #1e1e1e; height: 12px; margin: 0; }
QScrollBar::handle:horizontal { background: #3c3c3c; border-radius: 6px; min-width: 30px; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }

QToolTip {
    background-color: #2a2a2a; color: #e8e8e8;
    border: 1px solid #4a4a4a; padding: 5px; border-radius: 4px;
}
"""


def apply_theme(app):
    """Apply the font, stylesheet, and pyqtgraph colors to the whole app."""
    app.setFont(QtGui.QFont(FONT_FAMILY, FONT_PT))
    app.setStyleSheet(QSS)
    # softer plot canvas that sits with the dark theme
    pg.setConfigOption("background", "#161616")
    pg.setConfigOption("foreground", "#c8c8c8")
    pg.setConfigOptions(antialias=True)
