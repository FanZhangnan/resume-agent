"""Harness-controlled eight-stage resume analysis pipeline."""

import os
import threading
import time
import uuid
from concurrent.futures import (
    ThreadPoolExecutor,
    TimeoutError as FuturesTimeoutError,
    as_completed,
)

import config
from tools import execute_tool
from tools.common import use_run_deadline
from trace_catalog import emit_trace


STAGES = (
    (1, "parse_resume", "简历解析"),
    (2, "extract_resume", "简历信息结构化"),
    (3, "discover_jobs", "在招岗位发现"),
    (4, "analyze_jd", "岗位要求分析"),
    (5, "calculate_match", "匹配度分析"),
    (6, "generate_suggestions", "简历优化"),
    (7, "verify_output", "真实性审查"),
    (8, "render_report", "生成报告"),
)
_STAGE_BY_ID = {stage_id: (key, name) for stage_id, key, name in STAGES}


class PipelineStageError(RuntimeError):
    """A safe stage failure that never includes resume, JD, or model output."""

    def __init__(self, stage_id, stage_name, cause=None):
        self.stage_id = int(stage_id)
        self.stage_name = str(stage_name)
        self.error_class = type(cause).__name__ if cause is not None else "ToolFailure"
        super().__init__(
            f"Pipeline stage {self.stage_id} ({self.stage_name}) failed "
            f"with {self.error_class}"
        )


class PipelineRunDeadlineExceeded(TimeoutError):
    """The harness deadline expired while parallel pipeline work was pending."""

    is_run_deadline = True


def _shape(value):
    if isinstance(value, dict):
        return {"kind": "object", "field_count": len(value)}
    if isinstance(value, (list, tuple)):
        return {"kind": "array", "item_count": len(value)}
    if isinstance(value, str):
        return {"kind": "string", "chars": len(value)}
    return {"kind": type(value).__name__}


def _duration_ms(started):
    return max(0, int((time.monotonic() - started) * 1000))


