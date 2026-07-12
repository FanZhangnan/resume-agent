"""Deterministic requirement scoring for resume/JD matching."""

import re
import unicodedata
from collections import Counter, defaultdict
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP

from pydantic import ValidationError

from contracts import (
    EducationRecord,
    ProjectRecord,
    SkillRecord,
    WorkExperienceRecord,
)


CATEGORY_WEIGHTS = {
    "hard": 40,
    "skill": 25,
    "business": 20,
    "soft": 15,
}

STATUS_FACTORS = {
    "met": 1.0,
    "under_evidenced": 0.5,
    "missing": 0.0,
}

# Weight buckets are not evidence domains: an education record can support a
# bonus requirement, while basic metadata and generated summaries support none.
SCORABLE_EVIDENCE_TYPES = frozenset({
    "education", "experience", "project", "skill", "certificate",
    "achievement",
})

_RECORD_MODELS = {
    "education": EducationRecord,
    "experience": WorkExperienceRecord,
    "project": ProjectRecord,
}

_ENGLISH_STOP_TERMS = frozenset({
    "a", "above", "an", "and", "are", "as", "at", "be", "been", "being",
    "by", "candidate", "experience", "experienced", "familiar", "for",
    "from", "has", "have", "higher", "in", "is", "knowledge", "least",
    "minimum", "must", "of", "on", "or", "preferred", "proficient",
    "related", "relevant", "requirement", "requirements", "required", "s",
    "skill", "skills", "strong", "the", "to", "use", "used", "using",
    "with", "year", "years",
})

_CJK_STOP_TERMS = frozenset({
    "以上", "优先", "使用", "具备", "工作", "相关", "经验", "能力", "要求",
    "熟悉", "熟练", "负责",
})

_CREDENTIAL_TERMS = frozenset({
    "associate", "bachelor", "master", "doctorate",
})

_CREDENTIAL_LEVELS = {
    "associate": 1,
    "bachelor": 2,
    "master": 3,
    "doctorate": 4,
}

_GENERIC_MATCH_TERMS = frozenset({
    "application", "applications", "backend", "business", "cloud", "data",
    "deliver", "delivered", "delivery", "developer", "developers",
    "development", "engineer", "engineering", "engineers", "frontend",
    "operation", "operations", "platform", "production", "project",
    "projects", "qualification", "qualifications", "service", "services",
    "solution", "solutions", "support", "supporting", "system", "systems",
    "team", "web", "work", "业务",
    "云", "前端", "后端", "工程师", "开发", "数据", "服务", "平台", "系统",
    "项目", "运营",
})

_FALLBACK_CORE_TERMS = frozenset({"cloud"})

_LATIN_TERM_ALIASES = {
    "analyse": "analyze",
    "analysed": "analyze",
    "analyses": "analyze",
    "analysing": "analyze",
    "analysis": "analyze",
    "analyze": "analyze",
    "analyzed": "analyze",
    "analyzes": "analyze",
    "analyzing": "analyze",
}

_BUSINESS_ADMINISTRATION_TERM = "business_administration"

_DEGREE_SUBJECT_CONTEXT_TERMS = frozenset({
    "area", "areas", "discipline", "disciplines", "equivalent",
    "equivalents", "field", "fields", "honors", "honours", "qualification",
    "qualifications", "subject", "subjects",
})

_CJK_SUBJECT_CONTEXT_TERMS = (
    "相关专业", "相关领域", "相关学科", "相关方向", "专业", "领域", "学科",
    "方向", "具备", "熟悉", "相关", "要求",
)

_LATIN_CREDENTIAL_ALIASES = {
    "associate": "associate",
    "associates": "associate",
    "ba": "bachelor",
    "b.a": "bachelor",
    "bachelor": "bachelor",
    "bachelors": "bachelor",
    "bs": "bachelor",
    "b.s": "bachelor",
    "bsc": "bachelor",
    "b.sc": "bachelor",
    "doctor": "doctorate",
    "doctoral": "doctorate",
    "doctorate": "doctorate",
    "ma": "master",
    "m.a": "master",
    "master": "master",
    "masters": "master",
    "mba": "master",
    "ms": "master",
    "m.s": "master",
    "msc": "master",
    "m.sc": "master",
    "phd": "doctorate",
    "ph.d": "doctorate",
    "undergraduate": "bachelor",
}

