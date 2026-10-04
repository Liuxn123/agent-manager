from __future__ import annotations

import os
import platform
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path


def check_application(root: Path, payload: Path) -> None:
    if sys.platform == "win32":
        executable = payload / "AgentManager/AgentManager.exe"
    elif sys.platform == "darwin":
        executable = payload / "AgentManager.app/Contents/MacOS/AgentManager"
    else:
        executable = payload / "AgentManager/AgentManager"
    screenshot = root / "build/smoke.png"
    screenshot.unlink(missing_ok=True)
    command = [str(executable), "--data-dir", str(payload / "smoke-data"), "--smoke-test", "--screenshot", str(screenshot)]
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
        # Encode for the console explicitly: some Windows terminals use GBK.
        message = stderr.decode("utf-8", "replace")[-6000:]
        print(message.encode(sys.stdout.encoding or "utf-8", "replace").decode(sys.stdout.encoding or "utf-8"))
        error = payload / "smoke-data/startup-error.json"
        if error.is_file():
            print(error.read_text(encoding="utf-8"))
        raise SystemExit("Frozen application startup failed")
    print("Extracted package opened, rendered and exited successfully")


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    extension = ".zip" if sys.platform == "win32" else ".tar.gz"
    package = root / "dist" / f"AgentManager-{sys.platform}-{platform.machine()}{extension}"
    with tempfile.TemporaryDirectory(prefix="smoke-extract-", dir=root / "build") as temporary:
        payload = Path(temporary)
        if sys.platform == "win32":
            with zipfile.ZipFile(package) as archive:
                archive.extractall(payload)
        else:
            with tarfile.open(package, "r:gz") as archive:
                archive.extractall(payload, filter="data")
        check_application(root, payload)


if __name__ == "__main__":
    main()
