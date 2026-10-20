"""Smoke-test the actual windowed Windows executable on the build runner."""
import os
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.request import urlopen

import psutil


ROOT = Path(__file__).resolve().parents[1]
EXE = ROOT / "dist" / "面试题库助手" / "面试题库助手.exe"


def main():
    with tempfile.TemporaryDirectory() as data_dir:
        env = os.environ.copy()
        env["INTERVIEW_DATA_DIR"] = data_dir
        process = subprocess.Popen([str(EXE)], cwd=EXE.parent, env=env)
        try:
            for _ in range(300):
                if process.poll() is not None:
                    raise RuntimeError(f"Windowed executable exited early: {process.returncode}")
                try:
                    connections = psutil.Process(process.pid).net_connections(kind="tcp")
                except psutil.NoSuchProcess:
                    raise RuntimeError("Windowed executable disappeared during startup") from None
                ports = [conn.laddr.port for conn in connections
                         if conn.status == psutil.CONN_LISTEN and conn.laddr.ip == "127.0.0.1"]
                for port in ports:
                    try:
                        with urlopen(f"http://127.0.0.1:{port}/api/setup/status", timeout=1) as response:
                            if response.status == 200:
                                print("Windowed executable started and served HTTP")
                                return
                    except OSError:
                        pass
                time.sleep(0.1)
            raise RuntimeError("Windowed executable did not start its local server")
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


if __name__ == "__main__":
    main()
