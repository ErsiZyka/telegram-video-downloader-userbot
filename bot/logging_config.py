"""Centralized logging for the video downloader bot.

Logs go to BOTH the console (the start.bat window) and a persistent rotating
file at data/bot.log, with timestamps. Every module calls ``bot_log(...)`` which
is a thin wrapper over the standard ``logging`` module so yt-dlp / Telethon /
asyncio warnings are also captured.

The window shows real-time logs; the file keeps history across restarts so you
can review what happened after the fact.
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler

_CONFIGURED = False


def setup_logging(log_dir: str = "data", level: int = logging.INFO) -> logging.Logger:
    """Configure root logging once: console + rotating file. Returns the bot logger."""
    global _CONFIGURED
    logger = logging.getLogger("bot")
    if _CONFIGURED:
        return logger

    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "bot.log")

    fmt = logging.Formatter(
        fmt="%(asctime)s %(levelname)-5s %(message)s",
        datefmt="%H:%M:%S",
    )

    # Console handler -> the start.bat window (real-time).
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.setLevel(level)

    # File handler -> persistent, rotates at 2 MB, keeps 3 backups.
    file_h = RotatingFileHandler(
        log_path, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8",
    )
    file_h.setFormatter(fmt)
    file_h.setLevel(level)

    logger.setLevel(level)
    logger.addHandler(console)
    logger.addHandler(file_h)
    logger.propagate = False

    # Also capture third-party library logs at WARNING+ so yt-dlp / Telethon /
    # playwright errors surface in the same window.
    for name in ("yt_dlp", "telethon", "playwright", "asyncio"):
        lib = logging.getLogger(name)
        lib.setLevel(logging.WARNING)
        if not lib.handlers:
            lib.addHandler(console)
            lib.addHandler(file_h)

    _CONFIGURED = True
    logger.info("Logging avviato (console + %s)", log_path)
    return logger


def bot_log(msg: str, level: int = logging.INFO) -> None:
    """Emit a log line through the 'bot' logger. Drop-in for the old _log()."""
    logging.getLogger("bot").log(level, msg)


def get_logger(name: str) -> logging.Logger:
    """Get a child logger under 'bot' for a module."""
    return logging.getLogger(f"bot.{name}")
