from __future__ import annotations
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

from .server import app, init_agent


def _find_free_port(start: int = 8765) -> int:
    port = start
    while port < start + 50:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
        port += 1
    return start


def _run_flask(port: int) -> None:
    app.run(host="127.0.0.1", port=port, threaded=True, debug=False, use_reloader=False)


def main() -> None:
    project_root = Path.cwd()
    init_agent(project_root)

    port = _find_free_port()
    server_thread = threading.Thread(target=_run_flask, args=(port,), daemon=True)
    server_thread.start()
    time.sleep(0.8)  # give Flask a moment to bind before opening the window

    url = f"http://127.0.0.1:{port}"

    try:
        import webview  # pywebview - optional; gives a real app window instead of a browser tab
        webview.create_window("Local Code Agent", url, width=1150, height=820, min_size=(760, 560))
        webview.start()
    except ImportError:
        print(f"(pywebview not installed - opening in your default browser instead: {url})")
        print("For a dedicated app window instead of a browser tab: pip install pywebview")
        webbrowser.open(url)
        print("This window stays running in the background. Press Ctrl+C here to stop it when you're done.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\nStopping.")
            sys.exit(0)


if __name__ == "__main__":
    main()
