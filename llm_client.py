"""
LLM客户端封装：所有与大模型API的通信都通过这个模块
包含：
- 指数退避自动重试（应对网关过载、Cloudflare 504等临时故障）
- 流式传输（长输出经代理网关时避免首字节超时；端点不支持时自动回退非流式）
- Mock离线模式（没有API密钥时可完整演示全流程）
"""
import os
import re
import time
import uuid
from openai import OpenAI
import config
from runtime_context import current_settings
from trace_catalog import emit_trace


class CallDeadlineExceeded(TimeoutError):
    """一次逻辑LLM调用超出总时间预算。"""


class ExternalRunDeadlineExceeded(CallDeadlineExceeded):
    """The owning Agent run deadline expired during an LLM call."""

    is_run_deadline = True


_STREAM_UNSUPPORTED_PATTERNS = (
    re.compile(
        r"\b(?:stream|streaming)\b.{0,60}\b(?:not supported|unsupported|unavailable)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:unsupported|unknown|unrecognized|invalid)\b.{0,60}"
        r"\b(?:stream|streaming)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bdoes not support\b.{0,60}\b(?:stream|streaming)\b",
        re.IGNORECASE,
    ),
)


def _is_streaming_unsupported(error):
    """仅识别明确的流式参数/协议不支持，不把普通上游错误当成fallback。"""
    body = getattr(error, "body", None)
    if isinstance(body, dict):
        parameter = str(body.get("param") or body.get("parameter") or "").lower()
        code = str(body.get("code") or body.get("type") or "").lower()
        if parameter in ("stream", "streaming") and any(
            marker in code for marker in ("unsupported", "unknown", "invalid")
        ):
            return True
    text = str(error)
    return any(pattern.search(text) for pattern in _STREAM_UNSUPPORTED_PATTERNS)


