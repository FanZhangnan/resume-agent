import os
import unittest
from unittest.mock import patch

from quota_store import AdmissionDenied, QuotaStore, QuotaUnavailable


class FakeRedisExecutor:
    """Small stateful executor for the command contract emitted by QuotaStore."""

    def __init__(self):
        self.commands = []
        self.now = 0
        self.counts = {}
        self.expiries = {}
        self.active = {}
        self.leases = {}
        self.admissions = {}
        self.runs = {}
        self.history = {}
        self.credentials = {}

    async def __call__(self, command):
        self.commands.append(command)
        operation = command[0]
        if operation == "EVAL":
            script = command[1]
            key_count = int(command[2])
            keys = command[3:3 + key_count]
            args = command[3 + key_count:]
            if "quota-acquire-v1" in script:
                return self._acquire(keys, args)
            if "quota-release-v1" in script:
                return self._release(keys, args)
            if "quota-bind-run-v1" in script:
                return self._bind_run(keys, args)
            if "quota-update-run-v1" in script:
                return self._update_run(keys, args)
            if "quota-delete-run-v1" in script:
                return self._delete_run(keys, args)
            raise AssertionError("unknown Lua contract")
        if operation == "GET":
            self._purge(self.now)
            key = command[1]
            if key in self.credentials:
                return self.credentials[key]
            value = self.counts.get(key)
            return None if value is None else str(value)
        if operation == "SET":
            key, value = command[1], command[2]
            ttl = int(command[4])
            if key in self.credentials:
                return None
            self.credentials[key] = value
            self.expiries[key] = self.now + ttl
            return "OK"
        if operation == "DEL":
            key = command[1]
            existed = key in self.credentials
            self.credentials.pop(key, None)
            self.expiries.pop(key, None)
            return int(existed)
        if operation == "HGET":
            run = self.runs.get(command[1])
            return None if run is None else run.get(command[2])
        if operation == "HGETALL":
            run = self.runs.get(command[1])
            if run is not None:
                return [item for pair in run.items() for item in pair]
            admission = self.admissions.get(command[1])
            if admission is None:
                return []
            fields = {
                "admission_id": admission["admission_id"],
                "lease_key": admission["lease_key"],
                "daily_key": admission["daily_key"],
                "site_key": admission["site_key"],
            }
            return [item for pair in fields.items() for item in pair]
        if operation == "ZREVRANGE":
            values = self.history.get(command[1], {})
            ordered = sorted(values, key=lambda item: (values[item], item), reverse=True)
            start, stop = int(command[2]), int(command[3])
            return ordered[start:] if stop == -1 else ordered[start:stop + 1]
        raise AssertionError(f"unsupported command: {operation}")

    def _purge(self, now):
        self.now = now
        for admission_id, expiry in list(self.active.items()):
            if expiry <= now:
                del self.active[admission_id]
        for key, (_, expiry) in list(self.leases.items()):
            if expiry <= now:
                del self.leases[key]
        for key, expiry in list(self.expiries.items()):
            if expiry <= now:
                self.expiries.pop(key, None)
                self.counts.pop(key, None)
                self.credentials.pop(key, None)

    def _acquire(self, keys, args):
        global_key, lease_key, hourly_key, daily_key, site_key, admission_key = keys
        (
            admission_id,
            raw_now,
            raw_expiry,
            raw_hourly_limit,
            raw_free_limit,
            raw_site_limit,
            raw_funded,
            raw_admission_ttl,
            raw_hourly_ttl,
            raw_daily_ttl,
            session_hash,
            raw_max_concurrent,
        ) = args
        now = int(raw_now)
        expiry = int(raw_expiry)
        hourly_limit = int(raw_hourly_limit)
        free_limit = int(raw_free_limit)
        site_limit = int(raw_site_limit)
        funded = raw_funded == "1"
        self._purge(now)
        free_left = max(0, free_limit - self.counts.get(daily_key, 0))

        if lease_key in self.leases:
            return [0, "ip_concurrent", free_left]
        if len(self.active) >= int(raw_max_concurrent):
            return [0, "global_concurrent", free_left]
        if self.counts.get(hourly_key, 0) >= hourly_limit:
            return [0, "hourly_limit", free_left]
        if funded and self.counts.get(daily_key, 0) >= free_limit:
            return [0, "free_quota_exhausted", 0]
        if funded and self.counts.get(site_key, 0) >= site_limit:
            return [0, "site_quota_exhausted", free_left]

        self.counts[hourly_key] = self.counts.get(hourly_key, 0) + 1
        self.expiries[hourly_key] = now + int(raw_hourly_ttl)
        if funded:
            self.counts[daily_key] = self.counts.get(daily_key, 0) + 1
            self.counts[site_key] = self.counts.get(site_key, 0) + 1
            self.expiries[daily_key] = now + int(raw_daily_ttl)
            self.expiries[site_key] = now + int(raw_daily_ttl)
            free_left -= 1

        self.active[admission_id] = expiry
        self.leases[lease_key] = (admission_id, expiry)
        self.admissions[admission_key] = {
            "admission_id": admission_id,
            "global_key": global_key,
            "lease_key": lease_key,
            "daily_key": daily_key,
            "site_key": site_key,
            "funded": funded,
            "session_hash": session_hash,
            "expiry": now + int(raw_admission_ttl),
        }
        return [1, admission_id, free_left]

    def _release(self, keys, args):
        global_key, admission_key, lease_key, daily_key, site_key = keys
        admission_id, raw_refund = args
        meta = self.admissions.pop(admission_key, None)
        self.active.pop(admission_id, None)
        if meta is None:
            return 0
        if (
            lease_key != meta["lease_key"]
            or daily_key != meta["daily_key"]
            or site_key != meta["site_key"]
        ):
            return 0
        lease = self.leases.get(meta["lease_key"])
        if lease and lease[0] == admission_id:
            del self.leases[meta["lease_key"]]
        if raw_refund == "1" and meta["funded"]:
            for key in (meta["daily_key"], meta["site_key"]):
                self.counts[key] = max(0, self.counts.get(key, 0) - 1)
        self.assert_global_key(global_key, meta)
        return 1

    @staticmethod
    def assert_global_key(global_key, meta):
        if global_key != meta["global_key"]:
            raise AssertionError("release used the wrong global key")

    def _bind_run(self, keys, args):
        admission_key, run_key, history_key = keys
        (
            admission_id,
            run_id,
            session_hash,
            model,
            reasoning,
            created_at,
            raw_score,
            raw_ttl,
            raw_cap,
        ) = args
        meta = self.admissions.get(admission_key)
        if (
            meta is None
            or meta["admission_id"] != admission_id
            or meta["session_hash"] != session_hash
        ):
            return 0
        self.runs[run_key] = {
            "run_id": run_id,
            "session_hash": session_hash,
            "model": model,
            "reasoning": reasoning,
            "created_at": created_at,
            "status": "running",
            "safe_to_deliver": "0",
        }
        values = self.history.setdefault(history_key, {})
        values[run_id] = float(raw_score)
        cap = int(raw_cap)
        while len(values) > cap:
            oldest = min(values, key=lambda item: (values[item], item))
            del values[oldest]
        self.expiries[run_key] = self.now + int(raw_ttl)
        self.expiries[history_key] = self.now + int(raw_ttl)
        return 1

    def _update_run(self, keys, args):
        run = self.runs.get(keys[0])
        if run is None:
            return 0
        run["status"], run["safe_to_deliver"] = args
        return 1

    def _delete_run(self, keys, args):
        run_key, history_key = keys
        session_hash, run_id = args
        run = self.runs.get(run_key)
        if run is None or run["session_hash"] != session_hash:
            return 0
        del self.runs[run_key]
        self.history.get(history_key, {}).pop(run_id, None)
        return 1


