"""
工具层公共辅助：
- 每线程复用一个LLM客户端，并行阶段不共享可变调用状态
- 带自动重试的"LLM返回JSON"问答：
  · 输出被截断（finish_reason=length）→ 扩大token上限并要求压缩表述后重试
  · 格式错误 → 附加纠错提示重试
  · 重试仍失败返回None（工具据此返回success=False，Agent可感知并重试）
"""
import hashlib
import os
import threading
import time
from contextlib import contextmanager

import config
from llm_client import LLMClient
from pydantic import ValidationError
from runtime_context import current_settings, monotonic_deadline
from trace_catalog import emit_trace
from utils import parse_json_safely

_client = None
_thread_clients = threading.local()
_thread_runtime = threading.local()

_JSON_RETRY_SUFFIX = (
    "\n\n注意：你上一次的输出不是合法JSON。"
    "请重新输出，只输出一个合法的JSON对象，不要包含任何解释文字或代码块标记。"
)

_TRUNCATED_RETRY_SUFFIX = (
    "\n\n注意：你上一次的输出因超长被截断了。"
    "请压缩表述（数组各项更精炼、去掉冗余修饰），确保完整输出一个合法的JSON对象。"
)

_SCHEMA_RETRY_SUFFIX = (
    "\n\n注意：你上一次输出的JSON字段或类型不符合要求。"
    "请严格按照原始字段说明重新输出，只输出一个合法JSON对象，不要添加未知字段。"
)


def _validation_errors(error):
    """Return privacy-safe error codes and field paths only."""
    details = []
    for item in error.errors(
        include_url=False,
        include_context=False,
        include_input=False,
    ):
        path = list(item.get("loc") or ())
        category = str(item.get("type") or "validation_error")
        if category == "extra_forbidden" and path:
            path[-1] = "<extra>"
        details.append({
            "category": category,
            "path": ".".join(str(part) for part in path),
        })
    return details


def _api_key_fingerprint(api_key):
    """Return a stable cache discriminator without retaining the raw key."""
    normalized = "" if api_key is None else str(api_key).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _mock_cache_key(mock):
    if mock is None:
        return ("environment", os.environ.get("AGENT_MOCK", "") == "1")
    return ("explicit", mock)


def get_client():
    """获取当前线程的客户端；显式_client注入保留给现有离线测试。"""
    if _client is not None:
        return _client
    settings = current_settings()
    mock_key = _mock_cache_key(settings.mock)
    effective_mock = mock_key[1]
    if settings.api_key is not None and not effective_mock:
        # BYOK clients must remain run-scoped; a thread cache would retain raw
        # user credentials for the lifetime of a warm serverless worker.
        return LLMClient(
            model=settings.model,
            reasoning=settings.reasoning,
            api_key=settings.api_key,
            mock=settings.mock,
        )
    effective_api_key = None if effective_mock else config.API_KEY
    key = (
        settings.model,
        settings.reasoning,
        mock_key,
        _api_key_fingerprint(effective_api_key),
    )
    clients = getattr(_thread_clients, "clients", None)
    if clients is None:
        clients = {}
        _thread_clients.clients = clients
    client = clients.get(key)
    if client is None:
        client = LLMClient(
            model=settings.model,
            reasoning=settings.reasoning,
            api_key=None,
            mock=settings.mock,
        )
        clients[key] = client
    return client


def current_run_deadline():
    """Return the harness deadline bound to the current tool worker, if any."""
    return getattr(_thread_runtime, "run_deadline", None)


@contextmanager
def use_run_deadline(deadline):
    """Temporarily bind an Agent wall-clock deadline to this worker thread."""
    had_previous = hasattr(_thread_runtime, "run_deadline")
    previous = getattr(_thread_runtime, "run_deadline", None)
    if deadline is None:
        try:
            del _thread_runtime.run_deadline
        except AttributeError:
            pass
    else:
        _thread_runtime.run_deadline = float(deadline)
    try:
        yield
    finally:
        if had_previous:
            _thread_runtime.run_deadline = previous
        else:
            try:
                del _thread_runtime.run_deadline
            except AttributeError:
                pass


def ask_json(prompt, system, default, temperature=0.2, label=None, max_tokens=None,
             retry_max_tokens=None, validator=None):
    """调用LLM并解析JSON返回，失败自动重试一轮（区分截断和格式错误两种失败）
    成功时用default补齐缺失字段，保证下游字段访问安全
    """
    logical_deadline = time.monotonic() + max(0.0, float(config.CALL_DEADLINE))
    run_deadline = current_run_deadline()
    if run_deadline is None and current_settings().deadline_epoch is not None:
        run_deadline = monotonic_deadline(limit=float("inf"))
    if label:
        print(f"   ⏳ {label}...")
    client = get_client()
    current_prompt = prompt
    current_max = max_tokens
    operation = f"tool.{label or 'ask_json'}"
    last_reason = "invalid_json"
    last_validation_errors = []
    for attempt in (1, 2):
        content = client.simple_ask(
            prompt=current_prompt, system=system,
            temperature=temperature, max_tokens=current_max,
            operation=operation,
            logical_deadline=logical_deadline,
            external_deadline=run_deadline,
        )
        data = parse_json_safely(content, default={})
        if isinstance(data, dict) and data:
            if validator is not None:
                try:
                    validated = validator.model_validate(data, strict=True)
                except ValidationError as error:
                    last_reason = "schema_validation"
                    last_validation_errors = _validation_errors(error)
                else:
                    return validated.model_dump(mode="python")
            else:
                for key, value in default.items():
                    data.setdefault(key, value)
                return data
        else:
            last_reason = "invalid_json"
            last_validation_errors = []
        if attempt == 1:
            if client.last_finish_reason == "length":
                # 截断导致的失败：扩大输出上限 + 要求压缩表述
                expanded_max = max(
                    config.REPORT_MAX_TOKENS,
                    (current_max or config.MAX_TOKENS) * 2,
                )
                current_max = (
                    min(expanded_max, max(1, int(retry_max_tokens)))
                    if retry_max_tokens is not None
                    else expanded_max
                )
                current_prompt = prompt + _TRUNCATED_RETRY_SUFFIX
                reason = "truncated"
                validation_errors = []
                print("   ⚠️ LLM输出超长被截断，扩大输出上限后自动重试...")
            elif last_reason == "schema_validation":
                current_prompt = prompt + _SCHEMA_RETRY_SUFFIX
                reason = last_reason
                validation_errors = last_validation_errors
                print("   ⚠️ LLM返回JSON不符合字段契约，自动重试一轮...")
            else:
                current_prompt = prompt + _JSON_RETRY_SUFFIX
                reason = "invalid_json"
                validation_errors = []
                print("   ⚠️ LLM返回内容不是合法JSON，自动重试一轮...")
            trace_data = {
                "operation": operation,
                "attempt": attempt,
                "reason": reason,
                "next_max_tokens": current_max or config.MAX_TOKENS,
            }
            if validation_errors:
                trace_data["validation_errors"] = validation_errors
            emit_trace(
                "llm.semantic_json.retry",
                level="warning",
                data=trace_data,
            )
    failure_data = {
        "operation": operation,
        "attempts": 2,
        "reason": last_reason,
    }
    if last_validation_errors:
        failure_data["validation_errors"] = last_validation_errors
    emit_trace(
        "llm.semantic_json.failure",
        level="error",
        data=failure_data,
    )
    return None
