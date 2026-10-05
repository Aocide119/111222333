"""Confidence selection, addressable trace inspection, and bucketed synthesis."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from uuid import uuid4

from pydantic import Field, ValidationError

from evog.config import Settings
from evog.errors import ContractError, EvoGError
from evog.harness import prompt
from evog.io import dumps, fingerprint, safe_path
from evog.models import AnalysisReport, Diagnosis, EvidenceRef, Finding, QueryBucket, Record
from evog.pointers import resolve_pointer
from evog.providers import Provider, assistant_message, complete_before, stage_provider
from evog.runtime import trim_messages
from evog.store import Store


class InspectArgs(Record):
    run_id: str
    event_index: int = Field(ge=0)
    field_path: str = ""
    start_char: int = Field(default=0, ge=0)
    limit: int = Field(default=8000, ge=1, le=12000)


class SearchTraceArgs(Record):
    run_id: str
    term: str = Field(min_length=1, max_length=256)
    offset: int = Field(default=0, ge=0)


class ToolResultReadArgs(Record):
    run_id: str
    path: str
    start_char: int = Field(default=0, ge=0)
    limit: int = Field(default=12000, ge=1, le=60000)


class Findings(Record):
    findings: list[Finding] = Field(max_length=30)


def ref_key(ref: EvidenceRef) -> str:
    return dumps(ref.model_dump())


class TraceAccess:
    def __init__(self, store: Store, allowed: list[str]):
        self.store = store
        self.events = {run: store.events(run) for run in allowed}
        self.delivered: set[str] = set()
        self.archives: dict[tuple[str, str], int] = {}
        for run_id, events in self.events.items():
            for event in events:
                path = event.data.get("persisted_path") if event.kind == "tool" else None
                if isinstance(path, str) and path:
                    self.archives[(run_id, path)] = event.index

    def summary(self, run_id: str) -> dict[str, Any]:
        return {
            "run_id": run_id,
            "events": [
                {
                    "index": event.index,
                    "kind": event.kind,
                    "chars": len(dumps(event.data)),
                    **({"tool_name": event.data["name"]} if event.kind == "tool" else {}),
                }
                for event in self.events[run_id]
            ],
        }

    def inspect(self, args: InspectArgs) -> dict[str, Any]:
        if args.run_id not in self.events:
            raise ContractError("Trace is outside the analysis scope")
        events = self.events[args.run_id]
        if args.event_index >= len(events):
            raise ContractError("Event index is out of range")
        event = events[args.event_index]
        value = resolve_pointer(event.data, args.field_path)
        text = value if isinstance(value, str) else dumps(value)
        if args.start_char >= len(text):
            raise ContractError("No nonempty evidence at start_char")
        end = min(args.start_char + args.limit, len(text))
        ref = EvidenceRef(
            run_id=args.run_id,
            event_index=event.index,
            field_path=args.field_path,
            start_char=args.start_char,
            end_char=end,
        )
        self.delivered.add(ref_key(ref))
        return {
            "evidence_ref": ref.model_dump(),
            "kind": event.kind,
            "content": text[args.start_char : end],
            "total_chars": len(text),
            "truncated": end < len(text),
            "next_start_char": end if end < len(text) else None,
        }

    def search(self, args: SearchTraceArgs) -> dict[str, Any]:
        if args.run_id not in self.events:
            raise ContractError("Trace is outside the analysis scope")
        matching = [
            event
            for event in self.events[args.run_id]
            if args.term.casefold() in dumps(event.data).casefold()
        ]
        selected = matching[args.offset : args.offset + 10]
        next_offset = args.offset + len(selected)
        return {
            "events": [{"index": event.index, "kind": event.kind} for event in selected],
            "total": len(matching),
            "next_offset": next_offset if next_offset < len(matching) else None,
            "note": "Search locates events; inspect_trace is required before citing them.",
        }

    def read_tool_result(self, args: ToolResultReadArgs) -> dict[str, Any]:
        if args.run_id not in self.events:
            raise ContractError("Trace is outside the analysis scope")
        event_index = self.archives.get((args.run_id, args.path))
        if event_index is None or not args.path.startswith("tool_results/"):
            raise ContractError("Archive path is not present in the authorized trace")
        actual = safe_path(
            self.store.workspace / "tool_results" / args.run_id,
            args.path.removeprefix("tool_results/"),
        )
        if not actual.is_file():
            raise ContractError("Archived tool result is unavailable")
        text = actual.read_text(encoding="utf-8")
        total = len(text)
        if args.start_char >= total:
            raise ContractError("No nonempty evidence at start_char")
        end = min(args.start_char + args.limit, total)
        ref = EvidenceRef(
            run_id=args.run_id,
            event_index=event_index,
            field_path=f"/archive/{args.path}",
            start_char=args.start_char,
            end_char=end,
        )
        self.delivered.add(ref_key(ref))
        return {
            "evidence_ref": ref.model_dump(),
            "content": text[args.start_char : end],
            "total_chars": total,
            "truncated": end < total,
            "next_start_char": end if end < total else None,
        }

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "inspect_trace":
            return self.inspect(InspectArgs.model_validate(arguments))
        if name == "search_trace":
            return self.search(SearchTraceArgs.model_validate(arguments))
        if name == "read_tool_result":
            return self.read_tool_result(ToolResultReadArgs.model_validate(arguments))
        raise ContractError("Unknown analysis tool")


def _trial_group(
    store: Store, run: dict[str, Any], scope_run_ids: set[str] | None = None
) -> list[dict[str, Any]]:
    """Return the other recorded trials for the same question in this revision.

    A benchmark can execute one question more than once.  The product store has no
    benchmark-specific trial table, so the stable native key is the exact question
    text together with its revision.  This is metadata only: each diagnosis still
    has to inspect and cite its own trace.
    """
    target_groups = run.get("groups", [])
    rows = []
    for row in store.runs(run["revision_id"]):
        if scope_run_ids is not None and row["id"] not in scope_run_ids:
            continue
        row_groups = row.get("groups", [])
        if isinstance(row_groups, str):
            try:
                row_groups = json.loads(row_groups)
            except (TypeError, json.JSONDecodeError):
                row_groups = []
        if row["question"] == run["question"] and sorted(row_groups) == sorted(target_groups):
            rows.append(row)
    group: list[dict[str, Any]] = []
    for row in reversed(rows):
        events = store.events(row["id"])
        error_codes = [
            str(event.data.get("code"))
            for event in events
            if event.kind == "error" and event.data.get("code")
        ]
        timeout = row["status"] in {"budget_exhausted", "cancelled"} or (
            "question_timeout" in error_codes
        )
        answer = row.get("answer") or {}
        reflection = answer.get("reflection") if isinstance(answer, dict) else None
        group.append(
            {
                "run_id": row["id"],
                "status": row["status"],
                "verifier_outcome": row.get("outcome") or "unjudged",
                "confidence": answer.get("confidence"),
                "timeout": timeout,
                "error_codes": error_codes[:8],
                "answer_bias": _answer_bias(answer, reflection),
            }
        )
    return group


def _answer_bias(answer: Any, reflection: Any = None) -> str:
    """Keep the answering agent's self-report in a bounded, readable form."""
    if isinstance(answer, dict) and answer.get("answer_bias"):
        return str(answer["answer_bias"])[:4000]
    if not isinstance(reflection, dict):
        return ""
    parts = []
    for key in ("evidence_found", "unresolved", "limitation"):
        value = reflection.get(key)
        if isinstance(value, list):
            value = "; ".join(str(item) for item in value)
        if value:
            parts.append(f"{key}: {value}")
    return " | ".join(parts)[:4000]


