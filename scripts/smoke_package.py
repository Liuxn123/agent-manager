from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    if sys.platform == "win32":
        executable = root / "dist/AgentManager/AgentManager.exe"
    elif sys.platform == "darwin":
        executable = root / "dist/AgentManager.app/Contents/MacOS/AgentManager"
    else:
        executable = root / "dist/AgentManager/AgentManager"
    screenshot = root / "build/smoke.png"
    screenshot.unlink(missing_ok=True)
    command = [str(executable), "--data-dir", str(root / "build/smoke-data"), "--smoke-test", "--screenshot", str(screenshot)]
    process = subprocess.Popen(command, env={**os.environ, "QT_QPA_PLATFORM": "offscreen"}, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    try:
        stdout, stderr = process.communicate(timeout=40)
    except subprocess.TimeoutExpired:
        process.kill()  # This is our own empty-data smoke process, never a real task.
        process.communicate(timeout=5)
        raise SystemExit("Frozen application startup timed out")
    if process.returncode or not screenshot.is_file():
        print("Frozen exit code:", process.returncode)
        print(stderr.decode("utf-8", "replace")[-6000:])
        error = root / "build/smoke-data/startup-error.json"
        if error.is_file():
            print(error.read_text(encoding="utf-8"))
        raise SystemExit("Frozen application startup failed")
    print("Frozen application opened, rendered and exited successfully")


if __name__ == "__main__":
    main()
