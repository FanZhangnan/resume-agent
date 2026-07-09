"""
LLM客户端封装：所有与大模型API的通信都通过这个模块
包含：
- 指数退避自动重试（应对网关过载、Cloudflare 504等临时故障）
- 流式传输（长输出经代理网关时避免首字节超时；端点不支持时自动回退非流式）
- Mock离线模式（没有API密钥时可完整演示全流程）
"""
import os
import time
from openai import OpenAI
import config


class LLMClient:
    """封装OpenAI兼容API的调用"""

    def __init__(self):
        # 最近一次调用的结束原因："length"表示输出被max_tokens截断（调用方可据此扩容重试）
        self.last_finish_reason = None
        self.mock_mode = os.environ.get("AGENT_MOCK", "") == "1"
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
        self.streaming = config.STREAMING
        self.client = OpenAI(
            base_url=config.API_BASE_URL,
            api_key=config.API_KEY,
            default_headers={"User-Agent": "curl/8.7.1"},
            timeout=config.REQUEST_TIMEOUT,
        )

    def chat(self, messages, tools=None, temperature=0.3, max_tokens=None):
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
        if self.mock_mode:
            return self._mock_chat(messages, tools)

        kwargs = {
            "model": config.MODEL_NAME,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens or config.MAX_TOKENS,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        last_error = None
        for attempt in range(1, config.MAX_RETRIES + 1):
            try:
                if self.streaming:
                    return self._chat_streaming(dict(kwargs))
                return self._chat_once(kwargs)
            except Exception as e:
                last_error = e
                # 端点不支持流式 → 永久回退非流式，立即重试（不消耗等待时间）
                if self.streaming and "stream" in str(e).lower():
                    print("⚠️  当前端点疑似不支持流式传输，自动切换为非流式模式")
                    self.streaming = False
                    continue
                print(f"⚠️  LLM调用失败（第{attempt}/{config.MAX_RETRIES}次尝试）：{_brief_error(e)}")
                if attempt < config.MAX_RETRIES:
                    # 指数退避：3s → 15s → 60s，给过载的网关恢复时间
                    delay = min(config.RETRY_DELAY * (5 ** (attempt - 1)), config.RETRY_DELAY_CAP)
                    print(f"   ⏳ 等待{delay}秒后重试...")
                    time.sleep(delay)

        raise RuntimeError(f"LLM调用连续{config.MAX_RETRIES}次失败：{_brief_error(last_error)}")

    def _chat_once(self, kwargs):
        """非流式调用"""
        response = self.client.chat.completions.create(**kwargs)
        choice = response.choices[0]
        self.last_finish_reason = getattr(choice, "finish_reason", None)
        if self.last_finish_reason == "length":
            print("   ⚠️ 注意：本次LLM输出达到token上限被截断")
        return choice.message

    def _chat_streaming(self, kwargs):
        """流式调用：边生成边接收，拼装成与非流式一致的message对象
        经代理网关的长输出（如完整优化版简历）用流式可避免网关首字节超时（504）
        """
        kwargs["stream"] = True
        stream = self.client.chat.completions.create(**kwargs)

        content_parts = []
        tool_calls_acc = {}  # index -> {"id", "name", "arguments": [分片列表]}
        finish_reason = None
        for chunk in stream:
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
            if getattr(choice, "finish_reason", None):
                finish_reason = choice.finish_reason

        self.last_finish_reason = finish_reason
        if finish_reason == "length":
            print("   ⚠️ 注意：本次LLM输出达到token上限被截断")

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

    def simple_ask(self, prompt, system=None, temperature=0.3, max_tokens=None):
        """
        便捷方法：单轮提问，直接返回文本
        供各个"子LLM调用"工具使用（如提取简历信息、分析JD等）
        """
        if self.mock_mode:
            from mock_data import mock_simple_ask
            self.last_finish_reason = "stop"
            return mock_simple_ask(prompt, system)

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        message = self.chat(messages, temperature=temperature, max_tokens=max_tokens)
        return message.content or ""

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