def analysis_tools() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": model.model_json_schema(),
            },
        }
        for name, model, description in (
            (
                "inspect_trace",
                InspectArgs,
                "Inspect an event or JSON-pointer field in bounded character ranges; cite the returned evidence_ref exactly.",
            ),
            (
                "search_trace",
                SearchTraceArgs,
                "Locate event indices with a literal anchor. Search results are not inspected evidence.",
            ),
            (
                "read_tool_result",
                ToolResultReadArgs,
                "Read a bounded range from a complete tool_results archive explicitly recorded by a trace event; cite the returned evidence_ref exactly.",
            ),
        )
    ]


def select_experience(
    store: Store, settings: Settings, revision_id: str, *, run_ids: list[str] | None = None
) -> dict[str, Any]:
    coverage = {
        name: 0
        for name in (
            "total",
            "incidents",
            "provider_incidents",
            "tool_incidents",
            "contract_failures",
            "budget_exhausted",
            "unfinished",
            "CC",
            "CW",
            "UC",
            "UW",
            "unjudged_high",
            "unjudged_low",
            "missing_confidence",
        )
    }
    selected, controls = [], []
    priorities = {}
    rows = store.runs(revision_id)
    if run_ids is not None:
        if not set(run_ids).issubset({r["id"] for r in rows}):
            raise ContractError("Experience scope contains interactions outside this revision")
        rows = [r for r in rows if r["id"] in set(run_ids)]
    for run in rows:
        coverage["total"] += 1
        if run["status"] != "completed":
            label = {
                "contract_failed": "contract_failures",
                "budget_exhausted": "budget_exhausted",
                "provider_failed": "provider_incidents",
                "tool_failed": "tool_incidents",
                "incident": "incidents",
            }.get(run["status"], "unfinished")
            coverage[label] += 1
            if run["status"] in ("contract_failed", "budget_exhausted"):
                selected.append(run["id"])
                priorities[run["id"]] = (0, run["created_at"], run["id"])
            elif run["status"] in ("provider_failed", "tool_failed"):
                coverage["incidents"] += 1
            continue
        confidence = (run.get("answer") or {}).get("confidence")
        if confidence is None:
            coverage["missing_confidence"] += 1
            continue
        low = confidence <= settings.confidence_threshold
        outcome = run["outcome"]
        label = (
            ("U" if low else "C") + ("C" if outcome == "accepted" else "W")
            if outcome
            else ("unjudged_low" if low else "unjudged_high")
        )
        coverage[label] += 1
        if label in ("CW", "UC", "UW", "unjudged_low"):
            selected.append(run["id"])
            priorities[run["id"]] = (
                0 if label in ("CW", "UW") else 1,
                run["created_at"],
                run["id"],
            )
        elif label == "CC":
            controls.append(run["id"])
    selected.sort(key=priorities.__getitem__)
    eligible_before = len(selected)
    # ``analysis_max_runs`` is a question/job budget.  Keep every eligible trial
    # for a selected question so the debugger can compare repeated executions.
    row_by_id = {row["id"]: row for row in rows}

    def question_key(row: dict[str, Any]) -> tuple[str, tuple[str, ...]]:
        groups = row.get("groups", [])
        if isinstance(groups, str):
            try:
                groups = json.loads(groups)
            except (TypeError, json.JSONDecodeError):
                groups = []
        return row["question"], tuple(sorted(groups))

    selected_group_order: list[tuple[str, tuple[str, ...]]] = []
    for run_id in selected:
        key = question_key(row_by_id[run_id])
        if key not in selected_group_order:
            selected_group_order.append(key)
    selected_groups = set(
        selected_group_order
        if settings.analysis_all_eligible or settings.analysis_max_runs is None
        else selected_group_order[: settings.analysis_max_runs]
    )
    selected = [run_id for run_id in selected if question_key(row_by_id[run_id]) in selected_groups]
    controls.sort()
    coverage["eligible"] = eligible_before
    coverage["eligible_questions"] = len(selected_group_order)
    coverage["omitted_questions"] = len(selected_group_order) - len(selected_groups)
    coverage["selected"] = len(selected_groups)
    coverage["controls"] = min(len(controls), 2)
    return {
        "coverage": coverage,
        "selected": selected,
        "controls": controls[:2],
    }


