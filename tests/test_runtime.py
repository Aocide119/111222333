import json

import pytest
from conftest import ScriptedProvider

from evog.agents.demo import DemoProvider
from evog.agents.interaction import interact, trim_messages, validate_answer
from evog.app import Application
from evog.core.errors import ContractError, ProviderError, RunFailed
from evog.core.models import AnswerDraft
from evog.core.providers import ModelReply, ToolCall
from evog.harness.schema import Harness


def test_complete_offline_interaction_reflects_before_feedback(store, settings):
    answer = interact(store, DemoProvider(), settings, "Latest release?", ["demo-team"])
    assert "January 15" in answer.text
    assert answer.citations == ["demo-team/002"]
    assert answer.reflection is not None
    assert store.runs(answer.revision_id)[0]["outcome"] is None
    assert [event.kind for event in store.events(answer.run_id)][-2:] == ["answer", "reflection"]


def test_forged_citation_is_repaired_by_real_delivery(store, settings):
    forged = ModelReply(
        content=json.dumps(
            {"text": "claim", "confidence": 0.9, "status": "complete", "citations": ["private/1"]}
        )
    )
    search = ModelReply(
        tool_calls=[
            ToolCall(
                id="s", name="grep_search", arguments={"target": "memory_units", "query": "changed"}
            )
        ]
    )
    supported = ModelReply(
        content=json.dumps(
            {
                "text": "January 15",
                "confidence": 0.9,
                "status": "complete",
                "citations": ["demo-team/002"],
            }
        )
    )
    provider = ScriptedProvider(forged, search, supported)
    answer = interact(store, provider, settings, "Latest release?", ["demo-team"])
    assert answer.citations == ["demo-team/002"]
    assert any(event.data.get("code") == "answer_contract" for event in store.events(answer.run_id))


def test_invalid_final_answers_are_selected_contract_failures(store, settings):
    provider = ScriptedProvider(
        *[ModelReply(content="not JSON") for _ in range(settings.max_turns + 1)]
    )
    with pytest.raises(RunFailed) as failure:
        interact(store, provider, settings, "Question", ["demo-team"])
    assert store.run(failure.value.run_id)["status"] == "contract_failed"
    assert provider.calls[-1][1] == []
    from evog.agents.analysis import select_experience

    selection = select_experience(store, settings, store.harness().id)
    assert selection["selected"] == [failure.value.run_id]
    assert selection["coverage"]["contract_failures"] == 1
    assert selection["coverage"]["incidents"] == 0


def test_provider_failure_preserves_run_and_safe_error(store, settings):
    provider = ScriptedProvider(ProviderError("Provider transport failed"))
    with pytest.raises(RunFailed) as failure:
        interact(store, provider, settings, "Question", ["demo-team"])
    assert store.events(failure.value.run_id)[-1].data == {"code": "provider_error"}
    assert store.run(failure.value.run_id)["status"] == "provider_failed"


def test_tool_filesystem_failure_has_separate_status(store, settings, monkeypatch):
    from evog.harness.tools import Tools

    def fail(*_):
        raise PermissionError("private filesystem details")

    monkeypatch.setattr(Tools, "execute", fail)
    provider = ScriptedProvider(
        ModelReply(
            tool_calls=[ToolCall(id="s", name="list_files", arguments={"target": "workspace"})]
        )
    )
    with pytest.raises(RunFailed) as failure:
        interact(store, provider, settings, "Question", ["demo-team"])
    assert store.run(failure.value.run_id)["status"] == "tool_failed"
    assert store.events(failure.value.run_id)[-1].data == {"code": "tool_execution"}


def test_tool_only_model_at_turn_boundary_is_budget_exhaustion(store, settings):
    reply = ModelReply(
        tool_calls=[ToolCall(id="s", name="list_files", arguments={"target": "workspace"})]
    )
    provider = ScriptedProvider(*[reply for _ in range(settings.max_turns)])
    with pytest.raises(RunFailed) as failure:
        interact(store, provider, settings, "Question", ["demo-team"])
    assert store.run(failure.value.run_id)["status"] == "budget_exhausted"


