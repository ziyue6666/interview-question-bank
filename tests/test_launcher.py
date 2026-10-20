"""A windowed executable starts without console output streams."""
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


class LauncherTests(unittest.TestCase):
    def test_server_starts_without_console_streams(self):
        script = textwrap.dedent("""
            import sys
            import threading
            import time
            from urllib.request import urlopen

            sys.stdout = None
            sys.stderr = None
            from launcher import choose_port, make_server

            port = choose_port()
            server = make_server(port)
            thread = threading.Thread(target=server.run, daemon=True)
            thread.start()
            try:
                for _ in range(100):
                    if server.started:
                        break
                    if not thread.is_alive():
                        raise RuntimeError("Server stopped during startup")
                    time.sleep(0.1)
                if not server.started:
                    raise RuntimeError("Server did not start")
                with urlopen(f"http://127.0.0.1:{port}/", timeout=5) as response:
                    if response.status != 200:
                        raise RuntimeError(f"Unexpected status: {response.status}")
            finally:
                server.should_exit = True
                thread.join(timeout=5)
                if thread.is_alive():
                    raise RuntimeError("Server did not stop")
        """)
        with tempfile.TemporaryDirectory() as data_dir:
            env = os.environ.copy()
            env["INTERVIEW_DATA_DIR"] = data_dir
            result = subprocess.run(
                [sys.executable, "-c", script],
                cwd=Path(__file__).resolve().parents[1],
                env=env, capture_output=True, timeout=25,
            )
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
