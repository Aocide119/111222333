"""Confidence selection, addressable trace inspection, and bucketed synthesis."""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from uuid import uuid4

from pydantic import Field, ValidationError

from evog.config import Settings
from evog.errors import ContractError, EvoGError, ProviderError
from evog.harness import prompt
from evog.io import dumps, safe_path
from evog.models import AnalysisReport, Diagnosis, EvidenceRef, Finding, Record
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
        low = run["answer"]["confidence"] <= settings.confidence_threshold
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
    selected_groups = set(selected_group_order[: settings.analysis_max_runs])
    selected = [run_id for run_id in selected if question_key(row_by_id[run_id]) in selected_groups]
    controls.sort()
    coverage["eligible"] = eligible_before
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
    provider_failures = 0
    for turn in range(settings.analysis_turns):
        messages = trim_messages(
            messages, int(settings.analysis_max_context_chars * settings.context_trim_ratio)
        )
        if len(dumps(messages)) > settings.analysis_max_context_chars:
            raise ContractError("Analysis task anchors exceed the context budget")
        try:
            reply = complete_before(
                provider,
                messages,
                analysis_tools() if turn < settings.analysis_turns - 1 else [],
                deadline,
            )
        except ProviderError:
            provider_failures += 1
            if provider_failures >= settings.call_attempts or turn == settings.analysis_turns - 1:
                raise
            continue
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
                    "Use a short query_type and a supplied query_family. Copy evidence_ref values exactly "
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
    failure_keys = {key_for(run_id) for run_id in failures}
    failure_diagnoses = [d for d in diagnoses if key_for(d.run_id) in failure_keys]
    required_failures = min(settings.analysis_min_valid, len(failure_keys))
    ready = (
        bool(failures)
        and len(failure_diagnoses) >= required_failures
        and len(failure_diagnoses) / len(failure_keys) >= settings.analysis_min_coverage
    )
    findings = []
    if ready:
        assert provider is not None
        buckets: dict[str, list[dict[str, Any]]] = {}
        # Evidence is admitted only with the complete diagnosis that survived
        # context budgeting. A diagnosis may cite one of its sibling trials.
        allowed_refs: set[str] = set()
        supplied_ids = set()
        synthesis = {
            "coverage": coverage,
            "finding_min_support": settings.finding_min_support,
            "buckets": buckets,
            "incomplete": incomplete,
            "coverage_note": "Only included diagnoses support findings; omitted and unavailable runs do not.",
        }
        system = prompt("synthesize") + "\nSchema: " + dumps(Findings.model_json_schema())
        limit = int(settings.synthesis_max_context_chars * settings.context_trim_ratio)
        # Admit complete condensed diagnoses, never sliced references or invented missing content.
        for d in failure_diagnoses:
            signal = "error_repair"
            key = signal + "/" + d.query_family
            unit = {
                "run_id": d.run_id,
                "query_type": d.query_type,
                "query_family": d.query_family,
                "signal": signal,
                "category": d.category,
                "cause": d.cause,
                "condensed_rationale": d.condensed_rationale,
                "actionable_implication": d.actionable_implication,
                "uncertainty": d.uncertainty,
                "evidence": [ref.model_dump() for ref in d.evidence],
            }
            buckets.setdefault(key, []).append(unit)
            if (
                len(
                    dumps(
                        [
                            {"role": "system", "content": system},
                            {"role": "user", "content": dumps(synthesis)},
                        ]
                    )
                )
                > limit
            ):
                buckets[key].pop()
                if not buckets[key]:
                    del buckets[key]
                continue
            supplied_ids.add(d.run_id)
            allowed_refs.update(ref_key(ref) for ref in d.evidence)
        coverage.update(
            {
                "synthesis_included": len(supplied_ids),
                "synthesis_omitted": len(failure_diagnoses) - len(supplied_ids),
            }
        )
        ready = (
            len(supplied_ids) >= required_failures
            and len(supplied_ids) / len(failure_keys) >= settings.analysis_min_coverage
        )
        if ready:
            bounded_provider = stage_provider(provider, settings, "synthesis")
            messages = [
                {"role": "system", "content": system},
                {"role": "user", "content": dumps(synthesis)},
            ]
            deadline = time.monotonic() + settings.synthesis_timeout_seconds
            for attempt in range(settings.call_attempts):
                reply = None
                try:
                    messages = trim_messages(messages, limit)
                    if len(dumps(messages)) > settings.synthesis_max_context_chars:
                        raise ContractError("Synthesis anchors exceed the context budget")
                    reply = complete_before(bounded_provider, messages, [], deadline)
                    drafted = Findings.model_validate_json(reply.content).findings
                    if len({f.id for f in drafted}) != len(drafted):
                        raise ContractError("Finding IDs must be unique")
                    for finding in drafted:
                        if any(ref_key(ref) not in allowed_refs for ref in finding.evidence):
                            raise ContractError(
                                "Finding references evidence outside included, inspected diagnoses"
                            )
                        expected_keys = (
                            failure_keys
                            if finding.purpose == "error_repair"
                            else {key_for(run_id) for run_id in calibration}
                        )
                        supporting = {
                            key_for(ref.run_id) for ref in finding.evidence if ref.run_id in rows
                        } & expected_keys
                        minimum = (
                            settings.finding_min_support
                            if finding.support_kind == "repeated"
                            else 1
                        )
                        if len(supporting) < minimum:
                            raise ContractError(
                                "Finding lacks independent support for its declared purpose and pattern"
                            )
                    findings = drafted
                    break
                except (EvoGError, ValueError):
                    if attempt + 1 == settings.call_attempts:
                        ready = False
                        incomplete["synthesis"] = (
                            "Synthesis unavailable or failed evidence/support validation"
                        )
                    else:
                        messages.append(
                            {
                                "role": "assistant",
                                "content": reply.content if reply is not None else "",
                            }
                        )
                        messages.append(
                            {
                                "role": "user",
                                "content": "Return only corrected findings JSON. Copy exact references from included diagnoses. "
                                "Repeated patterns need distinct question-and-authorized-group scopes; "
                                "repeated trials of one question count once and isolated observations must be labelled isolated. "
                                "Do not treat uncertainty in an accepted answer as an observed error. Empty findings are valid.",
                            }
                        )
        else:
            incomplete["synthesis_context"] = (
                "Insufficient valid coverage after context-bounded inclusion"
            )
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
    )
    store.save_artifact(report.id, "analysis", report.model_dump(mode="json"))
    return report
