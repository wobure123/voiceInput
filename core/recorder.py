import struct
import time
from typing import Callable

import numpy as np
import pyaudio

from core.log import logger

_TAG = "[Recorder]"

# Rates sorted by suitability for 16 kHz ASR:
#   16000 — exact match (no resample)
#   32000 — clean 2× integer downsample
#   48000 — clean 3× integer downsample
#   44100 — common, non-integer ratio
#   22050 — non-integer ratio
#    8000 — low quality, data loss
_PREFERRED_RATES = (16000, 32000, 48000, 44100, 22050, 8000)


def _fix_name(name: str) -> str:
    """Fix PyAudio device name encoding on Chinese Windows.

    PyAudio sometimes decodes UTF-8 device name bytes using the system
    codepage (GBK), producing mojibake like '鑰虫満' instead of '耳机'.
    Re-encoding as GBK and decoding as UTF-8 recovers the original text.
    """
    try:
        return name.encode("gbk").decode("utf-8")
    except (UnicodeDecodeError, UnicodeEncodeError):
        return name


def _resample(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    """Resample 16-bit mono PCM via linear interpolation."""
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    new_len = int(len(samples) * dst_rate / src_rate)
    indices = np.linspace(0, len(samples) - 1, new_len)
    resampled = np.interp(indices, np.arange(len(samples)), samples)
    return resampled.astype(np.int16).tobytes()


def _mix_to_mono(data: bytes, channels: int) -> bytes:
    """Mix interleaved multi-channel int16 PCM to mono by averaging."""
    samples = np.frombuffer(data, dtype=np.int16)
    frames = samples.reshape(-1, channels)
    mono = frames.mean(axis=1).astype(np.int16)
    return mono.tobytes()


class VoiceRecorder:
    TARGET_RATE = 16000
    CHANNELS = 1
    MAX_DURATION = 600       # 10 min hard cap
    SILENCE_THRESHOLD = 200  # peak int16 amplitude below this = silence

    def __init__(self, device_index: int | None = None, preferred_name: str = ""):
        self._device_index = device_index
        self._preferred_name = preferred_name
        self._pa: pyaudio.PyAudio | None = None
        self._stream: pyaudio.Stream | None = None
        self._audio_chunks: list[bytes] = []
        self._recording = False
        self._on_audio_data: Callable[[bytes], None] | None = None
        self._on_max_reached: Callable[[], None] | None = None
        self._on_mic_error: Callable[[], None] | None = None
        self._start_time: float = 0.0
        self._chunk_count = 0
        self._peak_amplitude: int = 0
        self._status_errors: int = 0
        self._consecutive_errors: int = 0
        self._last_callback_time: float = 0.0
        self._device_name: str = ""
        self._stream_rate: int = self.TARGET_RATE
        self._stream_channels: int = self.CHANNELS
        self._max_channels: int = self.CHANNELS
        self._native_rate: int = 0
        self._prepared = False
        self._no_device = False

    @property
    def is_recording(self) -> bool:
        return self._recording

    @property
    def no_device(self) -> bool:
        return self._no_device

    @property
    def peak_amplitude(self) -> int:
        return self._peak_amplitude

    @property
    def status_error_count(self) -> int:
        return self._status_errors

    @property
    def device_name(self) -> str:
        return self._device_name

    # ── prepare / release ──

    def prepare(self):
        """Init PyAudio, negotiate params, and pre-open stream (stopped).

        The stream is opened with start=False so start() only needs
        start_stream(), which is near-instant.  The Windows mic icon
        does NOT appear because the stream is not active (only
        start_stream activates the audio pipeline).

        If the pre-open fails (rare), start() will retry on demand.
        """
        self._close_stream()
        self._release_pa()
        t0 = time.perf_counter()
        self._pa = pyaudio.PyAudio()
        info = self._device_info()

        if info is None:
            elapsed_ms = (time.perf_counter() - t0) * 1000
            self._no_device = True
            self._device_name = "Unknown"
            self._native_rate = 0
            self._max_channels = 0
            self._stream_rate = self.TARGET_RATE
            self._stream_channels = self.CHANNELS
            logger.info(f"{_TAG} No input device | NO STREAM ({elapsed_ms:.0f}ms)")
            self._prepared = True
            return

        self._no_device = False
        self._device_name = _fix_name(info.get("name", "Unknown"))
        self._native_rate = int(info.get("defaultSampleRate", 0))
        self._max_channels = int(info.get("maxInputChannels", self.CHANNELS))

        self._stream_rate, self._stream_channels = self._negotiate_params()

        stream_ok = self._try_open_at(self._stream_rate, self._stream_channels)
        if not stream_ok:
            logger.warning(f"{_TAG} Negotiated params failed, trying alternatives")
            stream_ok = self._try_open_stream()

        elapsed_ms = (time.perf_counter() - t0) * 1000
        ch_note = f" ch={self._stream_channels}→mono" \
            if self._stream_channels > 1 else ""
        rate_note = f" resample→{self.TARGET_RATE}" \
            if self._stream_rate != self.TARGET_RATE else ""
        status = "ready" if stream_ok else "NO STREAM"
        logger.info(
            f"{_TAG} Prepared '{self._device_name}' | "
            f"native={self._native_rate} Hz ch={self._max_channels} | "
            f"stream={self._stream_rate} Hz{ch_note}{rate_note} | "
            f"{status} ({elapsed_ms:.0f}ms)")
        self._prepared = True

    def release(self):
        """Full shutdown — terminate PyAudio. Call on app exit."""
        self._release_pa()
        self._prepared = False

    def set_device(self, index: int | None, preferred_name: str = ""):
        old = self._device_index
        old_name = self._preferred_name
        self._device_index = index
        self._preferred_name = preferred_name
        logger.info(
            f"{_TAG} Device changed: index {old} → {index}, "
            f"name '{old_name or 'system default'}' → '{preferred_name or 'system default'}'"
        )
        self.prepare()

    # ── recording lifecycle ──

    def start(self, on_audio_data: Callable[[bytes], None] | None = None,
              on_max_reached: Callable[[], None] | None = None,
              on_mic_error: Callable[[], None] | None = None):
        self._audio_chunks = []
        self._on_audio_data = on_audio_data
        self._on_max_reached = on_max_reached
        self._on_mic_error = on_mic_error
        self._start_time = time.monotonic()
        self._chunk_count = 0
        self._peak_amplitude = 0
        self._status_errors = 0
        self._consecutive_errors = 0
        self._last_callback_time = time.monotonic()

        if not self._prepared:
            self.prepare()

        if not self._stream:
            if not self._try_open_at(self._stream_rate, self._stream_channels):
                logger.warning(f"{_TAG} Negotiated params failed, trying alternatives")
                if not self._try_open_stream():
                    raise OSError(f"无法打开设备 '{self._device_name}'")

        t0 = time.perf_counter()
        self._recording = True
        try:
            self._stream.start_stream()
        except OSError:
            self._recording = False
            logger.warning(f"{_TAG} start_stream() failed, re-opening")
            self._close_stream()
            if not self._try_open_at(self._stream_rate, self._stream_channels):
                if not self._try_open_stream():
                    raise OSError(f"无法打开设备 '{self._device_name}'")
            self._recording = True
            self._stream.start_stream()
        start_ms = (time.perf_counter() - t0) * 1000

        dev_label = f"device {self._device_index}" \
            if self._device_index is not None else "default"
        logger.info(f"{_TAG} Recording started: {dev_label} "
                    f"'{self._device_name}' @ {self._stream_rate} Hz "
                    f"ch={self._stream_channels} (start_stream {start_ms:.0f}ms)")

    def stop(self) -> bytes:
        self._recording = False
        self._stop_stream()

        if not self._audio_chunks:
            logger.warning(f"{_TAG} Stop: no audio chunks collected")
            return b""

        pcm = b"".join(self._audio_chunks)
        raw_duration = len(pcm) / (self._stream_rate * 2)

        if self._stream_rate != self.TARGET_RATE:
            pcm = _resample(pcm, self._stream_rate, self.TARGET_RATE)
            logger.debug(f"{_TAG} Resampled {self._stream_rate} → {self.TARGET_RATE} Hz")

        duration = len(pcm) / (self.TARGET_RATE * 2)
        logger.info(f"{_TAG} Stop: {self._chunk_count} chunks, "
                    f"{raw_duration:.1f}s captured, {duration:.1f}s output, "
                    f"peak={self._peak_amplitude}, errors={self._status_errors}")
        return pcm

    def cancel(self):
        self._recording = False
        self._stop_stream()
        self._audio_chunks = []
        logger.info(f"{_TAG} Recording cancelled, chunks discarded")

    # ── stream management ──

    def _try_open_stream(self) -> bool:
        """Try to open stream: mono first, then device native channels.

        WASAPI devices often require their native channel count.  If mono
        fails at all rates, we retry with the device's maxInputChannels and
        mix to mono in the callback.
        """
        candidates = self._open_candidates()

        channels_to_try = [self.CHANNELS]
        if self._max_channels > self.CHANNELS:
            channels_to_try.append(self._max_channels)

        for ch in channels_to_try:
            for rate in candidates:
                if self._try_open_at(rate, ch):
                    return True

        return False

    def _try_open_at(self, rate: int, channels: int) -> bool:
        try:
            block = max(int(rate * 0.2), 1600)
            kwargs = dict(
                format=pyaudio.paInt16,
                channels=channels,
                rate=rate,
                input=True,
                start=False,
                frames_per_buffer=block,
                stream_callback=self._audio_callback,
            )
            if self._device_index is not None:
                kwargs["input_device_index"] = self._device_index
            self._stream = self._pa.open(**kwargs)
            self._stream_channels = channels
            self._stream_rate = rate
            notes = []
            if channels != self.CHANNELS:
                notes.append(f"ch={channels}→mono")
            if rate != self.TARGET_RATE:
                notes.append(f"resample→{self.TARGET_RATE}")
            if notes:
                logger.info(f"{_TAG} Opened at {rate} Hz ch={channels} "
                            f"({', '.join(notes)})")
            return True
        except OSError as e:
            logger.warning(f"{_TAG} open() failed @ {rate} Hz ch={channels}: {e}")
            return False

    def _open_candidates(self) -> list[int]:
        """Build ordered candidate list for fallback open() attempts."""
        seen: set[int] = set()
        result: list[int] = []
        for r in _PREFERRED_RATES:
            if r not in seen:
                result.append(r)
                seen.add(r)
        if self._native_rate > 0 and self._native_rate not in seen:
            result.append(self._native_rate)
        return result

    def get_duration(self) -> float:
        total_bytes = sum(len(c) for c in self._audio_chunks)
        return total_bytes / (self._stream_rate * 2)

    def is_silent(self) -> bool:
        return self._peak_amplitude < self.SILENCE_THRESHOLD

    STALL_TIMEOUT = 3.0

    def is_stalled(self) -> bool:
        """True if no callback has fired for STALL_TIMEOUT seconds."""
        if not self._recording or self._last_callback_time == 0.0:
            return False
        return (time.monotonic() - self._last_callback_time) > self.STALL_TIMEOUT

    # ── capability negotiation ──

    def _device_info(self) -> dict | None:
        if self._preferred_name and self._device_index is None:
            self._device_index = self.resolve_device(self._preferred_name, None)
        if self._device_index is not None:
            try:
                return self._pa.get_device_info_by_index(self._device_index)
            except OSError:
                logger.warning(
                    f"{_TAG} Device index {self._device_index} no longer valid, "
                    f"falling back to system default")
                self._device_index = None
        try:
            info = self._pa.get_default_input_device_info()
            try:
                from core.device_watcher import get_default_capture_device_name
                full_name = get_default_capture_device_name()
                if full_name:
                    info = dict(info)
                    info["name"] = full_name
            except Exception:
                pass
            return info
        except OSError:
            logger.warning(f"{_TAG} No default input device available")
            return None

    def _negotiate_params(self) -> tuple[int, int]:
        """Choose best (rate, channels) using is_format_supported only.

        No pa.open() calls — purely queries the driver.  Prefers mono over
        native channels, and rates closer to TARGET_RATE (see _PREFERRED_RATES).
        """
        candidates = list(_PREFERRED_RATES)
        if self._native_rate > 0 and self._native_rate not in candidates:
            candidates.append(self._native_rate)

        channels_options = [self.CHANNELS]
        if self._max_channels > self.CHANNELS:
            channels_options.append(self._max_channels)

        for rate in candidates:
            for ch in channels_options:
                if self._is_rate_supported(rate, ch):
                    logger.debug(f"{_TAG} Negotiated: {rate} Hz ch={ch}")
                    return (rate, ch)

        fallback_rate = self._native_rate if self._native_rate > 0 else self.TARGET_RATE
        fallback_ch = self._max_channels if self._max_channels > 0 else self.CHANNELS
        logger.warning(f"{_TAG} No format passed is_format_supported, "
                       f"fallback to {fallback_rate} Hz ch={fallback_ch}")
        return (fallback_rate, fallback_ch)

    def _is_rate_supported(self, rate: int, channels: int | None = None) -> bool:
        if channels is None:
            channels = self.CHANNELS
        try:
            return bool(self._pa.is_format_supported(
                rate,
                input_device=self._device_index,
                input_channels=channels,
                input_format=pyaudio.paInt16,
            ))
        except (ValueError, OSError):
            return False

    # ── audio callback ──

    CONSECUTIVE_ERROR_LIMIT = 15

    def _audio_callback(self, in_data, frame_count, time_info, status):
        if not self._recording:
            return (None, pyaudio.paContinue)

        self._last_callback_time = time.monotonic()

        if status:
            self._status_errors += 1
            self._consecutive_errors += 1
            if self._status_errors <= 3:
                flags = []
                if status & pyaudio.paInputUnderflow:
                    flags.append("InputUnderflow")
                if status & pyaudio.paInputOverflow:
                    flags.append("InputOverflow")
                logger.warning(f"{_TAG} PortAudio status: {' | '.join(flags) or status}")
            if self._consecutive_errors >= self.CONSECUTIVE_ERROR_LIMIT:
                logger.error(f"{_TAG} {self._consecutive_errors} consecutive errors, "
                             f"mic likely disconnected")
                self._recording = False
                if self._on_mic_error:
                    self._on_mic_error()
                return (None, pyaudio.paContinue)
        else:
            self._consecutive_errors = 0

        if self._stream_channels > 1:
            in_data = _mix_to_mono(in_data, self._stream_channels)

        chunk_peak = self._chunk_peak_amplitude(in_data)
        if chunk_peak > self._peak_amplitude:
            self._peak_amplitude = chunk_peak

        self._audio_chunks.append(in_data)
        self._chunk_count += 1

        if self._chunk_count == 1:
            logger.debug(f"{_TAG} First audio chunk received "
                         f"({len(in_data)} bytes, peak={chunk_peak})")

        if self._on_audio_data:
            self._on_audio_data(in_data)

        elapsed = time.monotonic() - self._start_time
        if elapsed >= self.MAX_DURATION:
            logger.warning(f"{_TAG} Max duration ({self.MAX_DURATION}s) reached")
            self._recording = False
            if self._on_max_reached:
                self._on_max_reached()

        return (None, pyaudio.paContinue)

    # ── internals ──

    def _stop_stream(self):
        """Stop the audio stream (releases mic indicator) but keep it open."""
        if self._stream:
            try:
                was_active = self._stream.is_active()
                if was_active:
                    self._stream.stop_stream()
                logger.debug(f"{_TAG} _stop_stream: was_active={was_active}")
            except Exception as e:
                logger.debug(f"{_TAG} _stop_stream error: {e}")

    def _close_stream(self):
        """Close the audio stream but keep PyAudio alive."""
        self._stop_stream()
        if self._stream:
            try:
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    def _release_pa(self):
        """Close stream and terminate PyAudio."""
        self._close_stream()
        if self._pa:
            try:
                self._pa.terminate()
            except Exception:
                pass
            self._pa = None

    @staticmethod
    def _chunk_peak_amplitude(data: bytes) -> int:
        n = len(data) // 2
        if n == 0:
            return 0
        samples = struct.unpack(f"<{n}h", data)
        peak = 0
        for s in samples:
            v = s if s >= 0 else -s
            if v > peak:
                peak = v
        return peak

    # ── device enumeration ──

    @classmethod
    def resolve_device(cls, saved_name: str, saved_index: int | None) -> int | None:
        """Resolve a saved device to a current valid index by name.

        Device indices drift when hardware is added/removed.  The device
        *name* is the stable identifier.  Returns None for system default.
        """
        if not saved_name and saved_index is None:
            return None

        devices = cls.list_devices()

        if saved_name:
            for dev in devices:
                if dev["name"] == saved_name:
                    if dev["index"] != saved_index:
                        logger.info(f"{_TAG} Device '{saved_name}' index "
                                    f"moved: {saved_index} → {dev['index']}")
                    return dev["index"]
            logger.warning(f"{_TAG} Saved device '{saved_name}' not found "
                           f"in current device list")
            return None

        logger.warning(f"{_TAG} No device name saved (legacy config), "
                       f"using system default")
        return None

    @classmethod
    def list_devices(cls) -> list[dict]:
        """List all WASAPI input devices (deduplicated by name)."""
        pa = pyaudio.PyAudio()
        try:
            wasapi_idx = None
            for h in range(pa.get_host_api_count()):
                api = pa.get_host_api_info_by_index(h)
                if "WASAPI" in api.get("name", ""):
                    wasapi_idx = h
                    break

            devices = []
            seen: set[str] = set()
            for i in range(pa.get_device_count()):
                info = pa.get_device_info_by_index(i)
                if info.get("maxInputChannels", 0) <= 0:
                    continue
                if wasapi_idx is not None and info.get("hostApi") != wasapi_idx:
                    continue
                name = _fix_name(info.get("name", f"Device {i}"))
                if name in seen:
                    continue
                seen.add(name)
                devices.append({"index": i, "name": name})

            logger.debug(f"{_TAG} Enumerated {len(devices)} input device(s)")
            return devices
        finally:
            pa.terminate()

    @classmethod
    def get_default_device_name(cls) -> str:
        pa = pyaudio.PyAudio()
        try:
            info = pa.get_default_input_device_info()
            return _fix_name(info.get("name", "Unknown"))
        except Exception:
            return "Unknown"
        finally:
            pa.terminate()
