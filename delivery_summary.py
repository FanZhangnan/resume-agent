"""把已有匹配和改写结果投影为有界交付摘要，不生成新的求职结论。"""

import math


MAX_TEXT_CHARS = 600
MAX_LIST_ITEMS = 8
MAX_CHANGES = 6


def _text(value):
    return value.strip()[:MAX_TEXT_CHARS] if isinstance(value, str) else ""


def _score(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return min(100, max(0, value))


def _strings(value):
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        cleaned = _text(item)
        if cleaned and cleaned not in result:
            result.append(cleaned)
        if len(result) == MAX_LIST_ITEMS:
            break
    return result


def _changes(value):
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        if not isinstance(item, dict):
            continue
        change = {
            field: _text(item.get(field))
            for field in ("section", "before", "after", "reason")
        }
        if change["before"] or change["after"]:
            result.append(change)
        if len(result) == MAX_CHANGES:
            break
    return result


def normalize_delivery_summary(value):
    """忽略未知字段与错误类型；缺失分数保留为 None，验证标记严格判断。"""
    source = value if isinstance(value, dict) else {}
    return {
        "match_score": _score(source.get("match_score")),
        "strengths": _strings(source.get("strengths")),
        "gaps": _strings(source.get("gaps")),
        "priority_actions": _strings(source.get("priority_actions")),
        "changes": _changes(source.get("changes")),
        "verified": source.get("verified") is True,
    }


def _rows(value):
    return value if isinstance(value, list) else []


def _join_fields(value, fields):
    if isinstance(value, str):
        return _text(value)
    if not isinstance(value, dict):
        return ""
    parts = []
    for field in fields:
        part = _text(value.get(field))
        if part and part not in parts:
            parts.append(part)
    return _text("；".join(parts))


def build_delivery_summary(state, verified=False):
    """仅映射现有状态字段；verified 由调用者的交付验证结果提供。"""
    source = state if isinstance(state, dict) else {}
    match = source.get("match_result")
    match = match if isinstance(match, dict) else {}
    suggestions = source.get("suggestions")
    suggestions = suggestions if isinstance(suggestions, dict) else {}
    partial = _rows(match.get("partial_matches"))
    missing = _rows(match.get("missing_requirements"))
    changes = []
    for item in _rows(suggestions.get("rewrite_suggestions")):
        if not isinstance(item, dict):
            continue
        changes.append({
            "section": item.get("section"),
            "before": item.get("before"),
            "after": item.get("after"),
            "reason": _text(item.get("reason")) or _join_fields(
                item, ("problem", "suggestion"),
            ),
        })
    return normalize_delivery_summary({
        "match_score": match.get("score"),
        "strengths": [
            _join_fields(item, ("requirement", "evidence", "reason"))
            for item in _rows(match.get("high_matches"))
        ],
        "gaps": [
            _join_fields(item, ("requirement", "gap")) for item in partial
        ] + [
            _join_fields(item, ("requirement", "impact")) for item in missing
        ],
        "priority_actions": [
            source.get("cohort_warning"),
            suggestions.get("overall_strategy"), match.get("recommendation"),
        ] + _rows(suggestions.get("honesty_boundaries")) + [
            _text(item.get("improvement"))
            for item in partial if isinstance(item, dict)
        ] + [
            _text(item.get("possible_action"))
            for item in missing if isinstance(item, dict)
        ],
        "changes": changes,
        "verified": verified is True,
    })
