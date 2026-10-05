"""CLI benchmark execution over the product runtime, with isolated question notes."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from threading import Event
from typing import Any
from uuid import uuid4

from evog import analysis, evolution
from evog.app import Application
from evog.benchmark_data import BenchmarkName, Corpus, Episode, load_corpus, load_episodes
from evog.benchmark_judge import score
from evog.benchmark_metrics import research_metrics, token_total
from evog.config import Settings
from evog.errors import ContractError, EvoGError, ProviderError, RunFailed
from evog.harness import Harness
from evog.io import atomic_write, dumps, fingerprint
from evog.memory import MemoryRound
from evog.models import AnalysisReport, AppliedRevision, EvolutionPlan
from evog.runtime import interact

Progress = Callable[[str], None]


def _trial_key(row: dict) -> tuple[str, int]:
    return row["episode_id"], row.get("trial_index", 1)


def _write(path: Path, payload: dict) -> None:
    atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _validate_resume_state(app: Application, payload: dict) -> None:
    """Require the active revision produced by the checkpoint's latest transition."""
    expected = payload.get("parent_revision_id")
    if not expected:
        return
    with app.store.connect() as db:
        transitions = [
            (row["kind"], json.loads(row["payload"]))
            for row in db.execute(
                "SELECT kind,payload FROM artifacts WHERE kind IN ('revision_activation','rollback')"
            )
        ]
    pending_candidate = None
    entries = sorted(payload.get("iterations", []), key=lambda entry: entry["iteration"])
    for entry in entries:
        if entry.get("parent_revision_id") != expected:
            raise ContractError("Checkpoint iteration has inconsistent revision lineage")
        pending_candidate = None
        if entry.get("plan"):
            report = AnalysisReport.model_validate(entry["analysis"])
            plan = EvolutionPlan.model_validate(entry["plan"])
            revised = evolution.candidate(app.store, plan, report)
            if entry.get("status") == "activated":
                activated = AppliedRevision.model_validate(entry["activated"])
                if (
                    activated.revision_id != revised.id
                    or activated.parent_revision_id != expected
                    or activated.plan_id != plan.id
                    or activated.validation != "business"
                ):
                    raise ContractError("Checkpoint activation is inconsistent with its plan")
                expected = revised.id
                continue
            if any(
                kind == "revision_activation"
                and row.get("revision_id") == revised.id
                and row.get("parent_revision_id") == expected
                and row.get("plan_id") == plan.id
                and row.get("validation") == "business"
                for kind, row in transitions
            ):
                pending_candidate = revised.id
        recovery = entry.get("recovery", {}).get("revision_id")
        if recovery and recovery != expected:
            selection = entry.get("evaluation", {}).get("selection_fingerprint")
            if not selection or not any(
                kind == "rollback"
                and row.get("from") == expected
                and row.get("to") == recovery
                and row.get("selection_fingerprint") == selection
                and row.get("reason") == "best_ever_regression_recovery"
                for kind, row in transitions
            ):
                raise ContractError("Checkpoint recovery has no matching rollback record")
            expected = recovery
            pending_candidate = None
    if app.store.harness().id not in {expected, pending_candidate}:
        raise ContractError("Active revision is outside the interrupted checkpoint transition")


def metrics(results: list[dict]) -> dict:
    passed = sum(row["passed"] is True for row in results)
    failed = sum(row["passed"] is False for row in results)
    total = len(results)
    questions: dict[str, list[bool | None]] = {}
    for row in results:
        questions.setdefault(row["episode_id"], []).append(row["passed"])
    question_passed = sum(all(v is True for v in trials) for trials in questions.values())
    question_unscored = sum(any(v is None for v in trials) for trials in questions.values())
    usage: dict[str, int] = {}
    for row in results:
        for stage in ("agent_usage", "judge_usage"):
            for key, value in row.get(stage, {}).items():
                usage[key] = usage.get(key, 0) + value
    return {
        "total": total,
        "metric_unit": "trial",
        "questions": len(questions),
        "all_trials_passed": question_passed,
        "question_unscored": question_unscored,
        "question_pass_rate": question_passed / len(questions) if questions else None,
        "passed": passed,
        "failed": failed,
        "unscored": total - passed - failed,
        "execution_failed": sum(
            row.get("status") in {"budget_exhausted", "contract_failed"} for row in results
        ),
        "infrastructure_incidents": sum(
            row.get("status") in {"provider_failed", "tool_failed", "incident"} for row in results
        ),
        "pass_rate": passed / total if total else None,
        "scored_pass_rate": passed / (passed + failed) if passed + failed else None,
        "tool_calls": sum(row.get("tool_calls", 0) for row in results),
        "usage": usage,
    }


