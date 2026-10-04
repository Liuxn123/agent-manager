from pathlib import Path

from PySide6.QtGui import QFont, QFontDatabase


def setup_theme(app) -> None:
    """Bundle a CJK font so fresh machines and offscreen QA stay readable."""
    font = Path(__file__).resolve().parents[1] / "assets/fonts/NotoSansSC-Regular.otf"
    identity = QFontDatabase.addApplicationFont(str(font))
    families = QFontDatabase.applicationFontFamilies(identity) if identity >= 0 else []
    if families:
        app.setFont(QFont(families[0], 10))
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)


STYLE = """
QWidget { font-size: 13px; color: #24323d; }
QMainWindow, QWidget#Content { background: #f4f6f8; }
QWidget#Sidebar { background: #182d39; }
QLabel#Brand { color: #f4f8fa; font-size: 22px; font-weight: 700; }
QLabel#BrandSub { color: #9eb5c2; font-size: 12px; }
QListWidget#Navigation { background: transparent; border: none; color: #c4d2d9; outline: none; }
QListWidget#Navigation::item { padding: 14px 12px; margin: 3px 0px; border-radius: 7px; }
QListWidget#Navigation::item:selected { background: #2c4958; color: white; }
QListWidget#Navigation::item:hover { background: #243e4c; }
QLabel#Title { font-size: 25px; font-weight: 700; }
QLabel#Subtitle { color: #647583; }
QLabel#CardValue { font-size: 29px; font-weight: 700; color: #245b50; }
QFrame#Card { background: white; border: 1px solid #e0e6ea; border-radius: 10px; }
QPushButton { background: white; border: 1px solid #d3dce2; padding: 8px 13px; border-radius: 6px; }
QPushButton:hover { background: #edf3f5; border-color: #8daab8; }
QPushButton:pressed { background: #e0eaed; }
QPushButton:disabled { color: #a2abb2; background: #f0f2f4; border-color: #e2e6e9; }
QPushButton[primary="true"] { background: #296454; border-color: #296454; color: white; }
QPushButton[primary="true"]:hover { background: #357b68; }
QPushButton[primary="true"]:disabled { background: #a6bdb6; border-color: #a6bdb6; }
QPushButton[danger="true"] { color: #a43e3e; }
QLineEdit, QComboBox, QSpinBox { background: white; border: 1px solid #d3dce2; border-radius: 5px; padding: 7px; min-height: 20px; }
QLineEdit:focus, QComboBox:focus { border-color: #4b8c79; }
QComboBox QAbstractItemView { background: white; selection-background-color: #e0eee8; selection-color: #24323d; }
QTableWidget { background: white; alternate-background-color: #f9fafb; border: 1px solid #e0e6ea; border-radius: 7px; gridline-color: #edf1f4; selection-background-color: #e3eee9; selection-color: #24323d; }
QHeaderView::section { background: #f5f7f8; border: none; border-bottom: 1px solid #e0e6ea; padding: 10px; color: #627380; font-weight: 600; }
QTextEdit, QTextBrowser { background: white; border: 1px solid #e0e6ea; border-radius: 7px; padding: 10px; }
QScrollArea { border: none; background: transparent; }
QDialog { background: #f4f6f8; }
QDialogButtonBox QPushButton { min-width: 70px; }
QProgressBar { border: none; background: #e6eeeb; border-radius: 3px; max-height: 5px; }
QProgressBar::chunk { background: #4b8c79; }
QStatusBar { background: white; border-top: 1px solid #e0e6ea; color: #627380; }
QCheckBox { spacing: 7px; }
QSplitter::handle { background: transparent; width: 10px; height: 10px; }
"""
