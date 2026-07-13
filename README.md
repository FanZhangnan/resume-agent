# 简历优化Agent

一个命令行和 Web 均可运行的简历分析 Agent：

- **确定性八阶段流水线**：解析、抽取、岗位发现、JD分析、匹配、改写、验证、报告均有明确边界和运行事件
- **有限并行**：用户已提供JD时，简历抽取与JD分析并行执行；后续步骤按依赖顺序运行
- **诊断回退**：旧 ReAct 调度仅通过 `AGENT_ORCHESTRATOR=react` 显式开启，不再承担默认生产调度
- **岗位推荐模式**：不知道投什么？只给简历不给JD，Agent自动推荐与你当前情况最匹配的大厂岗位（实习/工作，按匹配度排序），并针对第一名完成完整分析
- **运行过程可观察**：每个阶段、工具、重试、追问、修正和报告都会生成脱敏 Trace 事件
- **自我验证 + 自动修正**：交付前自动审查过度美化、编造、逻辑矛盾；验证不通过会**自动带着问题清单重新生成建议并复检**，全程记录修正日志
- **诚实第一**：不把"参与"改成"主导"，不编造数据；报告中明确区分"安全优化"和"需要你确认属实"的内容

## 快速开始

### 1. 安装依赖（只需一次）

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. 设置API密钥

```bash
export ZENMUX_API_KEY=你的密钥
```

没有密钥也可以先体验完整流程（离线演示模式，不联网）：

```bash
AGENT_MOCK=1 python agent.py --demo
```

### 3. 运行

```bash
python agent.py                        # 交互式（推荐）：按提示给简历和JD
python agent.py --demo                 # 用samples/中的示例简历和JD跑一遍
python agent.py resume.pdf             # 岗位推荐模式：只给简历，自动推荐最匹配的大厂岗位
python agent.py resume.pdf --prefer "在中国求职大厂实习"   # 推荐时指定偏好（地点/方向/公司）
python agent.py resume.pdf jd.txt      # 简历文件 + JD文件
python agent.py resume.docx "JD文本"   # 简历文件 + 直接贴JD文本
python agent.py --demo --model gpt-5.5 --reasoning xhigh  # 显式选择稳定基线
```

支持的简历格式：PDF、Word（.docx）、txt、Markdown。

### 4.（可选）Web UI

不想用命令行？**macOS直接双击项目里的 `启动WebUI.command`**（自动装依赖、启动服务并打开浏览器；
首次双击如被系统拦截：右键 → 打开）。或手动启动：

```bash
pip install -r requirements.txt        # 首次需安装fastapi/uvicorn
python webui/server.py                 # 打开 http://127.0.0.1:7860
```

密钥配置：项目根目录的 `.env` 会被自动加载（已存在的环境变量优先）。该文件包含API密钥，请勿外传。

## 公网部署（放到你的网站上给大家用）

> **Vercel Hobby（免费版，邀请制内测）**：无需长驻进程，用 FastAPI + Vercel Python Workflows 承载长运行。
> 完整环境变量、Private Blob、构建与验收步骤见 **[docs/deployment-vercel.md](docs/deployment-vercel.md)**。
> 该路径当前仅开放「粘贴 JD」流程，密钥只进 Vercel 环境变量、不下发浏览器。

本地默认是"个人模式"。设置 `AGENT_PUBLIC=1` 即切换为**公网多人模式**，专为"站长API资源有限"设计：

```bash
export AGENT_PUBLIC=1
export AGENT_UI_HOST=127.0.0.1     # 建议只监听本机，由nginx反代对外
export AGENT_TRUST_PROXY=1         # 经nginx反代时开启，正确识别访客IP
python webui/server.py
```

公网模式做了什么：

- **自带Key（BYOK）**：访客可在「高级设置」填自己的 API Key / Base URL，模型仍从站内 allowlist 引擎中选择，
  Key只在该次请求的子进程中使用，服务器不存储不记录，且**不占**你的额度
- **免费额度**：不带Key的访客用你 `.env` 里的Key，每IP每天默认2次（`AGENT_FREE_PER_DAY`），
  用完会提示填自己的Key——你的成本上限因此是固定的
- **限流**：每IP每小时最多6次启动（`AGENT_RUNS_PER_HOUR`）、Mock演示20次（`AGENT_MOCK_PER_HOUR`）、
  全局并发3个任务（`AGENT_MAX_CONCURRENT`）、每IP同时只能跑1个
- **会话隔离**：基于Cookie，访客只能看到/操作自己的任务和报告，历史报告互不可见
- **用后即焚**：上传的简历/JD与生成的报告文件在运行结束后立即从磁盘删除；
  报告只保留在该访客会话的内存里（最多5份、24小时过期），请访客及时下载

