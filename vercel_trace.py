"""Private Vercel Blob stage-trace store.

Every document is a small, redacted status object written to a fixed private
path so that parallel stages never share a mutable object. Resume text, JD text,
prompts, model output, report content, and credentials are never persisted here;
only allow-listed status metadata survives the sanitizer.
"""

import json
import os
import re

# Keys permitted in a stage/meta status document. Anything else is dropped
# before the document is written, so a caller cannot accidentally persist
# resume text, JD text, prompts, model output, or credentials.
_ALLOWED_KEYS = frozenset({
    "status", "stage_id", "stage", "name", "total_stages", "duration_ms",
    "model", "reasoning", "attempt", "tokens_prompt", "tokens_completion",
    "retry_category", "validation_status", "output_shape", "error_category",
    "started_at", "completed_at", "created_at", "revision_round", "reason",
    "safe_to_deliver", "terminal", "elapsed_ms",
})

_MAX_STR = 240
_STAGE_RE = re.compile(r"stage-(\d+)\.json$")


def _safe_run_id(run_id) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "", str(run_id))
    return cleaned or "run"


def _coerce_scalar(value):
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:_MAX_STR]
    return None


def _sanitize(doc) -> dict:
    """Keep only allow-listed keys with shallow, scalar-only values."""
    if not isinstance(doc, dict):
        return {}
    clean = {}
    for key, value in doc.items():
        if key not in _ALLOWED_KEYS:
            continue
        if isinstance(value, list):
            clean[key] = [
                item for item in (_coerce_scalar(v) for v in value)
                if item is not None
            ]
        elif isinstance(value, dict):
            # Permit a shallow scalar-only shape descriptor (e.g. output_shape).
            clean[key] = {
                str(k): _coerce_scalar(v)
                for k, v in value.items()
                if _coerce_scalar(v) is not None
            }
        else:
            coerced = _coerce_scalar(value)
            if coerced is not None or value is None:
                clean[key] = coerced
    return clean


def _default_client(token=None):
    from vercel.blob import AsyncBlobClient  # imported lazily; SDK only on Vercel

    return AsyncBlobClient(token=token or os.environ.get("BLOB_READ_WRITE_TOKEN"))


class TraceStore:
    """Write and read privacy-safe run traces in a connected Private Blob store."""

    def __init__(self, client=None, *, token=None, prefix="runs"):
        self._client = client if client is not None else _default_client(token)
        self._prefix = prefix.strip("/")

    # ---- path helpers -------------------------------------------------
    def _run_root(self, run_id) -> str:
        return f"{self._prefix}/{_safe_run_id(run_id)}"

    def _stage_path(self, run_id, stage_id) -> str:
        return f"{self._run_root(run_id)}/stage-{int(stage_id)}.json"

    def _cancel_path(self, run_id) -> str:
        return f"{self._run_root(run_id)}/cancel.json"

    def _meta_path(self, run_id) -> str:
        return f"{self._run_root(run_id)}/meta.json"

    # ---- low-level blob io -------------------------------------------
    async def _put_json(self, path, doc, *, created_epoch=None):
        payload = dict(doc)
        if created_epoch is not None:
            payload["_created"] = float(created_epoch)
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        await self._client.put(
            path, body, access="private", overwrite=True,
            content_type="application/json",
        )

    async def _get_json(self, path):
        try:
            result = await self._client.get(path, access="private", use_cache=False)
        except Exception:
            return None
        content = getattr(result, "content", None)
        if content is None:
            return None
        if isinstance(content, (bytes, bytearray)):
            content = content.decode("utf-8", "replace")
        try:
            return json.loads(content)
        except (ValueError, TypeError):
            return None

    async def _list(self, prefix):
        try:
            result = await self._client.list_objects(prefix=prefix)
        except Exception:
            return []
        return list(getattr(result, "blobs", []) or [])

    @staticmethod
    def _ref(blob):
        return getattr(blob, "url", None) or getattr(blob, "pathname", None)

    # ---- public api ---------------------------------------------------
    async def write_stage(self, run_id, stage_id, doc, *, created_epoch=None):
        payload = _sanitize(doc)
        payload.setdefault("stage_id", int(stage_id))
        await self._put_json(self._stage_path(run_id, stage_id), payload,
                             created_epoch=created_epoch)

    async def read_stages(self, run_id):
        stages = {}
        for blob in await self._list(self._run_root(run_id) + "/"):
            path = getattr(blob, "pathname", "") or ""
            match = _STAGE_RE.search(path)
            if not match:
                continue
            doc = await self._get_json(self._ref(blob))
            if isinstance(doc, dict):
                stages[int(match.group(1))] = doc
        return stages

    async def write_meta(self, run_id, doc, *, created_epoch=None):
        await self._put_json(self._meta_path(run_id), _sanitize(doc),
                             created_epoch=created_epoch)

    async def read_meta(self, run_id):
        return await self._get_json(self._meta_path(run_id)) or {}

    async def write_cancel(self, run_id):
        await self._put_json(self._cancel_path(run_id), {"status": "cancelled"})

    async def is_cancelled(self, run_id) -> bool:
        return bool(await self._list(self._cancel_path(run_id)))

    async def delete_run(self, run_id):
        refs = [self._ref(b) for b in await self._list(self._run_root(run_id) + "/")]
        refs = [ref for ref in refs if ref]
        if refs:
            try:
                await self._client.delete(refs)
            except Exception:
                for ref in refs:
                    try:
                        await self._client.delete(ref)
                    except Exception:
                        pass

    async def cleanup_before(self, epoch):
        """Delete every run whose most recent document predates ``epoch``."""
        newest = {}
        run_ids = {}
        for blob in await self._list(self._prefix + "/"):
            path = getattr(blob, "pathname", "") or ""
            parts = path.split("/")
            if len(parts) < 3:
                continue
            run_key = parts[1]
            doc = await self._get_json(self._ref(blob))
            created = 0.0
            if isinstance(doc, dict):
                try:
                    created = float(doc.get("_created") or 0.0)
                except (TypeError, ValueError):
                    created = 0.0
            newest[run_key] = max(newest.get(run_key, 0.0), created)
            run_ids[run_key] = run_key
        deleted = []
        for run_key, latest in newest.items():
            if latest < float(epoch):
                await self.delete_run(run_ids[run_key])
                deleted.append(run_key)
        return deleted
