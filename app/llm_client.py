# -*- coding: utf-8 -*-
"""OpenAI-compatible chat client.

The application no longer launches a local inference runtime.  Every AI call is
sent to a user-configured OpenAI-compatible endpoint, for example:

    https://api.openai.com/v1
    https://api.deepseek.com/v1
    http://127.0.0.1:11434/v1     # Ollama / LM Studio / vLLM

Configuration is kept in ``app/data/settings.json`` and is only read on this
machine.  The API key is never returned by the HTTP API; the settings UI only
receives a masked hint.
"""
from __future__ import annotations

import json
import re
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Optional

REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high")
_DEFAULT_TIMEOUT = 600
_MAX_ERROR_BODY = 500
_CORE_KEYS = ("model", "messages", "stream")


class LLMError(RuntimeError):
    """Base class for user-facing AI configuration / request errors."""


class LLMConfigError(LLMError):
    """The endpoint is missing or the saved settings are invalid."""


class LLMRequestError(LLMError):
    """The remote endpoint answered with an error or unusable payload."""

    def __init__(self, message: str, status: Optional[int] = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body or ""


class LLMConnectionError(LLMError):
    """The endpoint could not be reached."""


def _settings() -> dict:
    # Imported lazily so this module stays independent from FastAPI / server.
    import store

    return store.read_settings()


def normalize_base_url(raw: Any) -> str:
    """Accept a root URL or a pasted chat-completions URL.

    A trailing ``/v1`` is preserved.  ``/chat/completions`` and ``/models`` are
    removed because the client appends them itself.
    """
    url = str(raw or "").strip()
    if not url:
        return ""
    if not re.match(r"^https?://", url, re.IGNORECASE):
        raise LLMConfigError("接口地址必须以 http:// 或 https:// 开头")
    url = url.rstrip("/")
    low = url.lower()
    for suffix in ("/chat/completions", "/models"):
        if low.endswith(suffix):
            url = url[: -len(suffix)].rstrip("/")
            break
    return url


def _base_url(cfg: dict) -> str:
    return normalize_base_url(cfg.get("llm_base_url", ""))


def _api_key(cfg: dict) -> str:
    return str(cfg.get("llm_api_key") or "").strip()


def _model(cfg: dict) -> str:
    return str(cfg.get("llm_model") or "").strip()


def _timeout(cfg: dict) -> float:
    try:
        value = float(cfg.get("llm_timeout") or _DEFAULT_TIMEOUT)
    except (TypeError, ValueError):
        value = _DEFAULT_TIMEOUT
    return max(5.0, min(3600.0, value))


def _temperature(cfg: dict):
    raw = cfg.get("llm_temperature")
    if raw is None or str(raw).strip() == "":
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise LLMConfigError("温度必须是数字，或留空使用服务端默认值")
    if not 0 <= value <= 2:
        raise LLMConfigError("温度必须在 0 到 2 之间")
    return value


def _extra_body(cfg: dict) -> dict:
    raw = cfg.get("llm_extra_body")
    if raw is None or str(raw).strip() == "":
        return {}
    if isinstance(raw, dict):
        obj = dict(raw)
    else:
        try:
            obj = json.loads(str(raw))
        except Exception as exc:
            raise LLMConfigError("额外参数不是合法 JSON：%s" % exc)
    if not isinstance(obj, dict):
        raise LLMConfigError("额外参数必须是 JSON 对象（{...}）")
    for key in _CORE_KEYS:
        if key in obj:
            raise LLMConfigError("额外参数不能覆盖 %s，请删除该项" % key)
    return obj


def ensure_configured(cfg: Optional[dict] = None) -> dict:
    cfg = dict(cfg or _settings())
    base = _base_url(cfg)
    if not base:
        raise LLMConfigError("尚未配置 AI 接口：请打开「AI 设置」，填写 OpenAI 兼容接口地址")
    if not _model(cfg):
        raise LLMConfigError("尚未选择模型：请打开「AI 设置」，获取模型列表或手动填写模型名")
    return cfg


def _key_hint(key: str) -> str:
    if not key:
        return ""
    if len(key) < 8:
        return "已保存 · ••••"
    return "已保存 · ••••%s" % key[-4:]


def public_config() -> dict:
    cfg = _settings()
    base = str(cfg.get("llm_base_url") or "").strip()
    model = _model(cfg)
    key = _api_key(cfg)
    return {
        "provider": "openai-compatible",
        "base_url": base,
        "model": model,
        "reasoning_effort": str(cfg.get("llm_reasoning_effort") or ""),
        "temperature": cfg.get("llm_temperature") or "",
        "timeout": cfg.get("llm_timeout") or _DEFAULT_TIMEOUT,
        "extra_body": cfg.get("llm_extra_body") or "",
        "has_api_key": bool(key),
        "api_key_hint": _key_hint(key),
        "configured": bool(base and model),
    }


def save_config(payload: dict) -> dict:
    """Persist settings.  A blank api_key keeps the old key.

    Set ``clear_api_key`` to true to remove the stored secret explicitly.
    """
    payload = payload or {}
    current = _settings()
    updates: dict = {}

    if "base_url" in payload:
        updates["llm_base_url"] = normalize_base_url(payload.get("base_url"))
    if "model" in payload:
        updates["llm_model"] = str(payload.get("model") or "").strip()
    if "reasoning_effort" in payload:
        effort = str(payload.get("reasoning_effort") or "").strip().lower()
        if effort not in ("",) + REASONING_EFFORTS:
            raise LLMConfigError("思考强度必须是：不设置、none、minimal、low、medium 或 high")
        updates["llm_reasoning_effort"] = effort
    if "temperature" in payload:
        raw = payload.get("temperature")
        if raw is None or str(raw).strip() == "":
            updates["llm_temperature"] = ""
        else:
            try:
                value = float(raw)
            except (TypeError, ValueError):
                raise LLMConfigError("温度必须是数字，或留空")
            if not 0 <= value <= 2:
                raise LLMConfigError("温度必须在 0 到 2 之间")
            updates["llm_temperature"] = value
    if "timeout" in payload:
        try:
            timeout = int(payload.get("timeout"))
        except (TypeError, ValueError):
            raise LLMConfigError("超时时间必须是整数秒")
        if not 5 <= timeout <= 3600:
            raise LLMConfigError("超时时间必须在 5 到 3600 秒之间")
        updates["llm_timeout"] = timeout
    if "extra_body" in payload:
        raw = payload.get("extra_body")
        obj = _extra_body({"llm_extra_body": raw})
        updates["llm_extra_body"] = (
            json.dumps(obj, ensure_ascii=False, indent=2) if obj else ""
        )

    if payload.get("clear_api_key"):
        updates["llm_api_key"] = ""
    elif "api_key" in payload and str(payload.get("api_key") or "").strip():
        updates["llm_api_key"] = str(payload.get("api_key")).strip()

    import store

    store.save_settings(updates)
    # Validate the resulting combination early, but allow an incomplete draft.
    saved = store.read_settings()
    if _base_url(saved) and not _model(saved):
        pass
    return public_config()


def _effective_config(payload: Optional[dict] = None) -> dict:
    """Saved settings plus optional unsaved form overrides (for test/models)."""
    cfg = dict(_settings())
    payload = payload or {}
    if "base_url" in payload and str(payload.get("base_url") or "").strip():
        cfg["llm_base_url"] = normalize_base_url(payload.get("base_url"))
    if "model" in payload and str(payload.get("model") or "").strip():
        cfg["llm_model"] = str(payload.get("model")).strip()
    if payload.get("clear_api_key"):
        cfg["llm_api_key"] = ""
    elif "api_key" in payload and str(payload.get("api_key") or "").strip():
        cfg["llm_api_key"] = str(payload.get("api_key")).strip()
    if "reasoning_effort" in payload:
        cfg["llm_reasoning_effort"] = str(payload.get("reasoning_effort") or "").strip().lower()
    if "temperature" in payload:
        cfg["llm_temperature"] = payload.get("temperature")
    if "timeout" in payload:
        cfg["llm_timeout"] = payload.get("timeout")
    if "extra_body" in payload:
        cfg["llm_extra_body"] = payload.get("extra_body")
    return cfg


def _headers(key: str) -> dict:
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "application/json",
        "User-Agent": "GuWen/1.0 (OpenAI-compatible client)",
    }
    if key:
        headers["Authorization"] = "Bearer " + key
    return headers