nginx 反代示例（SSE需要关闭缓冲）：

```nginx
server {
    listen 443 ssl;
    server_name your.domain.com;
    # ssl_certificate ...; ssl_certificate_key ...;
    client_max_body_size 6m;
    location / {
        proxy_pass http://127.0.0.1:7860;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_buffering off;            # SSE实时推流必需
        proxy_read_timeout 3600s;
    }
}
```

systemd 常驻示例（`/etc/systemd/system/resume-agent.service`）：

```ini
[Unit]
Description=Resume Agent Web UI
After=network.target

[Service]
WorkingDirectory=/opt/resume-agent
Environment=AGENT_PUBLIC=1 AGENT_TRUST_PROXY=1
ExecStart=/opt/resume-agent/venv/bin/python webui/server.py
Restart=always

[Install]
WantedBy=multi-user.target
```

注意事项：

- 公网必须走 **HTTPS**（BYOK的Key经明文HTTP会被窃听）
- 简历属于敏感个人信息：页面已内置"不留存、用完即删"的隐私说明，请勿自行改成留存模式后继续对外宣传"不留存"
- `.env` 与 `webui/quota.json` 不要提交到仓库
- 本项目以 MIT 协议开源（见 LICENSE），欢迎大家自行部署

Web UI 支持：上传/粘贴简历、粘贴JD（留空自动进入岗位推荐模式）、Mock离线演示、
实时查看 🧠思考→🔧行动→📋观察 推理流和分析流水线、Agent中途追问在线回答、
报告在线渲染与下载、历史报告浏览。

**简历排版**：分析完成后，「简历排版」页可把优化版简历渲染进三套固定模板
（经典单栏 / 现代双栏 / 极简留白），可选择放或不放照片（照片只在浏览器本地处理，
不会上传服务器），支持打印存PDF与下载HTML。排版数据来自Agent输出的结构化简历
（报告同名的 `.json` 文件）；旧报告没有结构化数据时自动按纯文本排入模板。

分析完成后，完整报告（Markdown）会保存到 `output/` 目录，包含：
**简历解析 /（岗位推荐）/ 匹配度分析 / 优化建议 / 自我验证（含修正日志）/ 诚实评估 / 优化版简历**。

**岗位推荐模式说明**：不提供JD时，Agent根据你的教育阶段（在读→实习/校招）、地理位置和技能证据，
推荐5个大厂岗位并按预估匹配度排序，然后针对排名第一的岗位做完整的匹配分析和简历优化。
注意：推荐岗位是基于各公司公开招聘要求整理的"典型岗位画像"，投递前请以官方最新JD为准。

## 配置（都是可选的，用环境变量覆盖即可，不用改代码）

| 环境变量 | 默认值 | 说明 |
|---------|--------|------|
| `ZENMUX_API_KEY` / `OPENAI_API_KEY` | 无 | API密钥（必填其一，除非用Mock模式）。同时设置时`ZENMUX_API_KEY`优先 |
| `AGENT_BASE_URL` | `https://api.zenmux.ai/v1` | API地址（OpenAI兼容格式，可换成任何兼容网关） |
| `AGENT_MODEL` | `gpt-5.5` | 模型名。仅支持稳定基线 `gpt-5.5` 和实验引擎 `gpt-5.6-terra` |
| `AGENT_MOCK` | 关 | `=1` 开启离线演示模式 |
| `AGENT_REASONING_EFFORT` | `xhigh` | 两个模型均仅支持 `high`/`xhigh`。非法组合会直接拒绝，不会静默降级 |
| `AGENT_ORCHESTRATOR` | `pipeline` | 默认确定性八阶段流水线；`react` 仅用于诊断旧调度路径 |
| `AGENT_MAX_STEPS` | `20` | 仅 ReAct 诊断模式使用的最大循环步数 |
| `AGENT_MAX_REVISIONS` | `1` | 自我验证不通过时的自动修正轮数 |
| `AGENT_MAX_TOKENS` | `4096` | 单次LLM调用输出上限 |
| `AGENT_REPORT_MAX_TOKENS` | `8192` | 最终报告生成输出上限 |
| `AGENT_CALL_DEADLINE` | `110` | 一次逻辑 LLM 调用的总时间预算（秒） |
| `AGENT_RUN_TIMEOUT` | `720` | 整次 Agent 运行的总时间预算（秒） |
| `AGENT_WATCHDOG_GRACE` | `15` | Web 任务超时后终止进程前的宽限（秒） |
| `AGENT_ASK_TIMEOUT` | `45` | 等待用户回答追问的时间预算（秒） |

