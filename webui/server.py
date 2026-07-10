"""简历优化Agent Web UI 后端（支持本地个人模式 + 公网多人模式）

以子进程方式运行 agent.py（零侵入，不改动Agent核心代码）：
- POST /api/run        启动一次分析（文件上传 / 粘贴文本 / 演示模式，支持Mock与BYOK）
- GET  /api/events/:id SSE实时推送ReAct推理流
- POST /api/answer/:id Agent中途追问时，把用户回答写回子进程stdin
- POST /api/stop/:id   终止运行
- GET  /api/reports    历史报告（公网模式下仅本会话可见）

模式切换：
- 本地模式（默认）：报告存 output/，历史全量可见，无配额限制
- 公网模式（AGENT_PUBLIC=1）：
  * Cookie会话隔离：只能访问自己的任务与报告
  * 每日免费额度（按IP），自带API Key（BYOK）不占额度
  * 频率限制 + 并发上限
  * 用后即焚：上传的简历与生成的报告文件在运行结束后立即删除（报告仅存会话内存）

启动：python webui/server.py  （默认 http://127.0.0.1:7860 ）
"""
import codecs
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from datetime import date
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

BASE_DIR = Path(__file__).resolve().parent      # webui/
PROJECT_DIR = BASE_DIR.parent                   # resume-agent/
STATIC_DIR = BASE_DIR / "static"
UPLOAD_DIR = BASE_DIR / "uploads"
RUNS_DIR = BASE_DIR / "runs"                    # 公网模式的每任务临时输出目录
OUTPUT_DIR = PROJECT_DIR / "output"
QUOTA_FILE = BASE_DIR / "quota.json"

sys.path.insert(0, str(PROJECT_DIR))
import config as agent_config  # noqa: E402

# ===== 公网模式配置（都可用环境变量覆盖） =====
PUBLIC = os.environ.get("AGENT_PUBLIC") == "1"
FREE_PER_DAY = int(os.environ.get("AGENT_FREE_PER_DAY", "2"))       # 每IP每日免费真实分析次数
RUNS_PER_HOUR = int(os.environ.get("AGENT_RUNS_PER_HOUR", "6"))     # 每IP每小时启动上限（含BYOK）
MOCK_PER_HOUR = int(os.environ.get("AGENT_MOCK_PER_HOUR", "20"))    # 每IP每小时Mock演示上限
MAX_CONCURRENT = int(os.environ.get("AGENT_MAX_CONCURRENT", "3"))   # 全局并发任务上限
MAX_UPLOAD_MB = int(os.environ.get("AGENT_MAX_UPLOAD_MB", "5"))
TRUST_PROXY = os.environ.get("AGENT_TRUST_PROXY") == "1"            # 反代后置1以读取X-Forwarded-For
SESSION_TTL = 24 * 3600
SESSION_REPORT_CAP = 5

ALLOWED_RESUME_EXT = {".pdf", ".docx", ".doc", ".txt", ".md", ".markdown"}
REPORT_NAME_RE = re.compile(r"^resume_report_\d{8}_\d{6}\.md$")
BASE_URL_RE = re.compile(r"^https?://[\w\-.:/]+$")
MODEL_RE = re.compile(r"^[\w\-./: ]{1,100}$")
ANSWER_PROMPT = "请输入回答"

app = FastAPI(title="简历优化Agent UI")

JOBS = {}
JOBS_LOCK = threading.Lock()
SESSIONS = {}                                   # sid -> {created,last,reports:[...]}
SESSIONS_LOCK = threading.Lock()
RATE = {}                                       # ip -> [启动时间戳]
RATE_LOCK = threading.Lock()
QUOTA_LOCK = threading.Lock()


# ==================== 会话 / 身份 ====================

def _client_ip(request: Request):
    if TRUST_PROXY:
        fwd = request.headers.get("x-forwarded-for", "")
        if fwd:
            return fwd.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "unknown")[:64]


def _get_session(request: Request, response: Optional[Response] = None):
    """取/建会话：cookie里的sid；response非空时补种cookie"""
    sid = request.cookies.get("agent_sid", "")
    if not re.match(r"^[a-f0-9]{32}$", sid):
        sid = uuid.uuid4().hex
    with SESSIONS_LOCK:
        sess = SESSIONS.get(sid)
        if not sess:
            sess = {"created": time.time(), "last": time.time(), "reports": []}
            SESSIONS[sid] = sess
        sess["last"] = time.time()
    if response is not None:
        response.set_cookie("agent_sid", sid, max_age=SESSION_TTL, httponly=True, samesite="lax")
    return sid, sess


