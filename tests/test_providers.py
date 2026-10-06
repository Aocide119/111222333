import json

import httpx
import pytest

from evog.core.config import Settings
from evog.core.errors import DeadlineExceeded, ProviderError
from evog.core.providers import ChatProvider, stage_provider


def settings():
    return Settings(
        base_url="https://provider.example/v1", model="example", api_key="private-token"
    )


def test_provider_encodes_tools_and_parses_response_without_credential_output():
    def respond(request):
        assert str(request.url) == "https://provider.example/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer private-token"
        assert json.loads(request.content)["model"] == "example"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "function": {"name": "list_files", "arguments": "{}"},
                                }
                            ],
                        }
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        provider = ChatProvider(settings(), client=client)
        reply = provider.complete([{"role": "user", "content": "hi"}], [])
        assert reply.tool_calls[0].arguments == {}
        assert reply.usage["prompt_tokens"] == 10
        assert "private-token" not in reply.model_dump_json()
        provider.close()
        assert not client.is_closed


def test_provider_http_error_body_is_not_exposed():
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(403, text="private-token secret body")
        )
    ) as client:
        with pytest.raises(ProviderError, match="HTTP 403") as failure:
            ChatProvider(settings(), client=client).complete([], [])
        assert "private-token" not in str(failure.value)


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"choices": []},
        {"choices": [{"message": []}]},
        {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {"id": "1", "function": {"name": "x", "arguments": "not-json"}}
                        ]
                    }
                }
            ]
        },
    ],
)
def test_invalid_provider_response_has_a_safe_error(response):
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response))
    ) as client:
        with pytest.raises(ProviderError, match="invalid chat response"):
            ChatProvider(settings(), client=client).complete([], [])


def test_retry_is_bounded_and_http_success_is_parsed(monkeypatch):
    monkeypatch.setattr("evog.core.providers.time.sleep", lambda _: None)
    attempts = []

    def respond(_):
        attempts.append(1)
        if len(attempts) < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        assert ChatProvider(settings(), client=client).complete([], []).content == "{}"
    assert len(attempts) == 3


def test_provider_config_requires_credentials_and_clean_api_root():
    with pytest.raises(ProviderError, match="Set EVOG"):
        ChatProvider(Settings())
    with pytest.raises(ProviderError, match="Base URL"):
        ChatProvider(
            Settings(base_url="https://user:password@example.com/v1", model="x", api_key="x")
        )


def test_provider_accepts_host_url_and_optional_reasoning_setting():
    def respond(request):
        assert str(request.url) == "https://provider.example/v1/chat/completions"
        payload = json.loads(request.content)
        assert payload["reasoning_effort"] == "none"
        assert payload["thinking"] == {"type": "disabled"}
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        configured = settings().model_copy(
            update={"base_url": "https://provider.example/", "reasoning_effort": "none"}
        )
        assert ChatProvider(configured, client=client).complete([], []).content == "{}"


def test_all_stage_transports_keep_model_and_reasoning_flags_with_separate_output_budgets():
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    configured = settings().model_copy(update={"reasoning_effort": "none"})
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        p = ChatProvider(configured, client=client)
        for stage in ["analysis", "synthesis", "evolution"]:
            stage_provider(p, configured, stage).complete([], [])
    assert [r["max_completion_tokens"] for r in requests] == [20000, 20000, 32000]
    assert all(r["reasoning_effort"] == "none" and r["model"] == "example" for r in requests)


def test_deadline_bounds_http_timeout_and_prevents_retries_after_expiry(monkeypatch):
    now = [10.0]
    monkeypatch.setattr("evog.core.providers.time.monotonic", lambda: now[0])
    calls = []

    def respond(request):
        calls.append(request.extensions["timeout"])
        now[0] += 2
        return httpx.Response(503)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(DeadlineExceeded):
            ChatProvider(settings(), client=client).complete_before([], [], deadline=11)
    assert len(calls) == 1
    assert all(t <= 1 for t in calls[0].values())


def test_stalled_requests_retry_within_a_bounded_attempt_count(monkeypatch):
    monkeypatch.setattr("evog.core.providers.time.sleep", lambda _: None)
    calls = []

    def respond(request):
        calls.append(1)
        if len(calls) < 3:
            raise httpx.ReadTimeout("stalled", request=request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        assert ChatProvider(settings(), client=client).complete([], []).content == "{}"
    assert len(calls) == 3


def test_stage_requests_send_sampling_controls_and_keep_reasoning_disabled():
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    configured = settings().model_copy(
        update={
            "temperature": 0.1,
            "sampling_seed": 17,
            "analysis_temperature": 0.2,
            "synthesis_temperature": 0.3,
            "reflection_temperature": 0.4,
            "evolution_temperature": 0.7,
        }
    )
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        provider = ChatProvider(configured, client=client)
        provider.complete([], [])
        for stage in ("reflection", "analysis", "synthesis", "evolution"):
            stage_provider(provider, configured, stage).complete([], [])
    assert [r["temperature"] for r in requests] == [0.1, 0.4, 0.2, 0.3, 0.7]
    assert [r["max_completion_tokens"] for r in requests] == [8192, 8192, 20000, 20000, 32000]
    assert all(r["seed"] == 17 and r["model"] == "example" for r in requests)
    assert all(
        r["reasoning_effort"] == "none" and r["thinking"] == {"type": "disabled"} for r in requests
    )


def test_optional_sampling_controls_are_omitted():
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        ChatProvider(settings(), client=client).complete([], [])
    assert "seed" not in requests[0] and "temperature" not in requests[0]


@pytest.mark.parametrize("failure", ["timeout", "http"])
def test_analysis_transport_retries_three_total_attempts_with_ten_second_backoff(
    monkeypatch, failure
):
    attempts, delays = [], []
    monkeypatch.setattr("evog.core.providers.time.sleep", delays.append)

    def respond(request):
        attempts.append(1)
        if failure == "timeout":
            raise httpx.ReadTimeout("stalled", request=request)
        return httpx.Response(503)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        provider = stage_provider(ChatProvider(settings(), client=client), settings(), "analysis")
        with pytest.raises(ProviderError):
            provider.complete([], [])
    assert len(attempts) == 3 and delays == [10, 20]


def test_backoff_that_exceeds_deadline_does_not_sleep_or_issue_another_request(monkeypatch):
    attempts, delays = [], []
    monkeypatch.setattr("evog.core.providers.time.monotonic", lambda: 10)
    monkeypatch.setattr("evog.core.providers.time.sleep", delays.append)

    def respond(request):
        attempts.append(1)
        return httpx.Response(503)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        provider = stage_provider(ChatProvider(settings(), client=client), settings(), "analysis")
        with pytest.raises(DeadlineExceeded):
            provider.complete_before([], [], deadline=15)
    assert len(attempts) == 1 and delays == []
