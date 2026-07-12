"""Deterministic scoring and pure report rendering tests (offline only)."""

import copy
from decimal import Decimal
from unittest.mock import patch

from report_renderer import render_report
from tools.analysis import calculate_match
from tools.scoring import normalize_resume_evidence, score_requirements


def _four_categories():
    return [
        {"requirement_id": "hard-001", "category": "hard", "requirement": "Degree"},
        {"requirement_id": "skill-001", "category": "skill", "requirement": "Python"},
        {"requirement_id": "business-001", "category": "business", "requirement": "Growth"},
        {"requirement_id": "soft-001", "category": "soft", "requirement": "Communication"},
    ]


def _evidence(status="met"):
    return [
        {
            "requirement_id": requirement["requirement_id"],
            "status": status,
            "evidence_id": f"evidence-{index:03d}",
        }
        for index, requirement in enumerate(_four_categories(), start=1)
    ]


def test_exact_category_weights_and_evidence_ids():
    result = score_requirements(_four_categories(), _evidence(), {})
    assert result["score"] == 100
    assert result["eligible"] is True
    assert [row["points"] for row in result["requirements"]] == [40, 25, 20, 15]
    assert [row["status"] for row in result["requirements"]] == ["met"] * 4
    assert [row["evidence_ids"] for row in result["requirements"]] == [
        ["evidence-001"],
        ["evidence-002"],
        ["evidence-003"],
        ["evidence-004"],
    ]


def test_resume_evidence_catalog_ids_are_stable_unique_and_real():
    resume_info = {
        "basic_info": {"name": "Candidate", "location": "Brisbane"},
        "work_experience": [{"company": "Example", "title": "Analyst"}],
        "skills": ["Python", "SQL"],
    }
    first = normalize_resume_evidence(resume_info)
    second = normalize_resume_evidence(copy.deepcopy(resume_info))
    assert first == second
    ids = [item["evidence_id"] for item in first]
    assert len(ids) == len(set(ids))
    assert ids == [f"evidence-{index:03d}" for index in range(1, len(ids) + 1)]
    assert all(item["content"] not in (None, "", [], {}) for item in first)


def test_under_evidenced_awards_half_points():
    result = score_requirements(
        _four_categories(), _evidence("under_evidenced"), {}
    )
    assert result["score"] == 50
    assert [row["points"] for row in result["requirements"]] == [20, 12.5, 10, 7.5]


def test_row_points_add_up_to_exact_category_weight():
    requirements = [
        {"category": "hard", "requirement": f"Hard requirement {index}"}
        for index in range(1, 4)
    ]
    evidence = [
        {
            "requirement_id": f"hard-{index:03d}",
            "status": "met",
            "evidence_id": f"evidence-{index:03d}",
        }
        for index in range(1, 4)
    ]
    result = score_requirements(requirements, evidence, {})
    points = [Decimal(str(row["points"])) for row in result["requirements"]]
    assert sum(points) == Decimal("40")
    assert result["score"] == 40


def test_empty_categories_are_zero_and_generated_ids_are_stable():
    requirements = [{"category": "hard", "requirement": "Degree"}]
    evidence = [{
        "requirement_id": "hard-001",
        "status": "met",
        "evidence_id": "evidence-001",
    }]
    first = score_requirements(requirements, evidence, {})
    second = score_requirements(copy.deepcopy(requirements), copy.deepcopy(evidence), {})
    assert first == second
    assert first["score"] == 40
    assert first["requirements"] == [{
        "requirement_id": "hard-001",
        "status": "met",
        "points": 40,
        "evidence_ids": ["evidence-001"],
    }]


def test_required_location_and_work_authorization_gates_cannot_be_offset():
    gates = {
        "location": {"required": True, "met": False},
        "work_authorization": {"required": True, "met": True},
    }
    result = score_requirements(_four_categories(), _evidence(), gates)
    assert result["score"] == 100
    assert result["eligible"] is False
    assert result["gate_failures"] == ["location"]

    optional = score_requirements(
        _four_categories(),
        _evidence(),
        {"location": {"required": False, "met": False}},
    )
    assert optional["eligible"] is True


def test_score_is_clamped_to_zero_and_one_hundred():
    missing = score_requirements(_four_categories(), [], {})
    assert missing["score"] == 0
    duplicate_met = _evidence() + _evidence()
    maximum = score_requirements(_four_categories(), duplicate_met, {})
    assert maximum["score"] == 100


def test_met_without_real_evidence_is_downgraded_without_invented_id():
    result = score_requirements(
        [{"category": "hard", "requirement": "Degree"}],
        [{"requirement_id": "hard-001", "status": "met"}],
        {},
    )
    assert result["score"] == 0
    assert result["requirements"] == [{
        "requirement_id": "hard-001",
        "status": "missing",
        "points": 0,
        "evidence_ids": [],
    }]


