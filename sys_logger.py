"""System logging module.

Handles recording, rotating, persisting, and querying full-application logs,
including FastAPI endpoints, Uvicorn access/error records, background monitors,
and system diagnostics. Persists strictly to data/app.log.
"""

import collections
from datetime import datetime
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import re
from typing import Any, Dict, List, Optional

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
APP_LOG_FILE = DATA_DIR / "app.log"

MAX_BUFFER_SIZE = 1000
_sys_log_buffer = collections.deque(maxlen=MAX_BUFFER_SIZE)


class SysMemoryHandler(logging.Handler):
    """Stores formatted system log records into an in-memory ring buffer."""

    def emit(self, record: logging.LogRecord):
        try:
            msg = self.format(record)
            entry = {
                "id": f"{int(record.created * 1000)}-{int(record.msecs):03d}",
                "timestamp": datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S"),
                "level": record.levelname,
                "name": record.name,
                "message": record.getMessage(),
                "formatted": msg,
            }
            _sys_log_buffer.append(entry)
        except Exception:
            self.handleError(record)


_log_formatter = logging.Formatter(
    fmt="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# Global handlers
_file_handler = RotatingFileHandler(
    APP_LOG_FILE,
    maxBytes=5 * 1024 * 1024,  # 5 MB
    backupCount=3,
    encoding="utf-8",
)
_file_handler.setFormatter(_log_formatter)

_memory_handler = SysMemoryHandler()
_memory_handler.setFormatter(_log_formatter)


def _load_historical_logs():
    """Load latest lines from data/app.log into memory buffer on startup."""
    if not APP_LOG_FILE.exists():
        return
    try:
        with open(APP_LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        pattern = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[([A-Z]+)\] \[([^\]]+)\] (.*)$")
        recent_lines = lines[-MAX_BUFFER_SIZE:]
        for idx, line in enumerate(recent_lines):
            line = line.rstrip("\r\n")
            if not line:
                continue
            m = pattern.match(line)
            if m:
                ts, lvl, nm, msg = m.groups()
                _sys_log_buffer.append({
                    "id": f"hist-{idx}",
                    "timestamp": ts,
                    "level": lvl,
                    "name": nm,
                    "message": msg,
                    "formatted": line,
                })
            else:
                _sys_log_buffer.append({
                    "id": f"hist-{idx}",
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "level": "INFO",
                    "name": "system",
                    "message": line,
                    "formatted": line,
                })
    except Exception:
        pass


def setup_system_logging():
    """Attach memory and file handlers to root and key framework loggers."""
    root_logger = logging.getLogger()
    if _memory_handler not in root_logger.handlers:
        root_logger.addHandler(_memory_handler)
    if _file_handler not in root_logger.handlers:
        root_logger.addHandler(_file_handler)

    # Attach to uvicorn loggers so access and startup messages are captured
    for log_name in ("uvicorn", "uvicorn.access", "uvicorn.error", "fastapi"):
        lgr = logging.getLogger(log_name)
        if _memory_handler not in lgr.handlers:
            lgr.addHandler(_memory_handler)
        if _file_handler not in lgr.handlers:
            lgr.addHandler(_file_handler)

    _load_historical_logs()


def get_logs(limit: int = 250, level: str = "ALL", keyword: str = "") -> List[Dict[str, Any]]:
    """Retrieve filtered logs from memory buffer."""
    logs = list(_sys_log_buffer)

    if level and level.upper() != "ALL":
        target_lvl = level.upper()
        logs = [log for log in logs if log.get("level") == target_lvl]

    if keyword:
        kw = keyword.lower()
        logs = [
            log for log in logs
            if kw in log.get("message", "").lower()
            or kw in log.get("name", "").lower()
            or kw in log.get("formatted", "").lower()
        ]

    if limit > 0:
        logs = logs[-limit:]

    return logs


def clear_logs() -> bool:
    """Clear memory buffer and truncate data/app.log file."""
    _sys_log_buffer.clear()
    try:
        with open(APP_LOG_FILE, "w", encoding="utf-8") as f:
            f.truncate(0)
        return True
    except Exception:
        return False
