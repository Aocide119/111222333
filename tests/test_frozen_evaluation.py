import json

import pytest

from evog.agents.demo import DEMO_MESSAGES, DemoProvider
from evog.app import Application
from evog.core.errors import ContractError
from evog.core.io import fingerprint
from evog.core.models import Message
from evog.core.providers import ModelReply
from evog.evaluation.campaign import run_campaign
from evog.evaluation.data import Corpus, Episode
from evog.evaluation.frozen import evaluate_frozen, evaluate_transfer
from evog.evaluation.split import create_manifest
from evog.harness.bundle import verify_bundle
from evog.harness.memory import MemoryRound
from evog.harness.tools import Tools


class Offline(DemoProvider):
    def complete(self, messages, tools):
        if messages[0]["content"].startswith("You are an expert grader"):
            return ModelReply(content='{"label":"CORRECT"}')
        if messages[0]["content"].startswith("You are a strict judge"):
            return ModelReply(content="Final: Correct")
        return super().complete(messages, tools)


@pytest.fixture
def frozen_setup(tmp_path):
    corpus = Corpus([Message.model_validate(row) for row in DEMO_MESSAGES], tmp_path)
    episodes = [
        Episode(
            benchmark="evermembench",
            episode_id=str(i),
            scope="demo",
            question_type="temporal",
            question=f"Latest release schedule {i}?",
            gold="private synthetic reference",
        )
        for i in range(2)
    ]
    manifest = create_manifest(episodes, evolution_size=1)
    evolution = [e for e in episodes if e.episode_id in manifest["evolution_ids"]]
    heldout = [e for e in episodes if e.episode_id in manifest["held_out_ids"]]
    with Application(tmp_path / "source", provider=Offline()) as app:
        campaign = run_campaign(
            app,
            evolution,
            {"demo": corpus},
            output=tmp_path / "campaign.json",
            evaluation_rounds=2,
            split_manifest=manifest,
        )
        yield app, campaign, heldout, {"demo": corpus}, manifest


def test_frozen_heldout_has_no_source_store_or_memory_writeback(frozen_setup, tmp_path):
    app, campaign, heldout, corpora, manifest = frozen_setup
    database_before = app.store.database.read_bytes()
    memory_before = {
        p.relative_to(app.store.workspace): p.read_bytes()
        for p in (app.store.workspace / "round_memory").rglob("*")
        if p.is_file()
    }
    result = evaluate_frozen(
        app, campaign, heldout, corpora, output=tmp_path / "held.json", split_manifest=manifest
    )
    assert result["status"] == "completed" and result["selection_effect"] == "none"
    assert app.store.database.read_bytes() == database_before
    assert memory_before == {
        p.relative_to(app.store.workspace): p.read_bytes()
        for p in (app.store.workspace / "round_memory").rglob("*")
        if p.is_file()
    }
    assert "private synthetic reference" not in json.dumps(result["deployment_settings"])
    assert (
        evaluate_frozen(
            app,
            campaign,
            heldout,
            corpora,
            output=tmp_path / "held.json",
            split_manifest=manifest,
            resume=True,
        )
        == result
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("model", "different"),
        ("judge_model", "different"),
        ("max_output_tokens", 1000),
        ("sampling_seed", 42),
    ],
)
def test_frozen_rejects_changed_deployment(frozen_setup, tmp_path, field, value):
    app, campaign, heldout, corpora, manifest = frozen_setup
    app.settings = app.settings.model_copy(update={field: value})
    with pytest.raises(ContractError, match="deployment settings changed"):
        evaluate_frozen(
            app, campaign, heldout, corpora, output=tmp_path / "held.json", split_manifest=manifest
        )


def test_frozen_rejects_changed_source_split_and_best(frozen_setup, tmp_path):
    app, campaign, heldout, corpora, manifest = frozen_setup
    with pytest.raises(ContractError, match="frozen split"):
        evaluate_frozen(
            app,
            campaign,
            heldout,
            corpora,
            output=tmp_path / "held.json",
            split_manifest={**manifest, "source_fingerprint": "tampered"},
        )
    tampered = json.loads(json.dumps(campaign))
    tampered["best_checkpoint"]["round_index"] = 1
    with pytest.raises(ContractError, match="evaluated selection"):
        evaluate_frozen(
            app, tampered, heldout, corpora, output=tmp_path / "held.json", split_manifest=manifest
        )


def test_frozen_rejects_changed_runtime(frozen_setup, tmp_path):
    app, campaign, heldout, corpora, manifest = frozen_setup
    with pytest.raises(ContractError, match="runtime"):
        evaluate_frozen(
            app,
            {**campaign, "runtime_source_fingerprint": "changed"},
            heldout,
            corpora,
            output=tmp_path / "held.json",
            split_manifest=manifest,
        )


