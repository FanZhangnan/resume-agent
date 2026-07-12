"""Strict output contracts shared by tools and delivery checks."""

from typing import Any, Literal

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
