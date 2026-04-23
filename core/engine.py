"""Core voice engine — coordinates recording, ASR, and text injection."""
import time

from PyQt6.QtCore import QObject, pyqtSignal, QThread, QTimer

from config import Config
from core.log import logger
from core.recorder import VoiceRecorder
from core.asr import DashScopeASR, OpenAICompatASR
from core.injector import TextInjector
from core.history import HistoryManager
from core.polisher import TextPolisher

_TAG = "[Engine]"


class _TranscribeWorker(QThread):
    result_ready = pyqtSignal(str)
    error_occurred = pyqtSignal(str)

    def __init__(self, asr: DashScopeASR, pcm_data: bytes, duration: float):
        super().__init__()
        self._asr = asr
        self._pcm = pcm_data
        self._duration = duration

    def run(self):
        try:
            logger.info(f"[ASR] Transcribing {self._duration:.1f}s audio (model: {self._asr.model})")
            t0 = time.perf_counter()
            text = self._asr.transcribe(self._pcm)
            elapsed = time.perf_counter() - t0
            logger.info(f"[ASR] Transcription done in {elapsed:.1f}s → {len(text)} chars")
            self.result_ready.emit(text)
        except Exception as e:
            logger.error(f"[ASR] Transcription failed: {e}")
            self.error_occurred.emit(str(e))


class _PolishWorker(QThread):
    result_ready = pyqtSignal(str)
    polish_failed = pyqtSignal(str)

    def __init__(self, polisher: TextPolisher, raw_text: str, config: Config):
        super().__init__()
        self._polisher = polisher
        self._raw = raw_text
        self._config = config

    def run(self):
        extra = (self._config.active_prompt_text or "").strip()
        logger.info(f"[Polisher] Polishing {len(self._raw)} chars (model: {self._polisher._model})")
        t0 = time.perf_counter()
        ok, result = self._polisher.polish(self._raw, extra)
        elapsed = time.perf_counter() - t0
        logger.info(f"[Polisher] Done in {elapsed:.1f}s → {len(result)} chars")
        if not ok:
            self.polish_failed.emit("润色失败，已使用原文")
        self.result_ready.emit(result)


