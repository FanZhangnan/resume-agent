"""Opt-in live model benchmark using only a fixed synthetic fixture."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time

import config
from llm_client import LLMClient
from runtime_context import RunSettings, use_run_settings


SCHEMA_VERSION = "resume-agent.model-benchmark.v1"
OPERATIONS = (
    "extraction_json", "tool_call", "grounded_rewrite", "verifier"
)

_EXTRACTION_TEXT = (
    "Synthetic candidate SYNTHETIC-001 built a Python reporting tool. "
    "Return JSON with candidate_id and skills."
)
_GROUNDING_TEXT = (
    "Fact F1: Built a Python reporting tool. Return JSON with fact_id F1 and "
    "a concise rewrite. Do not add numbers, employers, dates, or outcomes."
)
_TOOL = {
    "type": "function",
    "function": {
        "name": "record_skill_match",
        "description": "Record a match to the supplied synthetic fact.",
        "parameters": {
            "type": "object",
            "properties": {
                "fact_id": {"type": "string", "enum": ["F1"]},
            },
            "required": ["fact_id"],
            "additionalProperties": False,
        },
    },
}


def _strict_json(text):
    try:
        value = json.loads(str(text or ""))
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _operation_result(operation, model, reasoning, latency_ms, completed,
                      json_valid=None, tool_call_success=None,
                      verifier_status=None, metrics=None, error_class=None):
    metrics = metrics if isinstance(metrics, dict) else {}
    result = {
        "operation": operation,
        "model": model,
        "reasoning": reasoning,
        "latency_ms": latency_ms,
        "completed": bool(completed),
        "json_valid": json_valid,
        "tool_call_success": tool_call_success,
        "input_tokens": metrics.get("input_tokens"),
        "output_tokens": metrics.get("output_tokens"),
        "total_tokens": metrics.get("total_tokens"),
        "verifier_status": verifier_status,
    }
    if error_class:
        result["error_class"] = error_class
    return result


def _run_extraction(client, context):
    text = client.simple_ask(
        _EXTRACTION_TEXT,
        system="This is a synthetic benchmark. Return strict JSON only.",
        temperature=0,
        max_tokens=256,
        operation="extraction_json",
    )
    value = _strict_json(text)
    valid = bool(
        value
        and value.get("candidate_id") == "SYNTHETIC-001"
        and isinstance(value.get("skills"), list)
    )
    return {"json_valid": valid}


def _run_tool_call(client, context):
    message = client.chat(
        [{"role": "user", "content": "Record synthetic fact F1."}],
        tools=[_TOOL],
        temperature=0,
        max_tokens=128,
        operation="tool_call",
    )
    calls = getattr(message, "tool_calls", None) or []
    valid_arguments = False
    success = False
    if calls:
        function = getattr(calls[0], "function", None)
        arguments = _strict_json(getattr(function, "arguments", ""))
        valid_arguments = arguments is not None
        success = bool(
            function
            and getattr(function, "name", "") == "record_skill_match"
            and arguments == {"fact_id": "F1"}
        )
    return {
        "json_valid": valid_arguments,
        "tool_call_success": success,
    }


def _run_grounded_rewrite(client, context):
    text = client.simple_ask(
        _GROUNDING_TEXT,
        system="Use only the supplied synthetic fact. Return strict JSON only.",
        temperature=0,
        max_tokens=256,
        operation="grounded_rewrite",
    )
    value = _strict_json(text)
    valid = bool(
        value
        and value.get("fact_id") == "F1"
        and isinstance(value.get("rewrite"), str)
        and value.get("rewrite", "").strip()
    )
    if valid:
        # Private in-memory handoff only; never pass this value to result assembly.
        context["grounded_rewrite"] = value["rewrite"].strip()
    return {"json_valid": valid}


def _run_verifier(client, context):
    rewrite = context.get("grounded_rewrite")
    if not rewrite:
        return {
            "json_valid": None,
            "verifier_status": "not_completed",
            "_completed": False,
        }
    verifier_text = (
        "Audit the candidate rewrite against its only allowed source fact.\n"
        "Allowed source fact F1: Built a Python reporting tool.\n"
        "<candidate_rewrite>\n"
        f"{rewrite}\n"
        "</candidate_rewrite>\n"
        "Return strict JSON with passed, safe_to_deliver, and required_fixes."
    )
    text = client.simple_ask(
        verifier_text,
        system="Audit only the supplied synthetic fact. Return strict JSON only.",
        temperature=0,
        max_tokens=256,
        operation="verifier",
    )
    value = _strict_json(text)
    valid = bool(
        value
        and isinstance(value.get("passed"), bool)
        and isinstance(value.get("safe_to_deliver"), bool)
        and isinstance(value.get("required_fixes"), list)
    )
    passed = bool(
        valid
        and value.get("passed") is True
        and value.get("safe_to_deliver") is True
        and value.get("required_fixes") == []
    )
    status = "passed" if passed else ("failed" if valid else "invalid")
    return {"json_valid": valid, "verifier_status": status}


_RUNNERS = {
    "extraction_json": _run_extraction,
    "tool_call": _run_tool_call,
    "grounded_rewrite": _run_grounded_rewrite,
    "verifier": _run_verifier,
}


def run_benchmark(model, reasoning, client_factory=None, clock=None):
    """Run the fixed live benchmark and return privacy-safe metrics only."""
    model, reasoning = config.validate_model_reasoning(model, reasoning)
    if client_factory is None:
        client_factory = lambda: LLMClient(model=model, reasoning=reasoning)
    clock = clock or time.perf_counter
    settings = RunSettings(model=model, reasoning=reasoning)
    with use_run_settings(settings):
        client = client_factory()
        results = []
        context = {}
        for operation in OPERATIONS:
            started = clock()
            completed = False
            outcome = {}
            error_class = None
            # Bind metrics to this operation. A pre-call failure must not inherit
            # token usage from the previous successful operation.
            client.last_call_metrics = {}
            try:
                outcome = _RUNNERS[operation](client, context)
                completed = outcome.get("_completed", True)
            except Exception as error:  # Metrics must survive individual failures.
                error_class = type(error).__name__
            latency_ms = max(0, int((clock() - started) * 1000))
            results.append(_operation_result(
                operation=operation,
                model=model,
                reasoning=reasoning,
                latency_ms=latency_ms,
                completed=completed,
                json_valid=outcome.get("json_valid"),
                tool_call_success=outcome.get("tool_call_success"),
                verifier_status=outcome.get("verifier_status"),
                metrics=getattr(client, "last_call_metrics", None),
                error_class=error_class,
            ))
    verifier = next(
        item for item in results if item["operation"] == "verifier"
    )
    return {
        "schema": SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "fixture": "synthetic",
        "model": model,
        "reasoning": reasoning,
        "operations": results,
        "verifier_status": verifier["verifier_status"] or "not_completed",
    }


def _write_metrics(path, payload):
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    descriptor = os.open(
        str(target), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600
    )
    os.chmod(target, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        file.write(data)
    return target


def _parser():
    parser = argparse.ArgumentParser(
        description="Run an opt-in live benchmark with a synthetic fixture."
    )
    parser.add_argument("--live", action="store_true",
                        help="Explicitly allow live LLM requests.")
    parser.add_argument("--model", default=config.DEFAULT_MODEL)
    parser.add_argument("--reasoning", default="")
    parser.add_argument("--output", default="output/model_benchmark.json")
    return parser


def main(argv=None, client_factory=None, clock=None):
    args = _parser().parse_args(argv)
    try:
        model, reasoning = config.validate_model_reasoning(
            args.model, args.reasoning
        )
    except ValueError as error:
        print(f"错误：{error}")
        return 2
    if not args.live:
        print("拒绝运行：实时基准测试必须显式传入 --live。")
        return 2

    payload = run_benchmark(
        model, reasoning, client_factory=client_factory, clock=clock
    )
    target = _write_metrics(args.output, payload)
    print(f"基准测试指标已写入：{target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
