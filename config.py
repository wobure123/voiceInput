import json
import os
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path


def _config_dir() -> Path:
    return Path(os.environ.get("USERPROFILE", os.path.expanduser("~"))) / ".voiceinput"


def _config_path() -> Path:
    return _config_dir() / "config.json"


@dataclass
class Config:
    hotkey: str = "lctrl+lshift+r"
    trigger_mode: str = "toggle"
    mode: str = "transcribe"
    custom_prompts: list = field(default_factory=list)
    active_prompt_id: str = ""
    prompts_initialized: bool = False
    language: str = "auto"

    api_key: str = ""
    api_base_url: str = "https://dashscope.aliyuncs.com/api/v1"
    asr_model: str = "qwen3-asr-flash"
    polish_model: str = "qwen3.5-flash"

    # OpenAI Compatible 自定义端点
    asr_provider: str = "dashscope"       # "dashscope" | "openai_compat"
    polish_provider: str = "dashscope"    # "dashscope" | "openai_compat"
    custom_asr_api_key: str = ""
    custom_asr_base_url: str = "https://api.openai.com/v1"
    custom_asr_model: str = "whisper-1"
    custom_polish_api_key: str = ""
    custom_polish_base_url: str = "https://api.openai.com/v1"
    custom_polish_model: str = "gpt-4o-mini"

    mic_index: int | None = None
    mic_name: str = ""

    paste_result: bool = True
    restore_clipboard: bool = False
    simulate_keypresses: bool = False
    tray_click_to_record: bool = True

    play_sounds: bool = True
    save_history: bool = True
    save_audio: bool = False
    hide_mini_window_when_idle: bool = False
    show_result_text: bool = False
    autostart_enabled: bool = False

    mini_window_x: int | None = None

    @property
    def active_prompt_text(self) -> str:
        if not self.active_prompt_id or not self.custom_prompts:
            return ""
        for p in self.custom_prompts:
            if p.get("id") == self.active_prompt_id:
                return p.get("content", "")
        return ""

    @classmethod
    def load(cls) -> "Config":
        path = _config_path()
        raw_data: dict = {}
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    raw_data = json.load(f)
                known = {fld.name for fld in cls.__dataclass_fields__.values()}
                filtered = {k: v for k, v in raw_data.items() if k in known}
                cfg = cls(**filtered)
            except Exception:
                cfg = cls()
        else:
            cfg = cls()

        old_text = raw_data.get("custom_prompt", "").strip()
        if old_text and not cfg.custom_prompts:
            pid = uuid.uuid4().hex[:8]
            cfg.custom_prompts = [{"id": pid, "name": "自定义提示词", "content": old_text}]
            cfg.active_prompt_id = pid
            cfg.prompts_initialized = True

        if cfg.custom_prompts:
            cfg.prompts_initialized = True
        elif not cfg.prompts_initialized:
            from core.prompt_templates import seed_default_prompt_templates

            seed_default_prompt_templates(cfg)
            cfg.prompts_initialized = True

        if not cfg.api_key:
            cfg.api_key = os.environ.get("DASHSCOPE_API_KEY", "")

        _VALID_KEYS = set("abcdefghijklmnopqrstuvwxyz")
        _VALID_KEYS |= {str(i) for i in range(10)}
        _VALID_KEYS |= {f"f{i}" for i in range(1, 25)}
        _VALID_KEYS |= {
            "lctrl", "rctrl", "lshift", "rshift", "lalt", "ralt",
            "space", "enter", "tab", "escape", "backspace", "delete",
            "insert", "home", "end", "pageup", "pagedown",
            "up", "down", "left", "right",
            "capslock", "numlock", "scrolllock", "printscreen", "pause",
            ";", "=", ",", "-", ".", "/", "`", "[", "\\", "]", "'",
        }
        parts = [p.strip().lower() for p in cfg.hotkey.split("+")]
        if not parts or not all(p in _VALID_KEYS for p in parts):
            cfg.hotkey = cls.hotkey

        cfg.save()
        return cfg

    def save(self):
        path = _config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, indent=2, ensure_ascii=False)

    @staticmethod
    def history_dir() -> Path:
        d = _config_dir() / "history"
        d.mkdir(parents=True, exist_ok=True)
        return d
