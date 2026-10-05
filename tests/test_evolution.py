import json

import pytest
from conftest import ScriptedProvider

from evog.analysis import analyze
from evog.app import Application
from evog.demo import DemoProvider
from evog.errors import ConflictError, ContractError
from evog.evolution import (
    apply,
    auto_rollback,
    best_ever_for,
    candidate,
    propose,
    record_evaluation,
)
from evog.harness import Harness
from evog.models import Feedback, PlanDraft
from evog.providers import ModelReply, ToolCall
from evog.runtime import interact


def prepare(store, settings):
    provider = DemoProvider()
    answer = interact(store, provider, settings, "Latest release?", ["demo-team"])
    store.feedback(Feedback(run_id=answer.run_id, outcome="rejected", source="test"))
    report = analyze(store, provider, settings)
    plan = propose(store, provider, settings, report)
    return report, plan


def test_revision_activate_and_rollback_preserve_sources_and_trace(store, settings):
    report, plan = prepare(store, settings)
    prior = store.harness().id
    activated = apply(store, plan.id)
    assert activated.validation == "structural"
    assert "skills/status-verification.md" in store.harness().contents
    with Application(store.workspace, provider=DemoProvider()) as group:
        group.rollback(prior)
    assert store.harness().id == prior
    assert len(store.messages("demo-team")) == 2
    assert store.events(report.selected_run_ids[0])


def test_business_validator_rejection_leaves_active_revision_unchanged(store, settings):
    _, plan = prepare(store, settings)
    prior = store.harness().id
    with pytest.raises(ContractError, match="Business validation"):
        apply(store, plan.id, validator=lambda _: False)
    assert store.harness().id == prior
    activated = apply(store, plan.id, validator=lambda harness: isinstance(harness, Harness))
    assert activated.validation == "business"


def test_validator_exception_does_not_activate(store, settings):
    _, plan = prepare(store, settings)

    def fail(_):
        raise RuntimeError("regression check crashed")

    with pytest.raises(RuntimeError):
        apply(store, plan.id, validator=fail)
    assert store.harness().id == plan.parent_revision_id


def test_stale_plan_cannot_replace_a_newer_revision(store, settings):
    report, plan = prepare(store, settings)
    other = propose(store, DemoProvider(), settings, report)
    apply(store, plan.id)
    with pytest.raises(ConflictError, match="Active revision changed"):
        apply(store, other.id)


@pytest.mark.parametrize(
    "path,interface",
    [
        ("../config.toml", "Policy"),
        ("prompts/group.md", "Operation"),
        ("tools/evil.py", "Operation"),
        ("settings.json", "Intervention"),
    ],
)
def test_candidate_rejects_outside_surfaces_and_wrong_interfaces(store, settings, path, interface):
    report, plan = prepare(store, settings)
    plan.changes[0].path = path
    plan.changes[0].interface = interface
    with pytest.raises(ContractError):
        candidate(store, plan, report)


def test_candidate_rejects_unknown_finding_or_task_identifier(store, settings):
    report, plan = prepare(store, settings)
    plan.changes[0].finding_ids = ["fabricated"]
    with pytest.raises(ContractError, match="unknown analysis"):
        candidate(store, plan, report)
    plan.changes[0].finding_ids = [report.findings[0].id]
    plan.changes[0].content += " Use demo-team/001 as the answer."
    with pytest.raises(ContractError, match="verbatim source"):
        candidate(store, plan, report)


@pytest.mark.parametrize(
    "path,interface,content",
    [
        ("representation.json", "Representation", '{"include_metadata":true}'),
        (
            "operations.json",
            "Operation",
            '{"search_mode":"all","search_limit":3,"context_window":1}',
        ),
        (
            "interventions.json",
            "Intervention",
            '{"max_citations":4,"max_answer_chars":1000,"require_citations_for_partial":true}',
        ),
        (
            "prompts/group.md",
            "Policy",
            "Use source records to verify current status and retain uncertainty.",
        ),
    ],
)
def test_all_four_interfaces_compile_into_real_runtime_settings(
    store, settings, path, interface, content
):
    report, plan = prepare(store, settings)
    plan.changes[0].path, plan.changes[0].interface, plan.changes[0].content = (
        path,
        interface,
        content,
    )
    revised = candidate(store, plan, report)
    assert revised.id != plan.parent_revision_id
    assert revised.contents[path] == content


