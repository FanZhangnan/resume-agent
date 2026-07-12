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


def verify_output(resume_info, jd_analysis, match_result, suggestions):
    system = "你是极其严格的简历优化审稿人。只输出JSON。你的任务是找问题，不是鼓励候选人。"
    prompt = f"""
请用批判性视角审查以下产出，输出JSON，字段必须包含：
passed: 布尔值，是否通过审查（存在任何必须修复的问题时为false）
overall_assessment: 总体评价
overstatement_issues: 数组，检查是否把参与写成主导、把协助写成负责、夸大成果
fabrication_risks: 数组，检查是否编造数据、项目、职责、技能或证书
logic_issues: 数组，检查时间线、经历层级、技能与经历是否矛盾
match_authenticity_issues: 数组，检查是否为了匹配JD而强行关联
required_fixes: 数组，必须修改的问题和建议修法
safe_to_deliver: 布尔值，是否可以交付给用户

原始简历信息：
{compact_text(to_pretty_json(resume_info))}

JD分析：
{compact_text(to_pretty_json(jd_analysis))}

匹配分析：
{compact_text(to_pretty_json(match_result))}

优化建议和优化版简历：
{compact_text(to_pretty_json(suggestions))}
"""
    result = ask_json(
        prompt,
        system,
        _VERIFY_SCHEMA,
        temperature=0.1,
        label="自我验证：审查过度美化与逻辑矛盾",
        validator=VerificationResult,
    )
    if result is None:
        return {"success": False, "error": "LLM未能返回合法JSON，请重试verify_output"}
    return {"success": True, "verification": result}
