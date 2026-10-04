"""Deployment settings are fixed kernel inputs, separate from evolvable files."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr

from evog.models import Record


class Settings(Record):
    base_url: str = ""
    model: str = ""
    api_key: SecretStr = SecretStr("")
    reasoning_effort: Literal["none", "minimal", "low", "medium", "high", "xhigh"] = "none"
    max_turns: int = Field(default=60, ge=3, le=60)
    # Twenty individual tool requests is the fixed per-question budget.
    # ``max_tool_rounds`` remains a separate guard for batched calls, but it
    # must not expand this cap.
    max_tool_calls: int = Field(default=20, ge=1, le=20)
    max_tool_rounds: int = Field(default=20, ge=1, le=100)
    max_parallel_tool_calls: int = Field(default=3, ge=1, le=16)
    repeated_tool_limit: int = Field(default=3, ge=2, le=10)
    max_tool_output_chars: int = Field(default=12000, ge=1024, le=60000)
    max_output_tokens: int = Field(default=8192, ge=256, le=32000)
    request_timeout: float = Field(default=120, ge=1, le=600)
    call_timeout_seconds: float = Field(default=60, ge=1, le=600)
    call_attempts: int = Field(default=3, ge=1, le=5)
    question_timeout_seconds: float = Field(default=500, ge=1, le=500)
    reflection_timeout_seconds: float = Field(default=60, ge=1, le=120)
    reflection_max_output_tokens: int = Field(default=8192, ge=256, le=16000)
    confidence_threshold: float = Field(default=0.5, ge=0, le=1)
    analysis_max_runs: int = Field(default=30, ge=1, le=200)
    analysis_concurrency: int = Field(default=1, ge=1, le=16)
    analysis_turns: int = Field(default=15, ge=2, le=30)
    analysis_timeout_seconds: float = Field(default=900, ge=1, le=3600)
    analysis_max_output_tokens: int = Field(default=20000, ge=256, le=32000)
    analysis_max_context_chars: int = Field(default=800000, ge=10000, le=1000000)
    analysis_min_valid: int = Field(default=10, ge=1, le=200)
    analysis_min_coverage: float = Field(default=0.7, gt=0, le=1)
    finding_min_support: int = Field(default=2, ge=2, le=20)
    synthesis_timeout_seconds: float = Field(default=300, ge=1, le=3600)
    synthesis_max_output_tokens: int = Field(default=20000, ge=256, le=32000)
    synthesis_max_context_chars: int = Field(default=800000, ge=10000, le=1000000)
    evolution_turns: int = Field(default=15, ge=2, le=50)
    evolution_timeout_seconds: float = Field(default=900, ge=1, le=3600)
    evolution_max_output_tokens: int = Field(default=32000, ge=256, le=32000)
    evolution_max_context_chars: int = Field(default=800000, ge=10000, le=1000000)
    candidate_acceptance: Literal["net_improvement", "no_regression"] = "net_improvement"
    benchmark_concurrency: int = Field(default=1, ge=1, le=16)
    max_context_chars: int = Field(default=800000, ge=10000, le=1000000)
    context_trim_ratio: float = Field(default=0.8, gt=0, le=1)

    @classmethod
    def load(cls, path: Path | None = None) -> Settings:
        values = {}
        if path:
            values = tomllib.loads(path.read_text(encoding="utf-8"))
        for field in ("base_url", "model", "api_key"):
            value = os.environ.get(f"EVOG_{field.upper()}")
            if value is not None:
                values[field] = value
        return cls.model_validate(values)