def test_calculate_match_uses_local_score_instead_of_llm_score():
    jd_analysis = {
        "hard_requirements": ["Degree"],
        "bonus_points": ["Python"],
        "responsibilities": ["Growth"],
        "implicit_requirements": ["Communication"],
    }
    llm_result = {
        "score": 3,
        "score_reason": "Evidence is present for every normalized requirement.",
        "high_matches": [
            {"requirement_id": "hard-001", "requirement": "Degree", "evidence": "BSc"},
            {"requirement_id": "skill-001", "requirement": "Python", "evidence": "Project"},
            {"requirement_id": "business-001", "requirement": "Growth", "evidence": "Revenue"},
            {"requirement_id": "soft-001", "requirement": "Communication", "evidence": "Team"},
        ],
        "partial_matches": [],
        "missing_requirements": [],
        "requirement_evidence": [
            {"requirement_id": "hard-001", "status": "met", "evidence_ids": ["evidence-001"]},
            {"requirement_id": "skill-001", "status": "met", "evidence_ids": ["evidence-001"]},
            {"requirement_id": "business-001", "status": "met", "evidence_ids": ["evidence-001"]},
            {"requirement_id": "soft-001", "status": "met", "evidence_ids": ["evidence-001"]},
        ],
    }
    with patch("tools.analysis.ask_json", return_value=llm_result) as ask:
        result = calculate_match({"skills": ["BSc Python Growth Team"]}, jd_analysis)

    assert result["success"] is True
    match = result["match_result"]
    assert match["score"] == 100
    assert match["score"] != llm_result["score"]
    assert match["score_reason"] == llm_result["score_reason"]
    assert match["eligible"] is True
    assert [row["requirement_id"] for row in match["requirement_scores"]] == [
        "hard-001", "skill-001", "business-001", "soft-001"
    ]
    assert ask.call_args.kwargs["validator"].__name__ == "MatchResult"


def test_calculate_match_scores_uncapped_exhaustive_ledger_not_ui_summaries():
    jd_analysis = {
        "hard_requirements": [f"Requirement {index}" for index in range(1, 8)],
        "bonus_points": [],
        "responsibilities": [],
        "implicit_requirements": [],
    }
    llm_result = {
        "score": 1,
        "score_reason": "All seven requirements cite the same verified resume record.",
        "high_matches": [{
            "requirement_id": "hard-001",
            "requirement": "Requirement 1",
            "evidence": "Verified evidence",
        }],
        "partial_matches": [],
        "missing_requirements": [],
        "requirement_evidence": [
            {
                "requirement_id": f"hard-{index:03d}",
                "status": "met",
                "evidence_ids": ["evidence-001"],
            }
            for index in range(1, 8)
        ],
    }
    with patch("tools.analysis.ask_json", return_value=llm_result):
        result = calculate_match({"skills": ["Verified evidence"]}, jd_analysis)

    match = result["match_result"]
    assert match["score"] == 40
    assert len(match["requirement_scores"]) == 7
    assert all(row["status"] == "met" for row in match["requirement_scores"])


def _report_state():
    return {
        "resume_info": {
            "basic_info": {"name": "Candidate"},
            "education": [],
            "work_experience": [],
            "projects": [],
            "skills": ["Python"],
        },
        "job_recommendations": {},
        "jd_analysis": {"job_title": "Engineer"},
        "match_result": {
            "score": 80,
            "score_reason": "Local evidence score",
            "high_matches": [],
            "partial_matches": [],
            "missing_requirements": [],
            "redundant_or_irrelevant": [],
            "risks": [],
            "recommendation": "Apply",
        },
        "suggestions": {
            "overall_strategy": "Keep claims factual.",
            "rewrite_suggestions": [],
            "star_rewrites": [],
            "keyword_injection": [],
            "honesty_boundaries": [],
            "optimized_resume": "Candidate\nPython Engineer",
        },
        "verification": {
            "passed": True,
            "safe_to_deliver": True,
            "required_fixes": [],
        },
        "correction_log": [],
        "user_clarifications": [],
        "generation_time": "2026-07-12 12:00",
        "analysis_engine": "offline-test",
    }


def test_report_renderer_is_pure_and_keeps_existing_sections():
    state = _report_state()
    before = copy.deepcopy(state)
    first = render_report(state)
    second = render_report(state)
    assert first == second
    assert state == before
    for section in (
        "【简历解析】",
        "【匹配度分析】",
        "【优化建议】",
        "【自我验证】",
        "【诚实评估】",
        "【优化版简历】",
    ):
        assert section in first
    assert "本报告不完整" not in first


def test_report_renderer_marks_partial_and_lists_unresolved_fixes():
    state = _report_state()
    state["verification"] = {
        "passed": True,
        "safe_to_deliver": True,
        "required_fixes": ["Remove unsupported revenue claim"],
    }
    report = render_report(
        state,
        terminal_status="partial",
        unresolved_fixes=["Remove unsupported revenue claim"],
    )
    assert "本报告不完整" in report
    assert "未解决修复项" in report
    assert "Remove unsupported revenue claim" in report
    assert "验证结果**：❌ 未通过" in report


def test_report_renderer_treats_missing_verification_as_partial():
    state = _report_state()
    state.pop("verification")
    report = render_report(state)
    assert "本报告不完整" in report
    assert "验证结果未满足严格交付契约" in report
    assert "验证结果**：❌ 未通过" in report


def main():
    tests = (
        test_exact_category_weights_and_evidence_ids,
        test_resume_evidence_catalog_ids_are_stable_unique_and_real,
        test_under_evidenced_awards_half_points,
        test_row_points_add_up_to_exact_category_weight,
        test_empty_categories_are_zero_and_generated_ids_are_stable,
        test_required_location_and_work_authorization_gates_cannot_be_offset,
        test_score_is_clamped_to_zero_and_one_hundred,
        test_met_without_real_evidence_is_downgraded_without_invented_id,
        test_calculate_match_uses_local_score_instead_of_llm_score,
        test_calculate_match_scores_uncapped_exhaustive_ledger_not_ui_summaries,
        test_report_renderer_is_pure_and_keeps_existing_sections,
        test_report_renderer_marks_partial_and_lists_unresolved_fixes,
        test_report_renderer_treats_missing_verification_as_partial,
    )
    for test in tests:
        test()
        print(f"PASS: {test.__name__}")
    print(f"\n{len(tests)} scoring/report tests passed")


if __name__ == "__main__":
    main()
