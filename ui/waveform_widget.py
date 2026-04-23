import numpy as np
from PyQt6.QtCore import QTimer, QRectF
from PyQt6.QtGui import QPainter, QColor, QPainterPath
from PyQt6.QtWidgets import QWidget

from ui.theme import Theme


class WaveformWidget(QWidget):
    BAR_COUNT = 40
    BAR_GAP = 2.5
    BAR_RADIUS = 1.5
    BAR_MIN_H = 2.0
    FPS = 24
    LERP_UP = 0.6
    LERP_DOWN = 0.3
    DECAY = 0.84
    PROPAGATION_DAMPING = 0.55

    AGC_WINDOW = 8
    AGC_BASE_GAIN = 3.5
    AGC_RATIO = 0.7
    AGC_MAX_SCALE = 15.0

    def __init__(self, parent=None, compact: bool = False):
        super().__init__(parent)
        self._compact = compact
        if compact:
            self.BAR_COUNT = 14
            self.BAR_GAP = 2.0

        self._levels = np.zeros(self.BAR_COUNT)
        self._raw_target = np.zeros(self.BAR_COUNT)
        self._color = Theme.WAVEFORM_ACTIVE
        self._frozen = False
        self._peak_buf = np.zeros(self.AGC_WINDOW)
        self._peak_idx = 0
        self._agc_primed = False

        center = (self.BAR_COUNT - 1) / 2.0
        distances = np.abs(np.arange(self.BAR_COUNT) - center) / max(center, 1.0)
        spread = 1.0 - distances * self.PROPAGATION_DAMPING
        self._lerp_up = self.LERP_UP * spread
        self._lerp_down = self.LERP_DOWN * spread

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(1000 // self.FPS)

    def update_data(self, pcm_chunk: bytes):
        if self._frozen:
            return
        samples = np.frombuffer(pcm_chunk, dtype=np.int16).astype(np.float32) / 32768.0
        n = len(samples)
        if n == 0:
            return
        chunk_size = max(1, n // self.BAR_COUNT)
        usable = chunk_size * self.BAR_COUNT
        if usable > n:
            samples = np.pad(samples, (0, usable - n))
        matrix = samples[:usable].reshape(self.BAR_COUNT, chunk_size)
        rms = np.sqrt(np.mean(matrix ** 2, axis=1))
        peak = np.max(np.abs(matrix), axis=1)
        raw = np.sqrt(rms * 0.6 + peak * 0.4)

        cur_mean = float(np.mean(raw))
        if not self._agc_primed:
            self._peak_buf[:] = cur_mean
            self._agc_primed = True
        else:
            self._peak_buf[self._peak_idx] = cur_mean
        self._peak_idx = (self._peak_idx + 1) % self.AGC_WINDOW
        recent_avg = float(np.mean(self._peak_buf))

        agc_scale = 0.45 / max(recent_avg, 1e-3)
        agc_scale = min(agc_scale, self.AGC_MAX_SCALE)
        r = self.AGC_RATIO
        scale = self.AGC_BASE_GAIN * (1 - r) + agc_scale * r

        self._raw_target = np.clip(raw * scale, 0.0, 1.0)

    def freeze(self):
        self._frozen = True
        self._color = Theme.WAVEFORM_FROZEN

    def unfreeze(self):
        self._frozen = False
        self._color = Theme.WAVEFORM_ACTIVE
        self._raw_target = np.zeros(self.BAR_COUNT)
        self._peak_buf = np.zeros(self.AGC_WINDOW)
        self._peak_idx = 0
        self._agc_primed = False

    def reset(self):
        self._levels = np.zeros(self.BAR_COUNT)
        self._raw_target = np.zeros(self.BAR_COUNT)
        self._frozen = False
        self._color = Theme.WAVEFORM_ACTIVE
        self._peak_buf = np.zeros(self.AGC_WINDOW)
        self._peak_idx = 0
        self._agc_primed = False
        self.update()

    def _tick(self):
        diff = self._raw_target - self._levels
        lerp = np.where(diff > 0, self._lerp_up, self._lerp_down)
        self._levels += diff * lerp

        if not self._frozen:
            self._raw_target *= self.DECAY
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        w = self.width()
        h = self.height()
        bar_w = (w - (self.BAR_COUNT - 1) * self.BAR_GAP) / self.BAR_COUNT
        gap = self.BAR_GAP
        if bar_w < 1.5:
            bar_w = 1.5
            gap = max(0.5, (w - self.BAR_COUNT * bar_w) / max(1, self.BAR_COUNT - 1))
        cy = h / 2.0

        color = QColor(self._color)
        path = QPainterPath()

        for i in range(self.BAR_COUNT):
            x = i * (bar_w + gap)
            bar_h = max(self.BAR_MIN_H, self._levels[i] * h * 0.85)
            half_h = bar_h / 2.0
            path.addRoundedRect(
                QRectF(x, cy - half_h, bar_w, bar_h),
                self.BAR_RADIUS, self.BAR_RADIUS,
            )

        p.fillPath(path, color)
        p.end()
