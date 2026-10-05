import json

import pytest
from conftest import ScriptedProvider

from evog.app import Application
from evog.benchmark_data import Episode, load_corpus, load_episodes
from evog.benchmark_judge import parse_evermem, parse_groupmem, score
from evog.benchmarks import compare, metrics, run
from evog.config import Settings
from evog.demo import DemoProvider
from evog.errors import ContractError, ProviderError
from evog.providers import ModelReply


def test_multiple_choice_prompt_preserves_text_answer_protocol():
    episode = Episode(
        benchmark="evermembench",
        episode_id="choice-1",
        question="Which schedule was selected?",
        gold="private-reference-answer",
        options={"A": "Monday", "B": "Tuesday"},
        scope="01",
        question_type="choice",
    )
    prompt = episode.prompt()
    assert "option letter after FINAL ANSWER:" in prompt
    assert "CONFIDENCE" in prompt and "ANSWER BIAS" in prompt
    assert "JSON" not in prompt and episode.gold not in prompt


def test_evermem_adapter_preserves_gold_outside_messages(tmp_path):
    root = tmp_path / "evermem"
    (root / "dataset" / "01").mkdir(parents=True)
    (root / "dataset" / "01" / "dialogue.json").write_text(
        json.dumps(
            {
                "dialogues": {
                    "2025-01-01": {
                        "Group 1": [
                            {
                                "dialogue": "hello",
                                "message_index": 1,
                                "speaker": "A",
                                "time": "2025-01-01 10:00:00",
                            }
                        ]
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (root / "dataset" / "01" / "qa_01.json").write_text(
        json.dumps(
            {
                "qars": [
                    {
                        "id": "F_SH_1",
                        "Q": "What?",
                        "A": "secret-gold",
                        "R": [{"private-evidence": "gold-locator"}],
                        "options": None,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    episodes = load_episodes("evermembench", root, topics=["01"], limit=1)
    corpus = load_corpus("evermembench", root, "01")
    messages = corpus.messages
    assert len(messages) == 1 and len(episodes) == 1
    assert episodes[0].gold == "secret-gold"
    assert "secret-gold" not in corpus.messages[0].model_dump_json()
    assert "gold-locator" not in episodes[0].prompt()
    assert "secret-gold" not in episodes[0].prompt()
    assert messages[0].message_id == "2025-01-01:1"
    assert messages[0].metadata["message_index"] == "1"
    assert corpus.group_ids == [messages[0].group_id]


def test_groupmem_adapter_maps_questions_and_reply_metadata(tmp_path):
    root = tmp_path / "groupmem"
    (root / "data" / "final" / "Finance").mkdir(parents=True)
    (root / "questions" / "Finance").mkdir(parents=True)
    (
        root / "data" / "final" / "Finance" / "synthetic_domain_channels_rolevariants_Finance.json"
    ).write_text(
        json.dumps(
            {
                "Channel": [
                    {
                        "msg_node": "Msg_1",
                        "content": "decision",
                        "author": "User_1",
                        "timestamp": "2025-01-01T00:00:00",
                        "reply_to": None,
                        "role": "Owner",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (root / "questions" / "Finance" / "multi_hop.jsonl").write_text(
        json.dumps(
            {
                "id": "multi_hop_1",
                "question": "What?",
                "answer": "decision",
                "asking_user_id": "User_1",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    episodes = load_episodes(
        "groupmembench", root, domains=["Finance"], question_types=["multi_hop"], limit=1
    )
    messages = load_corpus("groupmembench", root, "Finance").messages
    assert messages[0].metadata["role"] == "Owner"
    assert episodes[0].asking_user_id == "User_1"
    assert "User_1" in episodes[0].prompt()
    assert "decision" not in episodes[0].prompt()
    assert load_corpus("groupmembench", root, "Finance").group_ids == [messages[0].group_id]


@pytest.fixture
def ever_root(tmp_path):
    root = tmp_path / "evermem"
    folder = root / "dataset" / "01"
    folder.mkdir(parents=True)
    (folder / "dialogue.json").write_text(
        json.dumps(
            {
                "dialogues": {
                    "2025-01-05": {
                        "Group 1": [
                            {
                                "dialogue": "The release is scheduled for January 12.",
                                "message_index": 1,
                                "speaker": "User_1",
                                "time": "2025-01-05 09:00:00",
                            },
                            {
                                "dialogue": "The release schedule has changed to January 15.",
                                "message_index": 2,
                                "speaker": "User_2",
                                "time": "2025-01-05 10:00:00",
                            },
                        ],
                        "Empty": None,
                    },
                    "2025-01-06": {
                        "Group 1": [
                            {
                                "dialogue": "The schedule was confirmed.",
                                "message_index": 1,
                                "speaker": "User_2",
                                "time": "2025-01-06 10:00:00",
                            }
                        ]
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    (folder / "qa_01.json").write_text(
        json.dumps(
            {
                "qars": [
                    {"id": "F_SH_Top01_001", "Q": "Latest release schedule?", "A": "January 15"},
                    {
                        "id": "MA_C_Top01_001",
                        "Q": "Which schedule?",
                        "A": "B",
                        "options": ["A. January 12", "B) January 15"],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    return root


def test_source_date_coordinates_are_unique_and_timezone_is_explicit(ever_root):
    corpus = load_corpus("evermembench", ever_root, "01", "Asia/Shanghai")
    assert len(corpus.messages) == 3
    assert len({message.ref for message in corpus.messages}) == 3
    assert len(corpus.group_ids) == 1
    assert corpus.messages[0].timestamp.isoformat().endswith("+08:00")
    assert corpus.messages[0].metadata["assumed_timezone"] == "Asia/Shanghai"


def test_episode_filter_and_multiple_choice_options(ever_root, tmp_path):
    split = tmp_path / "ids.txt"
    split.write_text("# Episode selection\nMA_C_Top01_001\n", encoding="utf-8")
    rows = load_episodes("evermembench", ever_root, episode_file=split)
    assert len(rows) == 1 and rows[0].options == {"A": "January 12", "B": "January 15"}
    assert "Options:" in rows[0].prompt()
    assert "gold" not in rows[0].prompt()
    split.write_text("missing-id\n", encoding="utf-8")
    with pytest.raises(ContractError, match="outside"):
        load_episodes("evermembench", ever_root, episode_file=split)
    with pytest.raises(ContractError, match="positive"):
        load_episodes("evermembench", ever_root, limit=0)


def test_multiple_choice_uses_direct_scoring_without_a_model():
    episode = Episode(
        benchmark="evermembench",
        episode_id="MA_C_1",
        question="Which?",
        gold="B",
        options={"A": "one", "B": "two"},
        scope="01",
        question_type="MA_C",
    )
    provider = ScriptedProvider()
    assert score(provider, episode, "Choose **B**.")["passed"] is True
    assert score(provider, episode, "A")["passed"] is False
    assert not provider.calls


@pytest.mark.parametrize(
    "content,passed",
    [
        ('Explanation\n```json\n{"label":"CORRECT"}\n```', True),
        ('{"label":"WRONG"}', False),
        ("unreadable", None),
    ],
)
def test_evermem_official_verdict_parser(content, passed):
    assert parse_evermem(content) is passed


@pytest.mark.parametrize(
    "content,passed",
    [
        ("Reasoning.\nFinal: Correct", True),
        ("Final: Incorrect", False),
        ("Final: Correct\nFinal answer: Not correct", False),
        ("unknown", None),
    ],
)
def test_groupmem_official_verdict_parser(content, passed):
    assert parse_groupmem(content) is passed


def test_judge_failures_remain_unscored():
    episode = Episode(
        benchmark="groupmembench",
        episode_id="id",
        question="What?",
        gold="secret-gold",
        scope="Finance",
        question_type="multi_hop",
    )
    provider = ScriptedProvider(ProviderError("unavailable"))
    assert score(provider, episode, "candidate")["passed"] is None
    provider = ScriptedProvider(*[ModelReply(content="unknown") for _ in range(3)])
    judged = score(provider, episode, "candidate")
    assert judged["status"] == "judge_unparseable" and judged["attempts"] == 3
    assert "secret-gold" in provider.calls[0][0][1]["content"]


@pytest.mark.parametrize("content", ["unparseable", '{"label":"CORRECT"}'])
def test_cancelled_judgment_does_not_retry_or_create_a_verdict(content):
    from threading import Event

    cancel = Event()
    episode = Episode(
        benchmark="evermembench",
        episode_id="id",
        question="What?",
        gold="expected",
        scope="01",
        question_type="F_SH",
    )

    class Cancelling:
        calls = 0

        def complete(self, messages, tools):
            self.calls += 1
            cancel.set()
            return ModelReply(content=content, usage={"total_tokens": 10})

    provider = Cancelling()
    result = score(provider, episode, "candidate", cancel_event=cancel)
    assert provider.calls == 1
    assert result["passed"] is None and result["status"] == "cancelled"
    assert result["usage"] == {"total_tokens": 10}


def test_paired_regressions_and_missing_measurements():
    before = [
        {"episode_id": str(i), "passed": passed}
        for i, passed in enumerate([False, True, True, False, None])
    ]
    after = [
        {"episode_id": str(i), "passed": passed}
        for i, passed in enumerate([True, False, True, False, True])
    ]
    paired = compare(before, after)
    assert all(
        paired[key] == 1
        for key in ["fail_to_pass", "pass_to_fail", "pass_to_pass", "fail_to_fail", "unscored"]
    )
    with pytest.raises(ContractError, match="identical"):
        compare(before, after[:-1])
    summary = metrics(before)
    assert summary["unscored"] == 1 and summary["pass_rate"] == 0.4


def test_complete_benchmark_cycle_and_persisted_validation(ever_root, tmp_path):
    class CycleProvider(DemoProvider):
        judgments = 0

        def complete(self, messages, tools):
            if messages[0]["content"].startswith("You are an expert grader"):
                self.judgments += 1
                return ModelReply(
                    content='{"label":"WRONG"}' if self.judgments == 1 else '{"label":"CORRECT"}'
                )
            return super().complete(messages, tools)

    output = tmp_path / "results.json"
    settings = Settings(max_turns=4, analysis_turns=3)
    with Application(tmp_path / "workspace", settings=settings, provider=CycleProvider()) as app:
        result = run(
            app, "evermembench", ever_root, output=output, topics=["01"], limit=1, cycle=True
        )
        assert result["status"] == "completed"
        assert result["activated"]["validation"] == "business"
        assert result["comparison"]["pass_to_fail"] == 0
        assert result["candidate"]["metrics"]["passed"] == 1
        public_row = result["baseline"]["results"][0]
        assert "question" not in public_row and "gold" not in public_row
        assert app.store.revisions()[-1]["validation"] == "business"
        assert len(list((app.store.workspace / "sessions").glob("*/memory"))) == 2
        assert (
            "January 15"
            not in app.store.run(result["baseline"]["results"][0]["run_id"])["question"]
        )
        with pytest.raises(ContractError, match="already exists"):
            run(app, "evermembench", ever_root, output=output, limit=1)
    assert json.loads(output.read_text())["evolution_status"] == "activated"


def test_unchanged_scores_do_not_activate_a_candidate(ever_root, tmp_path):
    class Unchanged(DemoProvider):
        def complete(self, messages, tools):
            if messages[0]["content"].startswith("You are an expert grader"):
                return ModelReply(content='{"label":"CORRECT"}')
            return super().complete(messages, tools)

    with Application(tmp_path / "workspace", provider=Unchanged()) as app:
        prior = app.store.harness().id
        result = run(
            app, "evermembench", ever_root, output=tmp_path / "unchanged.json", limit=1, cycle=True
        )
        assert result["evolution_status"] == "no_supported_change"
        assert "activated" not in result
        assert result["analysis"]["calibration_run_ids"]
        assert not result["analysis"]["findings"]
        assert app.store.harness().id == result["parent_revision_id"]
        assert (
            app.store.harness().id != prior
        )  # source-view initialization is a separate structural revision


def test_source_view_preserves_evolved_representation(ever_root, tmp_path):
    from evog.harness import Harness

    with Application(tmp_path / "workspace", provider=DemoProvider()) as app:
        initial = app.store.harness()
        contents = dict(initial.contents)
        contents["representation.json"] = json.dumps(
            {"timestamp_view": "both", "include_reply_refs": True, "include_metadata": False}
        )
        evolved = Harness(contents)
        app.store.activate(evolved, initial.id, "test", "structural")
        run(app, "evermembench", ever_root, output=tmp_path / "baseline.json", limit=1)
        view = app.store.harness().representation
        assert view.include_metadata
        assert view.timestamp_view == "both"
        assert view.include_reply_refs


def test_evaluation_fingerprint_uses_actual_validation_questions(ever_root, tmp_path):
    class Judgments(DemoProvider):
        def complete(self, messages, tools):
            if messages[0]["content"].startswith("You are an expert grader"):
                return ModelReply(content='{"label":"WRONG"}')
            return super().complete(messages, tools)

    folder = ever_root / "dataset" / "01"
    qa = json.loads((folder / "qa_01.json").read_text())
    qa["qars"].append({"id": "F_SH_Top01_002", "Q": "Confirmed schedule?", "A": "January 15"})
    (folder / "qa_01.json").write_text(json.dumps(qa))
    train = tmp_path / "train.txt"
    train.write_text("F_SH_Top01_001\n")
    fingerprints = []
    for i, question in enumerate(["MA_C_Top01_001", "F_SH_Top01_002"]):
        validation = tmp_path / f"validation-{i}.txt"
        validation.write_text(question + "\n")
        with Application(tmp_path / f"workspace-{i}", provider=Judgments()) as app:
            result = run(
                app,
                "evermembench",
                ever_root,
                output=tmp_path / f"cycle-{i}.json",
                episode_file=train,
                validation_episode_file=validation,
                cycle=True,
            )
            fingerprints.append(result["evaluation"]["selection_fingerprint"])
    assert fingerprints[0] != fingerprints[1]


def test_candidate_policies_require_gain_and_keep_infrastructure_unknown():
    from evog.benchmarks import acceptance

    net = {"fail_to_pass": 2, "pass_to_fail": 1, "unscored": 0}
    assert acceptance(Settings(), net)[0]
    assert not acceptance(Settings(candidate_acceptance="no_regression"), net)[0]
    assert not acceptance(Settings(), {**net, "unscored": 1})[0]
    assert not acceptance(Settings(), {**net, "fail_to_pass": 0, "pass_to_fail": 0})[0]
    before = [{"episode_id": "e", "status": "budget_exhausted", "passed": None}]
    after = [{"episode_id": "e", "status": "completed", "passed": True}]
    assert compare(before, after)["fail_to_pass"] == 1
    before[0]["status"] = "provider_failed"
    assert compare(before, after)["unscored"] == 1


def test_bounded_multi_iteration_stops_when_next_revision_is_unchanged(ever_root, tmp_path):
    class Improving(DemoProvider):
        judgments = 0

        def complete(self, messages, tools):
            if messages[0]["content"].startswith("You are an expert grader"):
                self.judgments += 1
                return ModelReply(
                    content='{"label":"WRONG"}' if self.judgments == 1 else '{"label":"CORRECT"}'
                )
            return super().complete(messages, tools)

    with Application(tmp_path / "workspace", provider=Improving()) as app:
        result = run(
            app,
            "evermembench",
            ever_root,
            output=tmp_path / "multi.json",
            limit=1,
            cycle=True,
            iterations=3,
        )
        assert len(result["iterations"]) == 2
        assert result["iterations"][0]["status"] == "activated"
        assert result["iterations"][1]["status"] == "no_supported_change"
        assert app.store.harness().id == result["activated"]["revision_id"]


def test_multiple_trials_and_resume_preserve_finished_runs(ever_root, tmp_path):
    output = tmp_path / "resume.json"

    def interrupt(message):
        if "finished 1/4" in message:
            raise KeyboardInterrupt()

    with Application(tmp_path / "workspace", provider=DemoProvider()) as app:
        with pytest.raises(KeyboardInterrupt):
            run(
                app, "evermembench", ever_root, output=output, limit=2, trials=2, progress=interrupt
            )
        first = json.loads(output.read_text())["baseline"]["results"][0]
        result = run(app, "evermembench", ever_root, output=output, limit=2, trials=2, resume=True)
        rows = result["baseline"]["results"]
        assert len(rows) == 4 and len({r["run_id"] for r in rows}) == 4
        assert rows[0]["run_id"] == first["run_id"]
        assert [(r["episode_id"], r["trial_index"]) for r in rows] == [
            ("F_SH_Top01_001", 1),
            ("F_SH_Top01_001", 2),
            ("MA_C_Top01_001", 1),
            ("MA_C_Top01_001", 2),
        ]
        assert result["baseline"]["metrics"]["questions"] == 2
        assert len(app.store.runs(result["parent_revision_id"])) == 4
        assert compare(rows, rows)["paired_trials"] == 4
        assert len(list((app.store.workspace / "sessions").glob("*/memory/long_term_memory"))) == 4
        app.settings.max_turns += 1
        with pytest.raises(ContractError, match="changed"):
            run(app, "evermembench", ever_root, output=output, limit=2, trials=2, resume=True)


def test_resume_recovers_committed_activation_without_repeating_trials(ever_root, tmp_path):
    class Improving(DemoProvider):
        judgments = 0

        def complete(self, messages, tools):
            if messages[0]["content"].startswith("You are an expert grader"):
                self.judgments += 1
                return ModelReply(
                    content='{"label":"WRONG"}' if self.judgments == 1 else '{"label":"CORRECT"}'
                )
            return super().complete(messages, tools)

    output = tmp_path / "activation.json"
    with Application(tmp_path / "workspace", provider=Improving()) as app:
        original = app.apply

        def interrupted(*args, **kwargs):
            original(*args, **kwargs)
            raise KeyboardInterrupt()

        app.apply = interrupted
        with pytest.raises(KeyboardInterrupt):
            run(app, "evermembench", ever_root, output=output, limit=1, cycle=True)
        active = app.store.harness().id
        app.apply = original
        result = run(
            app, "evermembench", ever_root, output=output, limit=1, cycle=True, resume=True
        )
        assert result["status"] == "completed"
        assert result["activated"]["revision_id"] == active
        assert app.provider.judgments == 2
        assert len(app.store.revisions()) == 3


def test_resume_preserves_unscored_failures_before_a_run_was_created(
    ever_root, tmp_path, monkeypatch
):
    from evog import benchmarks

    attempted = []

    def fail_before_run(*args, **kwargs):
        attempted.append(1)
        raise ProviderError("Provider initialization unavailable")

    def interrupt(message):
        if "finished 1/2" in message:
            raise KeyboardInterrupt()

    monkeypatch.setattr(benchmarks, "interact", fail_before_run)
    output = tmp_path / "early-failure.json"
    with Application(tmp_path / "workspace", provider=DemoProvider()) as app:
        with pytest.raises(KeyboardInterrupt):
            run(app, "evermembench", ever_root, output=output, limit=2, progress=interrupt)
        first = json.loads(output.read_text())["baseline"]["results"][0]
        assert first["status"] == "failed" and "run_id" not in first
        result = run(app, "evermembench", ever_root, output=output, limit=2, resume=True)
        assert result["baseline"]["results"][0] == first
        assert len(attempted) == 2
        assert result["baseline"]["metrics"]["unscored"] == 2


@pytest.mark.parametrize(
    "updates",
    [
        {"status": "completed"},
        {"answer": {}},
        {"passed": False},
        {"passed": "false"},
        {"run_id": 123},
    ],
)
def test_resume_rejects_malformed_trials_without_runs(ever_root, tmp_path, updates):
    from evog.benchmarks import evaluate

    with Application(tmp_path / "workspace", provider=DemoProvider()) as app:
        episodes = load_episodes("evermembench", ever_root, limit=1)
        harness = app.store.harness()
        row = {
            "episode_id": episodes[0].episode_id,
            "revision_id": harness.id,
            "trial_index": 1,
            "status": "failed",
            "passed": None,
            **updates,
        }
        with pytest.raises(ContractError, match="Checkpoint"):
            evaluate(
                app,
                episodes,
                {},
                harness,
                batch_id="b",
                stage="baseline",
                progress=lambda _: None,
                feedback=False,
                checkpoint=lambda _: None,
                initial=[row],
            )


def test_resume_rejects_manual_rollback_behind_last_activated_iteration(
    ever_root, tmp_path, monkeypatch
):
    from evog import benchmarks

    class Improving(DemoProvider):
        judgments = 0

        def complete(self, messages, tools):
            if messages[0]["content"].startswith("You are an expert grader"):
                self.judgments += 1
                return ModelReply(
                    content='{"label":"WRONG"}' if self.judgments == 1 else '{"label":"CORRECT"}'
                )
            return super().complete(messages, tools)

    original = benchmarks._write

    def stop_after_activation(path, payload):
        original(path, payload)
        if payload.get("iterations") and payload["iterations"][-1].get("status") == "activated":
            raise KeyboardInterrupt()

    monkeypatch.setattr(benchmarks, "_write", stop_after_activation)
    output = tmp_path / "manual-rollback.json"
    with Application(tmp_path / "workspace", provider=Improving()) as app:
        with pytest.raises(KeyboardInterrupt):
            run(app, "evermembench", ever_root, output=output, limit=1, cycle=True, iterations=2)
        payload = json.loads(output.read_text())
        app.rollback(payload["parent_revision_id"])
        monkeypatch.setattr(benchmarks, "_write", original)
        with pytest.raises(ContractError, match="outside the interrupted checkpoint"):
            run(
                app,
                "evermembench",
                ever_root,
                output=output,
                limit=1,
                cycle=True,
                iterations=2,
                resume=True,
            )
        assert app.provider.judgments == 2


def test_runner_resumes_after_regression_recovery_to_historical_best(
    ever_root, tmp_path, monkeypatch
):
    from evog import benchmarks, evolution
    from evog.harness import Harness
    from evog.io import dumps

    qa_path = ever_root / "dataset" / "01" / "qa_01.json"
    qa = json.loads(qa_path.read_text())
    qa["qars"][1] = {"id": "F_SH_Top01_002", "Q": "Confirmed release?", "A": "January 15"}
    qa_path.write_text(json.dumps(qa))

    class Regressing(DemoProvider):
        judgments = 0

        def complete(self, messages, tools):
            if messages[0]["content"].startswith("You are an expert grader"):
                self.judgments += 1
                return ModelReply(
                    content='{"label":"CORRECT"}' if self.judgments == 2 else '{"label":"WRONG"}'
                )
            return super().complete(messages, tools)

    output = tmp_path / "recovered.json"
    original_write = benchmarks._write
    original_evaluation = evolution.record_evaluation
    with Application(tmp_path / "workspace", provider=Regressing()) as app:
        base = app.store.harness()
        contents = dict(base.contents)
        contents["representation.json"] = dumps(
            base.representation.model_copy(update={"include_metadata": True}).model_dump()
        )
        parent = Harness(contents)
        app.store.activate(parent, base.id, "fixture-parent", "business")
        historical_contents = dict(contents)
        historical_contents["prompts/group.md"] += "\nConfirm the current status before replying."
        historical = Harness(historical_contents)
        app.store.activate(historical, parent.id, "fixture-best", "business")
        app.store.activate(parent, historical.id, "fixture-current", "business")

        def record_with_history(*args, **kwargs):
            result = original_evaluation(*args, **kwargs)
            app.store.save_artifact(
                "historical-best",
                "best_ever",
                {
                    "revision_id": historical.id,
                    "passed": 2,
                    "total": 2,
                    "pass_rate": 1.0,
                    "fully_scored": True,
                    "selection_fingerprint": result["selection_fingerprint"],
                },
            )
            return result

        def stop_after_recovery(path, payload):
            original_write(path, payload)
            if payload.get("iterations") and payload["iterations"][-1].get("recovery"):
                raise KeyboardInterrupt()

        monkeypatch.setattr(evolution, "record_evaluation", record_with_history)
        monkeypatch.setattr(benchmarks, "_write", stop_after_recovery)
        with pytest.raises(KeyboardInterrupt):
            run(app, "evermembench", ever_root, output=output, limit=2, cycle=True)
        payload = json.loads(output.read_text())
        assert payload["comparison"]["pass_to_fail"] == 1
        assert payload["iterations"][0]["status"] == "regression_rejected"
        assert app.store.harness().id == historical.id
        assert payload["iterations"][0]["recovery"]["revision_id"] == historical.id
        monkeypatch.setattr(benchmarks, "_write", original_write)
        result = run(
            app, "evermembench", ever_root, output=output, limit=2, cycle=True, resume=True
        )
        assert result["status"] == "completed"
        assert app.store.harness().id == historical.id
        assert app.provider.judgments == 4


def test_evaluation_comparability_includes_budgets_reasoning_but_excludes_credentials(
    ever_root, tmp_path
):
    class Wrong(DemoProvider):
        def complete(self, messages, tools):
            if messages[0]["content"].startswith("You are an expert grader"):
                return ModelReply(content='{"label":"WRONG"}')
            return super().complete(messages, tools)

    fingerprints = []
    variants = [
        {"max_turns": 4, "reasoning_effort": "none", "api_key": "first-test-credential"},
        {"max_turns": 5, "reasoning_effort": "none", "api_key": "first-test-credential"},
        {"max_turns": 4, "reasoning_effort": "low", "api_key": "first-test-credential"},
        {"max_turns": 4, "reasoning_effort": "none", "api_key": "second-test-credential"},
    ]
    for index, variant in enumerate(variants):
        with Application(
            tmp_path / f"workspace-{index}", settings=Settings(**variant), provider=Wrong()
        ) as app:
            result = run(
                app,
                "evermembench",
                ever_root,
                output=tmp_path / f"cycle-{index}.json",
                limit=1,
                cycle=True,
            )
            fingerprints.append(result["evaluation"]["selection_fingerprint"])
    assert fingerprints[0] != fingerprints[1]
    assert fingerprints[0] != fingerprints[2]
    assert fingerprints[0] == fingerprints[3]