def test_context_exhaustion_is_distinct_from_provider_failure(store, settings, monkeypatch):
    # Lower the budget after settings validation to exercise the boundary before any model call.
    monkeypatch.setattr(settings, "max_context_chars", 1)
    provider = ScriptedProvider()
    with pytest.raises(RunFailed) as failure:
        interact(store, provider, settings, "Question", ["demo-team"])
    assert store.run(failure.value.run_id)["status"] == "budget_exhausted"
    assert not provider.calls


def test_reflection_failure_preserves_supported_answer(store, settings, empty_reply):
    provider = ScriptedProvider(empty_reply, ProviderError("unavailable"))
    answer = interact(store, provider, settings, "Question", ["demo-team"])
    assert answer.reflection is None
    assert store.run(answer.run_id)["status"] == "completed"


def test_fixed_complete_citation_guard_survives_policy_changes(store):
    contents = store.harness().contents
    contents["prompt/system.md"] = "Answer without evidence."
    draft = AnswerDraft(text="unsupported", confidence=0.9, status="complete", citations=[])
    with pytest.raises(ContractError, match="source citations"):
        validate_answer(draft, Harness(contents), set())


def test_intervention_answer_limit_is_effective(store):
    contents = store.harness().contents
    contents["middleware/interventions.json"] = (
        '{"max_answer_chars":256,"max_citations":1,"require_citations_for_partial":true}'
    )
    draft = AnswerDraft(
        text="x" * 257, confidence=0.9, status="complete", citations=["demo-team/002"]
    )
    with pytest.raises(ContractError, match="output limits"):
        validate_answer(draft, Harness(contents), {"demo-team/002"})


def test_tool_call_budget_is_enforced(store, settings, empty_reply):
    settings.max_tool_calls = 1
    batch = ModelReply(
        tool_calls=[
            ToolCall(
                id="1", name="grep_search", arguments={"target": "memory_units", "query": "release"}
            ),
            ToolCall(
                id="2",
                name="write_file",
                arguments={
                    "target": "memory_store",
                    "resource_path": "working_memory/x.md",
                    "content": "no",
                },
            ),
        ]
    )
    reflection = ModelReply(
        content='{"searched":["release"],"evidence_found":"some","unresolved":["question"],"limitation":"budget"}'
    )
    provider = ScriptedProvider(batch, empty_reply, reflection)
    answer = interact(store, provider, settings, "Question", ["demo-team"])
    results = [event for event in store.events(answer.run_id) if event.kind == "tool"]
    assert results[0].data["result"]["ok"]
    assert not results[1].data["result"]["ok"]


def test_runtime_enforces_tool_cap_when_model_validation_is_bypassed(store, settings):
    settings.max_tool_calls = 60  # simulate an older TOML loaded after validation
    settings.max_turns = 1
    provider = ScriptedProvider(
        ModelReply(
            tool_calls=[
                ToolCall(id=str(i), name="list_files", arguments={"target": "workspace"})
                for i in range(3)
            ]
        ),
        ModelReply(
            content='{"text":"No evidence","confidence":0.5,"status":"insufficient","citations":[]}'
        ),
        ModelReply(
            content='{"searched":[],"evidence_found":"none","unresolved":[],"limitation":"budget"}'
        ),
    )
    answer = interact(store, provider, settings, "Question", ["demo-team"])
    started = next(
        e.data
        for e in store.events(answer.run_id)
        if e.kind == "budget" and e.data["phase"] == "started"
    )
    assert started["max_tool_calls"] == 20
    assert started["configured_max_tool_calls"] == 60


def test_command_orchestration_and_empty_analysis_need_no_credentials(tmp_path):
    with Application(tmp_path / "workspace") as group:
        report = group.analyze()
        assert report.coverage["total"] == 0
        assert group.propose(report).changes == []


