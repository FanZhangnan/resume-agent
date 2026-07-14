# Vercel Hobby 部署与验收清单

面向**小范围公开测试**：把简历优化 Agent 部署到 Vercel Hobby（免费版），用 FastAPI 无服务器入口 + Vercel Python Workflows 承载 10–13 分钟的长运行。当前预览**仅开放「粘贴目标 JD」的流程**；无 JD 的在招岗位发现已在代码中留出接口但预览未启用。

密钥只进 Vercel 环境变量，绝不写进代码、前端、命令历史或对话。

---

## 0. 架构回顾

| 边界 | 文件 | 作用 |
|------|------|------|
| API 入口 | `webui/vercel_server.py`（`webui.vercel_server:app`） | 校验模型策略、执行配额准入、解析上传、启动工作流与轮询 |
| 持久工作流 | `workflows/resume_workflow.py` + `workflows/graph.py` | 八阶段确定性图；步内 `asyncio.to_thread` 复用现有同步工具；截止/取消/三重交付门 |
| 队列适配 | `workflows/vercel_worker.py` | 为 GA Services 注册 `__wkf_*` 私有队列消费者并导出 ASGI 入口 |
| 脱敏轨迹 | `run_trace_store.py` + `quota_store.py` | 同一 Redis 运行 Hash 保存 8 阶段脱敏状态与取消标记，绝对 TTL 24 小时 |
| 配额与会话 | `quota_store.py` + `public_security.py` | Upstash Redis 原子配额/并发租约，HttpOnly Cookie 归属，BYOK AES-GCM 临时加密 |
| 前端 | `webui/static/vercel_app.html` | Cookie 会话轮询、BYOK/Mock/剩余额度/最近任务，安全 Markdown 渲染与逐响应 CSP nonce |

- API 请求只负责解析与启动；LLM 调用在 Workflow worker 的 durable step 内执行。整次运行应用侧目标 **720s**、验收硬顶 **780s**。
- 本地 SSE 版 `webui/server.py`（`127.0.0.1:7860`）保持不变，仅用于本地开发。

---

## 1. 先决条件

- Vercel 账号（Hobby 即可）、`npx`（Node 18+）。
- 构建环境 Python **3.12**（`pyproject.toml` 已限定 `>=3.12,<3.13`；`requirements.txt` 用版本标记只在 3.12 装 `vercel==0.6.0`）。
- 得否 OpenAI 兼容网关及**已轮换**的密钥；服务端地址固定为 `https://api.wangdefou.studio/v1`。

## 2. 安全：先轮换密钥（务必第一步）

之前在会话/历史里出现过的网关密钥视为**已泄露**，上线前必须在网关侧作废并重新签发。新密钥只填入 Vercel 环境变量，不要再粘贴进任何对话或代码。

## 3. 连接存储

在 Vercel Marketplace 连接 **Upstash Redis Free**，选择 `iad1`、关闭自动升级，并连接 Preview 与 Production。Vercel 会注入 `KV_REST_API_URL` 与 `KV_REST_API_TOKEN`；它们用于跨实例配额、并发租约、会话归属、最近 5 份报告索引、脱敏阶段轨迹和取消标记。每个 `ra:run:<run_id>` Hash 从运行创建时起最长保留 24 小时，后续轨迹写入不延长 TTL。Redis 不可用时 API 在准入、归属和取消边界一律失败关闭。

## 4. 环境变量（全部服务端，Preview 与 Production 都要设）

