"""Compact floating recording indicator — with smooth animations.

Behavior:
  - Idle:      tiny pill stuck to top-center of screen
  - Hover:     expands to show three buttons: record, polish toggle, show-result toggle
  - Recording: capsule with waveform (stop button on hover)
  - Done:      if show-result is on, popup shows final text below
"""
from PyQt6.QtCore import (
    Qt, QTimer, QPropertyAnimation, QEasingCurve, QRect, QRectF, pyqtSignal,
)
from PyQt6.QtGui import QPainter, QColor, QPainterPath, QPen, QFont
from PyQt6.QtWidgets import (
    QWidget, QHBoxLayout, QVBoxLayout, QLabel, QPushButton,
    QApplication,
)

from core.log import logger
from ui.theme import Theme
from ui.waveform_widget import WaveformWidget

IDLE_W, IDLE_H = 48, 8
HOVER_W, HOVER_H = 120, 36       # 3 buttons
REC_W, REC_H = 80, 38             # waveform only (with padding)
REC_HOVER_W = 110                  # waveform + stop button on hover
RESULT_W = 340                    # result popup width
RADIUS = 19

_BTN_STYLE = """
    QPushButton {{
        background: {bg}; color: {fg};
        border: none; border-radius: {r}px;
        font-size: 13px; outline: none;
        padding: 0px; text-align: center;
    }}
    QPushButton:hover {{ background: {hover}; }}
"""


class _RecStopButton(QWidget):
    """Recording stop button. Click = stop recording. Long-press = cancel (discard)."""
    clicked = pyqtSignal()
    cancelled = pyqtSignal()

    CLICK_THRESHOLD_MS = 300
    HOLD_MS = 500
    _TICK = 25
    SIZE = 26

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setToolTip("点击停止 | 长按作废")
        self._progress = 0.0
        self._holding = False
        self._completed = False
        self._external_hold = False
        self._elapsed_ms = 0
        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(self._TICK)
        self._tick_timer.timeout.connect(self._tick)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._holding = True
            self._completed = False
            self._elapsed_ms = 0
            self._progress = 0.0
            self._tick_timer.start()
            self.update()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and not self._external_hold:
            self._tick_timer.stop()
            was_holding = self._holding
            in_click_zone = self._elapsed_ms < self.CLICK_THRESHOLD_MS
            self._holding = False
            self._progress = 0.0
            self._elapsed_ms = 0
            self.update()
            if not self._completed and was_holding and in_click_zone:
                self.clicked.emit()

    def _tick(self):
        self._elapsed_ms += self._TICK
        if self._elapsed_ms <= self.CLICK_THRESHOLD_MS:
            return
        hold_elapsed = self._elapsed_ms - self.CLICK_THRESHOLD_MS
        self._progress = min(hold_elapsed / self.HOLD_MS, 1.0)
        if self._progress >= 1.0:
            self._tick_timer.stop()
            self._progress = 0.0
            self._holding = False
            self._completed = True
            self._external_hold = False
            self._elapsed_ms = 0
            self.update()
            self.cancelled.emit()
            return
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        s = self.SIZE
        r = s / 2 - 1

        bg_path = QPainterPath()
        bg_path.addRoundedRect(QRectF(0, 0, s, s), s / 2, s / 2)
        p.fillPath(bg_path, QColor(Theme.BG_BUTTON_HOVER if self._holding else Theme.BG_BUTTON))

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#ff3b30"))
        stop_s = 9
        off = (s - stop_s) / 2
        p.drawRoundedRect(QRectF(off, off, stop_s, stop_s), 2, 2)

        if self._progress > 0:
            pen = QPen(QColor("#ff3b30"), 2.5)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            margin = 1.5
            arc_rect = QRectF(margin, margin, s - margin * 2, s - margin * 2)
            start_angle = 90 * 16
            span_angle = int(-self._progress * 360 * 16)
            p.drawArc(arc_rect, start_angle, span_angle)

        p.end()

    def enterEvent(self, event):
        self.update()

    def leaveEvent(self, event):
        if self._holding and not self._external_hold:
            self._holding = False
            self._tick_timer.stop()
            self._progress = 0.0
        self.update()

    def start_external_hold(self, skip_click_threshold: bool = False):
        """Begin the long-press progress animation from an external trigger."""
        self._external_hold = True
        self._holding = True
        self._completed = False
        if skip_click_threshold:
            self._elapsed_ms = self.CLICK_THRESHOLD_MS + self._TICK
            self._progress = min(self._TICK / self.HOLD_MS, 1.0)
        else:
            self._elapsed_ms = 0
            self._progress = 0.0
        self._tick_timer.start()
        self.update()

    def cancel_external_hold(self):
        """Cancel an in-progress external hold (hotkey released early)."""
        self._external_hold = False
        self._tick_timer.stop()
        self._holding = False
        self._progress = 0.0
        self._elapsed_ms = 0
        self.update()


