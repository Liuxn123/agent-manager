from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def build_environment() -> dict[str, str]:
    environment = os.environ.copy()
    if sys.platform == "win32":
        # PyInstaller resolves native dependencies using PATH. Unrelated tools
        # (for example Poppler/Conda) can supply an incompatible icuuc.dll and
        # shadow the Windows ICU ABI used by Qt. Keep only build/runtime paths.
        windows = Path(os.environ["SystemRoot"])
        directories = [Path(sys.executable).parent, Path(sys.base_prefix),
                       Path(sys.base_prefix) / "DLLs", windows / "System32", windows]
        environment["PATH"] = os.pathsep.join(str(path) for path in directories if path.is_dir())
    return environment


def main() -> None:
    command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed", "--onedir",
               "--name", "AgentManager", "--paths", str(ROOT / "src"), "--collect-submodules", "keyring.backends", "--collect-data", "agent_manager",
               "--distpath", str(ROOT / "dist"), "--workpath", str(ROOT / "build"), str(ROOT / "scripts/desktop_entry.py")]
    subprocess.run(command, cwd=ROOT, env=build_environment(), check=True)
    application = ROOT / "dist" / ("AgentManager.app" if sys.platform == "darwin" else "AgentManager")
    # A new staging directory prevents a removed DLL from surviving in a zip
    # produced by a subsequent build.
    with tempfile.TemporaryDirectory(prefix="package-", dir=ROOT / "build") as temporary:
        payload = Path(temporary)
        shutil.copytree(application, payload / application.name, symlinks=True)
        shutil.copy2(ROOT / "README.md", payload / "README.md")
        shutil.copy2(ROOT / "src/agent_manager/assets/fonts/OFL.txt", payload / "Noto-Font-OFL.txt")
        metadata = {"version": "0.2.0", "platform": sys.platform, "architecture": platform.machine(), "python": platform.python_version()}
        (payload / "BUILD-INFO.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        name = ROOT / "dist" / f"AgentManager-{sys.platform}-{platform.machine()}"
        result = shutil.make_archive(str(name), "zip" if sys.platform == "win32" else "gztar", root_dir=payload)
    print("Package:", result)


if __name__ == "__main__":
    main()
