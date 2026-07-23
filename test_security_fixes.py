"""P0 性能修复与 RED 文件读取越权修复的回归测试。

覆盖：
1. tools/file_parser.py 目录白名单（AGENT_ALLOWED_FILE_ROOTS）与符号链接逃逸
2. workflows/vercel_worker.py 全局 threading.Lock → asyncio.Semaphore
3. quota_store.py 模块级共享 httpx.AsyncClient
"""

import asyncio
import os


# ---- 修复 3a：parse_resume_file 目录白名单 ------------------------------


def test_parse_resume_file_rejects_outside_allowed_roots(tmp_path, monkeypatch):
    from tools.file_parser import parse_resume_file

    monkeypatch.setenv("AGENT_ALLOWED_FILE_ROOTS", str(tmp_path))
    outside_dir = tmp_path.parent  # 白名单之外
    outside_file = outside_dir / "ra_outside_secret.txt"
    outside_file.write_text("机密内容", encoding="utf-8")
    try:
        result = parse_resume_file(str(outside_file))
        assert result["success"] is False
        assert "受信任目录" in result["error"]
        assert result["text"] == ""
    finally:
        outside_file.unlink()


def test_parse_resume_file_allows_inside_allowed_roots(tmp_path, monkeypatch):
    from tools.file_parser import parse_resume_file

    monkeypatch.setenv("AGENT_ALLOWED_FILE_ROOTS", str(tmp_path))
    inside_file = tmp_path / "resume.txt"
    inside_file.write_text("姓名：张三\n技能：Python", encoding="utf-8")
    result = parse_resume_file(str(inside_file))
    assert result["success"] is True
    assert "张三" in result["text"]
    assert result["file_type"] == "text"


def test_parse_resume_file_rejects_symlink_escape(tmp_path, monkeypatch):
    from tools.file_parser import parse_resume_file

    monkeypatch.setenv("AGENT_ALLOWED_FILE_ROOTS", str(tmp_path))
    outside_file = tmp_path.parent / "ra_symlink_target.txt"
    outside_file.write_text("逃逸目标", encoding="utf-8")
    link = tmp_path / "innocent.txt"
    try:
        link.symlink_to(outside_file)
        result = parse_resume_file(str(link))
        assert result["success"] is False
        assert "受信任目录" in result["error"]
        assert result["text"] == ""
    finally:
        if link.exists() or link.is_symlink():
            link.unlink()
        outside_file.unlink()


# ---- 修复 1：vercel_worker 全局锁 → asyncio.Semaphore --------------------


def test_vercel_worker_uses_semaphore_not_global_lock():
    try:
        import workflows.vercel_worker as worker
    except ImportError:
        # vercel SDK 不可用时退化为源码级断言
        source_path = os.path.join(os.path.dirname(__file__), "workflows", "vercel_worker.py")
        with open(source_path, "r", encoding="utf-8") as file:
            source = file.read()
        assert "threading.Lock" not in source
        assert "asyncio.Semaphore" in source
        assert "_step_semaphore" in source
        return
    assert hasattr(worker, "_step_semaphore")
    assert isinstance(worker._step_semaphore, asyncio.Semaphore)
    assert not hasattr(worker, "_post_lock")


# ---- 修复 2：quota_store 共享 httpx.AsyncClient --------------------------


def test_shared_client_is_reused_and_carries_auth_header():
    from quota_store import _shared_client

    first = _shared_client("https://example.upstash.io", "t")
    second = _shared_client("https://example.upstash.io", "t")
    assert first is second
    assert first.headers.get("Authorization") == "Bearer t"

    other = _shared_client("https://example.upstash.io", "other-token")
    assert other is not first
    assert other.headers.get("Authorization") == "Bearer other-token"


def test_execute_uses_shared_client(monkeypatch):
    import quota_store

    calls = []

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"result": "1"}

    class FakeClient:
        is_closed = False

        async def post(self, url, json=None):
            calls.append((url, json))
            return FakeResponse()

    fake = FakeClient()
    monkeypatch.setitem(quota_store._SHARED_CLIENTS, ("https://example.upstash.io", "tok"), fake)

    store = quota_store.QuotaStore(url="https://example.upstash.io", token="tok")
    result = asyncio.run(store._execute(["GET", "some:key"]))
    assert result == "1"
    assert calls == [("https://example.upstash.io", ["GET", "some:key"])]
