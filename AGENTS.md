# Repository Guidelines

## Project Structure & Module Organization

`agent.py` contains the CLI and ReAct orchestration. Supporting gateway, configuration, prompt, and parsing code lives in `llm_client.py`, `config.py`, `prompts.py`, and `utils.py`. Tool implementations are grouped by responsibility under `tools/`; keep schemas and function registration synchronized in `tools/__init__.py`. The FastAPI backend is `webui/server.py`, while `webui/static/index.html` contains the inline HTML, CSS, and JavaScript frontend. Demo inputs live in `samples/`; executable checks are the root-level `test_*.py` files. Treat `output/`, `output_test/`, uploads, runs, and quota files as generated data.

## Build, Test, and Development Commands

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
AGENT_MOCK=1 python agent.py --demo   # offline CLI smoke test
python webui/server.py                # serve http://127.0.0.1:7860
AGENT_MOCK=1 python test_tools.py     # tool registry and implementations
AGENT_MOCK=1 python test_agent.py     # offline agent workflow
python test_models.py                 # model and reasoning combinations
python test_llm.py                    # live gateway check; requires an API key
```

There is no separate build step. On macOS, `启动WebUI.command` provides the same Web UI setup and launch flow.

## Coding Style & Naming Conventions

Use four-space Python indentation, `snake_case` for functions and variables, `PascalCase` for classes, `UPPER_SNAKE_CASE` for constants, and a leading underscore for internal helpers. Match the surrounding bilingual style: identifiers and serialized fields are English; user-facing copy, comments, and docstrings are mainly Chinese. No formatter or linter is configured, so keep diffs focused and avoid unrelated reformatting. Tool APIs should retain the existing `{"success": ..., ...}` dictionary contract. For frontend work, use `camelCase` JavaScript, kebab-case CSS names, and follow `webui/design-philosophy.md`.

## Testing Guidelines

Tests are executable scripts using built-in assertions, not a configured pytest or unittest suite. Name new files `test_<area>.py` and test functions `test_<behavior>`. Run the two mock-mode checks before submitting; reserve `test_llm.py` for intentional network testing. No coverage threshold is defined, but changes should exercise success and failure paths without requiring live credentials.

## Commit & Pull Request Guidelines

History uses concise Chinese, outcome-focused subjects such as `修复报告截断，完善用户追问闭环`; Conventional Commit prefixes are not used. Keep each commit logically scoped. Pull requests should explain behavior changes, list commands run, link relevant issues, call out configuration changes, and include screenshots for Web UI changes.

## Security & Configuration

Never commit `.env`, `.claude/`, API keys, uploaded resumes, generated reports, or `webui/quota.json`. Avoid logging credentials or personal resume data. Public deployments must use HTTPS; enable `AGENT_TRUST_PROXY=1` only behind a trusted reverse proxy.