class _ResultPopup(QWidget):
    """Floating text popup that shows below the mini window and auto-hides."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._label = QLabel("")
        self._label.setFont(Theme.font(13))
        self._label.setStyleSheet(f"""
            color: {Theme.TEXT_PRIMARY.name()};
            background: {Theme.BG_PRIMARY.name()};
            border: 1px solid rgba(255,255,255,30);
            border-radius: 10px;
            padding: 12px 14px;
        """)
        self._label.setWordWrap(True)
        self._label.setAlignment(
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft
        )
        self._label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self._label)

        self._auto_hide = QTimer(self)
        self._auto_hide.setSingleShot(True)
        self._auto_hide.timeout.connect(self.hide)

    def show_text(self, text: str, anchor_widget: QWidget, duration_ms: int = 3500):
        self._label.setText(text)
        self.setFixedWidth(RESULT_W)
        self.adjustSize()

        pos = anchor_widget.mapToGlobal(anchor_widget.rect().bottomLeft())
        screen = QApplication.primaryScreen()
        if screen:
            geo = screen.availableGeometry()
            x = geo.x() + (geo.width() - RESULT_W) // 2
        else:
            x = pos.x()
        self.move(x, pos.y() + 4)
        self.show()
        self._auto_hide.start(duration_ms)

    def enterEvent(self, event):
        self._auto_hide.stop()

    def leaveEvent(self, event):
        self._auto_hide.start(1500)

    def paintEvent(self, event):
        pass


class _StatusPopup(QWidget):
    """Floating status bar shown below the mini window during recording hover."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._label = QLabel()
        self._label.setFont(Theme.font(11))
        self._label.setStyleSheet(f"""
            color: {Theme.TEXT_SECONDARY.name()};
            background: {Theme.BG_PRIMARY.name()};
            border: 1px solid rgba(255,255,255,20);
            border-radius: 8px;
            padding: 5px 10px;
        """)
        layout.addWidget(self._label)

    def show_status(self, items: list[str], anchor: QWidget):
        if not items:
            self.hide()
            return
        self._label.setText("    ".join(items))
        self.adjustSize()
        pos = anchor.mapToGlobal(anchor.rect().bottomLeft())
        screen = QApplication.primaryScreen()
        if screen:
            geo = screen.availableGeometry()
            x = pos.x() + (anchor.width() - self.width()) // 2
            x = max(geo.x(), min(x, geo.x() + geo.width() - self.width()))
        else:
            x = pos.x()
        self.move(x, pos.y() + 3)
        self.show()

    def paintEvent(self, event):
        pass


