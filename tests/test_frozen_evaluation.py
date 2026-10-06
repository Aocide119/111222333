import json

import pytest

from evog.agents.demo import DEMO_MESSAGES, DemoProvider
from evog.app import Application
from evog.core.errors import ContractError
from evog.core.models import Message
from evog.core.providers import ModelReply
from evog.evaluation.campaign import run_campaign
from evog.evaluation.data import Corpus, Episode
from evog.evaluation.frozen import evaluate_frozen
from evog.evaluation.split import create_manifest


class Offline(DemoProvider):
    def complete(self, messages, tools):
        if messages[0]["content"].startswith("You are an expert grader"):
            return ModelReply(content='{"label":"CORRECT"}')
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