def diagnose(
    store: Store,
    provider: Provider,
    settings: Settings,
    run_id: str,
    controls: list[str],
    access: TraceAccess,
    scope_run_ids: set[str] | None = None,
) -> Diagnosis:
    run = store.run(run_id)
    provider = stage_provider(provider, settings, "analysis")
    deadline = time.monotonic() + settings.analysis_timeout_seconds
    answer = run.get("answer") or {}
    reflection = answer.get("reflection") if isinstance(answer, dict) else None
    trial_group = _trial_group(store, run, scope_run_ids or set(access.events))
    row_outcome = next(
        (item["outcome"] for item in store.runs(run["revision_id"]) if item["id"] == run_id),
        None,
    )
    timeout_events = [
        {
            "code": event.data.get("code"),
            "reason": event.data.get("reason"),
            "phase": event.data.get("phase"),
        }
        for event in store.events(run_id)
        if event.kind in {"error", "budget"}
        and (event.kind == "error" or event.data.get("phase") == "stopped")
    ]
    supplied = {
        "run_id": run_id,
        "question": run["question"],
        "status": run["status"],
        "subjective_confidence": answer.get("confidence"),
        "self_report": reflection,
        "answer_bias": _answer_bias(answer, reflection),
        "timeout": {
            "detected": any(item.get("code") == "question_timeout" for item in timeout_events)
            or run["status"] in {"budget_exhausted", "cancelled"},
            "events": timeout_events[:12],
        },
        "verifier": {
            "outcome": row_outcome or "unjudged",
            "source": "recorded feedback" if row_outcome else "unavailable",
        },
        "trial_group": trial_group,
        "trace": access.summary(run_id),
        "controls": [
            {"question": store.run(control)["question"], "trace": access.summary(control)}
            for control in controls
        ],
    }
    # Only a categorical feedback signal is exposed. No hidden expected answer exists.
    supplied["feedback_outcome"] = supplied["verifier"]["outcome"]
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": prompt("analyze") + "\nSchema: " + dumps(Diagnosis.model_json_schema()),
        },
        {"role": "user", "content": dumps(supplied)},
    ]
    inspected_here: set[str] = set()
    format_repairs = 0
    for turn in range(settings.analysis_turns):
        messages = trim_messages(
            messages, int(settings.analysis_max_context_chars * settings.context_trim_ratio)
        )
        if len(dumps(messages)) > settings.analysis_max_context_chars:
            raise ContractError("Analysis task anchors exceed the context budget")
        # Transport retries belong to the provider. A second retry loop here would
        # multiply the configured number of API attempts and consume analysis turns.
        reply = complete_before(
            provider,
            messages,
            analysis_tools() if turn < settings.analysis_turns - 1 else [],
            deadline,
        )
        messages.append(assistant_message(reply))
        if reply.tool_calls:
            if turn == settings.analysis_turns - 1:
                raise ContractError("No diagnosis returned at the analysis budget boundary")
            for call in reply.tool_calls:
                try:
                    result = access.execute(call.name, call.arguments)
                    if "evidence_ref" in result:
                        inspected_here.add(dumps(result["evidence_ref"]))
                except (EvoGError, ValueError):
                    result = {"error": "Invalid analysis request; check trace scope and schema"}
                messages.append({"role": "tool", "tool_call_id": call.id, "content": dumps(result)})
            continue
        try:
            diagnosis = Diagnosis.model_validate_json(reply.content)
            if diagnosis.run_id != run_id:
                raise ContractError("Diagnosis changed its target run")
            if any(ref_key(ref) not in inspected_here for ref in diagnosis.evidence):
                raise ContractError("Diagnosis cites trace fields or ranges it did not inspect")
            if not any(ref.run_id == run_id for ref in diagnosis.evidence):
                raise ContractError("Diagnosis must inspect the target interaction")
            check_diagnosis_quality(diagnosis)
            return diagnosis
        except (ValueError, ContractError) as exc:
            if format_repairs >= 2 or turn == settings.analysis_turns - 1:
                raise
            format_repairs += 1
            errors = (
                [
                    {"field": list(e["loc"]), "error": e["msg"]}
                    for e in exc.errors(include_input=False, include_url=False)
                ]
                if isinstance(exc, ValidationError)
                else str(exc)
            )
            messages.append(
                {
                    "role": "user",
                    "content": "The diagnosis failed validation: "
                    + dumps(errors)[:2000]
                    + ". Return only the corrected JSON object. Do not add fields. "
                    "Infer a short open semantic query_type from the question; query_family is only a legacy classification. Copy evidence_ref values exactly "
                    "from tool results you inspected. Give a supported mechanism and actionable implication, "
                    "or explicitly describe what remains unknown; never invent a cause to pass validation.",
                }
            )

    raise ContractError("Analysis turn budget exhausted")


