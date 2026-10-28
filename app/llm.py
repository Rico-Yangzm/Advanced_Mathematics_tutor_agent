import json
import urllib.error
import urllib.request
from typing import Any

from app.config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL


class LLMConfigurationError(RuntimeError):
    pass


class LLMRequestError(RuntimeError):
    pass


def chat(system_prompt: str, user_prompt: str, temperature: float = 0.3) -> str:
    if not LLM_BASE_URL or not LLM_API_KEY or not LLM_MODEL:
        raise LLMConfigurationError(
            "尚未配置模型服务。请设置 LLM_BASE_URL、LLM_API_KEY 和 LLM_MODEL。"
        )
    payload: dict[str, Any] = {
        "model": LLM_MODEL,
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    request = urllib.request.Request(
        f"{LLM_BASE_URL}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {LLM_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise LLMRequestError(f"模型服务请求失败：{error}") from error
    try:
        content = result["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as error:
        raise LLMRequestError("模型服务返回格式无效，未找到 choices[0].message.content。") from error
    if not isinstance(content, str) or not content.strip():
        raise LLMRequestError("模型服务返回了空内容。")
    return content.strip()
