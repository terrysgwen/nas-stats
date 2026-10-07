"""Music logging module.

Handles recording, rotating, persisting, and querying logs specifically for
fnOS Music services and audio streaming, saved strictly to data/music.log.
Never touches or alters data/nas-stats.db.
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
MUSIC_LOG_FILE = DATA_DIR / "music.log"

MAX_BUFFER_SIZE = 500
_log_buffer = collections.deque(maxlen=MAX_BUFFER_SIZE)


class MusicMemoryHandler(logging.Handler):
    def emit(self, record):
        try:
            msg = self.format(record)
            entry = {
                "id": f"{int(record.created * 1000)}-{int(record.msecs):03d}",
                "timestamp": datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S"),
                "level": record.levelname,
                "message": record.getMessage(),
                "formatted": msg
            }
            _log_buffer.append(entry)
        except Exception:
            self.handleError(record)


# Create dedicated fnmusic logger
music_logger = logging.getLogger("fnmusic")
music_logger.setLevel(logging.INFO)
# Avoid duplicate records if root logger has handlers
music_logger.propagate = False

# Formatter
_log_formatter = logging.Formatter(
    fmt="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

# Rotating File Handler
_file_handler = RotatingFileHandler(
    MUSIC_LOG_FILE,
    maxBytes=2 * 1024 * 1024,
    backupCount=3,
    encoding="utf-8"
)
_file_handler.setFormatter(_log_formatter)
music_logger.addHandler(_file_handler)

# Memory Handler
_memory_handler = MusicMemoryHandler()
_memory_handler.setFormatter(_log_formatter)
music_logger.addHandler(_memory_handler)


def _load_historical_logs():
    """Load latest lines from data/music.log into memory buffer on startup."""
    if not MUSIC_LOG_FILE.exists():
        return
    try:
        with open(MUSIC_LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        pattern = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[([A-Z]+)\] (.*)$")
        recent_lines = lines[-MAX_BUFFER_SIZE:]
        for idx, line in enumerate(recent_lines):
            line = line.rstrip("\r\n")
            if not line:
                continue
            m = pattern.match(line)
            if m:
                ts, lvl, msg = m.groups()
                _log_buffer.append({
                    "id": f"hist-{idx}",
                    "timestamp": ts,
                    "level": lvl,
                    "message": msg,
                    "formatted": line
                })
            else:
                _log_buffer.append({
                    "id": f"hist-{idx}",
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "level": "INFO",
                    "message": line,
                    "formatted": line
                })
    except Exception as e:
        print(f"[music_logger] Failed to load historical logs: {e}")


# Preload existing lines into memory
_load_historical_logs()


def log_info(msg: str):
    """Log an INFO level message for music services."""
    music_logger.info(msg)


def log_warn(msg: str):
    """Log a WARNING level message for music services."""
    music_logger.warning(msg)


def log_error(msg: str):
    """Log an ERROR level message for music services."""
    music_logger.error(msg)


def get_logs(limit: int = 150, level: str = "ALL", keyword: str = "") -> List[Dict[str, Any]]:
    """Retrieve logs matching filter criteria."""
    items = list(_log_buffer)
    # Filter by level
    if level and level.upper() != "ALL":
        lvl_upper = level.upper()
        if lvl_upper == "WARN":
            lvl_upper = "WARNING"
        items = [i for i in items if i.get("level") == lvl_upper]

    # Filter by keyword
    if keyword:
        kw = keyword.lower()
        items = [i for i in items if kw in i.get("message", "").lower()]

    # Limit results
    if limit > 0:
        items = items[-limit:]

    return items


def clear_logs() -> bool:
    """Clear in-memory log buffer and truncate data/music.log."""
    try:
        _log_buffer.clear()
        with open(MUSIC_LOG_FILE, "w", encoding="utf-8") as f:
            f.write("")
        log_info("音乐日志已手动清空。")
        return True
    except Exception as e:
        music_logger.error(f"清空音乐日志失败: {e}")
        return False