class VoiceEngine(QObject):
    state_changed = pyqtSignal(str)       # "ready" | "recording" | "processing"
    audio_data = pyqtSignal(bytes)         # raw PCM chunks for waveform
    live_text = pyqtSignal(str)            # status text for expanded panel
    transcription_done = pyqtSignal(str)   # final text
    # All user-visible failures; presentation: ui.user_notification_hub + core.user_errors
    error_occurred = pyqtSignal(str)
    # Device/mic failures (tray balloon)
    mic_unavailable = pyqtSignal(str)
    _max_reached = pyqtSignal()            # thread-safe auto-stop trigger
    _mic_error = pyqtSignal()              # mic issue detected during recording

    def __init__(self, config: Config):
        super().__init__()
        self.config = config

        resolved = None
        if config.mic_name:
            resolved = VoiceRecorder.resolve_device(config.mic_name, config.mic_index)
            if resolved != config.mic_index:
                config.mic_index = resolved
                config.save()

        self.recorder = VoiceRecorder(device_index=resolved, preferred_name=config.mic_name)
        self.recorder.prepare()
        self.asr = self._build_asr()
        self.injector = TextInjector()
        self.history = HistoryManager(config)
        self.polisher = self._build_polisher()
        self._state = "ready"
        self._worker: _TranscribeWorker | None = None
        self._polish_worker: _PolishWorker | None = None
        self._max_reached.connect(self._stop_recording)
        self._mic_error.connect(self._on_recording_mic_error)
        self._watchdog = QTimer(self)
        self._watchdog.setInterval(1000)
        self._watchdog.timeout.connect(self._check_recording_health)
        logger.info(f"{_TAG} Initialized (mode={config.mode}, "
                    f"asr={config.asr_model}/{config.asr_provider}, "
                    f"polish={config.polish_model}/{config.polish_provider})")

    @property
    def state(self) -> str:
        return self._state

    # ── backend factory ──

    def _build_asr(self) -> DashScopeASR | OpenAICompatASR:
        cfg = self.config
        if cfg.asr_provider == "openai_compat":
            return OpenAICompatASR(
                api_key=cfg.custom_asr_api_key,
                model=cfg.custom_asr_model,
                base_url=cfg.custom_asr_base_url,
            )
        return DashScopeASR(
            api_key=cfg.api_key,
            model=cfg.asr_model,
            base_url=cfg.api_base_url,
        )

    def _build_polisher(self) -> TextPolisher:
        cfg = self.config
        if cfg.polish_provider == "openai_compat":
            return TextPolisher(
                api_key=cfg.custom_polish_api_key,
                model=cfg.custom_polish_model,
                base_url=cfg.custom_polish_base_url,
                provider="openai_compat",
            )
        return TextPolisher(
            api_key=cfg.api_key,
            model=cfg.polish_model,
            base_url=cfg.api_base_url,
            provider="dashscope",
        )

    def reload_backends(self):
        """Rebuild ASR and Polisher from current config (e.g. after provider/key change)."""
        self.asr = self._build_asr()
        self.polisher = self._build_polisher()
        logger.info(f"{_TAG} Backends reloaded "
                    f"(asr={self.config.asr_provider}, polish={self.config.polish_provider})")

    def _set_state(self, s: str):
        self._state = s
        if s == "recording":
            self._watchdog.start()
        else:
            self._watchdog.stop()
        logger.debug(f"{_TAG} State → {s}")
        self.state_changed.emit(s)

    def toggle_record(self):
        if self._state == "ready":
            self._start_recording()
        elif self._state == "recording":
            self._stop_recording()

    def cancel(self):
        if self._state == "recording":
            logger.info(f"{_TAG} Recording cancelled by user")
            self.recorder.cancel()
            self._set_state("ready")

    def get_duration(self) -> float:
        return self.recorder.get_duration()

    # ── recording ──

    def _start_recording(self):
        logger.info(f"{_TAG} Start recording (mode={self.config.mode})")
        if self.recorder.no_device:
            logger.warning(f"{_TAG} No input device available")
            self.mic_unavailable.emit("未找到输入设备")
            return
        self._record_t0 = time.monotonic()
        try:
            self.recorder.start(
                on_audio_data=self._on_audio_chunk,
                on_max_reached=self._on_max_reached,
                on_mic_error=self._on_mic_error,
            )
        except Exception as e:
            logger.warning(f"{_TAG} Start failed ({e}), re-preparing...")
            try:
                self.recorder.prepare()
                self.recorder.start(
                    on_audio_data=self._on_audio_chunk,
                    on_max_reached=self._on_max_reached,
                    on_mic_error=self._on_mic_error,
                )
            except Exception as e2:
                logger.error(f"{_TAG} Failed to open microphone: {e2}")
                self.mic_unavailable.emit(f"无法打开麦克风: {e2}")
                self._set_state("ready")
                return

        self._set_state("recording")

    def _on_max_reached(self):
        self._max_reached.emit()

    def _on_mic_error(self):
        self._mic_error.emit()

    def _on_audio_chunk(self, data: bytes):
        self.audio_data.emit(data)

    def _on_recording_mic_error(self):
        """Handle mic error detected during recording (from audio callback thread)."""
        if self._state != "recording":
            return
        logger.error(f"{_TAG} Mic error during recording, auto-stopping")
        self.recorder.stop()
        self.mic_unavailable.emit("录音过程中发现麦克风异常，已自动停止")
        self._set_state("ready")

    def _check_recording_health(self):
        """Watchdog: detect stalled audio stream (e.g. Bluetooth disconnect)."""
        if self._state != "recording":
            return
        if self.recorder.is_stalled():
            logger.error(f"{_TAG} Audio stream stalled "
                         f"(no callback for >{self.recorder.STALL_TIMEOUT}s), "
                         f"device likely disconnected")
            self.recorder.stop()
            self.mic_unavailable.emit("麦克风似乎已断开连接，录音已自动停止")
            self._set_state("ready")

    def _stop_recording(self):
        if self._state != "recording":
            return
        wall_duration = time.monotonic() - self._record_t0
        pcm = self.recorder.stop()
        if not pcm:
            logger.warning(f"{_TAG} No audio captured after {wall_duration:.1f}s")
            self.error_occurred.emit("未录到音频，请重试")
            self._set_state("ready")
            return

        logger.info(f"{_TAG} Recording stopped — {wall_duration:.1f}s, "
                    f"PCM {len(pcm)} bytes")

        if self.recorder.is_silent():
            logger.info(f"{_TAG} Audio silent (peak={self.recorder.peak_amplitude}), "
                        f"skipping ASR")
            self.error_occurred.emit("未检测到语音")
            self._set_state("ready")
            return

        self._start_batch_transcribe(pcm, wall_duration)

    def _start_batch_transcribe(self, pcm: bytes, duration: float):
        self._set_state("processing")
        self._worker = _TranscribeWorker(self.asr, pcm, duration)
        self._worker.result_ready.connect(lambda t: self._finalize(t, pcm))
        self._worker.error_occurred.connect(self._on_transcribe_error)
        self._worker.finished.connect(self._cleanup_worker)
        self._worker.start()

    def _cleanup_worker(self):
        if self._worker:
            self._worker.deleteLater()
            self._worker = None

    def _cleanup_polish_worker(self):
        if self._polish_worker:
            self._polish_worker.deleteLater()
            self._polish_worker = None

    def _finalize(self, text: str, pcm: bytes):
        if not text:
            logger.warning(f"{_TAG} Empty transcription result")
            self.error_occurred.emit("识别结果为空")
            self._set_state("ready")
            return

        if self.config.mode == "polish":
            logger.info(f"{_TAG} Entering polish pipeline")
            self._set_state("processing")
            self.live_text.emit(f"[原文] {text}")
            self._polish_worker = _PolishWorker(self.polisher, text, self.config)
            self._polish_worker.result_ready.connect(lambda polished: self._inject_and_save(polished, pcm))
            self._polish_worker.polish_failed.connect(self.error_occurred)
            self._polish_worker.finished.connect(self._cleanup_polish_worker)
            self._polish_worker.start()
        else:
            self._inject_and_save(text, pcm)

    def _inject_and_save(self, text: str, pcm: bytes):
        if self.config.paste_result:
            self.injector.inject(text, restore_clipboard=self.config.restore_clipboard)
            logger.info(f"{_TAG} Pasted {len(text)} chars")
        else:
            self.injector.copy_only(text)
            logger.info(f"{_TAG} Copied {len(text)} chars to clipboard")

        duration = len(pcm) / (16000 * 2)
        self.history.save_entry(
            text=text,
            duration=duration,
            mode=self.config.mode,
            audio_data=pcm if self.config.save_audio else None,
        )
        self.transcription_done.emit(text)
        self._set_state("ready")

    def _on_transcribe_error(self, msg: str):
        logger.error(f"{_TAG} Transcription error: {msg}")
        self.error_occurred.emit(msg)
        self._set_state("ready")