_CJK_CREDENTIAL_ALIASES = {
    "大专": "associate",
    "专科": "associate",
    "本科": "bachelor",
    "学士": "bachelor",
    "硕士": "master",
    "博士": "doctorate",
}


def _clean_text(value):
    return str(value or "").strip()


def _normalize_technical_text(value):
    text = unicodedata.normalize("NFKC", _clean_text(value)).casefold()
    text = text.replace("’", "'")
    text = re.sub(
        r"(?<![a-z0-9])([a-z][a-z0-9]*)\+\+", r"\1plusplus", text
    )
    return re.sub(
        r"(?<![a-z0-9])([a-z][a-z0-9]*)#", r"\1sharp", text
    )


def _compact_key(value):
    return re.sub(
        r"[^0-9a-z\u4e00-\u9fff]+", "", _normalize_technical_text(value)
    )


def _semantic_terms(value):
    text = _normalize_technical_text(value)
    terms = set()
    for token in re.findall(r"[a-z][a-z0-9]*(?:\.[a-z0-9]+)*", text):
        token = token.strip(".")
        if not token or token in _ENGLISH_STOP_TERMS:
            continue
        alias = _LATIN_CREDENTIAL_ALIASES.get(token)
        if alias:
            terms.add(alias)
            continue
        alias = _LATIN_TERM_ALIASES.get(token)
        if alias:
            terms.add(alias)
            continue
        if token == "degree":
            terms.add("degree")
            continue
        terms.add(token)
        if len(token) > 4 and token.endswith("s") and not token.endswith("ss"):
            terms.add(token[:-1])

    for source, alias in _CJK_CREDENTIAL_ALIASES.items():
        if source in text:
            terms.add(alias)
    if "学历" in text:
        terms.add("degree")
    cjk_text = text
    for source in sorted(_CJK_CREDENTIAL_ALIASES, key=len, reverse=True):
        cjk_text = cjk_text.replace(source, " ")
    for marker in ("及以上", "或以上", "以上学历", "学历", "学位"):
        cjk_text = cjk_text.replace(marker, " ")
    for sequence in re.findall(r"[\u4e00-\u9fff]+", cjk_text):
        if len(sequence) < 2:
            continue
        max_size = min(4, len(sequence))
        for size in range(2, max_size + 1):
            for offset in range(len(sequence) - size + 1):
                term = sequence[offset:offset + size]
                if term not in _CJK_STOP_TERMS:
                    terms.add(term)
    return terms


def _searchable_evidence_values(evidence_type, content):
    if evidence_type == "education":
        fields = ("school", "degree", "major", "details")
    elif evidence_type == "experience":
        fields = ("company", "title", "responsibilities", "achievements")
    elif evidence_type == "project":
        fields = (
            "name", "role", "description", "achievements", "technologies",
        )
    elif evidence_type == "skill":
        fields = ("name", "category", "level", "details")
    else:
        return [content] if isinstance(content, str) else []

    if isinstance(content, str):
        return [content]
    if not isinstance(content, dict):
        return []
    values = []
    for field in fields:
        value = content.get(field)
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, list):
            values.extend(item for item in value if isinstance(item, str))
    return values


def _evidence_search_terms(evidence_type, content):
    terms = set()
    for value in _searchable_evidence_values(evidence_type, content):
        terms.update(_semantic_terms(value))
    terms.difference_update(_CREDENTIAL_TERMS)
    terms.discard("degree")
    terms.update(_evidence_credential_terms(evidence_type, content))
    return terms


def _evidence_credential_terms(evidence_type, content):
    if evidence_type != "education" or not isinstance(content, dict):
        return set()
    return _semantic_terms(content.get("degree")) & _CREDENTIAL_TERMS


def _core_terms(terms):
    return (
        set(terms)
        - _CREDENTIAL_TERMS
        - _GENERIC_MATCH_TERMS
        - {"degree"}
    )


def _allows_higher_credential(requirement_text):
    text = _normalize_technical_text(requirement_text)
    return any(marker in text for marker in (
        "at least", "minimum", "or above", "or higher", "and above",
        "and higher", "及以上", "或以上",
    ))


def _requires_all_core_terms(requirement_text):
    text = _normalize_technical_text(requirement_text)
    requirement_core = _requirement_core_terms(text)
    if len(requirement_core) < 2:
        return False
    connector_pattern = re.compile(
        r"\b(?:and|plus)\b|&|(?<!\+)\+(?!\+)|以及|和|与|及|、"
    )
    for connector in connector_pattern.finditer(text):
        left_core = _requirement_core_terms(text[:connector.start()])
        right_core = _requirement_core_terms(text[connector.end():])
        if requirement_core & left_core and requirement_core & right_core:
            return True
    return False


