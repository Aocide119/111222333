"""Internal command orchestration. The supported user interface is the evog CLI."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from threading import Lock
from typing import Literal
from uuid import uuid4

from evog import analysis, evolution
from evog.config import Settings
from evog.evolution import Validator
from evog.io import atomic_write, safe_path
from evog.models import AnalysisReport, Answer, AppliedRevision, EvolutionPlan, Feedback, Message
from evog.providers import ChatProvider, Provider
from evog.runtime import interact
from evog.store import Store


class Application:
    def __init__(
        self,
        workspace: str | Path = ".evog",
        *,
        settings: Settings | None = None,
        provider: Provider | None = None,
    ):
        self.settings = settings or Settings.load()
        self.store = Store(Path(workspace))
        self._model = provider
        self._owns_provider = provider is None
        self._provider_lock = Lock()

    @property
    def provider(self) -> Provider:
        with self._provider_lock:
            if self._model is None:
                self._model = ChatProvider(self.settings)
            return self._model

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
        root = Path(destination).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        for name, text in self.store.harness().contents.items():
            atomic_write(safe_path(root, name), text)
        return root

    def close(self) -> None:
        with self._provider_lock:
            if self._owns_provider and isinstance(self._model, ChatProvider):
                self._model.close()

    def __enter__(self) -> Application:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
