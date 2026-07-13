"""隐私安全的运行事件目录。"""

import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


TRACE_SCHEMA = "resume-agent.trace.v1"
TRACE_PREFIX = "@@TRACE@@"

_SENSITIVE_KEYS = {
    "answer",
    "api_key",
    "authorization",
    "content",
    "credential",
    "credentials",
    "jd",
    "jd_text",
    "messages",
    "password",
    "prompt",
    "question",
    "raw",
    "raw_arguments",
    "response",
    "resume",
    "resume_text",
    "secret",
    "system_prompt",
    "user_answer",
}
_CREDENTIAL_RE = re.compile(
    r"(?i)(bearer\s+\S+|sk-[a-z0-9_-]{8,}|(?:api[_-]?key|token|password)=\S+)"
)


def _safe_run_id(value):
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "-", str(value or "").strip())[:96]
    return cleaned.strip(".-") or uuid.uuid4().hex


def _is_sensitive_key(key):
    text = str(key).strip()
    text = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", text)
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text)
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").lower()
    return (
        normalized in _SENSITIVE_KEYS
        or normalized in ("key", "keys")
        or normalized.endswith("_key")
        or normalized.endswith("_keys")
        or normalized.endswith("_api_key")
        or normalized.endswith("_password")
        or normalized.endswith("_secret")
        or normalized.endswith("_credential")
    )


def _sanitize(value, key=None):
    if key is not None and _is_sensitive_key(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(item_key): _sanitize(item, item_key) for item_key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = str(value)
    text = _CREDENTIAL_RE.sub("[REDACTED]", text)
    return text if len(text) <= 500 else text[:500] + "...(truncated)"


def _raise_if_run_deadline(error):
    if getattr(error, "is_run_deadline", False):
        raise error


def _emergency_trace_record(event, kwargs, error):
    data = _sanitize(kwargs.get("data") or {})
    if not isinstance(data, dict):
        data = {"data_type": type(data).__name__}
    data["error_class"] = type(error).__name__
    return {
        "schema": TRACE_SCHEMA,
        "run_id": _safe_run_id(os.environ.get("AGENT_RUN_ID")),
        "seq": 0,
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        ),
        "mono_ms": 0,
        "event": str(event),
        "level": str(kwargs.get("level", "info")),
        "span": str(kwargs["span"]) if kwargs.get("span") is not None else None,
        "parent": str(kwargs["parent"]) if kwargs.get("parent") is not None else None,
        "step": kwargs.get("step"),
        "data": data,
    }


class TraceCatalog:
    """为单次运行写 JSONL，并把同一事件镜像到 stdout。"""

    def __init__(self, run_id=None, trace_dir=None):
        self.run_id = _safe_run_id(run_id or os.environ.get("AGENT_RUN_ID"))
        self.started_mono = time.monotonic()
        self.started_ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        )
        self._seq = 0
        self._lock = threading.Lock()
        configured_dir = trace_dir or os.environ.get("AGENT_TRACE_DIR")
        if configured_dir:
            self.trace_dir = Path(configured_dir)
        else:
            output_dir = Path(os.environ.get("AGENT_OUTPUT_DIR", "output"))
            self.trace_dir = output_dir / "traces" / self.run_id
        self.trace_path = self.trace_dir / "trace.jsonl"
        try:
            self._prepare_path()
        except Exception as error:
            _raise_if_run_deadline(error)
            self.trace_path = None

    def _prepare_path(self):
        self.trace_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.trace_dir, 0o700)
        except OSError:
            pass
        descriptor = os.open(
            self.trace_path,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            0o600,
        )
        os.close(descriptor)
        try:
            os.chmod(self.trace_path, 0o600)
        except OSError:
            pass

    def emit(self, event, level="info", span=None, parent=None, step=None, data=None):
        with self._lock:
            self._seq += 1
            record = {
                "schema": TRACE_SCHEMA,
                "run_id": self.run_id,
                "seq": self._seq,
                "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
                    "+00:00", "Z"
                ),
                "mono_ms": max(0, int((time.monotonic() - self.started_mono) * 1000)),
                "event": str(event),
                "level": str(level),
                "span": str(span) if span is not None else None,
                "parent": str(parent) if parent is not None else None,
                "step": step,
                "data": _sanitize(data or {}),
            }
            serialized = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
            if self.trace_path is not None:
                try:
                    with self.trace_path.open("a", encoding="utf-8") as trace_file:
                        trace_file.write(serialized + "\n")
                        trace_file.flush()
                except Exception as error:
                    _raise_if_run_deadline(error)
                    self.trace_path = None
            print(TRACE_PREFIX + serialized, flush=True)
            if (
                self.trace_path is not None
                and record["event"] in ("run.completed", "run.error")
            ):
                try:
                    self._write_summary(record)
                except Exception as error:
                    _raise_if_run_deadline(error)
                    self.trace_path = None
            return record

    def _write_summary(self, terminal_event):
        data = terminal_event["data"]
        status = data.get("status") if terminal_event["event"] == "run.completed" else "error"
        summary = {
            "schema": TRACE_SCHEMA,
            "run_id": self.run_id,
            "status": status or "completed",
            "terminal_event": terminal_event["event"],
            "terminal_seq": terminal_event["seq"],
            "timing": {
                "started_ts": self.started_ts,
                "ended_ts": terminal_event["ts"],
                "duration_ms": data.get("duration_ms", terminal_event["mono_ms"]),
                "ended_mono_ms": terminal_event["mono_ms"],
            },
            "counts": {
                key: data[key]
                for key in (
                    "steps", "revision_rounds", "llm_calls", "tool_calls",
                    "retries", "input_tokens", "output_tokens", "total_tokens",
                )
                if key in data
            },
            "result": {
                key: data[key]
                for key in (
                    "report_available", "partial_results", "stage", "error_class",
                    "error_category",
                )
                if key in data
            },
        }
        summary_path = self.trace_dir / "summary.json"
        temp_path = self.trace_dir / f".summary.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        descriptor = None
        try:
            descriptor = os.open(
                temp_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as summary_file:
                descriptor = None
                json.dump(summary, summary_file, ensure_ascii=False, separators=(",", ":"))
                summary_file.write("\n")
                summary_file.flush()
                os.fsync(summary_file.fileno())
            os.replace(temp_path, summary_path)
            try:
                os.chmod(summary_path, 0o600)
            except OSError:
                pass
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass


_catalog = None
_catalog_lock = threading.Lock()


def get_trace_catalog():
    global _catalog
    if _catalog is None:
        with _catalog_lock:
            if _catalog is None:
                _catalog = TraceCatalog()
    return _catalog


def emit_trace(event, **kwargs):
    """记录事件；事件目录故障不应中断主业务。"""
    try:
        return get_trace_catalog().emit(event, **kwargs)
    except Exception as error:
        _raise_if_run_deadline(error)
        record = _emergency_trace_record(event, kwargs, error)
        print(
            TRACE_PREFIX
            + json.dumps(record, ensure_ascii=False, separators=(",", ":")),
            flush=True,
        )
        return record


def _reset_trace_catalog_for_tests():
    global _catalog
    with _catalog_lock:
        _catalog = None