def check_diagnosis_quality(diagnosis: Diagnosis) -> None:
    fields = [
        diagnosis.earliest_break,
        diagnosis.cause,
        diagnosis.condensed_rationale,
        diagnosis.actionable_implication,
        diagnosis.uncertainty,
    ]
    if any(not value.strip() for value in fields):
        raise ContractError(
            "Diagnosis must include a mechanism or explicit uncertainty and a next step"
        )
    placeholders = {
        "n/a",
        "none",
        "unknown",
        "unavailable",
        "see trace",
        "metadata",
        "not applicable",
    }
    if diagnosis.category != "unknown" and all(
        value.strip().lower() in placeholders for value in fields[1:3]
    ):
        raise ContractError("Metadata or placeholders do not establish a root-cause diagnosis")
    if diagnosis.actionable_implication.strip().lower() in placeholders:
        raise ContractError(
            "Diagnosis needs a reusable implication or a specific missing-evidence check"
        )


def normalize_query_type(label: str) -> str:
    """Normalize spelling only; semantic alias merging is deliberately not inferred."""
    normalized = " ".join(label.split()).casefold()
    if not normalized:
        raise ContractError("A query bucket needs a nonblank semantic label")
    return normalized


def _experience_cell(row: dict[str, Any], threshold: float) -> str:
    if row["status"] != "completed":
        return row["status"]
    confidence = (row.get("answer") or {}).get("confidence")
    if confidence is None:
        return "missing_confidence"
    prefix = "U" if confidence <= threshold else "C"
    if row.get("outcome") is None:
        return "unjudged_low" if prefix == "U" else "unjudged_high"
    return prefix + ("C" if row["outcome"] == "accepted" else "W")


