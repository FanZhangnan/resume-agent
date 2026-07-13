import config
from contracts import (
    MatchResult,
    SuggestionResult,
    optimized_resume_struct_is_usable,
)
from tools.common import ask_json, get_client
from tools.scoring import (
    gates_from_jd,
    normalize_jd_requirements,
    normalize_resume_evidence,
    requirement_ledger_from_match_result,
    score_requirements,
)
from utils import clip_text, compact_text, parse_resume_text_to_struct, to_pretty_json

_MATCH_SCHEMA = {
    "score": 0,
    "score_reason": "",
    "high_matches": [],
    "partial_matches": [],
    "missing_requirements": [],
    "redundant_or_irrelevant": [],
    "risks": [],
    "recommendation": "",
    "requirement_evidence": [],
    "eligible": True,
    "requirement_scores": [],
    "gate_failures": [],
}


_SUGGESTION_SCHEMA = {
    "overall_strategy": "",
    "rewrite_suggestions": [],
    "star_rewrites": [],
    "keyword_injection": [],
    "honesty_boundaries": [],
    "optimized_resume": "",
    "optimized_resume_struct": {},
}


def _summary_rows_from_ledger(result, requirements, ledger):
    """Build user-visible buckets from the same conservative ledger as scoring."""
    requirements_by_id = {
        item["requirement_id"]: item["requirement"]
        for item in requirements
    }
    bucket_specs = {
        "met": ("high_matches", ("reason",)),
        "under_evidenced": (
            "partial_matches", ("gap", "improvement")
        ),
        "missing": (
            "missing_requirements", ("impact", "possible_action")
        ),
    }
    original_by_bucket = {}
    for _, (bucket, _) in bucket_specs.items():
        rows = result.get(bucket)
        original_by_bucket[bucket] = {
            str(row.get("requirement_id") or "").strip(): row
            for row in rows if isinstance(row, dict)
        } if isinstance(rows, list) else {}

    rebuilt = {bucket: [] for bucket, _ in bucket_specs.values()}
    for ledger_row in ledger:
        status = ledger_row["status"]
        bucket, safe_fields = bucket_specs[status]
        requirement_id = ledger_row["requirement_id"]
        summary = {
            "requirement_id": requirement_id,
            "requirement": requirements_by_id.get(requirement_id, ""),
        }
        if status != "missing":
            summary["evidence_ids"] = list(ledger_row.get("evidence_ids") or [])
        original = original_by_bucket[bucket].get(requirement_id, {})
        for field in safe_fields:
            if original.get(field) not in (None, "", [], {}):
                summary[field] = original[field]
        rebuilt[bucket].append(summary)
    return rebuilt


def calculate_match(resume_info, jd_analysis, preferences=None):
    system = "你是严谨的招聘匹配度评估专家。只输出JSON。必须诚实评估，不允许为了提高匹配度而强行关联。"
    requirements = normalize_jd_requirements(jd_analysis)
    evidence_catalog = normalize_resume_evidence(resume_info, preferences=preferences)
    prompt_evidence_catalog = [
        {key: value for key, value in item.items() if key != "search_text"}
        for item in evidence_catalog
    ]
    prompt = f"""
请根据下方标准化要求与证据目录逐项评估，只输出紧凑JSON：
score: 0到100的整数解释草稿
score_reason: 100字以内评分依据
requirement_evidence: 为每个requirement_id输出且仅输出一行，每行只含requirement_id, status, evidence_ids
redundant_or_irrelevant: 最多6项弱相关或冗余内容
risks: 最多6项年限、领域或证据风险
recommendation: 100字以内建议

status只能是met、under_evidenced、missing。met/under_evidenced必须引用证据目录中的真实evidence_id，missing的evidence_ids必须为空数组。requirement_id必须从要求清单原样选取，不得编造要求或证据。系统会在本地重建匹配分类并重算最终分数。

标准化要求清单：
{compact_text(to_pretty_json(requirements))}

简历证据目录：
{compact_text(to_pretty_json(prompt_evidence_catalog))}
"""
    result = ask_json(
        prompt,
        system,
        _MATCH_SCHEMA,
        temperature=0.2,
        label="计算简历与JD的匹配度",
        validator=MatchResult,
    )
    if result is None:
        return {"success": False, "error": "LLM未能返回合法JSON，请重试calculate_match"}
    result = dict(result)
    ledger = requirement_ledger_from_match_result(
        result,
        requirements,
        evidence_catalog=evidence_catalog,
    )
    scoring = score_requirements(
        requirements,
        ledger,
        gates_from_jd(jd_analysis, resume_info=resume_info),
    )
    result["score"] = scoring["score"]
    result["eligible"] = scoring["eligible"]
    result["requirement_evidence"] = ledger
    result["requirement_scores"] = scoring["requirements"]
    result["gate_failures"] = scoring["gate_failures"]
    result.update(_summary_rows_from_ledger(result, requirements, ledger))
    if not result.get("score_reason"):
        result["score_reason"] = "按岗位要求类别权重与简历证据充分度进行本地确定性评分。"
    if scoring["gate_failures"]:
        gates = "、".join(scoring["gate_failures"])
        result["score_reason"] = f"{result['score_reason']}；硬性门槛未满足：{gates}。"
    validated = MatchResult.model_validate(result, strict=True)
    return {"success": True, "match_result": validated.model_dump(mode="python")}


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
optimized_resume_struct: 结构化的完整优化版简历（主要输出，供排版渲染），字段：
  basic_info: {{name, phone, email, location, target_role}}
  summary: 个人简介字符串（100字以内）
  education: 数组 [{{school, degree, major, start, end, highlights: [要点数组]}}]
  experience: 数组 [{{company, title, start, end, bullets: [职责与成果要点数组]}}]
  projects: 数组 [{{name, role, bullets: [要点数组]}}]
  skills: 数组 [{{group: 分组名, items: [技能数组]}}]
  extras: 数组（证书/奖项/语言等字符串，可为空数组）
  内容必须覆盖原简历全部经历段落并体现本次优化建议，不得新增编造
