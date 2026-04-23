"""Tray icons and shared app icon helpers."""
import sys
from pathlib import Path

from PyQt6.QtCore import QRect, Qt
from PyQt6.QtGui import QColor, QIcon, QPainter, QPixmap

from ui.theme import Theme

_SIZE = 64
_APP_ICON: QIcon | None = None


def _app_icon_candidates() -> list[Path]:
    here = Path(__file__).resolve()
    candidates: list[Path] = [
        here.parent / "assets" / "app_icon.ico",
    ]
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable))
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "ui" / "assets" / "app_icon.ico")
        candidates.append(Path(meipass) / "assets" / "app_icon.ico")
    candidates.append(here.parents[2] / "assets" / "app_icon.ico")
    return candidates


def _draw_mic(p: QPainter, size: int) -> None:
    cx = size // 2
    bw, bh = size // 4, size * 5 // 12
    body = QRect(cx - bw, size // 6, bw * 2, bh)
    p.setBrush(p.pen().color())
    p.drawRoundedRect(body, bw * 0.6, bw * 0.6)

    arc_w = size * 5 // 8
    arc_rect = QRect(cx - arc_w // 2, size // 5, arc_w, size // 2)
    p.setBrush(Qt.BrushStyle.NoBrush)
    pen = p.pen()
    pen.setWidth(max(2, size // 16))
    p.setPen(pen)
    p.drawArc(arc_rect, -30 * 16, -120 * 16)

    stand_top = arc_rect.bottom() - size // 12
    stand_bot = size * 5 // 6
    p.drawLine(cx, stand_top, cx, stand_bot)
    base_w = size // 3
    p.drawLine(cx - base_w // 2, stand_bot, cx + base_w // 2, stand_bot)


def _make_icon(mic_color: QColor, dot_color: QColor | None = None) -> QIcon:
    pix = QPixmap(_SIZE, _SIZE)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = p.pen()
    pen.setColor(mic_color)
    pen.setWidth(max(1, _SIZE // 16))
    p.setPen(pen)
    _draw_mic(p, _SIZE)
    if dot_color is not None:
        r = _SIZE // 5
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(dot_color)
        p.drawEllipse(_SIZE - r - 1, 1, r, r)
    p.end()
    return QIcon(pix)


def app_icon() -> QIcon:
    global _APP_ICON
    if _APP_ICON is not None:
        return _APP_ICON

    for path in _app_icon_candidates():
        if not path.is_file():
            continue
        icon = QIcon(str(path))
        if not icon.isNull():
            _APP_ICON = icon
            return _APP_ICON

    _APP_ICON = icon_idle()
    return _APP_ICON


def icon_idle() -> QIcon:
    return _make_icon(QColor(140, 140, 140))


def icon_recording() -> QIcon:
    return _make_icon(QColor(30, 30, 30), Theme.COLOR_RECORDING)


def icon_processing() -> QIcon:
    return _make_icon(QColor(80, 80, 80), Theme.COLOR_PROCESSING)


def icon_done() -> QIcon:
    return _make_icon(QColor(80, 80, 80), Theme.COLOR_DONE)


def icon_key_invalid() -> QIcon:
    return _make_icon(QColor(140, 140, 140), Theme.COLOR_WARNING)
