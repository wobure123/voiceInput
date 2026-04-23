import copy
import ctypes
import os
import sys
from collections.abc import Callable
from contextlib import contextmanager
import threading
import time
import uuid

from PyQt6.QtCore import (
    Qt, QEvent, QObject, QThread, pyqtSignal, QTimer, QUrl, QCoreApplication,
)
from PyQt6.QtGui import QAction, QColor, QDesktopServices, QKeySequence
from PyQt6.QtWidgets import (
    QSystemTrayIcon, QMenu, QApplication,
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QLineEdit, QTextEdit, QListWidget, QListWidgetItem,
    QStyledItemDelegate, QStyleOptionViewItem,
    QMessageBox, QSplitter, QWidget,
    QRadioButton, QButtonGroup,
)

from typing import Any, TypeVar

from config import Config

_T_modal = TypeVar("_T_modal")
from core.log import logger
from core.engine import VoiceEngine
from core.polisher import DEFAULT_INSTRUCTIONS
from core.prompt_templates import default_prompt_templates
from ui.mini_window import MiniRecordingWindow
from ui.sounds import AudioCues
from ui import icons


_MOD_KEYS = frozenset({
    "ctrl", "shift", "alt", "capslock",
    "lctrl", "rctrl", "lshift", "rshift", "lalt", "ralt",
})

_VK_TO_NAME: dict[int, str] = {}
_NAME_TO_VK: dict[str, int] = {}


def _build_vk_maps():
    for i in range(0x41, 0x5B):
        n = chr(i).lower()
        _NAME_TO_VK[n] = i
        _VK_TO_NAME[i] = n
    for i in range(10):
        _NAME_TO_VK[str(i)] = 0x30 + i
        _VK_TO_NAME[0x30 + i] = str(i)
    for i in range(1, 25):
        _NAME_TO_VK[f"f{i}"] = 0x70 + i - 1
        _VK_TO_NAME[0x70 + i - 1] = f"f{i}"
    extras = {
        "space": 0x20, "enter": 0x0D, "tab": 0x09, "escape": 0x1B,
        "backspace": 0x08, "delete": 0x2E, "insert": 0x2D,
        "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
        "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
        "capslock": 0x14, "numlock": 0x90, "scrolllock": 0x91,
        "printscreen": 0x2C, "pause": 0x13,
        ";": 0xBA, "=": 0xBB, ",": 0xBC, "-": 0xBD, ".": 0xBE,
        "/": 0xBF, "`": 0xC0, "[": 0xDB, "\\": 0xDC, "]": 0xDD, "'": 0xDE,
    }
    for n, vk in extras.items():
        _NAME_TO_VK[n] = vk
        _VK_TO_NAME[vk] = n
    _VK_TO_NAME[0xA0] = "lshift"
    _VK_TO_NAME[0xA1] = "rshift"
    _VK_TO_NAME[0xA2] = "lctrl"
    _VK_TO_NAME[0xA3] = "rctrl"
    _VK_TO_NAME[0xA4] = "lalt"
    _VK_TO_NAME[0xA5] = "ralt"
    _VK_TO_NAME.setdefault(0x10, "lshift")
    _VK_TO_NAME.setdefault(0x11, "lctrl")
    _VK_TO_NAME.setdefault(0x12, "lalt")
    _NAME_TO_VK.update({
        "lctrl": 0xA2, "rctrl": 0xA3,
        "lshift": 0xA0, "rshift": 0xA1,
        "lalt": 0xA4, "ralt": 0xA5,
    })


_build_vk_maps()

_DISPLAY = {
    "ctrl": "Ctrl", "shift": "Shift", "alt": "Alt",
    "lctrl": "L-Ctrl", "rctrl": "R-Ctrl",
    "lshift": "L-Shift", "rshift": "R-Shift",
    "lalt": "L-Alt", "ralt": "R-Alt",
    "space": "Space", "enter": "Enter", "tab": "Tab",
    "escape": "Esc", "backspace": "Backspace", "delete": "Delete",
    "insert": "Insert", "home": "Home", "end": "End",
    "pageup": "PageUp", "pagedown": "PageDown",
    "up": "↑", "down": "↓", "left": "←", "right": "→",
    "capslock": "CapsLock", "numlock": "NumLock",
    "scrolllock": "ScrollLock", "printscreen": "PrtSc", "pause": "Pause",
}


def _canonical(parts) -> str:
    parts = list({p.strip().lower() for p in parts})
    mods = sorted(p for p in parts if p in _MOD_KEYS)
    others = sorted(p for p in parts if p not in _MOD_KEYS)
    return "+".join(mods + others)


def _hotkey_display(combo: str) -> str:
    parts = combo.split("+")
    return " + ".join(_DISPLAY.get(p, p.upper()) for p in parts)


_AUTOSTART_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_AUTOSTART_VALUE_NAME = "VoiceInput"


_PYN_KEYS: dict = {}


def _init_pynput():
    if _PYN_KEYS:
        return
    from pynput.keyboard import Key
    _PYN_KEYS.update({
        Key.ctrl_l: "lctrl", Key.ctrl_r: "rctrl",
        Key.shift_l: "lshift", Key.shift_r: "rshift",
        Key.alt_l: "lalt", Key.alt_r: "ralt", Key.alt_gr: "ralt",
        Key.space: "space", Key.enter: "enter", Key.tab: "tab",
        Key.esc: "escape", Key.backspace: "backspace", Key.delete: "delete",
        Key.insert: "insert", Key.home: "home", Key.end: "end",
        Key.page_up: "pageup", Key.page_down: "pagedown",
        Key.up: "up", Key.down: "down", Key.left: "left", Key.right: "right",
        Key.caps_lock: "capslock", Key.num_lock: "numlock",
        Key.scroll_lock: "scrolllock",
        Key.print_screen: "printscreen", Key.pause: "pause",
    })
    for i in range(1, 21):
        fk = getattr(Key, f"f{i}", None)
        if fk:
            _PYN_KEYS[fk] = f"f{i}"


def _pyn_key(key) -> str | None:
    from pynput.keyboard import KeyCode
    _init_pynput()
    if key in _PYN_KEYS:
        return _PYN_KEYS[key]
    if isinstance(key, KeyCode):
        if key.char and key.char.isprintable():
            return key.char.lower()
        if key.vk:
            return _VK_TO_NAME.get(key.vk)
    return None


# KBDLLHOOKSTRUCT.flags 位：事件由 SendInput/keybd_event 合成（非物理按键）。
# pynput Controller 使用 SendInput，TextInjector 的 Shift+Insert 粘贴会置此位；
# 过滤器命中此位时直接放行，避免钩子拦截自家注入事件导致修饰键残留。
_LLKHF_INJECTED = 0x10


class ComboHotkeyThread(QThread):
    """Global hotkey — order-independent combo detection with key suppression."""
    triggered = pyqtSignal()
    released = pyqtSignal(int)  # hold duration in ms

    def __init__(self, hotkey_str: str):
        super().__init__()
        parts = [p.strip().lower() for p in hotkey_str.split("+")]
        self._combo = frozenset(parts)
        self._pressed: set[str] = set()
        self._active = False
        self._active_time: float = 0.0
        self._hook_suppressed: set[str] = set()
        self._kb_listener = None

    def _is_combo_key(self, name: str | None) -> bool:
        """True if this physical key belongs to the configured shortcut (incl. generic ctrl/shift/alt)."""
        if not name:
            return False
        if name in self._combo:
            return True
        if "ctrl" in self._combo and name in ("lctrl", "rctrl"):
            return True
        if "shift" in self._combo and name in ("lshift", "rshift"):
            return True
        if "alt" in self._combo and name in ("lalt", "ralt"):
            return True
        return False

    def _combo_fully_pressed(self) -> bool:
        """Order-independent: every combo slot satisfied (generic mods match either side key)."""
        for key in self._combo:
            if key in self._pressed:
                continue
            if key == "ctrl" and ("lctrl" in self._pressed or "rctrl" in self._pressed):
                continue
            if key == "shift" and ("lshift" in self._pressed or "rshift" in self._pressed):
                continue
            if key == "alt" and ("lalt" in self._pressed or "ralt" in self._pressed):
                continue
            return False
        return True

    def run(self):
        try:
            from pynput.keyboard import Listener as KBL
        except Exception:
            logger.error("[Hotkey] Failed to import pynput listeners", exc_info=True)
            return

        def kb_filter(msg, data):
            if data.flags & _LLKHF_INJECTED:
                return
            name = _VK_TO_NAME.get(data.vkCode)
            if not name:
                return
            combo_key = self._is_combo_key(name)
            if msg in (0x0100, 0x0104):
                was_new = name not in self._pressed
                self._pressed.add(name)
                if was_new and self._combo_fully_pressed() and not self._active:
                    self._active = True
                    self._active_time = time.monotonic()
                    self.triggered.emit()
                if combo_key and self._combo_fully_pressed():
                    self._hook_suppressed.add(name)
                    self._kb_listener.suppress_event()
            elif msg in (0x0101, 0x0105):
                if self._active and combo_key:
                    hold_ms = int((time.monotonic() - self._active_time) * 1000)
                    self._active = False
                    self.released.emit(hold_ms)
                self._pressed.discard(name)
                # 只抑制「曾被抑制过 KEYDOWN」的键的 KEYUP；否则系统仍认为 Shift/Ctrl 未弹起（粘键），
                # 会导致滚轮变横滚、点击异常等。
                if name in self._hook_suppressed:
                    self._hook_suppressed.discard(name)
                    self._kb_listener.suppress_event()
                if not any(self._is_combo_key(n) for n in self._pressed):
                    self._hook_suppressed.clear()

        try:
            self._kb_listener = KBL(win32_event_filter=kb_filter)
            self._kb_listener.start()
            self._kb_listener.join()
        except Exception:
            logger.error("[Hotkey] Listener crashed", exc_info=True)

    def stop_hotkey(self):
        if self._kb_listener:
            self._kb_listener.stop()


class _HotkeyGrabSignals(QObject):
    key_down = pyqtSignal(str)
    key_up = pyqtSignal(str)


class _PynputHotkeyGrabWorker(threading.Thread):
    """快捷键设置窗：pynput ``WH_KEYBOARD_LL`` + ``win32_event_filter`` 全局拦键。

    ``suppress_event()`` 会抛 ``SuppressException`` 以拦键；须先 ``emit`` 再 ``suppress``。
    若 ``emit`` 时 ``QObject`` 已析构（关窗与卸钩竞态），捕获 ``RuntimeError`` 并
    ``return False``，本键交还系统，**不得**再 ``suppress``，以免整机键盘卡死。
    """
    def __init__(self, sigs: _HotkeyGrabSignals):
        super().__init__(name="HotkeyGrabPynput", daemon=True)
        self._sigs = sigs
        self._stop_requested = False
        self._kb_listener = None

    def stop_grab(self):
        self._stop_requested = True
        listener = self._kb_listener
        if listener is not None:
            listener.stop()

    def run(self):
        if sys.platform != "win32":
            return
        try:
            from pynput.keyboard import Listener as KBL
        except Exception:
            logger.error("[HotkeyGrab] Failed to import pynput", exc_info=True)
            return
        if self._stop_requested:
            return

        self_ref = self

        def kb_filter(msg, data):
            if msg not in (0x0100, 0x0101, 0x0104, 0x0105):
                return True
            if data.flags & _LLKHF_INJECTED:
                return True
            name = _VK_TO_NAME.get(data.vkCode)
            if not name:
                logger.debug("[HotkeyGrab] pass-through unmapped vk=0x%02X", data.vkCode)
                return True
            try:
                if msg in (0x0100, 0x0104):
                    self_ref._sigs.key_down.emit(name)
                elif msg in (0x0101, 0x0105):
                    self_ref._sigs.key_up.emit(name)
            except RuntimeError:
                # 对话框已关、_grab_sig 已删时 emit 失败；勿 suppress，否则按键整桌失效
                logger.debug("[HotkeyGrab] emit failed (dialog closed?), vk=0x%02X — passing through", data.vkCode)
                return False
            self_ref._kb_listener.suppress_event()

        try:
            self._kb_listener = KBL(win32_event_filter=kb_filter)
        except Exception:
            logger.error("[HotkeyGrab] Failed to create pynput Listener", exc_info=True)
            return
        if self._stop_requested:
            self._kb_listener = None
            return
        try:
            logger.info("[HotkeyGrab] pynput listener started")
            self._kb_listener.start()
            self._kb_listener.join()
        except Exception:
            logger.error("[HotkeyGrab] Listener crashed", exc_info=True)
        finally:
            self._kb_listener = None
            logger.info("[HotkeyGrab] pynput listener stopped")


_QT_KEY_NAMES = {
    Qt.Key.Key_Space: "space", Qt.Key.Key_Return: "enter",
    Qt.Key.Key_Enter: "enter", Qt.Key.Key_Tab: "tab",
    Qt.Key.Key_Escape: "escape", Qt.Key.Key_Backspace: "backspace",
    Qt.Key.Key_Delete: "delete", Qt.Key.Key_Insert: "insert",
    Qt.Key.Key_Home: "home", Qt.Key.Key_End: "end",
    Qt.Key.Key_PageUp: "pageup", Qt.Key.Key_PageDown: "pagedown",
    Qt.Key.Key_Up: "up", Qt.Key.Key_Down: "down",
    Qt.Key.Key_Left: "left", Qt.Key.Key_Right: "right",
    Qt.Key.Key_CapsLock: "capslock", Qt.Key.Key_NumLock: "numlock",
    Qt.Key.Key_ScrollLock: "scrolllock",
    Qt.Key.Key_Print: "printscreen", Qt.Key.Key_Pause: "pause",
}

