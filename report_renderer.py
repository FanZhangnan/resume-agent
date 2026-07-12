"""Pure Markdown rendering for complete and partial resume reports."""

from contracts import (
    delivery_is_complete,
    suggestions_are_usable,
    verification_is_deliverable,
)
from utils import render_resume_text


_KEY_LABELS = {
    "requirement_id": "要求ID", "requirement": "要求", "evidence": "证据",
    "reason": "理由", "gap": "差距", "improvement": "改进", "impact": "影响",
    "possible_action": "可行动作", "section": "段落", "problem": "问题",
    "suggestion": "建议", "before": "原文", "after": "改后", "original": "原文",
    "rewritten": "改写后", "situation": "情境(S)", "task": "任务(T)",
    "action": "行动(A)", "result": "结果(R)", "school": "学校", "degree": "学历",
    "major": "专业", "company": "公司", "title": "职位", "start_date": "开始",
    "end_date": "结束", "responsibilities": "职责", "achievements": "成果",
    "name": "名称", "role": "角色", "description": "描述", "technologies": "技术",
    "details": "详情", "keyword": "关键词", "placement": "位置", "round": "轮次",
    "issues": "问题", "resolved": "已解决", "question": "问题", "answer": "回答",
}


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _items(value):
    return value if isinstance(value, list) else []


def _format_item(item):
    if isinstance(item, dict):
        parts = []
        for key, value in item.items():
            if value in (None, "", [], {}):
                continue
            if isinstance(value, list):
                value = "；".join(str(entry) for entry in value)
            parts.append(f"{_KEY_LABELS.get(key, key)}：{value}")
        return " ｜ ".join(parts) if parts else "-"
    return str(item)


def _format_list(value, empty="（无）"):
    items = _items(value)
    if not items:
        return empty
    return "\n".join(f"- {_format_item(item)}" for item in items)


def _optimized_resume_text(suggestions):
    suggestions = _mapping(suggestions)
    return (
        render_resume_text(suggestions.get("optimized_resume_struct"))
        or str(suggestions.get("optimized_resume") or "")
    ).strip()


def _unresolved_items(verification, unresolved_fixes):
    if unresolved_fixes is not None:
        return list(unresolved_fixes) if isinstance(unresolved_fixes, list) else [unresolved_fixes]
    verification = _mapping(verification)
    fixes = verification.get("required_fixes")
    if isinstance(fixes, list) and fixes:
        return list(fixes)
    assessment = verification.get("overall_assessment")
    if assessment:
        return [assessment]
    return ["验证结果未满足严格交付契约"]


def _honest_assessment(match_result, verification, suggestions):
    match_result = _mapping(match_result)
    lines = []
    score = match_result.get("score")
    if score not in (None, ""):
        lines.append(f"综合匹配度 **{score}/100**。")
    high_matches = _items(match_result.get("high_matches"))
    if high_matches:
        lines.append(f"核心优势：有{len(high_matches)}项要求与你的经历高度匹配。")
    missing = _items(match_result.get("missing_requirements"))
    if missing:
        missing_names = "、".join(
            str(item.get("requirement", item)) if isinstance(item, dict) else str(item)
            for item in missing[:3]
        )
        lines.append(f"最大短板：{missing_names}。")
    risks = _items(match_result.get("risks"))
    if risks:
        lines.append("需要注意的风险：" + "；".join(str(item) for item in risks) + "。")
    if match_result.get("recommendation"):
        lines.append(f"行动建议：{match_result['recommendation']}")
    if not verification_is_deliverable(verification):
        lines.append(
            "⚠️ 注意：本报告的优化建议在自我验证中仍存在未完全解决的问题，"
            "使用前请逐条核对'诚实边界'和'必须修复项'。"
        )
    if not suggestions_are_usable(suggestions):
        lines.append(
            "⚠️ 注意：优化版简历未生成或结构无效，本次结果不可作为完整交付。"
        )
    if not lines:
        lines.append("分析数据不足，无法给出可靠评估，请补充简历或JD信息后重试。")
    return "\n".join(lines)


