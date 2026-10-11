# Resume Agent API 迁移与交付体验维护设计

目标：将原生产站点 resume.defou.ai 从已到期的 api.wangdefou.studio 迁移到同一公司的 api.defou.ai，完成真实分析、审查、报告、排版与下载的上线验证，同时让下一次服务商配置变化不再要求修改业务代码。

## 已确认范围

- 用户已授权仓库优化，包括 workflow、UI、新功能及 Vercel 上线。
- 新 API 服务商为 `https://api.defou.ai/`；密钥已经配置在 Vercel。
- 本次开发基线是 GitHub `main` 的 `821e344`。原本地分支及全部未提交内容已备份，并保留在原目录。
- 保留现有八阶段 durable workflow、Cookie 会话隔离、配额和真实性交付门。
- 目标用户为使用中文界面的求职者；匹配分数只说明证据匹配，不代表面试或录用概率。

## 方案选择

直接替换域名最快，但会再次受写死的模型及请求参数限制。全面重写前后端会扩大本次故障修复风险。本次采用现有架构上的配置和交付体验改进：服务端管理网关与模型，结果视图提供摘要和修改对照，增加可编辑 Word 下载。

## 网关与模型边界

- 默认 `AGENT_BASE_URL=https://api.defou.ai/v1`。地址只由服务端环境变量配置。
- 接受 HTTPS 公共网关地址，拒绝用户信息、查询串、fragment、私有或回环 IP 地址；本次不向公网请求开放任意 API 地址。
- `AGENT_MODEL_OPTIONS` 为管理员配置的 JSON 数组，包含 `id`, `label`, `reasoning_levels`, `default_reasoning`, `supports_temperature`, `token_parameter`；不提供该变量时保留已知模型目录。
- 只发送模型声明支持的请求参数。无推理参数的模型使用空字符串 reasoning；这是显式能力配置，不是遇错后的静默降级。
- `AGENT_MODEL` 可选择目录中的默认模型，空值使用目录首项。
- `AGENT_PROVIDER_NAME` 控制界面服务商名称，默认“得否 API”。
- `AGENT_LIVE_ENABLED=0` 可暂时关闭真实分析；此时 Mock 演示可用，真实请求在配额准入前返回 `503 service_unavailable`。
- API 的 public config 只暴露显示目录、服务可用标记和通用说明，不暴露网关地址或密钥。

## Workflow 与交付接口

- 独立阶段继续按依赖执行；重试、取消、绝对 deadline 和真实性审查保留。
- 不把生产故障替换为 Mock，不声称模型目录可读等于完整分析可用。
- 最终 Workflow 结果增加 `delivery_summary`：`match_score`（number/null）、`strengths`（string[]）、`gaps`（string[]）、`priority_actions`（string[]）、`changes`（`section/before/after/reason` 对象数组）和 `verified`（boolean）。所有列表与文本均有输出上限。
- `GET /api/runs/{id}` 只向归属 Cookie 会话返回该摘要，与原有报告、结构化简历相同。
- `GET /api/runs/{id}/resume.docx` 只对归属会话、已完成且通过真实性审查的结果开放，返回 `application/vnd.openxmlformats-officedocument.wordprocessingml.document` 和私有 no-store 头。原有 PDF 打印与 HTML 下载保留。
- Word 导出只使用规范化结构化简历；学校、时间、联系信息和事实条目原样保留，不新增事实。采用 A4 单栏样式，不承诺任意长度内容一定为一页。

## UI

- 继承纸白、墨线、单一信号色的设计规则，改善标题、表单说明、移动端布局和结果层级。
- 输入到交付提供清晰路径：“提供简历与岗位 → 查看差距与修改 → 审查并导出”。
- 模型和推理选项完全读取服务器配置；不展示固定供应商域名或过期价格承诺。
- 服务关闭时说明原因并保留演示入口。演示结果显式标识。
- 结果首屏展示匹配度含义、优势、差距、优先修改及事实边界；逐项对照使用 textContent/安全 DOM，不拼接未经转义的 HTML。
- Word 下载按钮仅在可交付完成态可用。失败和取消状态可复用当前表单重试；BYOK 必须由用户重新输入，不持久化密钥。
- 保留 8 阶段详情、历史任务、取消、Markdown 下载、三套模板和本地照片功能。

## 发布与验证

- Python 3.12；锁定可通过项目检查的依赖版本。
- GitHub Actions 运行全套离线测试，明确排除需真实 API 或 Vercel build 目录的测试。
- `.env*`、上传和输出目录、`.venv312`、`.superpowers` 以及维护证据不进入 Git/Vercel 部署。
- Preview 先验证 Mock、会话隔离、结构化摘要、Word/HTML 导出和移动端，再用合成样本验证新网关的真实分析。
- 更新 Vercel 服务端 Base URL；密钥保留现有配置。生产晋升前完成代码检查和 Preview 验证。
- 生产验收记录 Ready 状态、resume.defou.ai 的实际新界面、一个完整真实运行和 Word 下载。服务只在这些检查通过后报告恢复。
