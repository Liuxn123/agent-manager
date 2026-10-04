from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PySide6.QtCore import QLockFile, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from .storage import Store
from .ui.theme import setup_theme
from .ui.window import MainWindow


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Personal Agent Manager desktop")
    parser.add_argument("--data-dir", type=Path, help="Override the local application data directory")
    parser.add_argument("--smoke-test", action="store_true", help="Open and close the GUI for package validation")
    parser.add_argument("--screenshot", type=Path, help="Save a startup screenshot during --smoke-test")
    options = parser.parse_args(argv)
    app = QApplication(sys.argv[:1])
    app.setApplicationName("AgentManager")
    app.setOrganizationName("PersonalTools")
    setup_theme(app)
    try:
        store = Store(options.data_dir)
        lock = QLockFile(str(store.root / "manager.lock"))
        lock.setStaleLockTime(0)
        if not lock.tryLock(100):
            QMessageBox.information(None, "已在运行", "同一数据目录已有 Agent 管家运行。")
            return 1
        store.recover_interrupted()
        window = MainWindow(store)
        window.show()
        if options.smoke_test:
            def finish_smoke():
                if options.screenshot:
                    options.screenshot.parent.mkdir(parents=True, exist_ok=True)
                    if not window.grab().save(str(options.screenshot)):
                        app.exit(2)
                        return
                app.quit()
            QTimer.singleShot(600, finish_smoke)
        result = app.exec()
        lock.unlock()
        return result
    except Exception as exc:
        if options.smoke_test:
            if options.data_dir:
                import json
                import traceback
                options.data_dir.mkdir(parents=True, exist_ok=True)
                frames = [{"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name} for frame in traceback.extract_tb(exc.__traceback__)]
                (options.data_dir / "startup-error.json").write_text(json.dumps({"error_type": type(exc).__name__, "frames": frames}), encoding="utf-8")
            return 3
        QMessageBox.critical(None, "启动失败", f"无法初始化本机数据目录，请检查路径与权限。（{type(exc).__name__}）")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