# ==================== 配额 / 限流 ====================

def _load_quota():
    try:
        data = json.loads(QUOTA_FILE.read_text(encoding="utf-8"))
        if data.get("date") == date.today().isoformat():
            return data
    except (OSError, ValueError):
        pass
    return {"date": date.today().isoformat(), "ips": {}}


def _save_quota(data):
    try:
        tmp = QUOTA_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(QUOTA_FILE)
    except OSError:
        pass


def _quota_left(ip):
    with QUOTA_LOCK:
        data = _load_quota()
        return max(0, FREE_PER_DAY - data["ips"].get(ip, 0))


def _quota_consume(ip):
    with QUOTA_LOCK:
        data = _load_quota()
        used = data["ips"].get(ip, 0)
        if used >= FREE_PER_DAY:
            return False
        data["ips"][ip] = used + 1
        _save_quota(data)
        return True


def _rate_check(ip, mock):
    """滑动窗口限流 + 并发上限。超限抛429"""
    now = time.time()
    limit = MOCK_PER_HOUR if mock else RUNS_PER_HOUR
    with RATE_LOCK:
        stamps = [t for t in RATE.get(ip, []) if now - t < 3600]
        if len(stamps) >= limit:
            raise HTTPException(429, "操作太频繁了，请一小时后再试")
        stamps.append(now)
        RATE[ip] = stamps
    with JOBS_LOCK:
        running = [j for j in JOBS.values() if j.proc.poll() is None]
        if len(running) >= MAX_CONCURRENT:
            raise HTTPException(429, "当前分析任务较多，请稍等片刻再试")
        if any(j.ip == ip for j in running):
            raise HTTPException(429, "你已有一个分析在进行中，请等它完成")


# ==================== 任务 ====================

class Job:
    """一次Agent运行：子进程 + 事件缓冲（SSE从这里回放/续播）"""

    def __init__(self, proc, meta):
        self.id = uuid.uuid4().hex[:12]
        self.proc = proc
        self.meta = meta
        self.created = time.time()
        self.sid = meta.get("sid", "")
        self.ip = meta.get("ip", "")
        self.ephemeral = meta.get("ephemeral", False)
        self.run_dir = meta.get("run_dir")          # 公网模式的临时输出目录
        self.temp_files = meta.get("temp_files", [])  # 上传/粘贴落盘的文件，结束即删
        self.events = []
        self.lock = threading.Lock()
        self.done = False
        self.exit_code = None
        self.report_path = None
        self.report_text = None
        self.report_struct = None
        self.waiting_answer = False

    def emit(self, event):
        with self.lock:
            self.events.append(event)

    def snapshot(self, start):
        with self.lock:
            return list(self.events[start:])


def _reader_thread(job):
    """持续读取子进程stdout：拆行→事件；识别追问/报告保存/结束；公网模式用后即焚"""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    buffer = ""
    asking = False
    ask_question, ask_context = None, None
    suppress = False

    def handle_line(line):
        nonlocal asking, ask_question, ask_context, suppress
        text = line.rstrip("\r")
        if suppress:
            return
        if "💾 报告已保存：" in text:
            job.report_path = text.split("💾 报告已保存：", 1)[1].strip()
            if not job.ephemeral:
                job.emit({"type": "line", "text": text})
            suppress = True
            return
        if "💬 Agent需要补充信息" in text:
            asking, ask_question, ask_context = True, None, None
        elif asking and text.startswith("背景："):
            ask_context = text[len("背景："):].strip()
        elif asking and text.startswith("问题："):
            ask_question = text[len("问题："):].strip()
        job.emit({"type": "line", "text": text})

    fd = job.proc.stdout
    while True:
        chunk = fd.read(1)
        if chunk == b"" or chunk is None:
            break
        extra = fd.read1(65536) if hasattr(fd, "read1") else b""
        buffer += decoder.decode(chunk + extra)
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            handle_line(line)
        if ANSWER_PROMPT in buffer:
            handle_line(buffer)
            buffer = ""
            if asking and ask_question:
                job.waiting_answer = True
                job.emit({"type": "question", "question": ask_question,
                          "context": ask_context or ""})
            asking = False

    if buffer.strip():
        handle_line(buffer)
    job.proc.wait()
    job.exit_code = job.proc.returncode

    # 读取报告文件（限制在项目目录内，防路径穿越）；结构化简历在同名.json
    if job.report_path:
        try:
            path = (PROJECT_DIR / job.report_path).resolve() \
                if not os.path.isabs(job.report_path) else Path(job.report_path).resolve()
            if str(path).startswith(str(PROJECT_DIR.resolve())) and path.is_file():
                job.report_text = path.read_text(encoding="utf-8")
                struct_path = path.with_suffix(".json")
                if struct_path.is_file():
                    try:
                        job.report_struct = json.loads(struct_path.read_text(encoding="utf-8"))
                    except ValueError:
                        job.report_struct = None
        except Exception as error:  # noqa: BLE001
            job.emit({"type": "line", "text": f"⚠️ 读取报告文件失败：{error}"})

    if job.report_text:
        name = Path(job.report_path).name if job.report_path else f"resume_report_{job.id}.md"
        job.emit({"type": "report", "path": "" if job.ephemeral else (job.report_path or ""),
                  "name": name, "ephemeral": job.ephemeral, "content": job.report_text,
                  "struct": job.report_struct})
        if job.ephemeral:
            _session_store_report(job.sid, job.id, name, job.report_text, job.report_struct)

    # 用后即焚：删除临时输出目录与上传文件
    if job.ephemeral:
        _burn(job)
    job.emit({"type": "exit", "code": job.exit_code})
    job.done = True