def test_context_trim_preserves_task_and_complete_tool_exchanges():
    head = [{"role": "system", "content": "rules"}, {"role": "user", "content": "question"}]
    old = [
        {"role": "assistant", "tool_calls": [{"id": "old"}]},
        {"role": "tool", "tool_call_id": "old", "content": "x" * 10000},
    ]
    recent = [
        {"role": "assistant", "tool_calls": [{"id": "a"}, {"id": "b"}]},
        {"role": "tool", "tool_call_id": "a", "content": "first"},
        {"role": "tool", "tool_call_id": "b", "content": "second"},
    ]
    assert trim_messages(head + old + recent, 1000) == head + recent
    assert trim_messages(head + old + recent, 100) == head


def test_context_trimming_continues_answering_and_keeps_original_trace(store, settings):
    settings.max_context_chars = 20000
    provider = ScriptedProvider(
        ModelReply(content="x" * 10000),
        ModelReply(
            tool_calls=[
                ToolCall(
                    id="s",
                    name="grep_search",
                    arguments={"target": "memory_units", "query": "changed"},
                )
            ]
        ),
        ModelReply(
            content='{"text":"January 15","confidence":0.9,"status":"complete","citations":["demo-team/002"]}'
        ),
    )
    answer = interact(store, provider, settings, "Latest release?", ["demo-team"])
    events = store.events(answer.run_id)
    assert any(e.kind == "context_trim" for e in events)
    assert next(e for e in events if e.kind == "model").data["content"] == "x" * 10000
    assert provider.calls[1][0][:2] == provider.calls[0][0][:2]
    assert not any(m.get("content") == "x" * 10000 for m in provider.calls[1][0])


@pytest.mark.parametrize("boundary", ["turn", "calls", "rounds", "loop"])
def test_reserved_final_answer_at_each_budget_boundary(store, settings, boundary):
    settings.max_turns = 3
    if boundary == "calls":
        settings.max_tool_calls = 1
    if boundary == "rounds":
        settings.max_tool_rounds = 1
    n = 1 if boundary in {"calls", "rounds"} else 3
    # Distinct requests avoid the loop guard when testing the turn budget.
    searches = [
        ModelReply(
            tool_calls=[
                ToolCall(
                    id=f"s{i}",
                    name="grep_search",
                    arguments={
                        "target": "memory_units",
                        "query": "changed",
                        "offset": 0 if boundary == "loop" else i,
                    },
                )
            ]
        )
        for i in range(n)
    ]
    provider = ScriptedProvider(
        *searches,
        ModelReply(
            content='{"text":"January 15","confidence":0.9,"status":"complete","citations":["demo-team/002"]}'
        ),
    )
    answer = interact(store, provider, settings, "Latest release?", ["demo-team"])
    assert provider.calls[-1][1] == []
    assert len(provider.calls) == n + 1
    budget = next(
        e
        for e in store.events(answer.run_id)
        if e.kind == "budget" and e.data["phase"] == "answered"
    )
    assert budget.data["exit_reason"].endswith("+final_turn")


def test_tool_round_limit_counts_batches_and_parallel_cap_denies_extra_calls(store, settings):
    settings.max_tool_rounds = 1
    batch = ModelReply(
        tool_calls=[
            ToolCall(id=str(i), name="list_files", arguments={"target": "workspace"})
            for i in range(4)
        ]
    )
    provider = ScriptedProvider(
        batch,
        ModelReply(
            content='{"text":"No evidence","confidence":0.5,"status":"insufficient","citations":[]}'
        ),
        ModelReply(
            content='{"searched":[],"evidence_found":"none","unresolved":[],"limitation":"budget"}'
        ),
    )
    answer = interact(store, provider, settings, "Question", ["demo-team"])
    results = [e.data["result"] for e in store.events(answer.run_id) if e.kind == "tool"]
    assert [r["ok"] for r in results] == [True, True, True, False]
    budget = next(
        e.data
        for e in store.events(answer.run_id)
        if e.kind == "budget" and e.data["phase"] == "answered"
    )
    assert budget["tool_rounds"] == 1 and budget["tool_calls"] == 3


