"""Fail-closed projection for owner-scoped resume template rendering."""

import json
import unicodedata


MAX_SECTION_RECORDS = 50
MAX_LIST_ITEMS = 100
MAX_SCALAR_CHARS = 500
MAX_LONG_TEXT_CHARS = 4000
MAX_ENCODED_BYTES = 256 * 1024

_BASIC_FIELDS = ("name", "phone", "email", "location", "target_role")
_EDUCATION_FIELDS = ("school", "degree", "major", "start", "end")
_EXPERIENCE_FIELDS = ("company", "title", "start", "end")
_PROJECT_FIELDS = ("name", "role", "start", "end")


class _InvalidPublicResume(ValueError):
    pass


def _text(value, limit):
    if not isinstance(value, str):
        raise _InvalidPublicResume("resume leaf must be a string")
    cleaned = "".join(
        character
        for character in value
        if character in "\n\r\t" or unicodedata.category(character) != "Cc"
    ).strip()
    if len(cleaned) > limit:
        raise _InvalidPublicResume("resume leaf exceeds its size limit")
    return cleaned


def _mapping(value):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise _InvalidPublicResume("resume object has the wrong type")
    return value


def _list(value, maximum):
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > maximum:
        raise _InvalidPublicResume("resume list has the wrong type or size")
    return value


def _strings(value, *, maximum=MAX_LIST_ITEMS, limit=MAX_LONG_TEXT_CHARS):
    result = []
    for item in _list(value, maximum):
        cleaned = _text(item, limit)
        if cleaned:
            result.append(cleaned)
    return result


def _basic_info(value):
    source = _mapping(value)
    return {
        field: _text(source.get(field, ""), MAX_SCALAR_CHARS)
        for field in _BASIC_FIELDS
    }


def _records(value, scalar_fields, list_field, *, list_alias=None):
    records = []
    for raw_record in _list(value, MAX_SECTION_RECORDS):
        source = _mapping(raw_record)
        record = {
            field: _text(source.get(field, ""), MAX_SCALAR_CHARS)
            for field in scalar_fields
        }
        raw_list = source.get(list_field)
        if raw_list is None and list_alias is not None:
            raw_list = source.get(list_alias)
        record[list_field] = _strings(raw_list)
        if any(record[field] for field in scalar_fields) or record[list_field]:
            records.append(record)
    return records


def _skills(value):
    skills = []
    for raw_skill in _list(value, MAX_LIST_ITEMS):
        if isinstance(raw_skill, str):
            text = _text(raw_skill, MAX_SCALAR_CHARS)
            if text:
                skills.append({"group": "技能", "items": [text]})
            continue
        source = _mapping(raw_skill)
        group = _text(source.get("group", ""), MAX_SCALAR_CHARS) or "技能"
        items = _strings(
            source.get("items"),
            maximum=MAX_LIST_ITEMS,
            limit=MAX_SCALAR_CHARS,
        )
        if items:
            skills.append({"group": group, "items": items})
    return skills


def _record_has_text(record):
    return any(
        bool(value) if isinstance(value, str) else bool(value)
        for value in record.values()
    )


def normalize_public_resume(value):
    """Return the canonical template schema or ``None`` for invalid input."""
    if not isinstance(value, dict):
        return None
    try:
        result = {
            "basic_info": _basic_info(value.get("basic_info")),
            "summary": _text(value.get("summary", ""), MAX_LONG_TEXT_CHARS),
            "education": _records(
                value.get("education"),
                _EDUCATION_FIELDS,
                "highlights",
                list_alias="bullets",
            ),
            "experience": _records(
                value.get("experience"),
                _EXPERIENCE_FIELDS,
                "bullets",
            ),
            "projects": _records(
                value.get("projects"),
                _PROJECT_FIELDS,
                "bullets",
            ),
            "skills": _skills(value.get("skills")),
            "extras": _strings(value.get("extras")),
        }
    except _InvalidPublicResume:
        return None
    factual_records = (
        result["education"] + result["experience"] + result["projects"]
    )
    if not any(_record_has_text(record) for record in factual_records):
        return None
    encoded = json.dumps(
        result, ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > MAX_ENCODED_BYTES:
        return None
    return result
