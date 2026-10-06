import json
from uuid import uuid4

import pytest
from conftest import ScriptedProvider

from evog.agents.analysis import InspectArgs, TraceAccess, analyze, diagnose, select_experience
from evog.agents.demo import DemoProvider
from evog.agents.interaction import interact
from evog.core.errors import ContractError
from evog.core.models import Answer, EvidenceRef, Feedback, Reflection
from evog.core.providers import ModelReply, ToolCall


def create_run(store, confidence, outcome=None, status="completed", question="A question"):
    run_id = uuid4().hex
    revision = store.harness().id
    store.start_run(run_id, revision, question, ["demo-team"])
    answer = (
        Answer(
            text="answer",
            confidence=confidence,
            status="complete",
            citations=["demo-team/001"],
            run_id=run_id,
            revision_id=revision,
        )
        if status == "completed"
        else None
    )
    store.finish_run(run_id, answer, status)
    if outcome:
        store.feedback(Feedback(run_id=run_id, outcome=outcome, source="test"))
    return run_id


def test_all_confidence_feedback_cells_and_unknowns_remain_distinct(store, settings):
    run_ids = [
        create_run(store, confidence, outcome)
        for confidence, outcome in [
            (0.9, "accepted"),
            (0.9, "rejected"),
            (0.5, "accepted"),
            (0.1, "rejected"),
            (0.9, None),
            (0.1, None),
        ]
    ]
    create_run(store, 0.1, status="incident")
    selected = select_experience(store, settings, store.harness().id)
    assert all(
        selected["coverage"][label] == 1
        for label in ["CC", "CW", "UC", "UW", "unjudged_high", "unjudged_low", "incidents"]
    )
    assert selected["coverage"]["total"] == 7
    assert set(selected["selected"]) == {run_ids[1], run_ids[2], run_ids[3], run_ids[5]}
    assert selected["controls"] == [run_ids[0]]


def test_latest_feedback_overrides_without_deleting_prior_feedback(store, settings):
    run_id = create_run(store, 0.9, "accepted")
    store.feedback(Feedback(run_id=run_id, outcome="rejected", source="correction"))
    assert select_experience(store, settings, store.harness().id)["coverage"]["CW"] == 1
    with store.connect() as db:
        assert (
            db.execute("SELECT COUNT(*) FROM feedback WHERE run_id=?", (run_id,)).fetchone()[0] == 2
        )


def test_selection_reports_coverage_before_bounding(store, settings):
    for i in range(3):
        create_run(store, 0.1, question=f"question-{i}")
    settings.analysis_max_runs = 1
    result = select_experience(store, settings, store.harness().id)
    assert result["coverage"]["eligible"] == 3
    assert result["coverage"]["selected"] == 1
    assert len(result["selected"]) == 1


def test_analysis_max_runs_limits_questions_but_keeps_all_trials(store, settings):
    first = create_run(store, 0.1, "rejected", question="same")
    second = create_run(store, 0.1, "rejected", question="same")
    other = create_run(store, 0.9, "accepted", question="other")
    settings.analysis_max_runs = 1
    result = select_experience(store, settings, store.harness().id)
    assert result["coverage"]["selected"] == 1
    assert set(result["selected"]) == {first, second}
    assert other not in result["selected"]


def test_progressive_inspection_requires_scope_and_reports_exact_ranges(store):
    run_id = create_run(store, 0.1)
    store.event(run_id, "tool", {"name": "read_file", "result": {"text": "abcdefghij"}})
    access = TraceAccess(store, [run_id])
    first = access.inspect(
        InspectArgs(run_id=run_id, event_index=0, field_path="/result/text", limit=4)
    )
    assert first["content"] == "abcd"
    assert first["evidence_ref"]["end_char"] == 4
    assert first["truncated"] and first["next_start_char"] == 4
    second = access.inspect(
        InspectArgs(run_id=run_id, event_index=0, field_path="/result/text", start_char=4)
    )
    assert second["content"] == "efghij" and not second["truncated"]
    with pytest.raises(ContractError):
        access.inspect(InspectArgs(run_id="other", event_index=0))
    with pytest.raises(ContractError):
        access.inspect(InspectArgs(run_id=run_id, event_index=0, field_path="/missing"))


def test_diagnosis_rejects_an_uninspected_event(store, settings):
    run_id = create_run(store, 0.1)
    store.event(run_id, "answer", {"text": "answer"})
    reply = ModelReply(
        content=json.dumps(
            {
                "run_id": run_id,
                "query_type": "retrieval",
                "category": "retrieval",
                "earliest_break": "unseen",
                "cause": "unseen",
                "condensed_rationale": "unseen",
                "actionable_implication": "unseen",
                "uncertainty": "unknown",
                "evidence": [{"run_id": run_id, "event_index": 0, "end_char": 10}],
            }
        )
    )
    with pytest.raises(ContractError, match="did not inspect"):
        diagnose(
            store,
            ScriptedProvider(reply, reply, reply),
            settings,
            run_id,
            [],
            TraceAccess(store, [run_id]),
        )


