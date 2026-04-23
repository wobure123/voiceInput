from PyQt6.QtGui import QColor, QFont


class Theme:
    BG_PRIMARY = QColor(26, 26, 26, 242)        # #1a1a1a @ 95%
    BG_SECONDARY = QColor(42, 42, 42)            # #2a2a2a
    BG_BUTTON = QColor(58, 58, 58)               # #3a3a3a
    BG_BUTTON_HOVER = QColor(75, 75, 75)         # #4b4b4b
    BG_BUTTON_ACTIVE = QColor(90, 90, 90)

    TEXT_PRIMARY = QColor(255, 255, 255)
    TEXT_SECONDARY = QColor(153, 153, 153)        # #999
    TEXT_DIM = QColor(102, 102, 102)              # #666

    COLOR_RECORDING = QColor(255, 59, 48)         # iOS red
    COLOR_PROCESSING = QColor(0, 122, 255)        # iOS blue
    COLOR_DONE = QColor(52, 199, 89)              # iOS green
    COLOR_WARNING = QColor(255, 204, 0)           # iOS yellow

    WAVEFORM_ACTIVE = QColor(224, 224, 224)        # #e0e0e0
    WAVEFORM_FROZEN = QColor(80, 80, 80)

    PADDING = 24

    @staticmethod
    def font(size: int = 13, monospace: bool = False, bold: bool = False) -> QFont:
        family = "Consolas" if monospace else "Segoe UI"
        f = QFont(family, size)
        if bold:
            f.setBold(True)
        return f
