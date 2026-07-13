from contracts import VerificationResult
from tools.common import ask_json
from utils import compact_text, to_pretty_json

_VERIFY_SCHEMA = {
    "passed": False,
    "overall_assessment": "",
    "overstatement_issues": [],
    "fabrication_risks": [],
    "logic_issues": [],
    "match_authenticity_issues": [],
    "required_fixes": [],
    "safe_to_deliver": False,
}

_VERIFY_MAX_TOKENS = 2048


def _subset(value, keys):
    if not isinstance(value, dict):
        return {}
    return {
        key: value[key]
        for key in keys
        if key in value and value[key] not in (None, "", [], {})
    }


def _verification_context(resume_info, jd_analysis, match_result, suggestions):
    """Keep only facts needed to audit the deliverable, without score internals."""
    return {
        "original_facts": _subset(resume_info, (
            "basic_info", "education", "work_experience", "experience",
            "projects", "skills", "certifications", "languages",
            "user_clarifications", "potential_issues",
        )),
        "job_requirements": _subset(jd_analysis, (
            "job_title", "hard_requirements", "bonus_points",
            "responsibilities", "implicit_requirements", "keywords", "gates",
        )),
        "match_context": _subset(match_result, (
            "score", "score_reason", "eligible", "missing_requirements",
            "risks", "gate_failures",
        )),
        "deliverable": _subset(suggestions, (
            "optimized_resume_struct", "optimized_resume", "overall_strategy",
            "rewrite_suggestions", "star_rewrites", "keyword_injection",
            "honesty_boundaries",
        )),
    }


def verify_output(resume_info, jd_analysis, match_result, suggestions):
    system = "你是极其严格的简历优化审稿人。只输出JSON。你的任务是找问题，不是鼓励候选人。"
    context = _verification_context(
        resume_info, jd_analysis, match_result, suggestions,
    )
    original_facts = compact_text(
        to_pretty_json(context["original_facts"]), max_chars=6000,
    )
    job_requirements = compact_text(
        to_pretty_json(context["job_requirements"]), max_chars=3000,
    )
    match_context = compact_text(
        to_pretty_json(context["match_context"]), max_chars=3000,
    )
    deliverable = compact_text(
        to_pretty_json(context["deliverable"]), max_chars=8000,
    )
    prompt = f"""
请只审查待交付的优化建议和优化版简历是否忠于原始事实，输出紧凑JSON。不要复述输入，不要解释推理过程。

审查契约：
- optimized_resume_struct是主要交付格式；只要结构完整可用，optimized_resume为空是允许的，不得因此判失败。
- 匹配评分是只读背景，不属于本轮可修改内容；不得要求修改score、匹配分类或证据账本。
- 只把可通过修改优化建议或优化版简历解决的问题写入required_fixes。
- 原简历本身已有的信息缺口、时间空档或未提供的授权信息，只能作为风险提示；若优化稿没有新增虚构，不得列为必须修复。

字段必须包含：
passed: 布尔值，是否通过审查（存在任何必须修复的问题时为false）
overall_assessment: 120字以内总体评价
overstatement_issues: 最多4项，检查是否把参与写成主导、把协助写成负责、夸大成果
fabrication_risks: 最多4项，检查优化稿是否新增数据、项目、职责、技能或证书
logic_issues: 最多4项，检查优化稿自身的时间线和经历层级是否矛盾
match_authenticity_issues: 最多4项，检查优化稿是否为了匹配JD而强行关联
required_fixes: 最多6项字符串，每项120字以内，只写可直接修改优化稿的问题；不要输出priority/fix对象
safe_to_deliver: 布尔值，是否可以交付给用户

原始事实（只作真实性对照）：
{original_facts}

岗位关键要求：
{job_requirements}

只读匹配背景：
{match_context}

待交付的优化稿（必须审查）：
{deliverable}
"""
    result = ask_json(
        prompt,
        system,
        _VERIFY_SCHEMA,
        temperature=0.1,
        label="自我验证：审查过度美化与逻辑矛盾",
        max_tokens=_VERIFY_MAX_TOKENS,
        retry_max_tokens=_VERIFY_MAX_TOKENS,
        validator=VerificationResult,
    )
    if result is None:
        return {"success": False, "error": "LLM未能返回合法JSON，请重试verify_output"}
    return {"success": True, "verification": result}
