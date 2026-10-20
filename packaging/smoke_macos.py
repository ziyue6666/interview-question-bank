"""Start the app extracted from the distributable ZIP on a macOS runner."""
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.request import urlopen

dist = Path(__file__).resolve().parents[1] / "dist"


def main():
    archive = next(dist.glob("interview-question-bank-macos-*.zip"))
    with tempfile.TemporaryDirectory() as temp:
        subprocess.run(["ditto", "-x", "-k", str(archive), temp], check=True)
        exe = Path(temp) / "面试题库助手.app" / "Contents" / "MacOS" / "面试题库助手"
        data_dir = Path(temp) / "data"
        data_dir.mkdir()
        env = os.environ.copy()
        env["INTERVIEW_DATA_DIR"] = str(data_dir)
        env["BROWSER"] = "/usr/bin/true"
        process = subprocess.Popen([str(exe)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(300):
                if process.poll() is not None:
                    raise RuntimeError(f"Extracted app exited early: {process.returncode}")
                result = subprocess.run(["lsof", "-Pan", "-p", str(process.pid), "-iTCP", "-sTCP:LISTEN"], capture_output=True)
                ports = re.findall(rb"127\.0\.0\.1:(\d+)\s+\(LISTEN\)", result.stdout)
                for port in ports:
                    try:
                        with urlopen(f"http://127.0.0.1:{port.decode()}/api/setup/status", timeout=1) as response:
                            if response.status == 200:
                                print("Extracted macOS app started and served HTTP")
                                return
                    except OSError:
                        pass
                time.sleep(0.1)
            raise RuntimeError("Extracted app did not start its local server")
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


if __name__ == "__main__":
    main()
