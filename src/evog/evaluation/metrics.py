"""Versioned single-trial research metrics."""

from __future__ import annotations

from typing import Any

from evog.core.errors import ContractError
from evog.core.usage import token_components, token_total

SYSTEM_FAILURES = {
    "budget_exhausted",
    "contract_failed",
    "provider_failed",
    "tool_failed",
    "incident",
    "failed",
}


def research_metrics(results: list[dict], planned_ids: list[str]) -> dict[str, Any]:
    """Accuracy counts terminal system failures; pending judgements block finalization.

    Efficiency uses completed answers, whether judged correct or wrong. Each stage
    reports its own measured coverage; missing cost records are never treated as zero.
    """
    if len(set(planned_ids)) != len(planned_ids):
        raise ContractError("Research metrics require unique planned question IDs")
    rows = {}
    for row in results:
        key = row["episode_id"]
        if key not in planned_ids or key in rows or row.get("trial_index", 1) != 1:
            raise ContractError("Research metrics require one trial per planned question")
        if row.get("passed") is not None and type(row["passed"]) is not bool:
            raise ContractError("Invalid research correctness verdict")
        rows[key] = row
    completed = [r for r in rows.values() if r.get("status") == "completed"]
    failed = [r for r in rows.values() if r.get("status") in SYSTEM_FAILURES]
    scored = [r for r in completed if r.get("passed") is not None]
    passed = sum(r["passed"] is True for r in scored)
    pending = len(completed) - len(scored)
    unknown = len(rows) - len(completed) - len(failed)
    missing = len(planned_ids) - len(rows)

    def measured(values: list[int | float | None]) -> dict[str, Any]:
        present = [v for v in values if v is not None]
        return {
            "mean": sum(present) / len(present) if present else None,
            "measured_count": len(present),
            "missing_count": len(values) - len(present),
        }

    efficiency = {
        "population": "completed_answers_correct_or_wrong",
        "completed_count": len(completed),
        "system_failure_count": len(failed),
        "tool_calls": measured([r.get("tool_calls") for r in completed]),
        "token_accounting": "uncached_input + cached_input + output",
    }
    for stage in ("answer", "reflection", "judge"):
        efficiency[f"{stage}_seconds"] = measured([r.get(f"{stage}_seconds") for r in completed])
        tokens = [
            r[f"{stage}_tokens"]
            if f"{stage}_tokens" in r
            else token_total(r.get(f"{stage}_usage", {}))
            for r in completed
        ]
        efficiency[f"{stage}_tokens"] = measured(tokens)
        components = [
            token_components(r.get(f"{stage}_usage", {}) if total is not None else {})
            for r, total in zip(completed, tokens, strict=True)
        ]
        efficiency[f"{stage}_token_breakdown"] = {
            key: measured([value[key] for value in components])
            for key in ("uncached_input", "cached_input", "output")
        }
    categories = {}
    for kind in sorted({r.get("question_type", "unknown") for r in rows.values()}):
        subset = [r for r in rows.values() if r.get("question_type", "unknown") == kind]
        # Category denominators require planned metadata. Until supplied, expose counts only.
        categories[kind] = {
            "observed": len(subset),
            "passed": sum(
                r.get("status") == "completed" and r.get("passed") is True for r in subset
            ),
        }
    return {
        "schema": "evog.research-metrics.v1",
        "metric_unit": "question_single_trial",
        "planned": len(planned_ids),
        "observed": len(rows),
        "missing": missing,
        "answered": len(completed),
        "scored": len(scored),
        "passed": passed,
        "incorrect": sum(r["passed"] is False for r in scored),
        "system_failed": len(failed),
        "judge_pending": pending,
        "nonterminal_or_unknown": unknown,
        "report_complete": not (missing or pending or unknown),
        "accuracy": passed / len(planned_ids) if planned_ids else None,
        "accuracy_provisional": bool(missing or pending or unknown),
        "categories": categories,
        "efficiency": efficiency,
    }


def research_compare(before: list[dict], after: list[dict]) -> dict[str, Any]:
    """Pair single trials. Terminal system failures count as unsuccessful.

    Pending judges, missing records and cancellations are excluded explicitly.
    System-failure transitions are also exposed separately for interpretation.
    """

    def index(rows: list[dict]) -> dict[str, dict]:
        indexed = {r["episode_id"]: r for r in rows}
        if len(indexed) != len(rows) or any(r.get("trial_index", 1) != 1 for r in rows):
            raise ContractError("Research comparison requires unique single trials")
        if any(r.get("passed") is not None and type(r["passed"]) is not bool for r in rows):
            raise ContractError("Invalid research correctness verdict")
        return indexed

    left, right = index(before), index(after)
    counts = {k: 0 for k in ("fail_to_pass", "pass_to_fail", "pass_to_pass", "fail_to_fail")}
    excluded = system_pairs = 0

    def outcome(row: dict | None) -> bool | None:
        if row is None:
            return None
        if row.get("status") in SYSTEM_FAILURES:
            return False
        return row.get("passed") if row.get("status") == "completed" else None

    for key in left.keys() | right.keys():
        a, b = outcome(left.get(key)), outcome(right.get(key))
        if a is None or b is None:
            excluded += 1
            continue
        counts[("pass" if a else "fail") + "_to_" + ("pass" if b else "fail")] += 1
        system_pairs += any(r.get("status") in SYSTEM_FAILURES for r in (left[key], right[key]))
    wrong = counts["fail_to_pass"] + counts["fail_to_fail"]
    correct = counts["pass_to_pass"] + counts["pass_to_fail"]
    regression = counts["pass_to_fail"] / correct if correct else None
    return {
        "schema": "evog.research-comparison.v1",
        **counts,
        "paired": sum(counts.values()),
        "excluded": excluded,
        "system_failure_pairs": system_pairs,
        "system_failure_policy": "terminal_system_failures_are_unsuccessful",
        "gain": counts["fail_to_pass"] / wrong if wrong else None,
        "regression": regression,
        "retention": 1 - regression if regression is not None else None,
    }