class LLMClient:
    """封装OpenAI兼容API的调用"""

    def __init__(self, model=None, reasoning=None):
        active = current_settings()
        selected_model = active.model if model is None else model
        selected_reasoning = active.reasoning if reasoning is None else reasoning
        self.model, self.reasoning = config.validate_model_reasoning(
            selected_model,
            selected_reasoning,
        )
        # 最近一次调用的结束原因："length"表示输出被max_tokens截断（调用方可据此扩容重试）
        self.last_finish_reason = None
        # 只暴露运行指标，不保存prompt或模型原文。
        self.last_call_metrics = {}
        self.mock_mode = os.environ.get("AGENT_MOCK", "") == "1"
        self.streaming = config.STREAMING
        if self.mock_mode:
            from mock_data import reset_mock_counters
            self._mock_step = 0
            reset_mock_counters()
            print("🧪 [Mock模式] 当前使用离线演示数据，不会真正调用API")
            return

        if not config.API_KEY:
            raise ValueError(
                "未检测到API密钥！请先在终端执行：\n"
                "export ZENMUX_API_KEY=你的密钥\n"
                "（ZENMUX_API_KEY优先生效；也兼容OPENAI_API_KEY。"
                "不联网体验演示模式：AGENT_MOCK=1 python agent.py --demo）"
            )
        self.client = OpenAI(
            base_url=config.API_BASE_URL,
            api_key=config.API_KEY,
            default_headers={"User-Agent": "curl/8.7.1"},
            timeout=min(config.REQUEST_TIMEOUT, config.CALL_DEADLINE),
            max_retries=0,
        )

    def chat(self, messages, tools=None, temperature=0.3, max_tokens=None,
             operation="chat", parent_span=None, step=None,
             external_deadline=None, logical_deadline=None):
        """
        调用LLM进行一次对话（带指数退避重试）
        参数:
            messages: 对话历史列表（OpenAI格式）
            tools: 可选，工具定义列表（用于Function Calling）
            temperature: 随机性，越低越稳定
            max_tokens: 输出token上限，不传时使用config.MAX_TOKENS
        返回:
            message对象（包含.content和.tool_calls，可能是文本回复或工具调用请求）
        """
        self.last_call_metrics = {}
        token_limit = max_tokens or config.MAX_TOKENS
        span = f"llm-{uuid.uuid4().hex[:12]}"
        call_started = time.monotonic()
        self._external_deadline = (
            float(external_deadline) if external_deadline is not None else None
        )
        self._logical_deadline = (
            float(logical_deadline) if logical_deadline is not None else None
        )
        self._call_deadline = call_started + config.CALL_DEADLINE
        self._deadline_source = "call"
        if (
            self._logical_deadline is not None
            and self._logical_deadline < self._call_deadline
        ):
            self._call_deadline = self._logical_deadline
        if (
            self._external_deadline is not None
            and self._external_deadline < self._call_deadline
        ):
            self._call_deadline = self._external_deadline
            self._deadline_source = "run"
        self._remaining_timeout()
        if self.mock_mode:
            started = call_started
            base_data = {
                "operation": operation,
                "model": self.model,
                "reasoning": self.reasoning,
                "streaming": False,
                "attempt": 1,
                "max_tokens": token_limit,
                "message_count": len(messages or []),
                "tool_count": len(tools or []),
            }
            emit_trace("llm.call.started", span=span, parent=parent_span,
                       step=step, data=base_data)
            message = self._mock_chat(messages, tools)
            self.last_call_metrics = {
                "duration_ms": _elapsed_ms(started),
                "ttft_ms": None,
                "finish_reason": self.last_finish_reason,
                "usage_available": False,
                "input_tokens": None,
                "output_tokens": None,
                "total_tokens": None,
                "tool_call_count": len(getattr(message, "tool_calls", None) or []),
            }
            emit_trace(
                "llm.call.completed", span=span, parent=parent_span, step=step,
                data={
                    **base_data,
                    **self.last_call_metrics,
                },
            )
            return message

        model = getattr(self, "model", config.MODEL_NAME)
        reasoning = getattr(self, "reasoning", config.REASONING_EFFORT)
        base_kwargs = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": token_limit,
        }
        if tools:
            base_kwargs["tools"] = tools
            base_kwargs["tool_choice"] = "auto"
        if reasoning:
            base_kwargs["reasoning_effort"] = reasoning

        last_error = None
        max_attempts = max(1, config.MAX_RETRIES)
        for attempt in range(1, max_attempts + 1):
            remaining = self._remaining_timeout()
            kwargs = dict(base_kwargs)
            kwargs["timeout"] = remaining
            attempt_started = time.monotonic()
            self._attempt_started = attempt_started
            self._last_ttft_ms = None
            self._last_usage = _usage_fields(None)
            self._last_tool_call_count = None
            self.last_finish_reason = None
            streaming = bool(self.streaming)
            trace_data = {
                "operation": operation,
                "model": model,
                "reasoning": reasoning or None,
                "streaming": streaming,
                "attempt": attempt,
                "max_attempts": max_attempts,
                "max_tokens": token_limit,
                "message_count": len(messages or []),
                "tool_count": len(tools or []),
                "deadline_seconds": config.CALL_DEADLINE,
            }
            emit_trace("llm.call.started", span=span, parent=parent_span,
                       step=step, data=trace_data)
            try:
                if streaming:
                    message = self._chat_streaming(kwargs)
                else:
                    message = self._chat_once(kwargs)
                self._last_tool_call_count = len(
                    getattr(message, "tool_calls", None) or []
                )
                self.last_call_metrics = {
                    "duration_ms": _elapsed_ms(attempt_started),
                    "call_duration_ms": _elapsed_ms(call_started),
                    **self._attempt_metrics(),
                }
                emit_trace(
                    "llm.call.completed", span=span, parent=parent_span, step=step,
                    data={
                        **trace_data,
                        **self.last_call_metrics,
                    },
                )
                return message
            except Exception as e:
                if getattr(e, "is_run_deadline", False):
                    self.last_call_metrics = {
                        "duration_ms": _elapsed_ms(attempt_started),
                        "call_duration_ms": _elapsed_ms(call_started),
                        **self._attempt_metrics(),
                        "completed": False,
                        **_error_fields(e),
                    }
                    emit_trace(
                        "llm.call.failure", level="error", span=span,
                        parent=parent_span, step=step,
                        data={
                            **trace_data,
                            **self.last_call_metrics,
                        },
                    )
                    raise
                last_error = e
                # 端点不支持流式 → 永久回退非流式，立即重试（不消耗等待时间）
                is_deadline = isinstance(e, CallDeadlineExceeded)
                if streaming and not is_deadline and _is_streaming_unsupported(e) \
                        and attempt < max_attempts:
                    print("⚠️  当前端点疑似不支持流式传输，自动切换为非流式模式")
                    self.streaming = False
                    emit_trace(
                        "llm.call.retry", level="warning", span=span,
                        parent=parent_span, step=step,
                        data={
                            **trace_data,
                            "duration_ms": _elapsed_ms(attempt_started),
                            "reason": "stream_fallback",
                            "delay_ms": 0,
                            **self._attempt_metrics(),
                            **_error_fields(e),
                        },
                    )
                    continue
                print(f"⚠️  LLM调用失败（第{attempt}/{max_attempts}次尝试）：{_brief_error(e)}")
                if not is_deadline and attempt < max_attempts:
                    # 指数退避：3s → 15s → 60s，给过载的网关恢复时间
                    delay = min(config.RETRY_DELAY * (5 ** (attempt - 1)), config.RETRY_DELAY_CAP)
                    try:
                        remaining = self._remaining_timeout()
                    except CallDeadlineExceeded as deadline_error:
                        last_error = deadline_error
                        is_deadline = True
                        remaining = 0
                    if not is_deadline and delay >= remaining:
                        last_error = CallDeadlineExceeded("LLM call deadline exceeded before retry")
                        is_deadline = True
                    elif not is_deadline:
                        emit_trace(
                            "llm.call.retry", level="warning", span=span,
                            parent=parent_span, step=step,
                            data={
                                **trace_data,
                                "duration_ms": _elapsed_ms(attempt_started),
                                "reason": "network_error",
                                "delay_ms": int(delay * 1000),
                                **self._attempt_metrics(),
                                **_error_fields(e),
                            },
                        )
                        print(f"   ⏳ 等待{delay}秒后重试...")
                        time.sleep(delay)
                        continue
                emit_trace(
                    "llm.call.failure", level="error", span=span,
                    parent=parent_span, step=step,
                    data={
                        **trace_data,
                        "duration_ms": _elapsed_ms(attempt_started),
                        "call_duration_ms": _elapsed_ms(call_started),
                        **self._attempt_metrics(),
                        **_error_fields(last_error),
                    },
                )
                self.last_call_metrics = {
                    "duration_ms": _elapsed_ms(attempt_started),
                    "call_duration_ms": _elapsed_ms(call_started),
                    **self._attempt_metrics(),
                    "completed": False,
                    **_error_fields(last_error),
                }
                if isinstance(last_error, CallDeadlineExceeded):
                    raise last_error
                raise RuntimeError(
                    f"LLM调用连续{max_attempts}次失败：{_brief_error(last_error)}"
                ) from last_error

        raise RuntimeError("未知LLM调用错误")

    def _chat_once(self, kwargs):
        """非流式调用"""
        response = self.client.chat.completions.create(**kwargs)
        self._check_deadline()
        choice = response.choices[0]
        self.last_finish_reason = getattr(choice, "finish_reason", None)
        if self.last_finish_reason == "length":
            print("   ⚠️ 注意：本次LLM输出达到token上限被截断")
        usage = getattr(response, "usage", None)
        self._last_usage = _usage_fields(usage)
        _print_usage(usage)
        return choice.message

    def _chat_streaming(self, kwargs):
        """流式调用：边生成边接收，拼装成与非流式一致的message对象
        经代理网关的长输出（如完整优化版简历）用流式可避免网关首字节超时（504）
        """
        kwargs["stream"] = True
        stream = self.client.chat.completions.create(**kwargs)
        self._check_deadline()

        content_parts = []
        tool_calls_acc = {}  # index -> {"id", "name", "arguments": [分片列表]}
        finish_reason = None
        usage = None
        for chunk in stream:
            self._check_deadline()
            if self._last_ttft_ms is None:
                self._last_ttft_ms = _elapsed_ms(self._attempt_started)
            # 部分网关会在尾块携带usage（无需stream_options），能拿到就记录
            if getattr(chunk, "usage", None):
                usage = chunk.usage
                self._last_usage = _usage_fields(usage)
            if not getattr(chunk, "choices", None):
                continue
            choice = chunk.choices[0]
            delta = getattr(choice, "delta", None)
            if delta is not None:
                if getattr(delta, "content", None):
                    content_parts.append(delta.content)
                for tc in getattr(delta, "tool_calls", None) or []:
                    index = getattr(tc, "index", 0) or 0
                    acc = tool_calls_acc.setdefault(index, {"id": None, "name": "", "arguments": []})
                    if getattr(tc, "id", None):
                        acc["id"] = tc.id
                    function = getattr(tc, "function", None)
                    if function is not None:
                        if getattr(function, "name", None):
                            acc["name"] = function.name
                        if getattr(function, "arguments", None):
                            acc["arguments"].append(function.arguments)
                    self._last_tool_call_count = len(tool_calls_acc)
            if getattr(choice, "finish_reason", None):
                finish_reason = choice.finish_reason
                self.last_finish_reason = finish_reason

        self.last_finish_reason = finish_reason
        if finish_reason == "length":
            print("   ⚠️ 注意：本次LLM输出达到token上限被截断")
        self._last_usage = _usage_fields(usage)
        _print_usage(usage)

        tool_calls = None
        if tool_calls_acc:
            tool_calls = []
            for index in sorted(tool_calls_acc):
                acc = tool_calls_acc[index]
                tool_calls.append(_ToolCall(
                    call_id=acc["id"] or f"call_{index}",
                    name=acc["name"],
                    arguments="".join(acc["arguments"]),
                ))
        return _Message("".join(content_parts) or None, tool_calls)

    def simple_ask(self, prompt, system=None, temperature=0.3, max_tokens=None,
                   operation="simple_ask", parent_span=None, step=None,
                   external_deadline=None, logical_deadline=None):
        """
        便捷方法：单轮提问，直接返回文本
        供各个"子LLM调用"工具使用（如提取简历信息、分析JD等）
        """
        if self.mock_mode:
            deadlines = []
            if logical_deadline is not None:
                deadlines.append((float(logical_deadline), "call"))
            if external_deadline is not None:
                deadlines.append((float(external_deadline), "run"))
            if deadlines:
                deadline, source = min(deadlines)
                if time.monotonic() >= deadline:
                    if source == "run":
                        raise ExternalRunDeadlineExceeded(
                            "Agent run deadline exceeded"
                        )
                    raise CallDeadlineExceeded("LLM call deadline exceeded")
            from mock_data import mock_simple_ask
            self.last_finish_reason = "stop"
            return mock_simple_ask(prompt, system)

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        message = self.chat(
            messages, temperature=temperature, max_tokens=max_tokens,
            operation=operation, parent_span=parent_span, step=step,
            external_deadline=external_deadline,
            logical_deadline=logical_deadline,
        )
        return message.content or ""

    def _remaining_timeout(self):
        now = time.monotonic()
        remaining = self._call_deadline - now
        if remaining <= 0:
            if self._deadline_source == "run":
                raise ExternalRunDeadlineExceeded("Agent run deadline exceeded")
            raise CallDeadlineExceeded("LLM call deadline exceeded")
        return remaining

    def _check_deadline(self):
        self._remaining_timeout()

    def _attempt_metrics(self):
        return {
            "ttft_ms": self._last_ttft_ms,
            "finish_reason": self.last_finish_reason,
            **self._last_usage,
            "tool_call_count": self._last_tool_call_count,
        }

    def _mock_chat(self, messages, tools):
        """Mock模式：主循环按ReAct剧本推进；无工具调用时按提示词路由固定数据"""
        from mock_data import mock_agent_step, mock_simple_ask
        self.last_finish_reason = "stop"
        if tools:
            message = mock_agent_step(self._mock_step, messages)
            self._mock_step += 1
            return message
        system = ""
        prompt = ""
        for item in messages:
            if item.get("role") == "system":
                system = item.get("content", "")
            elif item.get("role") == "user":
                prompt = item.get("content", "")
        return _Message(mock_simple_ask(prompt, system), None)


