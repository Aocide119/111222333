"""Frozen held-out evaluation in a separate store; it never writes campaign history."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from evog.app import Application
from evog.benchmark_data import Corpus, Episode
from evog.benchmark_metrics import research_metrics
from evog.benchmarks import evaluate
from evog.campaign import _best
from evog.errors import ContractError
from evog.io import atomic_write, dumps, fingerprint, safe_path
from evog.memory import MemoryRound


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
) -> dict:
    if campaign.get("status") != "completed" or checkpoint not in {"best", "baseline"}:
        raise ContractError(
            "Frozen evaluation requires a completed campaign and a named checkpoint"
        )
    marker = source.store.artifact(campaign["id"], "research_campaign")
    if marker["signature"] != campaign["signature"]:
        raise ContractError("Campaign archive does not belong to this source workspace")
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
    if set(campaign["episode_ids"]) & {e.episode_id for e in episodes}:
        raise ContractError("Held-out questions overlap evolution questions")
    if campaign.get("split_manifest_fingerprint"):
        if split_manifest is None or (
            split_manifest["manifest_fingerprint"] != campaign["split_manifest_fingerprint"]
            or split_manifest["source_fingerprint"] != campaign["split_source_fingerprint"]
            or set(split_manifest["held_out_ids"]) != {e.episode_id for e in episodes}
        ):
            raise ContractError("Held-out evaluation does not match the campaign's frozen split")
    for scope, corpus in corpora.items():
        if scope in campaign["corpora"] and campaign["corpora"][scope] != corpus.fingerprint:
            raise ContractError("Shared source corpus changed since the campaign")
    harness = source.store.harness(revision)
    snapshot_id = evaluated.get("input_memory_snapshot_id")
    output = output.resolve()
    workspace = output.parent / (output.stem + ".workspace")
    if workspace.resolve() == source.store.workspace:
        raise ContractError("Held-out evaluation requires a separate workspace")
    settings = source.settings.model_dump(mode="json", exclude={"api_key", "judge_api_key"})
    if settings != campaign["deployment_settings"]:
        raise ContractError("Frozen evaluation model, judge or deployment settings changed")
    package = Path(__file__).parent
    runtime_fingerprint = fingerprint(
        {
            path.relative_to(package).as_posix(): path.read_text(encoding="utf-8")
            for path in sorted(package.rglob("*"))
            if path.is_file() and path.suffix in {".py", ".md"}
        }
    )
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
        }
    )
    if output.exists() and not resume or resume and not output.exists():
        raise ContractError("Use a fresh frozen output, or explicitly resume its existing archive")
    if not resume and workspace.exists():
        raise ContractError("Frozen workspace already exists; choose a fresh output")
    payload = (
        json.loads(output.read_text())
        if resume
        else {
            "schema": "evog.frozen-evaluation.v1",
            "signature": signature,
            "campaign_id": campaign["id"],
            "checkpoint": checkpoint,
            "revision_id": revision,
            "memory_snapshot_id": snapshot_id,
            "episode_ids": [e.episode_id for e in episodes],
            "selection_fingerprint": fingerprint([e.model_dump(mode="json") for e in episodes]),
            "split_manifest_fingerprint": campaign.get("split_manifest_fingerprint"),
            "split_source_fingerprint": campaign.get("split_source_fingerprint"),
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
    if resume and payload["status"] == "completed":
        return payload

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
            target.store.activate(
                harness, target.store.harness().id, "frozen-checkpoint", "structural"
            )
        if snapshot_id:
            snapshot_path = safe_path(
                source.store.workspace, f"round_memory/snapshots/{snapshot_id}.json"
            )
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            if fingerprint(snapshot) != snapshot_id:
                raise ContractError("Frozen memory snapshot fingerprint mismatch")
            destination = safe_path(
                target.store.workspace, f"round_memory/snapshots/{snapshot_id}.json"
            )
            atomic_write(destination, dumps(snapshot))
        memory = MemoryRound(target.store.workspace, snapshot_id, "held-out-discard")
        results = evaluate(
            target,
            episodes,
            corpora,
            harness,
            batch_id=signature,
            stage="held_out",
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