def _synthesis_input(
    phase: str,
    units: list[tuple[str, dict[str, Any]]],
    coverage: dict[str, int],
    incomplete: dict[str, str],
    minimum_support: int,
) -> dict[str, Any]:
    buckets: dict[str, list[dict[str, Any]]] = {}
    for label, unit in units:
        buckets.setdefault(label, []).append(unit)
    return {
        "phase": phase,
        "coverage": coverage,
        "buckets": buckets,
        "finding_min_support": minimum_support,
        "incomplete": {"unavailable_items": len(incomplete)},
        "coverage_note": (
            "Only complete included items support this batch. Missing items and other batches "
            "are not evidence. Bucket inputs contain condensed diagnoses; cross_bucket inputs "
            "contain verified bucket findings. Unjudged and nonsemantic failures retain their labels."
        ),
    }


def _synthesis_batches(
    phase: str,
    units: list[tuple[str, dict[str, Any]]],
    coverage: dict[str, int],
    incomplete: dict[str, str],
    settings: Settings,
    system: str,
) -> tuple[list[list[tuple[str, dict[str, Any]]]], list[tuple[str, dict[str, Any]]]]:
    """Pack complete inputs; overlarge items are explicitly reported, never sliced."""
    limit = int(settings.synthesis_max_context_chars * settings.context_trim_ratio)

    def fits(batch: list[tuple[str, dict[str, Any]]]) -> bool:
        supplied = _synthesis_input(
            phase, batch, coverage, incomplete, settings.finding_min_support
        )
        return (
            len(
                dumps(
                    [
                        {"role": "system", "content": system},
                        {"role": "user", "content": dumps(supplied)},
                    ]
                )
            )
            <= limit
        )

    batches, omitted, current = [], [], []
    for item in units:
        if fits([*current, item]):
            current.append(item)
            continue
        if current:
            batches.append(current)
            current = []
        if fits([item]):
            current = [item]
        else:
            omitted.append(item)
    if current:
        batches.append(current)
    return batches, omitted


