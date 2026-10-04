"""Centralized log collection for PropCopy dashboard."""

import collections
import logging
import logging.handlers
import multiprocessing
import os
import threading
import time


class LogEntry:
    __slots__ = ("timestamp", "level", "source", "message")

    def __init__(self, timestamp: float, level: str, source: str, message: str):
        self.timestamp = timestamp
        self.level = level
        self.source = source
        self.message = message

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "level": self.level,
            "source": self.source,
            "message": self.message,
        }


class LogBuffer:
    """Thread-safe ring buffer of recent log entries for the dashboard."""

    def __init__(self, maxlen: int = 500):
        self._buffer: collections.deque[LogEntry] = collections.deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def add(self, entry: LogEntry) -> None:
        with self._lock:
            self._buffer.append(entry)

    def get_recent(self, count: int = 200) -> list[dict]:
        with self._lock:
            entries = list(self._buffer)[-count:]
        return [e.to_dict() for e in entries]


class BufferHandler(logging.Handler):
    """Logging handler that captures entries into the LogBuffer."""

    def __init__(self, buffer: LogBuffer):
        super().__init__()
        self.buffer = buffer

    def emit(self, record: logging.LogRecord) -> None:
        entry = LogEntry(
            timestamp=record.created,
            level=record.levelname,
            source=record.name,
            message=self.format(record),
        )
        self.buffer.add(entry)


# Global log buffer shared by the main process
log_buffer = LogBuffer()


def setup_main_logging(log_dir: str = "data") -> None:
    """Configure root logger with console, file, and buffer handlers."""
    os.makedirs(log_dir, exist_ok=True)

    fmt = logging.Formatter(
        "%(asctime)s.%(msecs)03d [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    # Console
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    # Rotating file
    file_handler = logging.handlers.RotatingFileHandler(
        os.path.join(log_dir, "propcopy.log"),
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    # Buffer for dashboard
    buf_handler = BufferHandler(log_buffer)
    buf_handler.setFormatter(fmt)
    root.addHandler(buf_handler)


def setup_process_logging(log_queue: multiprocessing.Queue) -> None:
    """Configure logging in a child process to send records to the main process via queue."""
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.INFO)
    root.addHandler(logging.handlers.QueueHandler(log_queue))


def start_log_listener(log_queue: multiprocessing.Queue) -> threading.Thread:
    """Start a thread that reads log records from child processes and injects them into main logging."""

    def _listener():
        while True:
            try:
                record = log_queue.get(timeout=1.0)
                if record is None:
                    break
                logger = logging.getLogger(record.name)
                logger.handle(record)
            except Exception:
                continue

    t = threading.Thread(target=_listener, daemon=True, name="log-listener")
    t.start()
    return t
