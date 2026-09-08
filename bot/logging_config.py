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


class _TelegramNoiseFilter(logging.Filter):
    """Drop Telethon's internal-outage spam (server-side, unactionable)."""

    BLOCKED = (
        "PersistentTimestampOutdatedError",
        "Getting difference for channel updates",
    )

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        return not any(s in msg for s in self.BLOCKED)


def setup_logging(log_dir: str = "data", level: int = logging.INFO) -> logging.Logger:
    """Configure root logging once: console + rotating file. Returns the bot logger."""
    global _CONFIGURED
    logger = logging.getLogger("bot")
    if _CONFIGURED:
        return logger

    try:
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, "bot.log")
    except OSError as e:
        print(f"WARN: cartella log non creabile ({e}), solo console")
        log_path = None

    fmt = logging.Formatter(
        fmt="%(asctime)s %(levelname)-5s %(message)s",
        datefmt="%H:%M:%S",
    )

    # Console handler -> the start.bat window (real-time).
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.setLevel(level)

    handlers: list[logging.Handler] = [console]
    if log_path is not None:
        # File handler -> persistent, rotates at 2 MB, keeps 3 backups.
        try:
            file_h = RotatingFileHandler(
                log_path, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8",
            )
        except OSError as e:
            print(f"WARN: file di log non apribile ({e}), solo console")
        else:
            file_h.setFormatter(fmt)
            file_h.setLevel(level)
            handlers.append(file_h)

    logger.setLevel(level)
    for hdl in handlers:
        logger.addHandler(hdl)
    logger.propagate = False

    # Also capture third-party library logs at WARNING+ so yt-dlp / Telethon /
    # playwright errors surface in the same window.
    for name in ("yt_dlp", "telethon", "playwright", "asyncio"):
        lib = logging.getLogger(name)
        lib.setLevel(logging.WARNING)
        if not lib.handlers:
            for hdl in handlers:
                lib.addHandler(hdl)

    # Telethon's internals spam PersistentTimestampOutdatedError /
    # GetChannelDifference warnings during Telegram-side outages: they fill
    # the 2MB rotation and hide real errors. Drop just that noise.
    logging.getLogger("telethon").addFilter(_TelegramNoiseFilter())

    _CONFIGURED = True
    dest = log_path if log_path is not None else "console"
    logger.info("Logging avviato (%s)", dest)
    return logger


def bot_log(msg: str, level: int = logging.INFO) -> None:
    """Emit a log line through the 'bot' logger. Drop-in for the old _log()."""
    logging.getLogger("bot").log(level, msg)


def get_logger(name: str) -> logging.Logger:
    """Get a child logger under 'bot' for a module."""
    return logging.getLogger(f"bot.{name}")
