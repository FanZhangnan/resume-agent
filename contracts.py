"""Strict output contracts shared by tools and delivery checks."""

from typing import Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class _StrictResult(BaseModel):
    model_config = ConfigDict(
        strict=True,
        extra="forbid",
        revalidate_instances="always",
    )


class RequirementEvidence(_StrictResult):
    requirement_id: str
    status: Literal["met", "under_evidenced", "missing"]
    evidence_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_evidence_ids(self):
        if self.status in ("met", "under_evidenced") and not self.evidence_ids:
            raise ValueError("non-missing status requires evidence_ids")
        if self.status == "missing" and self.evidence_ids:
            raise ValueError("missing status cannot reference evidence_ids")
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise ValueError("evidence_ids must be unique within a row")
        return self


def _has_semantic_text(*values):
    for value in values:
        if isinstance(value, str) and value.strip():
            return True
        if isinstance(value, list) and any(
            isinstance(item, str) and item.strip() for item in value
        ):
            return True
    return False


class ResumeBasicInfo(_StrictResult):
    name: str = ""
    phone: str = ""
    email: str = ""
    location: str = ""
    target_role: str = ""
    work_authorization: bool = False


class EducationRecord(_StrictResult):
    school: str = ""
    degree: str = ""
    major: str = ""
    start_date: str = ""
    end_date: str = ""
    details: str = ""

    @model_validator(mode="after")
    def validate_semantic_content(self):
        if not _has_semantic_text(
            self.school, self.degree, self.major, self.details
        ):
            raise ValueError("education record requires semantic content")
        return self


class WorkExperienceRecord(_StrictResult):
    company: str = ""
    title: str = ""
    start_date: str = ""
    end_date: str = ""
    responsibilities: list[str] = Field(default_factory=list)
    achievements: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_semantic_content(self):
        if any(
            not item.strip()
            for item in self.responsibilities + self.achievements
        ):
            raise ValueError("work experience evidence cannot contain blanks")
        if not _has_semantic_text(
            self.company, self.title, self.responsibilities, self.achievements
        ):
            raise ValueError("work experience record requires semantic content")
        return self


class ProjectRecord(_StrictResult):
    name: str = ""
    role: str = ""
    start_date: str = ""
    end_date: str = ""
    description: str = ""
    achievements: list[str] = Field(default_factory=list)
    technologies: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_semantic_content(self):
        if any(
            not item.strip() for item in self.achievements + self.technologies
        ):
            raise ValueError("project evidence cannot contain blanks")
        if not _has_semantic_text(
            self.name,
            self.role,
            self.description,
            self.achievements,
            self.technologies,
        ):
            raise ValueError("project record requires semantic content")
        return self


class SkillRecord(_StrictResult):
    name: str = ""
    category: str = ""
    level: str = ""
    details: str = ""

    @model_validator(mode="after")
    def validate_name(self):
        if not self.name.strip():
            raise ValueError("skill record requires a non-empty name")
        return self


class ResumeInfo(_StrictResult):
    basic_info: ResumeBasicInfo = Field(default_factory=ResumeBasicInfo)
    education: list[EducationRecord] = Field(default_factory=list)
    work_experience: list[WorkExperienceRecord] = Field(default_factory=list)
    projects: list[ProjectRecord] = Field(default_factory=list)
    skills: list[Union[str, SkillRecord]] = Field(default_factory=list)
    certificates: list[str] = Field(default_factory=list)
    achievements: list[str] = Field(default_factory=list)
    potential_issues: list[str] = Field(default_factory=list)
    raw_summary: str = ""

    @model_validator(mode="after")
    def validate_string_items(self):
        for item in self.skills:
            if isinstance(item, str) and not item.strip():
                raise ValueError("skills cannot contain blank strings")
        for field in ("certificates", "achievements", "potential_issues"):
            if any(not item.strip() for item in getattr(self, field)):
                raise ValueError(f"{field} cannot contain blank strings")
        has_basic_info = _has_semantic_text(
            self.basic_info.name,
            self.basic_info.phone,
            self.basic_info.email,
            self.basic_info.location,
            self.basic_info.target_role,
        )
        has_evidence = any((
            self.education,
            self.work_experience,
            self.projects,
            self.skills,
            self.certificates,
            self.achievements,
        ))
        if not has_basic_info and not has_evidence:
            raise ValueError(
                "resume requires basic semantic information or evidence"
            )
        return self


class JDGateRequirement(_StrictResult):
    required: bool
    accepted_values: list[str]


class JDGates(_StrictResult):
    location: JDGateRequirement
    work_authorization: JDGateRequirement


