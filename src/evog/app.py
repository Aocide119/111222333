"""Internal command orchestration. The supported user interface is the evog CLI."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from threading import Lock
from typing import Literal
from uuid import uuid4

from evog.agents import analysis, evolution
from evog.agents.evolution import Validator
from evog.agents.interaction import interact
from evog.core.config import Settings
from evog.core.errors import ProviderError
from evog.core.models import (
    AnalysisReport,
    Answer,
    AppliedRevision,
    EvolutionPlan,
    Feedback,
    Message,
)
from evog.core.providers import ChatProvider, ConcurrentProvider, Provider
from evog.core.store import Store


class Application:
    def __init__(
        self,
        workspace: str | Path = ".evog",
        *,
        settings: Settings | None = None,
        provider: Provider | None = None,
        judge_provider: Provider | None = None,
    ):
        self.settings = settings or Settings.load()
        self.store = Store(Path(workspace))
        self._model = provider
        self._owns_provider = provider is None
        self._provider_lock = Lock()
        self._judge = judge_provider
        self._judge_transport: ChatProvider | None = None
        self._judge_lock = Lock()

    @property
    def provider(self) -> Provider:
        with self._provider_lock:
            if self._model is None:
                self._model = ChatProvider(self.settings)
            return self._model

    @property
    def judge_provider(self) -> Provider:
        """Create the explicitly configured judge only when semantic scoring needs it."""
        with self._judge_lock:
            if self._judge is None:
                from evog.agents.demo import DemoProvider

                if isinstance(self._model, DemoProvider):
                    self._judge = self._model
                else:
                    settings = self.settings
                    if not (
                        settings.judge_base_url
                        and settings.judge_model
                        and settings.judge_api_key.get_secret_value()
                    ):
                        raise ProviderError(
                            "Semantic scoring requires explicit judge_base_url, judge_model and judge_api_key"
                        )
                    bounded = settings.model_copy(
                        update={
                            "base_url": settings.judge_base_url,
                            "model": settings.judge_model,
                            "api_key": settings.judge_api_key,
                            "reasoning_effort": settings.judge_reasoning_effort,
                            "max_output_tokens": settings.judge_max_output_tokens,
                            "request_timeout": settings.judge_timeout_seconds,
                            "call_timeout_seconds": settings.judge_timeout_seconds,
                            "call_attempts": settings.judge_call_attempts,
                            "call_retry_backoff_seconds": settings.judge_retry_backoff_seconds,
                            "temperature": settings.judge_temperature,
                            "sampling_seed": None,
                        }
                    )
                    self._judge_transport = ChatProvider(bounded)
                    self._judge = self._judge_transport
            if not isinstance(self._judge, ConcurrentProvider):
                self._judge = ConcurrentProvider(self._judge, self.settings.judge_concurrency)
            return self._judge

    def ingest(self, messages: str | Path | Iterable[Message]) -> int:
        if isinstance(messages, (str, Path)):
            return self.store.ingest_jsonl(Path(messages))
        return self.store.ingest(iter(messages))

    def ask(self, question: str, *, groups: list[str]) -> Answer:
        return interact(self.store, self.provider, self.settings, question, groups)

    def feedback(
        self, run_id: str, outcome: Literal["accepted", "rejected"], *, source: str = "user"
    ) -> None:
        self.store.feedback(Feedback(run_id=run_id, outcome=outcome, source=source))

    def selection(self) -> dict:
        return analysis.select_experience(self.store, self.settings, self.store.harness().id)

    def analyze(self) -> AnalysisReport:
        if not self.selection()["selected"]:
            # No model call is necessary for an empty selection.
            return analysis.analyze(self.store, self._model, self.settings)
        return analysis.analyze(self.store, self.provider, self.settings)

    def propose(self, report: AnalysisReport | str) -> EvolutionPlan:
        if isinstance(report, str):
            report = AnalysisReport.model_validate(self.store.artifact(report, "analysis"))
        return evolution.propose(
            self.store,
            self.provider if report.findings or report.selected_run_ids else self._model,
            self.settings,
            report,
        )

    def apply(self, plan_id: str, *, validator: Validator | None = None) -> AppliedRevision:
        return evolution.apply(self.store, plan_id, validator)

    def rollback(self, revision_id: str) -> None:
        previous = self.store.harness().id
        self.store.rollback(revision_id)
        self.store.save_artifact(uuid4().hex, "rollback", {"from": previous, "to": revision_id})

    def trace(self, run_id: str) -> dict:
        return {
            "run": self.store.run(run_id),
            "events": [event.model_dump(mode="json") for event in self.store.events(run_id)],
        }

    def export_harness(self, destination: str | Path) -> Path:
        from evog.harness.bundle import materialize_bundle

        return materialize_bundle(self.store.harness(), Path(destination).expanduser())

    def import_harness(self, source: str | Path) -> str:
        from evog.harness.bundle import load_bundle
        from evog.harness.validation import validate_components

        harness = load_bundle(Path(source).expanduser())
        validate_components(harness)
        previous = self.store.harness().id
        if harness.id != previous:
            self.store.activate(harness, previous, "component-import", "structural")
        return harness.id

    def close(self) -> None:
        with self._provider_lock:
            if self._owns_provider and isinstance(self._model, ChatProvider):
                self._model.close()
        with self._judge_lock:
            if self._judge_transport is not None:
                self._judge_transport.close()

    def __enter__(self) -> Application:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
