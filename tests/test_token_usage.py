import httpx
import pytest

from evog.core.config import Settings
from evog.core.providers import ChatProvider
from evog.core.usage import aggregate_usage, normalize_usage, token_components, token_total
from evog.evaluation.metrics import research_metrics
from evog.evaluation.runner import _measured_tokens


@pytest.mark.parametrize(
    "usage,expected,cached,uncached",
    [
        (
            {
                "prompt_tokens": 1961,
                "completion_tokens": 38,
                "total_tokens": 1999,
                "prompt_tokens_details": {"cached_tokens": 1792},
            },
            1999,
            1792,
            169,
        ),
        (
            {
                "prompt_tokens": 100,
                "completion_tokens": 12,
                "prompt_cache_hit_tokens": 80,
                "prompt_cache_miss_tokens": 20,
            },
            112,
            80,
            20,
        ),
        (
            {
                "prompt_cache_hit_tokens": 80,
                "prompt_cache_miss_tokens": 20,
                "completion_tokens": 12,
            },
            112,
            80,
            20,
        ),
        (
            {
                "uncached_prompt_tokens": 20,
                "cached_prompt_tokens": 80,
                "completion_tokens": 12,
                "total_tokens": 32,
            },
            112,
            80,
            20,
        ),
        (
            {
                "prompt_tokens": 20,
                "prompt_cache_hit_tokens": 80,
                "prompt_cache_miss_tokens": 20,
                "completion_tokens": 12,
                "total_tokens": 32,
            },
            112,
            80,
            20,
        ),
        (
            {"prompt_tokens": 100, "completion_tokens": 12, "cached_tokens": 80},
            112,
            80,
            20,
        ),
        (
            {"prompt_tokens": 100, "uncached_prompt_tokens": 20, "completion_tokens": 12},
            112,
            80,
            20,
        ),
        (
            {
                "prompt_tokens": 100,
                "completion_tokens": 12,
                "prompt_tokens_details": {"cached_tokens": 0},
            },
            112,
            0,
            100,
        ),
    ],
)
def test_provider_cache_formats_share_disjoint_accounting(usage, expected, cached, uncached):
    settings = Settings(base_url="https://provider.example", model="example", api_key="fixture")
    response = {"choices": [{"message": {"content": "ok"}}], "usage": usage}
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response))
    ) as client:
        reply = ChatProvider(settings, client=client).complete([], [])
    assert reply.usage["uncached_prompt_tokens"] == uncached
    assert reply.usage["cached_prompt_tokens"] == cached
    assert reply.usage["prompt_tokens"] == uncached + cached
    assert reply.usage["total_tokens"] == expected
    assert token_total(reply.usage) == expected
    assert token_total(usage) == expected
    assert normalize_usage(reply.usage) == reply.usage


def test_reasoning_and_reported_total_are_not_added_to_output():
    usage = normalize_usage(
        {
            "prompt_tokens": 100,
            "completion_tokens": 12,
            "total_tokens": 120,
            "prompt_tokens_details": {"cached_tokens": 80},
            "completion_tokens_details": {"reasoning_tokens": 8},
        }
    )
    assert usage["reasoning_tokens"] == 8
    assert usage["reported_total_tokens"] == 120
    assert token_total(usage) == 112


@pytest.mark.parametrize(
    "raw",
    [
        None,
        [],
        {"prompt_tokens": -1},
        {"total_tokens": True},
        {"total_tokens": "10"},
        {"total_tokens": 1.5},
    ],
)
def test_invalid_or_missing_usage_is_not_zero_cost(raw):
    assert normalize_usage(raw) == {}
    assert token_total(raw) is None


def test_missing_cache_details_do_not_claim_zero_cache():
    usage = normalize_usage({"prompt_tokens": 100, "completion_tokens": 12})
    assert token_total(usage) == 112
    assert token_components(usage) == {"uncached_input": None, "cached_input": None, "output": 12}
    assert token_total({"total_tokens": 9}) == 9
    assert token_total({"cached_prompt_tokens": 8, "completion_tokens": 2}) is None


def test_mixed_cache_coverage_does_not_relabel_partial_sum_as_total():
    calls = [
        {"prompt_tokens": 1961, "completion_tokens": 40, "total_tokens": 2001},
        {
            "prompt_tokens": 1961,
            "completion_tokens": 38,
            "total_tokens": 1999,
            "prompt_tokens_details": {"cached_tokens": 1792},
        },
    ]
    usage = aggregate_usage(calls)
    assert usage == {"prompt_tokens": 3922, "completion_tokens": 78, "total_tokens": 4000}
    assert token_total(usage) == _measured_tokens(calls) == 4000
    assert "cached_prompt_tokens" not in usage and "uncached_prompt_tokens" not in usage