def _error_message(raw: Any) -> str:
    if isinstance(raw, dict):
        err = raw.get("error")
        if isinstance(err, dict):
            msg = err.get("message") or err.get("detail") or err.get("type")
            if msg:
                return str(msg)
        if isinstance(err, str):
            return err
        for key in ("message", "detail", "msg"):
            if raw.get(key):
                return str(raw.get(key))
    if isinstance(raw, list):
        return "; ".join(_error_message(x) for x in raw[:3])
    return ""


def _read_json(response) -> Any:
    body = response.read()
    if not body:
        raise LLMRequestError("接口返回了空响应")
    try:
        return json.loads(body.decode("utf-8"))
    except Exception as exc:
        excerpt = body[:300].decode("utf-8", "replace")
        raise LLMRequestError("接口返回的不是 JSON：%s" % excerpt, body=excerpt)


def _request_json(
    method: str,
    url: str,
    key: str,
    body: Optional[dict],
    timeout: float,
) -> Any:
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method=method, headers=_headers(key)
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return _read_json(response)
    except urllib.error.HTTPError as exc:
        try:
            raw_body = exc.read(_MAX_ERROR_BODY).decode("utf-8", "replace")
        except Exception:
            raw_body = ""
        detail = ""
        try:
            detail = _error_message(json.loads(raw_body))
        except Exception:
            detail = raw_body.strip()
        message = "HTTP %s：%s" % (exc.code, detail or exc.reason)
        raise LLMRequestError(message, status=exc.code, body=raw_body)
    except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
        reason = getattr(exc, "reason", exc)
        raise LLMConnectionError(
            "连接 AI 接口失败：%s（请检查地址、网络和代理）" % reason
        ) from exc
    except LLMRequestError:
        raise
    except Exception as exc:
        raise LLMConnectionError("请求 AI 接口失败：%s" % exc) from exc