def transfer_cohort(tmp_path):
    group = "groupmembench:Finance:Channel"
    corpus = Corpus(
        [Message.model_validate({**row, "group_id": group}) for row in DEMO_MESSAGES], tmp_path
    )
    episode = Episode(
        benchmark="groupmembench",
        episode_id="Finance__temporal__test",
        scope="Finance",
        question_type="temporal",
        question="Latest release schedule?",
        gold="January 15",
    )
    return [episode], {"Finance": corpus}


def workspace_bytes(workspace):
    return {
        p.relative_to(workspace).as_posix(): p.read_bytes()
        for p in workspace.rglob("*")
        if p.is_file()
    }


def test_transfer_reuses_selected_complete_workspace_without_evolution(
    frozen_setup, tmp_path, monkeypatch
):
    app, campaign, _, _, _ = frozen_setup
    episodes, corpora = transfer_cohort(tmp_path)
    source_before = workspace_bytes(app.store.workspace)
    selected = app.store.harness(campaign["best_checkpoint"]["revision_id"])
    assert app.store.harness().id != selected.id  # The final active revision is not the best one.

    def forbidden(*args, **kwargs):
        raise AssertionError("Frozen transfer must not analyze or evolve")

    monkeypatch.setattr("evog.agents.analysis.analyze", forbidden)
    monkeypatch.setattr("evog.agents.evolution.propose", forbidden)
    result = evaluate_transfer(
        app,
        campaign,
        episodes,
        corpora,
        output=tmp_path / "transfer.json",
        allow_small_cohort=True,
    )
    assert result["status"] == "completed" and result["metrics"]["passed"] == 1
    assert result["mode"] == "transfer" and result["target_benchmark"] == "groupmembench"
    assert result["revision_id"] == selected.id
    assert result["memory_snapshot_id"] == campaign["best_checkpoint"]["input_memory_snapshot_id"]
    assert result["deployment_settings"] == campaign["deployment_settings"]
    target_workspace = tmp_path / "transfer.workspace"
    assert verify_bundle(target_workspace / "workspace").contents == selected.contents
    assert set(result["component_manifest"]["files"]) == set(selected.contents)
    assert result["feedback"] == "disabled" and result["memory_writeback"] == "discarded"
    with Application(target_workspace, provider=Offline()) as target:
        assert target.store.groups() == [
            {"group_id": corpora["Finance"].group_ids[0], "messages": 2}
        ]
        assert target.store.harness().id == selected.id
        with target.store.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM feedback").fetchone()[0] == 0
            assert (
                db.execute("SELECT COUNT(*) FROM artifacts WHERE kind='analysis'").fetchone()[0]
                == 0
            )
    assert workspace_bytes(app.store.workspace) == source_before
    again = evaluate_transfer(
        app,
        campaign,
        episodes,
        corpora,
        output=tmp_path / "transfer.json",
        allow_small_cohort=True,
        resume=True,
    )
    assert again == result


def test_transfer_rejects_wrong_target_or_unacknowledged_small_cohort(frozen_setup, tmp_path):
    app, campaign, heldout, corpora, _ = frozen_setup
    with pytest.raises(ContractError, match="GroupMemBench targets"):
        evaluate_transfer(
            app, campaign, heldout, corpora, output=tmp_path / "wrong.json", allow_small_cohort=True
        )
    episodes, corpora = transfer_cohort(tmp_path)
    with pytest.raises(ContractError, match="745"):
        evaluate_transfer(app, campaign, episodes, corpora, output=tmp_path / "wrong.json")
    with pytest.raises(ContractError, match="outside the source"):
        evaluate_transfer(
            app,
            campaign,
            episodes,
            corpora,
            output=app.store.workspace / "transfer.json",
            allow_small_cohort=True,
        )


def test_transfer_resume_detects_changed_target_or_bundle(frozen_setup, tmp_path):
    app, campaign, _, _, _ = frozen_setup
    episodes, corpora = transfer_cohort(tmp_path)
    output = tmp_path / "transfer.json"
    evaluate_transfer(app, campaign, episodes, corpora, output=output, allow_small_cohort=True)
    changed = [episodes[0].model_copy(update={"gold": "changed reference"})]
    with pytest.raises(ContractError, match="inputs changed"):
        evaluate_transfer(
            app, campaign, changed, corpora, output=output, allow_small_cohort=True, resume=True
        )
    bundle_file = tmp_path / "transfer.workspace/workspace/prompt/system.md"
    bundle_file.write_text(bundle_file.read_text() + "\nChanged prompt")
    with pytest.raises(ContractError, match="manifest"):
        evaluate_transfer(
            app, campaign, episodes, corpora, output=output, allow_small_cohort=True, resume=True
        )


