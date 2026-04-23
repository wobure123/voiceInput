"""
Subscribe to ``VoiceEngine.error_occurred`` and show messages via tray balloon only.

Classification: :mod:`core.user_errors`; presentation: :class:`ui.tray.VoiceTray`.
"""
from __future__ import annotations

import time

from PyQt6.QtCore import QObject
from PyQt6.QtWidgets import QSystemTrayIcon

from core.engine import VoiceEngine
from core.log import logger
from core.user_errors import UserErrorDomain, classify_user_error, single_line_preview
from ui.tray import VoiceTray

_TAG = "[Notify]"
_API_TOAST_COOLDOWN_SEC = 12.0


class UserNotificationHub(QObject):
    def __init__(
        self,
        engine: VoiceEngine,
        tray: VoiceTray,
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self._tray = tray
        self._last_api_tray_at = 0.0
        engine.error_occurred.connect(self._on_engine_error)

    def _on_engine_error(self, message: str):
        if not (message or "").strip():
            return
        ctx = classify_user_error(message)
        logger.debug(f"{_TAG} error domain={ctx.domain.name} msg={ctx.message[:80]!r}")

        if ctx.domain == UserErrorDomain.API_CREDENTIALS:
            self._tray.set_key_warning(True)
            now = time.monotonic()
            if now - self._last_api_tray_at >= _API_TOAST_COOLDOWN_SEC:
                self._last_api_tray_at = now
                self._tray.show_api_key_invalid_notice()
            return

        _SILENT_DOMAINS = (
            UserErrorDomain.SPEECH_EMPTY,
            UserErrorDomain.SPEECH_SILENT,
        )
        if ctx.domain in _SILENT_DOMAINS:
            logger.info(f"{_TAG} Suppressed notification (domain={ctx.domain.name})")
            return

        body = f"处理失败：{single_line_preview(ctx.message)}"
        self._tray.show_tray_message(
            "VoiceInput",
            body,
            QSystemTrayIcon.MessageIcon.Warning,
            8000,
        )
