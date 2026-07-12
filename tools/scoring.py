"""Deterministic requirement scoring for resume/JD matching."""

import re
from collections import defaultdict
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


def _clean_text(value):
    return str(value or "").strip()


def _compact_key(value):
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", _clean_text(value).lower())


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
        catalog.append({
            "evidence_id": evidence_id,
            "source": source,
            "evidence_type": evidence_type,
            "content": content,
        })

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
    known_ids = {item["requirement_id"] for item in requirements}
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
    for item in raw_rows:
        if hasattr(item, "model_dump"):
            item = item.model_dump(mode="python")
        if not isinstance(item, dict):
            continue
        requirement_id = _clean_text(item.get("requirement_id"))
        if requirement_id not in known_ids or requirement_id in by_requirement:
            continue
        status = _clean_text(item.get("status")).lower()
        if status not in STATUS_FACTORS:
            status = "missing"
        evidence_ids = []
        for evidence_id in item.get("evidence_ids") or []:
            evidence_id = _clean_text(evidence_id)
            if not evidence_id or evidence_id in evidence_ids:
                continue
            if evidence_by_id is not None:
                catalog_item = evidence_by_id.get(evidence_id)
                if catalog_item is None:
                    continue
                evidence_type = _evidence_type_from_catalog_item(catalog_item)
                if evidence_type not in SCORABLE_EVIDENCE_TYPES:
                    continue
            evidence_ids.append(evidence_id)
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