def test_diagnosis_receives_trial_verifier_self_report_and_timeout_context(store, settings):
    first = create_run(store, 0.2, "rejected", question="Repeated question")
    second = create_run(store, 0.9, status="budget_exhausted", question="Repeated question")
    original = store.run(first)["answer"]
    original.pop("reflection", None)
    store.finish_run(
        first,
        Answer(
            **original,
            reflection=Reflection(
                searched=[],
                evidence_found="partial",
                unresolved=["date"],
                limitation="source omitted",
            ),
        ),
        "completed",
    )
    store.event(first, "answer", {"text": "answer"})
    store.event(second, "error", {"code": "question_timeout"})
    store.event(second, "budget", {"phase": "stopped", "tool_calls": 3})

    class ContextProvider:
        def __init__(self):
            self.supplied = None

        def complete(self, messages, tools):
            if self.supplied is None:
                self.supplied = json.loads(messages[1]["content"])
                return ModelReply(
                    tool_calls=[
                        ToolCall(
                            id="inspect",
                            name="inspect_trace",
                            arguments={"run_id": first, "event_index": 0},
                        )
                    ]
                )
            ref = json.loads(messages[-1]["content"])["evidence_ref"]
            return ModelReply(content=json.dumps(diagnosis_payload(first, ref)))

    provider = ContextProvider()
    diagnosis = diagnose(store, provider, settings, first, [], TraceAccess(store, [first, second]))
    assert diagnosis.run_id == first
    assert {item["run_id"] for item in provider.supplied["trial_group"]} == {first, second}
    assert provider.supplied["verifier"]["outcome"] == "rejected"
    assert (
        provider.supplied["answer_bias"]
        == "evidence_found: partial | unresolved: date | limitation: source omitted"
    )
    assert provider.supplied["timeout"]["detected"] is False
    timeout_trial = next(
        item for item in provider.supplied["trial_group"] if item["run_id"] == second
    )
    assert timeout_trial["timeout"] is True


def test_failed_experience_has_priority_over_recent_uncertain_success(store, settings):
    failed = create_run(store, 0.9, "rejected", question="failed")
    create_run(store, 0.1, "accepted", question="uncertain")
    settings.analysis_max_runs = 1
    a = select_experience(store, settings, store.harness().id)
    b = select_experience(store, settings, store.harness().id)
    assert a["selected"] == b["selected"] == [failed]


def test_partial_diagnosis_failure_preserves_sufficient_valid_coverage(
    store, settings, monkeypatch
):
    from evog.core.models import Diagnosis

    run_ids = [create_run(store, 0.9, "rejected", question=f"question-{i}") for i in range(3)]
    for r in run_ids:
        store.event(r, "answer", {"text": "answer"})
    settings.analysis_min_valid = 2
    settings.analysis_min_coverage = 0.6

    def diagnosed(store, provider, settings, r, controls, access):
        if r == run_ids[0]:
            raise ContractError("unavailable")
        ref = access.inspect(InspectArgs(run_id=r, event_index=0))["evidence_ref"]
        return Diagnosis.model_validate(diagnosis_payload(r, ref))

    monkeypatch.setattr("evog.agents.analysis.diagnose", diagnosed)
    refs = [
        TraceAccess(store, [r]).inspect(InspectArgs(run_id=r, event_index=0))["evidence_ref"]
        for r in run_ids[1:]
    ]
    finding = {
        "id": "repeat",
        "query_types": ["retrieval"],
        "pattern": "source missed",
        "suggested_change": "verify source",
        "evidence": refs,
        "counterevidence": "one unavailable interaction",
        "uncertainty": "limited coverage",
    }
    report = analyze(
        store,
        ScriptedProvider(
            *(ModelReply(content=json.dumps({"findings": [finding]})) for _ in range(2))
        ),
        settings,
    )
    assert report.incomplete and report.eligible_for_revision
    assert report.coverage["valid"] == 2 and len(report.findings) == 1


def test_repeated_finding_cannot_count_two_ranges_of_one_run_as_two_cases(store, settings):
    answer = interact(store, DemoProvider(), settings, "Latest release?", ["demo-team"])

    class Repeated(DemoProvider):
        def complete(self, messages, tools):
            reply = super().complete(messages, tools)
            if "synthesize EvoGroup" in messages[0]["content"]:
                data = json.loads(reply.content)
                f = data["findings"][0]
                f["support_kind"] = "repeated"
                f["evidence"] *= 2
                return ModelReply(content=json.dumps(data))
            return reply

    report = analyze(store, Repeated(), settings)
    assert report.selected_run_ids == [answer.run_id]
    assert not report.findings and not report.eligible_for_revision


