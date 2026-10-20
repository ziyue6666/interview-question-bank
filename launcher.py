"""Desktop entry point: run the bundled local server on an unused loopback port."""
import socket
import threading
import time
import webbrowser

import uvicorn
from app import app


def choose_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main():
    port=choose_port()
    config=uvicorn.Config(app,host="127.0.0.1",port=port,access_log=False,log_level="error")
    server=uvicorn.Server(config)
    thread=threading.Thread(target=server.run,daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            webbrowser.open(f"http://127.0.0.1:{port}/")
            break
        if not thread.is_alive():
            raise RuntimeError("本地题库服务未能启动")
        time.sleep(0.1)
    thread.join()


if __name__=="__main__":
    main()
