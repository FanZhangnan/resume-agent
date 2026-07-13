"""Static deployment contracts that mirror Vercel's build/runtime boundaries."""

import asyncio
import importlib
import inspect
import json
import os
import subprocess
import sys
import threading
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


def test_paid_llm_steps_disable_sdk_retries():
    from workflows import resume_workflow as module

    paid_steps = (
        module.step_extract,
        module.step_analyze_jd,
        module.step_match,
        module.step_suggest,
        module.step_verify,
    )
    assert all(step.max_retries == 0 for step in paid_steps)

    idempotent_steps = (
        module.step_trace_stage,
        module.step_trace_cancelled,
        module.step_run_boundary,
    )
    assert all(step.max_retries == 3 for step in idempotent_steps)


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


def test_worker_guard_serializes_post_callbacks_with_asyncio_context():
    import sniffio
    from workflows import vercel_worker as worker

    active = 0
    max_active = 0
    libraries = []
    state_lock = threading.Lock()

    async def inner_app(_scope, _receive, _send):
        nonlocal active, max_active
        try:
            library = await asyncio.to_thread(sniffio.current_async_library)
        except sniffio.AsyncLibraryNotFoundError:
            library = None
        libraries.append(library)
        with state_lock:
            active += 1
            max_active = max(max_active, active)
        try:
            await asyncio.sleep(0.01)
        finally:
            with state_lock:
                active -= 1

    async def scenario():
        guarded_app = worker._guard_worker_app(inner_app)

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(_message):
            return None

        scope = {"type": "http", "method": "POST", "path": "/"}
        await asyncio.gather(
            guarded_app(scope, receive, send),
            guarded_app(scope, receive, send),
        )

    asyncio.run(scenario())
    assert max_active == 1
    assert libraries == ["asyncio", "asyncio"]


def test_worker_guard_passes_non_post_requests_without_waiting():
    from workflows import vercel_worker as worker

    post_entered = asyncio.Event()
    release_post = asyncio.Event()
    get_completed = asyncio.Event()

    async def inner_app(scope, _receive, _send):
        if scope.get("method") == "POST":
            post_entered.set()
            await release_post.wait()
        else:
            get_completed.set()

    async def scenario():
        guarded_app = worker._guard_worker_app(inner_app)

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(_message):
            return None

        post_task = asyncio.create_task(
            guarded_app({"type": "http", "method": "POST"}, receive, send)
        )
        await post_entered.wait()
        try:
            await asyncio.wait_for(
                guarded_app({"type": "http", "method": "GET"}, receive, send),
                timeout=0.1,
            )
            assert get_completed.is_set()
        finally:
            release_post.set()
            await post_task

    asyncio.run(scenario())


def test_worker_guard_releases_post_lock_when_waiter_is_cancelled():
    from workflows import vercel_worker as worker

    class TrackingLock:
        def __init__(self):
            self._lock = threading.Lock()
            self.attempted_while_locked = threading.Event()
            self.acquired_after_wait = threading.Event()

        def acquire(self, blocking=True):
            was_locked = self._lock.locked()
            if was_locked:
                self.attempted_while_locked.set()
            acquired = self._lock.acquire(blocking=blocking)
            if was_locked and acquired:
                self.acquired_after_wait.set()
            return acquired

        def locked(self):
            return self._lock.locked()

        def release(self):
            self._lock.release()

    tracking_lock = TrackingLock()
    first_entered = asyncio.Event()
    release_first = asyncio.Event()

    async def inner_app(scope, _receive, _send):
        if scope.get("request") == "first":
            first_entered.set()
            await release_first.wait()

    async def scenario():
        guarded_app = worker._guard_worker_app(inner_app)

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(_message):
            return None

        first_task = asyncio.create_task(
            guarded_app({"method": "POST", "request": "first"}, receive, send)
        )
        await first_entered.wait()
        waiting_task = asyncio.create_task(
            guarded_app({"method": "POST", "request": "waiting"}, receive, send)
        )
        attempted = await asyncio.to_thread(
            tracking_lock.attempted_while_locked.wait, 0.5
        )
        assert attempted

        waiting_task.cancel()
        try:
            await waiting_task
        except asyncio.CancelledError:
            pass

        release_first.set()
        await first_task
        await asyncio.to_thread(tracking_lock.acquired_after_wait.wait, 0.1)
        assert not tracking_lock.locked()

    original_lock = worker._post_lock
    worker._post_lock = tracking_lock
    try:
        asyncio.run(scenario())
    finally:
        if tracking_lock.locked():
            tracking_lock.release()
        worker._post_lock = original_lock


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