def _report_body(state, deliverable):
    resume_info = _mapping(state.get("resume_info"))
    recommendations = _mapping(state.get("job_recommendations"))
    match_result = _mapping(state.get("match_result"))
    suggestions = _mapping(state.get("suggestions"))
    verification = state.get("verification")
    verification_data = _mapping(verification)
    verification_deliverable = verification_is_deliverable(verification)
    correction_log = _items(state.get("correction_log"))
    user_clarifications = _items(state.get("user_clarifications"))

    sections = ["## 【简历解析】"]
    basic = _mapping(resume_info.get("basic_info"))
    basic_parts = [
        str(value) for value in (
            basic.get("name"), basic.get("phone"), basic.get("email"),
            basic.get("location"), basic.get("target_role"),
        ) if value
    ]
    if basic_parts:
        sections.append(f"**基本信息**：{' ｜ '.join(basic_parts)}")
    if resume_info.get("raw_summary"):
        sections.append(f"**概要**：{resume_info['raw_summary']}")
    sections.append("**教育背景**：\n" + _format_list(resume_info.get("education")))
    sections.append("**工作经历**：\n" + _format_list(resume_info.get("work_experience")))
    sections.append("**项目经验**：\n" + _format_list(resume_info.get("projects")))
    skills = _items(resume_info.get("skills"))
    sections.append("**技能**：" + ("、".join(str(item) for item in skills) if skills else "（无）"))
    sections.append("**潜在问题**：\n" + _format_list(resume_info.get("potential_issues")))
    if user_clarifications:
        sections.append("**用户补充信息**：\n" + _format_list(user_clarifications))

    candidates = _items(recommendations.get("candidates"))
    if candidates:
        sections.append("\n## 【岗位推荐】")
        if recommendations.get("overall_advice"):
            sections.append(f"**投递策略**：{recommendations['overall_advice']}")
        for index, raw_job in enumerate(candidates, start=1):
            job = _mapping(raw_job)
            marker = "⭐（本报告深入分析此岗位）" if index == 1 else ""
            sections.append(
                f"**{index}. {job.get('company', '?')} — {job.get('role_title', '?')}**"
                f"（{job.get('job_type', '')} ｜ {job.get('location', '')} ｜ "
                f"预估匹配度 {job.get('estimated_score', '?')}/100）{marker}"
            )
            if job.get("why_match"):
                sections.append(f"   - 匹配理由：{job['why_match']}")
            gaps = _items(job.get("gaps"))
            if gaps:
                sections.append(f"   - 主要差距：{'；'.join(str(item) for item in gaps)}")
        disclaimer = recommendations.get("disclaimer") or (
            "岗位画像基于各公司公开招聘要求整理，投递前请以官方最新JD为准。"
        )
        sections.append(f"> ⚠️ {disclaimer}")

    sections.extend([
        "\n## 【匹配度分析】",
        f"**匹配度评分**：{match_result.get('score', 'N/A')}/100",
    ])
    if match_result.get("score_reason"):
        sections.append(f"**评分依据**：{match_result['score_reason']}")
    if match_result.get("eligible") is False:
        failures = "、".join(str(item) for item in _items(match_result.get("gate_failures")))
        sections.append(f"**硬性门槛**：不符合（{failures or '必要条件未满足'}）")
    sections.append("**高度匹配**：\n" + _format_list(match_result.get("high_matches")))
    sections.append("**部分匹配**：\n" + _format_list(match_result.get("partial_matches")))
    sections.append("**缺失项**：\n" + _format_list(match_result.get("missing_requirements")))
    sections.append("**冗余项**：\n" + _format_list(match_result.get("redundant_or_irrelevant")))
    sections.append("**风险点**：\n" + _format_list(match_result.get("risks")))

    sections.append("\n## 【优化建议】")
    if suggestions.get("overall_strategy"):
        sections.append(f"**总体策略**：{suggestions['overall_strategy']}")
    rewrite_items = _items(suggestions.get("rewrite_suggestions"))
    if rewrite_items:
        sections.append("**逐段修改建议**：")
        for index, item in enumerate(rewrite_items, start=1):
            if not isinstance(item, dict):
                sections.append(f"{index}. {item}")
                continue
            sections.append(f"{index}. **{item.get('section', '段落')}** — {item.get('problem', '')}")
            if item.get("before"):
                sections.append(f"   - 原文：{item['before']}")
            if item.get("after"):
                sections.append(f"   - 改后：{item['after']}")
            if item.get("suggestion"):
                sections.append(f"   - 理由：{item['suggestion']}")
    for label, key in (
        ("STAR法则改写", "star_rewrites"),
        ("关键词补充", "keyword_injection"),
        ("诚实边界（以下内容不可夸大或需你自己确认属实）", "honesty_boundaries"),
    ):
        values = _items(suggestions.get(key))
        if values:
            sections.append(f"**{label}**：\n" + _format_list(values))

    sections.append("\n## 【自我验证】")
    sections.append(
        f"**验证结果**："
        f"{'✅ 通过' if verification_deliverable else '❌ 未通过'}"
    )
    if verification_deliverable and not deliverable:
        sections.append("**交付内容**：⚠️ 优化版简历未生成或结构无效")
    if verification_data.get("overall_assessment"):
        sections.append(f"**总体评价**：{verification_data['overall_assessment']}")
    for label, key in (
        ("过度美化问题", "overstatement_issues"),
        ("编造风险", "fabrication_risks"),
        ("逻辑问题", "logic_issues"),
        ("强行匹配问题", "match_authenticity_issues"),
        ("必须修复项", "required_fixes"),
    ):
        values = _items(verification_data.get(key))
        if values:
            sections.append(f"**{label}**：\n" + _format_list(values))
    if correction_log:
        sections.append("**修正日志**：")
        for entry in correction_log:
            entry = _mapping(entry)
            status = "已修正并通过复检" if entry.get("resolved") else "修正后复检仍未通过"
            sections.append(f"- 第{entry.get('round')}轮（{status}）：")
            for issue in _items(entry.get("issues")):
                sections.append(f"  - {_format_item(issue)}")
    elif verification_deliverable:
        sections.append("本次分析首轮即通过验证，未触发修正。")

    sections.extend([
        "\n## 【诚实评估】",
        _honest_assessment(match_result, verification, suggestions),
        "\n## 【优化版简历】",
        _optimized_resume_text(suggestions) or "（未生成）",
    ])
    return "\n\n".join(part for part in sections if part)


