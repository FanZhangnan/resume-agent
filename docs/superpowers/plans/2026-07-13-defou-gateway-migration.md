# Defou Gateway Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make defou the only gateway used by runtime, BYOK, documentation, deployment helpers, and Vercel while preserving the existing UI and eight-stage behavior.

**Architecture:** Centralize gateway normalization and allowlisting in `config.py`; every caller consumes that single policy. Keep secrets in `OPENAI_API_KEY`, pass BYOK only to the per-run child process, and configure Vercel with the fixed `/v1` endpoint. Treat documentation and helper scripts as part of the deployable contract and verify the active tree contains no obsolete gateway label.

**Tech Stack:** Python 3.12, FastAPI, OpenAI Python SDK, executable assertion-based tests, Bash deployment helpers, Vercel CLI 55, Vercel Python Workflows.

---

## File Map

- Modify `config.py`: fixed defou constants, URL normalization, host allowlist, and generic key loading.
- Modify `test_models.py`: gateway policy and local Web API contract tests.
- Modify `webui/server.py`: apply centralized gateway validation to BYOK without changing layout or workflow behavior.
- Modify `webui/static/index.html`: show the correct defou `/v1` endpoint in the existing advanced setting.
- Modify `llm_client.py`: generic `OPENAI_API_KEY` setup guidance.
- Modify `test_agent.py`: live-test key lookup and instructions.
- Modify `test_trace_catalog.py`: provider-neutral synthetic secret fixture.
- Modify `README.md`: installation, environment, examples, and troubleshooting.
- Modify `docs/deployment-vercel.md`: Vercel environment contract and acceptance wording.
- Modify `docs/superpowers/specs/2026-07-13-vercel-public-quota-sessions-design.md`: site-funded key variable.
- Add `部署上线Vercel.command`: tracked production deployment helper already approved by the user.
- Add `配置密钥并重新部署.command`: tracked defou key synchronization helper already approved by the user.

### Task 1: Centralize the defou gateway policy

**Files:**
- Modify: `test_models.py`
- Modify: `config.py:27-32`

- [ ] **Step 1: Write failing gateway-policy tests**

Add these tests to `test_models.py` and invoke them from `main()`:

```python
def test_gateway_policy_uses_defou_v1_only():
    expected = "https://api.wangdefou.studio/v1"
    assert config.DEFOU_API_BASE_URL == expected
    assert config.normalize_gateway_base_url("") == expected
    assert config.normalize_gateway_base_url("https://api.wangdefou.studio") == expected
    assert config.normalize_gateway_base_url("https://api.wangdefou.studio/") == expected
    assert config.normalize_gateway_base_url(expected + "/") == expected


def test_gateway_policy_rejects_other_hosts_and_paths():
    for value in (
        "http://api.wangdefou.studio/v1",
        "https://example.com/v1",
        "https://api.wangdefou.studio/v2",
        "https://api.wangdefou.studio/v1?debug=1",
    ):
        try:
            config.normalize_gateway_base_url(value)
        except ValueError:
            continue
        raise AssertionError(f"网关地址应被拒绝：{value}")


def test_runtime_source_reads_only_the_generic_key():
    source = (Path(__file__).parent / "config.py").read_text(encoding="utf-8")
    obsolete_name = "ZEN" + "MUX_API_KEY"
    assert 'os.environ.get("OPENAI_API_KEY", "")' in source
    assert obsolete_name not in source
```

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```bash
AGENT_MOCK=1 .venv312/bin/python test_models.py
```

Expected: FAIL because `DEFOU_API_BASE_URL` and
`normalize_gateway_base_url()` do not exist and the obsolete key fallback is
still present.

- [ ] **Step 3: Implement the centralized gateway policy**

Replace the API configuration block in `config.py` with:

