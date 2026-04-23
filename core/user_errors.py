"""
User-visible error taxonomy for VoiceInput.

All string matching for presentation routing lives here so UI code does not
accumulate ad-hoc ``if "401" in msg`` branches. Engine emits plain messages;
this module classifies them for notification policy.

Mic/device errors use ``mic_unavailable`` (not ``error_occurred``) and bypass
this classification entirely.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto


class UserErrorDomain(Enum):
    API_CREDENTIALS = auto()
    CAPTURE = auto()
    SPEECH_EMPTY = auto()
    SPEECH_SILENT = auto()
    GENERAL = auto()


@dataclass(frozen=True)
class UserErrorContext:
    domain: UserErrorDomain
    message: str


def classify_user_error(message: str) -> UserErrorContext:
    """Map engine ``error_occurred`` text to a domain."""
    m = (message or "").strip()
    if not m:
        return UserErrorContext(UserErrorDomain.GENERAL, m)

    if "API 401:" in m or "API 403:" in m:
        return UserErrorContext(UserErrorDomain.API_CREDENTIALS, m)

    if m.startswith("未录到音频"):
        return UserErrorContext(UserErrorDomain.CAPTURE, m)
    if m == "识别结果为空":
        return UserErrorContext(UserErrorDomain.SPEECH_EMPTY, m)
    if m == "未检测到语音":
        return UserErrorContext(UserErrorDomain.SPEECH_SILENT, m)

    return UserErrorContext(UserErrorDomain.GENERAL, m)


def single_line_preview(text: str, max_len: int = 320) -> str:
    line = text.replace("\n", " ").strip()
    if len(line) > max_len:
        return line[: max_len - 1] + "…"
    return line