def _burn(job):
    """删除本次运行落盘的一切：临时输出目录、上传的简历/JD文件"""
    try:
        if job.run_dir and str(job.run_dir).startswith(str(RUNS_DIR)):
            shutil.rmtree(job.run_dir, ignore_errors=True)
    except OSError:
        pass
    for f in job.temp_files:
        try:
            Path(f).unlink(missing_ok=True)
        except OSError:
            pass


def _session_store_report(sid, key, name, content, struct=None):
    with SESSIONS_LOCK:
        sess = SESSIONS.get(sid)
        if not sess:
            return
        sess["reports"].insert(0, {
            "key": key, "name": name, "size": len(content.encode("utf-8")),
            "mtime": time.strftime("%Y-%m-%d %H:%M"), "content": content, "struct": struct,
        })
        del sess["reports"][SESSION_REPORT_CAP:]


def _sweeper():
    """后台清扫：过期会话、残留上传/临时目录、过期限流记录"""
    while True:
        time.sleep(1800)
        now = time.time()
        # 超时任务清扫：运行超过30分钟的子进程强制终止（如追问后用户离开的挂起任务）
        with JOBS_LOCK:
            for job in list(JOBS.values()):
                try:
                    if job.proc.poll() is None and now - job.created > 1800:
                        job.proc.terminate()
                except OSError:
                    pass
        with SESSIONS_LOCK:
            for sid in [s for s, v in SESSIONS.items() if now - v["last"] > SESSION_TTL]:
                SESSIONS.pop(sid, None)
        with RATE_LOCK:
            for ip in list(RATE):
                RATE[ip] = [t for t in RATE[ip] if now - t < 3600]
                if not RATE[ip]:
                    RATE.pop(ip, None)
        for folder, ttl in ((UPLOAD_DIR, 2 * 3600), (RUNS_DIR, 2 * 3600)):
            if not folder.is_dir():
                continue
            for item in folder.iterdir():
                try:
                    if now - item.stat().st_mtime > ttl:
                        shutil.rmtree(item, ignore_errors=True) if item.is_dir() else item.unlink()
                except OSError:
                    pass


threading.Thread(target=_sweeper, daemon=True).start()


# ==================== 输入落盘 ====================

def _save_upload(upload):
    suffix = Path(upload.filename or "").suffix.lower()
    if suffix not in ALLOWED_RESUME_EXT:
        raise HTTPException(400, f"不支持的简历格式：{suffix or '未知'}（支持PDF/Word/txt/Markdown）")
    data = upload.file.read()
    if len(data) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(400, f"文件过大（上限{MAX_UPLOAD_MB}MB）")
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    safe_stem = re.sub(r"[^\w一-鿿.-]", "_", Path(upload.filename).stem)[:60] or "resume"
    path = UPLOAD_DIR / f"{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}_{safe_stem}{suffix}"
    path.write_bytes(data)
    return path


def _write_temp_text(text, label):
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    path = UPLOAD_DIR / f"{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}_{label}.txt"
    path.write_text(text, encoding="utf-8")
    return path


# ==================== 路由 ====================

@app.get("/")
def index(request: Request):
    response = FileResponse(STATIC_DIR / "index.html")
    _get_session(request, response)
    return response


