import json

from evog.agents.demo import DEMO_MESSAGES, DemoProvider
from evog.app import Application
from evog.core.models import Message
from evog.core.providers import ModelReply
from evog.evaluation.campaign import run_campaign
from evog.evaluation.data import Corpus, Episode
from evog.evaluation.recovery import rejudge_campaign


def test_explicit_rejudge_uses_existing_answer_and_does_not_evolve(tmp_path):
    class Offline(DemoProvider):
        valid = False
        answer_calls = 0
        judge_calls = 0

        def complete(self, messages, tools):
            if messages[0]["content"].startswith("You are an expert grader"):
                self.judge_calls += 1
                return ModelReply(content='{"label":"CORRECT"}' if self.valid else "invalid")
            self.answer_calls += 1
            return super().complete(messages, tools)

    corpus = Corpus([Message.model_validate(row) for row in DEMO_MESSAGES], tmp_path)
    episodes = [
        Episode(
            benchmark="evermembench",
            episode_id="one",
            scope="demo",
            question_type="temporal",
            question="Latest release schedule?",
            gold="private synthetic reference",
        )
    ]
    output = tmp_path / "campaign.json"
    provider = Offline()
    with Application(tmp_path / "source", provider=provider) as app:
        pending = run_campaign(app, episodes, {"demo": corpus}, output=output, evaluation_rounds=2)
        assert pending["status"] == "incomplete" and provider.judge_calls == 3
        calls_before = provider.answer_calls
        provider.valid = True
        recovery = rejudge_campaign(app, output, episodes)
        assert recovery["recovered"] == 1
        assert provider.answer_calls == calls_before and provider.judge_calls == 4
        repaired = json.loads(output.read_text())
        assert repaired["status"] == "incomplete" and "analysis" not in repaired["rounds"][0]
        assert repaired["rounds"][0]["results"][0]["passed"] is True
        original_result = app.store.artifacts("benchmark_trial_result")[0]
        assert original_result["passed"] is None  # Original record remains immutable.
        done = run_campaign(
            app, episodes, {"demo": corpus}, output=output, evaluation_rounds=2, resume=True
        )
        assert done["status"] == "completed" and len(done["rounds"]) == 2