def test_synthesis_accepts_sibling_trial_evidence_but_counts_one_question(
    store, settings, monkeypatch
):
    from evog.core.models import Diagnosis

    first = create_run(store, 0.9, "rejected", question="same question")
    sibling = create_run(store, 0.9, "rejected", question="same question")
    store.event(first, "answer", {"text": "first"})
    store.event(sibling, "answer", {"text": "sibling"})

    def diagnosed(store, provider, settings, run_id, controls, access):
        sibling_ref = access.inspect(InspectArgs(run_id=sibling, event_index=0))["evidence_ref"]
        return Diagnosis.model_validate(diagnosis_payload(run_id, sibling_ref))

    monkeypatch.setattr("evog.agents.analysis.diagnose", diagnosed)
    finding = {
        "id": "sibling-evidence",
        "query_types": ["retrieval"],
        "pattern": "source missed",
        "suggested_change": "verify source",
        "evidence": [
            {
                "run_id": sibling,
                "event_index": 0,
                "field_path": "",
                "start_char": 0,
                "end_char": len('{"text":"sibling"}'),
            }
        ],
        "counterevidence": "one question with repeated trials",
        "uncertainty": "isolated question",
        "support_kind": "isolated",
    }
    report = analyze(
        store,
        ScriptedProvider(
            *(ModelReply(content=json.dumps({"findings": [finding]})) for _ in range(3))
        ),
        settings,
    )
    assert report.coverage["jobs"] == 1
    assert len(report.selected_run_ids) == 2
    assert len(report.findings) == 1
    assert report.findings[0].evidence[0].run_id == sibling

    repeated = dict(
        finding, support_kind="repeated", evidence=[finding["evidence"][0], finding["evidence"][0]]
    )
    repeated_report = analyze(
        store,
        ScriptedProvider(
            *(ModelReply(content=json.dumps({"findings": [repeated]})) for _ in range(3))
        ),
        settings,
    )
    assert not repeated_report.findings
    assert "synthesis" in repeated_report.incomplete


def test_synthesis_rejects_evidence_from_context_omitted_diagnosis(store, settings, monkeypatch):
    from evog.core.models import Diagnosis

    run_ids = [create_run(store, 0.9, "rejected", question=f"question-{i}") for i in range(2)]
    for run_id in run_ids:
        store.event(run_id, "answer", {"text": "answer"})

    def diagnosed(store, provider, settings, run_id, controls, access):
        ref = access.inspect(InspectArgs(run_id=run_id, event_index=0))["evidence_ref"]
        payload = diagnosis_payload(run_id, ref)
        payload["condensed_rationale"] = "x" * 1200
        return Diagnosis.model_validate(payload)

    monkeypatch.setattr("evog.agents.analysis.diagnose", diagnosed)
    settings.analysis_min_valid = 1
    settings.analysis_min_coverage = 0.5
    settings.synthesis_max_context_chars = 8000
    ordered = select_experience(store, settings, store.harness().id)["selected"]
    omitted_ref = {
        "run_id": ordered[-1],
        "event_index": 0,
        "field_path": "",
        "start_char": 0,
        "end_char": len('{"text":"answer"}'),
    }
    finding = {
        "id": "omitted",
        "query_types": ["retrieval"],
        "pattern": "unsupported",
        "suggested_change": "verify source",
        "evidence": [omitted_ref],
        "counterevidence": "omitted diagnosis",
        "uncertainty": "unknown",
        "support_kind": "isolated",
    }
    report = analyze(
        store,
        ScriptedProvider(
            *(ModelReply(content=json.dumps({"findings": [finding]})) for _ in range(3))
        ),
        settings,
    )
    assert not report.findings
    assert "synthesis" in report.incomplete


def test_exact_evidence_range_can_be_corrected_without_relaxing_validation(store, settings):
    run_id = create_run(store, 0.1)
    store.event(run_id, "answer", {"text": "answer"})
    ref = TraceAccess(store, [run_id]).inspect(InspectArgs(run_id=run_id, event_index=0))[
        "evidence_ref"
    ]
    bad = diagnosis_payload(run_id, {**ref, "end_char": ref["end_char"] - 1})
    provider = ScriptedProvider(
        ModelReply(
            tool_calls=[
                ToolCall(
                    id="i", name="inspect_trace", arguments={"run_id": run_id, "event_index": 0}
                )
            ]
        ),
        ModelReply(content=json.dumps(bad)),
        ModelReply(content=json.dumps(diagnosis_payload(run_id, ref))),
    )
    d = diagnose(store, provider, settings, run_id, [], TraceAccess(store, [run_id]))
    assert d.evidence[0].model_dump() == ref


def test_analysis_preserves_incomplete_diagnoses(store, settings):
    run_id = create_run(store, 0.1)
    provider = ScriptedProvider(*(ModelReply(content="invalid") for _ in range(3)))
    report = analyze(store, provider, settings)
    assert run_id in report.incomplete
    assert report.diagnoses == [] and report.findings == []
    assert len(provider.calls) == 3


def diagnosis_payload(run_id, ref):
    return {
        "run_id": run_id,
        "query_type": "retrieval",
        "category": "retrieval",
        "earliest_break": "retrieval",
        "cause": "insufficient retrieval",
        "condensed_rationale": "A relevant source was missed",
        "actionable_implication": "Refine the search",
        "uncertainty": "one observation",
        "evidence": [ref],
    }


