"""Evidence-linked plans, bounded candidates, atomic activation, and rollback."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any, Literal
from uuid import uuid4

from pydantic import ValidationError

from evog.analysis import TraceAccess, analysis_tools, ref_key
from evog.config import Settings
from evog.errors import ConflictError, ContractError, EvoGError, ProviderError
from evog.harness import (
    FILE_INTERFACES,
    Harness,
    Interventions,
    Operations,
    Representation,
    interface_for,
    prompt,
)
from evog.io import dumps, fingerprint
from evog.models import AnalysisReport, AppliedRevision, EvolutionPlan, Finding, PlanDraft, Record
from evog.providers import Provider, assistant_message, complete_before, stage_provider
from evog.runtime import trim_messages
from evog.store import Store

Validator = Callable[[Harness], bool]


def _findings(store: Store, report: AnalysisReport) -> dict[str, Finding]:
    findings = {finding.id: finding for finding in report.findings}
    with store.connect() as db:
        artifacts = [
            json.loads(row["payload"])
            for row in db.execute("SELECT payload FROM artifacts WHERE kind='evolution_finding'")
        ]
    for artifact in artifacts:
        if artifact.get("analysis_id") == report.id:
            finding = Finding.model_validate(artifact["finding"])
            findings[finding.id] = finding
    return findings


def _change_manifest(plan: EvolutionPlan) -> dict[str, Any]:
    """Build an immutable, per-change attribution record keyed by plan."""
    changes = []
    for index, change in enumerate(plan.changes, 1):
        changes.append(
            {
                "change_id": f"{plan.id}:{index}",
                "entry": change.interface,
                "path": change.path,
                "what_changed": change.rationale,
                "description": change.expected_effect,
                "finding_ids": list(change.finding_ids),
                "predicted_fixes": list(change.predicted_fix_runs),
                "risk_tasks": list(change.risk_runs),
                "validation": change.validation,
            }
        )
    return {
        "id": f"manifest:{plan.id}",
        "plan_id": plan.id,
        "analysis_id": plan.analysis_id,
        "parent_revision_id": plan.parent_revision_id,
        "changes": changes,
        "schema": "evog.change_manifest.v1",
    }


def _save_change_manifest(store: Store, plan: EvolutionPlan) -> dict[str, Any]:
    manifest = _change_manifest(plan)
    store.save_artifact(manifest["id"], "change_manifest", manifest)
    return manifest


def _load_change_manifest(store: Store, plan: EvolutionPlan) -> dict[str, Any]:
    """Load a plan's manifest, tolerating plans produced before manifests."""
    try:
        return store.artifact(f"manifest:{plan.id}", "change_manifest")
    except ContractError:
        return _change_manifest(plan)


def candidate(store: Store, plan: EvolutionPlan, report: AnalysisReport) -> Harness:
    if plan.analysis_id != report.id or plan.parent_revision_id != report.revision_id:
        raise ContractError("Plan is not linked to its source analysis and revision")
    parent = store.harness(plan.parent_revision_id)
    contents = dict(parent.contents)
    findings = set(_findings(store, report))
    seen = set()
    analysis_runs = set([*report.selected_run_ids, *report.control_run_ids])
    protected = set(analysis_runs)
    for group in store.groups():
        for message in store.messages(group["group_id"]):
            protected.add(message.ref)
            if len(message.text) >= 40:
                protected.add(message.text)
    for change in plan.changes:
        if not set(change.predicted_fix_runs + change.risk_runs).issubset(analysis_runs):
            raise ContractError("Change associations must reference source analysis interactions")
        if change.path in seen:
            raise ContractError("A plan must replace each file at most once")
        seen.add(change.path)
        if interface_for(change.path) != change.interface:
            raise ContractError("Change interface does not match the declared file's role")
        if not set(change.finding_ids).issubset(findings):
            raise ContractError("Change refers to an unknown analysis finding")
        if not all(
            value.strip()
            for value in (
                change.rationale,
                change.expected_effect,
                change.regression_risk,
                change.validation,
            )
        ):
            raise ContractError(
                "Changes require rationale, expected effect, regression risk and validation"
            )
        if any(value in change.content for value in protected):
            raise ContractError(
                "Reusable harness content includes an interaction ID or verbatim source"
            )
        contents[change.path] = change.content
    return Harness(contents)


class ReadAnalysisArgs(Record):
    kind: Literal["diagnosis", "finding"]
    id: str


class ValidateRevisionArgs(Record):
    draft: PlanDraft


