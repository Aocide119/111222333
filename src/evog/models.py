"""Typed contracts shared by commands, tools, traces, and evolution."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Message(Record):
    group_id: str = Field(min_length=1, max_length=128, pattern=r"^[^\s/\\\x00-\x1f]+$")
    message_id: str = Field(min_length=1, max_length=128, pattern=r"^[^\s/\\\x00-\x1f]+$")
    sender: str = Field(min_length=1, max_length=256)
    timestamp: datetime
    text: str = Field(min_length=1, max_length=20000)
    reply_to: str | None = Field(default=None, max_length=128, pattern=r"^[^\s/\\\x00-\x1f]+$")
    metadata: dict[str, str] = Field(default_factory=dict, max_length=32)

    @field_validator("sender", "text")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("sender and text must not be blank")
        return value

    @field_validator("metadata")
    @classmethod
    def bounded_metadata(cls, value: dict[str, str]) -> dict[str, str]:
        if any(len(key) > 128 or len(item) > 4000 for key, item in value.items()):
            raise ValueError("metadata keys and values exceed their size limits")
        if len(str(value)) > 16000:
            raise ValueError("metadata exceeds its total size limit")
        return value

    @field_validator("timestamp")
    @classmethod
    def aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must include a timezone")
        return value

    @property
    def ref(self) -> str:
        return f"{self.group_id}/{self.message_id}"


class AnswerDraft(Record):
    text: str = Field(min_length=1, max_length=12000)
    confidence: float = Field(ge=0, le=1, strict=True, allow_inf_nan=False)
    status: Literal["complete", "partial", "insufficient"]
    citations: list[str] = Field(default_factory=list, max_length=64)


class Reflection(Record):
    searched: list[str]
    evidence_found: str
    unresolved: list[str]
    limitation: str


class Answer(AnswerDraft):
    run_id: str
    revision_id: str
    reflection: Reflection | None = None
    answer_bias: str = ""
    raw_response: str = ""


class Feedback(Record):
    run_id: str
    outcome: Literal["accepted", "rejected"]
    source: str = Field(min_length=1, max_length=256)
    # No reference answers are stored in the feedback contract.


class Event(Record):
    index: int
    kind: Literal["model", "tool", "answer", "reflection", "error", "context_trim", "budget"]
    data: dict[str, Any]


class EvidenceRef(Record):
    run_id: str
    event_index: int = Field(ge=0)
    field_path: str = ""
    start_char: int = Field(default=0, ge=0)
    end_char: int = Field(ge=0)

    @model_validator(mode="after")
    def nonempty_range(self) -> EvidenceRef:
        if self.end_char <= self.start_char:
            raise ValueError("Evidence must cover a nonempty character range")
        return self


class Diagnosis(Record):
    run_id: str
    query_type: str = Field(min_length=1, max_length=100)
    query_family: Literal[
        "fact_lookup", "temporal", "attribution", "multi_step", "aggregation", "preference", "other"
    ] = "other"
    category: Literal[
        "retrieval",
        "attribution",
        "temporal",
        "interpretation",
        "delivery",
        "budget",
        "tool",
        "uncertainty",
        "unknown",
    ]
    earliest_break: str = Field(min_length=1, max_length=1600)
    cause: str = Field(min_length=1, max_length=3000)
    condensed_rationale: str = Field(min_length=1, max_length=1200)
    actionable_implication: str = Field(min_length=1, max_length=1600)
    evidence: list[EvidenceRef] = Field(min_length=1)
    uncertainty: str = Field(min_length=1, max_length=1600)

    @field_validator("query_type")
    @classmethod
    def nonblank_query_type(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query_type must contain a semantic query label")
        return value


class Finding(Record):
    id: str = Field(min_length=1)
    query_types: list[str]
    pattern: str
    suggested_change: str
    evidence: list[EvidenceRef] = Field(min_length=1)
    counterevidence: str
    uncertainty: str
    purpose: Literal["error_repair", "uncertainty_calibration"] = "error_repair"
    support_kind: Literal["repeated", "isolated"] = "repeated"


class QueryBucket(Record):
    """An open query-type bucket, including its original labels and inspected inputs."""

    id: str
    normalized_label: str
    original_labels: list[str]
    diagnosis_run_ids: list[str]
    findings: list[Finding] = Field(default_factory=list)


class AnalysisReport(Record):
    id: str
    revision_id: str
    coverage: dict[str, int]
    selected_run_ids: list[str]
    control_run_ids: list[str]
    diagnoses: list[Diagnosis]
    findings: list[Finding]
    incomplete: dict[str, str]
    failure_run_ids: list[str] = Field(default_factory=list)
    calibration_run_ids: list[str] = Field(default_factory=list)
    eligible_for_revision: bool = True
    schema_version: str = "evog.analysis.v1"
    query_type_normalization: str | None = None
    query_buckets: list[QueryBucket] = Field(default_factory=list)


Interface = Literal["Representation", "Operation", "Policy", "Intervention"]


class Change(Record):
    interface: Interface
    path: str
    content: str = Field(min_length=1, max_length=40000)
    finding_ids: list[str] = Field(min_length=1)
    rationale: str
    expected_effect: str
    regression_risk: str
    validation: str
    predicted_fix_runs: list[str] = Field(default_factory=list, max_length=200)
    risk_runs: list[str] = Field(default_factory=list, max_length=200)


class PlanDraft(Record):
    summary: str
    changes: list[Change] = Field(max_length=12)


class EvolutionPlan(PlanDraft):
    id: str
    analysis_id: str
    parent_revision_id: str


class AppliedRevision(Record):
    revision_id: str
    parent_revision_id: str
    plan_id: str
    validation: Literal["structural", "business"]