def test_operations_cannot_exceed_fixed_runtime_ceiling(store, settings):
    report, plan = prepare(store, settings)
    plan.changes[0].path = "operations.json"
    plan.changes[0].interface = "Operation"
    plan.changes[0].content = '{"search_limit":10000}'
    with pytest.raises(ValueError):
        candidate(store, plan, report)


def test_operation_revision_can_register_a_query_tool(store, settings):
    report, plan = prepare(store, settings)
    plan.changes[0].path = "operations.json"
    plan.changes[0].interface = "Operation"
    plan.changes[0].content = json.dumps(
        {
            "query_tools": [
                {
                    "name": "query_sender",
                    "description": "Find source records by sender.",
                    "search_fields": ["sender"],
                }
            ]
        }
    )
    revised = candidate(store, plan, report)
    assert revised.operations.query_tools[0].name == "query_sender"


def test_evolution_can_inspect_analysis_validate_and_repair_an_invalid_draft(store, settings):
    report, original = prepare(store, settings)
    draft = PlanDraft(summary=original.summary, changes=original.changes).model_dump()
    bad = json.loads(json.dumps(draft))
    bad["changes"][0]["path"] = "tools/arbitrary.py"

    def checked(messages, tools):
        assert json.loads(messages[-1]["content"])["valid"]
        return ModelReply(content=json.dumps(draft))

    provider = ScriptedProvider(
        ModelReply(
            tool_calls=[
                ToolCall(
                    id="a",
                    name="read_analysis",
                    arguments={"kind": "diagnosis", "id": report.selected_run_ids[0]},
                )
            ]
        ),
        ModelReply(content=json.dumps(bad)),
        ModelReply(
            tool_calls=[ToolCall(id="v", name="validate_revision", arguments={"draft": draft})]
        ),
        checked,
    )
    prior = store.harness().id
    plan = propose(store, provider, settings, report)
    assert plan.changes and store.harness().id == prior
    assert any(r["status"] == "invalid" for r in store.artifacts("proposal_attempt"))


def test_change_associations_cannot_use_source_refs_instead_of_analysis_runs(store, settings):
    report, plan = prepare(store, settings)
    plan.changes[0].predicted_fix_runs = ["demo-team/001"]
    with pytest.raises(ContractError, match="associations"):
        candidate(store, plan, report)


def test_observed_change_effects_and_best_revision_are_auditable_associations(store, settings):
    report, plan = prepare(store, settings)
    r = report.selected_run_ids[0]
    before = [{"episode_id": "e", "run_id": r, "status": "completed", "passed": False}]
    after = [{"episode_id": "e", "run_id": "candidate", "status": "completed", "passed": True}]
    record = record_evaluation(
        store,
        plan,
        report,
        before,
        after,
        accepted=True,
        reason="improved",
        validation_kind="same_sample_regression",
        selection_fingerprint="selection",
    )
    assert record["changes"][0]["classification"] == "EFFECTIVE"
    assert record["changes"][0]["association_only"]
    assert store.artifacts("evaluation")[0]["id"] == record["id"]
    assert record["best_observed"]["passed"] == 1


def test_best_observed_includes_prior_parent_measurements(store, settings):
    report, plan = prepare(store, settings)
    store.save_artifact(
        "older",
        "evaluation",
        {
            "selection_fingerprint": "selection",
            "parent_revision_id": "older-parent",
            "candidate_revision_id": "older-candidate",
            "before_passed": 5,
            "after_passed": 4,
        },
    )
    before = [{"episode_id": str(i), "passed": i < 2} for i in range(5)]
    after = [{"episode_id": str(i), "passed": i < 3} for i in range(5)]
    record = record_evaluation(
        store,
        plan,
        report,
        before,
        after,
        accepted=True,
        reason="improved",
        validation_kind="same_sample_regression",
        selection_fingerprint="selection",
    )
    assert record["best_observed"]["revision_id"] == "older-parent"
    assert record["best_observed"]["passed"] == 5