```python
DEFOU_API_ORIGIN = "https://api.wangdefou.studio"
DEFOU_API_BASE_URL = f"{DEFOU_API_ORIGIN}/v1"


def normalize_gateway_base_url(value=None):
    candidate = str(value or "").strip().rstrip("/")
    if not candidate or candidate == DEFOU_API_ORIGIN:
        return DEFOU_API_BASE_URL
    if candidate == DEFOU_API_BASE_URL:
        return DEFOU_API_BASE_URL
    raise ValueError("API 网关仅支持 https://api.wangdefou.studio/v1")


API_BASE_URL = normalize_gateway_base_url(os.environ.get("AGENT_BASE_URL"))
API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
```

- [ ] **Step 4: Run focused tests and verify GREEN**

Run:

```bash
AGENT_MOCK=1 .venv312/bin/python test_models.py
AGENT_MOCK=1 .venv312/bin/python test_runtime_policy.py
```

Expected: both exit 0; model/reasoning behavior remains unchanged.

- [ ] **Step 5: Commit the policy unit**

```bash
git add config.py test_models.py
git commit -m "收口得否网关配置"
```

### Task 2: Enforce the fixed gateway on local BYOK

**Files:**
- Modify: `test_models.py`
- Modify: `webui/server.py:63-66,79-89,518-595`
- Modify: `webui/static/index.html:430-445`

- [ ] **Step 1: Write failing local-Web gateway tests**

Add to `test_models.py` and call both tests from `main()`:

```python
def test_web_gateway_resolution_normalizes_defou_root():
    expected = config.DEFOU_API_BASE_URL
    assert server._resolve_gateway_base_url("") == expected
    assert server._resolve_gateway_base_url("https://api.wangdefou.studio") == expected
    assert server._resolve_gateway_base_url(expected) == expected


def test_web_gateway_resolution_rejects_other_hosts():
    try:
        server._resolve_gateway_base_url("https://example.com/v1")
    except HTTPException as error:
        assert error.status_code == 400
        return
    raise AssertionError("非得否网关应被拒绝")
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
AGENT_MOCK=1 .venv312/bin/python test_models.py
```

Expected: FAIL because `server._resolve_gateway_base_url` is absent.

- [ ] **Step 3: Route BYOK through the central policy**

Remove `BASE_URL_RE` from `webui/server.py` and add:

```python
def _resolve_gateway_base_url(value):
    try:
        return agent_config.normalize_gateway_base_url(value)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
```

In `run()`, replace the regex check with:

```python
base_url = _resolve_gateway_base_url(base_url)
```

In the BYOK environment block, keep the key in the child environment only and
always bind the normalized URL:

```python
if byok:
    env["OPENAI_API_KEY"] = api_key
    env["AGENT_BASE_URL"] = base_url
```

Do not change the current form layout, quota rules, session behavior, or child
process interface.

- [ ] **Step 4: Update the existing endpoint hint**

Change only the `byok-base` placeholder in `webui/static/index.html`:

```html
<input type="text" id="byok-base" placeholder="https://api.wangdefou.studio/v1">
```

- [ ] **Step 5: Verify the local Web unit**

Run:

```bash
AGENT_MOCK=1 .venv312/bin/python test_models.py
AGENT_MOCK=1 .venv312/bin/python test_web_trace_ui.py
```

Expected: both exit 0.

- [ ] **Step 6: Commit the BYOK policy**

```bash
git add test_models.py webui/server.py webui/static/index.html
git commit -m "限制BYOK仅使用得否网关"
```

### Task 3: Remove obsolete runtime guidance and fixtures

**Files:**
- Modify: `llm_client.py:102-108`
- Modify: `test_agent.py:274-291`
- Modify: `test_trace_catalog.py:44-47`

- [ ] **Step 1: Update the missing-key guidance**

Use this error in `LLMClient.__init__`:

```python
raise ValueError(
    "未检测到API密钥！请先在终端执行：\n"
    "export OPENAI_API_KEY=你的得否网关密钥\n"
    "不联网体验演示模式：AGENT_MOCK=1 python agent.py --demo"
)
```

- [ ] **Step 2: Update the executable live-test gate**

In `test_agent.py`, read only the generic key and print only the generic setup
command:

```python
api_key = os.environ.get("OPENAI_API_KEY", "")
if os.environ.get("AGENT_MOCK", "") == "1" or not api_key:
    print("\n⏭️ 跳过真实API测试（未设置API密钥或处于Mock模式）")
    print("如需运行真实测试，请执行：")
    print("export OPENAI_API_KEY=你的得否网关密钥 && python test_agent.py")
    return
```