@app.get("/api/status")
def status(request: Request):
    response = JSONResponse({})
    sid, _ = _get_session(request, response)
    ip = _client_ip(request)
    payload = {
        "model": agent_config.MODEL_NAME,
        "base_url": agent_config.API_BASE_URL,
        "has_key": bool(agent_config.API_KEY),
        "mock_env": os.environ.get("AGENT_MOCK") == "1",
        "max_steps": agent_config.MAX_STEPS,
        "public": PUBLIC,
        "free_per_day": FREE_PER_DAY if PUBLIC else None,
        "free_left": _quota_left(ip) if PUBLIC else None,
    }
    response = JSONResponse(payload)
    response.set_cookie("agent_sid", sid, max_age=SESSION_TTL, httponly=True, samesite="lax")
    return response


@app.post("/api/run")
def run(request: Request,
        mode: str = Form("custom"),
        resume_text: str = Form(""),
        jd_text: str = Form(""),
        preferences: str = Form(""),
        mock: str = Form("0"),
        api_key: str = Form(""),
        base_url: str = Form(""),
        model: str = Form(""),
        reasoning: str = Form(""),
        resume_file: Optional[UploadFile] = File(None)):
    sid, _ = _get_session(request)
    ip = _client_ip(request)
    is_mock = mock == "1"
    api_key = api_key.strip()[:200]
    base_url = base_url.strip()[:300]
    model = model.strip()[:100]
    byok = bool(api_key)
    reasoning = reasoning.strip().lower()
    if reasoning and reasoning not in agent_config.REASONING_LEVELS:
        raise HTTPException(400, f"推理强度取值仅限：{' / '.join(agent_config.REASONING_LEVELS)}")
    if len(resume_text) > 200_000 or len(jd_text) > 50_000:
        raise HTTPException(400, "文本过长，请精简后重试")
    if base_url and not BASE_URL_RE.match(base_url):
        raise HTTPException(400, "Base URL格式不正确（需以http(s)://开头）")
    if model and not MODEL_RE.match(model):
        raise HTTPException(400, "模型名格式不正确")

    quota_used = False
    if PUBLIC:
        _rate_check(ip, is_mock)
        if not is_mock and not byok:
            if not _quota_consume(ip):
                raise HTTPException(429,
                    f"今日{FREE_PER_DAY}次免费额度已用完。在「高级设置」填写你自己的API Key即可继续（不限次数），或明天再来。")
            quota_used = True

    cmd = [sys.executable, "-u", "agent.py"]
    meta = {"mode": mode, "sid": sid, "ip": ip, "ephemeral": PUBLIC, "temp_files": []}

    if mode == "demo":
        cmd.append("--demo")
    else:
        if resume_file is not None and (resume_file.filename or "").strip():
            resume_path = _save_upload(resume_file)
        elif resume_text.strip():
            resume_path = _write_temp_text(resume_text.strip(), "resume")
        else:
            raise HTTPException(400, "请上传简历文件或粘贴简历文本")
        cmd.append(str(resume_path))
        meta["temp_files"].append(str(resume_path))
        if jd_text.strip():
            jd_path = _write_temp_text(jd_text.strip(), "jd")
            cmd.append(str(jd_path))
            meta["temp_files"].append(str(jd_path))
        elif preferences.strip():
            cmd += ["--prefer", preferences.strip()[:200]]
        meta["job_search_mode"] = not jd_text.strip()

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    if is_mock:
        env["AGENT_MOCK"] = "1"
        env["AGENT_MOCK_ASK"] = "1"   # Web演示时展示"用户追问"环节（追问卡片可回答/跳过）
    else:
        env.pop("AGENT_MOCK", None)
        if reasoning:
            env["AGENT_REASONING_EFFORT"] = reasoning
        else:
            env.pop("AGENT_REASONING_EFFORT", None)
        if byok:
            # BYOK：仅本次子进程使用，不落盘不记录
            env.pop("ZENMUX_API_KEY", None)     # ZENMUX优先级更高，必须清掉
            env["OPENAI_API_KEY"] = api_key
            if base_url:
                env["AGENT_BASE_URL"] = base_url
            if model:
                env["AGENT_MODEL"] = model
        elif not agent_config.API_KEY:
            raise HTTPException(400, "未检测到API密钥：请在「高级设置」填写你的API Key，或勾选Mock演示模式")

    job_id_dir = uuid.uuid4().hex[:12]
    if PUBLIC:
        run_dir = RUNS_DIR / job_id_dir
        run_dir.mkdir(parents=True, exist_ok=True)
        env["AGENT_OUTPUT_DIR"] = str(run_dir)
        meta["run_dir"] = str(run_dir)

    proc = subprocess.Popen(
        cmd, cwd=str(PROJECT_DIR), env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    job = Job(proc, meta)
    with JOBS_LOCK:
        JOBS[job.id] = job
    threading.Thread(target=_reader_thread, args=(job,), daemon=True).start()
    return {"job_id": job.id, "mock": is_mock, "byok": byok,
            "model": (model or agent_config.MODEL_NAME) if byok else agent_config.MODEL_NAME,
            "quota_used": quota_used,
            "free_left": _quota_left(ip) if PUBLIC else None}


def _get_job(job_id, request: Request = None):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    if PUBLIC and request is not None:
        sid = request.cookies.get("agent_sid", "")
        if job.sid != sid:
            raise HTTPException(403, "无权访问该任务")
    return job


@app.get("/api/events/{job_id}")
def events(job_id: str, request: Request):
    job = _get_job(job_id, request)

    def generate():
        cursor = 0
        idle = 0.0
        while True:
            batch = job.snapshot(cursor)
            if batch:
                cursor += len(batch)
                idle = 0.0
                for event in batch:
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            elif job.done:
                break
            else:
                time.sleep(0.15)
                idle += 0.15
                if idle >= 15:
                    idle = 0.0
                    yield ": keepalive\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/answer/{job_id}")