def test_expired_question_does_not_accept_late_answer_or_make_another_call(
    store, settings, monkeypatch
):
    now = [100.0]
    monkeypatch.setattr("evog.agents.interaction.time.monotonic", lambda: now[0])
    settings.question_timeout_seconds = 1

    def late(messages, tools):
        now[0] += 2
        return ModelReply(
            content='{"text":"No evidence","confidence":0.5,"status":"insufficient","citations":[]}'
        )

    provider = ScriptedProvider(late)
    with pytest.raises(RunFailed, match="question_timeout") as failure:
        interact(store, provider, settings, "Question", ["demo-team"])
    assert len(provider.calls) == 1
    assert store.run(failure.value.run_id)["status"] == "budget_exhausted"


@pytest.mark.parametrize("ref", ["private/1", "demo-team/001"])
def test_reserved_final_answer_still_rejects_unread_or_foreign_citations(store, settings, ref):
    settings.max_tool_calls = 1
    provider = ScriptedProvider(
        ModelReply(
            tool_calls=[
                ToolCall(
                    id="s",
                    name="grep_search",
                    arguments={"target": "memory_units", "query": "changed"},
                )
            ]
        ),
        ModelReply(
            content=json.dumps(
                {"text": "claim", "confidence": 0.9, "status": "complete", "citations": [ref]}
            )
        ),
    )
    with pytest.raises(RunFailed, match="answer_contract_exhausted") as failure:
        interact(store, provider, settings, "Question", ["demo-team"])
    assert store.run(failure.value.run_id)["status"] == "contract_failed"


def test_archived_tool_preview_withholds_citations_and_trace_keeps_full_result(store, settings):
    from evog.agents.demo import DEMO_MESSAGES
    from evog.core.io import dumps, fingerprint
    from evog.core.models import Message

    store.ingest(
        iter(
            Message.model_validate(
                {**DEMO_MESSAGES[0], "message_id": f"large-{i:02d}", "text": "release " + "x" * 700}
            )
            for i in range(30)
        )
    )
    contents = store.harness().contents
    contents["tools/operations.json"] = '{"search_mode":"any","search_limit":30,"context_window":5}'
    path = "memory_units/" + fingerprint("demo-team") + ".jsonl"
    # Source order is timestamp/message ID; locate the source without delivering it to the model.
    ordered = list(store.messages("demo-team"))
    line = next(i + 1 for i, message in enumerate(ordered) if message.ref == "demo-team/large-00")
    final = ModelReply(
        content='{"text":"release","confidence":0.9,"status":"complete","citations":["demo-team/large-00"]}'
    )
    provider = ScriptedProvider(
        ModelReply(
            tool_calls=[
                ToolCall(
                    id="s",
                    name="grep_search",
                    arguments={"target": "memory_units", "query": "release"},
                )
            ]
        ),
        final,
        ModelReply(
            tool_calls=[
                ToolCall(
                    id="r",
                    name="read_file",
                    arguments={
                        "target": "memory_units",
                        "resource_path": path.removeprefix("memory_units/"),
                        "start_line": line,
                        "end_line": line,
                    },
                )
            ]
        ),
        final,
    )
    answer = interact(store, provider, settings, "Question", ["demo-team"], Harness(contents))
    assert len(provider.calls) == 4
    events = store.events(answer.run_id)
    archived = next(e for e in events if e.kind == "tool" and "persisted_path" in e.data)
    assert "matches" in archived.data["result"]
    assert len(dumps(archived.data["context_result"])) <= settings.max_tool_output_chars
    assert any(e.data.get("code") == "answer_contract" for e in events)
