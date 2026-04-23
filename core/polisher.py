"""Text polisher — refines raw ASR output via OpenAI-compatible API."""
import re

from openai import OpenAI

from core.log import logger

_TAG = "[Polisher]"

DEFAULT_INSTRUCTIONS = "去口语化，但不增删内容，保持原有的语句顺序。"

_TASK_PREAMBLE="将给你的语音识别原始文本按照要求润色。"

_OUTPUT_FORMAT = (
    "【输出格式】：用户会用 ```text 代码块包裹需要润色的内容。"
    "你也必须用 ```text 代码块包裹润色结果输出。"
    "如果代码块内容为空，则什么都不输出。"
    "任何时候不得违反【输出格式】要求。"
)


def _build_system_prompt(custom_instructions: str) -> str:
    custom = (custom_instructions or "").strip()
    if custom:
        return _TASK_PREAMBLE + "要求：" + custom + _OUTPUT_FORMAT
    return _TASK_PREAMBLE + "要求：" + DEFAULT_INSTRUCTIONS + _OUTPUT_FORMAT


def _extract_from_codeblock(text: str) -> str:
    """从 markdown 代码块中提取内容，解析失败则返回原文。"""
    match = re.search(r"```(?:\w*)\n(.*?)```", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    return text.strip()


def _to_compatible_url(base_url: str) -> str:
    """Convert DashScope API URL to its compatible-mode variant.
    Only transforms aliyun URLs; passes all other URLs through unchanged."""
    base_url = base_url.rstrip("/")
    if "dashscope.aliyuncs.com" in base_url and "/compatible-mode" not in base_url:
        base_url = base_url.rsplit("/api/", 1)[0] + "/compatible-mode/v1"
    return base_url


class TextPolisher:
    def __init__(self, api_key: str, model: str = "qwen3.5-flash",
                 base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1",
                 provider: str = "dashscope"):
        self._model = model
        self._provider = provider
        if provider == "dashscope":
            self._base_url = _to_compatible_url(base_url)
        else:
            self._base_url = base_url.rstrip("/")
        self._client = OpenAI(api_key=api_key, base_url=self._base_url)
        logger.info(f"{_TAG} Initialized (model={model}, provider={provider}, url={self._base_url})")

    def update_api_key(self, api_key: str):
        self._client = OpenAI(api_key=api_key, base_url=self._base_url)
        logger.info(f"{_TAG} API key updated")

    def set_model(self, model: str):
        old = self._model
        self._model = model
        logger.info(f"{_TAG} Model changed: {old} → {model}")

    def polish(self, raw_text: str, extra_instructions: str = "") -> tuple[bool, str]:
        """Returns (api_ok, text). api_ok is False only when the request raised."""
        if not raw_text.strip():
            return True, raw_text
        try:
            system_content = _build_system_prompt(extra_instructions)
            user_content = f"```text\n{raw_text}\n```"
            resp = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": system_content},
                    {"role": "user", "content": user_content},
                ],
                **({
                    "extra_body": {"enable_thinking": False},
                } if self._provider == "dashscope" else {}),
                timeout=15,
            )
            content = resp.choices[0].message.content
            raw_result = content.strip() if isinstance(content, str) else str(content).strip()
            result = _extract_from_codeblock(raw_result)
            logger.info(f"{_TAG} Result: {result[:80]}{'…' if len(result) > 80 else ''}")
            return True, (result or raw_text)
        except Exception as e:
            logger.error(f"{_TAG} Failed: {e}")
            return False, raw_text
