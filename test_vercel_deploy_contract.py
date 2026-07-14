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
from pathlib import Path
from unittest.mock import AsyncMock, patch

os.environ.setdefault("AGENT_WORKFLOW_TEST", "1")


ROOT = os.path.dirname(os.path.abspath(__file__))


def test_application_runtime_has_no_blob_dependency():
    sources = [
        Path(ROOT, "webui", "vercel_server.py"),
        Path(ROOT, "workflows", "resume_workflow.py"),
        Path(ROOT, "run_trace_store.py"),
        Path(ROOT, "quota_store.py"),
    ]
    text = "\n".join(path.read_text(encoding="utf-8") for path in sources)
    assert "vercel.blob" not in text
    assert "BLOB_READ_WRITE_TOKEN" not in text
    assert "list_objects(" not in text
    config = json.loads(Path(ROOT, "vercel.json").read_text(encoding="utf-8"))
    assert "crons" not in config


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
        "cryptography", "fastapi", "httpx", "openai", "pdfplumber", "pydantic",
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
        module.step_require_public_binding,
        module.step_finalize_public_run,
    )
    assert all(step.max_retries == 3 for step in idempotent_steps)


def test_paid_tool_loads_and_decrypts_credential_into_run_settings():
    from public_security import encrypt_api_key
    from quota_store import QuotaStore
    from workflows import resume_workflow as module

    signing_key = "contract-signing-key"
    secret = "sk-contract-secret-value"
    encrypted = encrypt_api_key(secret, signing_key)
    captured = {}

    class FakeStore:
        async def get_credential(self, reference):
            captured["reference"] = reference
            return encrypted

    def run_tool_sync(tool_name, arguments, settings):
        captured["tool_name"] = tool_name
        captured["arguments"] = arguments
        captured["settings"] = settings
        return {"success": True, "value": "ok"}

    with (
        patch.object(QuotaStore, "from_env", return_value=FakeStore()),
        patch.object(module, "_run_tool_sync", side_effect=run_tool_sync),
        patch.dict(os.environ, {"AGENT_RUN_SIGNING_KEY": signing_key}),
    ):
        result = asyncio.run(module._run_tool(
            "extract_resume_info",
            {"resume_text": "resume"},
            "gpt-5.5",
            "xhigh",
            1234.0,
            "credential-reference",
            True,
        ))

    settings = captured["settings"]
    assert captured["reference"] == "credential-reference"
    assert settings.api_key == secret
    assert settings.mock is True
    assert secret not in repr(settings)
    assert secret not in repr(result)


def test_paid_tool_rejects_missing_referenced_credential_without_site_fallback():
    from quota_store import QuotaStore
    from workflows import resume_workflow as module

    credential_ref = "missing-credential-reference"

    class MissingCredentialStore:
        async def get_credential(self, reference):
            assert reference == credential_ref
            return None

    with (
        patch.object(
            QuotaStore, "from_env", return_value=MissingCredentialStore(),
        ),
        patch.object(
            module,
            "_run_tool_sync",
            side_effect=AssertionError("paid tool must not run"),
        ),
    ):
        try:
            asyncio.run(module._run_tool(
                "extract_resume_info",
                {"resume_text": "resume"},
                "gpt-5.5",
                "xhigh",
                1234.0,
                credential_ref,
                False,
            ))
        except RuntimeError as error:
            message = str(error)
            assert message == "workflow credential unavailable"
            assert credential_ref not in message
        else:
            raise AssertionError("missing credential silently used the site key")


def test_finalize_public_run_attempts_all_actions_and_raises_generic_error():
    from workflows import resume_workflow as module

    calls = []
    secret = "credential-secret-must-not-leak"

    class PartlyBrokenStore:
        async def update_run(self, run_id, status, safe_to_deliver):
            calls.append(("update_run", run_id, status, safe_to_deliver))
            raise RuntimeError(secret)

        async def release(self, admission_id, refund_daily=False):
            calls.append(("release", admission_id, refund_daily))
            return True

        async def delete_credential(self, credential_ref):
            calls.append(("delete_credential", credential_ref))
            return True

    try:
        asyncio.run(module._finalize_public_run(
            "run-1",
            "admission-1",
            "credential-1",
            "failed",
            False,
            store=PartlyBrokenStore(),
        ))
    except RuntimeError as error:
        assert str(error) == "public run finalization failed"
        assert secret not in str(error)
    else:
        raise AssertionError("partial finalization failure was silently ignored")

    assert calls == [
        ("update_run", "run-1", "failed", False),
        ("release", "admission-1", False),
        ("delete_credential", "credential-1"),
    ]


def test_finalize_public_run_retries_false_update_before_other_cleanup():
    from workflows import resume_workflow as module

    calls = []

    class NotYetBoundStore:
        async def update_run(self, run_id, status, safe_to_deliver):
            calls.append(("update_run", run_id, status, safe_to_deliver))
            return False

        async def release(self, admission_id, refund_daily=False):
            calls.append(("release", admission_id, refund_daily))
            return True

        async def delete_credential(self, credential_ref):
            calls.append(("delete_credential", credential_ref))
            return True

    with patch.object(module, "_UPDATE_RUN_RETRY_DELAYS", (0, 0)):
        try:
            asyncio.run(module._finalize_public_run(
                "run-race",
                "admission-race",
                "credential-race",
                "completed",
                True,
                store=NotYetBoundStore(),
            ))
        except RuntimeError as error:
            assert str(error) == "public run finalization failed"
        else:
            raise AssertionError("unbound run update was treated as finalized")

    assert [call[0] for call in calls] == [
        "update_run",
        "update_run",
        "update_run",
        "release",
        "delete_credential",
    ]