def test_evaluation_retry_preserves_one_measurement_and_best_pointer(store, settings):
    report, plan = prepare(store, settings)
    before = [{"episode_id": "e", "passed": False}]
    after = [{"episode_id": "e", "passed": True}]
    arguments = dict(
        accepted=True,
        reason="improved",
        validation_kind="same_sample_regression",
        selection_fingerprint="retry-test",
    )
    first = record_evaluation(store, plan, report, before, after, **arguments)
    repeated = record_evaluation(store, plan, report, before, after, **arguments)
    assert repeated == first
    assert len(store.artifacts("evaluation")) == 1
    assert len(store.artifacts("best_ever")) == 1


def test_proposal_persists_a_per_change_manifest(store, settings):
    report, plan = prepare(store, settings)
    manifest = store.artifact(f"manifest:{plan.id}", "change_manifest")
    assert manifest["plan_id"] == plan.id
    assert len(manifest["changes"]) == len(plan.changes)
    assert manifest["changes"][0]["change_id"].startswith(plan.id + ":")


def test_evaluation_records_attribution_verdict_fields(store, settings):
    report, plan = prepare(store, settings)
    run_id = report.selected_run_ids[0]
    plan.changes[0].predicted_fix_runs = [run_id]
    before = [{"episode_id": "e", "run_id": run_id, "status": "completed", "passed": False}]
    after = [{"episode_id": "e", "run_id": run_id, "status": "completed", "passed": True}]
    record = record_evaluation(
        store,
        plan,
        report,
        before,
        after,
        accepted=True,
        reason="improved",
        validation_kind="same_sample_regression",
        selection_fingerprint="manifest-test",
    )
    change = record["changes"][0]
    assert change["actually_fixed"] == [run_id]
    assert change["still_failed"] == []
    assert change["risk_realized"] == []
    assert change["verdict"] == "EFFECTIVE"
    assert best_ever_for(store, "manifest-test")["revision_id"] == record["candidate_revision_id"]


def test_auto_rollback_is_explicit_and_audited(store, settings):
    report, plan = prepare(store, settings)
    activated = apply(store, plan.id)
    base = activated.parent_revision_id
    store.save_artifact(
        "manual-best",
        "best_ever",
        {
            "revision_id": base,
            "passed": 2,
            "pass_rate": 1.0,
            "total": 2,
            "fully_scored": True,
            "selection_fingerprint": "rollback-test",
        },
    )
    assert auto_rollback(store, "rollback-test") == base
    assert store.harness().id == base
    assert store.artifacts("rollback")[0]["reason"] == "best_ever_regression_recovery"


def test_auto_rollback_rejects_a_staged_unactivated_candidate(store, settings):
    report, plan = prepare(store, settings)
    revised = candidate(store, plan, report)
    with store.connect() as db:
        db.execute(
            "INSERT INTO revisions VALUES(?,?,?,?,?)",
            (
                revised.id,
                json.dumps(revised.contents),
                plan.parent_revision_id,
                plan.id,
                "structural",
            ),
        )
    store.save_artifact(
        "staged-best",
        "best_ever",
        {
            "revision_id": revised.id,
            "passed": 2,
            "pass_rate": 1.0,
            "selection_fingerprint": "staged-test",
            "fully_scored": True,
        },
    )
    assert auto_rollback(store, "staged-test") is None
    assert store.harness().id == plan.parent_revision_id