- [ ] **Step 3: Make the trace fixture provider-neutral**

In `test_trace_catalog.py` use:

```python
non_sk_secret = "gateway-live-secret-without-known-prefix"
```

- [ ] **Step 4: Run runtime and redaction tests**

```bash
AGENT_MOCK=1 .venv312/bin/python test_agent.py
AGENT_MOCK=1 .venv312/bin/python test_trace_catalog.py
```

Expected: both exit 0; live tests are skipped and both synthetic secret forms
remain absent from Trace output.

- [ ] **Step 5: Commit the runtime cleanup**

```bash
git add llm_client.py test_agent.py test_trace_catalog.py
git commit -m "清理旧网关运行提示"
```

### Task 4: Update documentation and deployment helpers

**Files:**
- Modify: `README.md`
- Modify: `docs/deployment-vercel.md`
- Modify: `docs/superpowers/specs/2026-07-13-vercel-public-quota-sessions-design.md`
- Add: `部署上线Vercel.command`
- Add: `配置密钥并重新部署.command`

- [ ] **Step 1: Update active documentation contracts**

Apply these exact configuration values throughout the three Markdown files:

```text
OPENAI_API_KEY=你的得否网关密钥
AGENT_BASE_URL=https://api.wangdefou.studio/v1
```

Remove claims of key precedence or compatibility. In the public-quota design,
site-funded runs read `OPENAI_API_KEY`; BYOK remains encrypted per run and uses
the fixed server-side gateway.

- [ ] **Step 2: Make the key synchronization helper defou-only**

Keep the existing no-echo behavior in `配置密钥并重新部署.command`, but read
only the generic key and force the verified endpoint:

```bash
API_KEY_VALUE="$(get_env OPENAI_API_KEY)"
BASE_URL_VALUE="https://api.wangdefou.studio/v1"

if [ -z "$API_KEY_VALUE" ]; then
  echo "❌ 本地 .env 里没找到 OPENAI_API_KEY，请先补上再运行"
  read -n 1 -s -r -p "按任意键关闭..."
  exit 1
fi
```

Change `set_env` to accept the target environment, then write
`OPENAI_API_KEY` and `AGENT_BASE_URL` to Preview and Production without
printing values:

```bash
set_env() {
  name="$1"
  value="$2"
  environment="$3"
  $VERCEL env rm "$name" "$environment" --yes >/dev/null 2>&1 || true
  printf '%s' "$value" | $VERCEL env add "$name" "$environment" >/dev/null
  echo "✅ 已写入 $name（$environment）"
}

for environment in preview production; do
  set_env OPENAI_API_KEY "$API_KEY_VALUE" "$environment"
  set_env AGENT_BASE_URL "$BASE_URL_VALUE" "$environment"
done
```

- [ ] **Step 3: Audit both deployment helpers before tracking**

Run:

```bash
bash -n '部署上线Vercel.command'
bash -n '配置密钥并重新部署.command'
if rg -n -i 'sk-[a-z0-9_-]{16,}|gho_[a-z0-9]{16,}' \
  '部署上线Vercel.command' '配置密钥并重新部署.command'; then exit 1; fi
```

Expected: both syntax checks exit 0 and the secret-literal scan prints no
matches.

- [ ] **Step 4: Commit docs and approved helpers**

```bash
git add README.md docs/deployment-vercel.md \
  docs/superpowers/specs/2026-07-13-vercel-public-quota-sessions-design.md \
  '部署上线Vercel.command' '配置密钥并重新部署.command'
git commit -m "更新得否网关部署说明"
```

### Task 5: Migrate Vercel configuration and deploy Mock Preview

**Files:**
- No source changes

- [ ] **Step 1: Verify the active Vercel project**

```bash
vercel whoami
vercel project inspect resume-agent
vercel env ls
```

Expected: authenticated account, project `resume-agent`, and no obsolete key
entry. If the obsolete entry remains, remove it from Preview and Production
using its name from the inventory before continuing.