class FailingExecutor:
    async def __call__(self, command):
        raise RuntimeError("backend exploded at https://secret.example with token-secret")


class MalformedExecutor:
    async def __call__(self, command):
        return [1, b"\xff", 1]


class MalformedWrapperExecutor:
    async def __call__(self, command):
        return {"unexpected": "backend-shape"}


class QuotaStoreTests(unittest.IsolatedAsyncioTestCase):
    def make_store(self, executor=None, **overrides):
        options = {
            "free_per_day": 2,
            "site_free_per_day": 20,
            "runs_per_hour": 6,
            "mock_per_hour": 20,
            "max_concurrent": 3,
            "session_ttl": 86400,
            "history_cap": 5,
            "admission_ttl": 900,
        }
        options.update(overrides)
        return QuotaStore(
            "https://redis.example",
            "redis-token",
            executor=executor or FakeRedisExecutor(),
            **options,
        )

    async def test_funded_daily_boundary_and_free_left(self):
        store = self.make_store()
        first = await store.acquire("ip-a", "session-a", now=100)
        self.assertEqual(first["free_left"], 1)
        await store.release(first["admission_id"])
        second = await store.acquire("ip-a", "session-a", now=101)
        self.assertEqual(second["free_left"], 0)
        await store.release(second["admission_id"])

        with self.assertRaises(AdmissionDenied) as caught:
            await store.acquire("ip-a", "session-a", now=102)
        self.assertEqual(caught.exception.code, "free_quota_exhausted")
        self.assertEqual(caught.exception.free_left, 0)
        self.assertEqual(await store.free_left("ip-a", now=102), 0)

    async def test_daily_counter_ttl_is_independent_from_session_ttl(self):
        store = self.make_store(free_per_day=1, session_ttl=10)
        admission = await store.acquire("ip-a", "session-a", now=150)
        await store.release(admission["admission_id"])

        with self.assertRaises(AdmissionDenied) as caught:
            await store.acquire("ip-a", "session-a", now=161)
        self.assertEqual(caught.exception.code, "free_quota_exhausted")

    async def test_site_daily_boundary_is_shared_across_ips(self):
        store = self.make_store(free_per_day=10, site_free_per_day=2)
        for ip_hash in ("ip-a", "ip-b"):
            admission = await store.acquire(ip_hash, "session", now=200)
            await store.release(admission["admission_id"])

        with self.assertRaises(AdmissionDenied) as caught:
            await store.acquire("ip-c", "session", now=200)
        self.assertEqual(caught.exception.code, "site_quota_exhausted")
        self.assertEqual(caught.exception.free_left, 10)

    async def test_real_hourly_limit_includes_byok(self):
        store = self.make_store(runs_per_hour=2)
        for _ in range(2):
            admission = await store.acquire("ip-a", "session", funded=False, now=300)
            await store.release(admission["admission_id"])

        with self.assertRaises(AdmissionDenied) as caught:
            await store.acquire("ip-a", "session", funded=False, now=300)
        self.assertEqual(caught.exception.code, "hourly_limit")

    async def test_mock_uses_independent_hourly_limit_and_no_daily_quota(self):
        store = self.make_store(mock_per_hour=2, free_per_day=1)
        for _ in range(2):
            admission = await store.acquire("ip-a", "session", kind="mock", funded=False, now=400)
            self.assertEqual(admission["free_left"], 1)
            await store.release(admission["admission_id"])

        with self.assertRaises(AdmissionDenied) as caught:
            await store.acquire("ip-a", "session", kind="mock", funded=False, now=400)
        self.assertEqual(caught.exception.code, "hourly_limit")

        real = await store.acquire("ip-a", "session", kind="real", now=400)
        self.assertEqual(real["free_left"], 0)

    async def test_byok_does_not_consume_ip_or_site_daily_quota(self):
        store = self.make_store(free_per_day=1, site_free_per_day=1)
        for _ in range(3):
            admission = await store.acquire("ip-a", "session", funded=False, now=500)
            await store.release(admission["admission_id"])
        self.assertEqual(await store.free_left("ip-a", now=500), 1)

        funded = await store.acquire("ip-a", "session", funded=True, now=500)
        self.assertEqual(funded["free_left"], 0)

    async def test_per_ip_and_global_concurrency(self):
        store = self.make_store(max_concurrent=2)
        first = await store.acquire("ip-a", "session-a", funded=False, now=600)
        with self.assertRaises(AdmissionDenied) as caught:
            await store.acquire("ip-a", "session-b", funded=False, now=600)
        self.assertEqual(caught.exception.code, "ip_concurrent")

        second = await store.acquire("ip-b", "session-b", funded=False, now=600)
        with self.assertRaises(AdmissionDenied) as caught:
            await store.acquire("ip-c", "session-c", funded=False, now=600)
        self.assertEqual(caught.exception.code, "global_concurrent")

        self.assertTrue(await store.release(first["admission_id"]))
        third = await store.acquire("ip-c", "session-c", funded=False, now=600)
        self.assertIn("admission_id", third)
        self.assertTrue(await store.release(second["admission_id"]))

    async def test_expired_leases_are_removed_before_admission(self):
        store = self.make_store(max_concurrent=1, admission_ttl=10)
        await store.acquire("ip-a", "session-a", funded=False, now=700)
        recovered = await store.acquire("ip-b", "session-b", funded=False, now=711)
        self.assertIn("admission_id", recovered)

    async def test_refund_restores_daily_counters_but_not_hourly_counter(self):
        store = self.make_store(
            free_per_day=1,
            site_free_per_day=1,
            runs_per_hour=2,
        )
        failed_start = await store.acquire("ip-a", "session", now=800)
        self.assertTrue(await store.release(failed_start["admission_id"], refund_daily=True))

        started = await store.acquire("ip-a", "session", now=800)
        self.assertEqual(started["free_left"], 0)
        await store.release(started["admission_id"])
        with self.assertRaises(AdmissionDenied) as caught:
            await store.acquire("ip-a", "session", funded=False, now=800)
        self.assertEqual(caught.exception.code, "hourly_limit")

    async def test_release_is_idempotent(self):
        store = self.make_store()
        admission = await store.acquire("ip-a", "session", funded=False, now=900)
        self.assertTrue(await store.release(admission["admission_id"]))
        self.assertFalse(await store.release(admission["admission_id"]))

    async def test_command_contract_uses_eval_keys_and_expected_ttls(self):
        executor = FakeRedisExecutor()
        store = self.make_store(executor=executor)
        admission = await store.acquire("ip-hash", "session-hash", now=1000)
        command = executor.commands[0]

        self.assertEqual(command[0], "EVAL")
        self.assertIn("ZREMRANGEBYSCORE", command[1])
        self.assertIn("quota-acquire-v1", command[1])
        self.assertEqual(command[2], 6)
        keys = command[3:9]
        self.assertTrue(any("ip-hash" in key for key in keys))
        self.assertTrue(any("admission" in key for key in keys))
        args = command[9:]
        self.assertIn("900", args)
        self.assertIn("3600", args)
        self.assertIn("86400", args)
        self.assertEqual(args[-1], "3")

        await store.release(admission["admission_id"], refund_daily=True)
        release_command = executor.commands[-1]
        self.assertEqual(release_command[0], "EVAL")
        self.assertIn("quota-release-v1", release_command[1])
        self.assertIn("DECR", release_command[1])
        self.assertEqual(release_command[2], 5)

    async def test_run_history_cap_updates_and_owner_isolation(self):
        store = self.make_store()
        admission = await store.acquire("ip-a", "session-a", funded=False, now=1100)
        for index in range(6):
            await store.bind_run(
                admission["admission_id"],
                f"run-{index}",
                "session-a",
                "gpt-test",
                "high",
                1100 + index,
            )

        runs = await store.list_runs("session-a")
        self.assertEqual([item["run_id"] for item in runs], [
            "run-5", "run-4", "run-3", "run-2", "run-1",
        ])
        self.assertTrue(await store.owns_run("run-5", "session-a"))
        self.assertFalse(await store.owns_run("run-5", "session-b"))

        self.assertTrue(await store.update_run("run-5", "completed", True))
        updated = await store.list_runs("session-a")
        self.assertEqual(updated[0]["status"], "completed")
        self.assertIs(updated[0]["safe_to_deliver"], True)
        self.assertFalse(await store.delete_run("run-5", "session-b"))
        self.assertTrue(await store.delete_run("run-5", "session-a"))
        self.assertFalse(await store.owns_run("run-5", "session-a"))

    async def test_admission_can_only_bind_a_run_to_its_session(self):
        store = self.make_store()
        admission = await store.acquire("ip-a", "session-a", funded=False, now=1150)

        with self.assertRaises(ValueError):
            await store.bind_run(
                admission["admission_id"],
                "run-cross-session",
                "session-b",
                "model",
                "high",
                1150,
            )
        self.assertFalse(await store.owns_run("run-cross-session", "session-b"))

    async def test_run_and_history_contract_use_24_hour_ttl_and_cap(self):
        executor = FakeRedisExecutor()
        store = self.make_store(executor=executor)
        admission = await store.acquire("ip-a", "session-a", funded=False, now=1200)
        await store.bind_run(
            admission["admission_id"], "run-a", "session-a", "model", "xhigh", 1200
        )
        command = executor.commands[-1]
        self.assertEqual(command[0], "EVAL")
        self.assertIn("quota-bind-run-v1", command[1])
        self.assertIn("EXPIRE", command[1])
        self.assertEqual(command[-2:], ["86400", "5"])

    async def test_encrypted_credential_reference_has_lease_ttl_and_can_be_deleted(self):
        executor = FakeRedisExecutor()
        store = self.make_store(executor=executor)
        reference = await store.store_credential("encrypted-secret")
        self.assertNotIn("encrypted-secret", reference)
        self.assertEqual(await store.get_credential(reference), "encrypted-secret")
        self.assertTrue(await store.delete_credential(reference))
        self.assertIsNone(await store.get_credential(reference))
        set_command = executor.commands[0]
        self.assertEqual(set_command[0], "SET")
        self.assertEqual(set_command[3:], ["EX", 900, "NX"])

    async def test_backend_errors_fail_closed_without_exposing_configuration(self):
        store = self.make_store(executor=FailingExecutor())
        with self.assertRaises(QuotaUnavailable) as caught:
            await store.acquire("ip-a", "session", now=1300)
        message = str(caught.exception)
        self.assertNotIn("redis.example", message)
        self.assertNotIn("redis-token", message)
        self.assertNotIn("token-secret", message)
        self.assertEqual(caught.exception.code, "quota_unavailable")

    async def test_malformed_backend_response_fails_closed(self):
        store = self.make_store(executor=MalformedExecutor())
        with self.assertRaises(QuotaUnavailable):
            await store.acquire("ip-a", "session", now=1350)

        wrapper_store = self.make_store(executor=MalformedWrapperExecutor())
        with self.assertRaises(QuotaUnavailable):
            await wrapper_store.get_credential("credential-ref")

    async def test_missing_default_backend_configuration_fails_closed(self):
        with patch.dict(os.environ, {}, clear=True):
            store = QuotaStore(None, None)
            with self.assertRaises(QuotaUnavailable):
                await store.free_left("ip-a", now=1400)


if __name__ == "__main__":
    unittest.main()
