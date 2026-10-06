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
QWidget { font-size: 13px; color: #27334f; }
QMainWindow, QWidget#Content { background: #f5f6fb; }
QWidget#DashboardBody { background: #f5f6fb; }
QWidget#Sidebar { background: qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #101d38,stop:1 #17213b); }
QLabel#Brand { color: #ffffff; font-size: 23px; font-weight: 700; }
QLabel#BrandSub { color: #8e9abb; font-size: 11px; }
QListWidget#Navigation { background: transparent; border: none; color: #b3bdd5; outline: none; }
QListWidget#Navigation::item { padding: 8px 12px; margin: 2px 0px; border-radius: 6px; border: none; }
QListWidget#Navigation::item:selected { background: qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #2867df,stop:1 #244c9b); color: #ffffff; }
QListWidget#Navigation::item:hover { background: #222d4b; }
QListWidget#ActionList { background: #ffffff; border: none; }
QListWidget#ActionList::item { padding: 5px; border-bottom: 1px solid #edf0f6; }
QListWidget { background: #ffffff; border: 1px solid #e8ebf4; border-radius: 10px; outline: none; }
QListWidget::item { padding: 10px 12px; border-bottom: 1px solid #f0f2f7; }
QListWidget::item:selected { background: #eef1fe; color: #34499a; }
QLabel#Title { color: #17213b; font-size: 27px; font-weight: 700; }
QLabel#Subtitle { color: #7a849c; }
QLabel#SectionTitle { color: #27334f; font-size: 15px; font-weight: 600; }
QLabel#HeroTitle { color: #ffffff; font-size: 26px; font-weight: 700; }
QLabel#HeroNote { color: #cad5fc; }
QFrame#Hero { background: qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #293866,stop:1 #4a5d9a); border-radius: 14px; }
QLabel#CardValue { font-size: 31px; font-weight: 700; color: #3e55ad; }
QFrame#Card { background: #ffffff; border: 1px solid #e8ebf4; border-radius: 12px; }
QPushButton { background: #ffffff; border: 1px solid #dfe4ef; padding: 9px 15px; border-radius: 8px; min-height: 18px; }
QPushButton:hover { background: #edf0fc; border-color: #aab7e1; }
QPushButton:pressed { background: #dfe5f9; }
QPushButton:focus { border: 1px solid #647bd1; }
QPushButton:disabled { color: #a4acc0; background: #eff1f6; border-color: #e6e9f1; }
QPushButton[primary="true"] { background: #5368c5; border-color: #5368c5; color: #ffffff; }
QPushButton[primary="true"]:hover { background: #6479d5; }
QPushButton[primary="true"]:disabled { background: #b2bce0; border-color: #b2bce0; }
QPushButton[danger="true"] { color: #b85060; }
QLineEdit, QComboBox, QSpinBox, QDateEdit { background: #ffffff; border: 1px solid #dfe4ef; border-radius: 7px; padding: 8px; min-height: 20px; selection-background-color: #5368c5; }
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDateEdit:focus { border-color: #7184d0; }
QComboBox QAbstractItemView { background: #ffffff; selection-background-color: #edf0fc; selection-color: #27334f; }
QTableWidget { background: #ffffff; alternate-background-color: #fafbfe; border: 1px solid #e8ebf4; border-radius: 10px; gridline-color: #f0f2f7; selection-background-color: #eef1fe; selection-color: #34499a; outline: none; }
QTableWidget::item { padding: 4px 8px; border: none; }
QHeaderView::section { background: #f9faff; border: none; border-bottom: 1px solid #e8ebf4; padding: 12px 8px; color: #8590a8; font-weight: 500; }
QTextEdit, QTextBrowser { background: #ffffff; border: 1px solid #e8ebf4; border-radius: 10px; padding: 14px; }
QScrollArea { border: none; background: transparent; }
QScrollBar:vertical { background: transparent; width: 7px; margin: 2px; }
QScrollBar::handle:vertical { background: #ced5e7; min-height: 32px; border-radius: 3px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QDialog { background: #f5f6fb; }
QDialogButtonBox QPushButton { min-width: 74px; }
QProgressBar { border: none; background: #e3e7f5; border-radius: 3px; max-height: 5px; }
QProgressBar::chunk { background: #7489d8; }
QStatusBar { background: #ffffff; border-top: 1px solid #e8ebf4; color: #8590a8; padding: 3px; }
QCheckBox { spacing: 8px; }
QGroupBox { border: 1px solid #e2e7f1; border-radius: 9px; margin-top: 16px; padding-top: 15px; }
QGroupBox::title { subcontrol-origin: margin; padding: 0 8px; color: #687594; }
QTabWidget::pane { border: none; }
QTabBar::tab { padding: 9px 12px; color: #8490aa; border-bottom: 2px solid transparent; }
QTabBar::tab:selected { color: #5368c5; border-bottom: 2px solid #5368c5; }
QSplitter::handle { background: transparent; width: 12px; height: 12px; }
QFrame#TrendBar { background: #9dadE9; border-radius: 3px; }
QFrame#DirectoryCard { background: #ffffff; border: 1px solid #e2e7f1; border-radius: 8px; }
QTextEdit#DirectoryPath { border: none; padding: 0px; background: transparent; color: #394f92; }
QToolTip { background: #24314f; color: #ffffff; border: none; padding: 6px; }
QWidget#SettingsBody { background: #f5f6fb; }
QWidget#SettingsPage QLabel#Title { font-size: 22px; }
QWidget#SettingsPage QPushButton { border-radius: 3px; padding: 6px 12px; }
QWidget#SettingsPage QPushButton[primary="true"] { background: #4d5b76; border-color: #4d5b76; }
QWidget#SettingsPage QPushButton[primary="true"]:hover { background: #3c4860; }
QWidget#SettingsPage QLineEdit, QWidget#SettingsPage QSpinBox { border-radius: 3px; padding: 6px; }
QWidget#SettingsPage QTextEdit { border-radius: 3px; padding: 8px; }
QWidget#SettingsPage QToolButton { border: none; background: transparent; padding: 6px 0px; color: #4d5b76; }
QWidget#SettingsPage QToolButton:hover { color: #26334f; }
QFrame#SettingsCard { background: #ffffff; border: 1px solid #e8edf5; border-radius: 12px; }
QLabel#SettingsIcon { background: #edf4ff; color: #3979ff; border-radius: 12px; font-size: 22px; font-weight: 600; }
QLabel#SettingsCardTitle { color: #20385e; font-size: 16px; font-weight: 650; }
QWidget#SettingsStatusPanel QLabel#SettingsStatusTile { background: #f6f8fc; border: 1px solid #edf1f7; border-radius: 10px; padding: 12px; color: #425776; }
QWidget#SettingsPage QPushButton { border-radius: 8px; padding: 8px 13px; }
QWidget#SettingsPage QPushButton[primary="true"] { background: #3979ff; border-color: #3979ff; color: #ffffff; }
QWidget#SettingsPage QPushButton[primary="true"]:hover { background: #2866e8; }
QWidget#SettingsPage QLineEdit, QWidget#SettingsPage QSpinBox { border-radius: 7px; padding: 8px; }
QWidget#SettingsPage QToolButton { border-radius: 6px; padding: 7px 9px; }
QPushButton#SidebarHelp { background: transparent; color: #b3bdd5; border: 1px solid #34405c; border-radius: 3px; padding: 6px; }
QPushButton#SidebarHelp:hover { background: #222d4b; color: #ffffff; }
QWidget#TodayPage, QWidget#TodayBody { background: transparent; }
QLabel#TodayDate { color: #15294e; font-size: 27px; font-weight: 700; }
QLabel#TodayMuted { color: #8592ac; font-size: 11px; }
QFrame#TodayMetric, QFrame#TodayCard { background: #ffffff; border: 1px solid #edf1f8; border-radius: 14px; }
QLabel#TodayMetricTitle, QLabel#TodaySectionTitle { color: #20385e; font-size: 13px; font-weight: 600; }
QLabel#TodayMetricValue { color: #17305c; font-size: 24px; font-weight: 700; }
QWidget#TodayPage QPushButton[primary="true"] { background: qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #4e8aff,stop:1 #2864ed); border-color: #3976f4; }
QWidget#TodayPage QPushButton[primary="true"]:hover { background: #3976f4; }
QPushButton#TodayLink { color: #3979ff; border: none; background: transparent; padding: 2px 0px; font-size: 11px; }
QPushButton#TodayLink:hover { color: #1e54c5; }
QListWidget#TodayList { border: none; border-radius: 0px; background: transparent; }
QListWidget#TodayList::item { padding: 0px; border: none; }
QPushButton#FocusButton { background: #f1f6ff; color: #54729b; padding: 4px 12px; border: 1px solid #e3ebf8; border-radius: 9px; font-size: 11px; }
QPushButton#TodayBackup { background: #f8faff; border: 1px solid #e7edf7; border-radius: 8px; padding: 9px 6px; font-size: 11px; min-height: 42px; text-align: left; }
QPushButton#TodayBackup:hover { border-color: #93b4f8; background: #eff5ff; }
QWidget#TodayPage QPushButton[quickColor="blue"] { background: #edf5ff; color: #3979ff; border-color: #edf5ff; padding: 7px 11px; }
QWidget#TodayPage QPushButton[quickColor="purple"] { background: #f3eeff; color: #8057df; border-color: #f3eeff; padding: 7px 11px; }
QWidget#TodayPage QPushButton[quickColor="green"] { background: #edf8f2; color: #25976a; border-color: #edf8f2; padding: 7px 11px; }
QWidget#ProjectPage QPushButton[primary="true"] { background: qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #4e8aff,stop:1 #2864ed); border-color: #3976f4; }
QWidget#ProjectPage QPushButton[primary="true"]:disabled, QWidget#TodayPage QPushButton[primary="true"]:disabled { background: #eff1f6; border-color: #e6e9f1; color: #a4acc0; }
QTableWidget#ProjectCards { border: none; background: transparent; }
QWidget#ProjectDetail { background: #f5f6fb; }
QTableWidget#ProjectCards::item { padding: 0px; background: transparent; }
QLabel#ProjectName { color: #142b52; font-size: 24px; font-weight: 700; }
QLabel#ProjectIcon { background: #3979ff; border-radius: 12px; }
QLabel#ProjectNext { color: #345a92; font-size: 13px; }
QLabel#ProjectMetricValue { color: #17305c; font-size: 18px; font-weight: 700; }
QTextBrowser#ProjectPreview { border: none; background: transparent; padding: 2px; color: #405575; }
QWidget#AgentPage { background: #f5f6fb; }
QWidget#AgentPage QLabel#Title { font-size: 28px; }
QWidget#AgentPage QFrame#AgentHero { background: #ffffff; border: 1px solid #e7ecf5; border-radius: 14px; }
QWidget#AgentPage QLabel#AgentIcon { background: #eaf2ff; color: #3979ff; border-radius: 14px; font-size: 27px; font-weight: 700; }
QWidget#AgentPage QLabel#AgentName { color: #162c53; font-size: 23px; font-weight: 700; }
QWidget#AgentPage QLabel#AgentMeta, QWidget#AgentPage QLabel#AgentPath { color: #71809e; }
QWidget#AgentPage QListWidget#AgentListing { border: 1px solid #e8ebf4; border-radius: 12px; padding: 5px; }
QWidget#AgentPage QListWidget#AgentListing::item { padding: 10px 12px; border: 1px solid #edf1f8; border-radius: 9px; color: #23395f; }
QWidget#AgentPage QListWidget#AgentListing::item:selected { background: #edf4ff; border-color: #8eb6ff; color: #173d83; }
QWidget#AgentPage QListWidget#AgentCompactList { border: none; background: transparent; }
QWidget#AgentPage QListWidget#AgentCompactList::item { padding: 7px 8px; border-bottom: 1px solid #eef1f6; }
QWidget#AgentPage QTabBar::tab { padding: 10px 15px; }
QWidget#CatalogPage { background: #f5f6fb; }
QWidget#CatalogPage QFrame#Card { border-radius: 12px; }
QWidget#CatalogSidePanel, QWidget#CatalogDetail { background: #ffffff; border: 1px solid #e8edf5; border-radius: 12px; }
QListWidget#CatalogFilterList { border: none; border-radius: 8px; background: transparent; }
QListWidget#CatalogFilterList::item { padding: 8px 9px; margin: 1px 0; border: none; border-radius: 7px; }
QListWidget#CatalogFilterList::item:selected { background: #edf4ff; color: #2866d7; }
QListWidget#CatalogResourceList { border: none; background: transparent; }
QListWidget#CatalogResourceList::item { padding: 10px 12px; margin: 2px 0; border: 1px solid transparent; border-radius: 9px; }
QListWidget#CatalogResourceList::item:selected { background: #eef5ff; border-color: #a9c8ff; color: #173b78; }
QLabel#CatalogDetailIcon { background: #edf4ff; color: #3979ff; border-radius: 13px; font-size: 25px; font-weight: 650; }
QLabel#CatalogDetailTitle { color: #172b52; font-size: 20px; font-weight: 700; }
QLabel#CatalogDetailFacts { color: #607493; line-height: 1.5; }
QTextBrowser#CatalogBodyPreview { background: transparent; border: none; padding: 4px 2px; color: #364d70; }
QWidget#CatalogPage QLineEdit, QWidget#CatalogPage QComboBox { min-height: 22px; }
QWidget#CatalogPage QPushButton { padding: 8px 12px; }
QWidget#SafetyPage { background: #f5f6fb; }
QWidget#SafetyPage QLabel#Title { font-size: 25px; }
QWidget#SafetyPage QFrame#SafetyControlCard { background: #ffffff; border: 1px solid #e6ecf5; border-radius: 12px; }
QWidget#SafetyPage QFrame#SafetyMetric { background: #ffffff; border: 1px solid #e6ecf5; border-radius: 12px; }
QWidget#SafetyPage QLabel#SafetyMetricValue { color: #17305c; font-size: 18px; font-weight: 700; }
QWidget#SafetyPage QLabel#SafetyMetricNote { color: #7685a1; font-size: 11px; }
QWidget#SafetyPage QLabel#SafetyEmpty { background: #edf4ff; border: 1px solid #dfeaff; border-radius: 10px; padding: 10px 13px; color: #607493; }
QWidget#SafetyPage QTabWidget#SafetyTabs::pane { background: transparent; border: none; }
QWidget#SafetyPage QTabWidget#SafetyTabs > QTabBar::tab { padding: 9px 14px; font-size: 13px; }
QWidget#SafetyPage QComboBox { min-height: 22px; }
QWidget#SafetyPage QPushButton[primary="true"] { background: qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #4e8aff,stop:1 #2864ed); border-color: #3976f4; }
QWidget#SafetyPage QPushButton[primary="true"]:hover { background: #3976f4; }
QWidget#ResourceManagementPage { background: transparent; }
QWidget#ResourceManagementPage QTabWidget::pane { background: #ffffff; border: 1px solid #e8edf5; border-radius: 12px; }
QWidget#ResourceManagementPage QTabBar::tab { padding: 9px 13px; }
QWidget#ResourceManagementPage QTextEdit { border: none; background: #ffffff; }
"""