例如换成其他OpenAI兼容网关和模型：

```bash
export AGENT_BASE_URL=https://api.wangdefou.studio/v1
export AGENT_MODEL=gpt-5.5
export AGENT_REASONING_EFFORT=xhigh
export OPENAI_API_KEY=该网关的密钥
```

### 模型政策与基准测试

Web UI 会分别显示「稳定/实验」状态和「未定价/免费」商业档位。GPT-5.5 的价格档暂未定义，
不会根据其稳定状态推断收费方式。GPT-5.6 Terra 的名称不代表它在本项目中比 GPT-5.5 更快或更好。

`benchmark_models.py` 默认拒绝联网；只有显式加 `--live` 才会使用固定合成样本发起请求：

```bash
python benchmark_models.py  # 安全拒绝，不构造API客户端
python benchmark_models.py --live --model gpt-5.5 --reasoning xhigh \
  --output output/gpt-5.5-xhigh-benchmark.json
```

输出只包含时延、完成状态、JSON/工具调用结果、token 用量和验证状态，不保存提示词或模型原始响应。

## 项目结构

```
agent.py            Agent入口、运行预算、自我修正、报告渲染与命令行入口
pipeline.py         默认八阶段任务图、条件分支、有限并行与阶段Trace
llm_client.py       LLM客户端封装（重试 / 超时 / Mock离线模式）
prompts.py          Agent系统提示词、报告格式化提示词
config.py           全部配置（支持环境变量覆盖）
utils.py            JSON容错解析、文本截断等工具函数
mock_data.py        离线演示数据（含"验证失败→自动修正"的完整剧本）
tools/
  __init__.py       工具注册表 + 统一执行入口
  common.py         带自动重试的LLM-JSON问答助手
  file_parser.py    PDF/Word/txt简历解析
  resume_tools.py   简历信息提取、JD分析
  recommendation.py 大厂岗位推荐（无JD时自动匹配）
  analysis.py       匹配度计算、优化建议生成（支持修正指令）
  verification.py   自我验证（批判性审查）
  interaction.py    向用户追问
samples/            示例简历和JD（--demo模式使用）
```

## 测试

```bash
AGENT_MOCK=1 python test_tools.py    # 8个工具逐个验证
AGENT_MOCK=1 python test_agent.py    # 默认流水线完整工作流（离线）
AGENT_MOCK=1 python test_pipeline.py # 阶段顺序、并发、修正和部分报告（离线）
python test_model_policy.py          # 稳定/实验政策与benchmark联网保护（离线）
python test_models.py                # 模型目录、商业档位与推理档位组合（离线）
python test_llm.py                   # 阶段1：API连通性（需要密钥）
```

Vercel 部署相关的模块（`run_security`、`vercel_trace`、`workflows/`、`webui/vercel_server`、
`webui/static/vercel_app.html`）需在 **Python 3.12** 下测试（见下方部署清单）：

```bash
/opt/homebrew/bin/python3.12 -m venv .venv312 && .venv312/bin/python -m pip install -r requirements.txt
AGENT_MOCK=1 .venv312/bin/python test_run_security.py     # 邀请码与签名令牌
AGENT_MOCK=1 .venv312/bin/python test_vercel_trace.py     # 私有 Blob 轨迹脱敏与隔离
AGENT_WORKFLOW_TEST=1 AGENT_MOCK=1 .venv312/bin/python test_vercel_workflow.py  # 八阶段工作流图
AGENT_WORKFLOW_TEST=1 .venv312/bin/python test_vercel_api.py                    # FastAPI 契约
AGENT_WORKFLOW_TEST=1 .venv312/bin/python test_vercel_deploy_contract.py        # Vercel 构建/沙箱契约
# 按 docs/deployment-vercel.md 在 .vercelignore 过滤后的临时目录构建后：
VERCEL_BUILD_ROOT=/tmp/clean-build .venv312/bin/python test_vercel_build_output.py  # 产物/敏感文件审计
AGENT_MOCK=1 .venv312/bin/python test_vercel_ui.py        # 前端源契约 + CSP + node --check
```

## 常见问题

- **报错"未检测到API密钥"**：先 `export ZENMUX_API_KEY=你的密钥`，或用 `AGENT_MOCK=1` 体验演示模式。
- **交互模式怎么结束粘贴？** 粘贴完成后另起一行输入 `END` 再回车。
- **Agent中途提问怎么办？** 直接回答即可；直接回车表示跳过，Agent会基于现有信息继续并在报告中标注信息缺失。
- **验证不通过会怎样？** Agent自动把问题清单交回给建议生成工具重写一版并复检（默认1轮）；如果仍不通过，报告里会如实标注，不会假装没问题。
