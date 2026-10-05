"""Explicit judge-only recovery of archived answers, without answering again."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from evog.app import Application
from evog.benchmark_data import Episode
from evog.benchmark_judge import score
from evog.benchmark_metrics import token_total
from evog.errors import ContractError
from evog.io import atomic_write, fingerprint


def rejudge_campaign(app: Application, output: Path, episodes: list[Episode]) -> dict:
    payload = json.loads(output.read_text(encoding="utf-8"))
    if (
        payload.get("schema") != "evog.research-campaign.v1"
        or payload.get("status") != "incomplete"
    ):
        raise ContractError("Judge recovery requires an incomplete research campaign")
    marker = app.store.artifact(payload["id"], "research_campaign")
    settings = app.settings.model_dump(mode="json", exclude={"api_key", "judge_api_key"})
    package = Path(__file__).parent
    runtime = fingerprint(
        {
            p.relative_to(package).as_posix(): p.read_text(encoding="utf-8")
            for p in sorted(package.rglob("*"))
            if p.is_file() and p.suffix in {".py", ".md"}
        }
    )
    if (
        marker["signature"] != payload["signature"]
        or settings != payload["deployment_settings"]
        or runtime != payload["runtime_source_fingerprint"]
    ):
        raise ContractError("Judge recovery configuration or runtime differs from the campaign")
    if (
        fingerprint([e.model_dump(mode="json") for e in episodes])
        != payload["selection_fingerprint"]
    ):
        raise ContractError("Judge recovery questions or reference data changed")
    by_id = {e.episode_id: e for e in episodes}
    recovered = 0
    for entry in payload["rounds"]:
        for row in entry["results"]:
            if row.get("status") != "completed" or row.get("passed") is not None:
                continue
            run = app.store.run(row["run_id"])
            episode = by_id[row["episode_id"]]
            if (
                run["status"] != "completed"
                or run["revision_id"] != entry["revision_id"]
                or run["question"] != episode.prompt()
            ):
                raise ContractError("Recovery record does not match its archived answer")
            judged = score(
                app.provider if episode.options else app.judge_provider,
                episode,
                run["answer"]["text"],
                timeout_seconds=app.settings.judge_timeout_seconds,
            )
            previous = row.get("judge", {})
            row["judge_recovery_history"] = [*row.get("judge_recovery_history", []), previous]
            row["judge"] = judged
            row["passed"] = judged["passed"]
            # Prior failed attempts may have incurred unreported cost; do not invent it.
            row["judge_tokens"] = None
            row["judge_seconds"] = None
            row["judge_recovery_returned_tokens"] = sum(
                token_total(u) or 0 for u in judged.get("usage_calls", [])
            )
            if judged["passed"] is not None:
                app.feedback(
                    row["run_id"],
                    "accepted" if judged["passed"] else "rejected",
                    source=f"{episode.benchmark}-official",
                )
                recovered += 1
            identity = fingerprint(
                {
                    "batch_id": payload["id"],
                    "stage": f"round_{entry['round_index']}",
                    "episode_id": row["episode_id"],
                    "trial_index": 1,
                }
            )
            app.store.save_artifact(
                uuid4().hex,
                "judge_recovery",
                {
                    "trial_result_id": identity,
                    "row": row,
                },
            )
            # Resume recomputes the completed round's metrics and comparison from saved rows.
            entry.pop("metrics", None)
            entry["status"] = "evaluating"
            atomic_write(output, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return {
        "recovered": recovered,
        "status": "judge_recovery_only",
        "next_action": "explicit_campaign_resume",
        "output": str(output.resolve()),
    }
