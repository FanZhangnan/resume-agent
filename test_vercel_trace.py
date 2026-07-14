"""Offline contract tests for the Redis-backed Vercel stage trace store."""

import asyncio
import json

from quota_store import QuotaStore, QuotaUnavailable
from run_trace_store import MissingRun, TraceStore
from test_quota_store import FakeRedisExecutor, FailingExecutor


def run(coro):
    return asyncio.run(coro)


async def bound_trace(run_id="run-a", *, created_at=1000, session_ttl=86400):
    redis = FakeRedisExecutor()
    quota = QuotaStore(
        "https://redis.example",
        "redis-token",
        session_ttl=session_ttl,
        executor=redis,
    )
    admission = await quota.acquire(
        "ip-a", "session-a", funded=False, now=created_at,
    )
    await quota.bind_run(
        admission["admission_id"],
        run_id,
        "session-a",
        "gpt-5.5",
        "xhigh",
        created_at,
    )
    return TraceStore(quota), quota, redis


def test_stages_are_isolated_redacted_and_read_once():
    async def scenario():
        trace, _, redis = await bound_trace()
        await trace.write_stage(
            "run-a", 2,
            {"status": "running", "resume_text": "PRIVATE", "attempt": 1},
        )
        await trace.write_stage(
            "run-a", 4,
            {"status": "completed", "jd_text": "PRIVATE-JD"},
        )
        redis.commands.clear()
        stages = await trace.read_stages("run-a")
        assert stages == {
            2: {"status": "running", "attempt": 1},
            4: {"status": "completed"},
        }
        assert "PRIVATE" not in json.dumps(stages)
        reads = [command for command in redis.commands if command[0] == "HGETALL"]
        assert reads == [["HGETALL", "ra:run:run-a"]]
    run(scenario())


def test_parallel_stages_update_distinct_hash_fields():
    async def scenario():
        trace, _, redis = await bound_trace("parallel")
        await asyncio.gather(
            trace.write_stage("parallel", 2, {"status": "completed"}),
            trace.write_stage("parallel", 4, {"status": "completed"}),
        )
        fields = redis.runs["ra:run:parallel"]
        assert "trace:stage:2" in fields
        assert "trace:stage:4" in fields
    run(scenario())


def test_meta_roundtrip_is_sanitized():
    async def scenario():
        trace, _, _ = await bound_trace("meta")
        assert await trace.write_meta(
            "meta",
            {"model": "gpt-5.5", "reasoning": "xhigh", "resume": "PRIVATE"},
        ) is True
        meta = await trace.read_meta("meta")
        assert meta == {"model": "gpt-5.5", "reasoning": "xhigh"}
    run(scenario())


def test_cancel_marker_distinguishes_active_cancelled_and_missing_runs():
    async def scenario():
        trace, _, _ = await bound_trace("cancel")
        assert await trace.is_cancelled("cancel") is False
        assert await trace.write_cancel("cancel") is True
        assert await trace.is_cancelled("cancel") is True
        try:
            await trace.is_cancelled("missing")
        except MissingRun:
            pass
        else:
            raise AssertionError("missing run must fail closed")
    run(scenario())


def test_cancel_backend_failure_is_not_treated_as_active():
    async def scenario():
        quota = QuotaStore(
            "https://redis.example", "redis-token", executor=FailingExecutor(),
        )
        trace = TraceStore(quota)
        try:
            await trace.is_cancelled("outage")
        except QuotaUnavailable:
            pass
        else:
            raise AssertionError("Redis outage must fail closed")
    run(scenario())


def test_trace_writes_do_not_extend_absolute_expiry():
    async def scenario():
        trace, _, redis = await bound_trace(
            "expiry", created_at=2000, session_ttl=3600,
        )
        run_key = "ra:run:expiry"
        assert redis.expiries[run_key] == 5600
        await trace.write_stage("expiry", 2, {"status": "completed"})
        await trace.write_meta("expiry", {"status": "running"})
        assert redis.expiries[run_key] == 5600
    run(scenario())


def test_deleted_run_cannot_be_recreated_by_late_trace_or_cancel_write():
    async def scenario():
        trace, quota, redis = await bound_trace("deleted")
        assert await quota.delete_run("deleted", "session-a") is True
        assert await trace.write_stage(
            "deleted", 2, {"status": "completed"},
        ) is False
        assert await trace.write_cancel("deleted") is False
        assert "ra:run:deleted" not in redis.runs
    run(scenario())


def test_invalid_stage_id_is_rejected_before_redis_write():
    async def scenario():
        trace, _, redis = await bound_trace("stage-id")
        redis.commands.clear()
        try:
            await trace.write_stage("stage-id", 9, {"status": "running"})
        except ValueError:
            pass
        else:
            raise AssertionError("stage id outside 1..8 must fail")
        assert redis.commands == []
    run(scenario())


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"\n{len(tests)} vercel-trace tests passed")
