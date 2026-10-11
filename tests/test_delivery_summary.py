"""交付摘要事实映射、类型边界与公开投影检查，完全离线。"""

from _bootstrap import PROJECT_ROOT  # noqa: F401

from copy import deepcopy

from delivery_summary import build_delivery_summary, normalize_delivery_summary
from mock_data import MOCK_MATCH, MOCK_SUGGESTIONS_ROUND2


def test_summary_maps_real_facts_without_mutating_state():
    state = {"match_result": deepcopy(MOCK_MATCH), "suggestions": deepcopy(MOCK_SUGGESTIONS_ROUND2)}
    original = deepcopy(state)
    summary = build_delivery_summary(state, verified=True)
    assert summary["match_score"] == MOCK_MATCH["score"]
    assert summary["verified"] is True
    assert summary["strengths"][0] == "本科及以上学历；江南大学市场营销本科；完全满足"
    assert summary["gaps"][0] == "活动运营（独立负责）；均为参与/协助角色，缺少独立负责的活动案例"
    assert summary["gaps"][-1] == "输出运营分析报告的独立能力；面试中可能被追问"
    assert MOCK_SUGGESTIONS_ROUND2["overall_strategy"] in summary["priority_actions"]
    assert MOCK_MATCH["recommendation"] in summary["priority_actions"]
    assert MOCK_MATCH["partial_matches"][0]["improvement"] in summary["priority_actions"]
    assert MOCK_MATCH["missing_requirements"][0]["possible_action"] in summary["priority_actions"]
    for boundary in MOCK_SUGGESTIONS_ROUND2["honesty_boundaries"]:
        assert boundary in summary["priority_actions"]
    rewrite = MOCK_SUGGESTIONS_ROUND2["rewrite_suggestions"][0]
    assert summary["changes"][0] == {
        "section": rewrite["section"], "before": rewrite["before"],
        "after": rewrite["after"], "reason": rewrite["problem"] + "；" + rewrite["suggestion"],
    }
    assert state == original


def test_missing_facts_stay_empty_and_verified_is_explicit():
    expected = {"match_score": None, "strengths": [], "gaps": [], "priority_actions": [], "changes": [], "verified": False}
    for invalid in (None, [], "bad", {}, {"match_result": [], "suggestions": "bad"}):
        assert build_delivery_summary(invalid) == expected
        assert normalize_delivery_summary(invalid) == expected
    assert build_delivery_summary({"verification": {"safe_to_deliver": True}}) == expected
    assert build_delivery_summary({}, verified="true")["verified"] is False
    assert normalize_delivery_summary({"verified": 1})["verified"] is False
    assert build_delivery_summary({"match_result": {"offer_probability": 99, "estimated_score": 90}}) == expected


def test_score_is_clamped_without_coercing_missing_or_invalid_values():
    for value, expected in ((-12, 0), (1000, 100), (0, 0), (84.5, 84.5), (True, None), ("76", None), (None, None), (float("nan"), None), (float("inf"), None)):
        assert normalize_delivery_summary({"match_score": value})["match_score"] == expected
        assert build_delivery_summary({"match_result": {"score": value}})["match_score"] == expected


def test_normalization_bounds_all_fields_and_ignores_arbitrary_data():
    long_text = "中" * 601
    value = {
        "match_score": 80, "strengths": [None, {}, " "] + [str(index) + long_text for index in range(12)],
        "gaps": [long_text + str(index) for index in range(12)],
        "priority_actions": ["行动" + str(index) + long_text for index in range(12)],
        "changes": [None, "bad", {"section": "no rewrite"}] + [
            {"section": long_text, "before": long_text, "after": long_text,
             "reason": long_text, "private": "PRIVATE"} for _ in range(12)
        ],
        "verified": True, "api_key": "PRIVATE", "report": "PRIVATE",
    }
    result = normalize_delivery_summary(value)
    assert set(result) == {"match_score", "strengths", "gaps", "priority_actions", "changes", "verified"}
    for field in ("strengths", "gaps", "priority_actions"):
        assert 1 <= len(result[field]) <= 8
        assert all(0 < len(text) <= 600 for text in result[field])
    assert len(result["strengths"]) == len(result["priority_actions"]) == 8
    assert len(result["changes"]) == 6
    assert all(set(change) == {"section", "before", "after", "reason"} for change in result["changes"])
    assert all(len(text) == 600 for change in result["changes"] for text in change.values())
    assert "PRIVATE" not in str(result)
    assert normalize_delivery_summary(result) == result


def test_wrong_leaf_types_and_only_known_source_fields_are_used():
    state = {
        "match_result": {
            "high_matches": [False, {"requirement_id": "PRIVATE", "unknown": "PRIVATE"}, " Excel ", {"requirement": "Excel", "evidence": {"private": 1}}],
            "partial_matches": [{"requirement": "SQL", "gap": "缺少案例", "improvement": "补充真实案例"}],
            "missing_requirements": ["Python"], "unknown": "PRIVATE",
        },
        "suggestions": {
            "rewrite_suggestions": [None, {"section": "工作", "before": 123, "after": "参与分析", "reason": "保留角色", "unknown": "PRIVATE"}],
            "honesty_boundaries": [False, "保持参与定位"], "optimized_resume": "PRIVATE",
        },
    }
    summary = build_delivery_summary(state)
    assert summary["strengths"] == ["Excel"]
    assert summary["gaps"] == ["SQL；缺少案例", "Python"]
    assert summary["priority_actions"] == ["保持参与定位", "补充真实案例"]
    assert summary["changes"] == [{"section": "工作", "before": "", "after": "参与分析", "reason": "保留角色"}]
    assert "PRIVATE" not in str(summary)
    normalized = normalize_delivery_summary({"strengths": "wrong", "gaps": {}, "priority_actions": 12, "changes": {}})
    assert all(normalized[field] == [] for field in ("strengths", "gaps", "priority_actions", "changes"))


if __name__ == "__main__":
    tests = [value for name, value in globals().copy().items() if name.startswith("test_")]
    for test in tests:
        test()
        print("PASS", test.__name__)