def compare(before: list[dict], after: list[dict]) -> dict:
    left, right = (
        {_trial_key(row): row for row in before},
        {_trial_key(row): row for row in after},
    )
    if len(left) != len(before) or len(right) != len(after) or left.keys() != right.keys():
        raise ContractError("Paired comparison requires unique, identical episode IDs")
    counts = {
        name: 0
        for name in ("fail_to_pass", "pass_to_fail", "pass_to_pass", "fail_to_fail", "unscored")
    }
    for key in left:
        a, b = evolution.validation_outcome(left[key]), evolution.validation_outcome(right[key])
        if a is None or b is None:
            counts["unscored"] += 1
        else:
            counts[("pass" if a else "fail") + "_to_" + ("pass" if b else "fail")] += 1
    return {
        "paired_episodes": len({key[0] for key in left}),
        "paired_trials": len(left),
        **counts,
    }


def acceptance(settings: Settings, paired: dict) -> tuple[bool, str]:
    if paired["unscored"]:
        return False, "Unresolved judge or infrastructure outcomes prevent business validation"
    if settings.candidate_acceptance == "no_regression":
        if paired["pass_to_fail"]:
            return False, "The no-regression policy rejects lost passing interactions"
        return (
            paired["fail_to_pass"] > 0,
            "Verified improvement" if paired["fail_to_pass"] else "No verified improvement",
        )
    gain = paired["fail_to_pass"] - paired["pass_to_fail"]
    return gain > 0, "Verified net improvement" if gain > 0 else "No verified net improvement"


def _evaluate_one(
    app: Application,
    episode: Episode,
    corpora: dict[str, Corpus],
    harness: Harness,
    *,
    batch_id: str,
    stage: str,
    feedback: bool,
    cancel_event: Event | None,
    trial_index: int = 1,
    memory_round: MemoryRound | None = None,
    run_started: Callable[[str, str], None] | None = None,
) -> dict:
    started = time.monotonic()
    row: dict[str, Any] = {
        "episode_id": episode.episode_id,
        "trial_index": trial_index,
        "scope": episode.scope,
        "question_type": episode.question_type,
        "passed": None,
        "revision_id": harness.id,
    }
    try:
        answer = interact(
            app.store,
            app.provider,
            app.settings,
            episode.prompt(),
            corpora[episode.scope].group_ids,
            harness,
            memory_session=f"{batch_id}/{stage}/{episode.episode_id}/{trial_index}/{uuid4().hex}",
            isolated_long_term=True,
            cancel_event=cancel_event,
            frozen_memory=memory_round.binding(corpora[episode.scope].group_ids, episode.episode_id)
            if memory_round
            else None,
            run_started=(lambda run_id: run_started(episode.episode_id, run_id))
            if run_started
            else None,
        )
        row.update(
            {
                "run_id": answer.run_id,
                "answer": answer.model_dump(mode="json"),
                "status": "completed",
            }
        )
        judge_started = time.monotonic()
        try:
            judged = (
                score(
                    app.provider if episode.options else app.judge_provider,
                    episode,
                    answer.text,
                    cancel_event=cancel_event,
                    timeout_seconds=app.settings.judge_timeout_seconds,
                )
                if cancel_event is None or not cancel_event.is_set()
                else {"passed": None, "status": "cancelled", "usage": {}}
            )
        except ProviderError:
            judged = {"passed": None, "status": "judge_unavailable", "usage": {}}
        row["judge_seconds"] = round(time.monotonic() - judge_started, 3)
        row.update(
            {
                "passed": judged["passed"],
                "judge": judged,
                "judge_usage": judged.get("usage", {}),
                "judge_tokens": (
                    0
                    if episode.options
                    else _measured_tokens(judged.get("usage_calls", []))
                    if judged.get("usage_complete")
                    else None
                ),
            }
        )
        if (
            feedback
            and judged["passed"] is not None
            and (cancel_event is None or not cancel_event.is_set())
        ):
            app.feedback(
                answer.run_id,
                "accepted" if judged["passed"] else "rejected",
                source=f"{episode.benchmark}-official",
            )
    except RunFailed as exc:
        row.update(
            {
                "run_id": exc.run_id,
                "status": app.store.run(exc.run_id)["status"],
                "error": str(exc),
            }
        )
    except EvoGError as exc:
        row.update({"status": "failed", "error": str(exc)})
    events = app.store.events(row["run_id"]) if row.get("run_id") else []
    usage: dict[str, int] = {}
    answer_usage: dict[str, int] = {}
    reflection_usage: dict[str, int] = {}
    for event in events:
        if event.kind in ("model", "reflection") or (
            event.kind == "error" and event.data.get("code") == "reflection_unavailable"
        ):
            for key, value in event.data.get("usage", {}).items():
                usage[key] = usage.get(key, 0) + value
                stage_usage = answer_usage if event.kind == "model" else reflection_usage
                stage_usage[key] = stage_usage.get(key, 0) + value
    model_costs = [event.data.get("usage", {}) for event in events if event.kind == "model"]
    reflection_events = [
        event
        for event in events
        if event.kind == "reflection"
        or (event.kind == "error" and event.data.get("code") == "reflection_unavailable")
    ]
    row.update(
        {
            "seconds": round(time.monotonic() - started, 3),
            "agent_usage": usage,
            "answer_usage": answer_usage,
            "reflection_usage": reflection_usage,
            "answer_tokens": _measured_tokens(model_costs),
            "answer_usage_call_count": len(model_costs),
            "reflection_tokens": _measured_tokens(
                [e.data.get("usage", {}) for e in reflection_events]
            )
            if all(e.data.get("usage_complete", True) for e in reflection_events)
            else None,
            "reflection_usage_call_count": len(reflection_events),
            "answer_seconds": next(
                (
                    event.data["elapsed_seconds"]
                    for event in events
                    if event.kind == "budget" and event.data.get("phase") == "answered"
                ),
                None,
            ),
            "reflection_seconds": sum(
                event.data.get("seconds", 0)
                for event in events
                if event.kind == "reflection"
                or (event.kind == "error" and event.data.get("code") == "reflection_unavailable")
            ),
            "tool_calls": sum(
                event.kind == "tool" and event.data.get("executed", True) for event in events
            ),
            "tool_rounds": next(
                (
                    event.data["tool_rounds"]
                    for event in reversed(events)
                    if event.kind == "budget" and "tool_rounds" in event.data
                ),
                0,
            ),
        }
    )
    app.store.save_artifact(
        fingerprint(
            {
                "batch_id": batch_id,
                "stage": stage,
                "episode_id": episode.episode_id,
                "trial_index": trial_index,
            }
        ),
        "benchmark_trial_result",
        row,
    )
    return row


