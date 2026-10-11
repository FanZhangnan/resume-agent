"""Private ASGI adapter for Vercel's GA service queue trigger."""

import asyncio
import os

import sniffio
from fastapi import FastAPI, Request
from fastapi.responses import Response
from vercel.workflow import ENDPOINT_PATH, MANIFEST_PATH

# Importing the workflow registry registers both durable workflow and step
# consumers with vercel.workers before the ASGI callback app is created.
import workflows.resume_workflow as _resume_workflow  # noqa: F401
from workflows.runtime import wf


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


class _WorkflowRequest:
    """Adapt Starlette's streaming request to the public Workflow protocol."""

    def __init__(self, request):
        self._request = request

    @property
    def method(self):
        return self._request.method

    @property
    def url(self):
        target = self._request.url.path
        if self._request.url.query:
            target += "?" + self._request.url.query
        return target

    @property
    def headers(self):
        return self._request.headers

    async def aiter_bytes(self, chunk_size=None):
        async for chunk in self._request.stream():
            if chunk:
                yield chunk


_inner_app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@_inner_app.api_route(MANIFEST_PATH, methods=["GET"], include_in_schema=False)
async def workflow_manifest(request: Request):
    result = await wf.manifest_handler(_WorkflowRequest(request))
    return Response(result.body, status_code=result.status, headers=result.headers)


@_inner_app.api_route("/{path:path}", methods=["POST", "GET", "HEAD", "OPTIONS"], include_in_schema=False)
async def workflow_delivery(request: Request, path: str):
    # The queue trigger can deliver to the function root; SDK health probes use
    # ENDPOINT_PATH. Both go through the same authenticated SDK queue handler.
    result = await wf.http_handler(_WorkflowRequest(request))
    return Response(result.body, status_code=result.status, headers=result.headers)


app = _guard_worker_app(_inner_app)
