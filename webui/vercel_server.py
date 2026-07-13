"""FastAPI serverless entrypoint for the Vercel Hobby deployment.

This module has no sweeper threads, subprocesses, global job registry, or quota
files. It validates the model policy, enforces Redis-backed public quotas and
HMAC cookie ownership, stores BYOK credentials only as encrypted references,
starts a durable workflow, and serves session-scoped status, cancellation,
deletion, history, and cron cleanup endpoints. Gateway credentials and the base
URL are never exposed to the browser.
"""

import asyncio
import hmac
import io
import os
import secrets
import tempfile
import time
import zipfile

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse

import config
from public_security import (
    encrypt_api_key,
    hash_ip,
    issue_session,
    normalize_client_ip,
    verify_session,
)
from quota_store import AdmissionDenied, QuotaStore, QuotaUnavailable
from vercel_trace import TraceStore

MAX_UPLOAD_BYTES = 4 * 1024 * 1024
ALLOWED_EXTS = {".pdf", ".docx", ".txt", ".md", ".text"}
MAX_RESUME_CHARS = 60_000
MAX_JD_CHARS = 60_000
MAX_API_KEY_CHARS = 200
MAX_DOCX_UNCOMPRESSED_BYTES = 20 * 1024 * 1024
MAX_DOCX_ENTRIES = 2_000
PARSE_TIMEOUT_SECONDS = 20.0
TRACE_RETENTION_SECONDS = 24 * 3600
SESSION_COOKIE_NAME = "agent_sid"
MAX_SESSION_TTL_SECONDS = 86400
_BIND_RETRY_DELAYS = (0.05, 0.15)
_SDK_TERMINAL_STATUSES = {"completed", "failed", "cancelled", "canceled"}

STAGE_ROWS = (
    (1, "简历解析"),
    (2, "简历信息结构化"),
    (3, "在招岗位发现"),
    (4, "岗位要求分析"),
    (5, "匹配度分析"),
    (6, "简历优化"),
    (7, "真实性审查"),
    (8, "生成报告"),
)

app = FastAPI(title="Resume Agent (Vercel)")

_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def _signing_key():
    return os.environ.get("AGENT_RUN_SIGNING_KEY", "")


def _session_ttl_seconds():
    try:
        ttl = int(os.environ.get("AGENT_SESSION_TTL", str(MAX_SESSION_TTL_SECONDS)))
    except (TypeError, ValueError, OverflowError):
        raise ValueError("invalid session TTL") from None
    if ttl <= 0 or ttl > MAX_SESSION_TTL_SECONDS:
        raise ValueError("invalid session TTL")
    return ttl


@app.middleware("http")
async def _ensure_session(request, call_next):
    now = int(time.time())
    signing_key = _signing_key()
    try:
        session_ttl = _session_ttl_seconds()
    except ValueError:
        return JSONResponse(
            {"error": "会话服务暂不可用", "code": "session_unavailable"},
            status_code=503,
        )
    session_hash = verify_session(
        request.cookies.get(SESSION_COOKIE_NAME), signing_key, now,
    )
    cookie_value = None
    if session_hash is None:
        try:
            cookie_value, session_hash, _ = issue_session(
                signing_key, now, session_ttl,
            )
        except ValueError:
            return JSONResponse(
                {"error": "会话服务暂不可用", "code": "session_unavailable"},
                status_code=503,
            )
    request.state.session_hash = session_hash
    response = await call_next(request)
    if cookie_value is not None:
        response.set_cookie(
            SESSION_COOKIE_NAME,
            cookie_value,
            max_age=session_ttl,
            path="/",
            secure=True,
            httponly=True,
            samesite="lax",
        )
    return response


# --------------------------------------------------------------------- runtime
class VercelBackend:
    """Production backend backed by the Vercel Workflows SDK."""

    async def start(self, payload):
        from workflows.resume_workflow import start_resume_run

        run = await start_resume_run(payload)
        return run.run_id

    async def status(self, run_id):
        from vercel.workflow import Run

        return await Run(run_id).status()

    async def result(self, run_id):
        from vercel.workflow import Run

        return await Run(run_id).return_value()

    async def cancel(self, run_id):
        await _trace().write_cancel(run_id)