class RegisterFindingArgs(Record):
    finding: Finding


def _error_summary(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        return dumps(
            [
                {"field": list(e["loc"]), "error": e["msg"]}
                for e in exc.errors(include_input=False, include_url=False)
            ]
        )[:2000]
    return str(exc)[:2000]


def _linked_plan(draft: PlanDraft, report: AnalysisReport) -> EvolutionPlan:
    return EvolutionPlan(
        **draft.model_dump(),
        id=uuid4().hex,
        analysis_id=report.id,
        parent_revision_id=report.revision_id,
    )


def propose(
    store: Store, provider: Provider | None, settings: Settings, report: AnalysisReport
) -> EvolutionPlan:
    if store.harness().id != report.revision_id:
        raise ContractError("Analyze the active revision before proposing changes")
    has_reviewable_trace = bool(report.selected_run_ids)
    if not report.findings and not has_reviewable_trace:
        draft = PlanDraft(
            summary="No sufficiently covered, inspected finding supports a revision", changes=[]
        )
    else:
        if provider is None:
            raise ContractError("A model provider is required for revision proposals")
        provider = stage_provider(provider, settings, "evolution")
        supplied: dict[str, Any] = {
            "harness": store.harness().contents,
            "findings": [finding.model_dump() for finding in report.findings],
            "recovered_findings": [
                finding.model_dump()
                for finding in _findings(store, report).values()
                if finding.id not in {f.id for f in report.findings}
            ],
            "selected_run_ids": report.selected_run_ids,
            "control_run_ids": report.control_run_ids,
            "diagnoses": [diagnosis.model_dump() for diagnosis in report.diagnoses],
            "eligible_for_revision": report.eligible_for_revision,
            "coverage": report.coverage,
            "incomplete": report.incomplete,
            "finding_min_support": settings.finding_min_support,
            "history": store.revisions(),
            "evaluations": store.artifacts("evaluation", limit=10),
            "campaign_history": store.artifacts("research_round_evaluation", limit=100),
            "best_ever": store.artifacts("best_ever", limit=10),
            "change_manifests": store.artifacts("change_manifest", limit=10),
            "activation_outcomes": store.artifacts("activation_outcome", limit=10),
            "failed_proposals": [
                r
                for r in store.artifacts("proposal_attempt", limit=20)
                if r["status"] != "validated"
            ][:5],
            "permitted_files": FILE_INTERFACES,
            "new_skill_paths": "skills/<lowercase-name>.md",
            "configuration_schemas": {
                "representation.json": Representation.model_json_schema(),
                "operations.json": Operations.model_json_schema(),
                "interventions.json": Interventions.model_json_schema(),
            },
        }
        # Remove oldest history entries before sacrificing current evidence or configuration.
        system = prompt("evolve") + "\nSchema: " + dumps(PlanDraft.model_json_schema())
        limit = int(settings.evolution_max_context_chars * settings.context_trim_ratio)
        for key in (
            "failed_proposals",
            "evaluations",
            "campaign_history",
            "activation_outcomes",
            "best_ever",
            "change_manifests",
            "history",
        ):
            while supplied[key] and len(dumps(supplied)) + len(system) > limit:
                supplied[key].pop(0 if key == "history" else -1)
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": dumps(supplied)},
        ]
        if len(dumps(messages)) > settings.evolution_max_context_chars:
            raise ContractError("Revision task anchors exceed the context budget")
        extra_tools = [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": schema.model_json_schema(),
                },
            }
            for name, description, schema in [
                (
                    "read_analysis",
                    "Read one supplied diagnosis or finding by ID; no source or judge access",
                    ReadAnalysisArgs,
                ),
                (
                    "validate_revision",
                    "Validate a proposed draft in isolation; no files or active revision are changed",
                    ValidateRevisionArgs,
                ),
                (
                    "register_finding",
                    "Register a reusable finding using exact evidence refs independently inspected in this evolution session; no files change",
                    RegisterFindingArgs,
                ),
            ]
        ]
        trace_access = None
        deadline = time.monotonic() + settings.evolution_timeout_seconds
        invalid = 0
        for turn in range(settings.evolution_turns):
            messages = trim_messages(messages, limit)
            available = (
                [*analysis_tools(), *extra_tools] if turn < settings.evolution_turns - 1 else []
            )
            try:
                reply = complete_before(provider, messages, available, deadline)
            except ProviderError as exc:
                store.save_artifact(
                    uuid4().hex,
                    "proposal_attempt",
                    {
                        "analysis_id": report.id,
                        "status": "provider_unavailable",
                        "error": _error_summary(exc),
                    },
                )
                # Transport owns retries; another stage retry multiplies its budget.
                raise
            messages.append(assistant_message(reply))
            if reply.tool_calls:
                for i, call in enumerate(reply.tool_calls):
                    try:
                        if not available or i >= settings.max_parallel_tool_calls:
                            raise ContractError("Evolution tool budget exceeded")
                        if call.name == "read_analysis":
                            args = ReadAnalysisArgs.model_validate(call.arguments)
                            collection = (
                                report.diagnoses
                                if args.kind == "diagnosis"
                                else list(_findings(store, report).values())
                            )
                            item = next(
                                (
                                    r
                                    for r in collection
                                    if (r.run_id if args.kind == "diagnosis" else r.id) == args.id
                                ),
                                None,
                            )
                            if item is None:
                                raise ContractError("Analysis ID is outside the supplied report")
                            result = item.model_dump(mode="json")
                        elif call.name == "validate_revision":
                            args = ValidateRevisionArgs.model_validate(call.arguments)
                            checked = candidate(store, _linked_plan(args.draft, report), report)
                            result = {
                                "valid": True,
                                "candidate_revision_id": checked.id,
                                "draft_fingerprint": fingerprint(args.draft.model_dump()),
                                "changed_files": [c.path for c in args.draft.changes],
                                "validation": "structural_only; business regression remains required",
                            }
                        elif call.name == "register_finding":
                            finding = RegisterFindingArgs.model_validate(call.arguments).finding
                            if not all(
                                value.strip()
                                for value in (
                                    finding.pattern,
                                    finding.suggested_change,
                                    finding.counterevidence,
                                    finding.uncertainty,
                                )
                            ):
                                raise ContractError(
                                    "Recovered finding needs a mechanism and evidence limitations"
                                )
                            if trace_access is None or any(
                                ref_key(ref) not in trace_access.delivered
                                for ref in finding.evidence
                            ):
                                raise ContractError(
                                    "Recovered finding requires independently inspected evidence"
                                )
                            targets = set(report.selected_run_ids)
                            rows = {row["id"]: row for row in store.runs(report.revision_id)}
                            support = set()
                            for ref in finding.evidence:
                                if ref.run_id not in targets:
                                    continue
                                row = rows[ref.run_id]
                                failed = row["status"] in {
                                    "contract_failed",
                                    "budget_exhausted",
                                } or (row["status"] == "completed" and row["outcome"] == "rejected")
                                if (finding.purpose == "error_repair") != failed:
                                    continue
                                groups = row["groups"]
                                if isinstance(groups, str):
                                    groups = json.loads(groups)
                                support.add((row["question"], tuple(sorted(groups))))
                            required = (
                                settings.finding_min_support
                                if finding.support_kind == "repeated"
                                else 1
                            )
                            if len(support) < required:
                                raise ContractError(
                                    "Recovered finding lacks independent question support for its declared purpose"
                                )
                            if finding.id in _findings(store, report):
                                raise ContractError("Finding ID is already registered")
                            store.save_artifact(
                                uuid4().hex,
                                "evolution_finding",
                                {
                                    "analysis_id": report.id,
                                    "revision_id": report.revision_id,
                                    "finding": finding.model_dump(mode="json"),
                                    "independently_inspected": True,
                                },
                            )
                            result = {"registered": True, "finding_id": finding.id}
                        elif call.name in {"inspect_trace", "search_trace", "read_tool_result"}:
                            if trace_access is None:
                                trace_access = TraceAccess(
                                    store, [*report.selected_run_ids, *report.control_run_ids]
                                )
                            result = trace_access.execute(call.name, call.arguments)
                        else:
                            raise ContractError("Unknown evolution tool")
                    except (EvoGError, ValueError) as exc:
                        result = {"valid": False, "error": _error_summary(exc)}
                    messages.append(
                        {"role": "tool", "tool_call_id": call.id, "content": dumps(result)}
                    )
                continue
            try:
                draft = PlanDraft.model_validate_json(reply.content)
                candidate(store, _linked_plan(draft, report), report)
                store.save_artifact(
                    uuid4().hex,
                    "proposal_attempt",
                    {
                        "analysis_id": report.id,
                        "status": "validated",
                        "turn": turn + 1,
                        "draft_fingerprint": fingerprint(draft.model_dump()),
                    },
                )
                break
            except (EvoGError, ValueError) as exc:
                invalid += 1
                error = _error_summary(exc)
                store.save_artifact(
                    uuid4().hex,
                    "proposal_attempt",
                    {
                        "analysis_id": report.id,
                        "status": "invalid",
                        "turn": turn + 1,
                        "error": error,
                    },
                )
                if invalid >= 3 or turn == settings.evolution_turns - 1:
                    raise
                messages.append(
                    {
                        "role": "user",
                        "content": "Draft validation failed: "
                        + error
                        + ". Repair the draft within the permitted surfaces. "
                        "Use validate_revision to check it. Return only the full corrected JSON; an empty changes list is valid when unsupported.",
                    }
                )
        else:
            raise ContractError("Evolution turn budget exhausted without a valid draft")
    plan = _linked_plan(draft, report)
    revised = candidate(store, plan, report)
    if plan.changes and revised.id == plan.parent_revision_id:
        plan = plan.model_copy(
            update={
                "changes": [],
                "summary": "Proposed behavior is already present in the active harness",
            }
        )
    store.save_artifact(plan.id, "plan", plan.model_dump(mode="json"))
    # Keep an immutable per-change manifest beside the plan.  Evaluation can
    # therefore attribute a later result even when the active revision has
    # moved on, and resume can recover without touching workspace files.
    _save_change_manifest(store, plan)
    return plan


