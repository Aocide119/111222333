"""Offline research state-machine tests with durable fixture answers and plans."""

from uuid import uuid4

import pytest

from evog.agents.demo import DEMO_MESSAGES, DemoProvider
from evog.app import Application
from evog.core import identity
from evog.core.errors import ContractError, ProviderError
from evog.core.models import AnalysisReport, Answer, Change, EvolutionPlan, Message
from evog.core.providers import ModelReply
from evog.evaluation import campaign
from evog.evaluation.data import Corpus, Episode
from evog.harness.schema import Harness


@pytest.mark.parametrize(
    "relative",
    [
        "agents/interaction.py",
        "agents/prompts/analyze.md",
        "harness/base/tools/registry.yaml",
        "harness/base/tools/operations.json",
        "harness/base/harness.toml",
    ],
)
def test_campaign_fingerprint_covers_other_packages_and_component_resources(
    tmp_path, monkeypatch, relative
):
    package = tmp_path / "evog"
    module = package / "core/identity.py"
    module.parent.mkdir(parents=True)
    module.write_text("# Fixed kernel\n")
    resource = package / relative
    resource.parent.mkdir(parents=True, exist_ok=True)
    resource.write_text("original\n")
    monkeypatch.setattr(identity, "__file__", str(module))
    original = identity.runtime_source_fingerprint()
    resource.write_text("modified\n")
    assert identity.runtime_source_fingerprint() != original
    resource.write_text("original\n")
    cache = resource.parent / "__pycache__"
    cache.mkdir()
    (cache / "compiled.pyc").write_bytes(b"cache-only")
    assert identity.runtime_source_fingerprint() == original


@pytest.fixture
def setup_campaign(tmp_path, monkeypatch):
    episodes = [
        Episode(
            benchmark="evermembench",
            episode_id=str(index),
            scope="scope",
            question_type="synthetic",
            question=f"Question {index}",
            gold="Reference",
        )
        for index in range(2)
    ]
    corpus = Corpus([Message.model_validate(row) for row in DEMO_MESSAGES], tmp_path)
    app = Application(tmp_path / "workspace", provider=DemoProvider())
    calls = {"evaluate": [], "analyze": [], "propose": []}
    outcomes, changes = [], []

    def evaluate(app, episodes, corpora, harness, *, initial, checkpoint, stage, **kwargs):
        round_index = int(stage.split("_")[-1])
        calls["evaluate"].append((round_index, harness.id, [r["episode_id"] for r in initial]))
        rows = list(initial)
        for episode in episodes:
            if any(r["episode_id"] == episode.episode_id for r in rows):
                continue
            passed = outcomes[round_index][int(episode.episode_id)]
            run_id = uuid4().hex
            app.store.start_run(
                run_id, harness.id, episode.prompt(), corpora[episode.scope].group_ids
            )
            kwargs["run_started"](episode.episode_id, run_id)
            answer = Answer(
                text="Prediction",
                confidence=0.9,
                status="complete",
                citations=[],
                run_id=run_id,
                revision_id=harness.id,
            )
            app.store.finish_run(run_id, answer, "completed")
            if passed is not None:
                app.feedback(
                    run_id,
                    "accepted" if passed else "rejected",
                    source=f"{episode.benchmark}-official",
                )
            rows.append(
                {
                    "episode_id": episode.episode_id,
                    "trial_index": 1,
                    "run_id": run_id,
                    "revision_id": harness.id,
                    "status": "completed",
                    "passed": passed,
                    "question_type": episode.question_type,
                    "answer": answer.model_dump(mode="json"),
                }
            )
            checkpoint(rows.copy())
        return rows

    def analyze(store, provider, settings, revision_id, *, run_ids):
        calls["analyze"].append(run_ids)
        report = AnalysisReport(
            id=uuid4().hex,
            revision_id=revision_id,
            coverage={"total": len(run_ids)},
            selected_run_ids=run_ids,
            control_run_ids=[],
            diagnoses=[],
            findings=[],
            incomplete={},
        )
        store.save_artifact(report.id, "analysis", report.model_dump(mode="json"))
        return report

    def propose(report):
        index = len(calls["propose"])
        calls["propose"].append(report.id)
        changed = changes[index] if index < len(changes) else False
        edit = Change(
            interface="Policy",
            path="prompt/system.md",
            content=f"Prompt revision {index}",
            finding_ids=["synthetic"],
            rationale="general policy",
            expected_effect="repair",
            regression_risk="unquantified text",
            validation="check general behavior",
        )
        plan = EvolutionPlan(
            id=uuid4().hex,
            analysis_id=report.id,
            parent_revision_id=report.revision_id,
            summary="Synthetic proposal",
            changes=[edit] if changed else [],
        )
        app.store.save_artifact(plan.id, "plan", plan.model_dump(mode="json"))
        return plan

    def candidate(store, plan, report):
        contents = dict(store.harness(plan.parent_revision_id).contents)
        for change in plan.changes:
            contents[change.path] = change.content
        return Harness(contents)

    monkeypatch.setattr(campaign.benchmarks, "evaluate", evaluate)
    monkeypatch.setattr(campaign.analysis, "analyze", analyze)
    monkeypatch.setattr(app, "propose", propose)
    monkeypatch.setattr(campaign.evolution, "candidate", candidate)
    yield app, episodes, {"scope": corpus}, tmp_path / "campaign.json", outcomes, changes, calls
    app.close()