def _synthesize_batch(
    provider: Provider,
    settings: Settings,
    phase: str,
    units: list[tuple[str, dict[str, Any]]],
    coverage: dict[str, int],
    incomplete: dict[str, str],
    system: str,
    rows: dict[str, dict[str, Any]],
    targets_by_purpose: dict[str, set[str]],
    key_for: Callable[[str], tuple[str, tuple[str, ...]]],
) -> list[Finding]:
    supplied = _synthesis_input(phase, units, coverage, incomplete, settings.finding_min_support)
    allowed_refs = {
        ref_key(EvidenceRef.model_validate(ref)) for _, unit in units for ref in unit["evidence"]
    }
    refs_by_query_type: dict[str, set[str]] = {}
    for _, unit in units:
        unit_refs = {ref_key(EvidenceRef.model_validate(ref)) for ref in unit["evidence"]}
        for label in unit.get("query_types", [unit["query_type"]]):
            refs_by_query_type.setdefault(normalize_query_type(label), set()).update(unit_refs)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": dumps(supplied)},
    ]
    limit = int(settings.synthesis_max_context_chars * settings.context_trim_ratio)
    deadline = time.monotonic() + settings.synthesis_timeout_seconds
    bounded_provider = stage_provider(provider, settings, "synthesis")
    for attempt in range(settings.call_attempts):
        messages = trim_messages(messages, limit)
        if len(dumps(messages)) > settings.synthesis_max_context_chars:
            raise ContractError("Synthesis anchors exceed the context budget")
        # Transport failure ends this batch; only invalid model output gets a
        # bounded correction here. The provider owns retry/backoff for HTTP calls.
        reply = complete_before(bounded_provider, messages, [], deadline)
        try:
            drafted = Findings.model_validate_json(reply.content).findings
            if len({finding.id for finding in drafted}) != len(drafted):
                raise ContractError("Finding IDs must be unique within a synthesis batch")
            for finding in drafted:
                if not finding.query_types or not all(
                    value.strip()
                    for value in (
                        finding.pattern,
                        finding.suggested_change,
                        finding.counterevidence,
                        finding.uncertainty,
                    )
                ):
                    raise ContractError(
                        "A finding needs query labels, a mechanism, and evidence limitations"
                    )
                if any(ref_key(ref) not in allowed_refs for ref in finding.evidence):
                    raise ContractError(
                        "Finding references evidence outside complete included items"
                    )
                cited_refs = {ref_key(ref) for ref in finding.evidence}
                if any(
                    not cited_refs.intersection(
                        refs_by_query_type.get(normalize_query_type(label), set())
                    )
                    for label in finding.query_types
                ):
                    raise ContractError(
                        "Each finding query type needs evidence from an included unit of that type"
                    )
                supporting = {
                    key_for(ref.run_id)
                    for ref in finding.evidence
                    if ref.run_id in rows and ref.run_id in targets_by_purpose[finding.purpose]
                }
                minimum = settings.finding_min_support if finding.support_kind == "repeated" else 1
                if len(supporting) < minimum:
                    raise ContractError(
                        "Finding lacks independent question support for its declared purpose"
                    )
            return drafted
        except (ValueError, ContractError):
            if attempt + 1 == settings.call_attempts:
                raise
            messages.extend(
                [
                    {"role": "assistant", "content": reply.content},
                    {
                        "role": "user",
                        "content": (
                            "Return only corrected findings JSON. Copy exact references and query labels from "
                            "included items. Repeated patterns need distinct question-and-group scopes; "
                            "isolated observations must be labelled isolated. Error repair needs evidence "
                            "from a selected failed interaction; accepted uncertainty is fragile success, "
                            "not an error. Unjudged uncertainty is not UC. Empty findings are valid."
                        ),
                    },
                ]
            )
    raise ContractError("Synthesis output correction budget exhausted")