_BACKEND = None
_TRACE = None
_QUOTA = None


def set_runtime(*, backend=None, trace=None, quota=None):
    """Inject backend, trace, and quota stores (used by tests)."""
    global _BACKEND, _TRACE, _QUOTA
    if backend is not None:
        _BACKEND = backend
    if trace is not None:
        _TRACE = trace
    if quota is not None:
        _QUOTA = quota


def _backend():
    global _BACKEND
    if _BACKEND is None:
        _BACKEND = VercelBackend()
    return _BACKEND


def _trace():
    global _TRACE
    if _TRACE is None:
        _TRACE = TraceStore()
    return _TRACE


def _quota():
    global _QUOTA
    if _QUOTA is None:
        _QUOTA = QuotaStore.from_env(
            free_per_day=os.environ.get("AGENT_FREE_PER_DAY", "2"),
            site_free_per_day=os.environ.get("AGENT_SITE_FREE_PER_DAY", "20"),
            runs_per_hour=os.environ.get("AGENT_RUNS_PER_HOUR", "6"),
            mock_per_hour=os.environ.get("AGENT_MOCK_PER_HOUR", "20"),
            max_concurrent=os.environ.get("AGENT_MAX_CONCURRENT", "3"),
            session_ttl=os.environ.get("AGENT_SESSION_TTL", "86400"),
            history_cap=os.environ.get("AGENT_SESSION_REPORT_CAP", "5"),
            admission_ttl=os.environ.get("AGENT_ADMISSION_TTL", "900"),
        )
    return _QUOTA


# --------------------------------------------------------------------- helpers
def _model_catalog():
    return {item["id"]: list(item["reasoning_levels"]) for item in config.MODEL_OPTIONS}


def _session_hash(request):
    return str(getattr(request.state, "session_hash", ""))


def _client_ip_hash(request):
    if os.environ.get("VERCEL") == "1":
        forwarded = request.headers.get("x-vercel-forwarded-for")
        normalized = normalize_client_ip(forwarded, None)
    else:
        client_host = request.client.host if request.client is not None else None
        normalized = normalize_client_ip(None, client_host)
    return hash_ip(normalized, _signing_key())


def _parse_mock(value):
    normalized = str(value or "").strip().lower()
    if normalized in {"", "0", "false", "off", "no"}:
        return False
    if normalized in {"1", "true", "on", "yes"}:
        return True
    raise ValueError("Mock 参数无效")


_ADMISSION_MESSAGES = {
    "free_quota_exhausted": "今日免费额度已用完，请使用自有 API Key",
    "site_quota_exhausted": "公站今日免费额度已用完，请使用自有 API Key",
    "hourly_limit": "请求过于频繁，请稍后再试",
    "ip_concurrent": "当前网络已有运行中的任务",
    "global_concurrent": "当前运行任务已满，请稍后再试",
}


def _admission_error_response(error):
    body = {
        "error": _ADMISSION_MESSAGES.get(error.code, "请求暂时无法接受"),
        "code": error.code,
    }
    if error.free_left is not None:
        body["free_left"] = error.free_left
    return JSONResponse(body, status_code=429)


def _quota_unavailable_response():
    return JSONResponse(
        {"error": "配额服务暂不可用，请稍后再试", "code": "quota_unavailable"},
        status_code=503,
    )


async def _rollback_create(
    *, quota, backend, trace, admission_id, credential_ref, run_id, session_hash,
):
    if run_id:
        try:
            await backend.cancel(run_id)
        except Exception:
            try:
                await trace.write_cancel(run_id)
            except Exception:
                pass
        try:
            await quota.delete_run(run_id, session_hash)
        except Exception:
            pass
    try:
        await quota.delete_credential(credential_ref)
    except Exception:
        pass
    try:
        await quota.release(admission_id, refund_daily=True)
    except Exception:
        pass


