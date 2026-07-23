"""Offline source contract for trace-driven Web pipeline rendering."""

import re
import subprocess
import tempfile
from pathlib import Path


HTML_PATH = Path(__file__).parent / "webui" / "static" / "index.html"


def _extract_function(source, name):
    start = source.index(f"function {name}(")
    brace = source.index("{", start)
    depth = 0
    for index in range(brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise AssertionError(f"unterminated JavaScript function: {name}")


def test_frontend_consumes_trace_events_and_maps_all_eight_stages():
    source = HTML_PATH.read_text(encoding="utf-8")
    assert 'ev.type === "trace"' in source
    assert "handleTraceEvent(ev.event)" in source
    assert "function handleTraceEvent(event)" in source
    assert "function selectStep(" in source

    expected = {
        "parse_resume": "parse",
        "extract_resume": "extract",
        "discover_jobs": "recommend",
        "analyze_jd": "jd",
        "calculate_match": "match",
        "generate_suggestions": "suggest",
        "verify_output": "verify",
        "render_report": "report",
    }
    for stage, ui_key in expected.items():
        assert re.search(
            rf"\b{re.escape(stage)}\s*:\s*\"{re.escape(ui_key)}\"",
            source,
        ), f"missing trace stage mapping: {stage}"

    for event_name in (
        "stage.started", "stage.completed", "stage.failed", "stage.skipped",
        "tool.started", "tool.completed", "run.interrupted", "run.completed",
    ):
        assert f'"{event_name}"' in source

    assert 'if(key === "recommend" && !jobSearch) return;' not in source
    assert "onToolCall(name, stepId, true)" in source
    assert "function stageFailed(" in source


def test_trace_handler_routes_parallel_observations_by_event_step():
    source = HTML_PATH.read_text(encoding="utf-8")
    handler = _extract_function(source, "handleTraceEvent")
    harness = """
const TRACE_STAGE_KEY = {};
let stepMeta = {}, stageFirst = {}, lastTool = null, toolByStep = {};
let plainToolAwaitTrace = {}, traceToolEcho = {}, selectedStep = 0;
const routed = [];
function selectStep(step){ selectedStep = Number(step); routed.push(selectedStep); return {}; }
function finishTraceStep(){}
function stageDone(){}
function stageFailed(){}
function stageSkipped(){}
function stageActive(){}
function onToolCall(){}
function setStatus(){}
function logAdd(){}
let traceRunPartial = false;
""" + handler + """
const observations = [];
handleTraceEvent({event:"tool.completed", step:2, data:{name:"extract_resume_info", success:true}});
observations.push([selectedStep, lastTool]);
handleTraceEvent({event:"tool.completed", step:4, data:{name:"analyze_jd", success:true}});
observations.push([selectedStep, lastTool]);
if(JSON.stringify(observations) !== JSON.stringify([
  [2,"extract_resume_info"], [4,"analyze_jd"]
])) process.exit(9);
"""
    result = subprocess.run(
        ["node", "-e", harness],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_trace_failure_and_partial_completion_use_non_success_states():
    source = HTML_PATH.read_text(encoding="utf-8")
    handler = _extract_function(source, "handleTraceEvent")
    finish_run = _extract_function(source, "finishRun")
    harness = """
const TRACE_STAGE_KEY = {parse_resume:"parse"};
let stepMeta = {}, stageFirst = {}, lastTool = null, toolByStep = {};
let plainToolAwaitTrace = {}, traceToolEcho = {}, traceRunPartial = false;
let failed = [], done = [], statuses = [], logs = [];
let running = true, timerIv = null, es = null, reportText = "partial report";
const elements = {};
function element(){ return {style:{}, classList:{remove(){},add(){}}, appendChild(){}, textContent:"", disabled:false}; }
function $(id){ return elements[id] || (elements[id] = element()); }
const document = {createElement(){ return element(); }};
function selectStep(){}
function finishTraceStep(){}
function stageDone(key){ done.push(key); }
function stageFailed(key){ failed.push(key); }
function stageSkipped(){}
function stageActive(){}
function onToolCall(){}
function setStatus(cls, text){ statuses.push(`${cls}:${text}`); }
function logAdd(text, cls){ logs.push(`${cls || ""}:${text}`); }
function closeStep(){}
function nowHMS(){ return "00:00:00"; }
function swarmSettle(){}
function swarmCore(){}
function swarmStatus(){}
""" + handler + "\n" + finish_run + """
handleTraceEvent({event:"stage.failed", step:1, data:{stage:"parse_resume", stage_id:1}});
if(JSON.stringify(failed) !== JSON.stringify(["parse"])) process.exit(10);
if(done.length !== 0) process.exit(11);
handleTraceEvent({event:"run.interrupted", data:{partial_results:true}});
handleTraceEvent({event:"run.completed", data:{status:"partial", report_available:true}});
finishRun(0);
if(!traceRunPartial) process.exit(12);
if(statuses[statuses.length - 1] !== "warn:部分完成") process.exit(13);
"""
    result = subprocess.run(
        ["node", "-e", harness],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_react_tool_trace_preserves_existing_step_total():
    source = HTML_PATH.read_text(encoding="utf-8")
    select_step = _extract_function(source, "selectStep")
    harness = """
let sawStep = false, stepCount = 0, stepEl = null, lastStepNo = 0, lastBlk = null;
const elements = {"cnt-steps":{textContent:""}, "step-txt":{textContent:""}};
function $(id){ return elements[id]; }
const card = {};
let stepMeta = {9:{el:card, total:20, closed:false, metaEl:null, tlEl:null}};
function newStep(){ process.exit(20); }
""" + select_step + """
selectStep(9);
if(elements["step-txt"].textContent !== "STEP 9/20") process.exit(21);
"""
    result = subprocess.run(
        ["node", "-e", harness],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_inline_javascript_is_syntax_valid():
    source = HTML_PATH.read_text(encoding="utf-8")
    scripts = re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", source, re.DOTALL)
    inline = "\n".join(script for script in scripts if script.strip())
    assert inline.strip()
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8") as file:
        file.write(inline)
        file.flush()
        result = subprocess.run(
            ["node", "--check", file.name],
            capture_output=True,
            text=True,
            check=False,
        )
    assert result.returncode == 0, result.stderr


def main():
    test_frontend_consumes_trace_events_and_maps_all_eight_stages()
    test_trace_handler_routes_parallel_observations_by_event_step()
    test_trace_failure_and_partial_completion_use_non_success_states()
    test_react_tool_trace_preserves_existing_step_total()
    test_inline_javascript_is_syntax_valid()
    print("Web trace UI source contract passed.")


if __name__ == "__main__":
    main()