def analyze(
    store: Store,
    provider: Provider | None,
    settings: Settings,
    revision_id: str | None = None,
    *,
    run_ids: list[str] | None = None,
) -> AnalysisReport:
    revision_id = store.harness(revision_id).id
    selection = select_experience(store, settings, revision_id, run_ids=run_ids)
    selected, controls = selection["selected"], selection["controls"]
    analysis_scope = (
        set(run_ids) if run_ids is not None else {row["id"] for row in store.runs(revision_id)}
    )
    diagnoses, incomplete = [], {}
    rows = {row["id"]: row for row in store.runs(revision_id)}

    def key_for(run_id: str) -> tuple[str, tuple[str, ...]]:
        row = rows[run_id]
        groups = row.get("groups", [])
        if isinstance(groups, str):
            try:
                groups = json.loads(groups)
            except (TypeError, json.JSONDecodeError):
                groups = []
        return row["question"], tuple(sorted(groups))

    job_ids: list[str] = []
    job_keys: set[tuple[str, tuple[str, ...]]] = set()
    for run_id in selected:
        key = key_for(run_id)
        if key not in job_keys:
            job_keys.add(key)
            job_ids.append(run_id)
    failures = [
        r for r in selected if rows[r]["status"] != "completed" or rows[r]["outcome"] == "rejected"
    ]
    calibration = [r for r in selected if r not in failures]

    def inspect_run(run_id: str) -> Diagnosis | None:
        try:
            if provider is None:
                raise ContractError("A provider is required for selected experience analysis")
            target = store.run(run_id)
            # Give the debugger read-only access to every trial of this exact
            # question, while retaining a hard requirement to cite the target run.
            trial_ids = [item["run_id"] for item in _trial_group(store, target, analysis_scope)]
            return diagnose(
                store,
                provider,
                settings,
                run_id,
                controls,
                TraceAccess(store, list(dict.fromkeys([*trial_ids, *controls]))),
            )
        except (EvoGError, ValueError):
            return None

    def collect(run_id: str, diagnosis: Diagnosis | None) -> None:
        if diagnosis is None:
            incomplete[run_id] = "Diagnosis unavailable or failed evidence validation"
        else:
            diagnoses.append(diagnosis)

    if settings.analysis_concurrency == 1:
        for run_id in job_ids:
            collect(run_id, inspect_run(run_id))
    else:
        with ThreadPoolExecutor(max_workers=settings.analysis_concurrency) as pool:
            for run_id, diagnosis in zip(job_ids, pool.map(inspect_run, job_ids), strict=True):
                collect(run_id, diagnosis)
    coverage = dict(selection["coverage"])
    required = min(settings.analysis_min_valid, len(job_ids))
    coverage.update(
        {
            "valid": len(diagnoses),
            "unavailable": len(incomplete),
            "required_valid": required,
            "failure_selected": len(failures),
            "calibration_selected": len(calibration),
            "raw_scope_size": len(analysis_scope),
            "missing_count": len(incomplete),
            "jobs": len(job_ids),
        }
    )
    targets_by_purpose = {
        "error_repair": set(failures),
        "uncertainty_calibration": set(calibration),
    }
    ready = (
        bool(job_ids)
        and len(diagnoses) >= required
        and len(diagnoses) / len(job_ids) >= settings.analysis_min_coverage
    )
    query_buckets: dict[str, QueryBucket] = {}
    bucket_units: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for diagnosis in diagnoses:
        label = normalize_query_type(diagnosis.query_type)
        bucket = query_buckets.setdefault(
            label,
            QueryBucket(
                id=fingerprint({"query_type": label, "normalization": "casefold-whitespace.v1"}),
                normalized_label=label,
                original_labels=[],
                diagnosis_run_ids=[],
            ),
        )
        if diagnosis.query_type not in bucket.original_labels:
            bucket.original_labels.append(diagnosis.query_type)
        bucket.diagnosis_run_ids.append(diagnosis.run_id)
        signal = (
            "error_repair"
            if diagnosis.run_id in targets_by_purpose["error_repair"]
            else "uncertainty_calibration"
        )
        unit = {
            "run_id": diagnosis.run_id,
            "query_type": diagnosis.query_type,
            "query_family": diagnosis.query_family,
            "signal": signal,
            "experience_cell": _experience_cell(
                rows[diagnosis.run_id], settings.confidence_threshold
            ),
            "evidence_signals": {
                ref.run_id: _experience_cell(rows[ref.run_id], settings.confidence_threshold)
                for ref in diagnosis.evidence
                if ref.run_id in rows
            },
            "category": diagnosis.category,
            "cause": diagnosis.cause,
            "condensed_rationale": diagnosis.condensed_rationale,
            "actionable_implication": diagnosis.actionable_implication,
            "uncertainty": diagnosis.uncertainty,
            "evidence": [ref.model_dump() for ref in diagnosis.evidence],
        }
        bucket_units.setdefault(label, []).append((label, unit))
    findings = []
    coverage.update(
        {
            "query_buckets": len(query_buckets),
            "bucket_batches": 0,
            "bucket_batches_completed": 0,
            "synthesis_included": 0,
            "synthesis_omitted": 0,
            "cross_bucket_batches": 0,
            "cross_bucket_batches_completed": 0,
            "cross_bucket_comparison_batches": 0,
            "cross_bucket_omitted_findings": 0,
        }
    )
    if ready:
        assert provider is not None
        system = prompt("synthesize") + "\nSchema: " + dumps(Findings.model_json_schema())
        completed_diagnoses: set[str] = set()
        cross_units_by_bucket: list[list[tuple[str, dict[str, Any]]]] = []
        for label, units in bucket_units.items():
            bucket = query_buckets[label]
            batches, omitted = _synthesis_batches(
                "bucket", units, coverage, incomplete, settings, system
            )
            coverage["bucket_batches"] += len(batches)
            for _, unit in omitted:
                incomplete[f"synthesis_context:{unit['run_id']}"] = (
                    "Complete diagnosis exceeds a synthesis batch context budget"
                )
                coverage["synthesis_omitted"] += 1
            cross_units = []
            for index, batch in enumerate(batches):
                try:
                    bucket_findings = _synthesize_batch(
                        provider,
                        settings,
                        "bucket",
                        batch,
                        coverage,
                        incomplete,
                        system,
                        rows,
                        targets_by_purpose,
                        key_for,
                    )
                except (EvoGError, ValueError):
                    incomplete[f"bucket:{bucket.id}:{index}"] = (
                        "Bucket synthesis unavailable or failed evidence/support validation"
                    )
                    coverage["synthesis_omitted"] += len(batch)
                    continue
                coverage["bucket_batches_completed"] += 1
                completed_diagnoses.update(unit["run_id"] for _, unit in batch)
                for finding in bucket_findings:
                    # Intermediate IDs are namespaced; final model finding IDs stay
                    # compatible with existing consumers when there is one batch.
                    finding = finding.model_copy(update={"id": f"{bucket.id}:{index}:{finding.id}"})
                    bucket.findings.append(finding)
                    evidence_signals = {
                        ref.run_id: _experience_cell(
                            rows[ref.run_id], settings.confidence_threshold
                        )
                        for ref in finding.evidence
                        if ref.run_id in rows
                    }
                    experience_cells = sorted(set(evidence_signals.values()))
                    cross_units.append(
                        (
                            label,
                            {
                                "id": finding.id,
                                "query_type": bucket.original_labels[0],
                                "query_types": finding.query_types,
                                "signal": finding.purpose,
                                "experience_cell": experience_cells[0]
                                if len(experience_cells) == 1
                                else "mixed",
                                "experience_cells": experience_cells,
                                "evidence_signals": evidence_signals,
                                "condensed_rationale": finding.pattern,
                                "actionable_implication": finding.suggested_change,
                                "counterevidence": finding.counterevidence,
                                "uncertainty": finding.uncertainty,
                                "support_kind": finding.support_kind,
                                "evidence": [ref.model_dump() for ref in finding.evidence],
                            },
                        )
                    )
            cross_units_by_bucket.append(cross_units)
        coverage["synthesis_included"] = len(completed_diagnoses)
        # Interleave bucket findings so bounded cross-bucket batches compare
        # different semantic types whenever the context admits both.
        cross_units = []
        for index in range(max((len(units) for units in cross_units_by_bucket), default=0)):
            cross_units.extend(
                units[index] for units in cross_units_by_bucket if index < len(units)
            )
        cross_batches, cross_omitted = _synthesis_batches(
            "cross_bucket", cross_units, coverage, incomplete, settings, system
        )
        coverage["cross_bucket_batches"] = len(cross_batches)
        coverage["cross_bucket_omitted_findings"] = len(cross_omitted)
        for _, unit in cross_omitted:
            incomplete[f"cross_bucket_context:{unit['id']}"] = (
                "Complete bucket finding exceeds a synthesis batch context budget"
            )
        for index, batch in enumerate(cross_batches):
            try:
                final_findings = _synthesize_batch(
                    provider,
                    settings,
                    "cross_bucket",
                    batch,
                    coverage,
                    incomplete,
                    system,
                    rows,
                    targets_by_purpose,
                    key_for,
                )
            except (EvoGError, ValueError):
                incomplete[f"cross_bucket:{index}"] = (
                    "Cross-bucket synthesis unavailable or failed evidence/support validation"
                )
                continue
            coverage["cross_bucket_batches_completed"] += 1
            coverage["cross_bucket_comparison_batches"] += int(
                len({label for label, _ in batch}) > 1
            )
            for finding in final_findings:
                if len(cross_batches) > 1:
                    finding = finding.model_copy(update={"id": f"cross:{index}:{finding.id}"})
                findings.append(finding)
        ready = (
            len(completed_diagnoses) >= required
            and len(completed_diagnoses) / len(job_ids) >= settings.analysis_min_coverage
            and not cross_omitted
            and coverage["cross_bucket_batches_completed"] == len(cross_batches)
        )
        if any(
            key.startswith(
                ("bucket:", "cross_bucket:", "synthesis_context:", "cross_bucket_context:")
            )
            for key in incomplete
        ):
            incomplete["synthesis"] = (
                "Some synthesis inputs or batches are unavailable; inspect scoped reasons and coverage"
            )
    else:
        coverage["synthesis_omitted"] = len(diagnoses)
        if diagnoses:
            incomplete["synthesis_coverage"] = "Insufficient valid diagnosis coverage for synthesis"
    report = AnalysisReport(
        id=uuid4().hex,
        revision_id=revision_id,
        coverage=coverage,
        selected_run_ids=selected,
        control_run_ids=controls,
        diagnoses=diagnoses,
        findings=findings,
        incomplete=incomplete,
        failure_run_ids=failures,
        calibration_run_ids=calibration,
        eligible_for_revision=ready,
        schema_version="evog.analysis.v2",
        query_type_normalization="casefold-whitespace.v1",
        query_buckets=list(query_buckets.values()),
    )
    store.save_artifact(report.id, "analysis", report.model_dump(mode="json"))
    return report