def test_transfer_copies_evaluated_memory_snapshot_and_keeps_group_authorization(tmp_path):
    with Application(tmp_path / "source", provider=Offline()) as app:
        corpus = Corpus([Message.model_validate(row) for row in DEMO_MESSAGES], tmp_path)
        app.ingest(iter(corpus.messages))
        initial = MemoryRound(app.store.workspace, None, "seed")
        snapshot_id = initial._save(
            {
                "schema": "evog.memory.v1",
                "parent": None,
                "scopes": {
                    fingerprint(corpus.group_ids): {
                        "groups": corpus.group_ids,
                        "files": {
                            "events/release.json": [
                                {"ref": "demo-team/001", "excerpt": "January 12"}
                            ]
                        },
                    }
                },
            }
        )
        campaign = run_campaign(
            app,
            [
                Episode(
                    benchmark="evermembench",
                    episode_id="seed",
                    scope="demo",
                    question_type="temporal",
                    question="Latest release?",
                    gold="January 15",
                )
            ],
            {"demo": corpus},
            output=tmp_path / "campaign.json",
            evaluation_rounds=1,
            memory_round_factory=lambda i, parent: MemoryRound(
                app.store.workspace, parent or snapshot_id, f"round-{i}"
            ),
        )
        episodes, corpora = transfer_cohort(tmp_path)
        result = evaluate_transfer(
            app,
            campaign,
            episodes,
            corpora,
            output=tmp_path / "transfer.json",
            allow_small_cohort=True,
        )
        assert result["memory_snapshot_id"] == snapshot_id
        target_workspace = tmp_path / "transfer.workspace"
        relative = f"round_memory/snapshots/{snapshot_id}.json"
        assert (target_workspace / relative).read_bytes() == (
            app.store.workspace / relative
        ).read_bytes()
        with Application(target_workspace, provider=Offline()) as target:
            memory = MemoryRound(target_workspace, snapshot_id, "read-probe")
            groups = corpora["Finance"].group_ids
            tools = Tools(
                target.store,
                target.store.harness(),
                groups,
                memory_session="scope-probe",
                isolated_long_term=True,
                frozen_memory=memory.binding(groups, "probe"),
            )
            assert "memory_store/long_term_memory/events/release.json" not in tools.files()
            assert set(tools.group_paths.values()) == set(groups)


def test_transfer_resume_rejects_a_workspace_symlink_before_source_writes(frozen_setup, tmp_path):
    import shutil

    app, campaign, _, _, _ = frozen_setup
    episodes, corpora = transfer_cohort(tmp_path)
    output = tmp_path / "transfer.json"
    evaluate_transfer(app, campaign, episodes, corpora, output=output, allow_small_cohort=True)
    before = workspace_bytes(app.store.workspace)
    target = tmp_path / "transfer.workspace"
    shutil.rmtree(target)
    target.symlink_to(app.store.workspace, target_is_directory=True)
    with pytest.raises(ContractError, match="symlink"):
        evaluate_transfer(
            app, campaign, episodes, corpora, output=output, allow_small_cohort=True, resume=True
        )
    assert workspace_bytes(app.store.workspace) == before


def test_transfer_uses_target_judge_and_cli(frozen_setup, tmp_path, monkeypatch, capsys):
    from evog.cli import main

    app, campaign, _, _, _ = frozen_setup
    root = tmp_path / "groupmem"
    folder = root / "data/final/Finance"
    folder.mkdir(parents=True)
    (folder / "synthetic_domain_channels_rolevariants_Finance.json").write_text(
        json.dumps(
            {
                "Channel": [
                    {
                        "msg_node": "1",
                        "content": "The release is scheduled for January 15.",
                        "author": "A",
                        "timestamp": "2025-01-01T12:00:00",
                    },
                ]
            }
        )
    )
    questions = root / "questions/Finance"
    questions.mkdir(parents=True)
    (questions / "temporal.jsonl").write_text(
        json.dumps({"id": "1", "question": "Latest release?", "answer": "January 15"}) + "\n"
    )
    checkpoint = tmp_path / "campaign.json"
    checkpoint.write_text(json.dumps(campaign))
    seen = []

    class JudgeChecked(Offline):
        def complete(self, messages, tools):
            if messages[0]["content"].startswith("You are a strict judge"):
                seen.append(messages)
            return super().complete(messages, tools)

    monkeypatch.setattr("evog.cli.Settings.load", lambda *a, **kw: app.settings)

    def offline_application(*args, **kwargs):
        return Application(*args, **{**kwargs, "provider": JudgeChecked()})

    monkeypatch.setattr("evog.cli.Application", offline_application)
    assert (
        main(
            [
                "--workspace",
                str(app.store.workspace),
                "benchmark",
                "transfer",
                "groupmembench",
                "--data-root",
                str(root),
                "--domain",
                "Finance",
                "--question-type",
                "temporal",
                "--all",
                "--allow-small-cohort",
                "--campaign-checkpoint",
                str(checkpoint),
                "--output",
                str(tmp_path / "cli-transfer.json"),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "completed"
    assert len(seen) == 1 and "January 15" in seen[0][1]["content"]
