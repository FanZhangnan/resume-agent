"""
简历优化Agent核心：实现ReAct推理循环
- 自主规划分析步骤
- 调用工具链（解析/提取/分析/匹配/建议/验证/追问）
- 观察结果并继续推理
- 自我验证未通过时自动修正并记录修正日志
- 输出结构化最终报告（Markdown）
"""
import json
import os
from datetime import datetime

import config
from llm_client import LLMClient
from prompts import AGENT_SYSTEM_PROMPT, FINAL_REPORT_FORMAT_PROMPT
from tools import execute_tool, get_tool_definitions
from utils import clip_text, compact_text, to_pretty_json


REQUIRED_SECTIONS = [
    "【简历解析】",
    "【匹配度分析】",
    "【优化建议】",
    "【自我验证】",
    "【诚实评估】",
    "【优化版简历】",
]

# 各工具允许的参数白名单：过滤模型幻觉出的多余参数，避免TypeError
_ALLOWED_PARAMS = {
    "parse_resume_file": {"file_path"},
    "extract_resume_info": {"resume_text"},
    "analyze_jd": {"jd_text"},
    "calculate_match": {"resume_info", "jd_analysis"},
    "generate_suggestions": {"resume_info", "jd_analysis", "match_result", "fix_instructions"},
    "verify_output": {"resume_info", "jd_analysis", "match_result", "suggestions"},
    "recommend_jobs": {"resume_info", "preferences"},
    "ask_user": {"question", "context"},
}

# 报告本地渲染时把英文字段名翻译成中文标签
_KEY_LABELS = {
    "requirement": "要求", "evidence": "证据", "reason": "理由", "gap": "差距",
    "improvement": "改进", "impact": "影响", "possible_action": "可行动作",
    "section": "段落", "problem": "问题", "suggestion": "建议", "before": "原文",
    "after": "改后", "original": "原文", "rewritten": "改写后",
    "situation": "情境(S)", "task": "任务(T)", "action": "行动(A)", "result": "结果(R)",
    "school": "学校", "degree": "学历", "major": "专业", "company": "公司",
    "title": "职位", "start_date": "开始", "end_date": "结束",
    "responsibilities": "职责", "achievements": "成果", "name": "名称",
    "role": "角色", "description": "描述", "technologies": "技术", "details": "详情",
    "keyword": "关键词", "placement": "位置", "round": "轮次", "issues": "问题",
    "resolved": "已解决", "question": "问题", "answer": "回答",
}


