"""与用户交互的工具。"""

import os
import select
import sys
import time
import uuid

import config
from trace_catalog import emit_trace


NEUTRAL_MISSING_INFO_ANSWER = (
    "（用户未提供补充信息。请基于现有内容继续分析，"
    "并在最终报告中标注该信息缺失。）"
)


def get_substantive_answer(result):
    """Return a verified user answer, while accepting legacy result payloads."""
    if not isinstance(result, dict):
        return None
    answer = str(result.get("answer") or "").strip()
    answered = result.get("answered")
    if answered is not None and answered is not True:
        return None
    if result.get("timed_out") or result.get("skipped"):
        return None
    if not answer or answer == NEUTRAL_MISSING_INFO_ANSWER:
        return None
    return answer


def _timed_input(prompt, timeout):
    """POSIX上限时等待stdin；其他环境回退到普通input。"""
    if os.name == "posix":
        try:
            sys.stdin.fileno()
            print(prompt, end="", flush=True)
            ready, _, _ = select.select([sys.stdin], [], [], max(0.0, timeout))
            if not ready:
                print()
                return "", True
            return input().strip(), False
        except OSError as error:
            if getattr(error, "is_run_deadline", False):
                raise
        except (AttributeError, TypeError, ValueError):
            pass
    try:
        return input(prompt).strip(), False
    except EOFError:
        return "", False


def ask_user(question, context=None):
    """向用户追问；保留中性answer兼容字段，并明确回答状态。"""
    print("\n" + "=" * 50)
    print("💬 Agent需要补充信息")
    print("=" * 50)
    if context:
        print(f"背景：{context}")
    print(f"问题：{question}")

    span = f"wait-{uuid.uuid4().hex[:12]}"
    started = time.monotonic()
    emit_trace(
        "user_wait.started",
        span=span,
        data={
            "timeout_seconds": config.ASK_TIMEOUT,
            "question_chars": len(str(question or "")),
            "context_provided": bool(context),
        },
    )
    try:
        answer, timed_out = _timed_input(
            "请输入回答（直接回车表示跳过）：",
            config.ASK_TIMEOUT,
        )
    except EOFError:
        answer, timed_out = "", False
    answer = str(answer or "").strip()
    timed_out = bool(timed_out)
    answered = bool(answer)
    skipped = not answered and not timed_out
    if not answered:
        answer = NEUTRAL_MISSING_INFO_ANSWER
    emit_trace(
        "user_wait.completed",
        span=span,
        data={
            "wait_ms": max(0, int((time.monotonic() - started) * 1000)),
            "timed_out": timed_out,
            "answered": answered,
            "skipped": skipped,
        },
    )
    return {
        "success": True,
        "question": question,
        "answer": answer,
        "answered": answered,
        "timed_out": timed_out,
        "skipped": skipped,
    }