def _is_business_administration(value):
    text = _normalize_technical_text(value)
    return bool(re.search(
        r"(?<![a-z0-9])m\.?b\.?a\.?(?![a-z0-9])", text
    )) or "master of business administration" in text


def _subject_terms(value):
    text = _normalize_technical_text(value)
    terms = (
        _semantic_terms(text)
        - _CREDENTIAL_TERMS
        - _DEGREE_SUBJECT_CONTEXT_TERMS
        - {"degree"}
    )
    terms = {
        term for term in terms
        if not re.search(r"[\u4e00-\u9fff]", term)
    }
    cjk_text = text
    for source in sorted(_CJK_CREDENTIAL_ALIASES, key=len, reverse=True):
        cjk_text = cjk_text.replace(source, " ")
    for marker in ("及以上", "或以上", "以上学历", "学历", "学位"):
        cjk_text = cjk_text.replace(marker, " ")
    for marker in _CJK_SUBJECT_CONTEXT_TERMS:
        cjk_text = cjk_text.replace(marker, " ")
    terms.update(
        f"cjk:{sequence}"
        for sequence in re.findall(r"[\u4e00-\u9fff]+", cjk_text)
        if len(sequence) >= 2
    )
    return terms


def _degree_subject_alternatives(value, preserve_education=False):
    text = _normalize_technical_text(value)
    has_in_subject = " in " in text
    subject_text = text.rsplit(" in ", 1)[-1] if has_in_subject else text
    subject_text = re.sub(
        r"\bor\s+(?:(?:a|an)\s+)?related\s+"
        r"(?:field|discipline|area|subject)s?\b",
        "",
        subject_text,
    )
    subject_text = re.sub(
        r"\bor\s+(?:(?:a|an)\s+)?equivalent"
        r"(?:\s+qualification)?\b",
        "",
        subject_text,
    )
    segments = re.split(r"\s*(?:,|\bor\b|或|、)\s*", subject_text)
    alternatives = [
        terms for terms in (_subject_terms(segment) for segment in segments)
        if terms
    ]
    if not has_in_subject and re.search(
        r"\b(?:associate|bachelor|master)(?:'s)?\s+of\s+(?:arts|science)\b",
        text,
    ):
        for terms in alternatives:
            terms.difference_update({"arts", "science"})
    if (
        not preserve_education
        and " of education" not in text
        and " in education" not in text
    ):
        for terms in alternatives:
            terms.discard("education")
    if _is_business_administration(value):
        if not has_in_subject:
            return [{_BUSINESS_ADMINISTRATION_TERM}]
        if not alternatives:
            alternatives = [set()]
        for terms in alternatives:
            terms.add(_BUSINESS_ADMINISTRATION_TERM)
    return [terms for terms in alternatives if terms]


def _credential_level_matches(required, evidence, allow_higher):
    if not evidence:
        return False
    if not required:
        return True
    if not allow_higher:
        return bool(required & evidence)
    minimum = min(_CREDENTIAL_LEVELS[value] for value in required)
    return any(_CREDENTIAL_LEVELS[value] >= minimum for value in evidence)


def _education_subject_alternatives(content):
    if not isinstance(content, dict):
        return []
    alternatives = _degree_subject_alternatives(content.get("degree"))
    for field in ("major", "details"):
        alternatives.extend(_degree_subject_alternatives(
            content.get(field), preserve_education=True
        ))
    if alternatives:
        alternatives.append(set().union(*alternatives))
    return alternatives


def _subject_alternative_matches(required, evidence):
    required_cjk = {
        term.removeprefix("cjk:")
        for term in required if term.startswith("cjk:")
    }
    evidence_cjk = {
        term.removeprefix("cjk:")
        for term in evidence if term.startswith("cjk:")
    }
    if any(
        not any(subject in candidate for candidate in evidence_cjk)
        for subject in required_cjk
    ):
        return False
    required_latin = {
        term for term in required if not term.startswith("cjk:")
    }
    evidence_latin = {
        term for term in evidence if not term.startswith("cjk:")
    }
    return required_latin <= evidence_latin