def _measured_tokens(calls: list[dict[str, int]]) -> int | None:
    totals = [token_total(usage) for usage in calls]
    return sum(totals) if all(total is not None for total in totals) else None


def evaluate(
    app: Application,
    episodes: list[Episode],
    corpora: dict[str, Corpus],
    harness: Harness,
    *,
    batch_id: str,
    stage: str,
    progress: Progress,
    feedback: bool,
    checkpoint: Callable[[list[dict]], None],
    trials: int = 1,
    initial: list[dict] | None = None,
    memory_round: MemoryRound | None = None,
    run_started: Callable[[str, str], None] | None = None,
) -> list[dict]:
    if not 1 <= trials <= 10:
        raise ContractError("Trials per question must be within 1–10")
    jobs = [(e, i) for e in episodes for i in range(1, trials + 1)]
    positions = {(e.episode_id, i): n for n, (e, i) in enumerate(jobs)}
    results = list(initial or [])
    existing_keys = {_trial_key(row) for row in results}
    for episode, trial in jobs:
        key = (episode.episode_id, trial)
        if key in existing_keys:
            continue
        identity = fingerprint(
            {
                "batch_id": batch_id,
                "stage": stage,
                "episode_id": episode.episode_id,
                "trial_index": trial,
            }
        )
        try:
            cached = app.store.artifact(identity, "benchmark_trial_result")
        except ContractError:
            continue
        results.append(cached)
    for row in results:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("episode_id"), str)
            or type(row.get("trial_index", 1)) is not int
            or "passed" not in row
            or row.get("passed") is not None
            and type(row.get("passed")) is not bool
            or row.get("revision_id") != harness.id
            or row.get("run_id") is not None
            and not isinstance(row.get("run_id"), str)
        ):
            raise ContractError("Checkpoint contains a malformed trial")
        if not row.get("run_id"):
            if (
                row.get("status") != "failed"
                or row.get("passed") is not None
                or row.get("answer") is not None
            ):
                raise ContractError(
                    "Checkpoint trial without a run must be an unscored terminal failure"
                )
        elif app.store.run(row["run_id"])["revision_id"] != harness.id:
            raise ContractError("Checkpoint trial belongs to a different revision")
    completed_keys = {_trial_key(r) for r in results}
    if len(completed_keys) != len(results) or not completed_keys.issubset(positions):
        raise ContractError("Checkpoint has duplicate or unknown trials")
    results.sort(key=lambda r: positions[_trial_key(r)])
    checkpoint(list(results))
    jobs = [(e, i) for e, i in jobs if (e.episode_id, i) not in completed_keys]
    cancel = Event()

    def worker(job: tuple[Episode, int]) -> dict:
        episode, trial_index = job
        progress(f"{stage} started {episode.episode_id} trial={trial_index}")
        return _evaluate_one(
            app,
            episode,
            corpora,
            harness,
            batch_id=batch_id,
            stage=stage,
            feedback=feedback,
            cancel_event=cancel,
            trial_index=trial_index,
            memory_round=memory_round,
            run_started=run_started,
        )

    def record(row: dict) -> None:
        results.append(row)
        results.sort(key=lambda r: positions[_trial_key(r)])
        checkpoint(list(results))
        progress(
            f"{stage} finished {len(results)}/{len(positions)}: {row['episode_id']} "
            f"status={row['status']} passed={row['passed']} tools={row['tool_calls']}"
        )

    if app.settings.benchmark_concurrency == 1:
        for job in jobs:
            record(worker(job))
    else:
        pool = ThreadPoolExecutor(max_workers=app.settings.benchmark_concurrency)
        todo = iter(jobs)
        futures = {
            pool.submit(worker, e)
            for e in [next(todo) for _ in range(min(len(jobs), app.settings.benchmark_concurrency))]
        }
        try:
            while futures:
                completed, futures = wait(futures, return_when=FIRST_COMPLETED)
                for future in completed:
                    record(future.result())
                for _ in completed:
                    episode = next(todo, None)
                    if episode is not None:
                        futures.add(pool.submit(worker, episode))
        except BaseException:
            cancel.set()
            raise
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
    return results


