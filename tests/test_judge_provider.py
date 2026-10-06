"""Independent judge routing and budgets, without contacting a model endpoint."""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock

import httpx
import pytest

from evog.agents.demo import DemoProvider
from evog.app import Application
from evog.core.config import Settings
from evog.core.errors import DeadlineExceeded, ProviderError
from evog.core.providers import ChatProvider, ConcurrentProvider, ModelReply
from evog.evaluation.data import Episode
from evog.evaluation.judge import score


def episode():
    return Episode(
        benchmark="groupmembench",
        episode_id="synthetic",
        scope="Finance",
        question_type="multi_hop",
        question="Question",
        gold="Reference",
    )


def test_live_answer_provider_is_never_an_implicit_judge(tmp_path):
    configured = Settings(
        base_url="https://answer.example/v1", model="answer", api_key="answer-test"
    )
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: pytest.fail("Unexpected request"))
    ) as client:
        answer = ChatProvider(configured, client=client)
        with Application(tmp_path, settings=configured, provider=answer) as app:
            assert app.provider is answer
            with pytest.raises(ProviderError, match="explicit judge"):
                _ = app.judge_provider


def test_explicit_judge_configuration_routes_to_separate_endpoint_model_and_key(
    tmp_path, monkeypatch
):
    requests, transports = [], []

    def respond(request):
        requests.append(
            (str(request.url), request.headers["Authorization"], json.loads(request.content))
        )
        return httpx.Response(200, json={"choices": [{"message": {"content": "Final: Correct"}}]})

    configured = Settings(
        base_url="https://answer.example/v1",
        model="answer",
        api_key="answer-test",
        judge_base_url="https://judge.example/v1",
        judge_model="judge",
        judge_api_key="judge-test",
        sampling_seed=123,
        judge_temperature=0.2,
    )
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:

        class CapturedProvider(ChatProvider):
            def __init__(self, settings):
                super().__init__(settings, client=client)
                transports.append(self)

        monkeypatch.setattr("evog.app.ChatProvider", CapturedProvider)
        with Application(tmp_path, settings=configured) as app:
            assert score(app.judge_provider, episode(), "Prediction")["passed"] is True
            # The answer transport remains uninitialized throughout judge-only scoring.
            assert app._model is None and len(transports) == 1
            assert app.judge_provider is app.judge_provider
        assert not client.is_closed
    url, authorization, payload = requests[0]
    assert (
        url == "https://judge.example/v1/chat/completions" and authorization == "Bearer judge-test"
    )
    assert payload["model"] == "judge" and payload["max_completion_tokens"] == 8192
    assert payload["temperature"] == 0.2 and "seed" not in payload
    assert payload["reasoning_effort"] == "none" and payload["thinking"] == {"type": "disabled"}


def test_demo_fixture_can_be_reused_but_explicit_judge_wins(tmp_path):
    answer, judge = DemoProvider(), DemoProvider()
    with Application(tmp_path / "shared", provider=answer) as app:
        assert app.judge_provider.provider is answer
    with Application(tmp_path / "separate", provider=answer, judge_provider=judge) as app:
        assert app.judge_provider.provider is judge


def test_judge_provider_failure_is_terminal_without_stage_retry():
    class Unavailable:
        calls = 0

        def complete(self, messages, tools):
            self.calls += 1
            raise ProviderError("unavailable")

    provider = Unavailable()
    judged = score(provider, episode(), "Prediction")
    assert (
        provider.calls == 1 and judged["passed"] is None and judged["status"] == "judge_unavailable"
    )


def test_judge_parser_repairs_share_one_deadline(monkeypatch):
    now = [0.0]
    monkeypatch.setattr("evog.core.providers.time.monotonic", lambda: now[0])
    monkeypatch.setattr("evog.evaluation.judge.time.monotonic", lambda: now[0])

    class Malformed:
        calls = 0

        def complete(self, messages, tools):
            self.calls += 1
            now[0] += 3
            return ModelReply(content="unparseable")

    provider = Malformed()
    judged = score(provider, episode(), "Prediction", timeout_seconds=5)
    assert provider.calls == 2 and judged["status"] == "judge_timeout"


def test_judge_concurrency_limits_independently_issued_requests():
    lock, release, reached_limit = Lock(), Event(), Event()

    class Blocking:
        active = maximum = calls = 0

        def complete(self, messages, tools):
            with lock:
                self.calls += 1
                self.active += 1
                self.maximum = max(self.maximum, self.active)
                if self.active == 2:
                    reached_limit.set()
            assert release.wait(2)
            with lock:
                self.active -= 1
            return ModelReply(content="ok")

    transport = Blocking()
    provider = ConcurrentProvider(transport, 2)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(provider.complete, [], []) for _ in range(4)]
        try:
            assert reached_limit.wait(2)
            assert transport.maximum == 2
        finally:
            release.set()
        assert all(f.result().content == "ok" for f in futures)
    assert transport.maximum == 2 and transport.calls == 4


def test_expired_judge_deadline_does_not_acquire_request_slot(monkeypatch):
    monkeypatch.setattr("evog.core.providers.time.monotonic", lambda: 10)
    provider = ConcurrentProvider(DemoProvider(), 1)
    with pytest.raises(DeadlineExceeded, match="Judge deadline"):
        provider.complete_before([], [], deadline=9)