def _education_supports_degree_requirement(requirement_text, content):
    requirement_terms = _semantic_terms(requirement_text)
    required_credentials = _requirement_credential_terms(requirement_text)
    evidence_credentials = _evidence_credential_terms("education", content)
    if not _credential_level_matches(
        required_credentials,
        evidence_credentials,
        _allows_higher_credential(requirement_text),
    ):
        return False
    required_alternatives = _degree_subject_alternatives(requirement_text)
    if not required_alternatives:
        return True
    evidence_alternatives = _education_subject_alternatives(content)
    return any(
        _subject_alternative_matches(required, evidence)
        for required in required_alternatives
        for evidence in evidence_alternatives
    )


def _requirement_credential_terms(requirement_text):
    text = _normalize_technical_text(requirement_text)
    semantic_credentials = _semantic_terms(text) & _CREDENTIAL_TERMS
    if (
        "degree" in _semantic_terms(text)
        or any(marker in text for marker in (
            "学历", "学位", "education", "qualification",
        ))
    ):
        return semantic_credentials

    credentials = set()
    if any(value in text for value in _CJK_CREDENTIAL_ALIASES):
        credentials.update(semantic_credentials)
    if re.search(
        r"(?<![a-z0-9])(?:b\.?sc|b\.?s\.?|bachelor(?:'s)?|undergraduate)"
        r"(?![a-z0-9])",
        text,
    ):
        credentials.add("bachelor")
    if (
        re.search(
            r"(?<![a-z0-9])(?:m\.?b\.?a\.?|m\.?sc|m\.?s\.?)"
            r"(?![a-z0-9])",
            text,
        )
        or re.search(r"\bmaster(?:'s)?\s+(?:of|in)\b", text)
    ):
        credentials.add("master")
    if re.search(
        r"(?<![a-z0-9])(?:ph\.?d\.?|doctorate|doctoral)(?![a-z0-9])",
        text,
    ):
        credentials.add("doctorate")
    if (
        re.search(r"\bassociate(?:'s)?\s+of\s+(?:arts|science)\b", text)
    ):
        credentials.add("associate")
    return credentials


def _is_degree_requirement(requirement_text):
    requirement_terms = _semantic_terms(requirement_text)
    return bool(
        _requirement_credential_terms(requirement_text)
        or "degree" in requirement_terms
    )


def _requirement_core_terms(requirement_text):
    requirement_terms = _semantic_terms(requirement_text)
    core = _core_terms(requirement_terms)
    return core or (requirement_terms & _FALLBACK_CORE_TERMS)


def _requirement_or_alternatives(requirement_text):
    text = _normalize_technical_text(requirement_text)
    parts = [
        part.strip()
        for part in re.split(r"\s*(?:\bor\b|/|或)\s*", text)
        if part.strip()
    ]
    return parts if len(parts) > 1 else []


def _requires_complete_met_coverage(requirement_text):
    if _requirement_or_alternatives(requirement_text):
        return False
    core = _requirement_core_terms(requirement_text)
    terms = _all_match_terms(_semantic_terms(requirement_text))
    return (
        bool(core)
        and len(terms) > 1
        and all(re.fullmatch(r"[a-z0-9_.]+", term) for term in terms)
    )


def _all_match_terms(terms):
    return set(terms) - _CREDENTIAL_TERMS - {"degree"}


def _evidence_core_terms(evidence_type, content):
    evidence_terms = _evidence_search_terms(evidence_type, content)
    core = _core_terms(evidence_terms)
    core.update(evidence_terms & _FALLBACK_CORE_TERMS)
    return core


def _evidence_supports_requirement(requirement_text, evidence_type, content):
    if _is_degree_requirement(requirement_text):
        return (
            evidence_type == "education"
            and _education_supports_degree_requirement(
                requirement_text, content
            )
        )

    alternatives = _requirement_or_alternatives(requirement_text)
    if alternatives:
        return any(
            _evidence_supports_requirement(
                alternative, evidence_type, content
            )
            for alternative in alternatives
        )

    requirement_core = _requirement_core_terms(requirement_text)
    if not requirement_core:
        requirement_terms = _all_match_terms(
            _semantic_terms(requirement_text)
        )
        evidence_terms = _all_match_terms(
            _evidence_search_terms(evidence_type, content)
        )
        return bool(requirement_terms) and requirement_terms <= evidence_terms
    evidence_core = _evidence_core_terms(evidence_type, content)
    if _requires_all_core_terms(requirement_text):
        return requirement_core <= evidence_core
    return bool(requirement_core & evidence_core)