class JDAnalysis(_StrictResult):
    job_title: str = ""
    company_or_industry: str = ""
    hard_requirements: list[str] = Field(default_factory=list)
    bonus_points: list[str] = Field(default_factory=list)
    implicit_requirements: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    responsibilities: list[str] = Field(default_factory=list)
    risk_points: list[str] = Field(default_factory=list)
    raw_summary: str = ""
    gates: JDGates

    @model_validator(mode="after")
    def validate_semantic_content(self):
        if not _has_semantic_text(
            self.job_title,
            self.responsibilities,
            self.hard_requirements,
            self.bonus_points,
            self.keywords,
            self.raw_summary,
        ):
            raise ValueError("JD analysis requires semantic job content")
        return self


class MatchResult(_StrictResult):
    score: int = Field(ge=0, le=100)
    score_reason: str = ""
    high_matches: list[Any] = Field(default_factory=list)
    partial_matches: list[Any] = Field(default_factory=list)
    missing_requirements: list[Any] = Field(default_factory=list)
    redundant_or_irrelevant: list[Any] = Field(default_factory=list)
    risks: list[Any] = Field(default_factory=list)
    recommendation: str = ""
    requirement_evidence: list[RequirementEvidence] = Field(default_factory=list)
    eligible: bool = True
    requirement_scores: list[Any] = Field(default_factory=list)
    gate_failures: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_unique_requirement_evidence(self):
        requirement_ids = [
            row.requirement_id.strip() for row in self.requirement_evidence
        ]
        if len(requirement_ids) != len(set(requirement_ids)):
            raise ValueError(
                "requirement_evidence must contain one row per requirement_id"
            )
        return self


def optimized_resume_struct_is_usable(value):
    """Accept only a renderable structure with at least one factual record."""
    if not isinstance(value, dict) or not isinstance(value.get("basic_info"), dict):
        return False
    has_record = False
    for field in ("education", "experience", "projects"):
        records = value.get(field, [])
        if not isinstance(records, list):
            return False
        for record in records:
            if not isinstance(record, dict) or not _has_nested_semantic_content(record):
                return False
            has_record = True
    return has_record


def _has_nested_semantic_content(value):
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return any(_has_nested_semantic_content(item) for item in value)
    if isinstance(value, dict):
        return any(_has_nested_semantic_content(item) for item in value.values())
    return False


class SuggestionResult(_StrictResult):
    generation_mode: Literal["llm", "conservative_fallback"] = "llm"
    overall_strategy: str = ""
    rewrite_suggestions: list[Any] = Field(default_factory=list)
    star_rewrites: list[Any] = Field(default_factory=list)
    keyword_injection: list[Any] = Field(default_factory=list)
    honesty_boundaries: list[Any] = Field(default_factory=list)
    optimized_resume: str = ""
    optimized_resume_struct: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_optimized_resume(self):
        if not (
            self.optimized_resume.strip()
            or optimized_resume_struct_is_usable(self.optimized_resume_struct)
        ):
            raise ValueError(
                "suggestions require optimized_resume text or a usable structure"
            )
        return self


def suggestions_are_usable(value):
    """Return True only for a strict suggestion payload with deliverable content."""
    if isinstance(value, SuggestionResult):
        value = value.model_dump(mode="python")
    if not isinstance(value, dict):
        return False
    try:
        SuggestionResult.model_validate(value, strict=True)
    except (ValidationError, TypeError, ValueError):
        return False
    return True


class VerificationResult(_StrictResult):
    passed: bool = False
    overall_assessment: str = ""
    overstatement_issues: list[Any] = Field(default_factory=list)
    fabrication_risks: list[Any] = Field(default_factory=list)
    logic_issues: list[Any] = Field(default_factory=list)
    match_authenticity_issues: list[Any] = Field(default_factory=list)
    required_fixes: list[Any] = Field(default_factory=list)
    safe_to_deliver: bool = False


def verification_is_deliverable(value):
    """Return True only when a complete, strict verification clears all gates."""
    if isinstance(value, VerificationResult):
        value = value.model_dump(mode="python")
    if not isinstance(value, dict):
        return False
    try:
        verification = VerificationResult.model_validate(value, strict=True)
    except (ValidationError, TypeError, ValueError):
        return False
    return (
        verification.passed is True
        and verification.safe_to_deliver is True
        and verification.required_fixes == []
    )


def delivery_is_complete(verification, suggestions):
    """Require both strict verification gates and a usable optimized resume."""
    return (
        verification_is_deliverable(verification)
        and suggestions_are_usable(suggestions)
    )
