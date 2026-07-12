"""Deterministic scoring and pure report rendering tests (offline only)."""

import copy
from decimal import Decimal
from unittest.mock import patch

from mock_data import MOCK_JD_ANALYSIS, MOCK_MATCH, MOCK_RESUME_INFO
from report_renderer import render_report
from tools.analysis import calculate_match
from tools.resume_tools import analyze_jd, extract_resume_info
from tools.scoring import (
    normalize_jd_requirements,
    normalize_resume_evidence,
    requirement_ledger_from_match_result,
    score_requirements,
)


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
    assert ids == [
        "evidence-basic-info-name",
        "evidence-basic-info-location",
        "evidence-experience-001",
        "evidence-skill-001",
        "evidence-skill-002",
    ]
    assert all(item["content"] not in (None, "", [], {}) for item in first)
    assert not any("candidate" in evidence_id.lower() for evidence_id in ids)


def test_basic_info_changes_do_not_renumber_structured_evidence_paths():
    structured = {
        "education": [{"school": "Example University"}],
        "work_experience": [{"company": "Example Company"}],
        "projects": [{"name": "Example Project"}],
        "skills": ["Python"],
    }
    baseline = normalize_resume_evidence({
        "basic_info": {"name": "Candidate"},
        **structured,
    })
    expanded = normalize_resume_evidence({
        "basic_info": {
            "name": "Candidate",
            "location": "Sydney",
            "target_role": "Engineer",
            "work_authorization": False,
        },
        **structured,
    })

    stable_sources = {
        "education[1]",
        "work_experience[1]",
        "projects[1]",
        "skills[1]",
    }
    baseline_ids = {
        row["source"]: row["evidence_id"]
        for row in baseline if row["source"] in stable_sources
    }
    expanded_ids = {
        row["source"]: row["evidence_id"]
        for row in expanded if row["source"] in stable_sources
    }
    assert baseline_ids == expanded_ids == {
        "education[1]": "evidence-education-001",
        "work_experience[1]": "evidence-experience-001",
        "projects[1]": "evidence-project-001",
        "skills[1]": "evidence-skill-001",
    }


def test_mock_hard_requirements_reference_semantically_correct_paths():
    catalog = normalize_resume_evidence(MOCK_RESUME_INFO)
    by_id = {row["evidence_id"]: row for row in catalog}
    ledger = requirement_ledger_from_match_result(
        MOCK_MATCH,
        normalize_jd_requirements(MOCK_JD_ANALYSIS),
        evidence_catalog=catalog,
    )
    by_requirement = {row["requirement_id"]: row for row in ledger}

    degree_ids = by_requirement["hard-001"]["evidence_ids"]
    experience_ids = by_requirement["hard-002"]["evidence_ids"]
    assert degree_ids == ["evidence-education-001"]
    assert experience_ids == ["evidence-experience-001"]
    assert [by_id[evidence_id]["source"] for evidence_id in degree_ids] == [
        "education[1]"
    ]
    assert [by_id[evidence_id]["evidence_type"] for evidence_id in degree_ids] == [
        "education"
    ]
    assert [by_id[evidence_id]["source"] for evidence_id in experience_ids] == [
        "work_experience[1]"
    ]
    assert [
        by_id[evidence_id]["evidence_type"] for evidence_id in experience_ids
    ] == ["experience"]


def test_requirement_ledger_rejects_inherently_non_scoring_existing_id():
    requirements = [{
        "requirement_id": "hard-001",
        "category": "hard",
        "requirement": "Bachelor degree",
    }]
    catalog = [
        {
            "evidence_id": "valid-basic-id",
            "source": "basic_info.work_authorization",
            "evidence_type": "basic_info",
            "content": False,
        },
        {
            "evidence_id": "valid-education-id",
            "source": "education[1]",
            "evidence_type": "education",
            "content": {"degree": "Bachelor"},
        },
        {
            "evidence_id": "forged-education-id",
            "source": "basic_info.work_authorization",
            "evidence_type": "education",
            "content": False,
        },
    ]
    incompatible = requirement_ledger_from_match_result(
        {
            "requirement_evidence": [{
                "requirement_id": "hard-001",
                "status": "met",
                "evidence_ids": ["valid-basic-id", "forged-education-id"],
            }],
        },
        requirements,
        evidence_catalog=catalog,
    )
    compatible = requirement_ledger_from_match_result(
        {
            "requirement_evidence": [{
                "requirement_id": "hard-001",
                "status": "met",
                "evidence_ids": ["valid-education-id"],
            }],
        },
        requirements,
        evidence_catalog=catalog,
    )

    assert incompatible == [{
        "requirement_id": "hard-001",
        "status": "missing",
        "evidence_ids": [],
    }]
    assert compatible == [{
        "requirement_id": "hard-001",
        "status": "met",
        "evidence_ids": ["valid-education-id"],
    }]