optimized_resume: 留空字符串即可——系统会从optimized_resume_struct自动生成文本版；仅当无法输出结构化时才在此给完整文本
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
    result = ask_json(
        prompt,
        system,
        _SUGGESTION_SCHEMA,
        temperature=0.2,
        label=label,
        max_tokens=config.REPORT_MAX_TOKENS,
        validator=SuggestionResult,
    )
    if result is None:
        return {"success": False, "error": "LLM未能返回合法JSON，请重试generate_suggestions"}
    result = _ensure_struct(result)
    validated = SuggestionResult.model_validate(result, strict=True)
    return {
        "success": True,
        "suggestions": validated.model_dump(mode="python"),
    }


def _valid_struct(struct):
    """结构化简历是否可用：需有basic_info且至少一段经历类内容"""
    return optimized_resume_struct_is_usable(struct)


def _ensure_struct(result):
    """排版数据100%保障：模型漏输出struct时逐层兜底
    第1层：专项LLM调用（只做文本→结构化转换，小任务成功率高）
    第2层：确定性文本解析器（零LLM依赖，永不失败到无输出）
    """
    if _valid_struct(result.get("optimized_resume_struct")):
        return result
    text = (result.get("optimized_resume") or "").strip()
    if not text:
        return result
    print("   ⚠️ 模型未输出结构化简历，启动专项补全...")
    struct = None
    if not get_client().mock_mode:          # Mock模式直接走确定性解析器
        struct = _struct_from_text(text)
    if not _valid_struct(struct):
        print("   ⚠️ 使用本地解析器兜底生成结构化数据")
        struct = parse_resume_text_to_struct(text)
    if _valid_struct(struct):
        result = dict(result)
        result["optimized_resume_struct"] = struct
    return result


def _struct_from_text(resume_text):
    """专项小调用：把优化版简历文本原样转换为结构化JSON（不新增不删减事实）"""
    schema = {"basic_info": {}, "summary": "", "education": [],
              "experience": [], "projects": [], "skills": [], "extras": []}
    prompt = f"""
把以下简历文本转换为结构化JSON，内容必须逐字忠于原文，不得新增、删减或改写任何事实。字段：
basic_info: {{name, phone, email, location, target_role}}
summary: 个人简介字符串（原文没有则留空字符串）
education: 数组 [{{school, degree, major, start, end, highlights: [要点数组]}}]
experience: 数组 [{{company, title, start, end, bullets: [要点数组]}}]
projects: 数组 [{{name, role, bullets: [要点数组]}}]
skills: 数组 [{{group, items: [技能数组]}}]
extras: 数组（证书/奖项/语言等，可为空数组）

简历文本：
{clip_text(resume_text, max_chars=6000)}
"""
    return ask_json(prompt, "你是精确的文档结构化助手。只输出JSON，不改写内容。",
                    schema, temperature=0.0, label="补全结构化排版数据",
                    max_tokens=config.REPORT_MAX_TOKENS)
