"""Programmatic audio cues via pyaudio callback stream.

Generates short beep/chirp sounds for audio feedback:
  - start: short rising chirp  (recording begins)
  - stop:  short falling chirp (recording stops)
  - done:  gentle confirmation tone (transcription complete)

Uses a pyaudio callback-mode output stream opened once at init and
kept alive for the app lifetime.  Sound data is queued into a buffer
and consumed by the audio callback without blocking the caller.
A silence prefix is prepended on first play to wake the Bluetooth HFP
channel.  Falls back to winsound if pyaudio output fails.
"""
import math
import struct
import io
import collections
import threading
import time
import winsound

import pyaudio

from core.log import logger

_SAMPLE_RATE = 22050
_TAG = "[Sound]"

_FRAMES_PER_BUFFER = 1024


def _make_wav(pcm: bytes) -> bytes:
    """Wrap raw int16 mono PCM into a WAV container (for winsound fallback)."""
    buf = io.BytesIO()
    data_size = len(pcm)
    buf.write(b"RIFF")
    buf.write(struct.pack("<I", 36 + data_size))
    buf.write(b"WAVE")
    buf.write(b"fmt ")
    buf.write(struct.pack("<IHHIIHH", 16, 1, 1, _SAMPLE_RATE, _SAMPLE_RATE * 2, 2, 16))
    buf.write(b"data")
    buf.write(struct.pack("<I", data_size))
    buf.write(pcm)
    return buf.getvalue()


def _gen_pcm(samples: list[float]) -> bytes:
    """Pack float samples [-1,1] into raw 16-bit mono PCM."""
    return b"".join(struct.pack("<h", int(max(-1.0, min(1.0, s)) * 32000)) for s in samples)


def _gen_chirp(duration_ms: int, freq_start: int, freq_end: int, volume: float = 0.4) -> bytes:
    n = int(_SAMPLE_RATE * duration_ms / 1000)
    samples = []
    for i in range(n):
        t = i / _SAMPLE_RATE
        progress = i / n
        freq = freq_start + (freq_end - freq_start) * progress
        envelope = math.sin(math.pi * progress)
        samples.append(math.sin(2 * math.pi * freq * t) * envelope * volume)
    return _gen_pcm(samples)


def _gen_confirm(volume: float = 0.3) -> bytes:
    """Two-tone confirmation: C5 then E5."""
    n1 = int(_SAMPLE_RATE * 0.08)
    n2 = int(_SAMPLE_RATE * 0.12)
    gap = int(_SAMPLE_RATE * 0.03)
    samples = []
    for i in range(n1):
        t = i / _SAMPLE_RATE
        env = math.sin(math.pi * i / n1)
        samples.append(math.sin(2 * math.pi * 523 * t) * env * volume)
    samples.extend([0.0] * gap)
    for i in range(n2):
        t = i / _SAMPLE_RATE
        env = math.sin(math.pi * i / n2)
        samples.append(math.sin(2 * math.pi * 659 * t) * env * volume)
    return _gen_pcm(samples)


class AudioCues:
    """Manages playback of UI sound effects.

    Opens a callback-mode pyaudio output stream at init and keeps it
    alive.  Enqueuing sound data returns immediately; the audio callback
    drains the buffer in the background.
    """

    def __init__(self):
        self._enabled = True
        self._sounds = {
            "start": _gen_chirp(80, 800, 1200, 0.35),
            "stop": _gen_chirp(80, 1000, 600, 0.35),
            "done": _gen_confirm(0.3),
        }
        self._pa: pyaudio.PyAudio | None = None
        self._stream: pyaudio.Stream | None = None
        self._lock = threading.Lock()
        self._buf: collections.deque[bytes] = collections.deque()
        self._stream_ready = False
        self._init_stream()

    def _init_stream(self):
        try:
            t0 = time.perf_counter()
            self._pa = pyaudio.PyAudio()
            self._stream = self._pa.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=_SAMPLE_RATE,
                output=True,
                frames_per_buffer=_FRAMES_PER_BUFFER,
                stream_callback=self._audio_callback,
            )
            self._stream_ready = True
            ms = (time.perf_counter() - t0) * 1000
            logger.info(f"{_TAG} Callback stream ready ({ms:.0f}ms)")
        except Exception as e:
            logger.warning(f"{_TAG} Stream init failed: {e}")
            self._stream_ready = False

    def _audio_callback(self, in_data, frame_count, time_info, status):
        needed = frame_count * 2  # 16-bit mono
        data = b""
        while len(data) < needed and self._buf:
            chunk = self._buf[0]
            take = needed - len(data)
            if len(chunk) <= take:
                data += self._buf.popleft()
            else:
                data += chunk[:take]
                self._buf[0] = chunk[take:]
        if len(data) < needed:
            data += b"\x00" * (needed - len(data))
        return (data, pyaudio.paContinue)

    def release(self):
        """Terminate PyAudio. Call on app exit."""
        with self._lock:
            if self._stream is not None:
                try:
                    self._stream.stop_stream()
                    self._stream.close()
                except Exception:
                    pass
                self._stream = None
            if self._pa:
                try:
                    self._pa.terminate()
                except Exception:
                    pass
                self._pa = None
            self._stream_ready = False
            self._buf.clear()

    def set_enabled(self, enabled: bool):
        self._enabled = enabled

    def _enqueue(self, name: str, pcm: bytes):
        t0 = time.perf_counter()
        with self._lock:
            if not self._stream_ready:
                raise RuntimeError("PyAudio stream not available")
            self._buf.append(pcm)
        ms = (time.perf_counter() - t0) * 1000
        logger.info(f"{_TAG} '{name}' enqueued ({ms:.0f}ms)")

    def _play_via_winsound(self, name: str, pcm: bytes):
        wav = _make_wav(pcm)
        winsound.PlaySound(wav, winsound.SND_MEMORY)

    def _play(self, name: str, pcm: bytes):
        logger.info(f"{_TAG} Playing '{name}' ({len(pcm)} B)")
        try:
            self._enqueue(name, pcm)
        except Exception as e:
            logger.warning(f"{_TAG} pyaudio failed for '{name}': {e}, trying winsound")
            try:
                threading.Thread(
                    target=self._play_via_winsound, args=(name, pcm), daemon=True
                ).start()
            except Exception as e2:
                logger.warning(f"{_TAG} '{name}' winsound also failed: {e2}")

    def play_start(self):
        if self._enabled:
            self._play("start", self._sounds["start"])

    def play_stop(self):
        if self._enabled:
            self._play("stop", self._sounds["stop"])

    def play_done(self):
        if self._enabled:
            self._play("done", self._sounds["done"])
