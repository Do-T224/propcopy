"""PropCopy - The fastest trade copier built for prop firm traders."""

import argparse
import multiprocessing
import os
import signal
import sys
import threading
import webbrowser

import uvicorn

from config_manager import load_config
from log_manager import setup_main_logging


CONFIG_PATH = os.path.join(os.getcwd(), "config.yaml")


def _first_run() -> bool:
    """Detect first run: no config.yaml means user hasn't set up yet."""
    return not os.path.exists(CONFIG_PATH)


def main():
    multiprocessing.freeze_support()

    parser = argparse.ArgumentParser(description="PropCopy Trade Copier")
    parser.add_argument("--headless", action="store_true", help="Run without dashboard (CLI only)")
    parser.add_argument("--port", type=int, default=8181, help="Dashboard port (default: 8181)")
    parser.add_argument("--no-browser", action="store_true", help="Don't auto-open browser (use native window)")
    parser.add_argument("--browser", action="store_true", help="Open in browser instead of native window")
    args = parser.parse_args()

    setup_main_logging()

    if args.headless:
        # Headless mode runs from config.yaml only
        config = load_config()
        _run_headless(config)
    else:
        _run_dashboard(args.port, use_browser=args.browser)


def _run_headless(config: dict):
    """Start engine directly without dashboard."""
    import logging
    from coordinator import SubprocessCopyTrader

    logger = logging.getLogger("main")

    if not config.get("slaves"):
        logger.error("No slave accounts configured - check config.yaml")
        sys.exit(1)

    logger.info(f"PropCopy starting (headless): master={config['master']['account']}, slaves={len(config['slaves'])}")

    trader = SubprocessCopyTrader(config)

    def signal_handler(sig, frame):
        logger.info("Received shutdown signal")
        trader.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    trader.start()

    try:
        trader.run()
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received")
    finally:
        trader.stop()


def _run_dashboard(port: int, use_browser: bool = False):
    """Start dashboard — native window by default, browser if --browser flag."""
    import logging

    logger = logging.getLogger("main")

    if _first_run():
        logger.info("First run detected — dashboard will show setup wizard")

    if use_browser:
        logger.info(f"PropCopy starting on http://localhost:{port}")
        threading.Timer(1.5, lambda: webbrowser.open(f"http://localhost:{port}")).start()
        uvicorn.run(
            "server:app",
            host="127.0.0.1",
            port=port,
            log_level="warning",
        )
    else:
        logger.info(f"PropCopy starting (native window) on port {port}")
        from webview_app import run as run_webview
        run_webview(port=port)


if __name__ == "__main__":
    main()