def test_start_resume_run_rejects_fields_outside_public_payload_allowlist():
    from workflows import resume_workflow as module

    allowed_payload = {
        "resume_text": "resume",
        "jd_text": "jd",
        "model": "gpt-5.5",
        "reasoning": "xhigh",
        "job_search": False,
        "deadline_epoch": 1234.0,
        "admission_id": "admission-1",
        "credential_ref": "credential-1",
        "session_hash": "session-1",
        "mock": False,
    }
    forbidden_value = "secret-value-must-not-leak"

    async def unexpected_start(*_args, **_kwargs):
        raise AssertionError("invalid payload reached Vercel Workflows")

    for forbidden_name in ("api_key", "base_url", "OPENAI_API_KEY"):
        payload = {**allowed_payload, forbidden_name: forbidden_value}
        with patch("vercel.workflow.start", side_effect=unexpected_start):
            try:
                asyncio.run(module.start_resume_run(payload))
            except ValueError as error:
                message = str(error)
                assert message == "invalid workflow payload"
                assert forbidden_name not in message
                assert forbidden_value not in message
            else:
                raise AssertionError("unexpected workflow payload field was accepted")

    incomplete_public_payload = {
        "resume_text": "resume",
        "jd_text": "jd",
        "model": "gpt-5.5",
        "reasoning": "xhigh",
        "credential_ref": "credential-without-binding",
        "mock": False,
    }
    with patch("vercel.workflow.start", side_effect=unexpected_start):
        try:
            asyncio.run(module.start_resume_run(incomplete_public_payload))
        except ValueError as error:
            assert str(error) == "invalid workflow payload"
        else:
            raise AssertionError("unbound credential payload was accepted")


def test_workflow_does_not_finalize_during_sdk_suspension():
    from workflows import resume_workflow as module

    graph = AsyncMock(side_effect=asyncio.CancelledError("Workflow suspended"))
    finalizer = AsyncMock()
    with (
        patch.object(module, "step_run_id", new=AsyncMock(return_value="run-suspend")),
        patch.object(module, "run_workflow_graph", new=graph),
        patch.object(module, "step_finalize_public_run", new=finalizer),
    ):
        try:
            asyncio.run(module.resume_workflow.func({
                "model": "gpt-5.5", "reasoning": "xhigh",
            }))
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("SDK suspension did not propagate")

    finalizer.assert_not_awaited()


def test_workflow_requires_public_binding_before_graph_or_finalizer():
    from workflows import resume_workflow as module

    binding_error = RuntimeError("public run binding unavailable")
    graph = AsyncMock()
    finalizer = AsyncMock()
    payload = {
        "model": "gpt-5.5",
        "reasoning": "xhigh",
        "admission_id": "admission-1",
        "session_hash": "session-1",
    }
    with (
        patch.object(module, "step_run_id", new=AsyncMock(return_value="run-1")),
        patch.object(
            module,
            "step_require_public_binding",
            new=AsyncMock(side_effect=binding_error),
        ),
        patch.object(module, "run_workflow_graph", new=graph),
        patch.object(module, "step_finalize_public_run", new=finalizer),
    ):
        try:
            asyncio.run(module.resume_workflow.func(payload))
        except RuntimeError as error:
            assert error is binding_error
        else:
            raise AssertionError("unbound public workflow was allowed to run")

    graph.assert_not_awaited()
    finalizer.assert_not_awaited()


def test_workflow_finalizes_terminal_result_and_preserves_graph_error():
    from workflows import resume_workflow as module

    payload = {
        "model": "gpt-5.5",
        "reasoning": "xhigh",
        "admission_id": "admission-1",
        "credential_ref": "credential-1",
        "session_hash": "session-1",
    }
    terminal = {
        "status": "deadline_exceeded",
        "safe_to_deliver": False,
    }
    finalizer = AsyncMock()
    with (
        patch.object(module, "step_run_id", new=AsyncMock(return_value="run-1")),
        patch.object(
            module, "step_require_public_binding", new=AsyncMock(return_value=True),
        ),
        patch.object(module, "run_workflow_graph", new=AsyncMock(return_value=terminal)),
        patch.object(module, "step_finalize_public_run", new=finalizer),
    ):
        result = asyncio.run(module.resume_workflow.func(payload))

    assert result is terminal
    finalizer.assert_awaited_once_with(
        "run-1", "admission-1", "credential-1", "deadline_exceeded", False,
    )

    graph_error = RuntimeError("graph failed before delivery")
    failed_finalizer = AsyncMock(side_effect=RuntimeError("housekeeping failed"))
    with (
        patch.object(module, "step_run_id", new=AsyncMock(return_value="run-2")),
        patch.object(
            module, "step_require_public_binding", new=AsyncMock(return_value=True),
        ),
        patch.object(module, "run_workflow_graph", new=AsyncMock(side_effect=graph_error)),
        patch.object(module, "step_finalize_public_run", new=failed_finalizer),
    ):
        try:
            asyncio.run(module.resume_workflow.func(payload))
        except RuntimeError as error:
            assert error is graph_error
        else:
            raise AssertionError("graph error was masked")

    failed_finalizer.assert_awaited_once_with(
        "run-2", "admission-1", "credential-1", "failed", False,
    )


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
