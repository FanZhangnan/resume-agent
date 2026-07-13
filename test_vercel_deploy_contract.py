"""Static deployment contracts that mirror Vercel's build/runtime boundaries."""

import asyncio
import importlib
import inspect
import json
import os
import subprocess
import sys
import tomllib

os.environ.setdefault("AGENT_WORKFLOW_TEST", "1")


ROOT = os.path.dirname(os.path.abspath(__file__))


def test_vercel_config_uses_ga_services_with_private_workflow_trigger():
    with open(os.path.join(ROOT, "vercel.json"), encoding="utf-8") as handle:
        config = json.load(handle)
    assert "experimentalServices" not in config
    services = config.get("services") or {}
    web = services.get("web") or {}
    assert web.get("root") == "."
    assert web.get("entrypoint") == "webui/vercel_server.py"
    assert {
        "source": "/(.*)",
        "destination": {"service": "web"},
    } in (config.get("rewrites") or [])

    worker = services.get("resume_workflow") or {}
    assert worker.get("root") == "."
    assert worker.get("entrypoint") == "workflows/vercel_worker.py"
    trigger = (
        (worker.get("functions") or {})
        .get("workflows/vercel_worker.py", {})
        .get("experimentalTriggers", [])
    )
    assert trigger == [{"type": "queue/v2beta", "topic": "__wkf_*"}]

    required_excludes = ("env", ".claude", ".playwright-cli", "output", "test_")
    for name, service in (("web", web), ("resume_workflow", worker)):
        entrypoint = service.get("entrypoint")
        function_config = (service.get("functions") or {}).get(entrypoint, {})
        excluded = function_config.get("excludeFiles")
        assert isinstance(excluded, str), (
            name, "@vercel/python only applies excludeFiles when it is a string"
        )
        assert all(token in excluded for token in required_excludes), (name, excluded)


def test_pyproject_declares_all_runtime_dependencies():
    with open(os.path.join(ROOT, "pyproject.toml"), "rb") as handle:
        project = tomllib.load(handle)["project"]
    dependencies = project.get("dependencies") or []
    normalized = {
        item.split(";", 1)[0].split("[", 1)[0].split("=", 1)[0]
        .split(">", 1)[0].split("<", 1)[0].strip().lower()
        for item in dependencies
    }
    required = {
        "fastapi", "httpx", "openai", "pdfplumber", "pydantic",
        "python-docx", "python-multipart", "uvicorn", "vercel",
    }
    assert required <= normalized, required - normalized


def test_workflow_body_has_no_blob_client_io():
    from vercel._internal.workflow.py_sandbox import workflow_sandbox
    from workflows import resume_workflow as module

    source = inspect.getsource(module.resume_workflow.func)
    assert "TraceStore" not in source
    assert hasattr(module, "step_trace_stage")
    assert hasattr(module, "step_trace_cancelled")

    with workflow_sandbox(random_seed="deploy-contract"):
        sandboxed_module = importlib.import_module("workflows.resume_workflow")
        trace = sandboxed_module.RunTrace("run-contract")
    assert trace._run_id == "run-contract"


def test_ga_worker_adapter_registers_workflow_subscriptions():
    source_path = os.path.join(ROOT, "workflows", "vercel_worker.py")
    assert os.path.isfile(source_path)
    env = os.environ.copy()
    env.pop("AGENT_WORKFLOW_TEST", None)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from vercel.workers import has_subscriptions; "
                "import workflows.vercel_worker as worker; "
                "assert has_subscriptions(); assert callable(worker.app)"
            ),
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_boundary_step_failure_is_fail_closed():
    from workflows import resume_workflow as module

    class BrokenBoundaryStep:
        async def __call__(self, *_args, **_kwargs):
            raise RuntimeError("boundary step failed after retries")

    async def scenario():
        original = module.step_run_boundary
        module.step_run_boundary = BrokenBoundaryStep()
        try:
            await module.RunTrace("run-fail-closed").check_boundary(0)
        finally:
            module.step_run_boundary = original

    try:
        asyncio.run(scenario())
    except RuntimeError as error:
        assert "boundary step failed" in str(error)
    else:
        raise AssertionError("boundary failure was silently ignored")


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"\n{len(tests)} Vercel deploy-contract tests passed")