def _chat_url(base: str) -> str:
    return base.rstrip("/") + "/chat/completions"


def _models_url(base: str) -> str:
    return base.rstrip("/") + "/models"


def _content_from_response(obj: Any) -> str:
    if not isinstance(obj, dict):
        raise LLMRequestError("接口响应不是 JSON 对象")
    choices = obj.get("choices")
    if not isinstance(choices, list) or not choices:
        # Some error-shaped 200 responses carry an error object.
        detail = _error_message(obj)
        raise LLMRequestError(detail or "接口响应缺少 choices")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") in (None, "text"):
                parts.append(str(item.get("text") or ""))
        content = "".join(parts)
    if content is None or str(content) == "":
        finish = choices[0].get("finish_reason") if isinstance(choices[0], dict) else ""
        raise LLMRequestError(
            "模型没有返回正文（finish_reason=%s）。如果是思考模型，请重试或检查接口参数"
            % (finish or "unknown")
        )
    return str(content)


def _post_chat(base: str, key: str, body: dict, timeout: float) -> dict:
    return _request_json("POST", _chat_url(base), key, body, timeout)


def _retryable(exc: LLMRequestError) -> bool:
    return exc.status is None or exc.status == 429 or (exc.status or 0) >= 500


def chat(
    messages: list,
    max_tokens: Optional[int] = None,
    timeout: Optional[float] = None,
    stop: Optional[list] = None,
    cfg: Optional[dict] = None,
) -> str:
    """Send one chat completion request and return the assistant text."""
    cfg = ensure_configured(cfg)
    base = _base_url(cfg)
    key = _api_key(cfg)
    model = _model(cfg)
    timeout = float(timeout or _timeout(cfg))

    body: dict = dict(_extra_body(cfg))
    # Core arguments always win over the optional provider-specific JSON.
    body["model"] = model
    body["messages"] = messages
    body["stream"] = False
    if max_tokens:
        body["max_tokens"] = int(max_tokens)
    temperature = _temperature(cfg)
    if temperature is not None:
        body["temperature"] = temperature
    effort = str(cfg.get("llm_reasoning_effort") or "").strip()
    if effort:
        body["reasoning_effort"] = effort
    if stop:
        body["stop"] = stop

    last_error = None
    for attempt in range(3):
        try:
            obj = _post_chat(base, key, body, timeout)
            return _content_from_response(obj)
        except LLMRequestError as exc:
            last_error = exc
            # A few OpenAI-style providers require max_completion_tokens
            # instead of the legacy max_tokens field.
            if exc.status == 400 and "max_tokens" in body and "max_tokens" in str(exc).lower():
                if "max_completion_tokens" in str(exc).lower() or "unsupported" in str(exc).lower():
                    body["max_completion_tokens"] = body.pop("max_tokens")
                    continue
            # Some reasoning models reject a custom temperature.
            if exc.status == 400 and "temperature" in body and "temperature" in str(exc).lower():
                body.pop("temperature", None)
                continue
            # Make the reasoning-effort failure actionable instead of silently
            # ignoring the user's choice.
            if exc.status == 400 and "reasoning_effort" in body and "reasoning" in str(exc).lower():
                raise LLMRequestError(
                    "当前模型/接口不支持 reasoning_effort。请在「AI 设置」把思考强度改为“不设置”，"
                    "或用额外参数 JSON 填写该供应商自己的思考开关。原始错误：%s" % exc,
                    status=exc.status,
                    body=exc.body,
                ) from exc
            if _retryable(exc) and attempt < 2:
                time.sleep(1.0 * (attempt + 1))
                continue
            raise
        except LLMConnectionError:
            if attempt < 2:
                time.sleep(1.0 * (attempt + 1))
                continue
            raise
    raise last_error or LLMRequestError("请求失败")