| 变量 | 必填 | 示例 / 说明 |
|------|------|-------------|
| `OPENAI_API_KEY` | ✅ | 轮换后的得否网关密钥 |
| `AGENT_BASE_URL` | ✅ | `https://api.wangdefou.studio/v1`；仅允许该服务端网关 |
| `AGENT_RUN_SIGNING_KEY` | ✅ | Cookie/HMAC 与 BYOK 加密根密钥（随机 32+ 字节，独立于网关密钥） |
| `KV_REST_API_URL` | ✅ | Upstash 连接自动生成，不下发浏览器 |
| `KV_REST_API_TOKEN` | ✅ | Upstash 连接自动生成，标记 Sensitive |
| `AGENT_MODEL` | 建议 | `gpt-5.5`（默认基线） |
| `AGENT_REASONING_EFFORT` | 建议 | `xhigh` |
| `AGENT_RUN_TIMEOUT` | 可选 | 默认 `720`（秒），无需修改即在 13 分钟内 |
| `AGENT_WORKFLOW_PARALLEL` | 可选 | 默认并发结构化+JD分析；设 `0` 走顺序回退（若并发冒烟不过） |
| `AGENT_FREE_PER_DAY` | 可选 | 每 IP 每日站点免费真实分析，默认 `2` |
| `AGENT_SITE_FREE_PER_DAY` | 可选 | 全站每日站点付费上限，默认 `20` |
| `AGENT_RUNS_PER_HOUR` | 可选 | 每 IP 真实/BYOK 启动上限，默认 `6` |
| `AGENT_MOCK_PER_HOUR` | 可选 | 每 IP Mock 启动上限，默认 `20` |
| `AGENT_MAX_CONCURRENT` | 可选 | 全站并发上限，默认 `3`；每 IP 固定 `1` |
| `AGENT_SESSION_TTL` | 可选 | Cookie 会话与历史 TTL，最长 `86400` 秒 |
| `AGENT_SESSION_REPORT_CAP` | 可选 | 每会话最近任务数，默认 `5` |
| `AGENT_ADMISSION_TTL` | 可选 | 并发租约/BYOK 凭据 TTL，默认 `900` 秒 |
| `AGENT_MOCK` | 仅本地调试 | 全局 Mock 开关；Production 必须删除，线上 Mock 由用户按次选择 |

核心网关配置应精确填写为：

```dotenv
OPENAI_API_KEY=你的得否网关密钥
AGENT_BASE_URL=https://api.wangdefou.studio/v1
AGENT_FREE_PER_DAY=2
AGENT_SITE_FREE_PER_DAY=20
AGENT_RUNS_PER_HOUR=6
AGENT_MOCK_PER_HOUR=20
AGENT_MAX_CONCURRENT=3
AGENT_SESSION_TTL=86400
AGENT_SESSION_REPORT_CAP=5
AGENT_ADMISSION_TTL=900
```

> **禁止**出现 `sol`、`max`、`low`、`medium` 等档位，或把网关密钥/base_url 暴露到浏览器。`AGENT_WORKFLOW_TEST` 只用于本地测试，**不要**在 Vercel 设置。

## 5. 本地离线验证（部署前跑一遍）

```bash
# 一次性：创建 3.12 venv 并装依赖（已在 .gitignore 忽略 .venv312/）
/opt/homebrew/bin/python3.12 -m venv .venv312
.venv312/bin/python -m pip install -r requirements.txt

# 全量离线套件（全部应为 exit 0）
for t in test_tools test_agent test_model_policy test_runtime_policy \
         test_contracts test_scoring test_pipeline test_trace_catalog \
         test_run_security test_vercel_trace test_web_trace_ui test_vercel_ui; do
  AGENT_MOCK=1 .venv312/bin/python $t.py || echo "FAIL $t"
done
AGENT_WORKFLOW_TEST=1 AGENT_MOCK=1 .venv312/bin/python test_vercel_workflow.py
AGENT_WORKFLOW_TEST=1 AGENT_MOCK=1 .venv312/bin/python test_vercel_api.py
AGENT_WORKFLOW_TEST=1 AGENT_MOCK=1 .venv312/bin/python test_vercel_deploy_contract.py

.venv312/bin/python -m compileall -q . && git diff --check
```

## 6. 构建与 Preview 部署

```bash
npx vercel@latest login          # 或 whoami 确认已登录
npx vercel@latest link           # 关联/新建项目
# 在控制台或 CLI 配置好 §4 的环境变量（Preview 环境）
npx vercel@latest deploy --dry --format=json  # 先确认上传清单无 .env/简历/本地环境
npx vercel@latest deploy --target=preview     # 让 Vercel 从 .vercelignore 过滤后的源码远程构建
```