def validation_outcome(row: dict) -> bool | None:
    if row.get("passed") is not None:
        return row["passed"]
    if row.get("status") in {"budget_exhausted", "contract_failed"}:
        return False
    return None


def record_evaluation(
    store: Store,
    plan: EvolutionPlan,
    report: AnalysisReport,
    before: list[dict],
    after: list[dict],
    *,
    accepted: bool,
    reason: str,
    validation_kind: str,
    selection_fingerprint: str,
) -> dict:
    manifest = _load_change_manifest(store, plan)
    evaluation_id = "evaluation:" + fingerprint(
        {
            "plan_id": plan.id,
            "analysis_id": report.id,
            "before": before,
            "after": after,
            "accepted": accepted,
            "reason": reason,
            "validation_kind": validation_kind,
            "selection_fingerprint": selection_fingerprint,
        }
    )
    try:
        return store.artifact(evaluation_id, "evaluation")
    except ContractError:
        pass

    def trial_key(row: dict) -> tuple[str, int]:
        return row["episode_id"], row.get("trial_index", 1)

    paired = {trial_key(row): row for row in after}
    baseline = {trial_key(row): row for row in before}
    if (
        len(paired) != len(after)
        or len(baseline) != len(before)
        or baseline.keys() != paired.keys()
    ):
        raise ContractError("Evaluation requires unique, identical episode/trial pairs")
    by_run = {r.get("run_id"): (r, paired[trial_key(r)]) for r in before}
    findings = _findings(store, report)
    effects = []
    manifest_changes = {
        row.get("path"): row for row in manifest.get("changes", []) if isinstance(row, dict)
    }
    for change in plan.changes:
        predicted = change.predicted_fix_runs or sorted(
            {ref.run_id for f in change.finding_ids for ref in findings[f].evidence}
        )
        risk = change.risk_runs or report.control_run_ids
        linked = set(predicted + risk)
        fixes = regressions = unknown = 0
        for run_id in linked:
            if run_id not in by_run:
                unknown += 1
                continue
            a, b = (validation_outcome(r) for r in by_run[run_id])
            if a is None or b is None:
                unknown += 1
            else:
                fixes += run_id in predicted and a is False and b is True
                regressions += a is True and b is False
        classification = (
            "MIXED"
            if fixes and regressions
            else "HARMFUL"
            if regressions
            else "EFFECTIVE"
            if fixes and fixes >= len(predicted)
            else "PARTIALLY_EFFECTIVE"
            if fixes
            else "UNVERIFIED"
            if unknown
            else "INEFFECTIVE"
        )
        manifest_row = manifest_changes.get(change.path, {})
        change_id = manifest_row.get("change_id") or f"{plan.id}:{len(effects) + 1}"
        actually_fixed = [
            run_id
            for run_id in predicted
            if run_id in by_run
            and validation_outcome(by_run[run_id][0]) is False
            and validation_outcome(by_run[run_id][1]) is True
        ]
        risk_realized = [
            run_id
            for run_id in risk
            if run_id in by_run
            and validation_outcome(by_run[run_id][0]) is True
            and validation_outcome(by_run[run_id][1]) is False
        ]
        effects.append(
            {
                "change_id": change_id,
                "entry": change.interface,
                "path": change.path,
                "predicted_fix_runs": predicted,
                "risk_runs": risk,
                "observed_fixes": fixes,
                "observed_regressions": regressions,
                "unresolved": unknown,
                "classification": classification,
                "actually_fixed": actually_fixed,
                "still_failed": [
                    run_id
                    for run_id in predicted
                    if run_id in by_run and validation_outcome(by_run[run_id][1]) is False
                ],
                "unverified_runs": [
                    run_id
                    for run_id in linked
                    if run_id not in by_run
                    or any(validation_outcome(row) is None for row in by_run[run_id])
                ],
                "risk_realized": risk_realized,
                "verdict": classification,
                "association_only": True,
            }
        )
    payload = {
        "id": evaluation_id,
        "plan_id": plan.id,
        "parent_revision_id": plan.parent_revision_id,
        "candidate_revision_id": candidate(store, plan, report).id,
        "accepted": accepted,
        "reason": reason,
        "validation_kind": validation_kind,
        "selection_fingerprint": selection_fingerprint,
        "metric_unit": "trial",
        "changes": effects,
        "before_passed": sum(r.get("passed") is True for r in before),
        "after_passed": sum(r.get("passed") is True for r in after),
        "before_total": len(before),
        "after_total": len(after),
        "before_fully_scored": bool(before)
        and all(row.get("passed") is not None for row in before),
        "after_fully_scored": bool(after) and all(row.get("passed") is not None for row in after),
        "note": "Observed associations across a combined candidate; not causal attribution to individual edits.",
        "change_manifest_id": manifest["id"],
    }
    comparable = [
        r
        for r in store.artifacts("evaluation", limit=100)
        if r.get("selection_fingerprint") == selection_fingerprint
    ]
    observed = [
        {"revision_id": plan.parent_revision_id, "passed": payload["before_passed"]},
        {"revision_id": payload["candidate_revision_id"], "passed": payload["after_passed"]},
        *[
            observation
            for r in comparable
            for observation in (
                {"revision_id": r["parent_revision_id"], "passed": r["before_passed"]},
                {"revision_id": r["candidate_revision_id"], "passed": r["after_passed"]},
            )
        ],
    ]
    payload["best_observed"] = max(observed, key=lambda r: r["passed"])
    payload["best_observed"]["activation_required"] = True
    # Persist a comparable best-ever pointer.  It is deliberately advisory:
    # activation and rollback remain explicit Store operations, so an
    # evaluation can never silently replace the active harness.
    total = len(after)
    best_candidates = []
    for revision_id, rows in (
        (plan.parent_revision_id, before),
        (payload["candidate_revision_id"], after),
    ):
        if rows and all(row.get("passed") is not None for row in rows):
            passed = sum(row.get("passed") is True for row in rows)
            best_candidates.append(
                {
                    "revision_id": revision_id,
                    "passed": passed,
                    "total": len(rows),
                    "pass_rate": passed / len(rows),
                    "fully_scored": True,
                    "metric_unit": "trial",
                    "evaluation_id": payload["id"],
                }
            )
    prior_best = [
        row
        for row in store.artifacts("best_ever", limit=100)
        if row.get("selection_fingerprint") == selection_fingerprint
        and row.get("fully_scored") is True
    ]
    if prior_best:
        best_candidates.extend(
            {
                "revision_id": row["revision_id"],
                "passed": row["passed"],
                "total": row.get("total", total),
                "pass_rate": row.get("pass_rate", 0.0),
                "fully_scored": True,
                "metric_unit": "trial",
                "evaluation_id": row.get("evaluation_id"),
            }
            for row in prior_best
        )
    best = None
    if best_candidates:
        best = max(best_candidates, key=lambda row: (row["pass_rate"], row["passed"]))
        best = {
            **best,
            "selection_fingerprint": selection_fingerprint,
            "activation_required": True,
            "source_evaluation_id": payload["id"],
        }
    payload["best_ever"] = best
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute(
            "SELECT payload FROM artifacts WHERE id=? AND kind='evaluation'", (evaluation_id,)
        ).fetchone()
        if existing is not None:
            return json.loads(existing[0])
        db.execute(
            "INSERT INTO artifacts VALUES(?,?,?)", (evaluation_id, "evaluation", dumps(payload))
        )
        if best is not None:
            db.execute(
                "INSERT INTO artifacts VALUES(?,?,?)",
                (f"best:{evaluation_id}", "best_ever", dumps(best)),
            )
    return payload


