"""The five cold-start tools, with scoped sources and writable navigation notes."""

from __future__ import annotations

import json
import re
from datetime import UTC
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import Field

from evog.errors import ContractError
from evog.harness import Harness
from evog.io import atomic_write, dumps, fingerprint, safe_path
from evog.memory import QuestionMemory
from evog.models import Message, Record
from evog.store import Store
from evog.tool_results import ToolResults


class ListArgs(Record):
    target: Literal["workspace", "memory_units", "memory_store", "skills"] = "workspace"
    resource_path: str = ""
    max_depth: int = Field(default=3, ge=1, le=10)
    path: str | None = Field(default=None, exclude=True)
    offset: int = Field(default=0, ge=0, exclude=True)
    limit: int = Field(default=20, ge=1, le=50, exclude=True)


class ReadArgs(Record):
    target: Literal["workspace", "memory_units", "memory_store", "skills"] = "memory_units"
    resource_path: str = ""
    start_line: int = Field(default=1, ge=1)
    end_line: int | None = Field(default=None, ge=1)
    path: str | None = Field(default=None, exclude=True)
    limit: int | None = Field(default=None, ge=1, le=50, exclude=True)


class SearchArgs(Record):
    target: Literal["memory_units", "memory_store"] = "memory_units"
    resource_path: str = ""
    query: str = ""
    patterns: list[str] = Field(default_factory=list, max_length=8)
    selected_patterns: list[str] = Field(default_factory=list, max_length=2)
    required_pattern_groups: list[Annotated[list[str], Field(min_length=1, max_length=8)]] = Field(
        default_factory=list, max_length=8
    )
    optional_patterns: list[str] = Field(default_factory=list, max_length=8)
    terms: list[str] = Field(default_factory=list, max_length=8, exclude=True)
    path: str | None = Field(default=None, exclude=True)
    offset: int = Field(default=0, ge=0, exclude=True)


class WriteArgs(Record):
    target: Literal["memory_store"] = "memory_store"
    resource_path: str = ""
    content: str = Field(max_length=16000)

    mode: Literal["append", "replace"] = "append"
    path: str | None = Field(default=None, exclude=True)


class CreateArgs(Record):
    target: Literal["memory_store"] = "memory_store"
    resource_path: str = ""
    content: str = Field(max_length=16000)
    overwrite: bool = False
    path: str | None = Field(default=None, exclude=True)


ARGUMENTS = {
    "list_files": (
        ListArgs,
        "List files under one allowed logical root. Use target and resource_path; never pass an absolute path.",
    ),
    "read_file": (
        ReadArgs,
        "Read a one-based line range from one allowed text file. Use the same target and resource_path returned by list_files or grep_search.",
    ),
    "grep_search": (
        SearchArgs,
        "Search using literal strings. Each required_pattern_groups group matches any alias; all groups must match. Selected patterns follow the harness any/all rule. Optional patterns only rank already matching records.",
    ),
    "write_file": (
        WriteArgs,
        "Append or replace a UTF-8 note under memory_store/long_term_memory/ or working_memory/. Source records and runtime state are read-only.",
    ),
    "create_file": (
        CreateArgs,
        "Create a new UTF-8 note under memory_store. Set overwrite only when intentionally replacing an existing note.",
    ),
}


def tool_definitions() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": _public_schema(name, args),
            },
        }
        for name, (args, description) in ARGUMENTS.items()
    ]


def _public_schema(name: str, model: type[Record]) -> dict[str, Any]:
    """Expose scoped logical resources while retaining legacy aliases internally."""
    schema = model.model_json_schema()
    for key in ("path", "offset", "limit", "terms"):
        schema.get("properties", {}).pop(key, None)
    schema["required"] = {
        "list_files": ["target"],
        "read_file": ["target", "resource_path"],
        "grep_search": ["target", "query"],
        "write_file": ["target", "resource_path", "content"],
        "create_file": ["target", "resource_path", "content"],
    }[name]
    return schema


