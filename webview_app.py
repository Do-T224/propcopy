"""Pywebview wrapper — runs the dashboard in a native OS window."""

import logging
import threading

import uvicorn
import webview

logger = logging.getLogger("webview_app")


def run(port: int = 8080):
    """Start uvicorn in a background thread, then open a pywebview window."""
    # Start FastAPI server in background
    server_thread = threading.Thread(
        target=_start_server,
        args=(port,),
        daemon=True,
    )
    server_thread.start()

    # Give uvicorn a moment to bind
    import time
    time.sleep(1.0)

    # Create native window pointing at the dashboard
    window = webview.create_window(
        "PropCopy",
        url=f"http://127.0.0.1:{port}",
        width=1280,
        height=860,
        min_size=(900, 600),
    )
    webview.start()  # blocks until window is closed
    logger.info("Window closed — shutting down")


def _start_server(port: int):
    uvicorn.run(
        "server:app",
        host="127.0.0.1",
        port=port,
        log_level="warning",
    )