class ResumeAgent:
    """简历优化Agent：通过ReAct循环自主完成简历分析、匹配评估与优化建议"""

    def __init__(self, resume_input, jd_text=None, resume_is_file=False, output_dir=None,
                 preferences=None):
        """
        参数:
            resume_input: 简历文本或文件路径
            jd_text: 职位JD文本。不提供时进入"岗位推荐模式"：自动推荐匹配度最高的大厂岗位再分析
            resume_is_file: resume_input是否为文件路径
            output_dir: 报告输出目录，默认使用config.OUTPUT_DIR
            preferences: 岗位推荐模式下用户的求职偏好（如"在中国求职大厂实习"、"只考虑远程"）
        """
        self.resume_input = resume_input
        self.jd_text = str(jd_text or "")
        # 岗位推荐模式：用户只给了简历没给JD → 先推荐大厂岗位，再针对第一名深入分析
        self.job_search_mode = not self.jd_text.strip()
        self.preferences = str(preferences or "").strip()
        self.resume_is_file = resume_is_file
        self.output_dir = output_dir or config.OUTPUT_DIR
        self.client = LLMClient()
        self.tool_definitions = get_tool_definitions()
        self.messages = []
        self.state = {
            "resume_text": None if resume_is_file else str(resume_input or ""),
            "resume_info": None,
            "job_recommendations": None,
            "jd_analysis": None,
            "match_result": None,
            "suggestions": None,
            "verification": None,
        }
        self.step_count = 0
        self.max_steps = config.MAX_STEPS
        # 自我修正状态：验证未通过时自动触发修正轮，并记录日志
        self.revision_rounds = 0
        self.pending_revision = False
        self.correction_log = []
        self.user_clarifications = []
        # 模型返回纯文本但分析未完成时的纠偏次数（防止提前退出产出空报告）
        self.nudge_count = 0
        # 运行中途网络故障时记录错误：已完成的分析仍会输出为部分报告，不白跑
        self.interrupted_error = None
        self._init_messages()

    def _init_messages(self):
        """初始化对话历史：系统提示 + 用户任务
        简历和JD分开截断并保留换行，避免长简历把JD挤掉、避免排版信息丢失
        """
        self.messages = [{"role": "system", "content": AGENT_SYSTEM_PROMPT}]
        if self.resume_is_file:
            resume_desc = f"简历文件路径：{self.resume_input}"
        else:
            resume_desc = f"简历文本：\n{clip_text(self.resume_input, max_chars=8000)}"
        if self.job_search_mode:
            prefs_line = f"\n用户求职偏好：{self.preferences}（调用recommend_jobs时必须通过preferences参数传入并严格遵循）\n" if self.preferences else ""
            user_content = (
                "用户只提供了简历，未提供目标JD。请进入岗位推荐模式：\n"
                "1. 解析并结构化简历\n"
                "2. 调用recommend_jobs推荐与候选人当前情况最匹配的大厂岗位（实习或工作）\n"
                "3. 针对排名第一的岗位完成完整的匹配分析、优化建议和自我验证\n"
                f"{prefs_line}\n"
                f"{resume_desc}"
            )
        else:
            user_content = (
                f"请分析以下简历与目标岗位的匹配度，并生成优化建议。\n\n"
                f"{resume_desc}\n\n"
                f"职位JD：\n{clip_text(self.jd_text, max_chars=4000)}"
            )
        self.messages.append({"role": "user", "content": user_content})

    def run(self):
        """主循环：运行ReAct推理循环，返回最终报告"""
        print("=" * 60)
        print("🚀 简历优化Agent启动")
        if self.client.mock_mode:
            print("🧪 模式：离线演示（Mock）")
        else:
            print(f"🤖 模型：{config.MODEL_NAME} @ {config.API_BASE_URL}")
        print(f"📄 简历来源：{'文件（' + str(self.resume_input) + '）' if self.resume_is_file else '文本'}")
        if self.job_search_mode:
            print("🎯 任务：未提供JD → 岗位推荐模式（自动匹配最合适的大厂岗位）")
            if self.preferences:
                print(f"💡 求职偏好：{self.preferences}")
        else:
            print(f"🎯 JD长度：{len(self.jd_text)}字符")
        print(f"🔧 可用工具：{len(self.tool_definitions)}个")
        print("=" * 60)

        try:
            self._loop()
        except Exception as error:
            # 已有部分分析成果时不白跑：输出部分报告；一无所获才向上抛出
            if any(self.state[key] for key in ("resume_info", "jd_analysis", "match_result")):
                self.interrupted_error = str(error)
                print(f"\n⚠️ 分析中途出错：{error}")
                print("⚠️ 将基于已完成的分析步骤生成部分报告（缺失章节会标注）")
            else:
                print(f"❌ Agent运行异常：{error}")
                raise

        final_report = self._generate_final_report()
        output_path = self._save_report(final_report)
        if output_path:
            print(f"\n💾 报告已保存：{output_path}")
        return final_report

    def _loop(self):
        """ReAct核心循环：思考→行动→观察（→验证未通过时自动修正）"""
        while self.step_count < self.max_steps:
            self.step_count += 1
            print(f"\n--- 步骤 {self.step_count}/{self.max_steps} ---")

            # 调用LLM，传入当前对话历史和工具定义
            response = self.client.chat(
                messages=self.messages,
                tools=self.tool_definitions,
                temperature=0.3,
            )

            # 提取思考内容和工具调用请求
            thinking = response.content or ""
            tool_calls = response.tool_calls or []

            if thinking:
                preview = thinking if len(thinking) <= 600 else thinking[:600] + "..."
                print(f"🧠 思考：{preview}")

            if tool_calls:
                # 执行工具调用，并把结果加入对话历史
                self._execute_tool_calls(thinking, tool_calls)

                # 检查是否已经收集到足够信息，可以结束循环
                if self._is_complete():
                    verification = self.state["verification"] or {}
                    if verification.get("passed") or verification.get("safe_to_deliver"):
                        print("\n✅ 关键分析步骤已完成，自我验证通过")
                    else:
                        print("\n⚠️ 已达最大修正轮数，自我验证仍未完全通过——剩余问题会在报告中如实标注")
                    break
            else:
                # 没有工具调用：可能是Agent输出结论，也可能是模型跑偏输出了纯文本规划
                if thinking:
                    self.messages.append({"role": "assistant", "content": thinking})
                if self._is_complete():
                    print("\n⏹️  分析已完成，Agent输出总结，结束推理循环")
                    break
                # 分析尚未完成却没有工具调用 → 纠偏：提醒模型继续调用工具（最多2次）
                if self.nudge_count < 2:
                    self.nudge_count += 1
                    print("⚠️ Agent返回了纯文本但分析尚未完成，提醒其继续调用工具...")
                    self.messages.append({"role": "user", "content": self._build_nudge()})
                    continue
                print("\n⏹️  Agent多次未调用工具，结束推理循环（报告可能不完整）")
                break

        if self.step_count >= self.max_steps:
            print(f"\n⚠️ 已达到最大步数限制（{self.max_steps}步）")

    def _build_nudge(self):
        """构造纠偏消息：明确告诉模型缺什么、下一步该调用哪个工具"""
        labels = {
            "resume_info": "简历结构化信息（extract_resume_info）",
            "jd_analysis": "JD分析（analyze_jd）",
            "match_result": "匹配度分析（calculate_match）",
            "suggestions": "优化建议（generate_suggestions）",
            "verification": "自我验证（verify_output）",
        }
        if self.job_search_mode:
            labels = {"job_recommendations": "大厂岗位推荐（recommend_jobs）", **labels}
        missing = [labels[key] for key in labels if self.state[key] is None]
        if self.pending_revision:
            return (
                "【系统提示】自我验证未通过的修正流程尚未完成。"
                "请勿用纯文本回复：立即调用generate_suggestions（传入fix_instructions）生成修正版，"
                "然后调用verify_output复检。")
        return (
            f"【系统提示】分析尚未完成，还缺少：{'、'.join(missing)}。"
            "请勿用纯文本回复，立即调用下一个工具继续分析。")

    def _execute_tool_calls(self, thinking, tool_calls):
        """执行一组工具调用，并更新状态和对话历史"""
        # OpenAI协议要求：必须先有一条包含tool_calls的assistant消息，再跟tool消息
        serializable_tool_calls = []
        for tool_call in tool_calls:
            serializable_tool_calls.append({
                "id": tool_call.id,
                "type": "function",
                "function": {
                    "name": tool_call.function.name,
                    "arguments": tool_call.function.arguments,
                },
            })

        assistant_message = {
            "role": "assistant",
            "content": thinking or None,
            "tool_calls": serializable_tool_calls,
        }
        self.messages.append(assistant_message)

        # 验证未通过时的修正指令，必须等所有tool消息都追加完再插入，避免破坏消息协议
        deferred_notes = []

        for tool_call in tool_calls:
            tool_id = tool_call.id
            tool_name = tool_call.function.name
            arguments = self._prepare_arguments(tool_name, tool_call.function.arguments)

            print(f"🔧 调用工具：{tool_name}")
            result = execute_tool(tool_name, arguments)

            # 将工具结果更新到Agent状态
            self._update_state(tool_name, result)

            # 自我验证未通过 → 触发自动修正
            if tool_name == "verify_output" and result.get("success"):
                note = self._handle_verification(result.get("verification") or {})
                if note:
                    deferred_notes.append(note)

            # 把工具结果加入对话历史
            self.messages.append({
                "role": "tool",
                "tool_call_id": tool_id,
                "name": tool_name,
                "content": self._result_for_history(tool_name, result),
            })

            print(f"📋 观察：{self._summarize_result(tool_name, result)}")

        for note in deferred_notes:
            self.messages.append({"role": "user", "content": note})

    def _prepare_arguments(self, tool_name, raw_arguments):
        """解析并修正工具参数：
        1. 过滤模型幻觉出的未知参数
        2. 用Agent状态中的完整数据替换模型转述的大参数（模型转述会截断劣化）
        """
        arguments = raw_arguments
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments or "{}")
            except json.JSONDecodeError:
                return raw_arguments  # 保留原始字符串，让execute_tool报出参数错误
        if not isinstance(arguments, dict):
            return arguments

        allowed = _ALLOWED_PARAMS.get(tool_name)
        if allowed is not None:
            arguments = {key: value for key, value in arguments.items() if key in allowed}

        state = self.state
        if tool_name == "extract_resume_info":
            if state["resume_text"]:
                arguments["resume_text"] = state["resume_text"]
            arguments.setdefault("resume_text", "")
        elif tool_name == "analyze_jd":
            if self.jd_text.strip():
                # 用户提供了JD → 始终用原文，避免模型转述劣化
                arguments["jd_text"] = self.jd_text
            elif not str(arguments.get("jd_text") or "").strip():
                # 岗位推荐模式且模型没自己给JD → 注入排名第一的推荐岗位典型JD
                arguments["jd_text"] = self._top_recommended_jd()
            arguments.setdefault("jd_text", "")
        elif tool_name == "recommend_jobs":
            if state["resume_info"] is not None:
                arguments["resume_info"] = state["resume_info"]
            arguments.setdefault("resume_info", {})
            # 用户明确给了求职偏好 → 兜底注入，确保推荐一定遵循（模型忘传也不丢）
            if self.preferences and not str(arguments.get("preferences") or "").strip():
                arguments["preferences"] = self.preferences
        elif tool_name == "calculate_match":
            for key in ("resume_info", "jd_analysis"):
                if state[key] is not None:
                    arguments[key] = state[key]
                arguments.setdefault(key, {})
        elif tool_name == "generate_suggestions":
            for key in ("resume_info", "jd_analysis", "match_result"):
                if state[key] is not None:
                    arguments[key] = state[key]
                arguments.setdefault(key, {})
        elif tool_name == "verify_output":
            for key in ("resume_info", "jd_analysis", "match_result", "suggestions"):
                if state[key] is not None:
                    arguments[key] = state[key]
                arguments.setdefault(key, {})
        return arguments

    def _top_recommended_jd(self):
        """岗位推荐模式：取排名第一的推荐岗位，拼出可供analyze_jd分析的JD文本"""
        recommendations = self.state["job_recommendations"] or {}
        candidates = recommendations.get("candidates") or []
        if not candidates:
            return ""
        top = candidates[0]
        return (
            f"公司：{top.get('company', '')}\n"
            f"岗位：{top.get('role_title', '')}（{top.get('job_type', '')}）\n"
            f"地点：{top.get('location', '')}\n\n"
            f"{top.get('typical_jd', '')}"
        )

    def _result_for_history(self, tool_name, result):
        """构造进入对话历史的工具结果文本
        简历全文不进历史（避免模型下一步转述劣化），只给预览；结构化结果给紧凑JSON
        """
        if tool_name == "parse_resume_file" and result.get("success"):
            summary = {
                "success": True,
                "file_type": result.get("file_type"),
                "char_count": result.get("char_count"),
                "preview": clip_text(result.get("text", ""), max_chars=500),
                "note": "简历全文已存入系统状态，后续工具会自动使用全文，无需你转述。",
            }
            return to_pretty_json(summary)
        return compact_text(to_pretty_json(result), max_chars=4000)

    def _summarize_result(self, tool_name, result):
        """生成一行可读的观察摘要，让推理链在终端里可观察"""
        if not result.get("success"):
            return f"❌ 失败：{result.get('error', '未知错误')}"
        if tool_name == "parse_resume_file":
            return f"✅ 解析成功（{result.get('file_type')}，{result.get('char_count')}字符）"
        if tool_name == "extract_resume_info":
            info = result.get("resume_info") or {}
            name = (info.get("basic_info") or {}).get("name") or "候选人"
            return (f"✅ 已提取「{name}」：工作经历{len(info.get('work_experience') or [])}段、"
                    f"项目{len(info.get('projects') or [])}个、技能{len(info.get('skills') or [])}项，"
                    f"潜在问题{len(info.get('potential_issues') or [])}个")
        if tool_name == "analyze_jd":
            jd = result.get("jd_analysis") or {}
            return (f"✅ 岗位「{jd.get('job_title') or '未知'}」：硬性要求{len(jd.get('hard_requirements') or [])}项、"
                    f"加分项{len(jd.get('bonus_points') or [])}项、隐含要求{len(jd.get('implicit_requirements') or [])}项")
        if tool_name == "calculate_match":
            match = result.get("match_result") or {}
            return (f"✅ 匹配度 {match.get('score', '?')}/100：高度匹配{len(match.get('high_matches') or [])}项、"
                    f"部分匹配{len(match.get('partial_matches') or [])}项、缺失{len(match.get('missing_requirements') or [])}项")
        if tool_name == "generate_suggestions":
            suggestions = result.get("suggestions") or {}
            return (f"✅ 生成建议{len(suggestions.get('rewrite_suggestions') or [])}条、"
                    f"STAR改写{len(suggestions.get('star_rewrites') or [])}条，"
                    f"优化版简历{len(suggestions.get('optimized_resume') or '')}字")
        if tool_name == "verify_output":
            verification = result.get("verification") or {}
            if verification.get("passed") or verification.get("safe_to_deliver"):
                return "✅ 自我验证通过，可以交付"
            issue_count = sum(len(verification.get(key) or []) for key in (
                "overstatement_issues", "fabrication_risks", "logic_issues", "match_authenticity_issues"))
            return f"⚠️ 自我验证未通过：发现{issue_count}个问题，{len(verification.get('required_fixes') or [])}项必须修复"
        if tool_name == "recommend_jobs":
            recommendations = result.get("recommendations") or {}
            candidates = recommendations.get("candidates") or []
            if candidates:
                top = candidates[0]
                return (f"✅ 推荐了{len(candidates)}个大厂岗位，第一名："
                        f"{top.get('company', '?')}·{top.get('role_title', '?')}"
                        f"（预估匹配度{top.get('estimated_score', '?')}/100）")
            return "✅ 完成岗位推荐"
        if tool_name == "ask_user":
            return f"✅ 用户回答：{str(result.get('answer', ''))[:80]}"
        return "✅ 完成"

    def _update_state(self, tool_name, result):
        """根据工具名称和结果更新Agent内部状态"""
        if not result.get("success"):
            return

        if tool_name == "parse_resume_file":
            self.state["resume_text"] = result.get("text", "")
        elif tool_name == "extract_resume_info":
            self.state["resume_info"] = result.get("resume_info")
        elif tool_name == "recommend_jobs":
            self.state["job_recommendations"] = result.get("recommendations")
        elif tool_name == "analyze_jd":
            self.state["jd_analysis"] = result.get("jd_analysis")
        elif tool_name == "calculate_match":
            self.state["match_result"] = result.get("match_result")
        elif tool_name == "generate_suggestions":
            self.state["suggestions"] = result.get("suggestions")
        elif tool_name == "verify_output":
            self.state["verification"] = result.get("verification")
        elif tool_name == "ask_user":
            # 记录用户澄清，写入最终报告供参考
            self.user_clarifications.append({
                "question": result.get("question", ""),
                "answer": result.get("answer", ""),
            })

    def _handle_verification(self, verification):
        """自我验证结果处理：未通过且还有修正额度时，触发自动修正轮
        返回需要插入对话的修正指令文本（None表示无需修正）
        """
        if self.pending_revision:
            # 本次verify是修正后的复检
            self.pending_revision = False

        passed = bool(verification.get("passed") or verification.get("safe_to_deliver"))
        if passed:
            if self.correction_log and not self.correction_log[-1].get("resolved"):
                self.correction_log[-1]["resolved"] = True
                print("   ✅ 修正后复检通过")
            return None

        if self.revision_rounds >= config.MAX_REVISION_ROUNDS:
            print("   ⚠️ 自我验证仍未通过且已达最大修正轮数，将在报告中如实标注")
            return None

        self.revision_rounds += 1
        self.pending_revision = True
        fixes = verification.get("required_fixes") or []
        if not fixes:
            fixes = [verification.get("overall_assessment") or "验证未通过，需要整体复查优化建议"]
        self.correction_log.append({
            "round": self.revision_rounds,
            "issues": fixes,
            "action": "已自动要求重新生成优化建议并复检",
            "resolved": False,
        })
        print(f"   🔁 自我验证未通过，启动第{self.revision_rounds}轮自动修正...")
        issues_text = compact_text(to_pretty_json(fixes), max_chars=2000)
        return (
            f"【系统提示】自我验证未通过（第{self.revision_rounds}轮修正）。必须修复的问题：\n{issues_text}\n"
            "请立即调用generate_suggestions，把上述问题作为fix_instructions传入以生成修正版建议，"
            "然后再次调用verify_output复检。"
        )

    def _is_complete(self):
        """判断是否已经完成核心分析链路且通过自我验证（或用尽修正轮数）"""
        required = ["resume_info", "jd_analysis", "match_result", "suggestions", "verification"]
        if self.job_search_mode:
            required.insert(1, "job_recommendations")
        for key in required:
            if self.state[key] is None:
                return False
        if self.pending_revision:
            return False
        verification = self.state["verification"]
        if verification.get("passed") or verification.get("safe_to_deliver"):
            return True
        return self.revision_rounds >= config.MAX_REVISION_ROUNDS

    # ==================== 最终报告 ====================

    def _required_sections(self):
        """最终报告必须包含的章节：岗位推荐模式下额外要求【岗位推荐】"""
        sections = list(REQUIRED_SECTIONS)
        if self.state["job_recommendations"]:
            sections.insert(1, "【岗位推荐】")
        return sections

    def _generate_final_report(self):
        """生成最终报告：优先使用LLM格式化，失败或章节缺失则使用本地Markdown模板"""
        if self.client.mock_mode:
            return self._compose_report(self._format_report_locally())
        if self.interrupted_error:
            # 网络已经不稳定，不再发起LLM调用，直接用本地模板渲染已有数据
            return self._compose_report(self._format_report_locally())

        data = self._get_report_data()
        prompt = FINAL_REPORT_FORMAT_PROMPT.format(
            data=compact_text(to_pretty_json(data), max_chars=16000))
        try:
            print("\n📝 正在生成最终报告...")
            formatted = self.client.simple_ask(
                prompt=prompt, temperature=0.3, max_tokens=config.REPORT_MAX_TOKENS)
            if formatted and all(section in formatted for section in self._required_sections()):
                return self._compose_report(self._ensure_full_resume(formatted))
            print("⚠️ LLM生成的报告缺少必要章节，改用本地模板")
        except Exception as error:
            print(f"⚠️ LLM格式化报告失败，改用本地模板：{error}")
        return self._compose_report(self._format_report_locally())

    def _ensure_full_resume(self, formatted):
        """LLM格式化时偶尔会把优化版简历概括掉——检测到疑似缺失时补上工具生成的原文"""
        optimized = ((self.state["suggestions"] or {}).get("optimized_resume") or "").strip()
        if not optimized:
            return formatted
        tail = formatted.split("【优化版简历】")[-1].strip()
        if len(tail) < min(200, max(50, len(optimized) // 2)):
            formatted += "\n\n（以下为工具生成的完整优化版简历原文）\n\n" + optimized
        return formatted

    def _compose_report(self, body):
        """给报告加上元信息头"""
        match_result = self.state["match_result"] or {}
        lines = [
            "# 简历优化报告",
            "",
            f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
            f"- 分析引擎：{'离线演示模式' if self.client.mock_mode else config.MODEL_NAME}",
        ]
        recommendations = self.state["job_recommendations"] or {}
        candidates = recommendations.get("candidates") or []
        if candidates:
            top = candidates[0]
            lines.append(
                f"- 推荐岗位：{top.get('company', '?')}·{top.get('role_title', '?')}"
                f"（{top.get('job_type', '')}，共推荐{len(candidates)}个，本报告针对第一名深入分析）")
        score = match_result.get("score")
        if score not in (None, ""):
            lines.append(f"- 匹配度评分：{score}/100")
        if self.correction_log:
            lines.append(f"- 自我修正：{len(self.correction_log)}轮")
        verification = self.state["verification"]
        if verification is not None:
            verify_passed = verification.get("passed") or verification.get("safe_to_deliver")
            lines.append(
                "- 自我验证：✅ 通过" if verify_passed
                else "- 自我验证：⚠️ 未完全通过（剩余问题详见【自我验证】章节，采纳建议前请逐条核对）")
        if self.interrupted_error:
            lines.extend([
                "",
                f"> ⚠️ **本报告不完整**：分析在中途因网络故障中断（{self.interrupted_error}）。",
                "> 以下内容基于已完成的分析步骤生成，缺失的章节会显示为空。建议稍后重新运行。",
            ])
        lines.extend(["", "---", "", body])
        return "\n".join(lines)

    def _get_report_data(self):
        """获取当前已收集的所有分析数据"""
        return {
            "resume_info": self.state["resume_info"] or {},
            "job_recommendations": self.state["job_recommendations"] or {},
            "jd_analysis": self.state["jd_analysis"] or {},
            "match_result": self.state["match_result"] or {},
            "suggestions": self.state["suggestions"] or {},
            "verification": self.state["verification"] or {},
            "correction_log": self.correction_log,
            "user_clarifications": self.user_clarifications,
        }

    def _format_report_locally(self):
        """本地Markdown模板渲染报告（作为LLM格式化的可靠兜底，人类可读）"""
        data = self._get_report_data()
        resume_info = data["resume_info"]
        jd_analysis = data["jd_analysis"]
        match_result = data["match_result"]
        suggestions = data["suggestions"]
        verification = data["verification"]

        sections = []

        # ---- 简历解析 ----
        sections.append("## 【简历解析】")
        basic = resume_info.get("basic_info") or {}
        basic_parts = [str(value) for value in (
            basic.get("name"), basic.get("phone"), basic.get("email"),
            basic.get("location"), basic.get("target_role")) if value]
        if basic_parts:
            sections.append(f"**基本信息**：{' ｜ '.join(basic_parts)}")
        if resume_info.get("raw_summary"):
            sections.append(f"**概要**：{resume_info['raw_summary']}")
        sections.append("**教育背景**：\n" + _format_list(resume_info.get("education")))
        sections.append("**工作经历**：\n" + _format_list(resume_info.get("work_experience")))
        sections.append("**项目经验**：\n" + _format_list(resume_info.get("projects")))
        skills = resume_info.get("skills") or []
        sections.append("**技能**：" + ("、".join(str(s) for s in skills) if skills else "（无）"))
        sections.append("**潜在问题**：\n" + _format_list(resume_info.get("potential_issues")))
        if data["user_clarifications"]:
            sections.append("**用户补充信息**：\n" + _format_list(data["user_clarifications"]))

        # ---- 岗位推荐（仅岗位推荐模式）----
        recommendations = data["job_recommendations"]
        candidates = recommendations.get("candidates") or []
        if candidates:
            sections.append("\n## 【岗位推荐】")
            if recommendations.get("overall_advice"):
                sections.append(f"**投递策略**：{recommendations['overall_advice']}")
            for index, job in enumerate(candidates, start=1):
                marker = "⭐（本报告深入分析此岗位）" if index == 1 else ""
                sections.append(
                    f"**{index}. {job.get('company', '?')} — {job.get('role_title', '?')}**"
                    f"（{job.get('job_type', '')} ｜ {job.get('location', '')} ｜ "
                    f"预估匹配度 {job.get('estimated_score', '?')}/100）{marker}")
                if job.get("why_match"):
                    sections.append(f"   - 匹配理由：{job['why_match']}")
                gaps = job.get("gaps") or []
                if gaps:
                    sections.append(f"   - 主要差距：{'；'.join(str(g) for g in gaps)}")
            disclaimer = recommendations.get("disclaimer") or "岗位画像基于各公司公开招聘要求整理，投递前请以官方最新JD为准。"
            sections.append(f"> ⚠️ {disclaimer}")

        # ---- 匹配度分析 ----
        sections.append("\n## 【匹配度分析】")
        sections.append(f"**匹配度评分**：{match_result.get('score', 'N/A')}/100")
        if match_result.get("score_reason"):
            sections.append(f"**评分依据**：{match_result['score_reason']}")
        sections.append("**高度匹配**：\n" + _format_list(match_result.get("high_matches")))
        sections.append("**部分匹配**：\n" + _format_list(match_result.get("partial_matches")))
        sections.append("**缺失项**：\n" + _format_list(match_result.get("missing_requirements")))
        sections.append("**冗余项**：\n" + _format_list(match_result.get("redundant_or_irrelevant")))
        sections.append("**风险点**：\n" + _format_list(match_result.get("risks")))

        # ---- 优化建议 ----
        sections.append("\n## 【优化建议】")
        if suggestions.get("overall_strategy"):
            sections.append(f"**总体策略**：{suggestions['overall_strategy']}")
        rewrite_items = suggestions.get("rewrite_suggestions") or []
        if rewrite_items:
            sections.append("**逐段修改建议**：")
            for index, item in enumerate(rewrite_items, start=1):
                if isinstance(item, dict):
                    sections.append(f"{index}. **{item.get('section', '段落')}** — {item.get('problem', '')}")
                    if item.get("before"):
                        sections.append(f"   - 原文：{item['before']}")
                    if item.get("after"):
                        sections.append(f"   - 改后：{item['after']}")
                    if item.get("suggestion"):
                        sections.append(f"   - 理由：{item['suggestion']}")
                else:
                    sections.append(f"{index}. {item}")
        star_items = suggestions.get("star_rewrites") or []
        if star_items:
            sections.append("**STAR法则改写**：\n" + _format_list(star_items))
        keyword_items = suggestions.get("keyword_injection") or []
        if keyword_items:
            sections.append("**关键词补充**：\n" + _format_list(keyword_items))
        honesty_items = suggestions.get("honesty_boundaries") or []
        if honesty_items:
            sections.append("**诚实边界（以下内容不可夸大或需你自己确认属实）**：\n" + _format_list(honesty_items))

        # ---- 自我验证 ----
        sections.append("\n## 【自我验证】")
        verify_passed = verification.get("passed") or verification.get("safe_to_deliver")
        sections.append(f"**验证结果**：{'✅ 通过' if verify_passed else '❌ 未通过'}")
        if verification.get("overall_assessment"):
            sections.append(f"**总体评价**：{verification['overall_assessment']}")
        for label, key in (
            ("过度美化问题", "overstatement_issues"),
            ("编造风险", "fabrication_risks"),
            ("逻辑问题", "logic_issues"),
            ("强行匹配问题", "match_authenticity_issues"),
            ("必须修复项", "required_fixes"),
        ):
            items = verification.get(key) or []
            if items:
                sections.append(f"**{label}**：\n" + _format_list(items))
        if data["correction_log"]:
            sections.append("**修正日志**：")
            for entry in data["correction_log"]:
                status = "已修正并通过复检" if entry.get("resolved") else "修正后复检仍未通过"
                sections.append(f"- 第{entry.get('round')}轮（{status}）：")
                for issue in entry.get("issues") or []:
                    sections.append(f"  - {_format_item(issue)}")
        else:
            sections.append("本次分析首轮即通过验证，未触发修正。" if verify_passed else "")

        # ---- 诚实评估 ----
        sections.append("\n## 【诚实评估】")
        sections.append(self._compose_honest_assessment())

        # ---- 优化版简历 ----
        sections.append("\n## 【优化版简历】")
        sections.append(suggestions.get("optimized_resume") or "（未生成）")

        return "\n\n".join(part for part in sections if part)

    def _compose_honest_assessment(self):
        """基于匹配与验证结果组装诚实评估段落"""
        match_result = self.state["match_result"] or {}
        verification = self.state["verification"] or {}
        lines = []

        score = match_result.get("score")
        if score not in (None, ""):
            lines.append(f"综合匹配度 **{score}/100**。")
        high_count = len(match_result.get("high_matches") or [])
        missing = match_result.get("missing_requirements") or []
        if high_count:
            lines.append(f"核心优势：有{high_count}项要求与你的经历高度匹配。")
        if missing:
            missing_names = "、".join(
                str(item.get("requirement", item)) if isinstance(item, dict) else str(item)
                for item in missing[:3])
            lines.append(f"最大短板：{missing_names}。")
        risks = match_result.get("risks") or []
        if risks:
            lines.append("需要注意的风险：" + "；".join(str(r) for r in risks) + "。")
        if match_result.get("recommendation"):
            lines.append(f"行动建议：{match_result['recommendation']}")

        if not (verification.get("passed") or verification.get("safe_to_deliver")):
            lines.append(
                "⚠️ 注意：本报告的优化建议在自我验证中仍存在未完全解决的问题，"
                "使用前请逐条核对'诚实边界'和'必须修复项'。")
        if not lines:
            lines.append("分析数据不足，无法给出可靠评估，请补充简历或JD信息后重试。")
        return "\n".join(lines)

    def _save_report(self, report_text):
        """将最终报告保存为Markdown文件"""
        if not report_text:
            return None
        try:
            os.makedirs(self.output_dir, exist_ok=True)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"resume_report_{timestamp}.md"
            filepath = os.path.join(self.output_dir, filename)
            with open(filepath, "w", encoding="utf-8") as file:
                file.write(report_text)
            return filepath
        except Exception as error:
            print(f"⚠️ 保存报告失败：{error}")
            return None


def _format_item(item):
    """把dict/str统一渲染成一行可读文本"""
    if isinstance(item, dict):
        parts = []
        for key, value in item.items():
            if value in (None, "", [], {}):
                continue
            if isinstance(value, list):
                value = "；".join(str(v) for v in value)
            parts.append(f"{_KEY_LABELS.get(key, key)}：{value}")
        return " ｜ ".join(parts) if parts else "-"
    return str(item)


def _format_list(items, empty="（无）"):
    """把列表渲染成Markdown无序列表"""
    items = items or []
    if not items:
        return empty
    return "\n".join(f"- {_format_item(item)}" for item in items)


# ==================== 命令行入口 ====================

_USAGE = """用法：
  python agent.py                      交互式运行（推荐，按提示给简历和JD即可）
  python agent.py --demo               演示模式：使用samples/中的示例简历和JD
  python agent.py <简历文件>            岗位推荐模式：只给简历，自动推荐最匹配的大厂岗位并深入分析
  python agent.py <简历文件> <JD文件或JD文本>
  python agent.py --text "简历文本" ["JD文本"]

岗位推荐模式可加 --prefer 指定求职偏好（地点/方向/公司类型等）：
  python agent.py resume.pdf --prefer "在中国求职大厂实习"

示例：
  python agent.py resume.pdf                        （不知道投什么？自动推荐大厂岗位）
  python agent.py resume.pdf --prefer "只考虑远程"
  python agent.py resume.pdf jd.txt
  python agent.py resume.docx "负责电商平台活动运营..."
  AGENT_MOCK=1 python agent.py --demo   （无API密钥时离线体验完整流程）
"""


def _read_multiline(title):
    """读取多行粘贴输入：单独一行输入END结束"""
    print(f"\n{title}")
    print("（直接粘贴内容，可以多行；粘贴完成后另起一行输入 END 并回车）")
    lines = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip() == "END":
            break
        lines.append(line)
    return "\n".join(lines).strip()


def _read_text_or_file(value):
    """参数既可能是文件路径也可能是文本：是存在的文件就解析，否则当文本"""
    if value and os.path.isfile(os.path.expanduser(value)):
        from tools.file_parser import parse_resume_file
        parsed = parse_resume_file(value)
        if parsed.get("success"):
            return parsed.get("text", "")
        print(f"⚠️ 文件解析失败（{parsed.get('error')}），将按纯文本处理")
    return value


def _interactive_inputs():
    """交互式收集简历和JD"""
    print("=" * 60)
    print("📄 简历优化Agent — 交互模式")
    print("=" * 60)
    resume_path = input("\n请输入简历文件路径（支持PDF/Word/txt），或直接回车改为粘贴文本：").strip()
    if resume_path:
        expanded = os.path.expanduser(resume_path)
        if not os.path.isfile(expanded):
            print(f"❌ 找不到文件：{expanded}")
            return None
        resume_input, resume_is_file = resume_path, True
    else:
        resume_input = _read_multiline("请粘贴简历文本：")
        resume_is_file = False
        if not resume_input:
            print("❌ 简历内容为空，无法分析")
            return None
    jd_text = _read_multiline("请粘贴目标岗位JD（没有目标岗位可直接输入END跳过，Agent会自动推荐最匹配的大厂岗位）：")
    if not jd_text:
        print("💡 未提供JD → 进入岗位推荐模式：将根据简历自动推荐最匹配的大厂岗位")
    return resume_input, jd_text, resume_is_file


def _demo_inputs():
    """演示模式：使用samples/目录下的示例简历和JD"""
    samples_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "samples")
    resume_path = os.path.join(samples_dir, "sample_resume.txt")
    jd_path = os.path.join(samples_dir, "sample_jd.txt")
    if not (os.path.isfile(resume_path) and os.path.isfile(jd_path)):
        print("❌ 未找到samples目录中的示例文件")
        return None
    print("🎬 演示模式：使用samples/中的示例简历和JD")
    with open(jd_path, "r", encoding="utf-8") as file:
        jd_text = file.read().strip()
    return resume_path, jd_text, True


def main():
    """命令行入口：交互式 / 演示模式 / 命令行参数三种方式"""
    import sys

    args = sys.argv[1:]

    if args and args[0] in ("-h", "--help"):
        print(_USAGE)
        return

    # 提取 --prefer 求职偏好参数（仅岗位推荐模式生效）
    preferences = None
    if "--prefer" in args:
        index = args.index("--prefer")
        if index + 1 >= len(args):
            print("❌ --prefer 后面需要跟偏好描述，例如：--prefer \"在中国求职大厂实习\"\n")
            print(_USAGE)
            return
        preferences = args[index + 1]
        args = args[:index] + args[index + 2:]

    if not args:
        inputs = _interactive_inputs()
    elif args[0] == "--demo":
        inputs = _demo_inputs()
    elif args[0] == "--text":
        if len(args) < 2:
            print("❌ --text 模式至少需要提供简历文本\n")
            print(_USAGE)
            return
        jd_text = _read_text_or_file(args[2]) if len(args) >= 3 else ""
        if not jd_text:
            print("💡 未提供JD → 进入岗位推荐模式：将根据简历自动推荐最匹配的大厂岗位")
        inputs = (args[1], jd_text, False)
    else:
        resume_path = os.path.expanduser(args[0])
        if not os.path.isfile(resume_path):
            print(f"❌ 找不到简历文件：{resume_path}\n")
            print(_USAGE)
            return
        jd_text = _read_text_or_file(args[1]) if len(args) >= 2 else ""
        if not jd_text:
            print("💡 未提供JD → 进入岗位推荐模式：将根据简历自动推荐最匹配的大厂岗位")
        inputs = (args[0], jd_text, True)

    if not inputs:
        return

    resume_input, jd_text, resume_is_file = inputs
    agent = ResumeAgent(
        resume_input=resume_input,
        jd_text=jd_text,
        resume_is_file=resume_is_file,
        preferences=preferences,
    )
    report = agent.run()
    print("\n" + "=" * 60)
    print(report)


if __name__ == "__main__":
    main()