def test_fully_measured_cache_aggregation_and_missing_call_coverage():
    calls = [
        {"prompt_tokens": 10, "completion_tokens": 2, "cached_prompt_tokens": 8},
        {"prompt_tokens": 20, "completion_tokens": 3, "cached_prompt_tokens": 12},
    ]
    usage = aggregate_usage(calls)
    assert token_components(usage) == {"uncached_input": 10, "cached_input": 20, "output": 5}
    assert token_total(usage) == _measured_tokens(calls) == 35
    assert _measured_tokens([calls[0], {}]) is None
    assert token_total(aggregate_usage([calls[0], {}])) is None
    assert aggregate_usage([]) == {}


def test_research_metrics_expose_component_means_with_matching_coverage():
    rows = [
        {
            "episode_id": "a",
            "status": "completed",
            "passed": True,
            "answer_usage": {
                "uncached_prompt_tokens": 20,
                "cached_prompt_tokens": 80,
                "completion_tokens": 12,
                "total_tokens": 32,
            },
        },
        {
            "episode_id": "b",
            "status": "completed",
            "passed": False,
            "answer_usage": {
                "prompt_tokens": 100,
                "cached_prompt_tokens": 60,
                "completion_tokens": 8,
            },
        },
        {
            "episode_id": "c",
            "status": "completed",
            "passed": True,
            "answer_tokens": None,
            "answer_usage": {
                "prompt_tokens": 100,
                "cached_prompt_tokens": 60,
                "completion_tokens": 8,
            },
        },
        {
            "episode_id": "d",
            "status": "provider_failed",
            "passed": None,
            "answer_usage": {"total_tokens": 99999},
        },
    ]
    metrics = research_metrics(rows, list("abcd"))["efficiency"]
    assert metrics["token_accounting"] == "uncached_input + cached_input + output"
    assert metrics["answer_tokens"] == {"mean": 110, "measured_count": 2, "missing_count": 1}
    parts = metrics["answer_token_breakdown"]
    assert {key: item["mean"] for key, item in parts.items()} == {
        "uncached_input": 30,
        "cached_input": 70,
        "output": 10,
    }
    assert all(item["missing_count"] == 1 for item in parts.values())


def test_judge_retries_preserve_complete_tokens_and_unknown_cache_breakdown():
    from evog.core.providers import ModelReply
    from evog.evaluation.data import Episode
    from evog.evaluation.judge import score

    class RetryJudge:
        def __init__(self):
            self.calls = 0

        def complete(self, messages, tools):
            self.calls += 1
            return ModelReply(
                content="invalid" if self.calls == 1 else '{"label":"CORRECT"}',
                usage={"prompt_tokens": 10, "completion_tokens": 2}
                if self.calls == 1
                else {"prompt_tokens": 20, "completion_tokens": 3, "cached_prompt_tokens": 12},
            )

    episode = Episode(
        benchmark="evermembench",
        episode_id="fixture",
        question="When?",
        gold="January 15",
        scope="demo",
        question_type="temporal",
    )
    result = score(RetryJudge(), episode, "January 15")
    assert result["passed"] is True and result["attempts"] == 2
    assert token_total(result["usage"]) == 35
    assert "cached_prompt_tokens" not in result["usage"]
    assert _measured_tokens(result["usage_calls"]) == 35


@pytest.mark.parametrize("field", ["cached", "uncached"])
def test_impossible_partial_input_counter_keeps_total_but_marks_split_unknown(field):
    usage = normalize_usage(
        {"prompt_tokens": 10, "completion_tokens": 2, field + "_prompt_tokens": 20}
    )
    assert token_total(usage) == 12
    assert token_components(usage) == {"uncached_input": None, "cached_input": None, "output": 2}
    assert usage["reported_" + field + "_prompt_tokens"] == 20
    assert normalize_usage(usage) == usage


def test_multiple_choice_without_judge_call_keeps_measured_agent_cost():
    from evog.evaluation.runner import metrics

    result = metrics(
        [
            {
                "episode_id": "one",
                "passed": True,
                "agent_usage": {"prompt_tokens": 10, "completion_tokens": 2},
                "judge_usage": {},
                "judge_tokens": 0,
            }
        ]
    )
    assert token_total(result["usage"]) == 12