def render_report(state, terminal_status="completed", unresolved_fixes=None):
    """Render a report without LLM calls, I/O, clocks, or state mutation."""
    state = _mapping(state)
    verification = state.get("verification")
    verification_present = verification is not None
    suggestions = state.get("suggestions")
    suggestions_usable = suggestions_are_usable(suggestions)
    verification_deliverable = verification_is_deliverable(verification)
    deliverable = delivery_is_complete(verification, suggestions)
    partial = terminal_status != "completed" or not deliverable

    match_result = _mapping(state.get("match_result"))
    lines = ["# 简历优化报告", ""]
    if state.get("generation_time"):
        lines.append(f"- 生成时间：{state['generation_time']}")
    if state.get("analysis_engine"):
        lines.append(f"- 分析引擎：{state['analysis_engine']}")
    recommendations = _mapping(state.get("job_recommendations"))
    candidates = _items(recommendations.get("candidates"))
    if candidates:
        top = _mapping(candidates[0])
        lines.append(
            f"- 推荐岗位：{top.get('company', '?')}·{top.get('role_title', '?')}"
            f"（{top.get('job_type', '')}，共推荐{len(candidates)}个，本报告针对第一名深入分析）"
        )
    score = match_result.get("score")
    if score not in (None, ""):
        lines.append(f"- 匹配度评分：{score}/100")
    correction_log = _items(state.get("correction_log"))
    if correction_log:
        lines.append(f"- 自我修正：{len(correction_log)}轮")
    if verification_present:
        lines.append(
            "- 自我验证：✅ 通过" if verification_deliverable
            else "- 自我验证：⚠️ 未完全通过（剩余问题详见【自我验证】章节，采纳建议前请逐条核对）"
        )
    if not suggestions_usable:
        lines.append("- 交付内容：⚠️ 优化版简历未生成或结构无效")

    if partial:
        if terminal_status == "deadline":
            reason = "运行达到总时限；本报告仅包含超时前完成的阶段。"
        else:
            reason = str(state.get("interrupted_error") or "严格交付校验未通过")
        lines.extend([
            "",
            f"> ⚠️ **本报告不完整**：{reason}",
            "> 以下内容基于已完成的分析步骤生成，缺失的章节会显示为空。建议稍后重新运行。",
        ])
        if unresolved_fixes is not None:
            unresolved = _unresolved_items(verification, unresolved_fixes)
        elif verification_is_deliverable(verification):
            unresolved = []
        else:
            unresolved = _unresolved_items(verification, None)
        suggestion_issue = "优化版简历未生成或结构无效"
        if not suggestions_usable and suggestion_issue not in unresolved:
            unresolved.append(suggestion_issue)
        if unresolved:
            lines.extend(["", "> **未解决修复项**："])
            lines.extend(f"> - {_format_item(item)}" for item in unresolved)

    lines.extend(["", "---", "", _report_body(state, deliverable)])
    return "\n".join(lines)