def _canonicalize_evidence_content(evidence_type, value):
    model = _RECORD_MODELS.get(evidence_type)
    if model is not None:
        if not isinstance(value, dict):
            return None
        known_fields = {
            name: value[name] for name in model.model_fields if name in value
        }
        if (
            evidence_type == "experience"
            and "achievements" not in known_fields
            and "achievement" in value
        ):
            achievement = value["achievement"]
            known_fields["achievements"] = (
                [achievement] if isinstance(achievement, str) else achievement
            )
        try:
            validated = model.model_validate(known_fields, strict=True)
        except (ValidationError, TypeError, ValueError):
            return None
        return validated.model_dump(mode="python", exclude_defaults=True)
    if evidence_type == "skill":
        if isinstance(value, str):
            return value.strip() or None
        if not isinstance(value, dict):
            return None
        known_fields = {
            name: value[name]
            for name in SkillRecord.model_fields if name in value
        }
        try:
            validated = SkillRecord.model_validate(known_fields, strict=True)
        except (ValidationError, TypeError, ValueError):
            return None
        return validated.model_dump(mode="python", exclude_defaults=True)
    if evidence_type in ("certificate", "achievement"):
        return value.strip() if isinstance(value, str) and value.strip() else None
    return None


def normalize_requirements(requirements):
    """Return ordered requirements with stable IDs and supported categories only."""
    if isinstance(requirements, dict):
        flattened = []
        for category in CATEGORY_WEIGHTS:
            for item in requirements.get(category) or []:
                if isinstance(item, dict):
                    flattened.append({**item, "category": category})
                else:
                    flattened.append({"category": category, "requirement": item})
        requirements = flattened
    if not isinstance(requirements, list):
        requirements = []

    counters = defaultdict(int)
    used_ids = set()
    normalized = []
    for item in requirements:
        if not isinstance(item, dict):
            continue
        category = _clean_text(item.get("category")).lower()
        if category not in CATEGORY_WEIGHTS:
            continue
        counters[category] += 1
        generated = f"{category}-{counters[category]:03d}"
        requirement_id = _clean_text(item.get("requirement_id")) or generated
        while requirement_id in used_ids:
            counters[category] += 1
            requirement_id = f"{category}-{counters[category]:03d}"
        used_ids.add(requirement_id)
        normalized.append({
            "requirement_id": requirement_id,
            "category": category,
            "requirement": _clean_text(
                item.get("requirement", item.get("text", item.get("name", "")))
            ),
        })
    return normalized


def normalize_jd_requirements(jd_analysis):
    """Map the existing JD shape to the four fixed scoring categories."""
    jd_analysis = jd_analysis if isinstance(jd_analysis, dict) else {}
    category_fields = {
        "hard": ("hard_requirements",),
        "skill": ("bonus_points", "keywords"),
        "business": ("responsibilities",),
        "soft": ("implicit_requirements",),
    }
    normalized = []
    for category, fields in category_fields.items():
        values = []
        for field in fields:
            candidate = jd_analysis.get(field)
            if isinstance(candidate, list) and candidate:
                values = candidate
                break
        seen = set()
        for value in values:
            if isinstance(value, dict):
                text = _clean_text(
                    value.get("requirement", value.get("text", value.get("name", "")))
                )
            else:
                text = _clean_text(value)
            key = _compact_key(text)
            if not text or key in seen:
                continue
            seen.add(key)
            normalized.append({"category": category, "requirement": text})
    return normalize_requirements(normalized)


