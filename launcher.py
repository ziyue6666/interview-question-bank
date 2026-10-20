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


def make_server(port):
    # Windowed executables have no stdout/stderr; Uvicorn's default formatter
    # calls isatty() on those streams while configuring logging.
    config=uvicorn.Config(app,host="127.0.0.1",port=port,access_log=False,log_level="error",log_config=None)
    return uvicorn.Server(config)


def main():
    port=choose_port()
    server=make_server(port)
    thread=threading.Thread(target=server.run,daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            webbrowser.open(f"http://127.0.0.1:{port}/")
            break
        if not thread.is_alive():
            raise RuntimeError("本地题库服务未能启动")
        time.sleep(0.1)
    if not server.started:
        server.should_exit=True
        thread.join(timeout=5)
        raise RuntimeError("本地题库服务启动超时，请重试")
    thread.join()


if __name__=="__main__":
    main()