class _RunBindingUncertain(RuntimeError):
    pass


async def _bind_started_run(
    quota, admission_id, run_id, session_hash, model, reasoning, created_at,
):
    """Idempotently bind a started workflow and resolve lost Redis responses."""
    attempts = 1 + len(_BIND_RETRY_DELAYS)
    for attempt in range(attempts):
        if attempt:
            await asyncio.sleep(_BIND_RETRY_DELAYS[attempt - 1])
        try:
            return await quota.bind_run(
                admission_id,
                run_id,
                session_hash,
                model,
                reasoning,
                created_at,
            )
        except QuotaUnavailable:
            continue

    try:
        if await quota.owns_run(run_id, session_hash):
            return True
    except QuotaUnavailable:
        raise _RunBindingUncertain() from None
    raise ValueError("run binding was not committed")


async def _request_cancel_best_effort(backend, trace, run_id):
    for operation in (backend.cancel, trace.write_cancel):
        try:
            await operation(run_id)
        except Exception:
            pass


def _bearer(request):
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    return ""


def _stage_rows(stage_docs):
    rows = []
    for stage_id, label in STAGE_ROWS:
        doc = stage_docs.get(stage_id) or {}
        rows.append({
            "stage_id": stage_id,
            "name": label,
            "status": doc.get("status", "pending"),
            "duration_ms": doc.get("duration_ms"),
            "revision_round": doc.get("revision_round"),
            "error_category": doc.get("error_category"),
            "reason": doc.get("reason"),
            "validation_status": doc.get("validation_status"),
            "attempt": doc.get("attempt"),
        })
    return rows


def _docx_archive_is_safe(data):
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_DOCX_ENTRIES:
                return False
            return sum(item.file_size for item in entries) <= MAX_DOCX_UNCOMPRESSED_BYTES
    except (zipfile.BadZipFile, OSError):
        return False