def test_six_evaluations_five_no_op_opportunities_and_initial_best(setup_campaign):
    app, episodes, corpora, output, outcomes, changes, calls = setup_campaign
    outcomes.extend([[True, False]] * 6)
    result = campaign.run_campaign(app, episodes, corpora, output=output)
    assert result["status"] == "completed" and len(result["rounds"]) == 6
    assert len(calls["evaluate"]) == len(calls["analyze"]) == 6 and len(calls["propose"]) == 5
    assert result["best_checkpoint"]["round_index"] == 0
    assert all(r["candidate_validation"]["status"] == "no_op" for r in result["rounds"][:-1])
    assert "plan" not in result["rounds"][-1]
    assert len({r["run_id"] for entry in result["rounds"] for r in entry["results"]}) == 12


def test_regression_continues_and_future_improvement_is_selected(setup_campaign):
    app, episodes, corpora, output, outcomes, changes, calls = setup_campaign
    outcomes.extend([[True, False], [False, False], [True, True]])
    changes.extend([True, True])
    result = campaign.run_campaign(app, episodes, corpora, output=output, evaluation_rounds=3)
    assert result["best_checkpoint"]["round_index"] == 2
    assert result["rounds"][1]["regressions"] == 1
    assert all(entry["activation"]["validation"] == "structural" for entry in result["rounds"][:-1])
    assert not app.store.artifacts("evaluation") and not app.store.artifacts("best_ever")
    assert [r["risk"]["assessment"] for r in result["rounds"]] == [
        "initial_zero_convention",
        "unquantified",
        "unquantified",
    ]


def test_invalid_candidate_keeps_current_harness_and_continues(setup_campaign, monkeypatch):
    app, episodes, corpora, output, outcomes, changes, calls = setup_campaign
    outcomes.extend([[True, False]] * 3)
    changes.extend([True, True])

    def invalid(*args):
        raise ContractError("Synthetic structural rejection")

    monkeypatch.setattr(campaign.evolution, "candidate", invalid)
    result = campaign.run_campaign(app, episodes, corpora, output=output, evaluation_rounds=3)
    assert result["status"] == "completed" and len(calls["evaluate"]) == 3
    assert len({r["revision_id"] for r in result["rounds"]}) == 1
    assert all(r["candidate_validation"]["status"] == "rejected" for r in result["rounds"][:-1])