def normalize_resume_evidence(resume_info, preferences=None):
    """Build a stable catalog whose IDs always point to real resume records."""
    resume_info = resume_info if isinstance(resume_info, dict) else {}
    catalog = []

    def add(evidence_id, source, evidence_type, content):
        if content in (None, "", [], {}):
            return
        item = {
            "evidence_id": evidence_id,
            "source": source,
            "evidence_type": evidence_type,
            "content": content,
        }
        if evidence_type in SCORABLE_EVIDENCE_TYPES:
            item["search_text"] = " ".join(sorted(
                _evidence_search_terms(evidence_type, content)
            ))
        catalog.append(item)

    basic = resume_info.get("basic_info")
    if isinstance(basic, dict):
        for key in ("name", "location", "target_role", "work_authorization"):
            add(
                f"evidence-basic-info-{key.replace('_', '-')}",
                f"basic_info.{key}",
                "basic_info",
                basic.get(key),
            )
    record_types = {
        "education": "education",
        "work_experience": "experience",
        "projects": "project",
    }
    for field, evidence_type in record_types.items():
        values = resume_info.get(field)
        if isinstance(values, list):
            for index, value in enumerate(values, start=1):
                canonical = _canonicalize_evidence_content(evidence_type, value)
                if canonical is None:
                    continue
                add(
                    f"evidence-{evidence_type}-{index:03d}",
                    f"{field}[{index}]",
                    evidence_type,
                    canonical,
                )
    item_types = {
        "skills": "skill",
        "certificates": "certificate",
        "achievements": "achievement",
    }
    for field, evidence_type in item_types.items():
        values = resume_info.get(field)
        if isinstance(values, list):
            for index, value in enumerate(values, start=1):
                canonical = _canonicalize_evidence_content(evidence_type, value)
                if canonical is None:
                    continue
                add(
                    f"evidence-{evidence_type}-{index:03d}",
                    f"{field}[{index}]",
                    evidence_type,
                    canonical,
                )
    add(
        "evidence-raw-summary",
        "raw_summary",
        "raw_summary",
        resume_info.get("raw_summary"),
    )
    add(
        "evidence-user-preferences",
        "user.preferences",
        "preference",
        _clean_text(preferences),
    )
    return catalog


def _evidence_type_from_catalog_item(item):
    source = _clean_text(item.get("source"))
    prefixes = (
        ("basic_info.", "basic_info"),
        ("education[", "education"),
        ("work_experience[", "experience"),
        ("projects[", "project"),
        ("skills[", "skill"),
        ("certificates[", "certificate"),
        ("achievements[", "achievement"),
    )
    evidence_type = next(
        (name for prefix, name in prefixes if source.startswith(prefix)),
        "raw_summary" if source == "raw_summary" else (
            "preference" if source == "user.preferences" else ""
        ),
    )
    declared = _clean_text(item.get("evidence_type")).lower()
    if declared and declared != evidence_type:
        return ""
    if (
        evidence_type in SCORABLE_EVIDENCE_TYPES
        and _canonicalize_evidence_content(
            evidence_type, item.get("content")
        ) is None
    ):
        return ""
    return evidence_type


def _normalize_evidence(evidence):
    if isinstance(evidence, dict):
        rows = []
        for requirement_id, value in evidence.items():
            if isinstance(value, dict):
                rows.append({**value, "requirement_id": requirement_id})
            else:
                rows.append({"requirement_id": requirement_id, "status": value})
        evidence = rows
    if not isinstance(evidence, list):
        return []

    rows = []
    for item in evidence:
        if not isinstance(item, dict):
            continue
        requirement_id = _clean_text(item.get("requirement_id"))
        if not requirement_id:
            continue
        status = _clean_text(item.get("status")).lower()
        if status not in STATUS_FACTORS:
            status = "missing"
        evidence_ids = item.get("evidence_ids")
        if isinstance(evidence_ids, list):
            ids = [_clean_text(value) for value in evidence_ids if _clean_text(value)]
        else:
            evidence_id = _clean_text(item.get("evidence_id"))
            ids = [evidence_id] if evidence_id else []
        if status != "missing" and not ids:
            status = "missing"
        rows.append({
            "requirement_id": requirement_id,
            "status": status,
            "evidence_ids": ids,
        })
    return rows


def _clean_points(value):
    value = Decimal(value)
    return int(value) if value == value.to_integral_value() else float(value)


def _gate_entries(gates):
    if isinstance(gates, list):
        for item in gates:
            if not isinstance(item, dict):
                continue
            name = _clean_text(item.get("name", item.get("gate"))).lower()
            yield name, bool(item.get("required", True)), item.get("met") is True
        return
    if not isinstance(gates, dict):
        return
    for name in ("location", "work_authorization"):
        value = gates.get(name)
        if isinstance(value, dict):
            yield name, bool(value.get("required", True)), value.get("met") is True
        elif isinstance(value, bool):
            yield name, True, value is True
        else:
            required_key = f"{name}_required"
            met_key = f"{name}_met"
            if required_key in gates or met_key in gates:
                yield name, gates.get(required_key) is True, gates.get(met_key) is True