@pytest.mark.parametrize("malformed", ["commentary", "extra_fields"])
def test_diagnosis_requests_schema_correction_without_discarding_fields(store, settings, malformed):
    run_id = create_run(store, 0.1)
    store.event(run_id, "answer", {"text": "answer"})
    ref = {
        "run_id": run_id,
        "event_index": 0,
        "field_path": "",
        "start_char": 0,
        "end_char": len('{"text":"answer"}'),
    }
    valid = diagnosis_payload(run_id, ref)
    invalid = dict(valid, query_type="x" * 101, extra=None)
    bad = (
        "Commentary\n```json\n" + json.dumps(valid) + "\n```"
        if malformed == "commentary"
        else json.dumps(invalid)
    )
    provider = ScriptedProvider(
        ModelReply(
            tool_calls=[
                ToolCall(
                    id="i", name="inspect_trace", arguments={"run_id": run_id, "event_index": 0}
                )
            ]
        ),
        ModelReply(content=bad),
        ModelReply(content=json.dumps(valid)),
    )
    result = diagnose(store, provider, settings, run_id, [], TraceAccess(store, [run_id]))
    assert result.evidence[0].model_dump() == ref
    correction = provider.calls[-1][0][-1]
    assert correction["role"] == "user"
    assert "corrected JSON" in correction["content"]
    assert provider.calls[-1][1] == []


def test_parallel_diagnoses_keep_trace_access_isolated_and_results_ordered(store, settings):
    import threading

    run_ids = [create_run(store, 0.1, question=f"Question {i}") for i in range(2)]
    for run_id in run_ids:
        store.event(run_id, "answer", {"text": "answer"})
    settings.analysis_concurrency = 2
    barrier = threading.Barrier(2)

    class ParallelProvider:
        def complete(self, messages, tools):
            if not tools:
                if "synthesize EvoGroup" in messages[0]["content"]:
                    return ModelReply(content='{"findings":[]}')
            supplied = json.loads(messages[1]["content"])
            run_id = supplied["run_id"]
            if len(messages) == 2:
                barrier.wait(timeout=5)
                other = next(r for r in run_ids if r != run_id)
                return ModelReply(
                    tool_calls=[
                        ToolCall(
                            id="own",
                            name="inspect_trace",
                            arguments={"run_id": run_id, "event_index": 0},
                        ),
                        ToolCall(
                            id="other",
                            name="inspect_trace",
                            arguments={"run_id": other, "event_index": 0},
                        ),
                    ]
                )
            assert "error" in json.loads(messages[-1]["content"])
            ref = json.loads(messages[-2]["content"])["evidence_ref"]
            return ModelReply(content=json.dumps(diagnosis_payload(run_id, ref)))

    report = analyze(store, ParallelProvider(), settings)
    assert [d.run_id for d in report.diagnoses] == report.selected_run_ids
    assert len(report.diagnoses) == 2
    assert report.incomplete == {}


def test_demo_analysis_inspects_evidence_and_preserves_findings(store, settings):
    answer = interact(store, DemoProvider(), settings, "Latest release?", ["demo-team"])
    store.feedback(Feedback(run_id=answer.run_id, outcome="accepted", source="test"))
    report = analyze(store, DemoProvider(), settings)
    assert len(report.diagnoses) == 1 and len(report.findings) == 1
    assert report.findings[0].purpose == "uncertainty_calibration"
    assert report.eligible_for_revision
    assert report.calibration_run_ids == [answer.run_id]
    assert report.incomplete == {}


def test_synthesis_repair_stays_within_context_budget(store, settings):
    from evog.core.io import dumps

    answer = interact(store, DemoProvider(), settings, "Latest release?", ["demo-team"])
    store.feedback(Feedback(run_id=answer.run_id, outcome="rejected", source="test"))
    settings.synthesis_max_context_chars = 10000

    class OversizedDraft(DemoProvider):
        contexts = []

        def complete(self, messages, tools):
            if "synthesize EvoGroup" in messages[0]["content"]:
                self.contexts.append(len(dumps(messages)))
                if len(self.contexts) == 1:
                    return ModelReply(content="x" * 20000)
            return super().complete(messages, tools)

    provider = OversizedDraft()
    report = analyze(store, provider, settings)
    assert report.eligible_for_revision
    assert len(provider.contexts) == 3  # bucket correction, then cross-bucket synthesis
    assert max(provider.contexts) <= settings.synthesis_max_context_chars


def test_running_interactions_are_distinct_from_incidents(store, settings):
    create_run(store, 0.1, status="running")
    selection = select_experience(store, settings, store.harness().id)
    assert selection["coverage"]["unfinished"] == 1
    assert selection["coverage"]["incidents"] == 0
    assert selection["selected"] == []


def test_synthesis_cannot_introduce_uninspected_evidence(store, settings):
    answer = interact(store, DemoProvider(), settings, "Latest release?", ["demo-team"])
    store.feedback(Feedback(run_id=answer.run_id, outcome="rejected", source="test"))

    class ForgedSynthesis(DemoProvider):
        def complete(self, messages, tools):
            reply = super().complete(messages, tools)
            if "synthesize EvoGroup" in messages[0]["content"]:
                data = json.loads(reply.content)
                data["findings"][0]["evidence"][0]["event_index"] = 999
                return ModelReply(content=json.dumps(data))
            return reply

    report = analyze(store, ForgedSynthesis(), settings)
    assert answer.run_id in report.selected_run_ids
    assert report.diagnoses and not report.findings
    assert "synthesis" in report.incomplete


