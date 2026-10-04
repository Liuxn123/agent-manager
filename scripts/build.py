from __future__ import annotations

import json
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed", "--onedir",
               "--name", "AgentManager", "--paths", str(ROOT / "src"), "--collect-submodules", "keyring.backends", "--collect-data", "agent_manager",
               "--distpath", str(ROOT / "dist"), "--workpath", str(ROOT / "build"), str(ROOT / "scripts/desktop_entry.py")]
    subprocess.run(command, cwd=ROOT, check=True)
    payload = ROOT / "build/package"
    payload.mkdir(parents=True, exist_ok=True)
    application = ROOT / "dist" / ("AgentManager.app" if sys.platform == "darwin" else "AgentManager")
    shutil.copytree(application, payload / application.name, dirs_exist_ok=True)
    shutil.copy2(ROOT / "README.md", payload / "README.md")
    shutil.copy2(ROOT / "src/agent_manager/assets/fonts/OFL.txt", payload / "Noto-Font-OFL.txt")
    metadata = {"version": "0.1.0", "platform": sys.platform, "architecture": platform.machine(), "python": platform.python_version()}
    (payload / "BUILD-INFO.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    name = ROOT / "dist" / f"AgentManager-{sys.platform}-{platform.machine()}"
    result = shutil.make_archive(str(name), "zip" if sys.platform == "win32" else "gztar", root_dir=payload)
    print("Package:", result)


if __name__ == "__main__":
    main()