def test_weight_category_does_not_reject_factual_evidence_type():
    requirements = [
        {
            "requirement_id": "skill-001",
            "category": "skill",
            "requirement": "MBA preferred",
        },
        {
            "requirement_id": "business-001",
            "category": "business",
            "requirement": "Cloud platform delivery",
        },
    ]
    catalog = [
        {
            "evidence_id": "evidence-education-001",
            "source": "education[1]",
            "evidence_type": "education",
            "content": {"degree": "MBA"},
        },
        {
            "evidence_id": "evidence-certificate-001",
            "source": "certificates[1]",
            "evidence_type": "certificate",
            "content": "Cloud certification",
        },
    ]
    ledger = requirement_ledger_from_match_result(
        {
            "requirement_evidence": [
                {
                    "requirement_id": "skill-001",
                    "status": "met",
                    "evidence_ids": ["evidence-education-001"],
                },
                {
                    "requirement_id": "business-001",
                    "status": "met",
                    "evidence_ids": ["evidence-certificate-001"],
                },
            ],
        },
        requirements,
        evidence_catalog=catalog,
    )

    assert ledger == [
        {
            "requirement_id": "skill-001",
            "status": "met",
            "evidence_ids": ["evidence-education-001"],
        },
        {
            "requirement_id": "business-001",
            "status": "met",
            "evidence_ids": ["evidence-certificate-001"],
        },
    ]


def test_extract_resume_info_uses_strict_resume_validator():
    valid_result = {
        "education": [{"degree": "BSc"}],
        "work_experience": [],
        "projects": [],
        "skills": ["Python"],
    }
    with patch("tools.resume_tools.ask_json", return_value=valid_result) as ask:
        result = extract_resume_info("Candidate resume")

    assert result["success"] is True
    assert ask.call_args.kwargs["validator"].__name__ == "ResumeInfo"
    prompt = ask.call_args.args[0]
    assert "responsibilities和achievements必须为字符串数组" in prompt
    assert "skills" in prompt and "name" in prompt


def test_normalize_resume_evidence_skips_malformed_nested_shapes():
    resume_info = {
        "education": [False, {}, {"school": "   "}, {"degree": "BSc"}],
        "work_experience": [
            7,
            {},
            {"company": ""},
            {"company": "Malformed", "responsibilities": [""]},
            {"company": "Example"},
        ],
        "projects": [
            "not a record",
            {},
            {"name": "   "},
            {"name": "Malformed", "technologies": ["   "]},
            {"description": "Built a service"},
        ],
        "skills": [
            False,
            7,
            {},
            "",
            "   ",
            {"name": ""},
            "Python",
            {"name": "SQL"},
        ],
    }

    catalog = normalize_resume_evidence(resume_info)
    assert [(row["evidence_id"], row["source"]) for row in catalog] == [
        ("evidence-education-004", "education[4]"),
        ("evidence-experience-005", "work_experience[5]"),
        ("evidence-project-005", "projects[5]"),
        ("evidence-skill-007", "skills[7]"),
        ("evidence-skill-008", "skills[8]"),
    ]


