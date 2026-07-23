"""Strict public rendering contract for optimized resume structures."""

import json

from public_resume import normalize_public_resume


def factual_resume(**overrides):
    value = {
        "basic_info": {
            "name": "Candidate", "phone": "", "email": "",
            "location": "Brisbane", "target_role": "Data Analyst",
        },
        "summary": "Evidence-based analyst.",
        "education": [],
        "experience": [{
            "company": "Example", "title": "Analyst", "start": "2024",
            "end": "2026", "bullets": ["Improved reporting accuracy."],
        }],
        "projects": [],
        "skills": [{"group": "Data", "items": ["Python", "SQL"]}],
        "extras": ["English"],
    }
    value.update(overrides)
    return value


def test_normalizes_only_the_rendering_schema():
    value = factual_resume()
    value["basic_info"]["api_key"] = "secret"
    value["experience"][0]["prompt"] = "PRIVATE"
    value["root_secret"] = "PRIVATE-ROOT"
    result = normalize_public_resume(value)
    assert set(result) == {
        "basic_info", "summary", "education", "experience", "projects",
        "skills", "extras",
    }
    assert result["basic_info"] == {
        "name": "Candidate", "phone": "", "email": "",
        "location": "Brisbane", "target_role": "Data Analyst",
    }
    serialized = json.dumps(result, ensure_ascii=False)
    assert "secret" not in serialized.lower()
    assert "PRIVATE" not in serialized


def test_maps_only_the_two_supported_compatibility_shapes():
    result = normalize_public_resume(factual_resume(
        education=[{
            "school": "Example University", "degree": "BSc", "major": "Data",
            "start": "2020", "end": "2023", "bullets": ["Dean list"],
        }],
        experience=[],
        skills=["Python"],
    ))
    assert result["education"][0]["highlights"] == ["Dean list"]
    assert result["skills"] == [{"group": "技能", "items": ["Python"]}]


def test_wrong_container_or_leaf_types_fail_closed():
    assert normalize_public_resume(factual_resume(experience="not-a-list")) is None
    assert normalize_public_resume(factual_resume(
        experience=[{"company": "Example", "bullets": [7]}],
    )) is None
    assert normalize_public_resume(factual_resume(
        skills=[{"group": "Data", "items": "Python"}],
    )) is None
    assert normalize_public_resume(factual_resume(summary=False)) is None


def test_basic_info_without_a_factual_record_is_not_renderable():
    assert normalize_public_resume({
        "basic_info": {"name": "Candidate"},
        "summary": "Summary only",
        "education": [], "experience": [], "projects": [],
        "skills": ["Python"], "extras": [],
    }) is None


def test_disallowed_controls_are_removed_but_html_remains_text():
    value = factual_resume(summary="Safe\x00 text <script>alert(1)</script>\nnext")
    result = normalize_public_resume(value)
    assert "\x00" not in result["summary"]
    assert "<script>alert(1)</script>" in result["summary"]
    assert "\nnext" in result["summary"]


def test_section_and_nested_array_limits_fail_closed():
    records = [
        {"company": f"Company {index}", "title": "Analyst", "bullets": []}
        for index in range(51)
    ]
    assert normalize_public_resume(factual_resume(experience=records)) is None
    too_many_bullets = [f"Evidence {index}" for index in range(101)]
    assert normalize_public_resume(factual_resume(
        experience=[{"company": "Example", "bullets": too_many_bullets}],
    )) is None


def test_string_limits_fail_closed_without_silent_fact_truncation():
    assert normalize_public_resume(factual_resume(
        basic_info={"name": "N" * 501},
    )) is None
    assert normalize_public_resume(factual_resume(summary="S" * 4001)) is None
    assert normalize_public_resume(factual_resume(
        experience=[{"company": "Example", "bullets": ["B" * 4001]}],
    )) is None


def test_aggregate_size_limit_fails_closed():
    records = [
        {
            "company": f"Company {index}", "title": "Analyst",
            "bullets": ["X" * 100 for _ in range(100)],
        }
        for index in range(50)
    ]
    assert normalize_public_resume(factual_resume(experience=records)) is None


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"\n{len(tests)} public-resume tests passed")