- [ ] **Step 2: Force the fixed endpoint in both environments**

```bash
vercel env add AGENT_BASE_URL preview \
  --value 'https://api.wangdefou.studio/v1' --force --yes
vercel env add AGENT_BASE_URL production \
  --value 'https://api.wangdefou.studio/v1' --force --yes
```

Expected: both commands report the environment value was saved. Do not pass a
secret with `--value`.

- [ ] **Step 3: Deploy Preview from the verified source tree**

```bash
vercel deploy --target=preview
```

Expected: deployment reaches `READY`; Preview retains `AGENT_MOCK=1` and
`AGENT_WORKFLOW_PARALLEL=0`.

- [ ] **Step 4: Run protected API and eight-stage Mock acceptance**

Use `vercel curl --deployment <preview-url>` to check `/api/status` and
`/api/config`, then submit `samples/sample_resume.txt` with
`samples/sample_jd.txt`, `gpt-5.5`, and `xhigh`. Poll with the returned bearer
token until terminal.

Expected: stages 1, 2, 4, 5, 6, 7, and 8 complete; stage 3 is skipped with
`jd_supplied`; report length is nonzero; `completed` or a verification-driven
`partial` is acceptable.

- [ ] **Step 5: Scan Preview logs**

```bash
vercel logs --deployment <preview-url> --since 20m --level error --expand
vercel logs --deployment <preview-url> --since 20m --query '409 Conflict' --expand
vercel logs --deployment <preview-url> --since 20m --query 'AsyncLibraryNotFoundError' --expand
```

Expected: no application errors, Workflow conflicts, or async runtime errors.

### Task 6: Full verification, push, and Production gate

**Files:**
- Verify all files modified in Tasks 1-4

- [ ] **Step 1: Verify the active tree has no obsolete gateway label**

Run the scan without embedding the obsolete label in tracked documentation:

```bash
legacy_pattern="$(printf '\172\145\156\155\165\170')"
if git grep -in "$legacy_pattern"; then exit 1; fi
if rg -n -i "$legacy_pattern" \
  '部署上线Vercel.command' '配置密钥并重新部署.command'; then exit 1; fi
```

Expected: both scans print no matches and exit through the no-match path.
The untracked Claude handoff document and the physical parent directory are
outside this acceptance scope by approved design.

- [ ] **Step 2: Run the complete offline verification set**

```bash
for test_file in \
  test_tools.py test_agent.py test_model_policy.py test_models.py \
  test_runtime_policy.py test_contracts.py test_scoring.py test_pipeline.py \
  test_trace_catalog.py test_run_security.py test_vercel_trace.py \
  test_web_trace_ui.py test_vercel_ui.py test_vercel_workflow.py \
  test_vercel_api.py test_vercel_deploy_contract.py; do
  AGENT_MOCK=1 .venv312/bin/python "$test_file" || exit 1
done
.venv312/bin/python -m compileall -q .
git diff --check
```

Expected: every script exits 0, compilation is quiet, and diff check is clean.

- [ ] **Step 3: Inspect scope and push the current branch**

```bash
git status --short
git diff origin/codex/reliability-live-jobs...HEAD --stat
git push -u origin codex/reliability-live-jobs
```

Expected: only the approved untracked Claude handoff document remains outside
Git, and the branch push succeeds.

- [ ] **Step 4: Gate Production on a rotated generic key**

Verify `vercel env ls` shows sensitive `OPENAI_API_KEY` for Production and that
Production has no `AGENT_MOCK`. If the key is absent, stop and ask the operator
to add it directly in Vercel without pasting it into chat.

- [ ] **Step 5: Promote and run one live acceptance only after the gate passes**

```bash
vercel deploy --prod
```

Submit the fixed sample pair with `gpt-5.5/xhigh`, poll to terminal, inspect all
eight stage documents, and scan logs for errors and secrets. Expected: stages
1, 2, 4, 5, 6, 7, and 8 complete; stage 3 is the only expected skip; the report
is nonempty and any `partial` status is caused only by explicit verification
findings.
