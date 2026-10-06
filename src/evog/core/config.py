"""Deployment settings are fixed kernel inputs, separate from evolvable files."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr

from evog.core.models import Record


class Settings(Record):
    base_url: str = ""
    model: str = ""
    api_key: SecretStr = SecretStr("")
    reasoning_effort: Literal["none", "minimal", "low", "medium", "high", "xhigh"] = "none"
    temperature: float | None = Field(default=None, ge=0, le=2)
    sampling_seed: int | None = Field(default=None, ge=0)
    judge_base_url: str = ""
    judge_model: str = ""
    judge_api_key: SecretStr = SecretStr("")
    judge_reasoning_effort: Literal["none", "minimal", "low", "medium", "high", "xhigh"] = "none"
    judge_temperature: float | None = Field(default=None, ge=0, le=2)
    judge_max_output_tokens: int = Field(default=8192, ge=256, le=32000)
    judge_timeout_seconds: float = Field(default=120, ge=1, le=3600)
    judge_concurrency: int = Field(default=4, ge=1, le=16)
    judge_call_attempts: int = Field(default=3, ge=1, le=5)
    judge_retry_backoff_seconds: float = Field(default=10, ge=0, le=60)
    max_turns: int = Field(default=60, ge=3, le=60)
    final_answer_within_turn_budget: bool = False
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
    call_retry_backoff_seconds: float = Field(default=0.5, ge=0, le=60)
    question_timeout_seconds: float = Field(default=500, ge=1, le=500)
    reflection_timeout_seconds: float = Field(default=60, ge=1, le=120)
    reflection_max_output_tokens: int = Field(default=8192, ge=256, le=16000)
    reflection_temperature: float | None = Field(default=None, ge=0, le=2)
    confidence_threshold: float = Field(default=0.5, ge=0, le=1)
    analysis_max_runs: int | None = Field(default=30, ge=1)
    analysis_all_eligible: bool = False
    analysis_concurrency: int = Field(default=1, ge=1, le=16)
    analysis_turns: int = Field(default=15, ge=2, le=30)
    analysis_timeout_seconds: float = Field(default=900, ge=1, le=3600)
    analysis_max_output_tokens: int = Field(default=20000, ge=256, le=32000)
    analysis_temperature: float | None = Field(default=None, ge=0, le=2)
    analysis_retry_backoff_seconds: float = Field(default=10, ge=0, le=60)
    analysis_max_context_chars: int = Field(default=800000, ge=10000, le=1000000)
    analysis_min_valid: int = Field(default=10, ge=1, le=200)
    analysis_min_coverage: float = Field(default=0.7, gt=0, le=1)
    finding_min_support: int = Field(default=2, ge=2, le=20)
    synthesis_timeout_seconds: float = Field(default=300, ge=1, le=3600)
    synthesis_max_output_tokens: int = Field(default=20000, ge=256, le=32000)
    synthesis_temperature: float | None = Field(default=None, ge=0, le=2)
    synthesis_max_context_chars: int = Field(default=800000, ge=10000, le=1000000)
    evolution_turns: int = Field(default=15, ge=2, le=500)
    evolution_timeout_seconds: float = Field(default=900, ge=1, le=3600)
    evolution_max_output_tokens: int = Field(default=32000, ge=256, le=32000)
    evolution_temperature: float | None = Field(default=0.7, ge=0, le=2)
    evolution_max_context_chars: int = Field(default=800000, ge=10000, le=1000000)
    candidate_acceptance: Literal["net_improvement", "no_regression"] = "net_improvement"
    benchmark_concurrency: int = Field(default=1, ge=1, le=16)
    max_context_chars: int = Field(default=800000, ge=10000, le=1000000)
    context_trim_ratio: float = Field(default=0.8, gt=0, le=1)

    @classmethod
    def load(
        cls, path: Path | None = None, *, profile: Literal["product", "paper"] = "product"
    ) -> Settings:
        values = cls.paper_defaults() if profile == "paper" else {}
        if path:
            values.update(tomllib.loads(path.read_text(encoding="utf-8")))
        for field in (
            "base_url",
            "model",
            "api_key",
            "judge_base_url",
            "judge_model",
            "judge_api_key",
        ):
            value = os.environ.get(f"EVOG_{field.upper()}")
            if value is not None:
                values[field] = value
        return cls.model_validate(values)

    @staticmethod
    def paper_defaults() -> dict:
        """Published stage settings; unspecified values retain documented engineering defaults.

        Context caps remain character estimates, not verified 200,000-token windows.
        Dataset split and evaluation rounds are campaign inputs, not evolvable settings.
        """
        return {
            "max_turns": 60,
            "final_answer_within_turn_budget": True,
            "max_tool_calls": 20,
            "max_output_tokens": 8192,
            "question_timeout_seconds": 500,
            "analysis_all_eligible": True,
            "analysis_concurrency": 8,
            "analysis_timeout_seconds": 900,
            "call_attempts": 3,
            "analysis_retry_backoff_seconds": 10,
            "evolution_turns": 500,
            "evolution_max_output_tokens": 32000,
            "evolution_temperature": 0.7,
            "context_trim_ratio": 0.75,
            "judge_model": "gpt-5.6-luna",
            "judge_concurrency": 4,
            "judge_call_attempts": 3,
            "benchmark_concurrency": 6,
        }
