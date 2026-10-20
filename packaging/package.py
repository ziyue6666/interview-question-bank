"""Create distributable ZIPs while preserving macOS app symlinks."""
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


root = Path(__file__).resolve().parents[1]
dist = root / "dist"
name = "面试题库助手"

if sys.platform == "darwin":
    label = "macos-arm64" if platform.machine() == "arm64" else "macos-intel"
    bundle = dist / f"{name}.app"
    archive = dist / f"interview-question-bank-{label}.zip"
    subprocess.run(["ditto", "-c", "-k", "--keepParent", str(bundle), str(archive)], check=True)
    with tempfile.TemporaryDirectory() as temp:
        subprocess.run(["ditto", "-x", "-k", str(archive), temp], check=True)
        subprocess.run(["codesign", "--verify", "--deep", "--strict", str(Path(temp) / bundle.name)], check=True)
elif sys.platform == "win32":
    archive = dist / "interview-question-bank-windows-x64.zip"
    shutil.make_archive(str(archive.with_suffix("")), "zip", dist, name)
else:
    raise RuntimeError(f"Unsupported platform: {sys.platform}")

print(f"Package complete: {archive}")
