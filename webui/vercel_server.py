"""FastAPI serverless entrypoint for the Vercel Hobby deployment.

This module has no sweeper threads, subprocesses, global job registry, sessions,
or quota files. It validates the invite and model policy, extracts the uploaded
resume in the request (deleting the temp file in ``finally``), starts a durable
workflow, and serves signed-token status, cancellation, deletion, and cron
cleanup endpoints. The gateway key and base URL are never exposed to the browser.
"""

import hmac
import os
import secrets
import tempfile
import time

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse

import config
import run_security
from vercel_trace import TraceStore

MAX_UPLOAD_BYTES = 4 * 1024 * 1024
ALLOWED_EXTS = {".pdf", ".docx", ".txt", ".md", ".text"}
MAX_RESUME_CHARS = 60_000
RUN_TOKEN_TTL = int(os.environ.get("AGENT_RUN_TOKEN_TTL", str(int(config.RUN_TIMEOUT) + 900)))
TRACE_RETENTION_SECONDS = 24 * 3600

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


_BACKEND = None
_TRACE = None


def set_runtime(*, backend=None, trace=None):
    """Inject a backend and/or trace store (used by tests)."""
    global _BACKEND, _TRACE
    if backend is not None:
        _BACKEND = backend
    if trace is not None:
        _TRACE = trace


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


# --------------------------------------------------------------------- helpers
def _model_catalog():
    return {item["id"]: list(item["reasoning_levels"]) for item in config.MODEL_OPTIONS}


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
            "attempt": doc.get("attempt"),
        })
    return rows


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
        from tools.file_parser import parse_resume_file

        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(suffix=ext)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            parsed = parse_resume_file(tmp_path)
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
    return text[:MAX_RESUME_CHARS], None


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
def api_config():
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
    }


@app.get("/api/status")
def api_status():
    return {"deployment_mode": "vercel", "public": True,
            "default_model": config.DEFAULT_MODEL}


@app.post("/api/runs")
async def create_run(
    request: Request,
    invite_code: str = Form(""),
    jd_text: str = Form(""),
    resume_text: str = Form(""),
    model: str = Form(""),
    reasoning: str = Form(""),
    job_search: str = Form("0"),
    resume_file: UploadFile = File(None),
):
    if not run_security.verify_invite(invite_code):
        return JSONResponse({"error": "邀请码无效"}, status_code=403)

    try:
        model, reasoning = config.validate_model_reasoning(model, reasoning)
    except ValueError as error:
        return JSONResponse({"error": str(error)}, status_code=400)

    if not jd_text.strip():
        return JSONResponse(
            {"error": "当前预览仅支持粘贴目标 JD 的流程；请提供职位描述"},
            status_code=422,
        )

    text, error_response = await _extract_resume(resume_file, resume_text)
    if error_response is not None:
        return error_response

    now = time.time()
    payload = {
        "resume_text": text,
        "jd_text": jd_text.strip(),
        "model": model,
        "reasoning": reasoning,
        "job_search": False,
        "deadline_epoch": now + float(config.RUN_TIMEOUT),
    }

    run_id = await _backend().start(payload)
    token, expires_at = run_security.issue_run_token(run_id, ttl_seconds=RUN_TOKEN_TTL)

    trace = _trace()
    await trace.write_meta(
        run_id,
        {"model": model, "reasoning": reasoning, "status": "running",
         "created_at": int(now)},
        created_epoch=now,
    )
    await trace.write_stage(
        run_id, 1, {"status": "completed", "stage_id": 1, "name": "简历解析"},
        created_epoch=now,
    )

    return JSONResponse(
        {
            "run_id": run_id,
            "token": token,
            "expires_at": expires_at,
            "deadline_epoch": payload["deadline_epoch"],
            "stages": _stage_rows(await trace.read_stages(run_id)),
            "model": model,
            "reasoning": reasoning,
        },
        status_code=202,
    )


def _authorize(request, run_id):
    token = _bearer(request)
    return run_security.verify_run_token(token, run_id)


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str, request: Request):
    if not _authorize(request, run_id):
        return JSONResponse({"error": "未授权"}, status_code=401)

    backend = _backend()
    trace = _trace()
    sdk_status = await backend.status(run_id)
    stage_docs = await trace.read_stages(run_id)
    overall = {
        "pending": "running", "running": "running", "completed": "completed",
        "failed": "failed", "cancelled": "cancelled",
    }.get(sdk_status, sdk_status)

    response = {
        "run_id": run_id,
        "status": overall,
        "stages": _stage_rows(stage_docs),
    }
    if sdk_status == "completed":
        result = await backend.result(run_id) or {}
        response["status"] = result.get("status", "completed")
        response["report"] = result.get("report", "")
        response["safe_to_deliver"] = bool(result.get("safe_to_deliver"))
        response["unresolved_fixes"] = result.get("unresolved_fixes", [])
        response["model"] = result.get("model", "")
        response["reasoning"] = result.get("reasoning", "")
    return response


@app.post("/api/runs/{run_id}/cancel")
async def cancel_run(run_id: str, request: Request):
    if not _authorize(request, run_id):
        return JSONResponse({"error": "未授权"}, status_code=401)
    await _trace().write_cancel(run_id)
    return JSONResponse({"run_id": run_id, "status": "cancelling"}, status_code=202)


@app.delete("/api/runs/{run_id}")
async def delete_run(run_id: str, request: Request):
    if not _authorize(request, run_id):
        return JSONResponse({"error": "未授权"}, status_code=401)
    await _trace().delete_run(run_id)
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