def test_pending_judge_blocks_analysis_candidate_selection_and_completion(setup_campaign):
    app, episodes, corpora, output, outcomes, changes, calls = setup_campaign
    outcomes.append([True, None])
    result = campaign.run_campaign(app, episodes, corpora, output=output)
    assert result["status"] == "incomplete" and not calls["analyze"] and not calls["propose"]
    assert "best_checkpoint" not in result
    resumed = campaign.run_campaign(app, episodes, corpora, output=output, resume=True)
    assert resumed["status"] == "incomplete" and len(app.store.runs(app.store.harness().id)) == 2
    assert calls["evaluate"][-1][2] == ["0", "1"]


def test_workspace_with_product_runs_or_evaluation_is_rejected(setup_campaign):
    app, episodes, corpora, output, outcomes, changes, calls = setup_campaign
    app.store.save_artifact("product-evaluation", "evaluation", {})
    with pytest.raises(ContractError, match="separate workspace"):
        campaign.run_campaign(app, episodes, corpora, output=output)
    assert not output.exists()


@pytest.mark.parametrize(
    "phase", ["evaluated", "analysis_saved", "plan_saved", "activation_saved", "round_completed"]
)
def test_resume_retains_durable_phase_work_without_repeat(setup_campaign, monkeypatch, phase):
    app, episodes, corpora, output, outcomes, changes, calls = setup_campaign
    outcomes.extend([[True, False], [True, True]])
    changes.append(True)
    original_write = campaign._write
    interrupted = [False]

    def stop(path, payload):
        first = payload["rounds"][0] if payload["rounds"] else {}
        trigger = (
            phase == "evaluated"
            and first.get("status") == "evaluated"
            or phase == "analysis_saved"
            and app.store.artifacts("analysis")
            and "analysis" not in first
            or phase == "plan_saved"
            and app.store.artifacts("plan")
            and "plan" not in first
            or phase == "activation_saved"
            and len(app.store.revisions()) == 2
            and "activation" not in first
            or phase == "round_completed"
            and first.get("status") == "completed"
        )
        if trigger and not interrupted[0]:
            interrupted[0] = True
            # Simulate a process stopping between a durable Store operation and its JSON archive.
            if phase in {"analysis_saved", "plan_saved", "activation_saved"}:
                raise KeyboardInterrupt()
            original_write(path, payload)
            raise KeyboardInterrupt()
        original_write(path, payload)

    if phase == "analysis_saved":
        original = campaign.analysis.analyze

        def analysis_gap(*args, **kwargs):
            original(*args, **kwargs)
            raise KeyboardInterrupt()

        monkeypatch.setattr(campaign.analysis, "analyze", analysis_gap)
    elif phase == "plan_saved":
        original = app.propose

        def plan_gap(*args, **kwargs):
            original(*args, **kwargs)
            raise KeyboardInterrupt()

        monkeypatch.setattr(app, "propose", plan_gap)
    elif phase == "activation_saved":
        original = app.apply

        def activation_gap(*args, **kwargs):
            original(*args, **kwargs)
            raise KeyboardInterrupt()

        monkeypatch.setattr(app, "apply", activation_gap)
    monkeypatch.setattr(campaign, "_write", stop)
    with pytest.raises(KeyboardInterrupt):
        campaign.run_campaign(app, episodes, corpora, output=output, evaluation_rounds=2)
    monkeypatch.setattr(campaign, "_write", original_write)
    if phase == "analysis_saved":
        monkeypatch.setattr(campaign.analysis, "analyze", original)
    elif phase == "plan_saved":
        monkeypatch.setattr(app, "propose", original)
    elif phase == "activation_saved":
        monkeypatch.setattr(app, "apply", original)
    result = campaign.run_campaign(
        app, episodes, corpora, output=output, evaluation_rounds=2, resume=True
    )
    assert result["status"] == "completed"
    assert len(calls["evaluate"]) == len(calls["analyze"]) == 2 and len(calls["propose"]) == 1
    assert sum(len(app.store.runs(r["id"])) for r in app.store.revisions()) == 4
    assert len(app.store.artifacts("revision_activation")) == 1