@pytest.mark.parametrize("start_char", [0, 3])
def test_inspection_cannot_deliver_empty_evidence(store, start_char):
    run_id = create_run(store, 0.1)
    store.event(run_id, "answer", {"text": "" if start_char == 0 else "abc"})
    access = TraceAccess(store, [run_id])
    with pytest.raises(ContractError, match="nonempty"):
        access.inspect(
            InspectArgs(run_id=run_id, event_index=0, field_path="/text", start_char=start_char)
        )
    assert not access.delivered


@pytest.mark.parametrize("start,end", [(0, 0), (3, 3), (4, 2)])
def test_empty_or_reversed_evidence_ref_is_invalid(start, end):
    with pytest.raises(ValueError, match="nonempty"):
        EvidenceRef(run_id="run", event_index=0, start_char=start, end_char=end)


@pytest.mark.parametrize("pointer", ["/rows/-1", "/rows/-", "/rows/+0", "/rows/01", "/rows/١"])
def test_trace_pointer_rejects_nonstandard_array_indices(store, pointer):
    run_id = create_run(store, 0.1)
    store.event(run_id, "answer", {"rows": ["first", "last"]})
    with pytest.raises(ContractError):
        TraceAccess(store, [run_id]).inspect(
            InspectArgs(run_id=run_id, event_index=0, field_path=pointer)
        )


def test_pointer_escapes_array_reads_and_literal_object_keys(store):
    run_id = create_run(store, 0.1)
    store.event(run_id, "answer", {"a/b": {"~key": ["first", "last"]}, "-1": "literal"})
    access = TraceAccess(store, [run_id])
    assert (
        access.inspect(InspectArgs(run_id=run_id, event_index=0, field_path="/a~1b/~0key/1"))[
            "content"
        ]
        == "last"
    )
    assert (
        access.inspect(InspectArgs(run_id=run_id, event_index=0, field_path="/-1"))["content"]
        == "literal"
    )
    with pytest.raises(ContractError, match="escape"):
        access.inspect(InspectArgs(run_id=run_id, event_index=0, field_path="/a~2b"))


def test_failed_output_contract_can_be_diagnosed_without_answer_or_confidence(store, settings):
    run_id = create_run(store, 0.1, status="contract_failed")
    store.event(run_id, "error", {"code": "answer_contract"})

    def checked_inspection(messages, tools):
        supplied = json.loads(messages[1]["content"])
        assert supplied["status"] == "contract_failed"
        assert supplied["subjective_confidence"] is None
        assert supplied["feedback_outcome"] == "unjudged"
        return ModelReply(
            tool_calls=[
                ToolCall(
                    id="i", name="inspect_trace", arguments={"run_id": run_id, "event_index": 0}
                )
            ]
        )

    def checked_diagnosis(messages, tools):
        ref = json.loads(messages[-1]["content"])["evidence_ref"]
        return ModelReply(
            content=json.dumps(
                {
                    "run_id": run_id,
                    "query_type": "answer delivery",
                    "category": "delivery",
                    "earliest_break": "output",
                    "cause": "invalid output",
                    "condensed_rationale": "output contract failed",
                    "actionable_implication": "clarify format",
                    "evidence": [ref],
                    "uncertainty": "one observation",
                }
            )
        )

    provider = ScriptedProvider(
        checked_inspection, checked_diagnosis, ModelReply(content='{"findings":[]}')
    )
    report = analyze(store, provider, settings)
    assert report.selected_run_ids == [run_id]
    assert len(report.diagnoses) == 1
    assert not report.incomplete


class SynthesisProvider:
    """Record the real synthesis phases and return evidence-preserving findings."""

    def __init__(self):
        self.inputs = []
        self.context_sizes = []

    def complete(self, messages, tools):
        from evog.core.io import dumps

        assert not tools
        supplied = json.loads(messages[1]["content"])
        self.inputs.append(supplied)
        self.context_sizes.append(len(dumps(messages)))
        units = [unit for bucket in supplied["buckets"].values() for unit in bucket]
        findings = [
            {
                "id": f"finding-{index}",
                "query_types": unit.get("query_types", [unit["query_type"]]),
                "pattern": unit["condensed_rationale"],
                "suggested_change": unit["actionable_implication"],
                "evidence": unit["evidence"],
                "counterevidence": "Only the included traces were inspected",
                "uncertainty": unit["uncertainty"],
                "purpose": unit["signal"],
                "support_kind": "isolated",
            }
            for index, unit in enumerate(units)
        ]
        return ModelReply(content=json.dumps({"findings": findings}))


