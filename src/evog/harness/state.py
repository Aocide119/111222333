"""Question-local obligations and evidence ledgers owned by the fixed runtime."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Literal

from pydantic import Field

from evog.core.errors import ContractError
from evog.core.io import atomic_write, dumps, fingerprint, safe_path
from evog.core.models import Record


class Obligation(Record):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")
    description: str = Field(min_length=1, max_length=512)
    parent_id: str | None = Field(default=None, min_length=1, max_length=64)


class Resolution(Record):
    id: str = Field(min_length=1, max_length=64)
    status: Literal["satisfied", "waived"]
    evidence_refs: list[str] = Field(default_factory=list, max_length=64)
    search_ids: list[str] = Field(default_factory=list, max_length=20)
    reason: str = Field(min_length=1, max_length=1024)


class StateUpdate(Record):
    obligations: list[Obligation] = Field(default_factory=list, max_length=32)
    resolutions: list[Resolution] = Field(default_factory=list, max_length=32)
    anchors: list[str] = Field(default_factory=list, max_length=32)


class QuestionState:
    def __init__(self, root: Path, question: str, groups: list[str]):
        self.root = root
        self.path = safe_path(root, "state.json")
        self.evidence_path = safe_path(root, "evidence_links.jsonl")
        self.evidence: dict[str, dict] = {}
        if self.evidence_path.exists():
            if self.evidence_path.stat().st_size > 4000000:
                raise ContractError("Question evidence ledger exceeds its size limit")
            for line in self.evidence_path.read_text(encoding="utf-8").splitlines():
                row = json.loads(line)
                if not isinstance(row, dict) or not isinstance(row.get("ref"), str):
                    raise ContractError("Question evidence ledger is invalid")
                self.evidence[row["ref"]] = row
        self.data = {
            "schema": "evog.question-state.v1",
            "question": question,
            "authorized_groups": sorted(set(groups)),
            "obligations": {},
            "anchors": [],
            "searches": {},
            "evidence_links": "working_memory/evidence_links.jsonl",
        }
        if self.path.exists():
            if self.path.stat().st_size > 196608:
                raise ContractError("Question state exceeds its size limit")
            existing = json.loads(self.path.read_text(encoding="utf-8"))
            if (
                not isinstance(existing, dict)
                or existing.keys() != self.data.keys()
                or existing.get("schema") != self.data["schema"]
            ):
                raise ContractError("Question state has an invalid schema")
            if (
                existing["question"] != question
                or existing["authorized_groups"] != self.data["authorized_groups"]
            ):
                raise ContractError("Question state belongs to another task or group scope")
            self.data = existing
        self.commit(self.data)
        if not self.evidence_path.exists():
            atomic_write(self.evidence_path, "")

    def prepare(
        self,
        update: StateUpdate | None,
        *,
        obligation_id: str | None = None,
        parent_id: str | None = None,
        description: str = "",
        delivered: set[str],
    ) -> dict:
        data = deepcopy(self.data)
        obligations = data["obligations"]
        update = update or StateUpdate()
        declarations = list(update.obligations)
        if (
            obligation_id
            and obligation_id not in obligations
            and not any(item.id == obligation_id for item in declarations)
        ):
            declarations.append(
                Obligation(id=obligation_id, description=description, parent_id=parent_id)
            )
        for item in declarations:
            declared = item.model_dump()
            if not item.description.strip():
                raise ContractError("Obligations need a nonblank fact description")
            previous = obligations.get(item.id)
            if previous and any(previous[key] != value for key, value in declared.items()):
                raise ContractError("An obligation cannot change its description or parent")
            obligations.setdefault(item.id, {**declared, "status": "open"})
        if len(obligations) > 64:
            raise ContractError("Question obligation limit exceeded")
        for item in obligations.values():
            seen = {item["id"]}
            parent = item["parent_id"]
            while parent is not None:
                if parent not in obligations or parent in seen:
                    raise ContractError("Obligation dependencies must be known and acyclic")
                seen.add(parent)
                parent = obligations[parent]["parent_id"]
        for resolution in update.resolutions:
            item = obligations.get(resolution.id)
            if item is None:
                raise ContractError("Cannot resolve an unknown obligation")
            if not resolution.reason.strip():
                raise ContractError("An obligation resolution needs an evidence-based reason")
            refs = set(resolution.evidence_refs)
            searches = set(resolution.search_ids)
            if not refs.issubset(delivered) or not searches.issubset(data["searches"]):
                raise ContractError("Resolution evidence must have been delivered or searched")
            if resolution.status == "satisfied" and not refs:
                raise ContractError("Satisfying an obligation requires delivered source evidence")
            if resolution.status == "waived" and not (refs or searches):
                raise ContractError("Waiving an obligation requires recorded observations")
            if any(data["searches"][key]["obligation_id"] != resolution.id for key in searches):
                raise ContractError("Waiver searches must belong to the resolved obligation")
            parent = item["parent_id"]
            if parent is not None and obligations[parent]["status"] == "open":
                raise ContractError("Resolve the parent obligation before its child")
            record = resolution.model_dump()
            if item["status"] != "open" and item.get("resolution") != record:
                raise ContractError("A resolved obligation cannot be rewritten")
            item.update(status=resolution.status, resolution=record)
        if parent_id is not None and obligation_id is None:
            raise ContractError("parent_id requires an obligation_id")
        if obligation_id is not None:
            item = obligations[obligation_id]
            if parent_id is not None and item["parent_id"] != parent_id:
                raise ContractError("Search parent differs from the registered dependency")
            if item["status"] != "open":
                raise ContractError("Search must address an open obligation")
            parent = item["parent_id"]
            if parent is not None and obligations[parent]["status"] == "open":
                raise ContractError("Resolve the parent obligation before searching its child")
        for anchor in update.anchors:
            if not anchor.strip() or len(anchor) > 256:
                raise ContractError("State anchors must contain 1–256 characters")
            if anchor not in data["anchors"]:
                data["anchors"].append(anchor)
        if len(data["anchors"]) > 64 or len(dumps(data).encode("utf-8")) > 196608:
            raise ContractError("Question state exceeds its size limit")
        return data

    def commit(self, data: dict) -> None:
        if len(dumps(data).encode("utf-8")) > 196608:
            raise ContractError("Question state exceeds its size limit")
        atomic_write(self.path, dumps(data))
        self.data = data

    def record_search(self, data: dict, arguments: dict, result: dict) -> str:
        identity = f"search-{len(data['searches']) + 1}"
        if len(data["searches"]) >= 64:
            raise ContractError("Question search ledger limit exceeded")
        data["searches"][identity] = {
            "obligation_id": arguments.get("obligation_id"),
            "target": arguments.get("target", "memory_units"),
            "query": arguments.get("query", "")[:256],
            "patterns": arguments.get("selected_patterns") or arguments.get("patterns", []),
            "required_pattern_groups": arguments.get("required_pattern_groups", []),
            "total_matches": result.get("total_matches"),
            "result_fingerprint": fingerprint(result),
        }
        return identity

    def record_evidence(self, refs: set[str], locations: dict[str, dict]) -> None:
        added = {ref: locations[ref] for ref in refs if ref not in self.evidence}
        if not added:
            return
        updated = {**self.evidence, **added}
        if len(dumps(updated).encode("utf-8")) > 4000000:
            raise ContractError("Question evidence ledger exceeds its size limit")
        self.evidence = updated
        atomic_write(
            self.evidence_path,
            "".join(dumps(self.evidence[ref]) + "\n" for ref in sorted(self.evidence)),
        )

    @property
    def open_ids(self) -> list[str]:
        return [key for key, value in self.data["obligations"].items() if value["status"] == "open"]

    def summary(self) -> dict:
        return {
            "obligations": [
                {key: item[key] for key in ("id", "description", "parent_id", "status")}
                for item in self.data["obligations"].values()
            ],
            "anchors": self.data["anchors"],
            "search_ids": list(self.data["searches"]),
            "state_path": "memory_store/working_memory/state.json",
            "evidence_path": "memory_store/working_memory/evidence_links.jsonl",
        }


def memory_guides(harness) -> dict[str, str]:
    """Expose routing instructions without introducing any learned cold-start memory."""
    guides = {
        "memory_store/README.md": (
            "# Memory routing\n\n"
            "Read long_term_memory/README.md and the relevant category guide before writing. "
            "Use working_memory for searches and unfinished reasoning. Original memory_units "
            "remain the only primary evidence.\n\n" + harness.memory_policy
        ),
        "memory_store/working_memory/README.md": (
            "# Question working memory\n\n"
            "Write temporary notes to notes.md or your own note files. state.json, "
            "evidence_links.jsonl and search_ledger.jsonl are runtime-owned and read-only. "
            "Use state_update on a tool call to register obligations or resolve them with "
            "evidence already delivered in a previous call. grep_search accepts obligation_id "
            "and parent_id; resolve a parent before searching its child.\n"
        ),
    }
    routes = []
    for category, destination in harness.memory_layout["categories"].items():
        routes.append(f"- {category}: `{destination}`")
        directory = destination.rpartition("/")[0] if Path(destination).suffix else destination
        if directory != "long_term_memory":
            guides[f"memory_store/{directory}/README.md"] = (
                f"# {category}\n\nStore {category} notes under `{destination}`. "
                "Retain source references; notes are navigation aids. "
                "In frozen rounds write JSON entries [{ref, excerpt}] using exact source excerpts.\n"
            )
    guides["memory_store/long_term_memory/README.md"] = (
        "# Group long-term memory\n\n" + "\n".join(routes) + "\n\n"
        "Use source-linked notes, never reference answers or evaluation scores. "
        "Frozen-round writes are staged for the next round as JSON {entries: [{ref, excerpt}]}; "
        "excerpt must be copied from a source record fully delivered to this question. "
        "Group memory is frozen during the round and merged deterministically afterwards.\n"
    )
    return guides
