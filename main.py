import sys
import os
import signal
import ctypes

sys.path.insert(0, os.path.dirname(__file__))

# Ctrl+C 直接终止进程，不抛 KeyboardInterrupt 到随机线程
signal.signal(signal.SIGINT, signal.SIG_DFL)

from PyQt6.QtCore import Qt, QObject, pyqtSignal
from PyQt6.QtWidgets import QApplication
from PyQt6.QtNetwork import QLocalServer, QLocalSocket

from config import Config
from core.log import logger, install_qt_handler
from core.engine import VoiceEngine
from ui import icons
from ui.mini_window import MiniRecordingWindow
from ui.tray import VoiceTray
from ui.user_notification_hub import UserNotificationHub

_APP_KEY = "VoiceInput_SingleInstance_Lock"
_APP_MUTEX_NAME = "VoiceInput_InstallAware_Mutex"
_SHUTDOWN_EVENT_NAME = "VoiceInput_Shutdown_Event"
_app_mutex_handle = None
_shutdown_event_handle = None
_shutdown_bridge = None


def _is_already_running() -> bool:
    """Try connecting to an existing instance. Returns True if one is found."""
    sock = QLocalSocket()
    sock.connectToServer(_APP_KEY)
    connected = sock.waitForConnected(200)
    sock.close()
    return connected


def _create_app_mutex():
    """Create a named Win32 mutex so installers can detect a running app."""
    global _app_mutex_handle
    if sys.platform != "win32":
        return
    handle = ctypes.windll.kernel32.CreateMutexW(None, False, _APP_MUTEX_NAME)
    if not handle:
        logger.warning("Failed to create install-aware app mutex")
        return
    _app_mutex_handle = handle


def _release_app_mutex():
    global _app_mutex_handle
    if not _app_mutex_handle:
        return
    ctypes.windll.kernel32.CloseHandle(_app_mutex_handle)
    _app_mutex_handle = None


def _create_shutdown_event():
    global _shutdown_event_handle
    if sys.platform != "win32":
        return
    handle = ctypes.windll.kernel32.CreateEventW(None, True, False, _SHUTDOWN_EVENT_NAME)
    if not handle:
        return
    _shutdown_event_handle = handle


def _release_shutdown_event():
    global _shutdown_event_handle
    if not _shutdown_event_handle:
        return
    ctypes.windll.kernel32.CloseHandle(_shutdown_event_handle)
    _shutdown_event_handle = None


class _ShutdownBridge(QObject):
    """Bridges the shutdown event from a background thread to the main thread via Qt signal."""
    shutdown_requested = pyqtSignal()


def _start_shutdown_watcher(quit_fn):
    """Background thread that waits for the shutdown event, then invokes quit on the main thread."""
    global _shutdown_bridge
    import threading

    _shutdown_bridge = _ShutdownBridge()
    _shutdown_bridge.shutdown_requested.connect(quit_fn, Qt.ConnectionType.QueuedConnection)

    def _watch():
        if not _shutdown_event_handle:
            return
        INFINITE = 0xFFFFFFFF
        ctypes.windll.kernel32.WaitForSingleObject(_shutdown_event_handle, INFINITE)
        logger.info("[Main] Shutdown event received from installer")
        _shutdown_bridge.shutdown_requested.emit()

    t = threading.Thread(target=_watch, name="ShutdownWatcher", daemon=True)
    t.start()


def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("VoiceInput")
    app.setWindowIcon(icons.app_icon())
    install_qt_handler()

    if _is_already_running():
        logger.warning("VoiceInput is already running, exiting.")
        sys.exit(0)

    server = QLocalServer()
    server.removeServer(_APP_KEY)
    server.listen(_APP_KEY)
    _create_app_mutex()
    _create_shutdown_event()
    app.aboutToQuit.connect(_release_app_mutex)
    app.aboutToQuit.connect(_release_shutdown_event)

    config = Config.load()

    # 当两个后端都使用 DashScope 时，强制禁用代理（阿里云国内地址，无需代理）
    if config.asr_provider == "dashscope" and config.polish_provider == "dashscope":
        for _pv in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            os.environ.pop(_pv, None)
        os.environ["NO_PROXY"] = "*"

    if not config.api_key:
        from config import _config_path
        logger.warning("API key not configured")
        logger.info(f"Set DASHSCOPE_API_KEY env var or edit: {_config_path()}")

    engine = VoiceEngine(config)
    mini = MiniRecordingWindow(engine)
    tray = VoiceTray(engine, mini, config)
    UserNotificationHub(engine, tray, parent=app)
    _start_shutdown_watcher(tray._quit)

    hotkey_display = config.hotkey.replace("+", " + ").title()
    logger.success(f"VoiceInput started. Press {hotkey_display} to record.")

    mini.refresh_visibility()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