def test_scoring_accepts_legacy_aliases_and_harmless_extra_fields():
    catalog = normalize_resume_evidence({
        "education": [{"degree": "BSc", "country": "AU"}],
        "work_experience": [{
            "achievement": "Growth",
            "source_system": "legacy",
        }],
        "projects": [{
            "description": "Launched service",
            "legacy_id": 7,
        }],
        "skills": [{"name": "Python", "years": 5}],
    })

    assert [(row["evidence_id"], row["source"]) for row in catalog] == [
        ("evidence-education-001", "education[1]"),
        ("evidence-experience-001", "work_experience[1]"),
        ("evidence-project-001", "projects[1]"),
        ("evidence-skill-001", "skills[1]"),
    ]
    by_id = {row["evidence_id"]: row["content"] for row in catalog}
    assert by_id["evidence-education-001"] == {"degree": "BSc"}
    assert by_id["evidence-experience-001"] == {"achievements": ["Growth"]}
    assert by_id["evidence-project-001"] == {"description": "Launched service"}
    assert by_id["evidence-skill-001"] == {"name": "Python"}


def test_ledger_rejects_malformed_catalog_content_and_scores_zero():
    requirements = [
        {"requirement_id": "hard-001", "category": "hard", "requirement": "Degree"},
        {"requirement_id": "skill-001", "category": "skill", "requirement": "Python"},
        {"requirement_id": "business-001", "category": "business", "requirement": "Delivery"},
        {"requirement_id": "soft-001", "category": "soft", "requirement": "Communication"},
    ]
    catalog = [
        {
            "evidence_id": "evidence-education-001",
            "source": "education[1]",
            "evidence_type": "education",
            "content": False,
        },
        {
            "evidence_id": "evidence-skill-001",
            "source": "skills[1]",
            "evidence_type": "skill",
            "content": False,
        },
        {
            "evidence_id": "evidence-experience-001",
            "source": "work_experience[1]",
            "evidence_type": "experience",
            "content": 7,
        },
        {
            "evidence_id": "evidence-project-001",
            "source": "projects[1]",
            "evidence_type": "project",
            "content": "not a record",
        },
    ]
    match_result = {
        "requirement_evidence": [
            {
                "requirement_id": requirement["requirement_id"],
                "status": "met",
                "evidence_ids": [catalog[index]["evidence_id"]],
            }
            for index, requirement in enumerate(requirements)
        ],
    }
    ledger = requirement_ledger_from_match_result(
        match_result,
        requirements,
        evidence_catalog=catalog,
    )
    scoring = score_requirements(requirements, ledger, {})

    assert all(row["status"] == "missing" for row in ledger)
    assert all(row["evidence_ids"] == [] for row in ledger)
    assert scoring["score"] == 0


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
            {"requirement_id": "hard-001", "status": "met", "evidence_ids": ["evidence-education-001"]},
            {"requirement_id": "skill-001", "status": "met", "evidence_ids": ["evidence-skill-001"]},
            {"requirement_id": "business-001", "status": "met", "evidence_ids": ["evidence-experience-001"]},
            {"requirement_id": "soft-001", "status": "met", "evidence_ids": ["evidence-project-001"]},
        ],
    }
    with patch("tools.analysis.ask_json", return_value=llm_result) as ask:
        result = calculate_match({
            "education": [{"degree": "BSc"}],
            "work_experience": [{"achievement": "Growth"}],
            "projects": [{"description": "Communication"}],
            "skills": ["Python"],
        }, jd_analysis)

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
                "evidence_ids": ["evidence-skill-001"],
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


def _empty_match_result():
    return {
        "score": 0,
        "score_reason": "No requirement evidence supplied.",
        "high_matches": [],
        "partial_matches": [],
        "missing_requirements": [],
        "requirement_evidence": [],
    }


def test_analyze_jd_uses_strict_explicit_gate_contract():
    jd_result = {
        "job_title": "Platform Engineer",
        "company_or_industry": "Technology",
        "hard_requirements": [],
        "bonus_points": [],
        "implicit_requirements": [],
        "keywords": [],
        "responsibilities": [],
        "risk_points": [],
        "raw_summary": "Brisbane role requiring existing work authorization.",
        "gates": {
            "location": {
                "required": True,
                "accepted_values": ["Brisbane"],
            },
            "work_authorization": {
                "required": True,
                "accepted_values": [],
            },
        },
    }
    with patch("tools.resume_tools.ask_json", return_value=jd_result) as ask:
        result = analyze_jd("This role is Brisbane-only. Existing work rights required.")

    assert result["success"] is True
    assert result["jd_analysis"]["gates"] == jd_result["gates"]
    assert ask.call_args.kwargs["validator"].__name__ == "JDAnalysis"
    prompt = ask.call_args.args[0]
    assert "accepted_values" in prompt
    assert "不得仅因JD提到城市" in prompt