def install_diagnoses(monkeypatch, labels=None, large=False):
    from evog.core.models import Diagnosis

    def diagnosed(store, provider, settings, run_id, controls, access):
        ref = access.inspect(InspectArgs(run_id=run_id, event_index=0))["evidence_ref"]
        data = diagnosis_payload(run_id, ref)
        data["query_type"] = (labels or {}).get(run_id, "Source attribution")
        data["query_family"] = "other"
        if large:
            data["cause"] = "The attribution bridge was not checked. " * 24
            data["condensed_rationale"] = "Resolve the attribution bridge. " * 25
        return Diagnosis.model_validate(data)

    monkeypatch.setattr("evog.agents.analysis.diagnose", diagnosed)


@pytest.mark.parametrize("use_all_flag", [False, True])
def test_all_eligible_analysis_does_not_truncate_question_scopes(store, settings, use_all_flag):
    ids = [create_run(store, 0.8, "rejected", question=f"Question {index}") for index in range(3)]
    settings.analysis_max_runs = 1 if use_all_flag else None
    settings.analysis_all_eligible = use_all_flag
    selection = select_experience(store, settings, store.harness().id)
    assert set(selection["selected"]) == set(ids)
    assert selection["coverage"]["eligible_questions"] == 3
    assert selection["coverage"]["omitted_questions"] == 0


def test_open_query_types_preserve_raw_labels_and_drive_two_real_synthesis_stages(
    store, settings, monkeypatch
):
    ids = [create_run(store, 0.8, "rejected", question=f"Question {index}") for index in range(3)]
    for run_id in ids:
        store.event(run_id, "answer", {"text": "answer"})
    labels = {
        ids[0]: "  Shared   Responsibility  ",
        ids[1]: "shared responsibility",
        ids[2]: "Novel provenance reconciliation",
    }
    install_diagnoses(monkeypatch, labels)
    provider = SynthesisProvider()
    report = analyze(store, provider, settings)
    assert [request["phase"] for request in provider.inputs] == ["bucket", "bucket", "cross_bucket"]
    assert {bucket.normalized_label for bucket in report.query_buckets} == {
        "shared responsibility",
        "novel provenance reconciliation",
    }
    assert {diagnosis.query_type for diagnosis in report.diagnoses} == set(labels.values())
    assert all(diagnosis.query_family == "other" for diagnosis in report.diagnoses)
    shared = next(
        bucket
        for bucket in report.query_buckets
        if bucket.normalized_label == "shared responsibility"
    )
    assert set(shared.original_labels) == {labels[ids[0]], labels[ids[1]]}
    assert set(shared.diagnosis_run_ids) == set(ids[:2])
    assert len(provider.inputs[-1]["buckets"]) == 2
    assert all(
        request["finding_min_support"] == settings.finding_min_support
        for request in provider.inputs
    )
    assert report.coverage["cross_bucket_comparison_batches"] == 1
    assert report.eligible_for_revision and len(report.findings) == 3
    assert report.query_type_normalization == "casefold-whitespace.v1"


def test_uc_only_experience_produces_findings_without_changing_correctness(
    store, settings, monkeypatch
):
    ids = [create_run(store, 0.5, "accepted", question=f"Question {index}") for index in range(2)]
    for run_id in ids:
        store.event(run_id, "answer", {"text": "correct but uncertain"})
    install_diagnoses(monkeypatch)
    provider = SynthesisProvider()
    report = analyze(store, provider, settings)
    assert report.coverage["UC"] == 2 and report.coverage["UW"] == 0
    assert report.failure_run_ids == [] and set(report.calibration_run_ids) == set(ids)
    assert report.eligible_for_revision and len(report.findings) == 2
    assert all(finding.purpose == "uncertainty_calibration" for finding in report.findings)
    assert {
        unit["experience_cell"]
        for units in provider.inputs[0]["buckets"].values()
        for unit in units
    } == {"UC"}
    assert {row["outcome"] for row in store.runs(store.harness().id)} == {"accepted"}


def test_unjudged_uncertainty_keeps_its_label_and_missing_confidence_is_not_low(
    store, settings, monkeypatch
):
    unjudged = create_run(store, 0.1, question="Unjudged")
    missing = create_run(store, 0.1, "accepted", question="Missing")
    store.finish_run(missing, None, "completed")
    store.event(unjudged, "answer", {"text": "uncertain"})
    install_diagnoses(monkeypatch)
    provider = SynthesisProvider()
    report = analyze(store, provider, settings)
    assert report.coverage["UC"] == 0 and report.coverage["UW"] == 0
    assert report.coverage["missing_confidence"] == 1
    assert report.selected_run_ids == [unjudged]
    unit = next(iter(provider.inputs[0]["buckets"].values()))[0]
    assert unit["experience_cell"] == "unjudged_low"
    assert unit["evidence_signals"][unjudged] == "unjudged_low"
    assert report.findings[0].purpose == "uncertainty_calibration"


