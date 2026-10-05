"""Command-line interface for group memory and harness evolution."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from evog import __version__
from evog.app import Application
from evog.benchmark_data import load_corpus, load_episodes, resolve_root
from evog.benchmark_split import create_manifest, validate_manifest, write_manifest
from evog.benchmarks import run as run_benchmark
from evog.config import Settings
from evog.demo import DEMO_MESSAGES, DemoProvider
from evog.errors import EvoGError
from evog.io import atomic_write
from evog.models import Message


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="evog", description="Group memory with evidence-driven evolution"
    )
    root.add_argument("--version", action="version", version=__version__)
    root.add_argument("--workspace", type=Path, default=Path(".evog"))
    root.add_argument("--config", type=Path, help="Optional deployment settings TOML")
    sub = root.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="Initialize a private workspace")
    ingest = sub.add_parser("ingest", help="Atomically import canonical group-message JSONL")
    ingest.add_argument("file", type=Path)
    sub.add_parser("groups", help="List imported groups")
    ask = sub.add_parser("ask", help="Answer using explicitly selected groups")
    ask.add_argument("question")
    ask.add_argument("--group", action="append", required=True, dest="groups")
    ask.add_argument("--json", action="store_true")
    feedback = sub.add_parser("feedback", help="Record user or business validation feedback")
    feedback.add_argument("run_id")
    feedback.add_argument("outcome", choices=("accepted", "rejected"))
    feedback.add_argument("--source", default="user")
    sub.add_parser("select", help="Inspect confidence/feedback selection without model calls")
    sub.add_parser("analyze", help="Inspect selected traces and synthesize recurring findings")
    propose = sub.add_parser("propose", help="Create a validated, evidence-linked revision plan")
    propose.add_argument("analysis_id")
    apply = sub.add_parser("apply", help="Activate a structurally validated revision plan")
    apply.add_argument("plan_id")
    rollback = sub.add_parser("rollback", help="Restore a previously saved harness")
    rollback.add_argument("revision_id")
    trace = sub.add_parser("trace", help="Inspect/export a private interaction trace")
    trace.add_argument("run_id")
    trace.add_argument("--output", type=Path)
    sub.add_parser("revisions", help="List harness checkpoints and validation levels")
    export = sub.add_parser("export-harness", help="Export active harness files for inspection")
    export.add_argument("directory", type=Path)
    sub.add_parser(
        "demo", help="Run an explicit offline fixture through the complete evolution loop"
    )
    benchmark = sub.add_parser("benchmark", help="Run the paper benchmark adapters")
    benchmark.add_argument(
        "action", choices=("list", "run", "cycle", "split", "campaign", "held-out", "rejudge")
    )
    benchmark.add_argument("name", choices=("evermembench", "groupmembench"))
    benchmark.add_argument(
        "--data-root", type=Path, help="Dataset checkout (or corresponding ROOT env)"
    )
    benchmark.add_argument(
        "--topic", action="append", dest="topics", help="EverMemBench topic; repeat to combine"
    )
    benchmark.add_argument(
        "--domain", action="append", dest="domains", help="GroupMemBench domain; repeat to combine"
    )
    benchmark.add_argument("--question-type", action="append", dest="question_types")
    selection = benchmark.add_mutually_exclusive_group()
    selection.add_argument("--limit", type=int, default=2, help="Number of questions (default 2)")
    selection.add_argument("--all", action="store_true", help="Run every selected question")
    benchmark.add_argument(
        "--episode-file",
        type=Path,
        help="Episode IDs, one per line; blank lines and # comments ignored",
    )
    benchmark.add_argument(
        "--validation-episode-file", type=Path, help="Disjoint held-out IDs for cycle validation"
    )
    benchmark.add_argument(
        "--iterations",
        type=int,
        default=1,
        help="Bounded evolution cycles (1–10); stops on rejection or no supported change",
    )
    benchmark.add_argument(
        "--trials", type=int, default=1, help="Independent trials per question (1–10)"
    )
    benchmark.add_argument(
        "--resume", action="store_true", help="Resume the exact output checkpoint"
    )
    benchmark.add_argument(
        "--timezone", default="UTC", help="Assumed IANA timezone for naive source timestamps"
    )
    benchmark.add_argument(
        "--output", type=Path, help="Private results JSON; defaults inside workspace"
    )
    benchmark.add_argument("--manifest", type=Path, help="Fixed stratified split manifest")
    benchmark.add_argument("--split-seed", type=int, default=0)
    benchmark.add_argument("--evolution-size", type=int, default=720)
    benchmark.add_argument("--evaluation-rounds", type=int, default=6)
    benchmark.add_argument("--seed", type=int, default=0, help="Campaign sampling seed")
    benchmark.add_argument(
        "--allow-small-cohort",
        action="store_true",
        help="Explicitly test a cohort outside the 2400/720/1680 paper sizes",
    )
    benchmark.add_argument(
        "--campaign-checkpoint",
        type=Path,
        help="Completed campaign JSON for frozen held-out evaluation",
    )
    benchmark.add_argument("--checkpoint", choices=("baseline", "best"), default="best")
    return root


def emit(value: Any) -> None:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        research = args.command == "benchmark" and args.action in {
            "campaign",
            "held-out",
            "rejudge",
        }
        settings = Settings.load(args.config, profile="paper" if research else "product")
        if args.command == "benchmark" and args.action == "campaign":
            settings = Settings.model_validate(
                {**settings.model_dump(), "sampling_seed": args.seed}
            )
        if (
            args.command == "benchmark"
            and args.action in {"held-out", "rejudge"}
            and args.campaign_checkpoint
        ):
            archived = json.loads(args.campaign_checkpoint.read_text(encoding="utf-8"))
            settings = Settings.model_validate(
                {
                    **settings.model_dump(),
                    "sampling_seed": archived["deployment_settings"]["sampling_seed"],
                }
            )
        with Application(
            args.workspace,
            settings=settings,
            provider=DemoProvider() if args.command == "demo" else None,
        ) as app:
            command = args.command
            if command == "init":
                emit({"workspace": str(app.store.workspace), "revision_id": app.store.harness().id})
            elif command == "ingest":
                emit({"imported": app.ingest(args.file)})
            elif command == "groups":
                emit(app.store.groups())
            elif command == "ask":
                answer = app.ask(args.question, groups=args.groups)
                if args.json:
                    emit(answer)
                else:
                    print(answer.text)
                    if answer.citations:
                        print("\nSources: " + ", ".join(answer.citations))
                    print(f"\nConfidence: {answer.confidence:g} | Run: {answer.run_id}")
            elif command == "feedback":
                app.feedback(args.run_id, args.outcome, source=args.source)
                emit({"run_id": args.run_id, "outcome": args.outcome})
            elif command == "select":
                emit(app.selection())
            elif command == "analyze":
                emit(app.analyze())
            elif command == "propose":
                emit(app.propose(args.analysis_id))
            elif command == "apply":
                emit(app.apply(args.plan_id))
            elif command == "rollback":
                app.rollback(args.revision_id)
                emit({"revision_id": app.store.harness().id})
            elif command == "revisions":
                emit(app.store.revisions())
            elif command == "export-harness":
                emit({"directory": str(app.export_harness(args.directory))})
            elif command == "trace":
                result = app.trace(args.run_id)
                if args.output:
                    atomic_write(args.output, json.dumps(result, ensure_ascii=False, indent=2))
                    emit({"output": str(args.output.resolve())})
                else:
                    emit(result)
            elif command == "demo":
                app.ingest(Message.model_validate(row) for row in DEMO_MESSAGES)
                answer = app.ask("What is the latest release schedule?", groups=["demo-team"])
                app.feedback(answer.run_id, "rejected", source="offline-fixture")
                report = app.analyze()
                plan = app.propose(report)
                activated = app.apply(plan.id) if plan.changes else None
                emit(
                    {
                        "mode": "offline-fixture",
                        "answer": answer.model_dump(mode="json"),
                        "analysis_id": report.id,
                        "coverage": report.coverage,
                        "plan": plan.model_dump(mode="json"),
                        "activated": activated.model_dump(mode="json") if activated else None,
                    }
                )
            elif command == "benchmark":
                root = resolve_root(args.name, args.data_root)
                if args.name == "evermembench" and args.domains:
                    raise ValueError("Use --topic for EverMemBench")
                if args.name == "groupmembench" and args.topics:
                    raise ValueError("Use --domain for GroupMemBench")
                selected = dict(
                    topics=args.topics,
                    domains=args.domains,
                    question_types=args.question_types,
                    limit=None if args.all else args.limit,
                    episode_file=args.episode_file,
                )
                if args.action == "list":
                    episodes = load_episodes(args.name, root, **selected)
                    emit(
                        [
                            {
                                "episode_id": episode.episode_id,
                                "scope": episode.scope,
                                "question_type": episode.question_type,
                                "has_options": bool(episode.options),
                            }
                            for episode in episodes
                        ]
                    )
                elif args.action in {"split", "campaign", "held-out", "rejudge"}:
                    from evog.campaign import run_campaign
                    from evog.frozen_evaluation import evaluate_frozen
                    from evog.judge_recovery import rejudge_campaign

                    if args.episode_file or args.validation_episode_file or args.trials != 1:
                        raise ValueError(
                            "Research actions use a split manifest and one trial per question"
                        )
                    episodes = load_episodes(args.name, root, **{**selected, "limit": None})
                    if args.action == "split":
                        manifest = create_manifest(
                            episodes, evolution_size=args.evolution_size, seed=args.split_seed
                        )
                    else:
                        if not args.manifest:
                            raise ValueError("Pass --manifest for a fixed research cohort")
                        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
                        validate_manifest(manifest, episodes)
                    if not args.allow_small_cohort and (
                        args.name != "evermembench"
                        or manifest["counts"]["total"] != 2400
                        or manifest["counts"]["evolution"] != 720
                        or manifest["counts"]["held_out"] != 1680
                        or len(manifest["counts"]["by_type"]) != 9
                    ):
                        raise ValueError(
                            "Paper cohort requires 2400 questions, nine types and a 720/1680 split; use --allow-small-cohort for a new smaller test"
                        )
                    if args.action == "split":
                        if not args.output:
                            raise ValueError("Pass --output DIRECTORY for the three split files")
                        emit(
                            {
                                k: str(v.resolve())
                                for k, v in write_manifest(manifest, args.output, episodes).items()
                            }
                        )
                    elif args.action == "rejudge":
                        if not args.campaign_checkpoint:
                            raise ValueError("Pass --campaign-checkpoint for judge-only recovery")
                        cohort = [e for e in episodes if e.episode_id in manifest["evolution_ids"]]
                        emit(rejudge_campaign(app, args.campaign_checkpoint, cohort))
                    else:
                        chosen = set(
                            manifest[
                                "evolution_ids" if args.action == "campaign" else "held_out_ids"
                            ]
                        )
                        cohort = [e for e in episodes if e.episode_id in chosen]
                        corpora = {
                            scope: load_corpus(args.name, root, scope, args.timezone)
                            for scope in sorted({e.scope for e in cohort})
                        }
                        output = args.output or app.store.workspace / (
                            "campaign.json"
                            if args.action == "campaign"
                            else f"held-out-{args.checkpoint}.json"
                        )

                        def progress(message: str) -> None:
                            print(message, file=sys.stderr, flush=True)

                        if args.action == "campaign":
                            for corpus in corpora.values():
                                app.ingest(iter(corpus.messages))
                            result = run_campaign(
                                app,
                                cohort,
                                corpora,
                                output=output,
                                evaluation_rounds=args.evaluation_rounds,
                                seed=args.seed,
                                resume=args.resume,
                                progress=progress,
                                split_manifest=manifest,
                            )
                        else:
                            if not args.campaign_checkpoint:
                                raise ValueError("Pass --campaign-checkpoint for frozen evaluation")
                            campaign = json.loads(
                                args.campaign_checkpoint.read_text(encoding="utf-8")
                            )
                            if set(campaign["episode_ids"]) != set(manifest["evolution_ids"]):
                                raise ValueError(
                                    "Campaign does not use this manifest's evolution cohort"
                                )
                            result = evaluate_frozen(
                                app,
                                campaign,
                                cohort,
                                corpora,
                                output=output,
                                checkpoint=args.checkpoint,
                                resume=args.resume,
                                progress=progress,
                                split_manifest=manifest,
                            )
                        emit(
                            {
                                "status": result["status"],
                                "output": str(output.resolve()),
                                "split_fingerprint": manifest["manifest_fingerprint"],
                                "best_checkpoint": result.get("best_checkpoint"),
                                "metrics": result.get("metrics"),
                            }
                        )
                else:
                    from uuid import uuid4

                    output = args.output or app.store.workspace / "benchmarks" / (
                        uuid4().hex + ".json"
                    )
                    result = run_benchmark(
                        app,
                        args.name,
                        root,
                        output=output,
                        **selected,
                        assumed_timezone=args.timezone,
                        cycle=args.action == "cycle",
                        iterations=args.iterations,
                        trials=args.trials,
                        resume=args.resume,
                        validation_episode_file=args.validation_episode_file,
                        progress=lambda message: print(message, file=sys.stderr, flush=True),
                    )
                    emit(
                        {
                            "benchmark": args.name,
                            "metrics": result["baseline"]["metrics"],
                            "evolution_status": result.get("evolution_status"),
                            "comparison": result.get("comparison"),
                            "output": str(output.resolve()),
                        }
                    )
        return 0
    except (EvoGError, ValidationError, OSError, ValueError) as exc:
        detail = (
            str(exc)
            if isinstance(exc, EvoGError)
            else f"Invalid input or local file ({type(exc).__name__})"
        )
        print(f"evog: {detail}", file=sys.stderr)
        return 1
