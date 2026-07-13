"""Static deployment contracts that mirror Vercel's build/runtime boundaries."""

import asyncio
import importlib
import inspect
import json
import os
import tomllib

os.environ.setdefault("AGENT_WORKFLOW_TEST", "1")


ROOT = os.path.dirname(os.path.abspath(__file__))


def test_vercel_config_does_not_mix_services_and_functions():
    with open(os.path.join(ROOT, "vercel.json"), encoding="utf-8") as handle:
        config = json.load(handle)
    assert not (
        "experimentalServices" in config and "functions" in config
    ), "Vercel rejects experimentalServices together with functions"


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