class DeterministicPipeline:
    """Execute the stable task graph without an LLM planner call."""

    def __init__(self, agent):
        self.agent = agent
        self._main_thread_id = threading.get_ident()
        self.completed_stages = set()

    def run(self):
        """Run stages 1-7. Stage 8 is invoked separately for partial delivery."""
        self._parse_resume()
        if self.agent.job_search_mode:
            self._run_job_search_branch()
        else:
            self._run_supplied_jd_branch()
        self._run_control_question_if_needed()
        self._run_tool_stage(5, "calculate_match", {})
        self._run_tool_stage(6, "generate_suggestions", {})
        verification = self._run_tool_stage(7, "verify_output", {})
        self._revise_once_if_needed(verification)

    def render_report(self, allow_expired_deadline=False):
        """Run local-only stage 8 and save through the owning ResumeAgent."""
        span, started = self._start_stage(
            8, allow_expired_deadline=allow_expired_deadline
        )
        try:
            report = self.agent._generate_final_report(force_local=True)
            if not allow_expired_deadline:
                self.agent._check_run_deadline()
            output_path = self.agent._save_report(report)
            if output_path:
                print(f"\n💾 报告已保存：{output_path}")
            self._complete_stage(
                8,
                span,
                started,
                data={"report_available": bool(output_path)},
            )
            return report, output_path
        except Exception as error:
            self._fail_stage(8, span, started, error)
            raise

    def _parse_resume(self):
        span, started = self._start_stage(1)
        if not self.agent.resume_is_file:
            self._complete_stage(1, span, started, data={"source": "text"})
            return
        arguments = self.agent._prepare_arguments(
            "parse_resume_file", {"file_path": self.agent.resume_input}
        )
        outcome = self._call_tool("parse_resume_file", arguments, span, 1)
        self._consume_tool_outcome(1, span, started, "parse_resume_file", outcome)

    def _run_supplied_jd_branch(self):
        extract_span, extract_started = self._start_stage(2)
        self._skip_stage(3, "jd_supplied")
        jd_span, jd_started = self._start_stage(4)
        extract_args = self.agent._prepare_arguments("extract_resume_info", {})
        jd_args = self.agent._prepare_arguments("analyze_jd", {"jd_text": self.agent.jd_text})

        pool = ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="resume-pipeline",
        )
        futures = {
            pool.submit(
                self._call_tool,
                "extract_resume_info",
                extract_args,
                extract_span,
                2,
            ): (2, extract_span, extract_started, "extract_resume_info"),
            pool.submit(
                self._call_tool,
                "analyze_jd",
                jd_args,
                jd_span,
                4,
            ): (4, jd_span, jd_started, "analyze_jd"),
        }
        failures = []
        abandon_workers = False
        try:
            deadline = getattr(self.agent, "_run_deadline", None)
            timeout = None if deadline is None else max(0, deadline - time.monotonic())
            for future in as_completed(futures, timeout=timeout):
                stage_id, span, started, tool_name = futures[future]
                outcome = future.result()
                try:
                    self._consume_tool_outcome(
                        stage_id, span, started, tool_name, outcome
                    )
                except Exception as error:
                    if getattr(error, "is_run_deadline", False):
                        abandon_workers = True
                        raise
                    failures.append(error)
        except FuturesTimeoutError as error:
            abandon_workers = True
            raise PipelineRunDeadlineExceeded(
                "Parallel pipeline work exceeded the Agent run deadline"
            ) from error
        except BaseException:
            abandon_workers = True
            raise
        finally:
            pending = [future for future in futures if not future.done()]
            if pending:
                abandon_workers = True
                for future in pending:
                    future.cancel()
            pool.shutdown(
                wait=not abandon_workers,
                cancel_futures=abandon_workers,
            )
        if failures:
            raise failures[0]

    def _run_job_search_branch(self):
        self._run_tool_stage(2, "extract_resume_info", {})
        self._run_tool_stage(3, "recommend_jobs", {})
        self._run_selected_job_analysis()

    def _run_selected_job_analysis(self):
        span, started = self._start_stage(4)
        try:
            selected_jd = self._selected_job_description()
        except Exception as error:
            self._fail_stage(4, span, started, error)
            raise PipelineStageError(4, _STAGE_BY_ID[4][1], error)
        prepared = self.agent._prepare_arguments(
            "analyze_jd", {"jd_text": selected_jd}
        )
        outcome = self._call_tool("analyze_jd", prepared, span, 4)
        return self._consume_tool_outcome(
            4, span, started, "analyze_jd", outcome
        )

    def _selected_job_description(self):
        recommendations = self.agent.state.get("job_recommendations") or {}
        candidates = recommendations.get("candidates") or []
        if not candidates or not isinstance(candidates[0], dict):
            raise ValueError("recommended job is missing")
        selected = str(
            candidates[0].get("description")
            or candidates[0].get("typical_jd")
            or ""
        ).strip()
        if not selected:
            raise ValueError("recommended job description is missing")
        return selected

    def _revise_once_if_needed(self, verification_result):
        verification = verification_result.get("verification") or {}
        revision_limit = min(1, max(0, int(config.MAX_REVISION_ROUNDS)))
        note = self.agent._handle_verification(
            verification, max_revision_rounds=revision_limit
        )
        if not note or not self.agent.pending_revision:
            return
        fixes = verification.get("required_fixes") or []
        if not fixes and self.agent.correction_log:
            fixes = self.agent.correction_log[-1].get("issues") or []
        self._run_tool_stage(
            6,
            "generate_suggestions",
            {"fix_instructions": fixes},
            revision_round=1,
        )
        revised = self._run_tool_stage(
            7,
            "verify_output",
            {},
            revision_round=1,
        )
        self.agent._handle_verification(
            revised.get("verification") or {},
            max_revision_rounds=revision_limit,
        )

    def _run_control_question_if_needed(self):
        if self.agent.client.mock_mode:
            if os.environ.get("AGENT_MOCK_ASK") != "1":
                return
            question = (
                "你在核心项目中负责的具体环节是什么？是否有可以核实的量化结果？"
            )
        else:
            resume_info = self.agent.state.get("resume_info") or {}
            issues = resume_info.get("potential_issues") or []
            issue = next(
                (str(item).strip() for item in issues if str(item).strip()),
                "",
            )
            if not issue:
                return
            question = (
                f"简历中存在待确认信息：{issue[:240]}。"
                "请补充你能核实的具体事实；没有或不方便提供时可以直接跳过。"
            )
        span = f"control-{uuid.uuid4().hex[:12]}"
        started = time.monotonic()
        emit_trace(
            "control.started",
            span=span,
            parent="run",
            data={"control": "user_question"},
        )
        arguments = {
            "question": question,
            "context": (
                "补充真实信息可以提高改写准确度；没有或不方便提供时可以直接跳过。"
            ),
        }
        outcome = self._call_tool("ask_user", arguments, span, None)
        self._emit_tool_completed(outcome)
        success = self._outcome_succeeded(outcome)
        if success:
            self._apply_result("ask_user", outcome["result"])
        else:
            print("⚠️ 用户补充信息未取得，继续使用现有事实分析")
        emit_trace(
            "control.completed",
            level="info" if success else "warning",
            span=span,
            parent="run",
            data={
                "control": "user_question",
                "success": success,
                "duration_ms": _duration_ms(started),
            },
        )

    def _run_tool_stage(self, stage_id, tool_name, arguments, revision_round=None):
        span, started = self._start_stage(stage_id, revision_round=revision_round)
        prepared = self.agent._prepare_arguments(tool_name, arguments)
        outcome = self._call_tool(tool_name, prepared, span, stage_id)
        return self._consume_tool_outcome(
            stage_id,
            span,
            started,
            tool_name,
            outcome,
            revision_round=revision_round,
        )

    def _call_tool(self, tool_name, arguments, parent_span, stage_id):
        tool_span = f"tool-{uuid.uuid4().hex[:12]}"
        started = time.monotonic()
        emit_trace(
            "tool.started",
            span=tool_span,
            parent=parent_span,
            step=stage_id,
            data={"name": tool_name, "arguments": _shape(arguments)},
        )
        print(f"🔧 调用工具：{tool_name}")
        try:
            with use_run_deadline(getattr(self.agent, "_run_deadline", None)):
                result = execute_tool(tool_name, arguments)
        except Exception as error:
            return {
                "name": tool_name,
                "span": tool_span,
                "parent": parent_span,
                "step": stage_id,
                "duration_ms": _duration_ms(started),
                "result": None,
                "error": error,
            }
        return {
            "name": tool_name,
            "span": tool_span,
            "parent": parent_span,
            "step": stage_id,
            "duration_ms": _duration_ms(started),
            "result": result,
            "error": None,
        }

    @staticmethod
    def _emit_tool_completed(outcome):
        result = outcome["result"]
        error = outcome["error"]
        success = (
            error is None
            and isinstance(result, dict)
            and bool(result.get("success"))
        )
        data = {
            "name": outcome["name"],
            "success": success,
            "duration_ms": outcome["duration_ms"],
        }
        if error is not None:
            data["error_class"] = type(error).__name__
        else:
            data["result"] = _shape(result)
        emit_trace(
            "tool.completed",
            level="info" if success else "error" if error else "warning",
            span=outcome["span"],
            parent=outcome["parent"],
            step=outcome["step"],
            data=data,
        )

    def _consume_tool_outcome(self, stage_id, span, started, tool_name, outcome,
                              revision_round=None):
        self._emit_tool_completed(outcome)
        result = outcome["result"]
        error = outcome["error"]
        if error is not None or not isinstance(result, dict) or not result.get("success"):
            failure = error or RuntimeError("tool returned unsuccessful result")
            self._fail_stage(
                stage_id,
                span,
                started,
                failure,
                revision_round=revision_round,
            )
            if getattr(failure, "is_run_deadline", False):
                raise failure
            raise PipelineStageError(stage_id, _STAGE_BY_ID[stage_id][1], failure)
        try:
            self._apply_result(tool_name, result)
        except Exception as error:
            self._fail_stage(
                stage_id,
                span,
                started,
                error,
                revision_round=revision_round,
            )
            raise PipelineStageError(stage_id, _STAGE_BY_ID[stage_id][1], error)
        self._complete_stage(
            stage_id,
            span,
            started,
            revision_round=revision_round,
        )
        return result

    def _apply_result(self, tool_name, result):
        if threading.get_ident() != self._main_thread_id:
            raise RuntimeError("Pipeline state updates must run on the main thread")
        self.agent._check_run_deadline()
        self.agent._update_state(tool_name, result)
        print(f"📋 观察：{self.agent._summarize_result(tool_name, result)}")

    @staticmethod
    def _outcome_succeeded(outcome):
        result = outcome["result"]
        error = outcome["error"]
        return error is None and isinstance(result, dict) and bool(result.get("success"))

    def _start_stage(self, stage_id, revision_round=None,
                     allow_expired_deadline=False):
        key, name = _STAGE_BY_ID[stage_id]
        if not allow_expired_deadline:
            self.agent._check_run_deadline()
        self.agent.step_count = stage_id
        span = f"stage-{stage_id}-{uuid.uuid4().hex[:10]}"
        started = time.monotonic()
        data = {
            "stage_id": stage_id,
            "stage": key,
            "name": name,
            "total_stages": len(STAGES),
        }
        if revision_round is not None:
            data["revision_round"] = revision_round
        emit_trace(
            "stage.started",
            span=span,
            parent="run",
            step=stage_id,
            data=data,
        )
        print(f"\n--- 步骤 {stage_id}/8 ---")
        print(f"⏳ 阶段：{name}")
        return span, started

    def _skip_stage(self, stage_id, reason):
        span, started = self._start_stage(stage_id)
        key, name = _STAGE_BY_ID[stage_id]
        emit_trace(
            "stage.skipped",
            span=span,
            parent="run",
            step=stage_id,
            data={
                "stage_id": stage_id,
                "stage": key,
                "name": name,
                "reason": reason,
                "duration_ms": _duration_ms(started),
            },
        )
        print(f"⏹️  跳过阶段：{name}")

    def _complete_stage(self, stage_id, span, started, data=None,
                        revision_round=None):
        key, name = _STAGE_BY_ID[stage_id]
        payload = {
            "stage_id": stage_id,
            "stage": key,
            "name": name,
            "status": "completed",
            "duration_ms": _duration_ms(started),
        }
        if revision_round is not None:
            payload["revision_round"] = revision_round
        payload.update(data or {})
        emit_trace(
            "stage.completed",
            span=span,
            parent="run",
            step=stage_id,
            data=payload,
        )
        self.completed_stages.add(stage_id)

    def _fail_stage(self, stage_id, span, started, error,
                    revision_round=None):
        key, name = _STAGE_BY_ID[stage_id]
        payload = {
            "stage_id": stage_id,
            "stage": key,
            "name": name,
            "status": "failed",
            "duration_ms": _duration_ms(started),
            "error_class": type(error).__name__,
        }
        if revision_round is not None:
            payload["revision_round"] = revision_round
        emit_trace(
            "stage.failed",
            level="error",
            span=span,
            parent="run",
            step=stage_id,
            data=payload,
        )
        print(f"❌ 阶段失败：{name}（{type(error).__name__}）")
