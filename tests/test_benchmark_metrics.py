import pytest

from evog.benchmark_metrics import research_compare, research_metrics, token_total
from evog.errors import ContractError


def row(key, passed, status="completed", **costs):
    return {"episode_id": key, "status": status, "passed": passed, **costs}


def test_planned_denominator_pending_and_completed_only_efficiency():
    rows = [
        row("a", True, answer_seconds=2, tool_calls=2, answer_usage={"total_tokens": 10}),
        row("b", False, answer_seconds=4, tool_calls=4, answer_usage={"total_tokens": 20}),
        row("c", None, "provider_failed", answer_seconds=100, tool_calls=100),
        row("d", None, answer_seconds=6, tool_calls=6),
    ]
    result = research_metrics(rows, list("abcde"))
    assert result["accuracy"] == 0.2
    assert result["missing"] == result["system_failed"] == result["judge_pending"] == 1
    assert not result["report_complete"]
    assert result["efficiency"]["answer_seconds"]["mean"] == 4
    assert result["efficiency"]["answer_tokens"] == {
        "mean": 15,
        "measured_count": 2,
        "missing_count": 1,
    }
    final = research_metrics(rows[:3], list("abc"))
    assert final["report_complete"] and final["accuracy"] == pytest.approx(1 / 3)


def test_research_transitions_rates_and_undefined_denominators():
    before = [row("a", False), row("b", False), row("c", True), row("d", True)]
    after = [row("a", True), row("b", False), row("c", False), row("d", True)]
    result = research_compare(before, after)
    assert result["gain"] == result["regression"] == result["retention"] == 0.5
    assert result["paired"] == 4
    only_pass = research_compare([row("a", True)], [row("a", True)])
    assert only_pass["gain"] is None and only_pass["retention"] == 1
    failed = research_compare([row("a", None, "provider_failed")], [row("a", True)])
    assert failed["gain"] == 1 and failed["system_failure_pairs"] == 1
    pending = research_compare([row("a", None)], [row("a", True)])
    assert pending["excluded"] == 1 and pending["regression"] is None


def test_costs_do_not_double_count_cache_and_metrics_reject_extra_trials():
    assert (
        token_total(
            {"prompt_tokens": 10, "completion_tokens": 2, "cached_tokens": 8, "total_tokens": 12}
        )
        == 12
    )
    assert token_total({"prompt_tokens": 10, "completion_tokens": 2}) == 12
    assert token_total({}) is None
    with pytest.raises(ContractError):
        research_metrics([row("a", True, trial_index=2)], ["a"])
    with pytest.raises(ContractError):
        research_metrics([row("a", True), row("a", False)], ["a"])
