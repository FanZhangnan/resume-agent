"""Offline tests for the Private Blob stage trace store with an injected fake."""

import asyncio
import json


class FakeBlobClient:
    """Minimal in-memory stand-in for vercel.blob.AsyncBlobClient."""

    def __init__(self, page_size=None):
        self.objects = {}  # pathname -> (bytes, access)
        self.put_calls = []
        self.get_calls = []
        self.page_size = page_size
        self.list_calls = []

    async def put(self, path, body, *, access="public", content_type=None,
                  add_random_suffix=False, overwrite=False, **kwargs):
        if path in self.objects and not overwrite:
            raise RuntimeError("would overwrite without overwrite=True")
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.objects[path] = (body, access)
        self.put_calls.append((path, access, overwrite))
        return type("PutResult", (), {"pathname": path, "url": f"blob://{path}"})()

    async def get(self, url_or_path, *, access="public", use_cache=True, **kwargs):
        self.get_calls.append((url_or_path, use_cache))
        path = url_or_path.replace("blob://", "")
        if path not in self.objects:
            raise FileNotFoundError(path)
        body, _ = self.objects[path]
        return type("GetResult", (), {"content": body, "pathname": path,
                                      "url": f"blob://{path}"})()

    async def list_objects(self, *, prefix=None, limit=None, cursor=None, mode=None):
        self.list_calls.append((prefix, cursor))
        blobs = []
        for path in sorted(self.objects):
            if prefix and not path.startswith(prefix):
                continue
            blobs.append(type("Item", (), {"pathname": path, "url": f"blob://{path}"})())
        start = int(cursor or 0)
        page_size = self.page_size or len(blobs) or 1
        page = blobs[start:start + page_size]
        next_cursor = str(start + page_size) if start + page_size < len(blobs) else None
        return type("ListResult", (), {"blobs": page, "cursor": next_cursor,
                                       "has_more": next_cursor is not None,
                                       "folders": []})()

    async def delete(self, url_or_path):
        targets = [url_or_path] if isinstance(url_or_path, str) else list(url_or_path)
        for target in targets:
            self.objects.pop(target.replace("blob://", ""), None)


from vercel_trace import TraceStore  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def test_stage_paths_are_isolated_and_redacted():
    async def scenario():
        client = FakeBlobClient()
        store = TraceStore(client=client)
        await store.write_stage("run-a", 2, {"status": "running", "resume": "secret-text"})
        await store.write_stage("run-a", 4, {"status": "completed", "jd_text": "secret-jd"})
        stages = await store.read_stages("run-a")
        assert stages[2]["status"] == "running"
        assert stages[4]["status"] == "completed"
        # Forbidden content-bearing keys must be dropped by the sanitizer.
        assert "secret-text" not in repr(stages)
        assert "secret-jd" not in repr(stages)
        assert "resume" not in stages[2]
        assert "jd_text" not in stages[4]
        # Writes must be private and overwrite-safe.
        assert all(access == "private" for _, access, _ in client.put_calls)
        assert all(overwrite for _, _, overwrite in client.put_calls)
        # Reads must bypass cache.
        assert all(use_cache is False for _, use_cache in client.get_calls)
    run(scenario())


def test_parallel_stages_use_distinct_paths():
    async def scenario():
        client = FakeBlobClient()
        store = TraceStore(client=client)
        await asyncio.gather(
            store.write_stage("run-b", 2, {"status": "completed"}),
            store.write_stage("run-b", 4, {"status": "completed"}),
        )
        paths = {path for path, _, _ in client.put_calls}
        assert paths == {"runs/run-b/stage-2.json", "runs/run-b/stage-4.json"}
    run(scenario())


def test_cancel_marker_roundtrip():
    async def scenario():
        client = FakeBlobClient()
        store = TraceStore(client=client)
        assert await store.is_cancelled("run-c") is False
        await store.write_cancel("run-c")
        assert await store.is_cancelled("run-c") is True
        # A different run is unaffected.
        assert await store.is_cancelled("run-d") is False
    run(scenario())


def test_meta_roundtrip_is_sanitized():
    async def scenario():
        client = FakeBlobClient()
        store = TraceStore(client=client)
        await store.write_meta("run-e", {"model": "gpt-5.5", "reasoning": "xhigh",
                                         "resume": "secret"})
        meta = await store.read_meta("run-e")
        assert meta["model"] == "gpt-5.5"
        assert "resume" not in meta
    run(scenario())


def test_delete_run_removes_all_paths():
    async def scenario():
        client = FakeBlobClient()
        store = TraceStore(client=client)
        await store.write_stage("run-f", 2, {"status": "completed"})
        await store.write_cancel("run-f")
        await store.write_meta("run-f", {"model": "gpt-5.5"})
        await store.delete_run("run-f")
        assert not [p for p in client.objects if p.startswith("runs/run-f/")]
    run(scenario())


def test_run_id_is_path_sanitized():
    async def scenario():
        client = FakeBlobClient()
        store = TraceStore(client=client)
        await store.write_stage("../../etc/passwd", 2, {"status": "running"})
        assert all(".." not in path for path in client.objects)
        assert all(path.startswith("runs/") for path in client.objects)
    run(scenario())


def test_cleanup_before_deletes_only_old_runs():
    async def scenario():
        client = FakeBlobClient()
        store = TraceStore(client=client)
        await store.write_meta("old-run", {"model": "gpt-5.5"}, created_epoch=1000.0)
        await store.write_meta("new-run", {"model": "gpt-5.5"}, created_epoch=5000.0)
        await store.write_stage("old-run", 2, {"status": "completed"}, created_epoch=1000.0)
        deleted = await store.cleanup_before(4000.0)
        assert "old-run" in deleted
        assert "new-run" not in deleted
        assert not [p for p in client.objects if p.startswith("runs/old-run/")]
        assert [p for p in client.objects if p.startswith("runs/new-run/")]
    run(scenario())


def test_list_paginates_for_reads_and_cleanup():
    async def scenario():
        client = FakeBlobClient(page_size=2)
        store = TraceStore(client=client)
        for stage_id in range(1, 6):
            await store.write_stage(
                "paged-run", stage_id, {"status": "completed"}, created_epoch=1000.0,
            )
        stages = await store.read_stages("paged-run")
        assert sorted(stages) == [1, 2, 3, 4, 5]
        assert len(client.list_calls) >= 3

        client.list_calls.clear()
        deleted = await store.cleanup_before(2000.0)
        assert deleted == ["paged-run"]
        assert not client.objects
        assert len(client.list_calls) >= 3
    run(scenario())


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"\n{len(tests)} vercel-trace tests passed")