def test_partial_analysis_can_register_independently_inspected_finding(store, settings):
    report, original = prepare(store, settings)
    report = report.model_copy(update={"findings": [], "eligible_for_revision": False})
    original_finding = store.artifact(original.analysis_id, "analysis")["findings"][0]
    evidence = original_finding["evidence"][0]
    finding = {**original_finding, "id": "recovered", "support_kind": "isolated"}
    draft = PlanDraft(summary=original.summary, changes=original.changes).model_dump()
    draft["changes"][0]["finding_ids"] = ["recovered"]
    provider = ScriptedProvider(
        ModelReply(
            tool_calls=[
                ToolCall(
                    id="i",
                    name="inspect_trace",
                    arguments={
                        "run_id": evidence["run_id"],
                        "event_index": evidence["event_index"],
                        "field_path": evidence["field_path"],
                        "start_char": evidence["start_char"],
                        "limit": evidence["end_char"] - evidence["start_char"],
                    },
                )
            ]
        ),
        ModelReply(
            tool_calls=[ToolCall(id="r", name="register_finding", arguments={"finding": finding})]
        ),
        ModelReply(content=json.dumps(draft)),
    )
    recovered = propose(store, provider, settings, report)
    assert recovered.changes[0].finding_ids == ["recovered"]
    assert candidate(store, recovered, report).id != report.revision_id
    assert store.artifacts("evolution_finding")[0]["independently_inspected"]


def test_unknown_results_are_unverified_and_never_best_ever(store, settings):
    report, plan = prepare(store, settings)
    run_id = report.selected_run_ids[0]
    plan.changes[0].predicted_fix_runs = [run_id]
    record = record_evaluation(
        store,
        plan,
        report,
        [{"episode_id": "e", "run_id": run_id, "passed": None}],
        [{"episode_id": "e", "run_id": "candidate", "passed": None}],
        accepted=False,
        reason="unknown",
        validation_kind="same_sample_regression",
        selection_fingerprint="unknown-test",
    )
    assert record["changes"][0]["still_failed"] == []
    assert record["changes"][0]["unverified_runs"] == [run_id]
    assert record["best_ever"] is None
    assert best_ever_for(store, "unknown-test") is None


def test_recovered_findings_require_current_session_trace_inspection(store, settings):
    report, _ = prepare(store, settings)
    finding = report.findings[0].model_copy(update={"id": "fabricated", "support_kind": "isolated"})

    def check_rejection(messages, tools):
        result = json.loads(messages[-1]["content"])
        assert result["valid"] is False
        assert "independently inspected" in result["error"]
        return ModelReply(content='{"summary":"Unsupported","changes":[]}')

    provider = ScriptedProvider(
        ModelReply(
            tool_calls=[
                ToolCall(
                    id="r", name="register_finding", arguments={"finding": finding.model_dump()}
                )
            ]
        ),
        check_rejection,
    )
    propose(store, provider, settings, report)
    assert not store.artifacts("evolution_finding")


@pytest.mark.parametrize("same_question", [True, False])
def test_recovered_repeated_support_counts_questions_not_trials(store, settings, same_question):
    report, _ = prepare(store, settings)
    first = report.selected_run_ids[0]
    second = interact(
        store,
        DemoProvider(),
        settings,
        store.run(first)["question"] if same_question else "Was the release completed?",
        ["demo-team"],
    )
    store.feedback(Feedback(run_id=second.run_id, outcome="rejected", source="test"))
    report = report.model_copy(update={"selected_run_ids": [first, second.run_id]})
    finding = report.findings[0].model_dump()
    finding.update(id="repeated-recovered", support_kind="repeated")
    ref = finding["evidence"][0]

    def register(messages, tools):
        finding["evidence"] = [
            json.loads(message["content"])["evidence_ref"]
            for message in messages
            if message["role"] == "tool"
        ]
        return ModelReply(
            tool_calls=[
                ToolCall(id="register", name="register_finding", arguments={"finding": finding})
            ]
        )

    def finish(messages, tools):
        result = json.loads(messages[-1]["content"])
        assert result.get("registered", False) is (not same_question)
        if same_question:
            assert "independent question support" in result["error"]
        return ModelReply(content='{"summary":"Reviewed evidence","changes":[]}')

    propose(
        store,
        ScriptedProvider(
            ModelReply(
                tool_calls=[
                    ToolCall(
                        id=f"read-{i}",
                        name="inspect_trace",
                        arguments={
                            "run_id": run_id,
                            "event_index": ref["event_index"],
                            "field_path": ref["field_path"],
                            "start_char": ref["start_char"],
                            "limit": ref["end_char"] - ref["start_char"],
                        },
                    )
                    for i, run_id in enumerate([first, second.run_id])
                ]
            ),
            register,
            finish,
        ),
        settings,
        report,
    )
    assert bool(store.artifacts("evolution_finding")) is (not same_question)