def score_requirements(requirements, evidence, gates):
    """Score normalized requirements with fixed category weights and hard gates."""
    normalized = normalize_requirements(requirements)
    evidence_rows = _normalize_evidence(evidence)
    by_requirement = defaultdict(list)
    for row in evidence_rows:
        by_requirement[row["requirement_id"]].append(row)

    category_counts = defaultdict(int)
    for requirement in normalized:
        category_counts[requirement["category"]] += 1

    total = Decimal("0")
    scored_rows = []
    for requirement in normalized:
        candidates = by_requirement.get(requirement["requirement_id"], [])
        if candidates:
            best_factor = max(STATUS_FACTORS[item["status"]] for item in candidates)
            status = next(
                name for name, factor in STATUS_FACTORS.items()
                if factor == best_factor
            )
            evidence_ids = []
            for item in candidates:
                if STATUS_FACTORS[item["status"]] != best_factor:
                    continue
                for evidence_id in item["evidence_ids"]:
                    if evidence_id not in evidence_ids:
                        evidence_ids.append(evidence_id)
        else:
            status = "missing"
            evidence_ids = []

        category = requirement["category"]
        per_requirement = (
            Decimal(CATEGORY_WEIGHTS[category])
            / Decimal(category_counts[category])
        )
        points = per_requirement * Decimal(str(STATUS_FACTORS[status]))
        total += points
        scored_rows.append({
            "requirement_id": requirement["requirement_id"],
            "status": status,
            "points": None,
            "evidence_ids": evidence_ids,
            "_category": category,
            "_raw_points": points,
        })

    cent = Decimal("0.01")
    for category in CATEGORY_WEIGHTS:
        indexes = [
            index for index, row in enumerate(scored_rows)
            if row["_category"] == category
        ]
        if not indexes:
            continue
        allocated = {
            index: scored_rows[index]["_raw_points"].quantize(
                cent, rounding=ROUND_DOWN
            )
            for index in indexes
        }
        target = sum(
            (scored_rows[index]["_raw_points"] for index in indexes),
            Decimal("0"),
        ).quantize(cent, rounding=ROUND_HALF_UP)
        remaining_cents = int(
            (target - sum(allocated.values(), Decimal("0"))) / cent
        )
        by_remainder = sorted(
            indexes,
            key=lambda index: (
                -(scored_rows[index]["_raw_points"] - allocated[index]),
                index,
            ),
        )
        for offset in range(remaining_cents):
            allocated[by_remainder[offset % len(by_remainder)]] += cent
        for index in indexes:
            scored_rows[index]["points"] = _clean_points(allocated[index])

    for row in scored_rows:
        row.pop("_category")
        row.pop("_raw_points")

    rounded_score = int(
        max(Decimal("0"), min(Decimal("100"), total)).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )
    gate_failures = [
        name for name, required, met in _gate_entries(gates)
        if name in ("location", "work_authorization") and required and not met
    ]
    return {
        "score": max(0, min(100, rounded_score)),
        "eligible": not gate_failures,
        "requirements": scored_rows,
        "gate_failures": gate_failures,
    }