def list_models(payload: Optional[dict] = None) -> list:
    cfg = _effective_config(payload)
    base = _base_url(cfg)
    if not base:
        raise LLMConfigError("请先填写接口地址")
    obj = _request_json("GET", _models_url(base), _api_key(cfg), None, 30.0)
    if isinstance(obj, list):
        rows = obj
    elif isinstance(obj, dict):
        rows = obj.get("data") if isinstance(obj.get("data"), list) else obj.get("models")
    else:
        rows = None
    if not isinstance(rows, list):
        raise LLMRequestError("接口没有返回模型列表（data/models）")
    models = []
    for row in rows:
        if isinstance(row, str):
            name = row
        elif isinstance(row, dict):
            name = row.get("id") or row.get("name") or row.get("model")
        else:
            name = None
        if name and str(name) not in models:
            models.append(str(name))
    models.sort(key=lambda x: x.lower())
    if not models:
        raise LLMRequestError("接口返回了空模型列表，可以直接手动填写模型名")
    return models


def test_connection(payload: Optional[dict] = None) -> dict:
    cfg = _effective_config(payload)
    if not _model(cfg):
        raise LLMConfigError("请先选择或填写模型名")
    started = time.time()
    reply = chat(
        [
            {"role": "system", "content": "你是连接测试助手，只回复两个字符：OK"},
            {"role": "user", "content": "ping"},
        ],
        max_tokens=16,
        timeout=min(45.0, _timeout(cfg)),
        cfg=cfg,
    )
    elapsed = int((time.time() - started) * 1000)
    return {
        "ok": True,
        "model": _model(cfg),
        "reply": (reply or "").strip()[:120],
        "latency_ms": elapsed,
    }
