"""Centralized logging via loguru.

Logs to:
  - Console (colorized, INFO level)
  - Session file (~/.voiceinput/logs/voiceinput_YYYY-MM-DD_HH-MM-SS.log, DEBUG level)
    One file per process run, from startup through exit; all levels in the same file.

Crash-level exceptions (unhandled) are also captured.

Usage:
  from core.log import logger
"""
import os
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path

from loguru import logger

_LOG_DIR = Path(os.environ.get("USERPROFILE", os.path.expanduser("~"))) / ".voiceinput" / "logs"
_LOG_DIR.mkdir(parents=True, exist_ok=True)

_LOG_FMT = "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<7} | {name}:{function}:{line} | {message}"
_RETENTION_DAYS = 7

_startup_ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
_session_log = _LOG_DIR / f"voiceinput_{_startup_ts}.log"


def _cleanup_old_logs():
    cutoff = datetime.now() - timedelta(days=_RETENTION_DAYS)
    for f in _LOG_DIR.glob("*.log"):
        try:
            if datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
                f.unlink()
        except Exception:
            pass


def _exception_hook(exc_type, exc_value, exc_tb):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_tb)
        return
    logger.opt(exception=(exc_type, exc_value, exc_tb)).critical(
        f"Unhandled exception: {exc_type.__name__}: {exc_value}"
    )


def _thread_exception_hook(args):
    if args.exc_type is SystemExit:
        return
    logger.opt(exception=(args.exc_type, args.exc_value, args.exc_traceback)).error(
        f"Thread '{args.thread.name}' exception: {args.exc_type.__name__}: {args.exc_value}"
    )


def install_qt_handler():
    """Install Qt message handler to capture C++ level warnings/errors.
    Must be called after QApplication is created."""
    from PyQt6.QtCore import qInstallMessageHandler, QtMsgType

    def _qt_msg_handler(mode, context, message):
        if mode == QtMsgType.QtWarningMsg:
            logger.warning(f"[Qt] {message}")
        elif mode == QtMsgType.QtCriticalMsg:
            logger.error(f"[Qt] {message}")
        elif mode == QtMsgType.QtFatalMsg:
            logger.critical(f"[Qt] {message}")
        else:
            logger.debug(f"[Qt] {message}")

    qInstallMessageHandler(_qt_msg_handler)


# ── setup ──

_cleanup_old_logs()

logger.remove()

if sys.stderr is not None:
    logger.add(
        sys.stderr,
        level="INFO",
        format="<green>{time:HH:mm:ss.SSS}</green> | <level>{level:<7}</level> | <level>{message}</level>",
        colorize=True,
    )

logger.add(
    str(_session_log),
    level="DEBUG",
    format=_LOG_FMT,
    encoding="utf-8",
)

sys.excepthook = _exception_hook
threading.excepthook = _thread_exception_hook