async def _extract_resume(resume_file, resume_text):
    """Return (text, error_response). error_response is a JSONResponse or None."""
    if resume_file is not None and resume_file.filename:
        ext = os.path.splitext(resume_file.filename)[1].lower()
        if ext not in ALLOWED_EXTS:
            return None, JSONResponse(
                {"error": f"暂不支持的文件类型：{ext or '未知'}；支持 PDF/DOCX/TXT/MD"},
                status_code=415,
            )
        data = await resume_file.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            return None, JSONResponse(
                {"error": "简历文件超过 4MB 上限"}, status_code=413,
            )
        if ext == ".docx" and not _docx_archive_is_safe(data):
            return None, JSONResponse(
                {"error": "DOCX 文件展开后过大或格式无效"}, status_code=413,
            )
        from tools.file_parser import parse_resume_file

        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(suffix=ext)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            try:
                parsed = await asyncio.wait_for(
                    asyncio.to_thread(parse_resume_file, tmp_path),
                    timeout=PARSE_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                return None, JSONResponse(
                    {"error": "简历文件解析超时，请改用可复制文本的版本"},
                    status_code=408,
                )
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
        if not parsed.get("success"):
            return None, JSONResponse(
                {"error": "简历解析失败，请确认文件未损坏"}, status_code=422,
            )
        text = str(parsed.get("text") or "")
    else:
        text = str(resume_text or "")

    if not text.strip():
        return None, JSONResponse(
            {"error": "未从简历中提取到文本；扫描件或图片型 PDF 请改用可复制文本的版本"},
            status_code=422,
        )
    if len(text) > MAX_RESUME_CHARS:
        return None, JSONResponse(
            {"error": "简历文本超过 60000 字符上限"}, status_code=413,
        )
    return text, None


# ---------------------------------------------------------------------- routes
def _content_security_policy(nonce):
    return (
        "default-src 'self'; "
        "base-uri 'none'; "
        "object-src 'none'; "
        "frame-ancestors 'none'; "
        "img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; "
        f"script-src 'self' 'nonce-{nonce}'; "
        "connect-src 'self'"
    )


@app.get("/", response_class=HTMLResponse)
def index():
    try:
        with open(os.path.join(_STATIC_DIR, "vercel_app.html"), "r", encoding="utf-8") as handle:
            template = handle.read()
    except OSError:
        return HTMLResponse("<h1>Resume Agent</h1>", status_code=200)
    nonce = secrets.token_urlsafe(16)
    html = template.replace("__CSP_NONCE__", nonce)
    return HTMLResponse(html, headers={"Content-Security-Policy": _content_security_policy(nonce)})


@app.get("/api/config")
async def api_config(request: Request):
    quota = _quota()
    try:
        free_left = await quota.free_left(_client_ip_hash(request))
    except ValueError:
        return JSONResponse(
            {"error": "无法确认请求来源", "code": "invalid_client_ip"},
            status_code=400,
        )
    except QuotaUnavailable:
        return _quota_unavailable_response()
    return {
        "models": _model_catalog(),
        "model_options": [
            {"id": item["id"], "label": item["label"],
             "reasoning_levels": list(item["reasoning_levels"]),
             "default_reasoning": item["default_reasoning"],
             "status_label": item["status_label"], "tier_label": item["tier_label"]}
            for item in config.MODEL_OPTIONS
        ],
        "default_model": config.DEFAULT_MODEL,
        "default_reasoning": config.DEFAULT_REASONING_BY_MODEL,
        "deployment_mode": "vercel",
        "max_upload_bytes": MAX_UPLOAD_BYTES,
        "job_search_enabled": False,
        "free_left": free_left,
        "free_per_day": quota.free_per_day,
    }


@app.get("/api/status")
def api_status():
    return {"deployment_mode": "vercel", "public": True,
            "default_model": config.DEFAULT_MODEL}


@app.post("/api/runs")
async def create_run(
    request: Request,
    jd_text: str = Form(""),
    resume_text: str = Form(""),
    model: str = Form(""),
    reasoning: str = Form(""),
    job_search: str = Form("0"),
    api_key: str = Form(""),
    mock: str = Form("0"),
    resume_file: UploadFile = File(None),
):
    try:
        model, reasoning = config.validate_model_reasoning(model, reasoning)
        mock_enabled = _parse_mock(mock)
    except ValueError as error:
        return JSONResponse({"error": str(error)}, status_code=400)

    supplied_key = "" if mock_enabled else str(api_key or "").strip()
    if len(supplied_key) > MAX_API_KEY_CHARS:
        return JSONResponse(
            {
                "error": "API Key 超过 200 字符上限",
                "code": "api_key_too_long",
            },
            status_code=413,
        )
    byok = bool(supplied_key)
    if not mock_enabled and not byok and not config.API_KEY.strip():
        return JSONResponse(
            {
                "error": "模型网关尚未配置，请联系管理员后重试",
                "code": "gateway_not_configured",
            },
            status_code=503,
        )

    if not jd_text.strip():
        return JSONResponse(
            {"error": "当前预览仅支持粘贴目标 JD 的流程；请提供职位描述"},
            status_code=422,
        )
    if len(jd_text) > MAX_JD_CHARS:
        return JSONResponse(
            {"error": "职位描述超过 60000 字符上限"}, status_code=413,
        )

    try:
        ip_hash = _client_ip_hash(request)
    except ValueError:
        return JSONResponse(
            {"error": "无法确认请求来源", "code": "invalid_client_ip"},
            status_code=400,
        )

    now = time.time()
    quota = _quota()
    session_hash = _session_hash(request)
    try:
        admission = await quota.acquire(
            ip_hash,
            session_hash,
            kind="mock" if mock_enabled else "real",
            funded=not mock_enabled and not byok,
            now=now,
        )
    except AdmissionDenied as error:
        return _admission_error_response(error)
    except QuotaUnavailable:
        return _quota_unavailable_response()

    admission_id = admission["admission_id"]
    credential_ref = None
    run_id = None
    backend = _backend()
    trace = _trace()
    try:
        text, error_response = await _extract_resume(resume_file, resume_text)
        if error_response is not None:
            await _rollback_create(
                quota=quota,
                backend=backend,
                trace=trace,
                admission_id=admission_id,
                credential_ref=credential_ref,
                run_id=run_id,
                session_hash=session_hash,
            )
            return error_response

        if byok:
            encrypted_key = encrypt_api_key(supplied_key, _signing_key())
            credential_ref = await quota.store_credential(encrypted_key)

        payload = {
            "resume_text": text,
            "jd_text": jd_text.strip(),
            "model": model,
            "reasoning": reasoning,
            "job_search": False,
            "deadline_epoch": now + float(config.RUN_TIMEOUT),
            "admission_id": admission_id,
            "credential_ref": credential_ref,
            "session_hash": session_hash,
            "mock": mock_enabled,
        }
        run_id = await backend.start(payload)
        await _bind_started_run(
            quota,
            admission_id,
            run_id,
            session_hash,
            model,
            reasoning,
            now,
        )
    except _RunBindingUncertain:
        # The bind may already be durable even though every response was lost.
        # Keep quota/credential accounting authoritative and request cancellation.
        await _request_cancel_best_effort(backend, trace, run_id)
        return JSONResponse(
            {
                "error": "运行已提交但状态确认失败，请稍后查看最近任务",
                "code": "run_start_uncertain",
            },
            status_code=503,
        )
    except QuotaUnavailable:
        await _rollback_create(
            quota=quota,
            backend=backend,
            trace=trace,
            admission_id=admission_id,
            credential_ref=credential_ref,
            run_id=run_id,
            session_hash=session_hash,
        )
        return _quota_unavailable_response()
    except Exception:
        await _rollback_create(
            quota=quota,
            backend=backend,
            trace=trace,
            admission_id=admission_id,
            credential_ref=credential_ref,
            run_id=run_id,
            session_hash=session_hash,
        )
        return JSONResponse(
            {"error": "运行启动失败，请稍后再试", "code": "run_start_failed"},
            status_code=503,
        )

    try:
        await trace.write_meta(
            run_id,
            {
                "model": model,
                "reasoning": reasoning,
                "status": "running",
                "created_at": int(now),
            },
            created_epoch=now,
        )
        await trace.write_stage(
            run_id,
            1,
            {"status": "completed", "stage_id": 1, "name": "简历解析"},
            created_epoch=now,
        )
        stage_rows = _stage_rows(await trace.read_stages(run_id))
    except Exception:
        # Once the workflow is bound, accounting must remain authoritative.
        # Blob observability can degrade without refunding or releasing a live run.
        stage_rows = _stage_rows({})

    return JSONResponse(
        {
            "run_id": run_id,
            "deadline_epoch": payload["deadline_epoch"],
            "stages": stage_rows,
            "model": model,
            "reasoning": reasoning,
            "free_left": admission["free_left"],
            "mock": mock_enabled,
            "byok": byok,
        },
        status_code=202,
    )


def _run_not_found_response():
    return JSONResponse(
        {"error": "运行不存在", "code": "run_not_found"}, status_code=404,
    )


async def _owns_run(request, run_id):
    return await _quota().owns_run(run_id, _session_hash(request))


@app.get("/api/runs")
async def list_runs(request: Request):
    try:
        ip_hash = _client_ip_hash(request)
        runs = await _quota().list_runs(_session_hash(request))
        free_left = await _quota().free_left(ip_hash)
    except ValueError:
        return JSONResponse(
            {"error": "无法确认请求来源", "code": "invalid_client_ip"},
            status_code=400,
        )
    except QuotaUnavailable:
        return _quota_unavailable_response()
    return {"runs": runs, "free_left": free_left}


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str, request: Request):
    try:
        if not await _owns_run(request, run_id):
            return _run_not_found_response()
    except QuotaUnavailable:
        return _quota_unavailable_response()

    backend = _backend()
    trace = _trace()
    try:
        sdk_status = await backend.status(run_id)
        stage_docs = await trace.read_stages(run_id)
    except Exception:
        return JSONResponse(
            {"error": "运行状态暂不可用", "code": "status_unavailable"},
            status_code=503,
        )
    overall = {
        "pending": "running", "running": "running", "completed": "completed",
        "failed": "failed", "cancelled": "cancelled", "canceled": "cancelled",
    }.get(sdk_status, sdk_status)

    response = {
        "run_id": run_id,
        "status": overall,
        "stages": _stage_rows(stage_docs),
    }
    if sdk_status == "completed":
        try:
            result = await backend.result(run_id) or {}
        except Exception:
            return JSONResponse(
                {"error": "运行结果暂不可用", "code": "status_unavailable"},
                status_code=503,
            )
        response["status"] = result.get("status", "completed")
        response["report"] = result.get("report", "")
        response["safe_to_deliver"] = bool(result.get("safe_to_deliver"))
        response["unresolved_fixes"] = result.get("unresolved_fixes", [])
        response["model"] = result.get("model", "")
        response["reasoning"] = result.get("reasoning", "")
    terminal_statuses = {
        "completed", "partial", "failed", "cancelled", "deadline_exceeded",
    }
    if response["status"] in terminal_statuses:
        try:
            updated = await _quota().update_run(
                run_id,
                response["status"],
                bool(response.get("safe_to_deliver")),
            )
        except QuotaUnavailable:
            return _quota_unavailable_response()
        if not updated:
            return _run_not_found_response()
    return response