def _qt_key(key_code: int) -> str | None:
    if key_code in _QT_KEY_NAMES:
        return _QT_KEY_NAMES[key_code]
    text = QKeySequence(key_code).toString().lower()
    return text if text else None


_LR_MOD_MAP = {
    Qt.Key.Key_Shift: [(0xA0, "lshift"), (0xA1, "rshift")],
    Qt.Key.Key_Control: [(0xA2, "lctrl"), (0xA3, "rctrl")],
    Qt.Key.Key_Alt: [(0xA4, "lalt"), (0xA5, "ralt")],
}


def _lr_mod_press(qt_key: int) -> str | None:
    pairs = _LR_MOD_MAP.get(qt_key)
    if not pairs:
        return None
    user32 = ctypes.windll.user32
    for vk, name in pairs:
        if user32.GetKeyState(vk) & 0x8000:
            return name
    return None


def _lr_mod_release(qt_key: int, pressed: set) -> None:
    pairs = _LR_MOD_MAP.get(qt_key)
    if not pairs:
        return
    user32 = ctypes.windll.user32
    for vk, name in pairs:
        if name in pressed and not (user32.GetKeyState(vk) & 0x8000):
            pressed.discard(name)


class _HotkeyDialog(QDialog):
    """Captures a key/mouse combo — order-independent, with conflict check."""

    _STYLE_DEFAULT = """
        background:#2a2a2a; color:#fff; border:1px solid #555;
        border-radius:8px; font-size:18px; font-weight:bold;
    """
    _STYLE_OK = """
        background:#1a3a1a; color:#34c759; border:1px solid #34c759;
        border-radius:8px; font-size:18px; font-weight:bold;
    """
    _STYLE_ERR = """
        background:#3a1a1a; color:#ff3b30; border:1px solid #ff3b30;
        border-radius:8px; font-size:18px; font-weight:bold;
    """

    def __init__(self, current: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("设置快捷键")
        self.setWindowIcon(icons.app_icon())
        self.setFixedSize(360, 180)
        self.setStyleSheet("background:#1e1e1e; color:#fff;")
        self._current = _canonical(current.split("+"))
        self._result: str | None = None
        self._captured: str | None = None
        self._available = False

        self._pressed: set[str] = set()
        self._best: set[str] = set()

        self._settle = QTimer(self)
        self._settle.setSingleShot(True)
        self._settle.setInterval(150)
        self._settle.timeout.connect(self._finalize)

        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        hint = QLabel("按下新的快捷键或快捷键组合：")
        hint.setStyleSheet("font-size:13px;")
        layout.addWidget(hint)

        self._key_display = QLabel(_hotkey_display(self._current))
        self._key_display.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._key_display.setFixedHeight(44)
        self._key_display.setStyleSheet(self._STYLE_DEFAULT)
        layout.addWidget(self._key_display)

        self._status = QLabel("")
        self._status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status.setStyleSheet("font-size:12px; color:#999;")
        layout.addWidget(self._status)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self._btn_ok = QPushButton("保存")
        self._btn_ok.setFixedWidth(80)
        self._btn_ok.setEnabled(False)
        self._btn_ok.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._btn_ok.setStyleSheet("""
            QPushButton { background:#007aff; color:#fff; border:none;
                          border-radius:6px; padding:6px 14px; font-size:13px; }
            QPushButton:hover { background:#0066dd; }
            QPushButton:disabled { background:#333; color:#666; }
        """)
        self._btn_ok.clicked.connect(self._do_accept)
        btn_row.addWidget(self._btn_ok)
        btn_cancel = QPushButton("取消")
        btn_cancel.setFixedWidth(80)
        btn_cancel.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        btn_cancel.setStyleSheet("""
            QPushButton { background:transparent; color:#999; border:1px solid #444;
                          border-radius:6px; padding:6px 14px; font-size:13px; }
            QPushButton:hover { background:#2a2a2a; color:#fff; }
        """)
        btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(btn_cancel)
        layout.addLayout(btn_row)

        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._grab_sig = _HotkeyGrabSignals(self)
        self._grab_sig.key_down.connect(
            self._on_grab_key_down, Qt.ConnectionType.QueuedConnection)
        self._grab_sig.key_up.connect(
            self._on_grab_key_up, Qt.ConnectionType.QueuedConnection)
        self._grab_worker: _PynputHotkeyGrabWorker | None = None
        self._hotkey_grab_disposed = False
        self._fg_poll = QTimer(self)
        self._fg_poll.setInterval(150)
        self._fg_poll.timeout.connect(self.sync_hotkey_grab_with_activation)
        self._sync_grab_debounce = QTimer(self)
        self._sync_grab_debounce.setSingleShot(True)
        self._sync_grab_debounce.setInterval(0)
        self._sync_grab_debounce.timeout.connect(self.sync_hotkey_grab_with_activation)

    def _hotkey_dialog_is_foreground(self) -> bool:
        """本对话框是否为 Win32 前台窗口（比 isActiveWindow 可靠）。"""
        if sys.platform != "win32":
            return self.isActiveWindow()
        try:
            hwnd = int(self.winId())
        except Exception:
            return False
        if not hwnd:
            return False
        user32 = ctypes.windll.user32
        fg = user32.GetForegroundWindow()
        if not fg:
            return False
        if fg == hwnd:
            return True
        GA_ROOT = 2
        return bool(user32.GetAncestor(fg, GA_ROOT) == hwnd)

    def showEvent(self, event):
        super().showEvent(event)
        self._fg_poll.start()
        # 挂在 self 上的单发定时器，避免关窗后仍投递 singleShot 再次起钩子
        self._sync_grab_debounce.start()

    def changeEvent(self, event):
        if event.type() in (
                QEvent.Type.WindowActivate,
                QEvent.Type.WindowDeactivate,
                QEvent.Type.ActivationChange):
            self._sync_grab_debounce.start()
        super().changeEvent(event)

    def closeEvent(self, event):
        # 须先于停定时器，防止 sync 与 _start 在关窗过程中再起线程
        self._hotkey_grab_disposed = True
        self._sync_grab_debounce.stop()
        self._fg_poll.stop()
        self._stop_hotkey_grab()
        super().closeEvent(event)

    def sync_hotkey_grab_with_activation(self):
        """仅当本对话框为系统前台时挂全局钩子；否则撤钩并复位 chord。"""
        if self._hotkey_grab_disposed:
            return
        if self._hotkey_dialog_is_foreground():
            self._start_hotkey_grab_if_needed()
        else:
            self._release_hotkey_grab_on_deactivate()

    def _release_hotkey_grab_on_deactivate(self):
        """失焦/切到其他应用：撤钩子并清空 chord，避免收不到 keyup 导致状态错乱。"""
        self._stop_hotkey_grab()
        self._settle.stop()
        self._pressed.clear()
        self._best.clear()
        combo = self._captured if self._captured else self._current
        self._key_display.setText(_hotkey_display(combo))
        self._validate(combo)

    def _start_hotkey_grab_if_needed(self):
        if self._hotkey_grab_disposed:
            return
        if self._grab_worker is not None or sys.platform != "win32":
            return
        if not self._hotkey_dialog_is_foreground():
            return
        try:
            from pynput.keyboard import Listener as _KBL  # noqa: F401
        except Exception:
            logger.warning("[HotkeyDialog] No global key grab (pynput missing)")
            return
        logger.info("[HotkeyDialog] Starting pynput keyboard grab")
        self._grab_worker = _PynputHotkeyGrabWorker(self._grab_sig)
        self._grab_worker.start()

    def _stop_hotkey_grab(self):
        if self._grab_worker is None:
            return
        logger.info("[HotkeyDialog] Stopping pynput keyboard grab")
        self._grab_worker.stop_grab()
        self._grab_worker.join(timeout=5.0)
        if self._grab_worker.is_alive():
            logger.error(
                "[HotkeyDialog] Grab thread did not exit in 5s — "
                "keyboard may still be stuck")
        self._grab_worker = None

    def _on_grab_key_down(self, name: str):
        if name == "escape" and not self._pressed:
            self.reject()
            return
        if name in self._pressed:
            return
        self._settle.stop()
        self._pressed.add(name)
        if len(self._pressed) >= len(self._best):
            self._best = set(self._pressed)
            self._show_best()

    def _on_grab_key_up(self, name: str):
        self._pressed.discard(name)
        if not self._pressed:
            self._settle.start()

    def event(self, e):
        if e.type() == QEvent.Type.ShortcutOverride:
            e.accept()
            return True
        return super().event(e)

    def keyPressEvent(self, event):
        if self._grab_worker is not None:
            event.accept()
            return
        if event.isAutoRepeat():
            return
        event.accept()
        key = event.key()
        if key in (Qt.Key.Key_unknown, Qt.Key.Key_Meta):
            return
        name = _lr_mod_press(key) or _qt_key(key)
        if not name:
            return
        if name == "escape" and not self._pressed:
            self.reject()
            return
        self._settle.stop()
        self._pressed.add(name)
        if len(self._pressed) >= len(self._best):
            self._best = set(self._pressed)
            self._show_best()

    def keyReleaseEvent(self, event):
        if self._grab_worker is not None:
            event.accept()
            return
        if event.isAutoRepeat():
            return
        event.accept()
        key = event.key()
        if key in (Qt.Key.Key_Shift, Qt.Key.Key_Control, Qt.Key.Key_Alt):
            _lr_mod_release(key, self._pressed)
        else:
            name = _qt_key(key)
            if name:
                self._pressed.discard(name)
        if not self._pressed:
            self._settle.start()

    def _show_best(self):
        if self._best:
            combo = _canonical(self._best)
            self._key_display.setText(_hotkey_display(combo))
            self._key_display.setStyleSheet(self._STYLE_DEFAULT)
            self._status.setText("")
            self._btn_ok.setEnabled(False)
            self._available = False

    def _finalize(self):
        if not self._best:
            return
        combo = _canonical(self._best)
        self._captured = combo
        self._best = set()
        self._validate(combo)

    def _validate(self, combo: str):
        parts = combo.split("+")

        if len(parts) == 1:
            p = parts[0]
            is_fkey = p.startswith("f") and p[1:].isdigit()
            if not (p in _MOD_KEYS or is_fkey or p.startswith("mouse_")):
                self._key_display.setStyleSheet(self._STYLE_ERR)
                self._status.setText("不允许使用单个常用按键，请搭配其他键使用。")
                self._status.setStyleSheet("font-size:12px; color:#ff6b60;")
                self._btn_ok.setEnabled(False)
                return

        if combo == self._current:
            self._status.setText("与当前快捷键相同")
            self._status.setStyleSheet("font-size:12px; color:#999;")
            self._btn_ok.setEnabled(False)
            return

        conflict = _test_system_conflict(combo)
        if conflict is False:
            self._key_display.setStyleSheet(self._STYLE_ERR)
            self._status.setText("✕ 已被系统或其他程序占用")
            self._status.setStyleSheet("font-size:12px; color:#ff3b30;")
            self._btn_ok.setEnabled(False)
            self._available = False
            return

        self._key_display.setStyleSheet(self._STYLE_OK)
        self._status.setText("✓ 快捷键可用")
        self._status.setStyleSheet("font-size:12px; color:#34c759;")
        self._btn_ok.setEnabled(True)
        self._available = True

    def _do_accept(self):
        if self._captured and self._available:
            self._result = self._captured
            self.accept()

    @property
    def hotkey(self) -> str | None:
        return self._result


_MOD_FLAGS = {
    "ctrl": 0x0002, "lctrl": 0x0002, "rctrl": 0x0002,
    "shift": 0x0004, "lshift": 0x0004, "rshift": 0x0004,
    "alt": 0x0001, "lalt": 0x0001, "ralt": 0x0001,
}


def _test_system_conflict(combo: str) -> bool | None:
    """True=available, False=system occupied, None=can't test (non-standard combo)."""
    parts = combo.split("+")
    mods = [p for p in parts if p in _MOD_KEYS]
    non_mods = [p for p in parts if p not in _MOD_KEYS]
    if len(non_mods) != 1 or non_mods[0].startswith("mouse_"):
        return None
    vk = _NAME_TO_VK.get(non_mods[0], 0)
    if not vk:
        return None
    mod = 0
    for m in mods:
        mod |= _MOD_FLAGS.get(m, 0)
    user32 = ctypes.windll.user32
    tid = 0x7FFF
    ok = user32.RegisterHotKey(None, tid, mod, vk)
    if ok:
        user32.UnregisterHotKey(None, tid)
    return bool(ok)


class _ApiSettingsDialog(QDialog):
    """Dialog to configure API providers (DashScope and/or OpenAI-compatible)."""

    _SECTION_STYLE = "font-size:12px; color:#888; font-weight:bold;"
    _LABEL_STYLE = "font-size:13px;"
    _INPUT = """
        QLineEdit {
            background:#2a2a2a; color:#fff; border:1px solid #555;
            border-radius:6px; padding:7px; font-size:13px;
            font-family: Consolas, monospace;
        }
        QLineEdit:focus { border:1px solid #007aff; }
    """
    _RADIO = "QRadioButton { font-size:13px; color:#fff; } QRadioButton::indicator { width:14px; height:14px; }"
    _LINK_BTN = """
        QPushButton { background:transparent; color:#007aff; border:none; font-size:12px; }
        QPushButton:hover { color:#339aff; text-decoration:underline; }
    """
    _SHOW_BTN = """
        QPushButton { background:transparent; color:#999; border:none; font-size:12px; }
        QPushButton:hover { color:#fff; }
    """

    def __init__(self, config: "Config", parent=None):
        super().__init__(parent)
        self.setWindowTitle("API 设置")
        self.setWindowIcon(icons.app_icon())
        self.setFixedSize(480, 580)
        self.setStyleSheet("background:#1e1e1e; color:#fff;")
        self._result_accepted = False

        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.setContentsMargins(16, 14, 16, 14)

        # ── DashScope ──
        lbl_ds = QLabel("── DashScope (阿里云百炼) ──")
        lbl_ds.setStyleSheet(self._SECTION_STYLE)
        layout.addWidget(lbl_ds)

        ds_key_row = QHBoxLayout()
        ds_key_row.setSpacing(6)
        ds_key_lbl = QLabel("API Key:")
        ds_key_lbl.setStyleSheet(self._LABEL_STYLE)
        ds_key_row.addWidget(ds_key_lbl)
        btn_get_key = QPushButton("获取 ↗")
        btn_get_key.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_get_key.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        btn_get_key.setStyleSheet(self._LINK_BTN)
        btn_get_key.clicked.connect(lambda: QDesktopServices.openUrl(
            QUrl("https://bailian.console.aliyun.com/cn-beijing/?tab=model#/api-key")))
        ds_key_row.addStretch()
        ds_key_row.addWidget(btn_get_key)
        layout.addLayout(ds_key_row)

        ds_key_input_row = QHBoxLayout()
        ds_key_input_row.setSpacing(4)
        self._ds_key = QLineEdit(config.api_key)
        self._ds_key.setPlaceholderText("sk-...")
        self._ds_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._ds_key.setStyleSheet(self._INPUT)
        ds_key_input_row.addWidget(self._ds_key)
        ds_show = QPushButton("显示")
        ds_show.setFixedWidth(42)
        ds_show.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        ds_show.setStyleSheet(self._SHOW_BTN)
        ds_show.clicked.connect(lambda: self._toggle_echo(self._ds_key, ds_show))
        ds_key_input_row.addWidget(ds_show)
        layout.addLayout(ds_key_input_row)

        # ── 自定义端点 · 语音识别 (ASR) ──
        layout.addSpacing(6)
        lbl_asr = QLabel("── 自定义端点 · 语音识别 (ASR) ──")
        lbl_asr.setStyleSheet(self._SECTION_STYLE)
        layout.addWidget(lbl_asr)

        def _field(label_text, placeholder, value, password=False):
            row = QHBoxLayout()
            row.setSpacing(4)
            lbl = QLabel(label_text)
            lbl.setStyleSheet(self._LABEL_STYLE)
            lbl.setFixedWidth(72)
            inp = QLineEdit(value)
            inp.setPlaceholderText(placeholder)
            inp.setStyleSheet(self._INPUT)
            if password:
                inp.setEchoMode(QLineEdit.EchoMode.Password)
            row.addWidget(lbl)
            row.addWidget(inp)
            if password:
                show_btn = QPushButton("显示")
                show_btn.setFixedWidth(42)
                show_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
                show_btn.setStyleSheet(self._SHOW_BTN)
                show_btn.clicked.connect(lambda _=False, i=inp, b=show_btn: self._toggle_echo(i, b))
                row.addWidget(show_btn)
            return row, inp

        asr_key_row, self._asr_api_key = _field(
            "API Key:", "sk-...", config.custom_asr_api_key, password=True)
        layout.addLayout(asr_key_row)

        asr_url_row, self._asr_base_url = _field(
            "Base URL:", "https://api.openai.com/v1", config.custom_asr_base_url)
        layout.addLayout(asr_url_row)

        asr_model_row, self._custom_asr_model = _field(
            "模型:", "whisper-1", config.custom_asr_model)
        layout.addLayout(asr_model_row)

        # ── 自定义端点 · 文本润色 ──
        layout.addSpacing(6)
        lbl_polish = QLabel("── 自定义端点 · 文本润色 ──")
        lbl_polish.setStyleSheet(self._SECTION_STYLE)
        layout.addWidget(lbl_polish)

        polish_key_row, self._polish_api_key = _field(
            "API Key:", "sk-...", config.custom_polish_api_key, password=True)
        layout.addLayout(polish_key_row)

        polish_url_row, self._polish_base_url = _field(
            "Base URL:", "https://api.openai.com/v1", config.custom_polish_base_url)
        layout.addLayout(polish_url_row)

        polish_model_row, self._custom_polish_model = _field(
            "模型:", "gpt-4o-mini", config.custom_polish_model)
        layout.addLayout(polish_model_row)

        # ── 提供商选择 ──
        layout.addSpacing(6)
        lbl_provider = QLabel("── 提供商选择 ──")
        lbl_provider.setStyleSheet(self._SECTION_STYLE)
        layout.addWidget(lbl_provider)

        providers_row = QHBoxLayout()
        providers_row.setSpacing(24)

        asr_col = QVBoxLayout()
        asr_col.setSpacing(4)
        asr_col.addWidget(QLabel("语音识别 (ASR):"))
        self._asr_grp = QButtonGroup(self)
        self._asr_ds = QRadioButton("DashScope")
        self._asr_ds.setStyleSheet(self._RADIO)
        self._asr_oc = QRadioButton("自定义端点")
        self._asr_oc.setStyleSheet(self._RADIO)
        self._asr_grp.addButton(self._asr_ds, 0)
        self._asr_grp.addButton(self._asr_oc, 1)
        if config.asr_provider == "openai_compat":
            self._asr_oc.setChecked(True)
        else:
            self._asr_ds.setChecked(True)
        asr_col.addWidget(self._asr_ds)
        asr_col.addWidget(self._asr_oc)
        providers_row.addLayout(asr_col)

        polish_col = QVBoxLayout()
        polish_col.setSpacing(4)
        polish_col.addWidget(QLabel("文本润色 (Polish):"))
        self._polish_grp = QButtonGroup(self)
        self._polish_ds = QRadioButton("DashScope")
        self._polish_ds.setStyleSheet(self._RADIO)
        self._polish_oc = QRadioButton("自定义端点")
        self._polish_oc.setStyleSheet(self._RADIO)
        self._polish_grp.addButton(self._polish_ds, 0)
        self._polish_grp.addButton(self._polish_oc, 1)
        if config.polish_provider == "openai_compat":
            self._polish_oc.setChecked(True)
        else:
            self._polish_ds.setChecked(True)
        polish_col.addWidget(self._polish_ds)
        polish_col.addWidget(self._polish_oc)
        providers_row.addLayout(polish_col)
        providers_row.addStretch()

        layout.addLayout(providers_row)
        layout.addStretch()

        # ── 按钮行 ──
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_ok = QPushButton("保存")
        btn_ok.setFixedWidth(80)
        btn_ok.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        btn_ok.setStyleSheet("""
            QPushButton { background:#007aff; color:#fff; border:none;
                          border-radius:6px; padding:6px 14px; font-size:13px; }
            QPushButton:hover { background:#0066dd; }
        """)
        btn_ok.clicked.connect(self._do_save)
        btn_row.addWidget(btn_ok)
        btn_cancel = QPushButton("取消")
        btn_cancel.setFixedWidth(80)
        btn_cancel.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        btn_cancel.setStyleSheet("""
            QPushButton { background:transparent; color:#999; border:1px solid #444;
                          border-radius:6px; padding:6px 14px; font-size:13px; }
            QPushButton:hover { background:#2a2a2a; color:#fff; }
        """)
        btn_cancel.clicked.connect(self.reject)
        btn_row.addWidget(btn_cancel)
        layout.addLayout(btn_row)

    @staticmethod
    def _toggle_echo(inp: QLineEdit, btn: QPushButton):
        if inp.echoMode() == QLineEdit.EchoMode.Password:
            inp.setEchoMode(QLineEdit.EchoMode.Normal)
            btn.setText("隐藏")
        else:
            inp.setEchoMode(QLineEdit.EchoMode.Password)
            btn.setText("显示")

    def _do_save(self):
        self._result_accepted = True
        self.accept()

    @property
    def api_key(self) -> str:
        return self._ds_key.text().strip()

    @property
    def custom_asr_api_key(self) -> str:
        return self._asr_api_key.text().strip()

    @property
    def custom_asr_base_url(self) -> str:
        return self._asr_base_url.text().strip()

    @property
    def custom_asr_model(self) -> str:
        return self._custom_asr_model.text().strip()

    @property
    def custom_polish_api_key(self) -> str:
        return self._polish_api_key.text().strip()

    @property
    def custom_polish_base_url(self) -> str:
        return self._polish_base_url.text().strip()

    @property
    def custom_polish_model(self) -> str:
        return self._custom_polish_model.text().strip()

    @property
    def asr_provider(self) -> str:
        return "openai_compat" if self._asr_grp.checkedId() == 1 else "dashscope"

    @property
    def polish_provider(self) -> str:
        return "openai_compat" if self._polish_grp.checkedId() == 1 else "dashscope"


class _KeepWhiteTextDelegate(QStyledItemDelegate):
    """Force white text in all states so selection/hover never flips to black."""

    def initStyleOption(self, option: QStyleOptionViewItem, index):
        super().initStyleOption(option, index)
        option.palette.setColor(option.palette.ColorRole.Text, QColor("#fff"))
        option.palette.setColor(option.palette.ColorRole.HighlightedText, QColor("#fff"))


class _DragReorderListWidget(QListWidget):
    """QListWidget with live drag-to-reorder: items swap as the cursor passes over them.

    Row 0 (默认提示词) is pinned and cannot be dragged or swapped into.
    Emits *orderChanged(int, int)* after each swap with (from_row, to_row).
    """

    orderChanged = pyqtSignal(int, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._drag_row: int = -1
        self._dragging = False

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            row = self.row(self.itemAt(event.pos()))
            if row >= 1:
                self._drag_row = row
                self._dragging = True
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._dragging and self._drag_row >= 1:
            target = self.row(self.itemAt(event.pos()))
            if target >= 1 and target != self._drag_row:
                self._swap_rows(self._drag_row, target)
                self._drag_row = target
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = False
            self._drag_row = -1
        super().mouseReleaseEvent(event)

    def _swap_rows(self, a: int, b: int):
        if a < 1 or b < 1:
            return
        item_a = self.item(a)
        item_b = self.item(b)
        if item_a is None or item_b is None:
            return
        text_a, data_a = item_a.text(), item_a.data(Qt.ItemDataRole.UserRole)
        text_b, data_b = item_b.text(), item_b.data(Qt.ItemDataRole.UserRole)
        item_a.setText(text_b)
        item_a.setData(Qt.ItemDataRole.UserRole, data_b)
        item_b.setText(text_a)
        item_b.setData(Qt.ItemDataRole.UserRole, data_a)
        self.orderChanged.emit(a, b)
        self.setCurrentRow(b)


_DEFAULT_PROMPT_ID = "__default__"
# QAction.data 标记「默认提示词」项，用于仅同步勾选而不 clear 子菜单（避免首次弹出错位）
_TRAY_MENU_DEFAULT_PROMPT = "__tray_default_prompt__"

# ── 管理提示词：QSS 调色板与组合样式（颜色单源；滚动条仅一处实现） ──
_PROMPT_QSS_PANEL_BG = "#2a2a2a"
_PROMPT_QSS_PANEL_BORDER = "#555"
_PROMPT_QSS_READONLY_BG = "#252525"
_PROMPT_QSS_READONLY_BORDER = "#444"
_PROMPT_QSS_DIALOG_BG = "#1e1e1e"
_PROMPT_QSS_SPLITTER_HANDLE = "#444"
_PROMPT_QSS_SB_HANDLE = "#555"
_PROMPT_QSS_SB_HANDLE_HOVER = "#666"
_PROMPT_QSS_FOCUS = "#007aff"
_PROMPT_QSS_MSGBOX_FG = "#ececec"
_PROMPT_QSS_TOOLTIP_BG = "#2d2d2d"


def _prompt_qss_scrollbar(widget_prefix: str, track_bg: str) -> str:
    """列表/编辑区滚动条：轨道与面板同色，滑块统一灰阶；避免在多处复制 QSS。"""
    h, hh = _PROMPT_QSS_SB_HANDLE, _PROMPT_QSS_SB_HANDLE_HOVER
    return f"""
    {widget_prefix} QScrollBar:vertical {{ background: {track_bg}; }}
    {widget_prefix} QScrollBar::handle:vertical {{ background: {h}; }}
    {widget_prefix} QScrollBar::handle:vertical:hover {{ background: {hh}; }}
    {widget_prefix} QScrollBar:horizontal {{ background: {track_bg}; }}
    {widget_prefix} QScrollBar::handle:horizontal {{ background: {h}; }}
    {widget_prefix} QScrollBar::handle:horizontal:hover {{ background: {hh}; }}
    """


# 主对话框外壳（与二级 QMessageBox、列表面板样式表分离，避免互相套用）
_PROMPT_DIALOG_CHROME_QSS = f"""
    QDialog {{
        background: {_PROMPT_QSS_DIALOG_BG};
        color: #fff;
        border: none;
        border-radius: 0px;
    }}
    QToolTip {{
        background-color: {_PROMPT_QSS_TOOLTIP_BG};
        color: {_PROMPT_QSS_MSGBOX_FG};
        border: 1px solid {_PROMPT_QSS_PANEL_BORDER};
        padding: 6px 9px;
        border-radius: 4px;
        font-size: 12px;
        max-width: 420px;
    }}
"""

_INPUT_STYLE = f"""
    QLineEdit {{
        background: {_PROMPT_QSS_PANEL_BG};
        color: #fff;
        border: 1px solid {_PROMPT_QSS_PANEL_BORDER};
        border-radius: 6px;
        padding: 8px;
        font-size: 13px;
    }}
    QLineEdit:focus {{ border: 1px solid {_PROMPT_QSS_FOCUS}; }}
    QLineEdit:read-only {{ color: #999; }}
"""
_TEXTEDIT_STYLE = f"""
    QTextEdit {{
        background: {_PROMPT_QSS_PANEL_BG};
        color: #fff;
        border: 1px solid {_PROMPT_QSS_PANEL_BORDER};
        border-radius: 6px;
        padding: 8px;
        font-size: 13px;
    }}
    QTextEdit:focus {{ border: 1px solid {_PROMPT_QSS_FOCUS}; }}
""" + _prompt_qss_scrollbar("QTextEdit", _PROMPT_QSS_PANEL_BG)

_TEXTEDIT_READONLY_STYLE = f"""
    QTextEdit {{
        background: {_PROMPT_QSS_READONLY_BG};
        color: #999;
        border: 1px solid {_PROMPT_QSS_READONLY_BORDER};
        border-radius: 6px;
        padding: 8px;
        font-size: 13px;
    }}
""" + _prompt_qss_scrollbar("QTextEdit", _PROMPT_QSS_READONLY_BG)

_INPUT_READONLY_STYLE = f"""
    QLineEdit {{
        background: {_PROMPT_QSS_READONLY_BG};
        color: #999;
        border: 1px solid {_PROMPT_QSS_READONLY_BORDER};
        border-radius: 6px;
        padding: 8px;
        font-size: 13px;
    }}
"""
_LIST_STYLE = f"""
    QListWidget {{
        background: {_PROMPT_QSS_PANEL_BG};
        border: 1px solid {_PROMPT_QSS_PANEL_BORDER};
        border-radius: 6px;
        padding: 4px;
        font-size: 13px;
        color: #fff;
        outline: none;
    }}
    QListWidget::item {{
        padding: 8px 10px;
        border-radius: 4px;
        margin: 2px 0;
        border: 1px solid transparent;
        color: #fff;
    }}
    QListWidget::item:selected {{
        background: transparent;
        border: 1px solid {_PROMPT_QSS_FOCUS};
        color: #fff;
    }}
    QListWidget::item:hover:!selected {{ background: #333; color: #fff; }}
""" + _prompt_qss_scrollbar("QListWidget", _PROMPT_QSS_PANEL_BG)

# 管理提示词按钮：统一尺寸；主色按钮用与底色同色的 1px 边框（勿用 border:none），否则比灰底按钮少 2px 高
_PROMPT_QSS_BTN_METRICS = (
    "border-radius: 6px; padding: 4px 14px; font-size: 13px; min-height: 26px;"
)

_BTN = f"""
    QPushButton {{ background:#333; color:#fff; border:1px solid #555;
                  {_PROMPT_QSS_BTN_METRICS} }}
    QPushButton:hover {{ background:#444; border-color:#666; }}
    QPushButton:disabled {{ color:#555; border-color:#444; }}
"""
_BTN_DANGER = f"""
    QPushButton {{ background:transparent; color:#ff6b60; border:1px solid #553030;
                  {_PROMPT_QSS_BTN_METRICS} }}
    QPushButton:hover {{ background:#3a1a1a; border-color:#ff3b30; }}
    QPushButton:disabled {{ color:#553030; border-color:#444; }}
"""
_BTN_PRIMARY = f"""
    QPushButton {{ background:{_PROMPT_QSS_FOCUS}; color:#fff; border:1px solid {_PROMPT_QSS_FOCUS};
                  {_PROMPT_QSS_BTN_METRICS} }}
    QPushButton:hover {{ background:#0066dd; border-color:#0066dd; }}
"""
# 二级 QMessageBox：背景与列表面板一致；按钮样式与主面板底栏相同（_BTN / _BTN_PRIMARY / _BTN_DANGER）
_PROMPT_MSGBOX_STYLE = f"""
    QMessageBox {{
        background-color: {_PROMPT_QSS_PANEL_BG};
        color: {_PROMPT_QSS_MSGBOX_FG};
        border: 1px solid {_PROMPT_QSS_PANEL_BORDER};
    }}
    QMessageBox QLabel {{
        color: {_PROMPT_QSS_MSGBOX_FG};
        font-size: 13px;
    }}
"""
class _PolishPromptDialog(QDialog):
    """Split-pane prompt manager.

    Left: prompt list (click = browse, double-click or button = activate).
    Right: inline name + content editor.

    数据约定：`_prompts` 为自定义条目的有序列表；列表第 0 行为内置「默认提示词」
    （不在 `_prompts` 内）。右侧编辑区始终绑定 `_editing_prompt_id` 所指的 dict；
    写回内存时只按 id 查找，绝不使用「行号 − 1」当下标，以免拖拽重排后错位。
    """

    _BTN_SAVE_CLEAN = f"""
        QPushButton {{ background:#333; color:#888; border:1px solid #555;
                      {_PROMPT_QSS_BTN_METRICS} }}
        QPushButton:hover {{ background:#444; color:#aaa; }}
    """
    _BTN_SAVE_DIRTY = f"""
        QPushButton {{ background:{_PROMPT_QSS_FOCUS}; color:#fff; border:1px solid {_PROMPT_QSS_FOCUS};
                      {_PROMPT_QSS_BTN_METRICS} }}
        QPushButton:hover {{ background:#0066dd; border-color:#0066dd; }}
    """
    _BTN_ACTIVATE_ON = f"""
        QPushButton {{ background:#0a5c2a; color:#4cdf90; border:1px solid #1a8040;
                      {_PROMPT_QSS_BTN_METRICS} }}
    """
    _BTN_ACTIVATE_OFF = f"""
        QPushButton {{ background:#333; color:#fff; border:1px solid #555;
                      {_PROMPT_QSS_BTN_METRICS} }}
        QPushButton:hover {{ background:#444; border-color:#666; }}
    """
    # 与 _BTN 同尺寸；用于「还原此项」不可用时的视觉灰化（保持 enabled 以便显示悬停提示）
    _BTN_REVERT_INACTIVE = f"""
        QPushButton {{ background:{_PROMPT_QSS_PANEL_BG}; color:#666; border:1px solid #444;
                      {_PROMPT_QSS_BTN_METRICS} }}
        QPushButton:hover {{ background:#333; color:#888; border-color:#555; }}
    """

    def __init__(self, prompts: list, active_id: str, default_text: str = "",
                 config: Config | None = None, parent=None,
                 on_active_applied: Callable[[], None] | None = None,
                 on_prompts_saved: Callable[[], None] | None = None,
                 run_modal_with_hotkey_paused: Callable[
                     [Callable[[], Any]], Any] | None = None):
        super().__init__(parent)
        self.setWindowTitle("管理提示词")
        self.setWindowIcon(icons.app_icon())
        self.setMinimumSize(680, 420)
        self.setStyleSheet(_PROMPT_DIALOG_CHROME_QSS)

        self._prompts: list[dict] = copy.deepcopy(prompts) if prompts else []
        self._active_id: str = active_id or ""
        self._default_text: str = default_text
        self._config_ref: Config | None = config
        self._on_active_applied = on_active_applied
        self._on_prompts_saved = on_prompts_saved
        self._run_modal_with_hotkey_paused = run_modal_with_hotkey_paused
        self._accepted = False
        self._switching = False
        self._last_row: int = -1
        self._editing_prompt_id: str = ""

        root = QVBoxLayout(self)
        root.setSpacing(8)

        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        splitter.setHandleWidth(1)
        splitter.setStyleSheet(
            f"QSplitter::handle {{ background:{_PROMPT_QSS_SPLITTER_HANDLE}; }}")

        # ── left panel ──
        left = QWidget()
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(0, 0, 4, 0)
        left_lay.setSpacing(6)

        self._list = _DragReorderListWidget()
        self._list.setStyleSheet(_LIST_STYLE)
        self._list.setItemDelegate(_KeepWhiteTextDelegate(self._list))
        self._list.orderChanged.connect(self._on_list_order_swapped)
        left_lay.addWidget(self._list)

        left_btns = QHBoxLayout()
        left_btns.setSpacing(6)
        left_btns.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        btn_add = QPushButton("+ 新增")
        btn_add.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        btn_add.setStyleSheet(_BTN)
        btn_add.clicked.connect(self._add_item)
        left_btns.addWidget(btn_add)
        self._btn_duplicate = QPushButton("复制")
        self._btn_duplicate.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._btn_duplicate.setStyleSheet(_BTN)
        self._btn_duplicate.clicked.connect(self._duplicate_item)
        left_btns.addWidget(self._btn_duplicate)
        self._btn_delete = QPushButton("删除")
        self._btn_delete.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._btn_delete.setStyleSheet(_BTN_DANGER)
        self._btn_delete.clicked.connect(self._delete_item)
        left_btns.addWidget(self._btn_delete)
        left_btns.addStretch()
        left_lay.addLayout(left_btns)

        splitter.addWidget(left)

        # ── right panel ──
        right = QWidget()
        right_lay = QVBoxLayout(right)
        right_lay.setContentsMargins(4, 0, 0, 0)
        right_lay.setSpacing(8)

        _LBL_MUTED = "font-size:12px; color:#aaa;"
        _LBL_STAR = "font-size:12px; color:#ff9f0a; padding:0 1px;"
        _LBL_HINT = "font-size:12px; color:#ff9f0a; padding:0 2px;"

        top_row = QHBoxLayout()
        top_row.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        name_title = QHBoxLayout()
        name_title.setSpacing(0)
        self._lbl_name_text = QLabel("名称")
        self._lbl_name_text.setStyleSheet(_LBL_MUTED)
        self._lbl_name_star = QLabel("")
        self._lbl_name_star.setStyleSheet(_LBL_STAR)
        self._lbl_name_star.setVisible(False)
        name_title.addWidget(self._lbl_name_text)
        name_title.addWidget(self._lbl_name_star)
        top_row.addLayout(name_title)
        top_row.addStretch()
        self._btn_revert_row = QPushButton("还原此项")
        self._btn_revert_row.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._btn_revert_row.setStyleSheet(self._BTN_REVERT_INACTIVE)
        self._btn_revert_row.setToolTip("将本条名称与内容恢复为已保存的磁盘版本")
        self._btn_revert_row.clicked.connect(self._revert_current_row_from_disk)
        top_row.addWidget(self._btn_revert_row)
        self._btn_activate = QPushButton("设为当前")
        self._btn_activate.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._btn_activate.setStyleSheet(self._BTN_ACTIVATE_OFF)
        self._btn_activate.clicked.connect(self._activate_selected)
        top_row.addWidget(self._btn_activate)
        right_lay.addLayout(top_row)

        self._name_input = QLineEdit()
        self._name_input.setPlaceholderText("例：学术论文风格")
        self._name_input.setStyleSheet(_INPUT_STYLE)
        right_lay.addWidget(self._name_input)

        content_row = QHBoxLayout()
        content_title = QHBoxLayout()
        content_title.setSpacing(0)
        self._lbl_content_text = QLabel("提示词内容")
        self._lbl_content_text.setStyleSheet(_LBL_MUTED)
        self._lbl_content_star = QLabel("")
        self._lbl_content_star.setStyleSheet(_LBL_STAR)
        self._lbl_content_star.setVisible(False)
        content_title.addWidget(self._lbl_content_text)
        content_title.addWidget(self._lbl_content_star)
        content_row.addLayout(content_title)
        content_row.addStretch()
        self._lbl_row_unsaved = QLabel("")
        self._lbl_row_unsaved.setStyleSheet(_LBL_HINT)
        self._lbl_row_unsaved.setVisible(False)
        self._lbl_row_unsaved.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        content_row.addWidget(self._lbl_row_unsaved)
        right_lay.addLayout(content_row)

        self._content_edit = QTextEdit()
        self._content_edit.setPlaceholderText("输入润色提示词内容…")
        self._content_edit.setStyleSheet(_TEXTEDIT_STYLE)
        right_lay.addWidget(self._content_edit)

        right_btns = QHBoxLayout()
        right_btns.setSpacing(8)
        right_btns.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        self._btn_restore_factory = QPushButton("恢复默认模板")
        self._btn_restore_factory.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._btn_restore_factory.setStyleSheet(_BTN)
        self._btn_restore_factory.clicked.connect(self._restore_factory_defaults)
        right_btns.addWidget(self._btn_restore_factory)
        right_btns.addStretch()
        self._btn_revert_all = QPushButton("全部还原")
        self._btn_revert_all.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._btn_revert_all.setStyleSheet(self._BTN_REVERT_INACTIVE)
        self._btn_revert_all.setToolTip(
            "将全部提示词列表恢复为最后一次保存的磁盘版本（丢弃未保存修改）")
        self._btn_revert_all.clicked.connect(self._revert_all_from_disk)
        right_btns.addWidget(self._btn_revert_all)
        self._btn_save = QPushButton("保存")
        self._btn_save.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._btn_save.setStyleSheet(self._BTN_SAVE_CLEAN)
        self._btn_save.clicked.connect(self._do_save)
        right_btns.addWidget(self._btn_save)
        btn_close = QPushButton("关闭")
        btn_close.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        btn_close.setStyleSheet(_BTN)
        btn_close.clicked.connect(self.close)
        right_btns.addWidget(btn_close)
        right_lay.addLayout(right_btns)

        splitter.addWidget(right)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        root.addWidget(splitter)

        self._name_input.textChanged.connect(self._on_editor_changed)
        self._content_edit.textChanged.connect(self._on_editor_changed)
        self._list.currentRowChanged.connect(self._on_row_changed)
        self._list.itemDoubleClicked.connect(self._on_item_double_clicked)
        self._refresh_list()
        self._select_active_row()
        self._update_save_button()

    # ── 底部「保存」= 全局写入名称/内容/列表；「当前使用项」单独立即写入磁盘，不计入未保存 ──

    def _prompt_data_differs_from_disk(self) -> bool:
        """与磁盘不一致时视为未保存：仅名称、提示词内容、增删列表；不含 active_prompt_id。
        调用方须已调用过 flush 将编辑区写回 _prompts。"""
        if self._config_ref is None:
            return False
        disk = self._config_ref.custom_prompts
        if len(self._prompts) != len(disk):
            return True
        if [p["id"] for p in self._prompts] != [p["id"] for p in disk]:
            return True
        disk_map = {p["id"]: p for p in disk}
        for p in self._prompts:
            d = disk_map.get(p["id"])
            if d is None:
                return True
            if (p.get("name") or "") != (d.get("name") or "") or (
                p.get("content") or "") != (d.get("content") or ""):
                return True
        return False

    def _is_custom_entry_unsaved(self, pid: str) -> bool:
        """当前内存中的该条与磁盘 config 中是否不一致（不含 active_id，仅名称与内容）。"""
        if self._config_ref is None or not pid:
            return False
        p = next((x for x in self._prompts if x["id"] == pid), None)
        if p is None:
            return True
        d = next(
            (x for x in self._config_ref.custom_prompts if x["id"] == pid),
            None,
        )
        if d is None:
            return True
        return (p.get("name") or "") != (d.get("name") or "") or (
            p.get("content") or "") != (d.get("content") or "")

    def _disk_custom_entry(self, pid: str):
        if self._config_ref is None or not pid:
            return None
        return next(
            (x for x in self._config_ref.custom_prompts if x["id"] == pid),
            None,
        )

    def _is_custom_name_unsaved(self, pid: str) -> bool:
        if self._config_ref is None or not pid:
            return False
        p = next((x for x in self._prompts if x["id"] == pid), None)
        if p is None:
            return True
        d = self._disk_custom_entry(pid)
        if d is None:
            return True
        return (p.get("name") or "") != (d.get("name") or "")

    def _is_custom_content_unsaved(self, pid: str) -> bool:
        if self._config_ref is None or not pid:
            return False
        p = next((x for x in self._prompts if x["id"] == pid), None)
        if p is None:
            return True
        d = self._disk_custom_entry(pid)
        if d is None:
            return True
        return (p.get("content") or "") != (d.get("content") or "")

    def _format_row_text(self, row: int) -> str:
        if row == 0:
            return ("● " if not self._active_id else "   ") + "默认提示词"
        idx = row - 1
        if idx < 0 or idx >= len(self._prompts):
            return ""
        p = self._prompts[idx]
        pid = p["id"]
        prefix = "● " if self._active_id == pid else "   "
        name = (p.get("name") or "").strip() or "未命名"
        mark = " *" if self._is_custom_entry_unsaved(pid) else ""
        return f"{prefix}{name}{mark}"

    def _update_prompt_list_labels(self):
        """同步左侧列表文案（名称、未保存 *），不整表 clear，避免闪烁。

        调用方须已调用过 flush 将编辑区写回 _prompts。
        """
        if self._list.count() != 1 + len(self._prompts):
            self._refresh_list()
            return
        self._switching = True
        for row in range(self._list.count()):
            it = self._list.item(row)
            if it is not None:
                it.setText(self._format_row_text(row))
        self._switching = False

    def _update_right_unsaved_hint(self):
        """刷新右侧编辑区的未保存标记。使用 _last_row，调用方须已 flush。"""
        row = self._last_row
        try:
            if row < 0:
                self._lbl_row_unsaved.setVisible(False)
                self._lbl_name_star.setVisible(False)
                self._lbl_content_star.setVisible(False)
                return
            if self._is_default_row(row):
                self._lbl_name_star.setVisible(False)
                self._lbl_content_star.setVisible(False)
                self._lbl_row_unsaved.setVisible(False)
                return
            pid = self._selected_prompt_id(row)
            self._lbl_name_star.setText("*")
            self._lbl_name_star.setVisible(self._is_custom_name_unsaved(pid))
            self._lbl_content_star.setText("*")
            self._lbl_content_star.setVisible(self._is_custom_content_unsaved(pid))
            if self._is_custom_entry_unsaved(pid):
                self._lbl_row_unsaved.setText("本条修改尚未保存")
                self._lbl_row_unsaved.setVisible(True)
            else:
                self._lbl_row_unsaved.setVisible(False)
        finally:
            self._update_revert_row_button()

    def _update_revert_row_button(self):
        """「还原此项」可点击当且仅当：自定义项、磁盘已有该条、本条相对磁盘未保存。
        保持 QPushButton 为 enabled，用样式区分可点/灰显。调用方须已 flush。"""
        row = self._last_row
        if self._config_ref is None or row < 0:
            self._btn_revert_row.setStyleSheet(self._BTN_REVERT_INACTIVE)
            return
        if self._is_default_row(row):
            self._btn_revert_row.setStyleSheet(self._BTN_REVERT_INACTIVE)
            return
        pid = self._selected_prompt_id(row)
        if not pid:
            self._btn_revert_row.setStyleSheet(self._BTN_REVERT_INACTIVE)
            return
        d = self._disk_custom_entry(pid)
        if d is None:
            self._btn_revert_row.setStyleSheet(self._BTN_REVERT_INACTIVE)
            return
        if self._is_custom_entry_unsaved(pid):
            self._btn_revert_row.setStyleSheet(_BTN)
        else:
            self._btn_revert_row.setStyleSheet(self._BTN_REVERT_INACTIVE)

    def _update_save_button(self):
        if self._prompt_data_differs_from_disk():
            self._btn_save.setStyleSheet(self._BTN_SAVE_DIRTY)
            self._btn_save.setText("● 保存")
        else:
            self._btn_save.setStyleSheet(self._BTN_SAVE_CLEAN)
            self._btn_save.setText("保存")
        self._update_revert_all_button()

    def _update_revert_all_button(self):
        """「全部还原」：有磁盘快照且与内存不一致时可点，与「保存」脏状态一致。"""
        if self._config_ref is None:
            self._btn_revert_all.setStyleSheet(self._BTN_REVERT_INACTIVE)
            return
        if self._prompt_data_differs_from_disk():
            self._btn_revert_all.setStyleSheet(_BTN)
        else:
            self._btn_revert_all.setStyleSheet(self._BTN_REVERT_INACTIVE)

    def sync_from_config(self) -> None:
        """托盘等处已更新 config 时，将当前使用中的提示词与列表展示同步到磁盘状态。"""
        if self._config_ref is None:
            return
        self._flush_editing_prompt()
        self._active_id = self._config_ref.active_prompt_id or ""
        self._refresh_list()
        self._select_active_row()
        self._update_save_button()

    def _on_editor_changed(self):
        if self._switching:
            return
        self._flush_editing_prompt()
        self._update_prompt_list_labels()
        self._update_right_unsaved_hint()
        self._update_save_button()

    # ── list ──

    def _refresh_list(self):
        """重建左侧列表项。不修改 _last_row——由调用方在操作完成后显式设置。"""
        self._switching = True
        prev = self._list.currentRow()
        self._list.clear()
        item = QListWidgetItem(self._format_row_text(0))
        item.setData(Qt.ItemDataRole.UserRole, _DEFAULT_PROMPT_ID)
        self._list.addItem(item)
        for i in range(len(self._prompts)):
            row = i + 1
            p = self._prompts[i]
            it = QListWidgetItem(self._format_row_text(row))
            it.setData(Qt.ItemDataRole.UserRole, p["id"])
            self._list.addItem(it)
        if prev >= 0 and prev < self._list.count():
            self._list.setCurrentRow(prev)
        self._switching = False

    def _select_active_row(self):
        target = 0
        if self._active_id:
            for i, p in enumerate(self._prompts):
                if p["id"] == self._active_id:
                    target = i + 1
                    break
        self._switching = True
        self._list.setCurrentRow(target)
        self._switching = False
        self._last_row = target
        self._load_editor(target)

    def _is_default_row(self, row: int) -> bool:
        if row < 0:
            return True
        item = self._list.item(row)
        return item is not None and item.data(Qt.ItemDataRole.UserRole) == _DEFAULT_PROMPT_ID

    def _selected_prompt_id(self, row: int) -> str:
        if row < 0:
            return ""
        item = self._list.item(row)
        if item is None:
            return ""
        pid = item.data(Qt.ItemDataRole.UserRole)
        return "" if pid == _DEFAULT_PROMPT_ID else (pid or "")

    # ── selection (browse only, no activation) ──

    def _on_row_changed(self, row: int):
        if self._switching:
            return
        self._flush_editing_prompt()
        self._last_row = row
        self._load_editor(row)
        self._update_save_button()

    def _on_list_order_swapped(self, row_a: int, row_b: int):
        """Visual rows swapped — rebuild _prompts order from the list widget."""
        self._flush_editing_prompt()
        id_to_prompt = {p["id"]: p for p in self._prompts}
        new_order: list[dict] = []
        for i in range(self._list.count()):
            it = self._list.item(i)
            if it is None:
                continue
            pid = it.data(Qt.ItemDataRole.UserRole)
            if pid == _DEFAULT_PROMPT_ID:
                continue
            p = id_to_prompt.get(pid)
            if p is not None:
                new_order.append(p)
        self._prompts = new_order
        self._persist_order_to_disk()
        self._update_prompt_list_labels()
        self._update_save_button()

    def _load_editor(self, row: int):
        self._switching = True
        is_default = self._is_default_row(row)
        if is_default:
            self._editing_prompt_id = _DEFAULT_PROMPT_ID
            self._name_input.setText("默认提示词")
            self._content_edit.setPlainText(self._default_text)
            self._name_input.setReadOnly(True)
            self._content_edit.setReadOnly(True)
            self._name_input.setStyleSheet(_INPUT_READONLY_STYLE)
            self._content_edit.setStyleSheet(_TEXTEDIT_READONLY_STYLE)
        else:
            pid = self._selected_prompt_id(row)
            self._editing_prompt_id = pid or ""
            p = next((x for x in self._prompts if x["id"] == pid), None) if pid else None
            if p is not None:
                self._name_input.setText(p.get("name", ""))
                self._content_edit.setPlainText(p.get("content", ""))
            self._name_input.setReadOnly(False)
            self._content_edit.setReadOnly(False)
            self._name_input.setStyleSheet(_INPUT_STYLE)
            self._content_edit.setStyleSheet(_TEXTEDIT_STYLE)
        self._btn_delete.setEnabled(not is_default)
        self._btn_duplicate.setEnabled(not is_default)
        self._update_activate_button(row)
        self._switching = False
        self._update_prompt_list_labels()
        self._update_right_unsaved_hint()

    def _update_activate_button(self, row: int):
        pid = self._selected_prompt_id(row)
        is_default = self._is_default_row(row)
        already_active = (is_default and not self._active_id) or \
                         (not is_default and pid == self._active_id)
        if already_active:
            self._btn_activate.setText("✓ 当前使用中")
            self._btn_activate.setStyleSheet(self._BTN_ACTIVATE_ON)
            self._btn_activate.setEnabled(False)
        else:
            self._btn_activate.setText("设为当前")
            self._btn_activate.setStyleSheet(self._BTN_ACTIVATE_OFF)
            self._btn_activate.setEnabled(True)

    def _flush_editing_prompt(self) -> None:
        """将右侧编辑区写回 `_editing_prompt_id` 在 `_prompts` 中对应条目。

        唯一写回入口：行切换、重排、保存、关闭等凡需落内存处均调用本方法，
        不根据 QListWidget 行号当下标，避免拖拽后顺序与下标不一致。
        """
        pid = self._editing_prompt_id
        if not pid or pid == _DEFAULT_PROMPT_ID:
            return
        name = self._name_input.text().strip()
        content = self._content_edit.toPlainText().strip()
        for p in self._prompts:
            if p["id"] == pid:
                p["name"] = name
                p["content"] = content
                return

    def _persist_order_to_disk(self) -> None:
        """将当前内存中的条目顺序写盘，但保留磁盘版的 name/content。

        - 磁盘已有的条目：按内存 _prompts 的新顺序排列，name/content 保持磁盘值不变。
        - 磁盘没有的条目（新增未保存）：不写入磁盘，等用户手动保存。
        - 磁盘有但内存已删除的条目：不写入磁盘（删除的效果随拖拽生效）。
        """
        if self._config_ref is None:
            return
        disk_map = {p["id"]: p for p in self._config_ref.custom_prompts}
        reordered = [disk_map[p["id"]] for p in self._prompts if p["id"] in disk_map]
        self._config_ref.custom_prompts = reordered
        self._config_ref.save()
        if self._on_prompts_saved is not None:
            self._on_prompts_saved()

    def _run_modal_guarding_hotkey(self, fn: Callable[[], _T_modal]) -> _T_modal:
        """经 VoiceTray.run_modal_with_hotkey_paused：模态框期间卸全局热键，避免 pynput 与 Qt 抢键。"""
        runner = self._run_modal_with_hotkey_paused
        if runner is None:
            return fn()
        return runner(fn)

    # ── activation ──

    def _activate_selected(self):
        row = self._list.currentRow()
        self._flush_editing_prompt()

        if self._is_default_row(row):
            new_active = ""
        else:
            pid = self._selected_prompt_id(row)
            if not pid:
                return
            new_active = pid

        if new_active == self._active_id:
            return

        if self._prompt_data_differs_from_disk():
            def _ask_activate():
                box = QMessageBox(self)
                box.setWindowTitle("未保存的修改")
                box.setText(
                    "名称、提示词内容或列表的修改尚未保存。\n"
                    "请先保存后再设为当前使用项，或使用「保存并设为当前」。")
                box.setIcon(QMessageBox.Icon.Warning)
                btn_save = box.addButton("保存并设为当前", QMessageBox.ButtonRole.AcceptRole)
                btn_cancel = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
                box.setDefaultButton(btn_save)
                box.setStyleSheet(_PROMPT_MSGBOX_STYLE)
                btn_save.setStyleSheet(_BTN_PRIMARY)
                btn_cancel.setStyleSheet(_BTN)
                box.exec()
                return box.clickedButton() is btn_save

            if not self._run_modal_guarding_hotkey(_ask_activate):
                return
            self._active_id = new_active
            self._do_save()
            self._update_activate_button(self._list.currentRow())
            if self._on_active_applied is not None:
                self._on_active_applied()
            return

        self._active_id = new_active
        if self._config_ref is not None:
            self._config_ref.active_prompt_id = self._active_id
            self._config_ref.save()
            logger.info("[PromptDlg] Active prompt applied (prompt data unchanged)")
        self._refresh_list()
        self._update_activate_button(row)
        self._update_right_unsaved_hint()
        self._update_save_button()
        if self._on_active_applied is not None:
            self._on_active_applied()

    def _on_item_double_clicked(self, item):
        row = self._list.row(item)
        if row == self._list.currentRow():
            self._activate_selected()

    # ── add / delete / restore ──

    def _add_item(self):
        self._flush_editing_prompt()
        pid = uuid.uuid4().hex[:8]
        self._prompts.append({"id": pid, "name": "新提示词", "content": ""})
        new_row = len(self._prompts)
        self._refresh_list()
        self._switching = True
        self._list.setCurrentRow(new_row)
        self._switching = False
        self._last_row = new_row
        self._load_editor(new_row)
        self._name_input.setFocus()
        self._name_input.selectAll()
        self._update_save_button()

    def _duplicate_item(self):
        self._flush_editing_prompt()
        row = self._list.currentRow()
        if self._is_default_row(row):
            return
        idx = row - 1
        if idx < 0 or idx >= len(self._prompts):
            return
        src = self._prompts[idx]
        pid = uuid.uuid4().hex[:8]
        self._prompts.append(
            {
                "id": pid,
                "name": str(src.get("name") or ""),
                "content": str(src.get("content") or ""),
            },
        )
        new_row = len(self._prompts)
        self._refresh_list()
        self._switching = True
        self._list.setCurrentRow(new_row)
        self._switching = False
        self._last_row = new_row
        self._load_editor(new_row)
        self._name_input.setFocus()
        self._name_input.selectAll()
        self._update_save_button()

    def _delete_item(self):
        """删除列表中蓝框选中的那条自定义提示词。

        以 QListWidget.currentRow() 为唯一行号来源（删除按钮 focusPolicy=NoFocus，
        点击它不会改变列表选中项）。不依赖 _last_row，避免其被其他路径污染。
        """
        row = self._list.currentRow()
        if row < 0 or self._is_default_row(row):
            return
        idx = row - 1
        if idx < 0 or idx >= len(self._prompts):
            return
        len_before = len(self._prompts)
        removed = self._prompts.pop(idx)
        if self._active_id == removed["id"]:
            self._active_id = ""
        len_after = len(self._prompts)
        if idx < len_before - 1:
            target_row = row
        else:
            target_row = row - 1
        target_row = max(0, min(target_row, len_after))
        self._last_row = -1
        self._refresh_list()
        self._switching = True
        self._list.setCurrentRow(target_row)
        self._switching = False
        self._last_row = target_row
        self._load_editor(target_row)
        self._update_save_button()

    def _revert_current_row_from_disk(self):
        """仅将当前选中自定义项的名称与内容恢复为磁盘 config 中的版本。"""
        self._flush_editing_prompt()
        row = self._last_row
        if row < 0 or self._is_default_row(row):
            return
        idx = row - 1
        if idx < 0 or idx >= len(self._prompts):
            return
        pid = self._prompts[idx]["id"]
        d = self._disk_custom_entry(pid)
        if d is None or not self._is_custom_entry_unsaved(pid):
            return
        self._prompts[idx]["name"] = str(d.get("name") or "")
        self._prompts[idx]["content"] = str(d.get("content") or "")
        self._last_row = row
        self._load_editor(row)
        self._update_save_button()

    def _revert_all_from_disk(self):
        """将提示词列表与名称/内容恢复为磁盘 config 中最后一次保存的状态。"""
        if self._config_ref is None:
            return
        self._flush_editing_prompt()
        if not self._prompt_data_differs_from_disk():
            return

        def _ask_revert_all():
            box = QMessageBox(self)
            box.setWindowTitle("全部还原")
            box.setText("将放弃本次全部更改，是否继续？")
            box.setIcon(QMessageBox.Icon.Warning)
            btn_yes = box.addButton("是", QMessageBox.ButtonRole.AcceptRole)
            btn_no = box.addButton("否", QMessageBox.ButtonRole.RejectRole)
            box.setDefaultButton(btn_no)
            box.setStyleSheet(_PROMPT_MSGBOX_STYLE)
            btn_yes.setStyleSheet(_BTN_DANGER)
            btn_no.setStyleSheet(_BTN)
            box.exec()
            return box.clickedButton() is btn_yes

        if not self._run_modal_guarding_hotkey(_ask_revert_all):
            return
        self._prompts = copy.deepcopy(self._config_ref.custom_prompts)
        self._active_id = self._config_ref.active_prompt_id or ""
        self._refresh_list()
        self._select_active_row()
        self._update_save_button()
        self._update_right_unsaved_hint()

    def _restore_factory_defaults(self):
        def _ask_restore():
            box = QMessageBox(self)
            box.setWindowTitle("恢复默认模板")
            box.setText(
                "将提示词列表恢复为默认模板，当前编辑将丢失。是否继续？")
            box.setIcon(QMessageBox.Icon.Warning)
            btn_yes = box.addButton("是", QMessageBox.ButtonRole.AcceptRole)
            btn_no = box.addButton("否", QMessageBox.ButtonRole.RejectRole)
            box.setDefaultButton(btn_no)
            box.setStyleSheet(_PROMPT_MSGBOX_STYLE)
            btn_yes.setStyleSheet(_BTN_DANGER)
            btn_no.setStyleSheet(_BTN)
            box.exec()
            return box.clickedButton() is btn_yes

        if not self._run_modal_guarding_hotkey(_ask_restore):
            return
        self._prompts = copy.deepcopy(default_prompt_templates())
        self._active_id = self._prompts[0]["id"]
        self._last_row = 1
        self._refresh_list()
        self._switching = True
        self._list.setCurrentRow(1)
        self._switching = False
        self._load_editor(1)
        self._update_save_button()

    # ── save / close ──

    def _do_save(self):
        self._flush_editing_prompt()
        self._accepted = True
        if self._config_ref is not None:
            self._config_ref.custom_prompts = copy.deepcopy(self._prompts)
            self._config_ref.active_prompt_id = self._active_id
            self._config_ref.save()
            logger.info("[PromptDlg] Saved prompts to config")
            if self._on_prompts_saved is not None:
                self._on_prompts_saved()
        self._refresh_list()
        self._update_save_button()
        self._update_right_unsaved_hint()

    def closeEvent(self, event):
        self._flush_editing_prompt()
        if self._prompt_data_differs_from_disk():
            def _ask_close():
                box = QMessageBox(self)
                box.setWindowTitle("未保存的修改")
                box.setText("当前有未保存的修改，关闭后将丢失。")
                box.setIcon(QMessageBox.Icon.Warning)
                btn_save = box.addButton("保存并关闭", QMessageBox.ButtonRole.AcceptRole)
                btn_discard = box.addButton("不保存", QMessageBox.ButtonRole.DestructiveRole)
                btn_cancel = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
                box.setDefaultButton(btn_save)
                box.setStyleSheet(_PROMPT_MSGBOX_STYLE)
                btn_save.setStyleSheet(_BTN_PRIMARY)
                btn_discard.setStyleSheet(_BTN_DANGER)
                btn_cancel.setStyleSheet(_BTN)
                box.exec()
                clicked = box.clickedButton()
                if clicked is btn_save:
                    return "save"
                if clicked is btn_discard:
                    return "discard"
                return "cancel"

            choice = self._run_modal_guarding_hotkey(_ask_close)
            if choice == "save":
                self._do_save()
                self.accept()
                event.accept()
            elif choice == "discard":
                self.reject()
                event.accept()
            else:
                event.ignore()
            return
        if self._accepted:
            self.accept()
        else:
            self.reject()
        event.accept()


MENU_STYLE = """
    QMenu {
        background: #2a2a2a;
        color: #ffffff;
        border: 1px solid #444;
        border-radius: 8px;
        padding: 6px 0;
    }
    QMenu::item {
        padding: 7px 28px 7px 16px;
        font-size: 13px;
    }
    QMenu::item:selected {
        background: #3a3a3a;
    }
    QMenu::item:disabled {
        color: #666;
    }
    QMenu::separator {
        height: 1px;
        background: #3a3a3a;
        margin: 4px 12px;
    }
"""


class _DeviceRefreshWorker(QThread):
    """Enumerate audio devices off the main thread."""
    finished = pyqtSignal(str, list)  # (default_name, devices)

    def __init__(self, release_recorder, release_audio):
        super().__init__()
        self._release_recorder = release_recorder
        self._release_audio = release_audio

    def run(self):
        from core.recorder import VoiceRecorder
        from core.device_watcher import (
            get_default_capture_device_name,
            get_full_device_names,
        )
        try:
            self._release_recorder()
            self._release_audio()
            full_names = get_full_device_names()
            default_name = (
                get_default_capture_device_name()
                or VoiceRecorder.get_default_device_name()
            )
            raw_devices = VoiceRecorder.list_devices()
            raw_by_name = {dev["name"]: dev for dev in raw_devices}
            if full_names:
                devices = [
                    {
                        "name": trunc,
                        "display_name": full,
                        "index": raw_by_name.get(trunc, {}).get("index"),
                    }
                    for trunc, full in full_names.items()
                ]
            else:
                devices = raw_devices
            if default_name:
                trunc = default_name[:31]
                if trunc in full_names:
                    default_name = full_names[trunc]
        except Exception:
            default_name = "Unknown"
            devices = []
        self.finished.emit(default_name, devices)


class VoiceTray(QSystemTrayIcon):
    def __init__(self, engine: VoiceEngine, mini: MiniRecordingWindow, config: Config):
        super().__init__()
        self._engine = engine
        self._mini = mini
        self._config = config
        self._prompt_dlg: _PolishPromptDialog | None = None
        self._apikey_dlg: _ApiSettingsDialog | None = None
        self._hotkey_pause_depth = 0

        self._audio = AudioCues()
        self._audio.set_enabled(config.play_sounds)

        self._key_warning = not config.api_key and not config.custom_asr_api_key and not config.custom_polish_api_key
        self._mic_warning = False
        self._pending_device_apply = False
        self._sync_autostart_state(save_if_changed=True)
        if self._key_warning:
            self.setIcon(icons.icon_key_invalid())
            self._update_tooltip("API Key 未配置，右键点击配置")
        else:
            self.setIcon(icons.icon_idle())
            self._update_tooltip("就绪")

        self._build_menu()

        if config.tray_click_to_record:
            self.activated.connect(self._on_activated)

        engine.state_changed.connect(self._on_state)
        engine.transcription_done.connect(self._on_done)
        engine.mic_unavailable.connect(self._on_mic_unavailable)

        mini.request_record.connect(self._on_tray_click)
        mini.request_stop.connect(self._on_tray_click)
        mini.request_cancel.connect(self._on_cancel)
        mini.request_history.connect(self._open_history)
        mini.mode_changed.connect(self._on_mini_mode_changed)
        mini.show_result_changed.connect(self._on_mini_show_result_changed)

        self._hotkey_hold_active = False
        self._hotkey = ComboHotkeyThread(config.hotkey)
        self._hotkey.triggered.connect(self._on_hotkey)
        self._hotkey.released.connect(self._on_hotkey_release)
        self._hotkey.start()

        from core.device_watcher import AudioDeviceWatcher
        self._device_watcher = AudioDeviceWatcher()
        self._device_change_timer = QTimer()
        self._device_change_timer.setSingleShot(True)
        self._device_change_timer.setInterval(500)
        self._device_change_timer.timeout.connect(self._start_async_refresh)
        self._device_watcher.signals.changed.connect(self._device_change_timer.start)
        self._device_watcher.start()

        self.show()

    def _build_menu(self):
        menu = QMenu()
        menu.setStyleSheet(MENU_STYLE)

        self._act_record = QAction("开始录音", menu)
        self._act_record.triggered.connect(self._on_tray_click)
        menu.addAction(self._act_record)

        menu.addSeparator()

        act_history = QAction("打开历史记录", menu)
        act_history.triggered.connect(self._open_history)
        menu.addAction(act_history)

        act_log = QAction("查看处理日志", menu)
        act_log.triggered.connect(self._open_log)
        menu.addAction(act_log)

        self._act_save_audio = QAction("保存录音文件", menu)
        self._act_save_audio.setCheckable(True)
        self._act_save_audio.setChecked(self._config.save_audio)
        self._act_save_audio.triggered.connect(self._toggle_save_audio)
        menu.addAction(self._act_save_audio)

        menu.addSeparator()

        self._device_menu = QMenu("输入设备", menu)
        self._device_menu.setStyleSheet(MENU_STYLE)
        self._device_menu.aboutToShow.connect(self._on_device_menu_show)
        self._cached_default_name = ""
        self._cached_devices: list[dict] = []
        self._dev_refresh_running = False
        self._dev_refresh_repeat = False
        self._dev_refresh_ready = False
        self._dev_refresh_worker: _DeviceRefreshWorker | None = None
        menu.addMenu(self._device_menu)

        self._mode_menu = QMenu("切换模式", menu)
        self._mode_menu.setStyleSheet(MENU_STYLE)
        for mode_id, mode_name in [("transcribe", "纯转录"), ("polish", "智能润色")]:
            act = QAction(mode_name, self._mode_menu)
            act.setCheckable(True)
            act.setChecked(self._config.mode == mode_id)
            act.triggered.connect(lambda checked, m=mode_id: self._set_mode(m))
            self._mode_menu.addAction(act)
        menu.addMenu(self._mode_menu)

        self._polish_model_menu = QMenu("润色模型", menu)
        self._polish_model_menu.setStyleSheet(MENU_STYLE)
        self._polish_models = [
            ("qwen3.5-flash", "Qwen3.5 Flash"),
            ("qwen-flash", "Qwen Flash"),
            ("qwen-plus", "Qwen Plus"),
            ("qwen-max", "Qwen Max"),
        ]
        for model_id, display_name in self._polish_models:
            act = QAction(display_name, self._polish_model_menu)
            act.setCheckable(True)
            act.setChecked(self._config.polish_model == model_id)
            act.triggered.connect(lambda checked, m=model_id: self._set_polish_model(m))
            self._polish_model_menu.addAction(act)
        menu.addMenu(self._polish_model_menu)

        menu.addSeparator()

        self._prompt_menu = QMenu("自定义提示词", menu)
        self._prompt_menu.setStyleSheet(MENU_STYLE)
        self._prompt_menu.aboutToShow.connect(self._sync_prompt_menu_checks)
        menu.addMenu(self._prompt_menu)

        menu.addSeparator()

        self._act_show_result_text = QAction("显示识别原文", menu)
        self._act_show_result_text.setCheckable(True)
        self._act_show_result_text.setChecked(self._config.show_result_text)
        self._act_show_result_text.triggered.connect(self._toggle_show_result_text)
        menu.addAction(self._act_show_result_text)

        self._act_hide_idle_mini = QAction("空闲时隐藏顶部磁吸栏", menu)
        self._act_hide_idle_mini.setCheckable(True)
        self._act_hide_idle_mini.setChecked(self._config.hide_mini_window_when_idle)
        self._act_hide_idle_mini.triggered.connect(self._toggle_hide_idle_mini)
        menu.addAction(self._act_hide_idle_mini)

        act_reset_pos = QAction("重置指示器位置", menu)
        act_reset_pos.triggered.connect(self._reset_mini_position)
        menu.addAction(act_reset_pos)

        menu.addSeparator()

        self._act_autostart = QAction("开机自启", menu)
        self._act_autostart.setCheckable(True)
        self._act_autostart.setChecked(self._config.autostart_enabled)
        self._act_autostart.triggered.connect(self._toggle_autostart)
        menu.addAction(self._act_autostart)

        menu.addSeparator()

        hotkey_display = _hotkey_display(self._config.hotkey)
        self._act_hotkey = QAction(f"快捷键: {hotkey_display}", menu)
        self._act_hotkey.triggered.connect(self._configure_hotkey)
        menu.addAction(self._act_hotkey)

        act_apikey = QAction("API 设置...", menu)
        act_apikey.triggered.connect(self._configure_apikey)
        menu.addAction(act_apikey)

        menu.addSeparator()

        act_quit = QAction("退出", menu)
        act_quit.triggered.connect(self._quit)
        menu.addAction(act_quit)

        menu.aboutToShow.connect(self._start_async_refresh)
        self.setContextMenu(menu)

        self._rebuild_prompt_menu()
        self._start_async_refresh()

    # ── device refresh (background thread) ──

    def _release_recorder_pa(self):
        if (self._engine.state != "recording"
                and self._engine.recorder._pa is not None):
            self._engine.recorder._release_pa()

    def _restore_all_pa(self):
        if not self._audio._stream_ready:
            self._audio._init_stream()
        if self._engine.recorder._pa is None and self._engine.state != "recording":
            self._engine.recorder.prepare()

    def _start_async_refresh(self):
        if self._dev_refresh_running:
            self._dev_refresh_repeat = True
            return
        self._dev_refresh_running = True
        worker = _DeviceRefreshWorker(
            self._release_recorder_pa, self._audio.release,
        )
        worker.finished.connect(self._on_refresh_result)
        worker.finished.connect(worker.deleteLater)
        self._dev_refresh_worker = worker
        worker.start()

    def _on_refresh_result(self, default_name: str, devices: list):
        self._cached_default_name = default_name
        self._cached_devices = devices
        self._dev_refresh_ready = True
        logger.info(f"[Tray] Device refresh done: "
                    f"default='{default_name}', {len(devices)} device(s)")
        self._restore_all_pa()
        self._sync_system_default_device()
        self._auto_fallback_if_device_gone()
        self._rebuild_device_menu()
        self._sync_tray_icon_with_engine()
        self._dev_refresh_running = False
        if self._dev_refresh_repeat:
            self._dev_refresh_repeat = False
            self._start_async_refresh()

    def _on_device_menu_show(self):
        if not self._dev_refresh_ready:
            self._device_menu.clear()
            act = QAction("正在刷新…", self._device_menu)
            act.setEnabled(False)
            self._device_menu.addAction(act)
            return
        self._rebuild_device_menu()

    def _sync_system_default_device(self):
        """If following system default, rebind recorder when default device changed."""
        if self._config.mic_name:
            return
        current_default = self._cached_default_name or ""
        recorder_name = self._engine.recorder.device_name or ""
        if not current_default or recorder_name == current_default:
            return
        if self._engine.state != "recording":
            self._engine.recorder.set_device(None, "")
            logger.info(f"[Tray] System default device changed: '{recorder_name}' -> '{current_default}'")
        else:
            self._pending_device_apply = True
            logger.info(f"[Tray] System default changed during recording, will apply: '{current_default}'")

    def _auto_fallback_if_device_gone(self):
        """If the selected device is no longer present, switch to system default.

        Device indices are unstable across PyAudio re-init, so we match by
        name.  If the same-named device still exists but its index changed,
        we silently update the stored index.
        """
        if not self._config.mic_name:
            return
        saved_name = self._config.mic_name
        match = next((d for d in self._cached_devices if d["name"] == saved_name), None)
        if match is not None:
            if match["index"] is None and self._config.mic_index is not None:
                self._config.mic_index = None
                self._config.save()
                if self._engine.state != "recording":
                    self._engine.recorder.set_device(None, saved_name)
                else:
                    self._pending_device_apply = True
                logger.info(f"[Tray] Device '{saved_name}' temporarily unresolved, cleared stale index")
                return
            if match["index"] is not None and match["index"] != self._config.mic_index:
                old_idx = self._config.mic_index
                self._config.mic_index = match["index"]
                self._config.save()
                if self._engine.state != "recording":
                    self._engine.recorder.set_device(match["index"], saved_name)
                else:
                    self._pending_device_apply = True
                logger.info(f"[Tray] Device '{saved_name}' index changed "
                            f"{old_idx} → {match['index']}")
            return
        old = self._config.mic_index
        self._config.mic_index = None
        self._config.mic_name = ""
        self._config.save()
        if self._engine.state != "recording":
            self._engine.recorder.set_device(None, "")
        else:
            self._pending_device_apply = True
        logger.info(f"[Tray] Selected device '{saved_name}' (index={old}) gone, "
                    f"switched to system default")

    def _rebuild_device_menu(self):
        self._device_menu.clear()

        label = f"系统默认 ({self._cached_default_name})" if self._cached_default_name else "系统默认"
        act_default = QAction(label, self._device_menu)
        act_default.setCheckable(True)
        act_default.setChecked(not self._config.mic_name)
        act_default.triggered.connect(lambda checked: self._set_default_device())
        self._device_menu.addAction(act_default)
        self._device_menu.addSeparator()

        if not self._cached_devices:
            act = QAction("(未发现兼容设备)", self._device_menu)
            act.setEnabled(False)
            self._device_menu.addAction(act)
            return

        for dev in self._cached_devices:
            display = dev.get("display_name", dev["name"])
            act = QAction(display, self._device_menu)
            act.setCheckable(True)
            act.setChecked(self._config.mic_name == dev["name"])
            act.triggered.connect(
                lambda checked, idx=dev.get("index"), name=dev["name"]: self._set_device(name, idx))
            self._device_menu.addAction(act)

    def _set_mode(self, mode_id: str):
        self._config.mode = mode_id
        self._config.save()
        self._sync_mode_menu()
        self._mini.sync_mode()
        logger.info(f"[Tray] Mode → {mode_id}")

    def _on_mini_mode_changed(self, mode_id: str):
        self._sync_mode_menu()

    def _on_mini_show_result_changed(self, on: bool):
        self._act_show_result_text.blockSignals(True)
        self._act_show_result_text.setChecked(on)
        self._act_show_result_text.blockSignals(False)

    def _sync_mode_menu(self):
        for act in self._mode_menu.actions():
            mode_name_map = {"纯转录": "transcribe", "智能润色": "polish"}
            act.setChecked(mode_name_map.get(act.text(), "") == self._config.mode)

    def _rebuild_prompt_menu(self):
        self._prompt_menu.clear()

        act_config = QAction("配置提示词", self._prompt_menu)
        act_config.triggered.connect(self._configure_polish_extra)
        self._prompt_menu.addAction(act_config)
        self._prompt_menu.addSeparator()

        act_none = QAction("默认提示词", self._prompt_menu)
        act_none.setCheckable(True)
        act_none.setData(_TRAY_MENU_DEFAULT_PROMPT)
        act_none.setChecked(not self._config.active_prompt_id)
        act_none.triggered.connect(lambda: self._set_active_prompt(""))
        self._prompt_menu.addAction(act_none)

        for p in self._config.custom_prompts:
            pid, name = p["id"], p.get("name", "未命名")
            act = QAction(name, self._prompt_menu)
            act.setCheckable(True)
            act.setData(pid)
            act.setChecked(self._config.active_prompt_id == pid)
            act.triggered.connect(lambda checked, _id=pid: self._set_active_prompt(_id))
            self._prompt_menu.addAction(act)

    def _sync_prompt_menu_checks(self):
        """仅更新勾选，不在弹出时 clear 子菜单，避免 Windows 上首次展开几何错位。"""
        cur = self._config.active_prompt_id or ""
        for act in self._prompt_menu.actions():
            if act.isSeparator() or not act.isCheckable():
                continue
            d = act.data()
            if d == _TRAY_MENU_DEFAULT_PROMPT:
                act.setChecked(not cur)
            elif isinstance(d, str) and d:
                act.setChecked(cur == d)

    def _set_active_prompt(self, prompt_id: str):
        self._config.active_prompt_id = prompt_id
        self._config.save()
        name = ""
        for p in self._config.custom_prompts:
            if p["id"] == prompt_id:
                name = p.get("name", "")
                break
        logger.info(f"[Tray] Active prompt → {name or '(none)'}")
        self._sync_prompt_menu_checks()
        dlg = self._prompt_dlg
        if dlg is not None:
            dlg.sync_from_config()

    def _configure_polish_extra(self):
        if self._prompt_dlg is not None:
            self._prompt_dlg.raise_()
            self._prompt_dlg.activateWindow()
            return
        dlg = _PolishPromptDialog(
            self._config.custom_prompts,
            self._config.active_prompt_id,
            default_text=DEFAULT_INSTRUCTIONS,
            config=self._config,
            on_active_applied=self._sync_prompt_menu_checks,
            on_prompts_saved=self._rebuild_prompt_menu,
            run_modal_with_hotkey_paused=self.run_modal_with_hotkey_paused,
        )
        dlg.setWindowModality(Qt.WindowModality.NonModal)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dlg.finished.connect(self._on_prompt_dlg_finished)
        self._prompt_dlg = dlg
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _on_prompt_dlg_finished(self, _result: int):
        # Prompt list is persisted from _PolishPromptDialog._do_save; active_prompt_id
        # may also be updated immediately via「设为当前」.
        # Do not copy dlg state here: dlg.accepted stayed True after a prior save and would
        # re-apply unsaved edits when the user chose「不保存」on close.
        self._prompt_dlg = None
        self._rebuild_prompt_menu()

    def _menu_parent(self):
        """Parent for modal dialogs so they stay above the tray context."""
        try:
            return QApplication.activeWindow() or QApplication.focusWidget()
        except Exception:
            return None

    def _stop_hotkey_listener(self) -> None:
        """停止全局 ComboHotkeyThread 并等待 pynput 钩子退出。"""
        self._hotkey.stop_hotkey()
        self._hotkey.wait(2000)

    def _spawn_hotkey_thread(self, combo: str | None = None) -> None:
        """新建并启动 ComboHotkeyThread（替换 self._hotkey）。调用前应已 stop_hotkey_listener。"""
        key = self._config.hotkey if combo is None else combo
        self._hotkey = ComboHotkeyThread(key)
        self._hotkey.triggered.connect(self._on_hotkey)
        self._hotkey.released.connect(self._on_hotkey_release)
        self._hotkey.start()

    @contextmanager
    def hotkey_paused(self):
        """嵌套安全：模态 UI 期间卸全局热键，退出最后一层时按当前 config.hotkey 恢复。"""
        self._hotkey_pause_depth += 1
        if self._hotkey_pause_depth == 1:
            self._stop_hotkey_listener()
        try:
            yield
        finally:
            self._hotkey_pause_depth -= 1
            if self._hotkey_pause_depth < 0:
                logger.warning("[Tray] hotkey pause depth underflow")
                self._hotkey_pause_depth = 0
            if self._hotkey_pause_depth != 0:
                return
            if QCoreApplication.closingDown():
                return
            self._spawn_hotkey_thread()

    def run_modal_with_hotkey_paused(self, fn: Callable[[], _T_modal]) -> _T_modal:
        """供提示词等对话框在 QMessageBox.exec 等模态调用外包裹，与 hotkey_paused 同一套 refcount。"""
        with self.hotkey_paused():
            return fn()

    def _set_polish_model(self, model_id: str):
        self._config.polish_model = model_id
        self._config.save()
        self._engine.polisher.set_model(model_id)
        for act in self._polish_model_menu.actions():
            mid = next((m for m, d in self._polish_models if d == act.text()), "")
            act.setChecked(mid == model_id)
        logger.info(f"[Tray] Polish model → {model_id}")

    def _refresh_polish_model_menu(self):
        """Update polish model menu check state after provider change."""
        is_custom = self._config.polish_provider == "openai_compat"
        for act in self._polish_model_menu.actions():
            mid = next((m for m, d in self._polish_models if d == act.text()), "")
            if is_custom:
                act.setChecked(False)
            else:
                act.setChecked(mid == self._config.polish_model)

    def _configure_hotkey(self):
        self._stop_hotkey_listener()
        dlg = _HotkeyDialog(self._config.hotkey)
        accepted = (
            dlg.exec() == QDialog.DialogCode.Accepted and bool(dlg.hotkey))
        if accepted:
            combo = dlg.hotkey
            self._config.hotkey = combo
            self._config.save()
            display = _hotkey_display(combo)
            self._act_hotkey.setText(f"快捷键: {display}")
            logger.info(f"[Tray] Hotkey → {display}")
        else:
            combo = self._config.hotkey
        self._spawn_hotkey_thread(combo)

    def _configure_apikey(self):
        if self._apikey_dlg is not None:
            self._apikey_dlg.raise_()
            self._apikey_dlg.activateWindow()
            return
        dlg = _ApiSettingsDialog(self._config)
        dlg.setWindowModality(Qt.WindowModality.NonModal)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dlg.finished.connect(self._on_apikey_dlg_finished)
        self._apikey_dlg = dlg
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _on_apikey_dlg_finished(self, result: int):
        dlg = self._apikey_dlg
        self._apikey_dlg = None
        if dlg is None or result != QDialog.DialogCode.Accepted or not dlg._result_accepted:
            return
        self._config.api_key = dlg.api_key
        self._config.custom_asr_api_key = dlg.custom_asr_api_key
        self._config.custom_asr_base_url = dlg.custom_asr_base_url
        self._config.custom_asr_model = dlg.custom_asr_model
        self._config.custom_polish_api_key = dlg.custom_polish_api_key
        self._config.custom_polish_base_url = dlg.custom_polish_base_url
        self._config.custom_polish_model = dlg.custom_polish_model
        self._config.asr_provider = dlg.asr_provider
        self._config.polish_provider = dlg.polish_provider
        self._config.save()
        self._engine.reload_backends()
        self._refresh_polish_model_menu()
        logger.info(f"[Tray] API settings updated "
                    f"(asr={dlg.asr_provider}, polish={dlg.polish_provider})")
        has_key = bool(dlg.api_key) or bool(dlg.custom_asr_api_key) or bool(dlg.custom_polish_api_key)
        if has_key:
            self.set_key_warning(False)

    def _toggle_save_audio(self, checked: bool):
        self._config.save_audio = checked
        self._config.save()
        logger.info(f"[Tray] Save audio → {'on' if checked else 'off'}")

    def _toggle_hide_idle_mini(self, checked: bool):
        self._config.hide_mini_window_when_idle = checked
        self._config.save()
        self._mini.refresh_visibility()
        logger.info(f"[Tray] Hide idle mini window → {'on' if checked else 'off'}")

    def _toggle_show_result_text(self, checked: bool):
        self._config.show_result_text = checked
        self._config.save()
        self._mini.sync_show_result()
        logger.info(f"[Tray] Show result text → {'on' if checked else 'off'}")

    def _sync_autostart_state(self, save_if_changed: bool = False):
        actual = self._read_autostart_enabled()
        changed = self._config.autostart_enabled != actual
        self._config.autostart_enabled = actual
        if save_if_changed and changed:
            self._config.save()
        if hasattr(self, "_act_autostart"):
            self._act_autostart.blockSignals(True)
            self._act_autostart.setChecked(actual)
            self._act_autostart.blockSignals(False)

    def _read_autostart_enabled(self) -> bool:
        if sys.platform != "win32":
            return False
        try:
            import winreg
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                _AUTOSTART_RUN_KEY,
                0,
                winreg.KEY_QUERY_VALUE,
            ) as key:
                value, _ = winreg.QueryValueEx(key, _AUTOSTART_VALUE_NAME)
            return bool(str(value).strip())
        except FileNotFoundError:
            return False
        except OSError:
            return False

    def _resolve_autostart_command(self) -> str | None:
        src_dir = os.path.dirname(os.path.abspath(__file__))
        app_root = os.path.dirname(src_dir)
        if os.path.basename(app_root).lower() == "src":
            app_root = os.path.dirname(app_root)
        exe = os.path.join(app_root, "VoiceInput.exe")
        if not os.path.isfile(exe):
            return None
        return f'"{exe}"'

    def _write_autostart_enabled(self, enabled: bool):
        if sys.platform != "win32":
            raise RuntimeError("当前平台不支持开机自启")

        import winreg

        if enabled:
            command = self._resolve_autostart_command()
            if not command:
                raise RuntimeError("当前运行方式无法确定启动命令，请使用安装包版本后再设置")
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _AUTOSTART_RUN_KEY) as key:
                winreg.SetValueEx(key, _AUTOSTART_VALUE_NAME, 0, winreg.REG_SZ, command)
            return

        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                _AUTOSTART_RUN_KEY,
                0,
                winreg.KEY_SET_VALUE,
            ) as key:
                winreg.DeleteValue(key, _AUTOSTART_VALUE_NAME)
        except FileNotFoundError:
            return

    def _toggle_autostart(self, checked: bool):
        try:
            self._write_autostart_enabled(checked)
        except Exception as e:
            logger.warning(f"[Tray] Autostart toggle failed: {e}")
            self.showMessage(
                "VoiceInput",
                f"设置开机自启失败：{e}",
                QSystemTrayIcon.MessageIcon.Warning,
                4000,
            )
            self._sync_autostart_state()
            return

        self._config.autostart_enabled = checked
        self._config.save()
        logger.info(f"[Tray] Autostart → {'on' if checked else 'off'}")

    def _set_default_device(self):
        self._config.mic_index = None
        self._config.mic_name = ""
        self._config.save()
        if self._engine.state == "recording":
            self._pending_device_apply = True
            logger.info("[Tray] Input device saved for next session → system default")
            self.showMessage("VoiceInput", "输入设备已切换，下次录音生效",
                             QSystemTrayIcon.MessageIcon.Information, 2000)
        else:
            self._pending_device_apply = False
            self._engine.recorder.set_device(None, "")
            logger.info("[Tray] Input device → system default (index=None)")
        self._rebuild_device_menu()
        self._mic_warning = False
        self._sync_tray_icon_with_engine()

    def _set_device(self, name: str, idx: int | None = None):
        resolved = idx if idx is not None else None
        self._config.mic_name = name
        self._config.mic_index = resolved
        self._config.save()
        if self._engine.state == "recording":
            self._pending_device_apply = True
            logger.info(f"[Tray] Input device saved for next session "
                        f"→ {name} (index={resolved})")
            self.showMessage("VoiceInput", "输入设备已切换，下次录音生效",
                             QSystemTrayIcon.MessageIcon.Information, 2000)
        else:
            self._pending_device_apply = False
            self._engine.recorder.set_device(resolved, name)
            logger.info(f"[Tray] Input device → {name} (index={resolved})")
        self._rebuild_device_menu()
        self._mic_warning = False
        self._sync_tray_icon_with_engine()

    def _maybe_apply_deferred_input_device(self):
        if not self._pending_device_apply or self._engine.state == "recording":
            return
        self._pending_device_apply = False
        idx = self._config.mic_index
        name = self._config.mic_name
        self._engine.recorder.set_device(idx, name)
        logger.info(f"[Tray] Deferred input device applied (name='{name or 'system default'}', index={idx})")

    # ── tray interaction ──

    def _on_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._on_tray_click()

    def _on_tray_click(self):
        """Tray icon click: simple toggle, no hold-to-cancel."""
        if self._engine.state == "processing":
            return
        if self._engine.state == "ready":
            if self._key_warning:
                self.show_api_key_invalid_notice()
                return
            if not self._config.api_key and not self._config.custom_asr_api_key and not self._config.custom_polish_api_key:
                self._configure_apikey()
                return
            self._audio.play_start()
        elif self._engine.state == "recording":
            self._audio.play_stop()
        self._engine.toggle_record()

    def _on_cancel(self):
        self._hotkey_hold_active = False
        if self._engine.state == "recording":
            self._audio.play_stop()
            self._engine.cancel()

    def _on_hotkey(self):
        if self._engine.state == "processing":
            return
        if self._engine.state == "ready":
            if self._key_warning:
                self.show_api_key_invalid_notice()
                return
            if not self._config.api_key and not self._config.custom_asr_api_key and not self._config.custom_polish_api_key:
                self._configure_apikey()
                return
            self._audio.play_start()
            self._engine.toggle_record()
        elif self._engine.state == "recording":
            self._hotkey_hold_active = True
            self._mini.start_hotkey_hold()

    def _on_hotkey_release(self, hold_ms: int):
        if not self._hotkey_hold_active:
            return
        self._hotkey_hold_active = False
        if self._engine.state != "recording":
            self._mini.stop_hotkey_hold()
            return
        self._mini.stop_hotkey_hold()
        if hold_ms < self._mini.hotkey_click_threshold_ms():
            self._audio.play_stop()
            self._engine.toggle_record()

    # ── engine state ──

    def _update_tooltip(self, status: str):
        self.setToolTip(f"VoiceInput — {status}")

    def _sync_tray_icon_with_engine(self):
        """Align icon and primary tooltip with engine state (e.g. after menu device refresh)."""
        st = self._engine.state
        if st == "recording":
            self.setIcon(icons.icon_recording())
            self._update_tooltip("录音中")
        elif st == "processing":
            self.setIcon(icons.icon_processing())
            self._update_tooltip("识别中")
        else:
            self._restore_idle_icon()

    def _on_state(self, state: str):
        if state != "recording":
            self._maybe_apply_deferred_input_device()
        if state == "recording":
            self._mic_warning = False
        self._sync_tray_icon_with_engine()
        if state == "recording":
            self._act_record.setText("停止录音")
        elif state == "processing":
            self._act_record.setText("处理中...")
            self._act_record.setEnabled(False)
        elif state == "ready":
            self._act_record.setText("开始录音")
            self._act_record.setEnabled(True)

    def _on_done(self, text: str):
        self._audio.play_done()
        self.setIcon(icons.icon_done())
        self._update_tooltip("就绪")
        QTimer.singleShot(2000, self._restore_idle_icon)

    def show_tray_message(
        self,
        title: str,
        body: str,
        icon: QSystemTrayIcon.MessageIcon,
        milliseconds: int,
    ):
        self.showMessage(title, body, icon, milliseconds)

    def show_api_key_invalid_notice(self):
        self.showMessage(
            "VoiceInput",
            "API Key 不可用，请右键点击托盘图标重新配置",
            QSystemTrayIcon.MessageIcon.Critical,
            5000,
        )

    def set_key_warning(self, warning: bool):
        self._key_warning = warning
        self._restore_idle_icon()

    def _on_mic_unavailable(self, msg: str):
        if self._engine.recorder.no_device:
            self._mic_warning = False
        else:
            self._mic_warning = True
        text = (msg or "").strip() or "未找到输入设备"
        logger.warning(f"[Tray] Mic unavailable: {text}")
        self.showMessage(
            "VoiceInput",
            text,
            QSystemTrayIcon.MessageIcon.Warning,
            5000,
        )
        self._restore_idle_icon()
        self._start_async_refresh()

    def _restore_idle_icon(self):
        if self._engine.state in ("recording", "processing"):
            self._sync_tray_icon_with_engine()
            return
        if self._key_warning:
            self.setIcon(icons.icon_key_invalid())
            self._update_tooltip("API Key 无效，右键点击配置")
        elif self._engine.recorder.no_device:
            self.setIcon(icons.icon_key_invalid())
            self._update_tooltip("未找到输入设备")
        elif self._mic_warning:
            self.setIcon(icons.icon_key_invalid())
            self._update_tooltip("麦克风不可用，右键切换输入设备")
        else:
            self.setIcon(icons.icon_idle())
            self._update_tooltip("就绪")

    def _reset_mini_position(self):
        self._mini.reset_position()

    def _open_history(self):
        path = Config.history_dir()
        os.startfile(str(path))

    def _open_log(self):
        from core.log import _LOG_DIR
        os.startfile(str(_LOG_DIR))

    def _quit(self):
        logger.info("[Tray] Quit requested")
        if self._engine.state == "recording":
            self._engine.cancel()
        self._device_watcher.stop()
        self._engine.recorder.release()
        self._stop_hotkey_listener()
        QApplication.quit()
