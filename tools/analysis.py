import config
from tools.common import ask_json
from utils import compact_text, to_pretty_json

_MATCH_SCHEMA = {
    "score": 0,
    "score_reason": "",
    "high_matches": [],
    "partial_matches": [],
    "missing_requirements": [],
    "redundant_or_irrelevant": [],
    "risks": [],
    "recommendation": "",
}


_SUGGESTION_SCHEMA = {
    "overall_strategy": "",
    "rewrite_suggestions": [],
    "star_rewrites": [],
    "keyword_injection": [],
    "honesty_boundaries": [],
    "optimized_resume": "",
}


def calculate_match(resume_info, jd_analysis):
    system = "你是严谨的招聘匹配度评估专家。只输出JSON。必须诚实评估，不允许为了提高匹配度而强行关联。"
    prompt = f"""
请逐项对比候选人简历结构化信息与JD分析，输出紧凑JSON，每个数组最多6项，字段必须包含：
score: 0到100的整数匹配度
score_reason: 100字以内评分依据
high_matches: 数组，高度匹配项，每项包含 requirement, evidence, reason
partial_matches: 数组，部分匹配项，每项包含 requirement, evidence, gap, improvement
missing_requirements: 数组，缺失项，每项包含 requirement, impact, possible_action
redundant_or_irrelevant: 数组，简历中与目标岗位弱相关或冗余的内容
risks: 数组，风险点，如年限不足、领域不匹配、证据薄弱
recommendation: 100字以内建议

简历信息：
{compact_text(to_pretty_json(resume_info))}

JD分析：
{compact_text(to_pretty_json(jd_analysis))}
"""
    result = ask_json(prompt, system, _MATCH_SCHEMA, temperature=0.2, label="计算简历与JD的匹配度")
    if result is None:
        return {"success": False, "error": "LLM未能返回合法JSON，请重试calculate_match"}
    return {"success": True, "match_result": result}


def generate_suggestions(resume_info, jd_analysis, match_result, fix_instructions=None):
    system = "你是资深简历优化顾问。只输出JSON。必须遵守诚实边界：不能虚构公司、职位、数据、项目职责或成果；不得把'参与/协助'升级为'主导/负责'。"

    fix_block = ""
    if fix_instructions:
        fix_block = f"""
【重要】上一轮自我验证发现以下必须修复的问题，本轮必须全部修正，且不得引入新的夸大或编造：
{compact_text(to_pretty_json(fix_instructions), max_chars=2000)}
"""

    prompt = f"""
请基于简历、JD和匹配分析生成优化建议，输出紧凑JSON，数组最多8项，字段必须包含：
overall_strategy: 150字以内总体优化策略
rewrite_suggestions: 数组，逐段建议，每项包含 section, problem, suggestion, before, after
star_rewrites: 数组，用STAR法则改写经历，每项包含 original, situation, task, action, result, rewritten
keyword_injection: 数组，可自然补充的关键词及放置位置，每项包含 keyword, placement
honesty_boundaries: 数组，明确哪些内容不能夸大或编造
optimized_resume: 完整的优化版中文简历文本，必须覆盖原简历的全部经历段落，可直接使用，不得省略
{fix_block}
简历信息：
{compact_text(to_pretty_json(resume_info))}

JD分析：
{compact_text(to_pretty_json(jd_analysis))}

匹配分析：
{compact_text(to_pretty_json(match_result))}
"""
    label = "根据验证意见重新生成优化建议" if fix_instructions else "生成优化建议与优化版简历"
    # 该调用要输出完整优化版简历全文+全部建议，是最重的输出载荷，直接用大token预算
    result = ask_json(prompt, system, _SUGGESTION_SCHEMA, temperature=0.2, label=label,
                      max_tokens=config.REPORT_MAX_TOKENS)
    if result is None:
        return {"success": False, "error": "LLM未能返回合法JSON，请重试generate_suggestions"}
    return {"success": True, "suggestions": result}
