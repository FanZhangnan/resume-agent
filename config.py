import os


def _load_env_file():
    """加载项目根目录的.env文件（KEY=VALUE格式，支持#注释）
    已存在的环境变量优先，不会被.env覆盖；无需安装python-dotenv
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.isfile(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as file:
            for line in file:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        pass


_load_env_file()

# ===== API配置（都可以用环境变量覆盖，不需要改代码）=====
# 生产基线与实验引擎均通过明确 allowlist 开放；不根据模型名推断能力。
API_BASE_URL = os.environ.get("AGENT_BASE_URL", "https://api.zenmux.ai/v1")
# 密钥优先级：ZENMUX_API_KEY 优先，避免机器上残留的旧OPENAI_API_KEY静默覆盖
API_KEY = os.environ.get("ZENMUX_API_KEY") or os.environ.get("OPENAI_API_KEY", "")

# status 表示质量/稳定性政策，tier 表示商业分档，两者不可混用。
MODEL_OPTIONS = (
    {
        "id": "gpt-5.5",
        "label": "GPT-5.5",
        "tier": "unassigned",
        "tier_label": "未定价",
        "status": "stable",
        "status_label": "稳定",
        "default_reasoning": "xhigh",
        "reasoning_levels": ("high", "xhigh"),
    },
    {
        "id": "gpt-5.6-terra",
        "label": "GPT-5.6 Terra",
        "tier": "free",
        "tier_label": "免费",
        "status": "experimental",
        "status_label": "实验",
        "default_reasoning": "xhigh",
        "reasoning_levels": ("high", "xhigh"),
    },
)
SUPPORTED_MODELS = tuple(item["id"] for item in MODEL_OPTIONS)
MODEL_OPTION_BY_ID = {item["id"]: item for item in MODEL_OPTIONS}
MODEL_REASONING_LEVELS = {
    item["id"]: item["reasoning_levels"] for item in MODEL_OPTIONS
}
DEFAULT_MODEL = "gpt-5.5"
DEFAULT_REASONING_BY_MODEL = {
    item["id"]: item["default_reasoning"] for item in MODEL_OPTIONS
}
REASONING_LEVELS = ("high", "xhigh")


def reasoning_levels_for_model(model):
    return MODEL_REASONING_LEVELS.get(model, ())


def validate_model_reasoning(model, reasoning=None):
    """精确校验模型/推理档位；空档位使用该模型的显式默认值。"""
    selected_model = str(model or "").strip()
    if selected_model not in SUPPORTED_MODELS:
        raise ValueError(f"模型仅限：{' / '.join(SUPPORTED_MODELS)}")
    selected_reasoning = (
        DEFAULT_REASONING_BY_MODEL[selected_model]
        if reasoning is None or reasoning == ""
        else str(reasoning).strip()
    )
    allowed = reasoning_levels_for_model(selected_model)
    if selected_reasoning not in allowed:
        raise ValueError(
            f"{selected_model} 的推理强度仅限：{' / '.join(allowed)}"
        )
    return selected_model, selected_reasoning


_MODEL = os.environ.get("AGENT_MODEL", DEFAULT_MODEL).strip()
_EFFORT = os.environ.get("AGENT_REASONING_EFFORT", "").strip()
try:
    MODEL_NAME, REASONING_EFFORT = validate_model_reasoning(_MODEL, _EFFORT)
except ValueError as error:
    raise ValueError(
        "AGENT_MODEL / AGENT_REASONING_EFFORT 配置无效："
        f"{error}"
    ) from None

# ===== Agent行为配置 =====
ORCHESTRATOR_OPTIONS = ("pipeline", "react")


def validate_orchestrator(value):
    """编排器使用精确allowlist；空值回到确定性流水线。"""
    selected = "pipeline" if value is None or value == "" else str(value)
    if selected not in ORCHESTRATOR_OPTIONS:
        raise ValueError(
            f"AGENT_ORCHESTRATOR 仅限：{' / '.join(ORCHESTRATOR_OPTIONS)}"
        )
    return selected


ORCHESTRATOR = validate_orchestrator(os.environ.get("AGENT_ORCHESTRATOR", ""))
MAX_STEPS = int(os.environ.get("AGENT_MAX_STEPS", "20"))  # ReAct循环最大步数，防止无限循环
MAX_REVISION_ROUNDS = int(os.environ.get("AGENT_MAX_REVISIONS", "1"))  # 自我验证未通过时的自动修正轮数
MAX_TOKENS = int(os.environ.get("AGENT_MAX_TOKENS", "4096"))  # 常规LLM调用的输出token上限
REPORT_MAX_TOKENS = int(os.environ.get("AGENT_REPORT_MAX_TOKENS", "8192"))  # 最终报告生成的输出token上限

# 可靠性预算：所有网络重试由本项目负责，SDK内部重试必须关闭。
# MAX_RETRIES 表示应用层总尝试次数（默认2次：首次 + 1次重试）。
MAX_RETRIES = int(os.environ.get("AGENT_MAX_RETRIES", "2"))
RETRY_DELAY = 3          # 首次重试等待秒数，之后每次×5，上限60秒
RETRY_DELAY_CAP = 60
CALL_DEADLINE = float(os.environ.get("AGENT_CALL_DEADLINE", "110"))
RUN_TIMEOUT = float(os.environ.get("AGENT_RUN_TIMEOUT", "720"))
WATCHDOG_GRACE = float(os.environ.get("AGENT_WATCHDOG_GRACE", "15"))
ASK_TIMEOUT = float(os.environ.get("AGENT_ASK_TIMEOUT", "45"))
# 保留旧变量名兼容外部配置；每次请求会再按剩余deadline收紧。
REQUEST_TIMEOUT = float(os.environ.get("AGENT_TIMEOUT", str(CALL_DEADLINE)))

# 仅表示未来可选能力，当前trace仍只写安全摘要；公网模式永不允许开启。
TRACE_RAW_CAPTURE = (
    os.environ.get("AGENT_TRACE_RAW", "0") == "1"
    and os.environ.get("AGENT_PUBLIC", "0") != "1"
)

# 流式传输：长输出（如完整优化版简历）经代理网关时容易触发首字节超时（Cloudflare约100秒），
# 流式可以边生成边接收，大幅降低504概率。端点不支持时会自动回退到非流式。
STREAMING = os.environ.get("AGENT_STREAM", "1") != "0"

# 报告输出目录（Web UI公网模式会为每次运行指定独立临时目录，用后即焚）
OUTPUT_DIR = os.environ.get("AGENT_OUTPUT_DIR", "output")