@app.post("/api/runs/{run_id}/cancel")
async def cancel_run(run_id: str, request: Request):
    try:
        if not await _owns_run(request, run_id):
            return _run_not_found_response()
    except QuotaUnavailable:
        return _quota_unavailable_response()
    try:
        await _trace().write_cancel(run_id)
    except Exception:
        return JSONResponse(
            {"error": "取消请求写入失败", "code": "cancel_unavailable"},
            status_code=503,
        )
    return JSONResponse({"run_id": run_id, "status": "cancelling"}, status_code=202)


@app.delete("/api/runs/{run_id}")
async def delete_run(run_id: str, request: Request):
    quota = _quota()
    session_hash = _session_hash(request)
    try:
        if not await quota.owns_run(run_id, session_hash):
            return _run_not_found_response()
    except QuotaUnavailable:
        return _quota_unavailable_response()

    try:
        sdk_status = await _backend().status(run_id)
    except Exception:
        return JSONResponse(
            {"error": "运行状态暂不可用", "code": "status_unavailable"},
            status_code=503,
        )
    if sdk_status not in _SDK_TERMINAL_STATUSES:
        return JSONResponse(
            {
                "error": "运行结束后才能删除；如需停止请先取消并等待终态",
                "code": "run_not_terminal",
            },
            status_code=409,
        )

    try:
        await _trace().delete_run(run_id)
    except Exception:
        return JSONResponse(
            {"error": "运行删除失败", "code": "delete_unavailable"},
            status_code=503,
        )
    try:
        deleted = await quota.delete_run(run_id, session_hash)
    except QuotaUnavailable:
        deleted = False
    if not deleted:
        return JSONResponse(
            {"error": "运行删除失败", "code": "delete_unavailable"},
            status_code=503,
        )
    return JSONResponse({"run_id": run_id, "status": "deleted"}, status_code=200)


@app.get("/api/maintenance/cleanup")
async def cleanup(request: Request):
    secret = os.environ.get("CRON_SECRET", "")
    provided = _bearer(request) or request.query_params.get("secret", "")
    if not provided:
        return JSONResponse({"error": "缺少凭证"}, status_code=401)
    if not secret or not hmac.compare_digest(provided, secret):
        return JSONResponse({"error": "凭证无效"}, status_code=403)
    deleted = await _trace().cleanup_before(time.time() - TRACE_RETENTION_SECONDS)
    return JSONResponse({"deleted": deleted, "count": len(deleted)}, status_code=200)
