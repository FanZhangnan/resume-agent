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
# 默认值遵循企划文档：ZenMux官方地址 + 免费Fable模型
# 如果你用其他OpenAI兼容网关：export AGENT_BASE_URL=网关地址  AGENT_MODEL=模型名
API_BASE_URL = os.environ.get("AGENT_BASE_URL", "https://api.zenmux.ai/v1")
MODEL_NAME = os.environ.get("AGENT_MODEL", "claude-fable-5-free")
# 密钥优先级：ZENMUX_API_KEY 优先，避免机器上残留的旧OPENAI_API_KEY静默覆盖
API_KEY = os.environ.get("ZENMUX_API_KEY") or os.environ.get("OPENAI_API_KEY", "")

# 推理强度：gpt-5.5实测支持 none / low / medium / high / xhigh（minimal会被拒），留空=网关默认
# Web UI中用户可按次选择四档；none=关闭推理（仅CLI/环境变量可用）
# 换网关/模型后可用 probe_reasoning.py 重新实测
REASONING_LEVELS = ("none", "low", "medium", "high", "xhigh")
_EFFORT = os.environ.get("AGENT_REASONING_EFFORT", "").strip().lower()
REASONING_EFFORT = _EFFORT if _EFFORT in REASONING_LEVELS else ""

# ===== Agent行为配置 =====
MAX_STEPS = int(os.environ.get("AGENT_MAX_STEPS", "20"))  # ReAct循环最大步数，防止无限循环
MAX_REVISION_ROUNDS = int(os.environ.get("AGENT_MAX_REVISIONS", "1"))  # 自我验证未通过时的自动修正轮数
MAX_TOKENS = int(os.environ.get("AGENT_MAX_TOKENS", "4096"))  # 常规LLM调用的输出token上限
REPORT_MAX_TOKENS = int(os.environ.get("AGENT_REPORT_MAX_TOKENS", "8192"))  # 最终报告生成的输出token上限

# 重试策略：网关过载（如Cloudflare 504）常需要等较久，采用指数退避 3s→15s→60s
MAX_RETRIES = int(os.environ.get("AGENT_MAX_RETRIES", "4"))
RETRY_DELAY = 3          # 首次重试等待秒数，之后每次×5，上限60秒
RETRY_DELAY_CAP = 60
REQUEST_TIMEOUT = int(os.environ.get("AGENT_TIMEOUT", "300"))  # 单次请求超时（秒）

# 流式传输：长输出（如完整优化版简历）经代理网关时容易触发首字节超时（Cloudflare约100秒），
# 流式可以边生成边接收，大幅降低504概率。端点不支持时会自动回退到非流式。
STREAMING = os.environ.get("AGENT_STREAM", "1") != "0"

# 报告输出目录（Web UI公网模式会为每次运行指定独立临时目录，用后即焚）
OUTPUT_DIR = os.environ.get("AGENT_OUTPUT_DIR", "output")
