# Vercel Hobby 部署与验收清单

面向**小范围公开测试**：把简历优化 Agent 部署到 Vercel Hobby（免费版），用 FastAPI 无服务器入口 + Vercel Python Workflows 承载 10–13 分钟的长运行。当前预览**仅开放「粘贴目标 JD」的流程**；无 JD 的在招岗位发现已在代码中留出接口但预览未启用。

密钥只进 Vercel 环境变量，绝不写进代码、前端、命令历史或对话。

---

## 0. 架构回顾

| 边界 | 文件 | 作用 |
|------|------|------|
| API 入口 | `webui/vercel_server.py`（`webui.vercel_server:app`） | 校验模型策略、解析上传、启动工作流、轮询状态、取消/删除/清理 |
| 持久工作流 | `workflows/resume_workflow.py` + `workflows/graph.py` | 八阶段确定性图；步内 `asyncio.to_thread` 复用现有同步工具；截止/取消/三重交付门 |
| 队列适配 | `workflows/vercel_worker.py` | 为 GA Services 注册 `__wkf_*` 私有队列消费者并导出 ASGI 入口 |
| 私有轨迹 | `vercel_trace.py` | 每阶段脱敏状态文档写入 **Private Blob**（不含简历/JD/提示词/报告正文） |
| 访问控制 | `run_security.py` | HMAC-SHA256 签名运行令牌与会话安全 |
| 前端 | `webui/static/vercel_app.html` | 无 BYOK 的轮询界面，`escapeHtml` 先转义再渲染 Markdown，逐响应 CSP nonce |

- API 请求只负责解析与启动；LLM 调用在 Workflow worker 的 durable step 内执行。整次运行应用侧目标 **720s**、验收硬顶 **780s**。
- 本地 SSE 版 `webui/server.py`（`127.0.0.1:7860`）保持不变，仅用于本地开发。

---

## 1. 先决条件

- Vercel 账号（Hobby 即可）、`npx`（Node 18+）。
- 构建环境 Python **3.12**（`pyproject.toml` 已限定 `>=3.12,<3.13`；`requirements.txt` 用版本标记只在 3.12 装 `vercel==0.6.0`）。
- 得否 OpenAI 兼容网关及**已轮换**的密钥；服务端地址固定为 `https://api.wangdefou.studio/v1`。

## 2. 安全：先轮换密钥（务必第一步）

之前在会话/历史里出现过的网关密钥视为**已泄露**，上线前必须在网关侧作废并重新签发。新密钥只填入 Vercel 环境变量，不要再粘贴进任何对话或代码。

## 3. 连接 Private Blob 存储

Vercel 控制台 → 项目 → **Storage** → 新建 **Blob** 存储并连接到本项目 → 复制生成的 `BLOB_READ_WRITE_TOKEN`（读写令牌仅服务端使用）。阶段轨迹与取消标记都写在这里，保留期由每日 Cron 清理（见 §7）。

## 4. 环境变量（全部服务端，Preview 与 Production 都要设）

| 变量 | 必填 | 示例 / 说明 |
|------|------|-------------|
| `OPENAI_API_KEY` | ✅ | 轮换后的得否网关密钥 |
| `AGENT_BASE_URL` | ✅ | `https://api.wangdefou.studio/v1`；仅允许该服务端网关 |
| `AGENT_RUN_SIGNING_KEY` | ✅ | 运行令牌 HMAC 签名密钥（随机 32+ 字节，独立于网关密钥） |
| `BLOB_READ_WRITE_TOKEN` | ✅ | 连接 Blob 时生成 |
| `CRON_SECRET` | ✅ | 清理端点凭证；Vercel Cron 会自动带 `Authorization: Bearer $CRON_SECRET` |
| `AGENT_MODEL` | 建议 | `gpt-5.5`（默认基线） |
| `AGENT_REASONING_EFFORT` | 建议 | `xhigh` |
| `AGENT_RUN_TIMEOUT` | 可选 | 默认 `720`（秒），无需修改即在 13 分钟内 |
| `AGENT_WORKFLOW_PARALLEL` | 可选 | 默认并发结构化+JD分析；设 `0` 走顺序回退（若并发冒烟不过） |
| `AGENT_MOCK` | 仅 Preview 冒烟 | `1` 时不调用网关；Production 必须删除该变量 |

核心网关配置应精确填写为：

```dotenv
OPENAI_API_KEY=你的得否网关密钥
AGENT_BASE_URL=https://api.wangdefou.studio/v1
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

- 简历/JD 原文只存在于：TLS 保护的启动请求、Vercel 工作流状态（Hobby **保留 1 天**）、以及经令牌鉴权的最终结果。**不进** Blob 轨迹、不进浏览器存储、不进 Vercel Logs。
- Blob 轨迹只存脱敏状态字段；`DELETE /api/runs/{id}` 可即时删除；每日 Cron `GET /api/maintenance/cleanup` 清理 >24h 的轨迹（Hobby 精度为小时/天，实际清理可能滞后到约 48h）。
- 前端仅在用户明确操作后于 `sessionStorage` 保存**签名令牌**（非密钥），刷新可恢复运行。

## 8. 上线验收矩阵（每个开放组合都要过）

2 模型 × 2 推理档 × 供 JD 路径，共 4 组：

| 模型 | 档位 | 供 JD 完成态 | 终态 < 780s | 报告可交付/部分标注清晰 |
|------|------|-------------|-------------|--------------------------|
| gpt-5.5 | high | ☐ | ☐ | ☐ |
| gpt-5.5 | xhigh | ☐ | ☐ | ☐ |
| gpt-5.6-terra | high | ☐ | ☐ | ☐ |
| gpt-5.6-terra | xhigh | ☐ | ☐ | ☐ |

同时人工确认：8 阶段行推进、明细抽屉只显示脱敏字段、刷新恢复、会话隔离、协作式取消、报告渲染与导出、CSP 无控制台报错、浏览器/响应中**不出现任何密钥**。任一组合不过，暂不对该组合开放，补一个失败回归测试再修。

## 9. 晋升生产

Preview 全绿、删除 Preview 的 `AGENT_MOCK`、配置轮换后的网关密钥，并完成四组真实模型验收后，用同一 commit：

```bash
npx vercel@latest deploy --target=production
```

复检 `/api/config`、一次 mock 运行、一次 `gpt-5.5/xhigh` 供 JD 实跑、Blob 轨迹删除、Vercel Logs 脱敏。记录生产 URL 与剩余 Hobby 额度。

## 10. 已知边界

- **无 JD 在招岗位发现**：本预览未启用；`POST /api/runs` 无 JD 时返回 422，工作流图会把阶段 3 标为 `not_enabled` 并产出明确的部分报告。后续接 `tools/job_sources.py`（Greenhouse/Lever/Ashby/Adzuna）再开放。
- **并发步**：结构化+JD 分析用 `asyncio.gather`，属 SDK 运行时支持但未文档化的行为，需部署冒烟确认；不行就设 `AGENT_WORKFLOW_PARALLEL=0` 顺序执行（仍在预算内）。
- **Python Workflows 为 beta**：`vercel==0.6.0` API 可能变化；已只用公开 API（`Workflows`/`@wf.step`/`@wf.workflow`/`get_step_metadata`/`start`/`Run`/`vercel.blob`），未引用 `vercel._internal`。