def requirement_ledger_from_match_result(match_result, requirements,
                                         evidence_catalog=None):
    """Return exactly one conservative scoring row per normalized requirement."""
    match_result = match_result if isinstance(match_result, dict) else {}
    requirements = normalize_requirements(requirements)
    requirements_by_id = {
        item["requirement_id"]: item for item in requirements
    }
    known_ids = set(requirements_by_id)
    evidence_by_id = None
    if isinstance(evidence_catalog, list):
        evidence_by_id = {}
        for item in evidence_catalog:
            if not isinstance(item, dict):
                continue
            evidence_id = _clean_text(item.get("evidence_id"))
            if evidence_id and evidence_id not in evidence_by_id:
                evidence_by_id[evidence_id] = item
    by_requirement = {}
    raw_rows = match_result.get("requirement_evidence") or []
    if not isinstance(raw_rows, list):
        raw_rows = []
    row_counts = Counter()
    for item in raw_rows:
        if hasattr(item, "model_dump"):
            item = item.model_dump(mode="python")
        if isinstance(item, dict):
            requirement_id = _clean_text(item.get("requirement_id"))
            if requirement_id in known_ids:
                row_counts[requirement_id] += 1
    for item in raw_rows:
        if hasattr(item, "model_dump"):
            item = item.model_dump(mode="python")
        if not isinstance(item, dict):
            continue
        requirement_id = _clean_text(item.get("requirement_id"))
        if (
            requirement_id not in known_ids
            or row_counts[requirement_id] != 1
            or requirement_id in by_requirement
        ):
            continue
        status = _clean_text(item.get("status")).lower()
        if status not in STATUS_FACTORS:
            status = "missing"
        evidence_ids = []
        evidence_candidates = []
        seen_evidence_ids = set()
        for evidence_id in item.get("evidence_ids") or []:
            evidence_id = _clean_text(evidence_id)
            if not evidence_id or evidence_id in seen_evidence_ids:
                continue
            seen_evidence_ids.add(evidence_id)
            if evidence_by_id is not None:
                catalog_item = evidence_by_id.get(evidence_id)
                if catalog_item is None:
                    continue
                evidence_type = _evidence_type_from_catalog_item(catalog_item)
                if evidence_type not in SCORABLE_EVIDENCE_TYPES:
                    continue
                canonical_content = _canonicalize_evidence_content(
                    evidence_type, catalog_item.get("content")
                )
                evidence_candidates.append((
                    evidence_id, evidence_type, canonical_content,
                ))
            else:
                evidence_ids.append(evidence_id)
        if evidence_by_id is not None:
            requirement_text = requirements_by_id[requirement_id]["requirement"]
            requires_complete_coverage = (
                not _is_degree_requirement(requirement_text)
                and (
                    _requires_all_core_terms(requirement_text)
                    or (
                        status == "met"
                        and _requires_complete_met_coverage(requirement_text)
                    )
                )
            )
            if requires_complete_coverage:
                requirement_core = _requirement_core_terms(requirement_text)
                requirement_terms = _all_match_terms(
                    _semantic_terms(requirement_text)
                )
                requirement_context = (
                    requirement_terms & _GENERIC_MATCH_TERMS
                )
                combined_core = set()
                combined_terms = set()
                relevant_ids = []
                for evidence_id, evidence_type, content in evidence_candidates:
                    item_core = _evidence_core_terms(evidence_type, content)
                    item_terms = _all_match_terms(
                        _evidence_search_terms(evidence_type, content)
                    )
                    combined_core.update(item_core)
                    combined_terms.update(item_terms)
                    if (
                        requirement_core & item_core
                        or requirement_context & item_terms
                    ):
                        relevant_ids.append(evidence_id)
                context_matches = (
                    not requirement_context
                    or bool(requirement_context & combined_terms)
                )
                if (
                    requirement_core
                    and requirement_core <= combined_core
                    and context_matches
                ):
                    evidence_ids = relevant_ids
            else:
                evidence_ids = [
                    evidence_id
                    for evidence_id, evidence_type, content in evidence_candidates
                    if _evidence_supports_requirement(
                        requirement_text, evidence_type, content
                    )
                ]
        if status != "missing" and not evidence_ids:
            status = "missing"
        if status == "missing":
            evidence_ids = []
        by_requirement[requirement_id] = {
            "requirement_id": requirement_id,
            "status": status,
            "evidence_ids": evidence_ids,
        }

    return [
        by_requirement.get(requirement["requirement_id"], {
            "requirement_id": requirement["requirement_id"],
            "status": "missing",
            "evidence_ids": [],
        })
        for requirement in requirements
    ]


def evidence_from_match_result(match_result, requirements, evidence_catalog=None):
    """Backward-compatible name for the exhaustive scoring ledger."""
    return requirement_ledger_from_match_result(
        match_result, requirements, evidence_catalog=evidence_catalog
    )


def _normalized_exact_value(value):
    return " ".join(_clean_text(value).casefold().split())


def gates_from_jd(jd_analysis, resume_info=None):
    """Resolve only explicit JD gates against explicit structured resume facts."""
    jd_analysis = jd_analysis if isinstance(jd_analysis, dict) else {}
    resume_info = resume_info if isinstance(resume_info, dict) else {}
    basic_info = resume_info.get("basic_info")
    basic_info = basic_info if isinstance(basic_info, dict) else {}
    raw_gates = jd_analysis.get("gates")
    if not isinstance(raw_gates, dict):
        return {}

    resolved = {}
    for name in ("location", "work_authorization"):
        gate = raw_gates.get(name)
        if not isinstance(gate, dict):
            continue
        required = gate.get("required") is True
        if name == "location":
            accepted = gate.get("accepted_values")
            accepted = accepted if isinstance(accepted, list) else []
            accepted_keys = {
                _normalized_exact_value(value)
                for value in accepted
                if _normalized_exact_value(value)
            }
            candidate = _normalized_exact_value(basic_info.get("location"))
            met = bool(candidate and candidate in accepted_keys)
        else:
            met = basic_info.get("work_authorization") is True
        resolved[name] = {"required": required, "met": met}
    return resolved