def test_resume_recovers_uncheckpointed_answer_without_answer_replay(setup_campaign, monkeypatch):
    app, episodes, corpora, output, outcomes, changes, calls = setup_campaign
    outcomes.append([True, False])
    original_write = campaign._write
    interrupted = [False]

    def stop(path, payload):
        rows = payload["rounds"][0]["results"] if payload["rounds"] else []
        if len(rows) == 1 and not interrupted[0]:
            interrupted[0] = True
            raise KeyboardInterrupt()
        original_write(path, payload)

    monkeypatch.setattr(campaign, "_write", stop)
    with pytest.raises(KeyboardInterrupt):
        campaign.run_campaign(app, episodes, corpora, output=output, evaluation_rounds=1)
    monkeypatch.setattr(campaign, "_write", original_write)
    result = campaign.run_campaign(
        app, episodes, corpora, output=output, evaluation_rounds=1, resume=True
    )
    assert result["status"] == "completed"
    assert calls["evaluate"][-1][2] == ["0"]
    assert len(app.store.runs(app.store.harness().id)) == 2
    assert "recovery" in result["rounds"][0]["results"][0]


def test_target_stop_survives_last_round_archive_gap(setup_campaign, monkeypatch):
    app, episodes, corpora, output, outcomes, changes, calls = setup_campaign
    outcomes.append([True, True])
    original_write = campaign._write

    def stop(path, payload):
        original_write(path, payload)
        if payload["rounds"] and payload["rounds"][0]["status"] == "completed":
            raise KeyboardInterrupt()

    monkeypatch.setattr(campaign, "_write", stop)
    with pytest.raises(KeyboardInterrupt):
        campaign.run_campaign(app, episodes, corpora, output=output, target_accuracy=1)
    monkeypatch.setattr(campaign, "_write", original_write)
    result = campaign.run_campaign(
        app, episodes, corpora, output=output, target_accuracy=1, resume=True
    )
    assert result["status"] == "completed" and len(result["rounds"]) == 1
    assert len(calls["evaluate"]) == 1 and not calls["propose"]


def test_memory_checkpoint_selects_input_snapshot_and_pending_judge_never_merges(setup_campaign):
    app, episodes, corpora, output, outcomes, changes, calls = setup_campaign
    outcomes.extend([[True, True], [False, False]])
    merged = []

    class Memory:
        def __init__(self, index, parent):
            self.index, self.snapshot_id = index, parent or "cold-empty"

        def merge(self):
            merged.append(self.index)
            return f"after-round-{self.index}"

    result = campaign.run_campaign(
        app, episodes, corpora, output=output, evaluation_rounds=2, memory_round_factory=Memory
    )
    assert merged == [0, 1]
    assert result["best_checkpoint"]["input_memory_snapshot_id"] == "cold-empty"
    assert result["rounds"][1]["input_memory_snapshot_id"] == "after-round-0"
    assert result["rounds"][0]["output_memory_snapshot_id"] == "after-round-0"


def test_changed_settings_or_episode_ids_cannot_resume(setup_campaign):
    app, episodes, corpora, output, outcomes, changes, calls = setup_campaign
    outcomes.append([True, True])
    campaign.run_campaign(app, episodes, corpora, output=output, evaluation_rounds=1)
    app.settings.max_turns -= 1
    with pytest.raises(ContractError, match="inputs"):
        campaign.run_campaign(
            app, episodes, corpora, output=output, evaluation_rounds=1, resume=True
        )


def test_proposal_contract_rejection_continues(setup_campaign, monkeypatch):
    app, episodes, corpora, output, outcomes, changes, calls = setup_campaign
    outcomes.extend([[True, False]] * 2)

    def reject(report):
        raise ContractError("Final candidate fails structural checks")

    monkeypatch.setattr(app, "propose", reject)
    result = campaign.run_campaign(app, episodes, corpora, output=output, evaluation_rounds=2)
    assert result["status"] == "completed" and len(calls["evaluate"]) == 2
    assert result["rounds"][0]["candidate_validation"]["status"] == "rejected"