def apply(store: Store, plan_id: str, validator: Validator | None = None) -> AppliedRevision:
    plan = EvolutionPlan.model_validate(store.artifact(plan_id, "plan"))
    report = AnalysisReport.model_validate(store.artifact(plan.analysis_id, "analysis"))
    if not plan.changes:
        raise ContractError("Plan has no changes to activate")
    next_harness = candidate(store, plan, report)
    if next_harness.id == plan.parent_revision_id:
        raise ContractError("Plan does not change the harness")
    validation = "structural"
    if validator is not None:
        if validator(next_harness) is not True:
            raise ContractError(
                "Business validation rejected the candidate; active revision is unchanged"
            )
        validation = "business"
    store.activate(next_harness, plan.parent_revision_id, plan.id, validation)
    return AppliedRevision(
        revision_id=next_harness.id,
        parent_revision_id=plan.parent_revision_id,
        plan_id=plan.id,
        validation=validation,
    )


def best_ever_for(store: Store, selection_fingerprint: str) -> dict[str, Any] | None:
    """Return the highest observed revision for one comparable evaluation set."""
    rows = [
        row
        for row in store.artifacts("best_ever", limit=100)
        if row.get("selection_fingerprint") == selection_fingerprint
        and row.get("fully_scored") is True
    ]
    if not rows:
        return None
    return max(rows, key=lambda row: (row.get("pass_rate", 0.0), row.get("passed", 0)))


