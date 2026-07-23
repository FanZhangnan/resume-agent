"""Private ASGI adapter for Vercel's GA service queue trigger."""

import asyncio
import os

import sniffio

# Importing the workflow registry registers both durable workflow and step
# consumers with vercel.workers before the ASGI callback app is created.
import workflows.resume_workflow as _resume_workflow  # noqa: F401
from vercel.workers import get_asgi_app


# 并发上限可用 AGENT_WORKER_STEP_CONCURRENCY 调整；模块级创建在 Python 3.10+ 安全
# （asyncio.Semaphore 不再在 __init__ 中绑定事件循环）。
_step_semaphore = asyncio.Semaphore(int(os.environ.get("AGENT_WORKER_STEP_CONCURRENCY", "3")))


def _guard_worker_app(inner_app):
    async def guarded_app(scope, receive, send):
        method = str(scope.get("method") or "").upper()
        if method != "POST":
            await inner_app(scope, receive, send)
            return

        library_token = None
        async with _step_semaphore:
            try:
                library_token = sniffio.current_async_library_cvar.set("asyncio")
                await inner_app(scope, receive, send)
            finally:
                if library_token is not None:
                    sniffio.current_async_library_cvar.reset(library_token)

    return guarded_app


_inner_app = get_asgi_app()
app = _guard_worker_app(_inner_app)
