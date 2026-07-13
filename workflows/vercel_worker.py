"""Private ASGI adapter for Vercel's GA service queue trigger."""

import asyncio
import threading

import sniffio

# Importing the workflow registry registers both durable workflow and step
# consumers with vercel.workers before the ASGI callback app is created.
import workflows.resume_workflow as _resume_workflow  # noqa: F401
from vercel.workers import get_asgi_app


_post_lock = threading.Lock()


def _guard_worker_app(inner_app):
    async def guarded_app(scope, receive, send):
        method = str(scope.get("method") or "").upper()
        if method != "POST":
            await inner_app(scope, receive, send)
            return

        acquired = False
        library_token = None
        try:
            while not acquired:
                acquired = _post_lock.acquire(blocking=False)
                if not acquired:
                    await asyncio.sleep(0.01)

            library_token = sniffio.current_async_library_cvar.set("asyncio")
            await inner_app(scope, receive, send)
        finally:
            if library_token is not None:
                sniffio.current_async_library_cvar.reset(library_token)
            if acquired:
                _post_lock.release()

    return guarded_app


_inner_app = get_asgi_app()
app = _guard_worker_app(_inner_app)
