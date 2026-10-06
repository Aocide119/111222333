"""Frozen held-out and cross-benchmark evaluation without campaign writeback."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from evog.app import Application
from evog.core.errors import ContractError
from evog.core.identity import runtime_source_fingerprint
from evog.core.io import atomic_write, dumps, fingerprint, safe_path
from evog.evaluation.campaign import _best
from evog.evaluation.data import Corpus, Episode
from evog.evaluation.metrics import research_metrics
from evog.evaluation.runner import evaluate
from evog.harness.bundle import bundle_manifest, materialize_bundle
from evog.harness.memory import MemoryRound


def evaluate_frozen(
    source: Application,
    campaign: dict,
    episodes: list[Episode],
    corpora: dict[str, Corpus],
    *,
    output: Path,
    checkpoint: str = "best",
    resume: bool = False,
    progress: Callable[[str], None] = lambda _: None,
    split_manifest: dict | None = None,
    _mode: str = "held_out",
) -> dict:
    if _mode not in {"held_out", "transfer"}:
        raise ContractError("Unknown frozen evaluation mode")
    if campaign.get("status") != "completed" or checkpoint not in {"best", "baseline"}:
        raise ContractError(
            "Frozen evaluation requires a completed campaign and a named checkpoint"
        )
    marker = source.store.artifact(campaign["id"], "research_campaign")
    if marker["signature"] != campaign["signature"]:
        raise ContractError("Campaign archive does not belong to this source workspace")
    if _mode == "transfer" and marker.get("benchmark") != campaign.get("benchmark"):
        raise ContractError(
            "Transfer source benchmark differs from the campaign's ownership record"
        )
    if campaign.get("best_checkpoint") != _best(campaign):
        raise ContractError("Campaign best checkpoint differs from its evaluated selection")
    selected = campaign["best_checkpoint"] if checkpoint == "best" else campaign["rounds"][0]
    revision = selected["revision_id"]
    evaluated = next(
        (r for r in campaign["rounds"] if r["round_index"] == selected["round_index"]), None
    )
    if (
        not evaluated
        or not evaluated["metrics"]["report_complete"]
        or evaluated["revision_id"] != revision
    ):
        raise ContractError("Frozen checkpoint was not completely evaluated")
    if not episodes or len({e.episode_id for e in episodes}) != len(episodes):
        raise ContractError("Frozen evaluation requires unique nonempty episodes")
    if any(e.scope not in corpora for e in episodes):
        raise ContractError("Frozen evaluation is missing an authorized source corpus")
    if set(campaign["episode_ids"]) & {e.episode_id for e in episodes}:
        raise ContractError("Held-out questions overlap evolution questions")
    if _mode == "held_out" and campaign.get("split_manifest_fingerprint"):
        if split_manifest is None or (
            split_manifest["manifest_fingerprint"] != campaign["split_manifest_fingerprint"]
            or split_manifest["source_fingerprint"] != campaign["split_source_fingerprint"]
            or set(split_manifest["held_out_ids"]) != {e.episode_id for e in episodes}
        ):
            raise ContractError("Held-out evaluation does not match the campaign's frozen split")
    for scope, corpus in corpora.items():
        if (
            _mode == "held_out"
            and scope in campaign["corpora"]
            and campaign["corpora"][scope] != corpus.fingerprint
        ):
            raise ContractError("Shared source corpus changed since the campaign")
    harness = source.store.harness(revision)
    snapshot_id = evaluated.get("input_memory_snapshot_id")
    if not snapshot_id:
        raise ContractError("Frozen checkpoint is missing its evaluated memory snapshot")
    snapshot_path = safe_path(source.store.workspace, f"round_memory/snapshots/{snapshot_id}.json")
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    if fingerprint(snapshot) != snapshot_id:
        raise ContractError("Frozen memory snapshot fingerprint mismatch")
    component_manifest = bundle_manifest(harness)
    workspace_fingerprint = fingerprint({"components": component_manifest, "memory": snapshot_id})
    output = output.resolve()
    workspace = output.parent / (output.stem + ".workspace")
    if workspace.is_symlink():
        raise ContractError("Frozen workspace must not be a symlink")
    if (
        workspace == source.store.workspace
        or source.store.workspace in workspace.parents
        or workspace in source.store.workspace.parents
    ):
        raise ContractError("Frozen evaluation requires a separate workspace outside the source")
    if source.store.workspace in output.parents:
        raise ContractError("Frozen output must stay outside the source workspace")
    settings = source.settings.model_dump(mode="json", exclude={"api_key", "judge_api_key"})
    if settings != campaign["deployment_settings"]:
        raise ContractError("Frozen evaluation model, judge or deployment settings changed")
    runtime_fingerprint = runtime_source_fingerprint()
    if campaign.get("runtime_source_fingerprint") != runtime_fingerprint:
        raise ContractError("Frozen evaluation runtime or packaged prompts changed")
    signature = fingerprint(
        {
            "campaign_id": campaign["id"],
            "revision_id": revision,
            "memory": snapshot_id,
            "episodes": [e.model_dump(mode="json") for e in episodes],
            "corpora": {k: v.fingerprint for k, v in corpora.items()},
            "settings": settings,
            "runtime": runtime_fingerprint,
            "mode": _mode,
            "checkpoint": checkpoint,
            "workspace_snapshot": workspace_fingerprint,
        }
    )
    if output.exists() and not resume or resume and not output.exists():
        raise ContractError("Use a fresh frozen output, or explicitly resume its existing archive")
    if not resume and workspace.exists():
        raise ContractError("Frozen workspace already exists; choose a fresh output")
    if resume and not workspace.is_dir():
        raise ContractError("Frozen resume requires the original evaluation workspace")
    payload = (
        json.loads(output.read_text())
        if resume
        else {
            "schema": "evog.frozen-evaluation.v1",
            "mode": _mode,
            "source_benchmark": campaign.get("benchmark"),
            "target_benchmark": episodes[0].benchmark,
            "corpora": {k: v.fingerprint for k, v in corpora.items()},
            "component_manifest": component_manifest,
            "workspace_snapshot_fingerprint": workspace_fingerprint,
            "signature": signature,
            "campaign_id": campaign["id"],
            "checkpoint": checkpoint,
            "revision_id": revision,
            "memory_snapshot_id": snapshot_id,
            "episode_ids": [e.episode_id for e in episodes],
            "selection_fingerprint": fingerprint([e.model_dump(mode="json") for e in episodes]),
            "split_manifest_fingerprint": campaign.get("split_manifest_fingerprint")
            if _mode == "held_out"
            else None,
            "split_source_fingerprint": campaign.get("split_source_fingerprint")
            if _mode == "held_out"
            else None,
            "results": [],
            "deployment_settings": settings,
            "status": "preparing",
            "runtime_source_fingerprint": runtime_fingerprint,
            "feedback": "disabled",
            "memory_writeback": "discarded",
            "selection_effect": "none",
        }
    )
    if payload["signature"] != signature:
        raise ContractError("Frozen evaluation inputs changed")

    def save(rows: list[dict]) -> None:
        payload["results"] = rows
        payload["metrics"] = research_metrics(rows, payload["episode_ids"])
        atomic_write(output, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")

    save(payload["results"])
    with Application(
        workspace, settings=source.settings, provider=source.provider, judge_provider=source._judge
    ) as target:
        for corpus in corpora.values():
            target.ingest(iter(corpus.messages))
        if target.store.harness().id != harness.id:
            if resume:
                raise ContractError("Frozen workspace has a different active checkpoint")
            target.store.activate(
                harness, target.store.harness().id, "frozen-checkpoint", "structural"
            )
        materialize_bundle(harness, safe_path(target.store.workspace, "workspace"))
        destination = safe_path(
            target.store.workspace, f"round_memory/snapshots/{snapshot_id}.json"
        )
        if destination.exists() and destination.read_text(encoding="utf-8") != dumps(snapshot):
            raise ContractError("Frozen workspace memory changed")
        atomic_write(destination, dumps(snapshot))
        memory = MemoryRound(target.store.workspace, snapshot_id, _mode + "-discard")
        results = evaluate(
            target,
            episodes,
            corpora,
            harness,
            batch_id=signature,
            stage=_mode,
            progress=progress,
            feedback=False,
            checkpoint=save,
            initial=payload["results"],
            memory_round=memory,
        )
        save(results)
    payload["status"] = "completed" if payload["metrics"]["report_complete"] else "incomplete"
    save(payload["results"])
    return payload


def evaluate_transfer(
    source: Application,
    campaign: dict,
    episodes: list[Episode],
    corpora: dict[str, Corpus],
    *,
    output: Path,
    checkpoint: str = "best",
    resume: bool = False,
    progress: Callable[[str], None] = lambda _: None,
    allow_small_cohort: bool = False,
) -> dict:
    """Apply the entire EverMemBench checkpoint workspace to GroupMemBench unchanged."""
    if (
        campaign.get("benchmark") != "evermembench"
        or not episodes
        or any(e.benchmark != "groupmembench" for e in episodes)
    ):
        raise ContractError("Transfer requires an EverMemBench campaign and GroupMemBench targets")
    if not allow_small_cohort and len(episodes) != 745:
        raise ContractError(
            "Paper transfer requires 745 questions; explicitly allow a smaller test"
        )
    source_groups = {row["group_id"] for row in source.store.groups()}
    target_groups = {group for corpus in corpora.values() for group in corpus.group_ids}
    if source_groups & target_groups:
        raise ContractError("Transfer corpora must use new group scopes")
    return evaluate_frozen(
        source,
        campaign,
        episodes,
        corpora,
        output=output,
        checkpoint=checkpoint,
        resume=resume,
        progress=progress,
        _mode="transfer",
    )