class Tools:
    def __init__(
        self,
        store: Store,
        harness: Harness,
        group_ids: list[str],
        *,
        memory_session: str | None = None,
        isolated_long_term: bool = False,
        result_session: str | None = None,
        max_output_chars: int = 12000,
        frozen_memory: QuestionMemory | None = None,
    ):
        if not group_ids:
            raise ContractError("Explicit group scope is required")
        known = {row["group_id"] for row in store.groups()}
        if not set(group_ids).issubset(known):
            raise ContractError("One or more requested groups have no imported messages")
        self.harness = harness
        self.frozen_memory = frozen_memory
        # Each source view is frozen at interaction start, even if a later import occurs.
        self.context: dict[str, list[dict[str, Any]]] = {}
        self.group_paths = {}
        for group in sorted(set(group_ids)):
            path = f"context/{fingerprint(group)}.jsonl"
            self.group_paths[path] = group
            self.context[path] = [self._record(message) for message in store.messages(group)]
        # Keep durable notes separate from per-question scratch state while
        # retaining the compact ``memory/`` compatibility surface. Long-term notes are scoped to
        # the authorized groups and survive question sessions; working memory is
        # session-scoped when a caller supplies ``memory_session``.
        scope = fingerprint(sorted(set(group_ids)))
        self.memory_scope_root = store.workspace / "memory" / scope
        self.long_term_root = self.memory_scope_root / "long_term_memory"
        if memory_session is not None:
            session_root = (
                store.workspace
                / "sessions"
                / fingerprint({"session": memory_session, "groups": sorted(set(group_ids))})
                / "memory"
            )
            self.working_root = session_root / "working_memory"
            if isolated_long_term:
                self.long_term_root = session_root / "long_term_memory"
        else:
            self.working_root = self.memory_scope_root / "working_memory"
        # ``memory_root`` is a compatibility alias for the per-question
        # working layer; new callers should use the explicit layer paths.
        self.memory_root = self.working_root
        for root in (self.memory_scope_root, self.long_term_root, self.working_root):
            safe_path(store.workspace, root.relative_to(store.workspace).as_posix())
            root.mkdir(parents=True, exist_ok=True)
        if frozen_memory is not None:
            if frozen_memory.groups != sorted(set(group_ids)) or not isolated_long_term:
                raise ContractError("Frozen memory requires matching groups and private staging")
            frozen_memory.seed(self.long_term_root)
        self.search_ledger_path = self.working_root / "search_ledger.jsonl"
        self.delivered_refs: set[str] = set()
        result_root = safe_path(store.workspace, "tool_results/" + (result_session or uuid4().hex))
        self.results = ToolResults(result_root, max_output_chars)
        self.last_full_result: dict[str, Any] | None = None
        self.last_result_path: str | None = None

    def _record(self, message: Message) -> dict[str, Any]:
        row = message.model_dump(mode="json")
        if not self.harness.representation.include_metadata:
            row.pop("metadata")
        row["ref"] = message.ref
        view = self.harness.representation.timestamp_view
        if view == "utc":
            row["source_timestamp"] = row["timestamp"]
            row["timestamp"] = message.timestamp.astimezone(UTC).isoformat()
        elif view == "both":
            row["utc_timestamp"] = message.timestamp.astimezone(UTC).isoformat()
        if self.harness.representation.include_reply_refs and message.reply_to:
            row["reply_ref"] = f"{message.group_id}/{message.reply_to}"
        return row

    def files(self) -> list[str]:
        notes = []
        # ``memory/`` is a compatibility alias for working_memory. Explicit
        # layer names are also exposed so prompts can distinguish
        # durable notes from per-question scratch state.
        for root, prefix in (
            (self.working_root, "memory/"),
            (self.working_root, "memory_store/working_memory/"),
            (self.long_term_root, "memory_store/long_term_memory/"),
        ):
            for path in root.rglob("*"):
                if path.is_file() and not path.is_symlink():
                    relative = path.relative_to(root).as_posix()
                    safe_path(root, relative)
                    notes.append(prefix + relative)
        skills = [name for name in self.harness.contents if name.startswith("skills/")]
        # memory_units is a read-only alias for the immutable source context.
        units = ["memory_units/" + path.removeprefix("context/") for path in self.context]
        return sorted([*self.context, *units, *notes, *skills, *self.results.paths])

    @staticmethod
    def _split_legacy_path(path: str) -> tuple[str, str]:
        if not path:
            return "", ""
        head, _, tail = path.partition("/")
        roots = {"workspace", "memory_units", "memory_store", "skills"}
        if head in roots:
            return head, tail
        if path.startswith("context/"):
            return "memory_units", path.removeprefix("context/")
        if path.startswith("memory/"):
            return "memory_store", "working_memory/" + path.removeprefix("memory/")
        return "memory_store", path

    @staticmethod
    def _logical_path(target: str, resource_path: str) -> str:
        if target == "memory_units":
            return "memory_units/" + resource_path if resource_path else "memory_units/"
        if target == "memory_store":
            return "memory_store/" + resource_path if resource_path else "memory_store/"
        if target in {"skills", "workspace"}:
            if target == "workspace" and resource_path.startswith("tool_results/"):
                return resource_path
            return target + "/" + resource_path if resource_path else target + "/"
        raise ContractError("Unknown logical resource root")

    def _normalize_args(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        raw = dict(arguments or {})
        if name == "list_files" and not raw:
            raw["target"] = "memory_units"
        path = raw.get("path")
        if isinstance(path, str) and path:
            target, resource = self._split_legacy_path(path)
            raw["target"] = "memory_store" if name in {"write_file", "create_file"} else target
            raw.setdefault("resource_path", resource)
            if name == "write_file" and "mode" not in raw:
                raw["mode"] = "replace"
        if name == "read_file" and raw.get("limit") is not None and raw.get("end_line") is None:
            raw["end_line"] = int(raw.get("start_line", 1)) + int(raw["limit"]) - 1
        if name == "grep_search" and raw.get("terms") and not raw.get("patterns"):
            raw["patterns"] = list(raw["terms"])
            raw["selected_patterns"] = list(raw["terms"])[:2]
            raw["query"] = " ".join(str(item) for item in raw["terms"])
        return raw

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.last_full_result = None
        self.last_result_path = None
        if name not in ARGUMENTS:
            raise ContractError("Unknown tool")
        args = ARGUMENTS[name][0].model_validate(self._normalize_args(name, arguments))
        before = self.delivered_refs.copy()
        result = getattr(self, name)(args)
        self.last_full_result = result
        if len(dumps(result)) <= self.results.max_chars:
            return result
        refs = self.delivered_refs - before
        self.delivered_refs = before
        preview = self.results.persist(result, refs)
        self.last_result_path = preview["full_result_path"]
        return preview

    def list_files(self, args: ListArgs) -> dict[str, Any]:
        prefix = self._logical_path(args.target, args.resource_path)
        names = [name for name in self.files() if name.startswith(prefix)]
        if args.target == "workspace":
            names = sorted(
                name for name in self.results.paths if name.startswith(args.resource_path)
            )
        if args.target == "memory_units":
            names = [name for name in names if name.startswith("memory_units/")]
        names = [
            name
            for name in names
            if len(name.removeprefix(prefix).strip("/").split("/")) <= args.max_depth
        ]
        start = args.offset
        rows = [
            {
                "path": name,
                "target": args.target,
                "resource_path": name.removeprefix(args.target + "/"),
                **(
                    {"group_id": self.group_paths["context/" + name.removeprefix("memory_units/")]}
                    if args.target == "memory_units"
                    and "context/" + name.removeprefix("memory_units/") in self.group_paths
                    else {}
                ),
            }
            for name in names[start : start + args.limit]
        ]
        next_offset = start + len(rows)
        truncated = next_offset < len(names)
        return {
            "files": rows,
            "total": len(names),
            "truncated": truncated,
            "next_offset": next_offset if truncated else None,
        }

    def _lines(self, path: str) -> list[str]:
        if path in self.results.paths:
            actual = safe_path(self.results.root, path.removeprefix("tool_results/"))
            return actual.read_text(encoding="utf-8").splitlines()
        if path in self.context:
            return [dumps(row) for row in self.context[path]]
        if path.startswith("memory_units/"):
            source = "context/" + path.removeprefix("memory_units/")
            if source in self.context:
                return [dumps(row) for row in self.context[source]]
        if path.startswith("skills/") and path in self.harness.contents:
            return self.harness.contents[path].splitlines()
        root = None
        relative = ""
        if path.startswith("memory/"):
            root, relative = self.working_root, path.removeprefix("memory/")
        elif path.startswith("memory_store/working_memory/"):
            root, relative = self.working_root, path.removeprefix("memory_store/working_memory/")
        elif path.startswith("memory_store/long_term_memory/"):
            root, relative = (
                self.long_term_root,
                path.removeprefix("memory_store/long_term_memory/"),
            )
        if root is not None:
            actual = safe_path(root, relative)
            if actual.stat().st_size > 64000:
                raise ContractError("Note exceeds the read limit")
            return actual.read_text(encoding="utf-8").splitlines()
        raise ContractError("Path is not available in the authorized scope")

    def read_file(self, args: ReadArgs) -> dict[str, Any]:
        path = args.path or self._logical_path(args.target, args.resource_path)
        source = self._lines(path)
        start = args.start_line - 1
        if start >= len(source) and source:
            raise ContractError("start_line exceeds file length")
        selected = []
        size = 0
        end_line = args.end_line or (start + self.harness.operations.read_limit)
        limit = max(0, end_line - start)
        for index in range(start, min(start + limit, len(source))):
            text = source[index]
            # A single large line must reach execute() so its full content can be
            # archived and recovered in chunks before citation delivery is granted.
            if selected and size + len(text) > 60000:
                break
            row: dict[str, Any] = {"line": index + 1, "text": text}
            source_path = path
            if path.startswith("memory_units/"):
                source_path = "context/" + path.removeprefix("memory_units/")
            if source_path in self.context:
                ref = self.context[source_path][index]["ref"]
                row["ref"] = ref
                self.delivered_refs.add(ref)
            selected.append(row)
            size += len(text)
        end = start + len(selected)
        truncated = end < len(source)
        self.delivered_refs.update(self.results.delivered(path, start, end, len(source)))
        self._append_ledger(
            {
                "operation": "read",
                "path": path,
                "target": args.target,
                "resource_path": args.resource_path,
                "start_line": start + 1,
                "end_line": end,
                "complete": not truncated,
                "refs": sorted(row.get("ref") for row in selected if row.get("ref")),
            }
        )
        return {
            "path": path,
            "target": args.target,
            "resource_path": args.resource_path,
            "lines": selected,
            "total_lines": len(source),
            "truncated": truncated,
            "next_start_line": end + 1 if truncated else None,
        }

    def grep_search(self, args: SearchArgs) -> dict[str, Any]:
        selected = args.selected_patterns or args.patterns
        if not selected and not args.required_pattern_groups and args.query:
            selected = [args.query]
        terms = [term.casefold().strip() for term in selected]
        groups = [
            [term.casefold().strip() for term in group] for group in args.required_pattern_groups
        ]
        optional = [term.casefold().strip() for term in args.optional_patterns]
        if any(
            not term or len(term) > 256
            for term in [*terms, *optional, *(t for g in groups for t in g)]
        ):
            raise ContractError("Search terms must contain 1–256 characters")
        if not terms and not groups:
            raise ContractError("Search needs a query, selected pattern or required group")
        matches = []
        operations = self.harness.operations

        def contains(term: str, field: str) -> bool:
            return (
                term in field
                if operations.match_mode == "literal"
                else re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", field) is not None
            )

        search_path = self._logical_path(args.target, args.resource_path)
        for path in self.files():
            if not path.startswith(search_path):
                continue
            # Select one spelling of each source and working-memory path for broad searches.
            if args.target != "memory_units" and path.startswith("memory_units/"):
                continue
            if not search_path.startswith("memory_store/working_memory/") and path.startswith(
                "memory_store/working_memory/"
            ):
                continue
            # The ledger is runtime-owned metadata, not conversation evidence;
            # excluding it also prevents a broad search from feeding previous
            # search terms back into later searches.
            if path.endswith("search_ledger.jsonl"):
                continue
            for index, line in enumerate(self._lines(path)):
                source_path = path
                if path.startswith("memory_units/"):
                    source_path = "context/" + path.removeprefix("memory_units/")
                if source_path in self.context:
                    record = self.context[source_path][index]
                    fields = [
                        str(record[key]).casefold()
                        for key in operations.search_fields
                        if key in record and key not in {"metadata", "reply_to"}
                    ]
                    if (
                        "reply_to" in operations.search_fields
                        and record.get("reply_to") is not None
                    ):
                        fields.append(record["reply_to"].casefold())
                    if "metadata" in operations.search_fields and "metadata" in record:
                        fields.extend(value.casefold() for value in record["metadata"].values())
                else:
                    fields = [line.casefold()]
                hits = [any(contains(term, field) for field in fields) for term in terms]
                base_match = not terms or (
                    all(hits) if operations.search_mode == "all" else any(hits)
                )
                required_match = all(
                    any(contains(term, field) for term in group for field in fields)
                    for group in groups
                )
                if base_match and required_match:
                    rank = sum(any(contains(term, field) for field in fields) for term in optional)
                    matches.append((path, index, line, rank))
        matches.sort(key=lambda match: (-match[3], match[0], match[1]))
        selected = matches[args.offset : args.offset + self.harness.operations.search_limit]
        rows = []
        returned_refs = set()
        response_chars = 0
        for path, index, line, rank in selected:
            row_refs = set()
            row: dict[str, Any] = {
                "path": path,
                "target": args.target,
                "resource_path": path.removeprefix(args.target + "/"),
                "line": index + 1,
                "excerpt": line[: operations.excerpt_chars],
                "excerpt_truncated": len(line) > operations.excerpt_chars,
                "optional_match_count": rank,
            }
            source_path = path
            if path.startswith("memory_units/"):
                source_path = "context/" + path.removeprefix("memory_units/")
            if source_path in self.context:
                record = self.context[source_path][index]
                row["ref"] = record["ref"]
                row["group_id"] = record["group_id"]
                # Only complete delivered source records qualify for citations.
                if len(line) <= operations.excerpt_chars:
                    row_refs.add(record["ref"])
                window = self.harness.operations.context_window
                if window:
                    neighborhood = self.context[source_path][
                        max(0, index - window) : index + window + 1
                    ]
                    row["context"] = [
                        {
                            "ref": item["ref"],
                            "text": dumps(item)[: operations.excerpt_chars],
                            "truncated": len(dumps(item)) > operations.excerpt_chars,
                        }
                        for item in neighborhood
                    ]
                    row_refs.update(
                        item["ref"]
                        for item in neighborhood
                        if len(dumps(item)) <= operations.excerpt_chars
                    )
            size = len(dumps(row))
            if response_chars + size > 60000:
                break
            rows.append(row)
            returned_refs.update(row_refs)
            response_chars += size
        self.delivered_refs.update(returned_refs)
        next_offset = args.offset + len(rows)
        self._append_ledger(
            {
                "operation": "search",
                "path": search_path,
                "target": args.target,
                "resource_path": args.resource_path,
                "terms": terms,
                "required_pattern_groups": groups,
                "optional_patterns": optional,
                "offset": args.offset,
                "total_matches": len(matches),
                "returned": len(rows),
                "returned_refs": sorted(returned_refs),
                "truncated": next_offset < len(matches),
            }
        )
        truncated = next_offset < len(matches)
        return {
            "matches": rows,
            "total_matches": len(matches),
            "mode": self.harness.operations.search_mode,
            "truncated": truncated,
            "next_offset": next_offset if truncated else None,
        }

    def _append_ledger(self, entry: dict[str, Any]) -> None:
        """Persist a bounded, local search/read ledger for later diagnosis."""
        try:
            record = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
            if len(record.encode("utf-8")) > 64000:
                return
            lines = (
                self.search_ledger_path.read_text(encoding="utf-8").splitlines(keepends=True)
                if self.search_ledger_path.exists()
                else []
            )
            lines.append(record)
            while len("".join(lines).encode("utf-8")) > 64000:
                lines.pop(0)
            atomic_write(self.search_ledger_path, "".join(lines))
        except OSError:
            # The ledger is diagnostic metadata and must never make answering fail.
            return

    def _write(self, args: WriteArgs | CreateArgs, create: bool) -> dict[str, Any]:
        resource = args.resource_path
        if resource.startswith("memory/"):
            relative = "working_memory/" + resource.removeprefix("memory/")
        elif resource.startswith("memory_store/"):
            relative = resource.removeprefix("memory_store/")
        else:
            relative = resource
        if relative.startswith("working_memory/"):
            root, relative = self.working_root, relative.removeprefix("working_memory/")
        elif relative.startswith("long_term_memory/"):
            root, relative = self.long_term_root, relative.removeprefix("long_term_memory/")
        else:
            raise ContractError(
                "Writes are limited to writable memory_store long_term_memory/ and working_memory/"
            )
        if relative == "search_ledger.jsonl":
            raise ContractError("The search ledger is runtime-owned and read-only")
        path = safe_path(root, relative)
        if path.suffix not in (".md", ".txt", ".json", ".jsonl"):
            raise ContractError("Navigation notes must be text files")
        if self.frozen_memory is not None and root == self.long_term_root:
            if create and path.exists() and not getattr(args, "overwrite", False):
                raise FileExistsError("Note already exists")
            append = not create and getattr(args, "mode", "replace") == "append"
            sources = {row["ref"]: row["text"] for rows in self.context.values() for row in rows}
            content = self.frozen_memory.validate_and_stage(
                relative,
                args.content,
                self.delivered_refs,
                sources,
                append=append,
            )
            entries = json.loads(content)["entries"]
            if append and path.exists():
                entries = [*json.loads(path.read_text())["entries"], *entries]
            unique = {dumps(entry): entry for entry in entries}
            atomic_write(path, dumps({"entries": [unique[key] for key in sorted(unique)]}))
            return {
                "target": args.target,
                "resource_path": args.resource_path,
                "written_chars": len(args.content),
                "staged_for_next_round": True,
                "input_snapshot_id": self.frozen_memory.parent.snapshot_id,
            }
        if create and not getattr(args, "overwrite", False):
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("x", encoding="utf-8") as stream:
                stream.write(args.content)
        elif create or getattr(args, "mode", "replace") == "replace":
            atomic_write(path, args.content)
        else:
            existing = path.read_text(encoding="utf-8") if path.exists() else ""
            atomic_write(path, existing + args.content)
        return {
            "target": args.target,
            "resource_path": args.resource_path,
            "written_chars": len(args.content),
            "mode": "create" if create else getattr(args, "mode", "replace"),
        }

    def write_file(self, args: WriteArgs) -> dict[str, Any]:
        return self._write(args, create=False)

    def create_file(self, args: WriteArgs) -> dict[str, Any]:
        return self._write(args, create=True)