def test_accepted_sibling_evidence_cannot_supply_error_support_for_a_failed_question(
    store, settings, monkeypatch
):
    from evog.core.models import Diagnosis

    failed = create_run(store, 0.9, "rejected", question="Same")
    accepted = create_run(store, 0.2, "accepted", question="Same")
    for run_id in (failed, accepted):
        store.event(run_id, "answer", {"text": "answer"})

    def diagnosed(store, provider, settings, run_id, controls, access):
        own = access.inspect(InspectArgs(run_id=failed, event_index=0))["evidence_ref"]
        sibling = access.inspect(InspectArgs(run_id=accepted, event_index=0))["evidence_ref"]
        return Diagnosis.model_validate(
            {**diagnosis_payload(run_id, own), "evidence": [own, sibling]}
        )

    monkeypatch.setattr("evog.agents.analysis.diagnose", diagnosed)
    provider = SynthesisProvider()

    def forged(messages, tools):
        reply = provider.complete(messages, tools)
        finding = json.loads(reply.content)["findings"][0]
        finding["evidence"] = [ref for ref in finding["evidence"] if ref["run_id"] == accepted]
        assert finding["purpose"] == "error_repair"
        return ModelReply(content=json.dumps({"findings": [finding]}))

    scripted = ScriptedProvider(forged, forged, forged)
    report = analyze(store, scripted, settings)
    assert not report.findings and not report.eligible_for_revision
    assert "synthesis" in report.incomplete


def test_context_bounded_bucket_batches_cover_every_complete_diagnosis(
    store, settings, monkeypatch
):
    ids = [create_run(store, 0.9, "rejected", question=f"Question {index}") for index in range(8)]
    for run_id in ids:
        store.event(run_id, "answer", {"text": "answer"})
    settings.analysis_all_eligible = True
    settings.synthesis_max_context_chars = 12500
    install_diagnoses(monkeypatch, large=True)
    provider = SynthesisProvider()
    report = analyze(store, provider, settings)
    bucket_requests = [request for request in provider.inputs if request["phase"] == "bucket"]
    assert len(bucket_requests) > 1
    assert {
        unit["run_id"]
        for request in bucket_requests
        for units in request["buckets"].values()
        for unit in units
    } == set(ids)
    assert report.coverage["synthesis_included"] == 8
    assert report.coverage["synthesis_omitted"] == 0
    assert report.eligible_for_revision and not report.incomplete
    assert max(provider.context_sizes) <= settings.synthesis_max_context_chars
    assert any(request["phase"] == "cross_bucket" for request in provider.inputs)
    assert len({finding.id for finding in report.findings}) == len(report.findings)


def test_cross_bucket_stage_cannot_reintroduce_evidence_not_in_bucket_findings(
    store, settings, monkeypatch
):
    from evog.core.models import Diagnosis

    run_id = create_run(store, 0.9, "rejected")
    store.event(run_id, "answer", {"text": "first"})
    store.event(run_id, "tool", {"name": "read_file", "result": "second"})
    refs = [
        TraceAccess(store, [run_id]).inspect(InspectArgs(run_id=run_id, event_index=i))[
            "evidence_ref"
        ]
        for i in range(2)
    ]

    def diagnosed(store, provider, settings, target, controls, access):
        return Diagnosis.model_validate({**diagnosis_payload(target, refs[0]), "evidence": refs})

    monkeypatch.setattr("evog.agents.analysis.diagnose", diagnosed)
    provider = SynthesisProvider()

    def draft(messages, tools):
        reply = provider.complete(messages, tools)
        finding = json.loads(reply.content)["findings"][0]
        phase = json.loads(messages[1]["content"])["phase"]
        finding["evidence"] = [refs[0] if phase == "bucket" else refs[1]]
        return ModelReply(content=json.dumps({"findings": [finding]}))

    report = analyze(store, ScriptedProvider(draft, draft, draft, draft), settings)
    assert report.query_buckets[0].findings
    assert not report.findings and not report.eligible_for_revision
    assert "cross_bucket:0" in report.incomplete


def test_transport_failure_is_not_retried_by_diagnosis_or_synthesis(store, settings, monkeypatch):
    from evog.core.errors import ProviderError

    run_id = create_run(store, 0.9, "rejected")
    store.event(run_id, "answer", {"text": "answer"})
    failed_diagnosis = ScriptedProvider(ProviderError("transport exhausted"))
    with pytest.raises(ProviderError):
        diagnose(store, failed_diagnosis, settings, run_id, [], TraceAccess(store, [run_id]))
    assert len(failed_diagnosis.calls) == 1
    install_diagnoses(monkeypatch)
    failed_synthesis = ScriptedProvider(ProviderError("transport exhausted"))
    report = analyze(store, failed_synthesis, settings)
    assert len(failed_synthesis.calls) == 1
    assert not report.eligible_for_revision and "synthesis" in report.incomplete


def test_historical_analysis_reports_load_without_claiming_new_bucket_provenance():
    from evog.core.models import AnalysisReport

    old = {
        "id": "old",
        "revision_id": "revision",
        "coverage": {},
        "selected_run_ids": [],
        "control_run_ids": [],
        "diagnoses": [],
        "findings": [],
        "incomplete": {},
    }
    report = AnalysisReport.model_validate(old)
    assert report.schema_version == "evog.analysis.v1"
    assert report.query_type_normalization is None and report.query_buckets == []