def test_recovered_error_finding_cannot_use_accepted_calibration_as_failure(store, settings):
    report, _ = prepare(store, settings)
    run_id = report.selected_run_ids[0]
    store.feedback(Feedback(run_id=run_id, outcome="accepted", source="test"))
    finding = report.findings[0].model_dump()
    finding.update(id="false-error", support_kind="isolated", purpose="error_repair")
    ref = finding["evidence"][0]

    def finish(messages, tools):
        result = json.loads(messages[-1]["content"])
        assert not result["valid"]
        assert "declared purpose" in result["error"]
        return ModelReply(content='{"summary":"No observed failure","changes":[]}')

    propose(
        store,
        ScriptedProvider(
            ModelReply(
                tool_calls=[
                    ToolCall(
                        id="read",
                        name="inspect_trace",
                        arguments={
                            "run_id": run_id,
                            "event_index": ref["event_index"],
                            "field_path": ref["field_path"],
                            "start_char": ref["start_char"],
                            "limit": ref["end_char"] - ref["start_char"],
                        },
                    )
                ]
            ),
            ModelReply(
                tool_calls=[
                    ToolCall(id="register", name="register_finding", arguments={"finding": finding})
                ]
            ),
            finish,
        ),
        settings,
        report,
    )
    assert not store.artifacts("evolution_finding")


def test_evaluation_pairs_trials_without_overwriting_another_trial(store, settings):
    report, plan = prepare(store, settings)
    run_id = report.selected_run_ids[0]
    plan.changes[0].predicted_fix_runs = [run_id]
    record = record_evaluation(
        store,
        plan,
        report,
        [
            {"episode_id": "e", "trial_index": 1, "run_id": run_id, "passed": False},
            {"episode_id": "e", "trial_index": 2, "run_id": "trial-two", "passed": False},
        ],
        [
            {"episode_id": "e", "trial_index": 1, "passed": True},
            {"episode_id": "e", "trial_index": 2, "passed": False},
        ],
        accepted=True,
        reason="improved",
        validation_kind="same_sample_regression",
        selection_fingerprint="trial-test",
    )
    assert record["changes"][0]["actually_fixed"] == [run_id]
    assert record["metric_unit"] == "trial"
    assert record["best_ever"]["pass_rate"] == 0.5


def test_auto_rollback_uses_best_activated_observation_when_best_candidate_is_staged(
    store, settings
):
    report, plan = prepare(store, settings)
    activated = apply(store, plan.id)
    store.rollback(activated.parent_revision_id)
    store.save_artifact(
        "activated-observation",
        "evaluation",
        {
            "id": "activated-observation",
            "selection_fingerprint": "recovery-test",
            "parent_revision_id": activated.parent_revision_id,
            "candidate_revision_id": activated.revision_id,
            "before_passed": 1,
            "before_total": 3,
            "before_fully_scored": True,
            "after_passed": 2,
            "after_total": 3,
            "after_fully_scored": True,
        },
    )
    contents = dict(store.harness().contents)
    contents["prompts/group.md"] += "\nConfirm ordering before summarizing."
    staged = Harness(contents)
    with store.connect() as db:
        db.execute(
            "INSERT INTO revisions VALUES(?,?,?,?,?)",
            (
                staged.id,
                json.dumps(staged.contents),
                activated.parent_revision_id,
                "staged",
                "structural",
            ),
        )
    store.save_artifact(
        "unactivated-best",
        "best_ever",
        {
            "revision_id": staged.id,
            "passed": 3,
            "total": 3,
            "pass_rate": 1.0,
            "fully_scored": True,
            "selection_fingerprint": "recovery-test",
        },
    )
    assert best_ever_for(store, "recovery-test")["revision_id"] == staged.id
    assert auto_rollback(store, "recovery-test") == activated.revision_id
    assert store.harness().id == activated.revision_id