不要直接从含 `.env`、`.venv312` 或本地输出的工作目录执行
`vercel deploy --prebuilt`。CLI 55 的 GA Python 本地构建不会把
`functions.excludeFiles` 用于源码收集；若必须使用预构建，先把项目按
`.vercelignore` 同步到临时目录，在该目录执行 `vercel build --target=preview`，
再用 `VERCEL_BUILD_ROOT=<临时目录> test_vercel_build_output.py` 审计通过后部署。

部署后先只验证零成本项，再花网关额度：

```bash
curl -s https://<preview>/api/config      # 只应有 gpt-5.5 与 gpt-5.6-terra 的 high/xhigh
curl -s https://<preview>/api/status      # deployment_mode=vercel
```

> **Workflow worker 注意**：新项目使用 GA `services`。`resume_workflow`
> service 指向 `workflows/vercel_worker.py`，并在 `functions` 中声明
> `queue/v2beta` 的 `__wkf_*` 触发器；只有 `web` service 通过顶层 rewrite
> 对外暴露。Vercel 的 Python Workflows beta 页面仍展示旧
> `experimentalServices` 示例，不适用于新项目。

## 7. 隐私与保留

- 简历/JD 原文只存在于：TLS 保护的启动请求、Vercel 工作流状态（Hobby **保留 1 天**）、以及当前 HttpOnly Cookie 会话可读的最终结果。**不进** Redis 轨迹、不进浏览器存储、不进 Vercel Logs。
- Redis 运行 Hash 只保存 HMAC 化的归属、任务元数据、脱敏阶段状态和取消标记。`DELETE /api/runs/{id}` 使用一次 Lua 原子删除运行 Hash 和历史索引；非终态删除返回 `409`。过期由 Redis `EXPIREAT` 自动处理，不再需要 Blob 或 Cron 扫描。
- BYOK 仅以 AES-GCM 密文保存到运行终态或 15 分钟 TTL；前端不使用 `localStorage`/`sessionStorage` 保存 Key 或运行凭证。

## 8. 上线验收矩阵（每个开放组合都要过）

2 模型 × 2 推理档 × 供 JD 路径，共 4 组：

| 模型 | 档位 | 供 JD 完成态 | 终态 < 780s | 报告可交付/部分标注清晰 |
|------|------|-------------|-------------|--------------------------|
| gpt-5.5 | high | ☐ | ☐ | ☐ |
| gpt-5.5 | xhigh | ☐ | ☐ | ☐ |
| gpt-5.6-terra | high | ☐ | ☐ | ☐ |
| gpt-5.6-terra | xhigh | ☐ | ☐ | ☐ |

同时人工确认：8 阶段行推进、明细抽屉只显示脱敏字段、Cookie 刷新恢复、历史任务与跨会话隔离、Mock/BYOK/免费额度、协作式取消、报告渲染与导出、CSP 无控制台报错、浏览器/响应中**不出现任何密钥**。任一组合不过，暂不对该组合开放，补一个失败回归测试再修。

## 9. 晋升生产

Preview 全绿、删除 Preview 的 `AGENT_MOCK`、配置轮换后的网关密钥，并完成四组真实模型验收后，用同一 commit：

```bash
npx vercel@latest deploy --target=production
```

复检 `/api/config`、一次 mock 运行、一次 `gpt-5.5/xhigh` 供 JD 实跑、Redis 原子删除、零应用 Blob 调用与 Vercel Logs 脱敏。记录生产 URL 与剩余 Hobby 额度。

## 10. 已知边界

- **无 JD 在招岗位发现**：本预览未启用；`POST /api/runs` 无 JD 时返回 422，工作流图会把阶段 3 标为 `not_enabled` 并产出明确的部分报告。后续接 `tools/job_sources.py`（Greenhouse/Lever/Ashby/Adzuna）再开放。
- **并发步**：结构化+JD 分析用 `asyncio.gather`，属 SDK 运行时支持但未文档化的行为，需部署冒烟确认；不行就设 `AGENT_WORKFLOW_PARALLEL=0` 顺序执行（仍在预算内）。
- **Python Workflows 为 beta**：`vercel==0.6.0` API 可能变化；应用运行时只使用公开 Workflow API（`Workflows`/`@wf.step`/`@wf.workflow`/`get_step_metadata`/`start`/`Run`），未引用 `vercel._internal` 或 `vercel.blob`。