def test_provider_failure_is_not_an_invalid_candidate_or_no_op(setup_campaign, monkeypatch):
    app, episodes, corpora, output, outcomes, changes, calls = setup_campaign
    outcomes.append([True, False])

    def unavailable(report):
        raise ProviderError("Transport failure")

    monkeypatch.setattr(app, "propose", unavailable)
    with pytest.raises(ProviderError):
        campaign.run_campaign(app, episodes, corpora, output=output, evaluation_rounds=2)
    assert len(calls["evaluate"]) == 1
    import json

    archived = json.loads(output.read_text())
    assert archived["status"] == "incomplete"
    assert "candidate_validation" not in archived["rounds"][0]


def test_duplicate_question_text_with_distinct_episode_ids_is_supported(setup_campaign):
    app, episodes, corpora, output, outcomes, changes, calls = setup_campaign
    episodes[1] = episodes[1].model_copy(update={"question": episodes[0].question})
    outcomes.append([True, False])
    result = campaign.run_campaign(app, episodes, corpora, output=output, evaluation_rounds=1)
    assert result["status"] == "completed" and len(result["rounds"][0]["results"]) == 2


def test_completed_trial_cache_preserves_costs_across_json_gap(setup_campaign, monkeypatch):
    app, episodes, corpora, output, outcomes, changes, calls = setup_campaign
    outcomes.append([True, False])
    original = campaign._write
    interrupted = [False]

    def stop(path, payload):
        if payload["rounds"] and len(payload["rounds"][0]["results"]) == 1 and not interrupted[0]:
            interrupted[0] = True
            row = dict(payload["rounds"][0]["results"][0])
            row["answer_seconds"] = 12
            row["answer_usage"] = {"total_tokens": 17}
            identity = campaign.fingerprint(
                {"batch_id": payload["id"], "stage": "round_0", "episode_id": "0", "trial_index": 1}
            )
            app.store.save_artifact(identity, "benchmark_trial_result", row)
            raise KeyboardInterrupt()
        original(path, payload)

    monkeypatch.setattr(campaign, "_write", stop)
    with pytest.raises(KeyboardInterrupt):
        campaign.run_campaign(app, episodes, corpora, output=output, evaluation_rounds=1)
    monkeypatch.setattr(campaign, "_write", original)
    result = campaign.run_campaign(
        app, episodes, corpora, output=output, evaluation_rounds=1, resume=True
    )
    row = result["rounds"][0]["results"][0]
    assert row["answer_seconds"] == 12 and row["answer_usage"] == {"total_tokens": 17}
    assert "recovery" not in row


def test_pending_judge_never_merges_memory(setup_campaign):
    app, episodes, corpora, output, outcomes, changes, calls = setup_campaign
    outcomes.append([True, None])

    class NeverMerge:
        def __init__(self, index, parent):
            self.snapshot_id = "synthetic-empty"

        def merge(self):
            pytest.fail("Incomplete evaluation must not merge memory")

    result = campaign.run_campaign(
        app, episodes, corpora, output=output, memory_round_factory=NeverMerge
    )
    assert result["status"] == "incomplete"


def test_actual_offline_runtime_analysis_and_campaign_with_frozen_memory(tmp_path):
    class Offline(DemoProvider):
        def complete(self, messages, tools):
            if messages[0]["content"].startswith("You are an expert grader"):
                return ModelReply(content='{"label":"CORRECT"}')
            return super().complete(messages, tools)

    corpus = Corpus([Message.model_validate(row) for row in DEMO_MESSAGES], tmp_path)
    episode = Episode(
        benchmark="evermembench",
        episode_id="synthetic-offline",
        scope="demo",
        question_type="temporal",
        question="What is the latest release schedule?",
        gold="January 15",
    )
    with Application(tmp_path / "workspace", provider=Offline()) as app:
        result = campaign.run_campaign(
            app, [episode], {"demo": corpus}, output=tmp_path / "actual.json", evaluation_rounds=2
        )
        assert result["status"] == "completed"
        assert all(entry["metrics"]["report_complete"] for entry in result["rounds"])
        assert all(
            entry["input_memory_snapshot_id"] and entry["output_memory_snapshot_id"]
            for entry in result["rounds"]
        )
        assert len(result["rounds"]) == 2 and "plan" not in result["rounds"][-1]