def run(
    app: Application,
    name: BenchmarkName,
    root: Path,
    *,
    output: Path,
    topics: list[str] | None = None,
    domains: list[str] | None = None,
    question_types: list[str] | None = None,
    limit: int | None = 2,
    episode_file: Path | None = None,
    assumed_timezone: str = "UTC",
    cycle: bool = False,
    iterations: int = 1,
    validation_episode_file: Path | None = None,
    trials: int = 1,
    resume: bool = False,
    progress: Progress = lambda _: None,
) -> dict:
    if not 1 <= iterations <= 10 or (iterations != 1 and not cycle):
        raise ContractError("Use 1–10 iterations with an evolution cycle")
    if iterations > 1 and validation_episode_file:
        raise ContractError(
            "Repeated evolution requires a fresh independent validation set per cycle; use separate held-out runs"
        )
    if not 1 <= trials <= 10:
        raise ContractError("Trials per question must be within 1–10")
    if output.exists() and not resume:
        raise ContractError("Output already exists; use a new path to preserve prior results")
    if resume and not output.exists():
        raise ContractError("Resume requires an existing checkpoint")
    episodes = load_episodes(
        name,
        root,
        topics=topics,
        domains=domains,
        question_types=question_types,
        limit=limit,
        episode_file=episode_file,
    )
    validation = None
    if validation_episode_file:
        validation = load_episodes(
            name,
            root,
            topics=topics,
            domains=domains,
            question_types=question_types,
            episode_file=validation_episode_file,
        )
        if {row.episode_id for row in episodes} & {row.episode_id for row in validation}:
            raise ContractError("Validation and evolution episode IDs must be disjoint")
    deployment_settings = app.settings.model_dump(mode="json", exclude={"api_key", "judge_api_key"})
    runtime_source = {
        path.relative_to(Path(__file__).parent).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(Path(__file__).parent.rglob("*"))
        if path.is_file() and path.suffix in {".py", ".md"}
    }
    runtime_source_fingerprint = fingerprint(runtime_source)
    signature = fingerprint(
        {
            "benchmark": name,
            "workspace": str(app.store.workspace),
            "root": str(root.resolve()),
            "episodes": [row.model_dump() for row in episodes],
            "validation": [row.model_dump() for row in validation or []],
            "settings": deployment_settings,
            "timezone": assumed_timezone,
            "cycle": cycle,
            "iterations": iterations,
            "trials": trials,
            "runtime": runtime_source_fingerprint,
        }
    )
    payload: dict[str, Any] = (
        json.loads(output.read_text(encoding="utf-8"))
        if resume
        else {
            "resume_signature": signature,
            "schema": "evog.benchmark.v2",
            "trials_per_question": trials,
            "requested_iterations": iterations,
            "id": uuid4().hex,
            "benchmark": name,
            "status": "preparing",
            "model": app.settings.model,
            "base_url": app.settings.base_url,
            "deployment_settings": deployment_settings,
            "runtime_source_fingerprint": runtime_source_fingerprint,
            "assumed_timezone": assumed_timezone,
            "episode_ids": [row.episode_id for row in episodes],
            "selection_fingerprint": fingerprint([row.model_dump() for row in episodes]),
            "judge_protocol": "official",
            "note_isolation": "fresh-per-question-per-stage",
            "validation_kind": "held_out" if validation else "same_sample_regression",
            "corpora": {},
        }
    )
    if resume:
        if payload.get("resume_signature") != signature:
            raise ContractError(
                "Checkpoint inputs, deployment settings, workspace or runtime changed"
            )
        if payload.get("status") == "completed":
            return payload
        _validate_resume_state(app, payload)
        progress("Resuming checkpoint; completed trials and iteration artifacts are retained")
    batch_id = payload["id"]
    _write(output, payload)
    corpora: dict[str, Corpus] = {}
    all_episodes = episodes + (validation or [])
    for scope in sorted({episode.scope for episode in all_episodes}):
        corpus = load_corpus(name, root, scope, assumed_timezone)
        recorded = payload["corpora"].get(scope)
        if recorded and recorded["fingerprint"] != corpus.fingerprint:
            raise ContractError("Corpus changed since the checkpoint")
        imported = app.ingest(iter(corpus.messages))
        corpora[scope] = corpus
        payload["corpora"][scope] = {
            "fingerprint": corpus.fingerprint,
            "messages": len(corpus.messages),
            "imported": imported,
            "groups": len(corpus.group_ids),
        }
        progress(
            f"Imported {scope}: {len(corpus.messages)} messages / {len(corpus.group_ids)} groups"
        )
    # Evaluate the explicit starting harness without mutating its representation.
    active = app.store.harness(payload.get("parent_revision_id")) if resume else app.store.harness()
    payload["parent_revision_id"] = active.id

    def execute(
        stage: str, selected: list[Episode], harness: Harness, attach_feedback: bool
    ) -> list[dict]:
        payload["status"] = stage
        _write(output, payload)

        def checkpoint(rows: list[dict]) -> None:
            payload[stage] = {"results": rows, "metrics": metrics(rows)}
            if trials == 1:
                payload[stage]["research_metrics"] = research_metrics(
                    rows, [episode.episode_id for episode in selected]
                )
            _write(output, payload)

        return evaluate(
            app,
            selected,
            corpora,
            harness,
            batch_id=batch_id,
            stage=stage,
            progress=progress,
            feedback=attach_feedback,
            checkpoint=checkpoint,
            trials=trials,
            initial=payload.get(stage, {}).get("results", []) if resume else None,
        )

    baseline = execute("baseline", episodes, active, True)
    if cycle:
        payload.setdefault("iterations", [])
        experience = baseline
        before = baseline
        for iteration in range(1, iterations + 1):
            entry = next((e for e in payload["iterations"] if e["iteration"] == iteration), None)
            if entry and entry.get("status") == "activated":
                experience = before = entry["candidate"]["results"]
                for row in experience:
                    if row.get("run_id") and row["passed"] is not None:
                        recorded = next(
                            r
                            for r in app.store.runs(row["revision_id"])
                            if r["id"] == row["run_id"]
                        )
                        expected = "accepted" if row["passed"] else "rejected"
                        if recorded["outcome"] != expected:
                            app.feedback(row["run_id"], expected, source=f"{name}-official")
                continue
            if entry and entry.get("status") in {"regression_rejected", "no_supported_change"}:
                break
            active = (
                app.store.harness(entry["parent_revision_id"]) if entry else app.store.harness()
            )
            if entry and entry.get("analysis"):
                report = AnalysisReport.model_validate(entry["analysis"])
            else:
                payload["status"] = "analysis"
                _write(output, payload)
                progress("Analyzing selected experience")
                report = analysis.analyze(
                    app.store,
                    app.provider,
                    app.settings,
                    active.id,
                    run_ids=[r["run_id"] for r in experience if r.get("run_id")],
                )
                entry = {
                    "iteration": iteration,
                    "parent_revision_id": active.id,
                    "analysis": report.model_dump(mode="json"),
                }
                payload["iterations"].append(entry)
                payload["analysis"] = entry["analysis"]
                _write(output, payload)
            progress("Proposing an evidence-linked harness revision")
            plan = (
                EvolutionPlan.model_validate(entry["plan"])
                if entry.get("plan")
                else app.propose(report)
            )
            payload["plan"] = entry["plan"] = plan.model_dump(mode="json")
            _write(output, payload)
            if not plan.changes:
                payload["evolution_status"] = entry["status"] = "no_supported_change"
                break
            revised = evolution.candidate(app.store, plan, report)
            with app.store.connect() as db:
                db.execute(
                    "INSERT OR IGNORE INTO revisions VALUES(?,?,?,?,?)",
                    (revised.id, dumps(revised.contents), active.id, plan.id, "structural"),
                )
            checking = validation or episodes
            if validation:
                before = execute("validation_baseline", checking, active, False)
            stage = "candidate" if iteration == 1 else f"candidate_{iteration}"
            after = execute(stage, checking, revised, False)
            paired = compare(before, after)
            payload["comparison"] = entry["comparison"] = paired
            entry["candidate"] = {"metrics": metrics(after), "results": after}
            accepted, reason = acceptance(app.settings, paired)
            evaluation = evolution.record_evaluation(
                app.store,
                plan,
                report,
                before,
                after,
                accepted=accepted,
                reason=reason,
                validation_kind=payload["validation_kind"],
                selection_fingerprint=fingerprint(
                    {
                        "selection": fingerprint([row.model_dump() for row in checking]),
                        "corpora": {k: v.fingerprint for k, v in corpora.items()},
                        "deployment_settings": deployment_settings,
                        "runtime": runtime_source_fingerprint,
                        "timezone": assumed_timezone,
                        "trials_per_question": trials,
                    }
                ),
            )
            payload["evaluation"] = entry["evaluation"] = evaluation
            if not accepted:
                payload["evolution_status"] = entry["status"] = "regression_rejected"
                if paired["pass_to_fail"]:
                    restored = evolution.auto_rollback(
                        app.store, evaluation["selection_fingerprint"]
                    )
                    entry["recovery"] = {"revision_id": restored, "reason": "observed_regression"}
                _write(output, payload)
                break
            try:
                if app.store.harness().id == revised.id:
                    # Recover an activation committed immediately before checkpoint interruption.
                    validation_level = next(
                        r for r in app.store.revisions() if r["id"] == revised.id
                    )["validation"]
                    if validation_level != "business":
                        raise ContractError("Checkpoint candidate lacks business validation")
                    activated = AppliedRevision(
                        revision_id=revised.id,
                        parent_revision_id=active.id,
                        plan_id=plan.id,
                        validation="business",
                    )
                else:
                    activated = app.apply(plan.id, validator=lambda _: True)
            except EvoGError as exc:
                app.store.save_artifact(
                    uuid4().hex,
                    "activation_outcome",
                    {"evaluation_id": evaluation["id"], "status": "rejected", "reason": str(exc)},
                )
                payload["status"] = payload["evolution_status"] = entry["status"] = (
                    "activation_rejected"
                )
                _write(output, payload)
                raise
            app.store.save_artifact(
                uuid4().hex,
                "activation_outcome",
                {
                    "evaluation_id": evaluation["id"],
                    "status": "activated",
                    "revision_id": activated.revision_id,
                },
            )
            payload["activated"] = entry["activated"] = activated.model_dump(mode="json")
            payload["evolution_status"] = entry["status"] = "activated"
            if iteration < iterations:
                # Only evolution episodes receive feedback; validation data never becomes analysis input.
                for row in after:
                    if row.get("run_id") and row["passed"] is not None:
                        app.feedback(
                            row["run_id"],
                            "accepted" if row["passed"] else "rejected",
                            source=f"{name}-official",
                        )
                experience = before = after
            _write(output, payload)
    payload["status"] = (
        "incomplete"
        if any(
            isinstance(value, dict)
            and "research_metrics" in value
            and not value["research_metrics"]["report_complete"]
            for value in payload.values()
        )
        else "completed"
    )
    _write(output, payload)
    return payload