def auto_rollback(
    store: Store,
    selection_fingerprint: str,
) -> str | None:
    """Restore the recorded best revision after an explicit regression decision.

    This is intentionally an opt-in operation; evaluation never calls it
    implicitly.  A candidate staged for evaluation cannot become active
    through this recovery action.  The returned revision ID is suitable for
    a CLI audit message.
    """
    revisions = {row["id"]: row for row in store.revisions()}
    with store.connect() as db:
        artifacts = [
            (row["kind"], json.loads(row["payload"]))
            for row in db.execute(
                "SELECT kind,payload FROM artifacts WHERE kind IN "
                "('activation_outcome','revision_activation','evaluation','best_ever')"
            )
        ]
    activated_ids = {
        row.get("revision_id")
        for kind, row in artifacts
        if kind in {"activation_outcome", "revision_activation"}
        and row.get("status", "activated") == "activated"
    }
    observations = []
    for kind, row in artifacts:
        if row.get("selection_fingerprint") != selection_fingerprint:
            continue
        if kind == "best_ever" and row.get("fully_scored") is True:
            observations.append(row)
        elif kind == "evaluation":
            for stage, field in (
                ("before", "parent_revision_id"),
                ("after", "candidate_revision_id"),
            ):
                total = row.get(f"{stage}_total", 0)
                if row.get(f"{stage}_fully_scored") is True and total:
                    passed = row[f"{stage}_passed"]
                    observations.append(
                        {
                            "revision_id": row[field],
                            "passed": passed,
                            "pass_rate": passed / total,
                            "evaluation_id": row["id"],
                        }
                    )
    recoverable = [
        observation
        for observation in observations
        if (revision := revisions.get(observation.get("revision_id"))) is not None
        and (
            revision.get("parent_id") is None
            or revision.get("validation") == "business"
            or revision["id"] in activated_ids
        )
    ]
    if not recoverable:
        return None
    best = max(recoverable, key=lambda row: (row.get("pass_rate", 0.0), row.get("passed", 0)))
    revision_id = best["revision_id"]
    current = store.harness().id
    if current == revision_id:
        return current
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        active = db.execute("SELECT value FROM state WHERE key='active_revision'").fetchone()[0]
        if active != current:
            raise ConflictError("Active revision changed during best-ever recovery")
        db.execute("UPDATE state SET value=? WHERE key='active_revision'", (revision_id,))
        db.execute(
            "INSERT INTO artifacts VALUES(?,?,?)",
            (
                uuid4().hex,
                "rollback",
                dumps(
                    {
                        "from": current,
                        "to": revision_id,
                        "reason": "best_ever_regression_recovery",
                        "selection_fingerprint": selection_fingerprint,
                        "evaluation_id": best.get("evaluation_id")
                        or best.get("source_evaluation_id"),
                    }
                ),
            ),
        )
    return revision_id