class MiniRecordingWindow(QWidget):
    request_record = pyqtSignal()
    request_stop = pyqtSignal()
    request_cancel = pyqtSignal()
    request_history = pyqtSignal()
    mode_changed = pyqtSignal(str)
    show_result_changed = pyqtSignal(bool)

    def __init__(self, engine):
        super().__init__()
        self._engine = engine
        self._mode = "idle"
        self._drag_pos = None
        self._hovered = False
        self._show_result = engine.config.show_result_text
        self._anchor_x: int | None = engine.config.mini_window_x

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        self._geom_anim = QPropertyAnimation(self, b"geometry")
        self._geom_anim.finished.connect(self._on_anim_finished)
        self._target_size = (IDLE_W, IDLE_H)

        self._result_popup = _ResultPopup()
        self._status_popup = _StatusPopup()

        self._build_ui()
        self._set_widgets_for_mode("idle")
        self._position_at(IDLE_W, IDLE_H)

        self._hover_timer = QTimer(self)
        self._hover_timer.setSingleShot(True)
        self._hover_timer.timeout.connect(self._on_hover_timeout)
        self._hotkey_hold_timer = QTimer(self)
        self._hotkey_hold_timer.setSingleShot(True)
        self._hotkey_hold_timer.timeout.connect(self._begin_hotkey_hold_visuals)
        self._deferred_shrink_timer = QTimer(self)
        self._deferred_shrink_timer.setSingleShot(True)
        self._deferred_shrink_timer.timeout.connect(self._shrink_to_idle)

        engine.state_changed.connect(self._on_engine_state)
        engine.audio_data.connect(self._on_audio)
        engine.transcription_done.connect(self._on_done)

    # ── UI build ──

    def _build_ui(self):
        self._root = QVBoxLayout(self)
        self._root.setContentsMargins(0, 0, 0, 0)
        self._root.setSpacing(0)

        self._top_bar = QWidget()
        self._top_layout = QHBoxLayout(self._top_bar)
        self._top_layout.setContentsMargins(6, 5, 6, 5)
        self._top_layout.setSpacing(6)

        self._top_layout.addStretch(1)

        self._waveform = WaveformWidget(compact=True)
        self._waveform.setFixedSize(56, 26)
        self._top_layout.addWidget(self._waveform)

        self._btn_action = QPushButton("●")
        self._btn_action.setFixedSize(26, 26)
        self._btn_action.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._btn_action.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_action.clicked.connect(self._on_action_click)
        self._top_layout.addWidget(self._btn_action)
        self._style_action_record()

        self._btn_polish = QPushButton("✦")
        self._btn_polish.setFixedSize(26, 26)
        self._btn_polish.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._btn_polish.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_polish.clicked.connect(self._toggle_polish)
        self._top_layout.addWidget(self._btn_polish)
        self._update_polish_style()

        self._btn_show_result = QPushButton("◳")
        self._btn_show_result.setFixedSize(26, 26)
        self._btn_show_result.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._btn_show_result.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_show_result.clicked.connect(self._toggle_show_result)
        self._top_layout.addWidget(self._btn_show_result)
        self._update_show_result_style()

        self._btn_rec_stop = _RecStopButton()
        self._btn_rec_stop.clicked.connect(lambda: self.request_stop.emit())
        self._btn_rec_stop.cancelled.connect(self._on_cancel)
        self._top_layout.addWidget(self._btn_rec_stop)
        self._btn_rec_stop.setVisible(False)

        self._top_layout.addStretch(1)

        self._root.addWidget(self._top_bar)

        # Status dot — overlaid at top-right, not in layout
        self._dot_status = QLabel("●", self)
        self._dot_status.setFixedSize(10, 10)
        self._dot_status.setFont(Theme.font(7))
        self._dot_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._dot_status.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    # ── button styles ──

    def _update_polish_style(self):
        is_on = self._engine.config.mode == "polish"
        if is_on:
            self._btn_polish.setStyleSheet(_BTN_STYLE.format(
                bg=Theme.BG_BUTTON.name(), fg="#ffffff",
                r=13, hover=Theme.BG_BUTTON_HOVER.name(),
            ))
            self._btn_polish.setToolTip("润色: 开")
        else:
            self._btn_polish.setStyleSheet(_BTN_STYLE.format(
                bg=Theme.BG_BUTTON.name(), fg=Theme.TEXT_SECONDARY.name(),
                r=13, hover=Theme.BG_BUTTON_HOVER.name(),
            ))
            self._btn_polish.setToolTip("润色: 关")

    def _toggle_polish(self):
        cfg = self._engine.config
        cfg.mode = "transcribe" if cfg.mode == "polish" else "polish"
        cfg.save()
        self._update_polish_style()
        self.mode_changed.emit(cfg.mode)
        logger.info(f"[MiniWin] Mode toggled → {cfg.mode}")

    def sync_mode(self):
        """Refresh polish button style after external mode change."""
        self._update_polish_style()

    def sync_show_result(self):
        """Refresh ◳ button after tray menu toggles show_result_text."""
        self._show_result = self._engine.config.show_result_text
        self._update_show_result_style()

    def _update_show_result_style(self):
        if self._show_result:
            self._btn_show_result.setStyleSheet(_BTN_STYLE.format(
                bg=Theme.BG_BUTTON.name(), fg="#ffffff",
                r=13, hover=Theme.BG_BUTTON_HOVER.name(),
            ))
            self._btn_show_result.setToolTip("显示原文: 开")
        else:
            self._btn_show_result.setStyleSheet(_BTN_STYLE.format(
                bg=Theme.BG_BUTTON.name(), fg=Theme.TEXT_SECONDARY.name(),
                r=13, hover=Theme.BG_BUTTON_HOVER.name(),
            ))
            self._btn_show_result.setToolTip("显示原文: 关")

    def _toggle_show_result(self):
        self._show_result = not self._show_result
        cfg = self._engine.config
        cfg.show_result_text = self._show_result
        cfg.save()
        self._update_show_result_style()
        self.show_result_changed.emit(self._show_result)

    def _style_action_record(self):
        self._btn_action.setText("●")
        self._btn_action.setToolTip("开始录音")
        self._btn_action.setStyleSheet(_BTN_STYLE.format(
            bg=Theme.BG_BUTTON.name(), fg="#ff3b30",
            r=13, hover=Theme.BG_BUTTON_HOVER.name(),
        ))

    def _on_cancel(self):
        self.request_cancel.emit()

    def start_hotkey_hold(self):
        """External trigger: delay stop button until hold becomes a long-press."""
        if self._mode != "recording":
            return
        self._hotkey_hold_timer.start(self.hotkey_click_threshold_ms())

    def stop_hotkey_hold(self):
        """External trigger: cancel the long-press animation (short press release)."""
        self._hotkey_hold_timer.stop()
        self._btn_rec_stop.cancel_external_hold()
        if not self._hovered:
            self._btn_rec_stop.setVisible(False)
            self._animate_to(REC_W, REC_H, 150)

    def hotkey_click_threshold_ms(self) -> int:
        """Return the short-press threshold shared with the stop button."""
        return self._btn_rec_stop.CLICK_THRESHOLD_MS

    def _begin_hotkey_hold_visuals(self):
        """Show the stop button only after the short-press window has passed."""
        if self._mode != "recording":
            return
        self._btn_rec_stop.setVisible(True)
        if not self._hovered:
            self._animate_to(REC_HOVER_W, REC_H, 150)
        self._btn_rec_stop.start_external_hold(skip_click_threshold=True)

    def _show_recording_status(self):
        items: list[str] = []
        dev = self._engine.recorder.device_name
        if dev:
            items.append(f"🎤 {dev}")
        cfg = self._engine.config
        if cfg.mode == "polish":
            model = getattr(self._engine.polisher, '_model', 'unknown')
            items.append(f"✦ {model}")
            prompt_name = "默认提示词"
            if cfg.active_prompt_id:
                for p in cfg.custom_prompts:
                    if p.get("id") == cfg.active_prompt_id:
                        prompt_name = p.get("name", "未命名")
                        break
            items.append(f"📝 {prompt_name}")
        self._status_popup.show_status(items, self)

    def _hide_recording_status(self):
        self._status_popup.hide()

    def _cancel_deferred_shrink(self):
        self._deferred_shrink_timer.stop()

    def _schedule_deferred_shrink(self, delay_ms: int):
        self._cancel_deferred_shrink()
        self._deferred_shrink_timer.start(delay_ms)

    def refresh_visibility(self):
        """Apply the current hide-when-idle preference to the minimal idle state."""
        if not self._engine.config.hide_mini_window_when_idle:
            self.show()
            return
        if self._mode == "idle" and self._engine.state == "ready":
            self.hide()

    def _on_action_click(self):
        if self._mode == "hover":
            self.request_record.emit()

    def _set_widgets_for_mode(self, mode: str):
        is_idle = mode == "idle"
        is_hover = mode == "hover"
        is_rec = mode == "recording"

        self._waveform.setVisible(mode in ("recording", "processing", "done"))
        self._btn_action.setVisible(is_hover)
        self._btn_rec_stop.setVisible(is_rec)
        self._btn_polish.setVisible(is_hover)
        self._btn_show_result.setVisible(is_hover)
        self._dot_status.setVisible(mode in ("processing", "done"))
        self._top_bar.setVisible(not is_idle)

    # ── animation helpers ──

    def _get_x_for_width(self, w: int) -> int:
        screen = QApplication.primaryScreen()
        if not screen:
            return self._anchor_x if self._anchor_x is not None else 0
        geo = screen.availableGeometry()
        if self._anchor_x is not None:
            x = self._anchor_x - w // 2
            x = max(geo.x(), min(x, geo.x() + geo.width() - w))
            return x
        return geo.x() + (geo.width() - w) // 2

    def _animate_to(self, w, h, duration=220,
                    easing=QEasingCurve.Type.OutCubic):
        self._target_size = (w, h)
        screen = QApplication.primaryScreen()
        if not screen:
            return
        # Expanding from hidden: show first, keep idle-sized start rect, then animate.
        if not self.isVisible():
            self.show()
            if self.width() < 2 or self.height() < 2:
                self._position_at(IDLE_W, IDLE_H)

        geo = screen.availableGeometry()
        x = self._get_x_for_width(w)
        y = geo.y() + 4
        target = QRect(x, y, w, h)

        self.setMinimumSize(0, 0)
        self.setMaximumSize(16777215, 16777215)

        self._geom_anim.stop()
        self._geom_anim.setEasingCurve(easing)
        self._geom_anim.setDuration(duration)
        self._geom_anim.setStartValue(self.geometry())
        self._geom_anim.setEndValue(target)
        self._geom_anim.start()

    def _on_anim_finished(self):
        w, h = self._target_size
        self.setFixedSize(w, h)
        if self._mode == "shrinking":
            self._mode = "idle"
            self.update()
            if (self._engine.config.hide_mini_window_when_idle
                    and self._engine.state == "ready"):
                self.hide()
            else:
                self.show()

    def _position_at(self, w, h):
        screen = QApplication.primaryScreen()
        if not screen:
            return
        geo = screen.availableGeometry()
        x = self._get_x_for_width(w)
        y = geo.y() + 4
        self.setFixedSize(w, h)
        self.move(x, y)

    # ── state transitions ──

    def _apply_hover(self):
        self._mode = "hover"
        self._style_action_record()
        self._update_polish_style()
        self._update_show_result_style()
        self._set_widgets_for_mode("hover")
        self._animate_to(HOVER_W, HOVER_H, 300,
                         QEasingCurve.Type.InOutQuart)

    def _apply_recording(self):
        self._cancel_deferred_shrink()
        self._mode = "recording"
        self._waveform.reset()
        self._dot_status.setStyleSheet(f"color: {Theme.COLOR_RECORDING.name()};")

        self._waveform.setVisible(True)
        self._btn_action.setVisible(False)
        self._btn_polish.setVisible(False)
        self._btn_show_result.setVisible(False)
        self._dot_status.setVisible(False)
        self._top_bar.setVisible(True)

        if self._hovered:
            self._btn_rec_stop.setVisible(True)
            self._animate_to(REC_HOVER_W, REC_H, 280)
        else:
            self._btn_rec_stop.setVisible(False)
            self._animate_to(REC_W, REC_H, 280)

    def _apply_processing(self):
        self._cancel_deferred_shrink()
        self._mode = "processing"
        self._waveform.freeze()
        self._dot_status.setStyleSheet(f"color: {Theme.COLOR_PROCESSING.name()};")
        self._btn_action.setVisible(False)
        self._dot_status.setVisible(True)

    def _apply_done(self):
        self._mode = "done"
        self._dot_status.setStyleSheet(f"color: {Theme.COLOR_DONE.name()};")

    def _shrink_to_idle(self):
        if self._engine.state != "ready":
            return
        if self._mode in ("idle", "shrinking"):
            return
        self._mode = "shrinking"
        self._hide_recording_status()
        self._set_widgets_for_mode("idle")
        self._animate_to(IDLE_W, IDLE_H, 280,
                         QEasingCurve.Type.InOutQuart)
        self.show()

    # ── engine signals ──

    def _on_engine_state(self, state: str):
        logger.debug(f"[MiniWin] Engine state → {state} (was {self._mode})")
        if state == "recording":
            self._apply_recording()
        elif state == "processing":
            self._apply_processing()
        elif state == "ready":
            if self._mode in ("recording", "processing"):
                self._shrink_to_idle()

    def _on_audio(self, data: bytes):
        if self._mode == "recording":
            self._waveform.update_data(data)

    def _on_done(self, text: str):
        self._apply_done()
        if self._show_result:
            self._result_popup.show_text(text, self)
        self._schedule_deferred_shrink(800)

    def _on_hover_timeout(self):
        if not self._hovered and self._mode == "hover":
            self._shrink_to_idle()

    # ── painting ──

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._dot_status.move(self.width() - 13, 3)
        self._dot_status.raise_()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        w, h = float(self.width()), float(self.height())
        r = min(RADIUS, h / 2)
        path.addRoundedRect(0, 0, w, h, r, r)
        bg = QColor(Theme.BG_PRIMARY)
        bg.setAlpha(245)
        p.fillPath(path, bg)
        p.setPen(QPen(QColor(255, 255, 255, 30), 1.0))
        p.drawPath(path)
        p.end()

    # ── hover / drag ──

    def enterEvent(self, event):
        self._hovered = True
        self._hover_timer.stop()
        if self._mode in ("idle", "shrinking"):
            self._apply_hover()
        elif self._mode == "recording":
            self._btn_rec_stop.setVisible(True)
            self._animate_to(REC_HOVER_W, REC_H, 150)
            self._show_recording_status()
        self.update()

    def leaveEvent(self, event):
        self._hovered = False
        self._hide_recording_status()
        if self._mode == "hover":
            self._hover_timer.start(300)
        elif self._mode == "recording":
            self._btn_rec_stop.setVisible(False)
            self._animate_to(REC_W, REC_H, 150)
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = (
                event.globalPosition().toPoint()
                - self.frameGeometry().topLeft()
            )

    def mouseMoveEvent(self, event):
        if self._drag_pos and event.buttons() & Qt.MouseButton.LeftButton:
            new_pos = event.globalPosition().toPoint() - self._drag_pos
            self.move(new_pos)
            self._anchor_x = new_pos.x() + self.width() // 2

    def mouseReleaseEvent(self, event):
        if self._drag_pos is not None and self._anchor_x is not None:
            self._engine.config.mini_window_x = self._anchor_x
            self._engine.config.save()
        self._drag_pos = None

    def reset_position(self):
        self._anchor_x = None
        self._engine.config.mini_window_x = None
        self._engine.config.save()
        w, h = self._target_size
        self._position_at(w, h)

    def contextMenuEvent(self, event):
        event.accept()
