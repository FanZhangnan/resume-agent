"""
工具层公共辅助：
- 共享的LLM客户端（避免每次工具调用重复初始化）
- 带自动重试的"LLM返回JSON"问答：
  · 输出被截断（finish_reason=length）→ 扩大token上限并要求压缩表述后重试
  · 格式错误 → 附加纠错提示重试
  · 重试仍失败返回None（工具据此返回success=False，Agent可感知并重试）
"""
import config
from llm_client import LLMClient
from utils import parse_json_safely

_client = None

_JSON_RETRY_SUFFIX = (
    "\n\n注意：你上一次的输出不是合法JSON。"
    "请重新输出，只输出一个合法的JSON对象，不要包含任何解释文字或代码块标记。"
)

_TRUNCATED_RETRY_SUFFIX = (
    "\n\n注意：你上一次的输出因超长被截断了。"
    "请压缩表述（数组各项更精炼、去掉冗余修饰），确保完整输出一个合法的JSON对象。"
)


def get_client():
    """获取共享的LLM客户端实例"""
    global _client
    if _client is None:
        _client = LLMClient()
    return _client


def ask_json(prompt, system, default, temperature=0.2, label=None, max_tokens=None):
    """调用LLM并解析JSON返回，失败自动重试一轮（区分截断和格式错误两种失败）
    成功时用default补齐缺失字段，保证下游字段访问安全
    """
    if label:
        print(f"   ⏳ {label}...")
    client = get_client()
    current_prompt = prompt
    current_max = max_tokens
    for attempt in (1, 2):
        content = client.simple_ask(
            prompt=current_prompt, system=system,
            temperature=temperature, max_tokens=current_max,
        )
        data = parse_json_safely(content, default={})
        if isinstance(data, dict) and data:
            for key, value in default.items():
                data.setdefault(key, value)
            return data
        if attempt == 1:
            if client.last_finish_reason == "length":
                # 截断导致的失败：扩大输出上限 + 要求压缩表述
                current_max = max(config.REPORT_MAX_TOKENS, (current_max or config.MAX_TOKENS) * 2)
                current_prompt = prompt + _TRUNCATED_RETRY_SUFFIX
                print("   ⚠️ LLM输出超长被截断，扩大输出上限后自动重试...")
            else:
                current_prompt = prompt + _JSON_RETRY_SUFFIX
                print("   ⚠️ LLM返回内容不是合法JSON，自动重试一轮...")
    return None