def test_apd_summary_and_search_do_not_deliver_hidden_trace_evidence(store):
    from evog.agents.analysis import SearchTraceArgs

    run_id = create_run(store, 0.1, "rejected")
    store.event(
        run_id, "tool", {"name": "read_file", "result": "trace payload not initially visible"}
    )
    access = TraceAccess(store, [run_id])
    assert "trace payload" not in json.dumps(access.summary(run_id))
    result = access.search(SearchTraceArgs(run_id=run_id, term="trace payload"))
    assert result["events"] == [{"index": 0, "kind": "tool"}]
    assert access.delivered == set()
    inspected = access.inspect(InspectArgs(run_id=run_id, event_index=0, field_path="/result"))
    assert inspected["content"] == "trace payload not initially visible"
    assert len(access.delivered) == 1


def test_normalization_does_not_guess_semantic_synonyms():
    from evog.agents.analysis import normalize_query_type

    assert normalize_query_type("  Temporal   Reasoning ") == "temporal reasoning"
    assert normalize_query_type("Chronology") != normalize_query_type("Temporal reasoning")
    with pytest.raises(ContractError):
        normalize_query_type(" \t\n ")


@pytest.mark.parametrize("include_both_types", [False, True])
def test_cross_bucket_claims_require_evidence_from_each_declared_type(
    store, settings, monkeypatch, include_both_types
):
    ids = [create_run(store, 0.9, "rejected", question=f"Question {index}") for index in range(2)]
    for run_id in ids:
        store.event(run_id, "answer", {"text": "answer"})
    install_diagnoses(monkeypatch, {ids[0]: "Attribution", ids[1]: "Chronology"})
    provider = SynthesisProvider()

    def draft(messages, tools):
        reply = provider.complete(messages, tools)
        supplied = json.loads(messages[1]["content"])
        if supplied["phase"] == "bucket":
            return reply
        findings = json.loads(reply.content)["findings"]
        joined = findings[0]
        joined["query_types"] = [label for finding in findings for label in finding["query_types"]]
        if include_both_types:
            joined["evidence"] = [ref for finding in findings for ref in finding["evidence"]]
            joined["support_kind"] = "repeated"
        return ModelReply(content=json.dumps({"findings": [joined]}))

    report = analyze(store, ScriptedProvider(*([draft] * 8)), settings)
    assert len(report.query_buckets) == 2
    if include_both_types:
        assert report.eligible_for_revision and len(report.findings) == 1
        assert set(report.findings[0].query_types) == {"Attribution", "Chronology"}
        assert {ref.run_id for ref in report.findings[0].evidence} == set(ids)
    else:
        assert not report.findings and not report.eligible_for_revision
        assert "cross_bucket:0" in report.incomplete


def test_cross_bucket_inputs_preserve_uc_and_unjudged_evidence_status(store, settings, monkeypatch):
    uc = create_run(store, 0.2, "accepted", question="Successful uncertainty")
    unjudged = create_run(store, 0.2, question="Unjudged uncertainty")
    for run_id in (uc, unjudged):
        store.event(run_id, "answer", {"text": "answer"})
    install_diagnoses(monkeypatch)
    provider = SynthesisProvider()
    report = analyze(store, provider, settings)
    assert report.eligible_for_revision
    units = [
        unit
        for request in provider.inputs
        if request["phase"] == "cross_bucket"
        for bucket in request["buckets"].values()
        for unit in bucket
    ]
    signals = {
        run_id: label for unit in units for run_id, label in unit["evidence_signals"].items()
    }
    assert signals == {uc: "UC", unjudged: "unjudged_low"}
    assert {unit["experience_cell"] for unit in units} == {"UC", "unjudged_low"}
    assert all(unit["experience_cells"] == [unit["experience_cell"]] for unit in units)


def test_multiple_cross_batches_namespace_every_id_even_prefixed_model_ids(
    store, settings, monkeypatch
):
    from evog.agents import analysis

    ids = [create_run(store, 0.9, "rejected", question=f"Question {index}") for index in range(3)]
    for run_id in ids:
        store.event(run_id, "answer", {"text": "answer"})
    install_diagnoses(monkeypatch)
    original_batches = analysis._synthesis_batches

    def batches(phase, units, *args):
        if phase == "cross_bucket":
            return [units[:2], units[2:]], []
        return original_batches(phase, units, *args)

    monkeypatch.setattr(analysis, "_synthesis_batches", batches)
    provider = SynthesisProvider()

    def draft(messages, tools):
        reply = provider.complete(messages, tools)
        if json.loads(messages[1]["content"])["phase"] == "bucket":
            return reply
        findings = json.loads(reply.content)["findings"]
        for finding, label in zip(findings, ["foo", "cross:1:foo"], strict=False):
            finding["id"] = label
        return ModelReply(content=json.dumps({"findings": findings}))

    report = analyze(store, ScriptedProvider(draft, draft, draft), settings)
    assert report.eligible_for_revision
    assert {finding.id for finding in report.findings} == {
        "cross:0:foo",
        "cross:0:cross:1:foo",
        "cross:1:foo",
    }