def _brief_error(error):
    """把冗长的网关错误压缩成一行可读信息"""
    text = str(error)
    if len(text) > 300:
        text = text[:300] + "...(已截断)"
    return text


def _elapsed_ms(started):
    return max(0, int((time.monotonic() - started) * 1000))


def _usage_fields(usage):
    if not usage:
        return {
            "usage_available": False,
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
        }
    input_tokens = getattr(usage, "prompt_tokens", None)
    if input_tokens is None:
        input_tokens = getattr(usage, "input_tokens", None)
    output_tokens = getattr(usage, "completion_tokens", None)
    if output_tokens is None:
        output_tokens = getattr(usage, "output_tokens", None)
    total_tokens = getattr(usage, "total_tokens", None)
    available = any(value is not None for value in (
        input_tokens, output_tokens, total_tokens
    ))
    return {
        "usage_available": available,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }


def _error_fields(error):
    status = getattr(error, "status_code", None)
    if getattr(error, "is_run_deadline", False):
        category = "run_timeout"
    elif isinstance(error, (CallDeadlineExceeded, TimeoutError)):
        category = "timeout"
    elif isinstance(status, int) and status >= 500:
        category = "upstream"
    elif isinstance(status, int) and status >= 400:
        category = "request"
    else:
        category = "network"
    return {
        "error_class": type(error).__name__,
        "error_category": category,
        "http_status": status if isinstance(status, int) else None,
    }


def _print_usage(usage):
    """打印真实token用量（网关返回时才有；Web UI据此汇总展示，拿不到就不显示不估算）"""
    if not usage:
        return
    total = getattr(usage, "total_tokens", None)
    if not total:
        return
    prompt = getattr(usage, "prompt_tokens", None) or 0
    completion = getattr(usage, "completion_tokens", None) or 0
    print(f"   🧮 tokens：输入{prompt} + 输出{completion} = {total}")


class _ToolCallFunction:
    """流式拼装出的工具调用function部分（与SDK对象同构）"""
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class _ToolCall:
    """流式拼装出的工具调用（与SDK对象同构：有.id/.type/.function）"""
    def __init__(self, call_id, name, arguments):
        self.id = call_id
        self.type = "function"
        self.function = _ToolCallFunction(name, arguments)


class _Message:
    """统一的消息对象（流式拼装和Mock模式共用，与SDK message同构）"""
    def __init__(self, content, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls
