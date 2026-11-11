# -*- coding: utf-8 -*-
"""Start the local GuWen service and open the browser when it is ready.

The application itself is a local FastAPI service.  This launcher does *not*
start any model runtime: the AI endpoint is configured in 「AI 设置」 and is
called over HTTP (OpenAI-compatible).
"""
from __future__ import annotations

import os
import sys
import threading
import time
import urllib.request
import webbrowser

ROOT = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(ROOT, "app")
if APP not in sys.path:
    sys.path.insert(0, APP)

HOST = os.environ.get("GUWEN_HOST", "127.0.0.1")
PORT = int(os.environ.get("GUWEN_PORT", "8770"))
URL = "http://127.0.0.1:%d/" % PORT


def _open_browser_when_ready() -> None:
    if os.environ.get("GUWEN_OPEN_BROWSER", "1").strip() in ("0", "false", "no"):
        return
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(URL + "api/health", timeout=0.8) as response:
                if response.status == 200:
                    webbrowser.open(URL)
                    return
        except Exception:
            pass
        time.sleep(0.35)


def main() -> None:
    import uvicorn

    from server import app  # app/server.py

    threading.Thread(target=_open_browser_when_ready, daemon=True).start()
    print("GuWen: http://127.0.0.1:%d/  (Ctrl+C to stop)" % PORT)
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    main()