def test_calculate_match_fails_explicit_brisbane_only_gate_for_sydney_candidate():
    resume_info = {
        "basic_info": {
            "name": "Candidate",
            "location": "Sydney",
            "work_authorization": True,
        },
        "skills": [],
    }
    jd_analysis = {
        "hard_requirements": [],
        "bonus_points": [],
        "responsibilities": [],
        "implicit_requirements": [],
        "gates": {
            "location": {
                "required": True,
                "accepted_values": ["Brisbane"],
            },
            "work_authorization": {
                "required": False,
                "accepted_values": [],
            },
        },
    }
    with patch("tools.analysis.ask_json", return_value=_empty_match_result()) as ask:
        result = calculate_match(
            resume_info,
            jd_analysis,
            preferences="Sydney roles only",
        )

    match = result["match_result"]
    assert match["eligible"] is False
    assert match["gate_failures"] == ["location"]
    prompt = ask.call_args.args[0]
    assert "Sydney roles only" in prompt
    assert "basic_info.work_authorization" in prompt
    assert "user.preferences" in prompt


def test_calculate_match_fails_explicit_work_authorization_gate_when_not_met():
    resume_info = {
        "basic_info": {
            "name": "Candidate",
            "location": "Brisbane",
            "work_authorization": False,
        },
        "skills": [],
    }
    jd_analysis = {
        "hard_requirements": [],
        "bonus_points": [],
        "responsibilities": [],
        "implicit_requirements": [],
        "gates": {
            "location": {
                "required": False,
                "accepted_values": [],
            },
            "work_authorization": {
                "required": True,
                "accepted_values": [],
            },
        },
    }
    with patch("tools.analysis.ask_json", return_value=_empty_match_result()):
        result = calculate_match(resume_info, jd_analysis)

    match = result["match_result"]
    assert match["eligible"] is False
    assert match["gate_failures"] == ["work_authorization"]


def test_calculate_match_does_not_infer_gates_from_requirement_keywords():
    resume_info = {
        "basic_info": {
            "location": "Sydney",
            "work_authorization": False,
        },
    }
    jd_analysis = {
        "hard_requirements": [
            "Brisbane-only role",
            "Existing work authorization required",
        ],
        "bonus_points": [],
        "responsibilities": [],
        "implicit_requirements": [],
    }
    with patch("tools.analysis.ask_json", return_value=_empty_match_result()):
        result = calculate_match(resume_info, jd_analysis)

    match = result["match_result"]
    assert match["eligible"] is True
    assert match["gate_failures"] == []


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
        test_basic_info_changes_do_not_renumber_structured_evidence_paths,
        test_mock_hard_requirements_reference_semantically_correct_paths,
        test_requirement_ledger_rejects_inherently_non_scoring_existing_id,
        test_weight_category_does_not_reject_factual_evidence_type,
        test_extract_resume_info_uses_strict_resume_validator,
        test_normalize_resume_evidence_skips_malformed_nested_shapes,
        test_scoring_accepts_legacy_aliases_and_harmless_extra_fields,
        test_ledger_rejects_malformed_catalog_content_and_scores_zero,
        test_under_evidenced_awards_half_points,
        test_row_points_add_up_to_exact_category_weight,
        test_empty_categories_are_zero_and_generated_ids_are_stable,
        test_required_location_and_work_authorization_gates_cannot_be_offset,
        test_score_is_clamped_to_zero_and_one_hundred,
        test_met_without_real_evidence_is_downgraded_without_invented_id,
        test_calculate_match_uses_local_score_instead_of_llm_score,
        test_calculate_match_scores_uncapped_exhaustive_ledger_not_ui_summaries,
        test_analyze_jd_uses_strict_explicit_gate_contract,
        test_calculate_match_fails_explicit_brisbane_only_gate_for_sydney_candidate,
        test_calculate_match_fails_explicit_work_authorization_gate_when_not_met,
        test_calculate_match_does_not_infer_gates_from_requirement_keywords,
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
