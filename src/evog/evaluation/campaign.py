"""A fixed research campaign, separate from product candidate acceptance."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from evog.agents import analysis, evolution
from evog.app import Application
from evog.core.errors import ContractError, ProviderError
from evog.core.identity import runtime_source_fingerprint
from evog.core.io import atomic_write, fingerprint
from evog.core.models import AnalysisReport, AppliedRevision, EvolutionPlan
from evog.evaluation import runner as benchmarks
from evog.evaluation.data import Corpus, Episode
from evog.evaluation.metrics import research_compare, research_metrics
from evog.harness.memory import MemoryRound


def _write(path: Path, payload: dict) -> None:
    atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _risk(plan: EvolutionPlan | None) -> dict:
    # Text is not a quantitative risk estimate. Unknown edits share one conservative tier.
    return {
        "rank": 0 if plan is None else 1,
        "assessment": "initial_zero_convention" if plan is None else "unquantified",
        "statements": [] if plan is None else [change.regression_risk for change in plan.changes],
    }


def _rank(entry: dict) -> tuple:
    return (
        entry["metrics"]["accuracy"],
        -entry["regressions"],
        -entry["metrics"]["system_failed"],
        -entry["risk"]["rank"],
        -entry["round_index"],
    )


def _best(payload: dict) -> dict | None:
    eligible = [
        entry for entry in payload["rounds"] if entry.get("metrics", {}).get("report_complete")
    ]
    if not eligible:
        return None
    selected = max(eligible, key=_rank)
    return {
        "round_index": selected["round_index"],
        "revision_id": selected["revision_id"],
        "input_memory_snapshot_id": selected.get("input_memory_snapshot_id"),
        "accuracy": selected["metrics"]["accuracy"],
        "regressions": selected["regressions"],
        "system_failed": selected["metrics"]["system_failed"],
        "risk": selected["risk"],
        "selection_dataset": "evolution_only",
    }


def _known_runs(app: Application) -> set[str]:
    with app.store.connect() as db:
        return {row[0] for row in db.execute("SELECT id FROM runs")}


def _artifact_ids(app: Application, kind: str) -> set[str]:
    with app.store.connect() as db:
        return {row[0] for row in db.execute("SELECT id FROM artifacts WHERE kind=?", (kind,))}


def _new_artifacts(app: Application, kind: str, previous: list[str]) -> list[dict]:
    return [app.store.artifact(key, kind) for key in _artifact_ids(app, kind) - set(previous)]


def _validate_workspace_runs(app: Application, payload: dict) -> None:
    owned = set()
    for identity in _artifact_ids(app, "campaign_run_started"):
        marker = app.store.artifact(identity, "campaign_run_started")
        if marker.get("campaign_id") == payload["id"]:
            owned.add(marker["run_id"])
    if not _known_runs(app).issubset(owned):
        raise ContractError("Campaign workspace contains an unrecorded or unrelated interaction")
    if app.store.artifacts("evaluation") or app.store.artifacts("best_ever"):
        raise ContractError("Product evaluation artifacts cannot enter a research campaign")


def _official_outcome(app: Application, run_id: str, episode: Episode) -> bool | None:
    with app.store.connect() as db:
        feedback = db.execute(
            "SELECT outcome,source FROM feedback WHERE run_id=? ORDER BY id DESC LIMIT 1",
            (run_id,),
        ).fetchone()
    if feedback is None or feedback["source"] != f"{episode.benchmark}-official":
        return None
    return feedback["outcome"] == "accepted"


def _recover_results(
    app: Application,
    payload: dict,
    entry: dict,
    episodes: list[Episode],
    corpora: dict[str, Corpus],
) -> None:
    """Retain uncheckpointed work; never issue another answer for an observed attempt.

    A completed answer without a durable judge decision remains pending. Lost cost
    measurements are unavailable, rather than estimated or filled with zero.
    """
    known = set(entry["starting_run_ids"])
    by_episode = {row["episode_id"]: row for row in entry["results"]}
    with app.store.connect() as database:
        recoveries = [
            json.loads(row[0])
            for row in database.execute(
                "SELECT payload FROM artifacts WHERE kind='judge_recovery' ORDER BY rowid"
            )
        ]
    latest_recovery = {r["trial_result_id"]: r["row"] for r in recoveries}
    for episode in episodes:
        identity = fingerprint(
            {
                "batch_id": payload["id"],
                "stage": f"round_{entry['round_index']}",
                "episode_id": episode.episode_id,
                "trial_index": 1,
            }
        )
        if identity in latest_recovery:
            by_episode[episode.episode_id] = latest_recovery[identity]
            continue
        try:
            cached = app.store.artifact(identity, "benchmark_trial_result")
        except ContractError:
            continue
        if cached.get("revision_id") != entry["revision_id"]:
            raise ContractError("Cached trial belongs to another campaign revision")
        by_episode.setdefault(episode.episode_id, cached)
    by_run = {row.get("run_id") for row in by_episode.values()}
    episodes_by_id = {episode.episode_id: episode for episode in episodes}
    markers = {}
    for artifact in _artifact_ids(app, "campaign_run_started"):
        marker = app.store.artifact(artifact, "campaign_run_started")
        if marker["campaign_id"] == payload["id"] and marker["round_index"] == entry["round_index"]:
            markers[marker["run_id"]] = marker
    for row in app.store.runs(entry["revision_id"]):
        if row["id"] in known or row["id"] in by_run:
            continue
        marker = markers.get(row["id"])
        episode = episodes_by_id.get(marker["episode_id"]) if marker else None
        if episode is None or episode.episode_id in by_episode:
            raise ContractError(
                "Unrecorded or duplicate attempt in the isolated campaign workspace"
            )
        groups = json.loads(row["groups"]) if isinstance(row["groups"], str) else row["groups"]
        if row["question"] != episode.prompt() or sorted(groups) != sorted(
            corpora[episode.scope].group_ids
        ):
            raise ContractError(
                "Recorded attempt differs from its planned question or authorization"
            )
        record = {
            "episode_id": episode.episode_id,
            "trial_index": 1,
            "scope": episode.scope,
            "question_type": episode.question_type,
            "revision_id": entry["revision_id"],
            "run_id": row["id"],
            "status": row["status"],
            "passed": None,
            "recovery": "durable_answer_and_feedback_only_costs_unavailable",
        }
        if row["status"] == "completed":
            record["answer"] = row["answer"]
            official = _official_outcome(app, row["id"], episode)
            if official is not None:
                record["passed"] = official
            else:
                record["judge"] = {"status": "recovery_judge_pending", "passed": None}
        by_episode[episode.episode_id] = record
    # A previously unresolved judge can be completed explicitly without another answer.
    for record in by_episode.values():
        if (
            record.get("run_id")
            and record.get("status") == "completed"
            and record.get("passed") is None
        ):
            official = _official_outcome(
                app, record["run_id"], episodes_by_id[record["episode_id"]]
            )
            if official is not None:
                record["passed"] = official
    entry["results"] = [by_episode[e.episode_id] for e in episodes if e.episode_id in by_episode]


def run_campaign(
    app: Application,
    episodes: list[Episode],
    corpora: dict[str, Corpus],
    *,
    output: Path,
    evaluation_rounds: int = 6,
    seed: int = 0,
    resume: bool = False,
    progress: Callable[[str], None] = lambda _: None,
    target_accuracy: float | None = None,
    memory_round_factory: Callable[[int, str | None], Any] | None = None,
    split_manifest: dict | None = None,
) -> dict:
    """Evaluate each checkpoint once, archive, then try a structurally valid edit.

    This kernel accepts evolution episodes only. Held-out evaluation belongs to a
    different workspace. ``seed`` identifies a run; it does not guarantee API replay.
    """
    output = Path(output).resolve()
    if not episodes or len({e.episode_id for e in episodes}) != len(episodes):
        raise ContractError("Campaign requires unique, nonempty evolution episodes")
    if type(evaluation_rounds) is not int or not 1 <= evaluation_rounds <= 100:
        raise ContractError("Evaluation rounds must be within 1–100")
    if type(seed) is not int or seed < 0:
        raise ContractError("Campaign seed must be a nonnegative integer")
    if target_accuracy is not None and not 0 <= target_accuracy <= 1:
        raise ContractError("Target accuracy must be within 0–1")
    if any(e.scope not in corpora for e in episodes):
        raise ContractError("Campaign is missing an authorized source corpus")
    if split_manifest is not None and set(split_manifest["evolution_ids"]) != {
        e.episode_id for e in episodes
    }:
        raise ContractError("Campaign episodes differ from the fixed split manifest")
    settings = app.settings.model_dump(mode="json", exclude={"api_key", "judge_api_key"})
    runtime_fingerprint = runtime_source_fingerprint()
    signature = fingerprint(
        {
            "episodes": [e.model_dump(mode="json") for e in episodes],
            "corpora": {scope: corpora[scope].fingerprint for scope in sorted(corpora)},
            "settings": settings,
            "rounds": evaluation_rounds,
            "seed": seed,
            "target_accuracy": target_accuracy,
            "memory_protocol": "frozen_round.v1",
            "workspace": str(app.store.workspace.resolve()),
            "runtime_source_fingerprint": runtime_fingerprint,
            "split_manifest_fingerprint": split_manifest.get("manifest_fingerprint")
            if split_manifest
            else None,
            "split_source_fingerprint": split_manifest.get("source_fingerprint")
            if split_manifest
            else None,
        }
    )
    markers = app.store.artifacts("research_campaign", limit=100)
    if not resume:
        if output.exists():
            raise ContractError("Campaign output already exists")
        if (
            _known_runs(app)
            or markers
            or app.store.artifacts("evaluation")
            or app.store.artifacts("best_ever")
        ):
            raise ContractError(
                "Research campaigns require a separate workspace without prior runs or evaluations"
            )
        payload = {
            "schema": "evog.research-campaign.v1",
            "split_manifest_fingerprint": split_manifest.get("manifest_fingerprint")
            if split_manifest
            else None,
            "split_source_fingerprint": split_manifest.get("source_fingerprint")
            if split_manifest
            else None,
            "id": uuid4().hex,
            "signature": signature,
            "status": "preparing",
            "seed": seed,
            "seed_semantics": "run_identifier_api_determinism_not_guaranteed",
            "requested_evaluation_rounds": evaluation_rounds,
            "workspace": str(app.store.workspace.resolve()),
            "runtime_source_fingerprint": runtime_fingerprint,
            "analysis_scope_policy": "all_eligible"
            if app.settings.analysis_all_eligible
            else "bounded",
            "episode_ids": [e.episode_id for e in episodes],
            "deployment_settings": settings,
            "selection_fingerprint": fingerprint([e.model_dump(mode="json") for e in episodes]),
            "corpora": {scope: corpus.fingerprint for scope, corpus in corpora.items()},
            "initial_revision_id": app.store.harness().id,
            "rounds": [],
            "tie_break_conventions": {
                "initial_regressions": 0,
                "initial_risk_rank": 0,
                "unquantified_revision_risk_rank": 1,
                "order": [
                    "accuracy",
                    "fewer_regressions",
                    "fewer_system_failures",
                    "lower_risk_rank",
                    "earlier_round",
                ],
            },
        }
        _write(output, payload)
        app.store.save_artifact(
            payload["id"],
            "research_campaign",
            {"id": payload["id"], "signature": signature, "output": str(output)},
        )
    else:
        if not output.exists():
            raise ContractError("Campaign resume requires its existing checkpoint")
        payload = json.loads(output.read_text(encoding="utf-8"))
        if (
            payload.get("schema") != "evog.research-campaign.v1"
            or payload.get("signature") != signature
        ):
            raise ContractError("Campaign inputs, settings or workspace changed")
        if markers and not any(
            m["id"] == payload["id"] and m["signature"] == signature and m["output"] == str(output)
            for m in markers
        ):
            raise ContractError("Workspace belongs to another research campaign")
        if not markers:
            if _known_runs(app):
                raise ContractError("Campaign workspace has no matching ownership marker")
            app.store.save_artifact(
                payload["id"],
                "research_campaign",
                {"id": payload["id"], "signature": signature, "output": str(output)},
            )
        if payload["status"] == "completed":
            return payload
    for corpus in corpora.values():
        app.ingest(iter(corpus.messages))
    planned_ids = [e.episode_id for e in episodes]
    for round_index in range(evaluation_rounds):
        _validate_workspace_runs(app, payload)
        entry = next((r for r in payload["rounds"] if r["round_index"] == round_index), None)
        if entry and entry["status"] == "completed":
            if entry.get("stop_requested"):
                break
            continue
        previous = next((r for r in payload["rounds"] if r["round_index"] == round_index - 1), None)
        if entry is None:
            expected = previous["next_revision_id"] if previous else payload["initial_revision_id"]
            if app.store.harness().id != expected:
                raise ContractError("Campaign active harness differs from its archived lineage")
            entry = {
                "round_index": round_index,
                "revision_id": expected,
                "status": "evaluating",
                "results": [],
                "starting_run_ids": sorted(_known_runs(app)),
                "risk": previous["next_risk"] if previous else _risk(None),
                "parent_memory_snapshot_id": previous.get("output_memory_snapshot_id")
                if previous
                else None,
            }
            payload["rounds"].append(entry)
            _write(output, payload)
        harness = app.store.harness(entry["revision_id"])
        memory_round = (
            memory_round_factory(round_index, entry.get("parent_memory_snapshot_id"))
            if memory_round_factory
            else MemoryRound(
                app.store.workspace,
                entry.get("parent_memory_snapshot_id"),
                f"{payload['id']}/{round_index}",
            )
        )
        if memory_round is not None:
            memory_id = memory_round.snapshot_id
            if entry.get("input_memory_snapshot_id") not in (None, memory_id):
                raise ContractError("Campaign input memory snapshot changed")
            entry["input_memory_snapshot_id"] = memory_id
            _write(output, payload)
        if "metrics" not in entry or not entry["metrics"]["report_complete"]:
            _recover_results(app, payload, entry, episodes, corpora)
            payload["status"] = entry["status"] = "evaluating"
            _write(output, payload)

            def checkpoint(rows: list[dict], current: dict = entry) -> None:
                current["results"] = rows
                _write(output, payload)

            def run_started(episode_id: str, run_id: str, current: dict = entry) -> None:
                identity = fingerprint(
                    {
                        "campaign_id": payload["id"],
                        "round_index": current["round_index"],
                        "episode_id": episode_id,
                    }
                )
                app.store.save_artifact(
                    identity,
                    "campaign_run_started",
                    {
                        "campaign_id": payload["id"],
                        "round_index": current["round_index"],
                        "episode_id": episode_id,
                        "run_id": run_id,
                        "revision_id": current["revision_id"],
                    },
                )

            results = benchmarks.evaluate(
                app,
                episodes,
                corpora,
                harness,
                batch_id=payload["id"],
                stage=f"round_{round_index}",
                progress=progress,
                feedback=True,
                checkpoint=checkpoint,
                trials=1,
                initial=entry["results"],
                memory_round=memory_round,
                run_started=run_started,
            )
            entry["results"] = results
            entry["metrics"] = research_metrics(results, planned_ids)
            if not entry["metrics"]["report_complete"]:
                payload["status"] = entry["status"] = "incomplete"
                _write(output, payload)
                return payload
            entry["comparison"] = (
                research_compare(payload["rounds"][round_index - 1]["results"], results)
                if round_index
                else None
            )
            entry["regressions"] = entry["comparison"]["pass_to_fail"] if round_index else 0
            entry["status"] = "evaluated"
            _write(output, payload)
        evaluation_id = fingerprint({"campaign_id": payload["id"], "round_index": round_index})
        evaluation_record = {
            "campaign_id": payload["id"],
            "round_index": round_index,
            "revision_id": entry["revision_id"],
            "metrics": entry["metrics"],
            "comparison": entry["comparison"],
            "risk": entry["risk"],
            "input_memory_snapshot_id": entry["input_memory_snapshot_id"],
            "selection_dataset": "evolution_only",
            "preceding_plan_id": previous.get("plan", {}).get("id") if previous else None,
        }
        try:
            archived = app.store.artifact(evaluation_id, "research_round_evaluation")
        except ContractError:
            app.store.save_artifact(evaluation_id, "research_round_evaluation", evaluation_record)
        else:
            if archived != evaluation_record:
                raise ContractError("Completed research evaluation archive changed")
        if "analysis" not in entry:
            run_ids = [
                r["run_id"]
                for r in entry["results"]
                if r.get("run_id")
                and r.get("status") == "completed"
                and r.get("passed") is not None
            ]
            if "analysis_start_artifact_ids" not in entry:
                entry["analysis_start_artifact_ids"] = sorted(_artifact_ids(app, "analysis"))
                _write(output, payload)
            recovered = _new_artifacts(app, "analysis", entry["analysis_start_artifact_ids"])
            if len(recovered) > 1 or recovered and recovered[0].get("revision_id") != harness.id:
                raise ContractError("Ambiguous analysis artifacts in the isolated campaign")
            report = (
                AnalysisReport.model_validate(recovered[0])
                if recovered
                else analysis.analyze(
                    app.store,
                    app.provider if run_ids else None,
                    app.settings,
                    harness.id,
                    run_ids=run_ids,
                )
            )
            entry["analysis"] = report.model_dump(mode="json")
            entry["status"] = "analyzed"
            _write(output, payload)
        report = AnalysisReport.model_validate(entry["analysis"])
        payload["best_checkpoint"] = _best(payload)
        _write(output, payload)
        stopping = round_index == evaluation_rounds - 1 or (
            target_accuracy is not None and entry["metrics"]["accuracy"] >= target_accuracy
        )
        entry["stop_requested"] = stopping
        _write(output, payload)
        if not stopping and "plan" not in entry and "proposal_rejected" not in entry:
            if "plan_start_artifact_ids" not in entry:
                entry["plan_start_artifact_ids"] = sorted(_artifact_ids(app, "plan"))
                _write(output, payload)
            recovered = _new_artifacts(app, "plan", entry["plan_start_artifact_ids"])
            if len(recovered) > 1 or recovered and recovered[0].get("analysis_id") != report.id:
                raise ContractError("Ambiguous proposal artifacts in the isolated campaign")
            if app.store.harness().id != harness.id:
                raise ContractError("Active harness changed before the campaign proposal")
            try:
                plan = (
                    EvolutionPlan.model_validate(recovered[0]) if recovered else app.propose(report)
                )
            except ContractError as exc:
                entry["proposal_rejected"] = {"status": "rejected", "reason": str(exc)}
                entry["candidate_validation"] = entry["proposal_rejected"]
            except ProviderError:
                payload["status"] = entry["status"] = "incomplete"
                entry["proposal_incident"] = {"status": "provider_unavailable"}
                _write(output, payload)
                raise
            else:
                entry["plan"] = plan.model_dump(mode="json")
            entry["status"] = "planned"
            _write(output, payload)
        if not stopping and "next_revision_id" not in entry:
            plan = EvolutionPlan.model_validate(entry["plan"]) if entry.get("plan") else None
            next_harness = None
            try:
                next_harness = (
                    evolution.candidate(app.store, plan, report) if plan and plan.changes else None
                )
            except ContractError as exc:
                entry["candidate_validation"] = {"status": "rejected", "reason": str(exc)}
            if next_harness is None or next_harness.id == harness.id:
                entry["next_revision_id"] = harness.id
                entry["next_risk"] = entry["risk"]
                entry.setdefault("candidate_validation", {"status": "no_op"})
            else:
                if app.store.harness().id == next_harness.id:
                    matching = [
                        r
                        for r in app.store.artifacts("revision_activation", limit=100)
                        if r.get("revision_id") == next_harness.id
                        and r.get("parent_revision_id") == harness.id
                        and r.get("plan_id") == plan.id
                        and r.get("validation") == "structural"
                    ]
                    if not matching:
                        raise ContractError(
                            "Candidate activation has no matching research transition"
                        )
                    activated = AppliedRevision(
                        revision_id=next_harness.id,
                        parent_revision_id=harness.id,
                        plan_id=plan.id,
                        validation="structural",
                    )
                elif app.store.harness().id == harness.id:
                    activated = app.apply(plan.id)
                else:
                    raise ContractError(
                        "Active harness is outside the interrupted campaign transition"
                    )
                entry["activation"] = activated.model_dump(mode="json")
                entry["next_revision_id"] = next_harness.id
                entry["next_risk"] = _risk(plan)
                entry["candidate_validation"] = {
                    "status": "structural",
                    "empirical_effect": "pending_next_evaluation",
                }
            entry["status"] = "transitioned"
            _write(output, payload)
        if memory_round is not None and "output_memory_snapshot_id" not in entry:
            entry["output_memory_snapshot_id"] = memory_round.merge()
            _write(output, payload)
        entry["status"] = "completed"
        payload["best_checkpoint"] = _best(payload)
        _write(output, payload)
        if stopping:
            break
    payload["status"] = "completed"
    payload["completed_evaluation_rounds"] = len(payload["rounds"])
    _write(output, payload)
    return payload