async def answer(job_id: str, payload: dict, request: Request):
    job = _get_job(job_id, request)
    if job.done or job.proc.poll() is not None:
        raise HTTPException(409, "任务已结束")
    text = str(payload.get("answer", "")).replace("\n", " ").strip()[:2000]
    try:
        job.proc.stdin.write((text + "\n").encode("utf-8"))
        job.proc.stdin.flush()
    except Exception as error:  # noqa: BLE001
        raise HTTPException(500, f"写入回答失败：{error}")
    job.waiting_answer = False
    job.emit({"type": "answered", "answer": text or "（跳过）"})
    return {"ok": True}


@app.post("/api/stop/{job_id}")
def stop(job_id: str, request: Request):
    job = _get_job(job_id, request)
    if job.proc.poll() is None:
        job.proc.terminate()
    return {"ok": True}


@app.get("/api/reports")
def reports(request: Request):
    if PUBLIC:
        sid, sess = _get_session(request)
        return [{"key": r["key"], "name": r["name"], "size": r["size"], "mtime": r["mtime"]}
                for r in sess["reports"]]
    if not OUTPUT_DIR.is_dir():
        return []
    items = []
    for path in sorted(OUTPUT_DIR.glob("resume_report_*.md"), reverse=True):
        items.append({"key": path.name, "name": path.name, "size": path.stat().st_size,
                      "mtime": time.strftime("%Y-%m-%d %H:%M", time.localtime(path.stat().st_mtime))})
    return items[:50]


@app.get("/api/reports/{key}")
def report_content(key: str, request: Request):
    if PUBLIC:
        sid, sess = _get_session(request)
        for r in sess["reports"]:
            if r["key"] == key:
                return JSONResponse({"name": r["name"], "content": r["content"],
                                     "struct": r.get("struct")})
        raise HTTPException(404, "报告不存在或已过期（公网模式仅保留本会话最近的报告）")
    if not REPORT_NAME_RE.match(key):
        raise HTTPException(400, "非法的报告文件名")
    path = OUTPUT_DIR / key
    if not path.is_file():
        raise HTTPException(404, "报告不存在")
    struct = None
    struct_path = path.with_suffix(".json")
    if struct_path.is_file():
        try:
            struct = json.loads(struct_path.read_text(encoding="utf-8"))
        except ValueError:
            struct = None
    return JSONResponse({"name": key, "content": path.read_text(encoding="utf-8"),
                         "struct": struct})


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("AGENT_UI_PORT", "7860"))
    mode = "公网多人模式" if PUBLIC else "本地个人模式"
    print(f"🌐 简历优化Agent UI（{mode}）→ http://127.0.0.1:{port}")
    if PUBLIC:
        print(f"   免费额度：每IP每日{FREE_PER_DAY}次 ｜ 限流：{RUNS_PER_HOUR}次/时 ｜ 并发：{MAX_CONCURRENT}")
    uvicorn.run(app, host=os.environ.get("AGENT_UI_HOST", "127.0.0.1"), port=port, log_level="warning")
