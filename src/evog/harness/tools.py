"""The five cold-start tools, with scoped sources and writable navigation notes."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import UTC
from importlib.resources import files
from typing import Annotated, Any, Literal
from uuid import uuid4

import yaml
from pydantic import Field

from evog.core.errors import ContractError, DeadlineExceeded
from evog.core.io import atomic_write, dumps, fingerprint, safe_path
from evog.core.models import Message, Record
from evog.core.store import Store
from evog.harness.memory import QuestionMemory
from evog.harness.schema import Harness
from evog.harness.tool_results import ToolResults


class ListArgs(Record):
    target: Literal["workspace", "memory_units", "memory_store", "skills"] = "workspace"
    resource_path: str = ""
    max_depth: int = Field(default=3, ge=1, le=10)
    offset: int = Field(default=0, ge=0, exclude=True)
    limit: int = Field(default=20, ge=1, le=50, exclude=True)


class ReadArgs(Record):
    target: Literal["workspace", "memory_units", "memory_store", "skills"] = "memory_units"
    resource_path: str = ""
    start_line: int = Field(default=1, ge=1)
    end_line: int | None = Field(default=None, ge=1)
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
    offset: int = Field(default=0, ge=0, exclude=True)


class WriteArgs(Record):
    target: Literal["memory_store"] = "memory_store"
    resource_path: str = ""
    content: str = Field(max_length=16000)

    mode: Literal["append", "replace"] = "append"


class CreateArgs(Record):
    target: Literal["memory_store"] = "memory_store"
    resource_path: str = ""
    content: str = Field(max_length=16000)
    overwrite: bool = False
    path: str | None = Field(default=None, exclude=True)


class QueryToolArgs(Record):
    """Public arguments shared by every declarative Operation query tool."""

    query: str = Field(min_length=1, max_length=256)
    patterns: list[str] = Field(default_factory=list, max_length=8)
    selected_patterns: list[str] = Field(default_factory=list, max_length=2)
    required_pattern_groups: list[Annotated[list[str], Field(min_length=1, max_length=8)]] = Field(
        default_factory=list, max_length=8
    )
    optional_patterns: list[str] = Field(default_factory=list, max_length=8)
    offset: int = Field(default=0, ge=0)


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


def _default_bundle() -> dict[str, str]:
    root = files("evog").joinpath("harness/base/tools")
    contents = {}

    def visit(directory, prefix):
        for item in directory.iterdir():
            path = prefix + "/" + item.name
            if item.is_dir():
                visit(item, path)
            elif item.name.endswith((".py", ".yaml")):
                contents[path] = item.read_text(encoding="utf-8")

    visit(root, "tools")
    return contents


def _registry(harness: Harness | None) -> tuple[dict[str, str], list[dict]]:
    contents = dict(harness.contents) if harness else {}
    if "tools/registry.yaml" not in contents:
        contents.update(_default_bundle())
    registry = yaml.safe_load(contents["tools/registry.yaml"])
    return contents, [row for row in registry["tools"] if row.get("enabled", True)]


def tool_definitions(harness: Harness | None = None) -> list[dict[str, Any]]:
    """Load authoritative candidate tool descriptions and declarative queries."""
    contents, registry = _registry(harness)
    definitions = []
    for entry in registry:
        description = yaml.safe_load(contents[entry["description_file"]])
        definitions.append({"type": "function", "function": description})
    if harness is not None:
        for query_tool in harness.operations.query_tools:
            definitions.append(
                {
                    "type": "function",
                    "function": {
                        "name": query_tool.name,
                        "description": query_tool.description,
                        "parameters": QueryToolArgs.model_json_schema(),
                    },
                }
            )
    return definitions


def _validate_arguments(value: Any, schema: dict) -> None:
    """Evaluate candidate argument schemas without resolving external resources."""
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import SchemaError, ValidationError
    from referencing import Registry
    from referencing.exceptions import NoSuchResource, Unresolvable

    def deny_remote(uri):
        raise NoSuchResource(ref=uri)

    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode("utf-8")) > 128000:
            raise ContractError("Tool arguments exceed the byte budget")
        Draft202012Validator.check_schema(schema)
        validator = Draft202012Validator(schema, registry=Registry(retrieve=deny_remote))
        if next(validator.iter_errors(value), None) is not None:
            raise ContractError("Tool arguments do not match the declared JSON schema")
    except (
        SchemaError,
        ValidationError,
        Unresolvable,
        NoSuchResource,
        TypeError,
        ValueError,
        RecursionError,
        OverflowError,
    ) as exc:
        raise ContractError("Tool arguments or parameter schema are invalid") from exc


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
        output_transform: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        defer_delivery: bool = False,
        component_deadline: float | None = None,
    ):
        if not group_ids:
            raise ContractError("Explicit group scope is required")
        known = {row["group_id"] for row in store.groups()}
        if not set(group_ids).issubset(known):
            raise ContractError("One or more requested groups have no imported messages")
        self.harness = harness
        self.output_transform = output_transform
        self.defer_delivery = defer_delivery
        self.component_deadline = component_deadline
        self._operation_override = None
        self.frozen_memory = frozen_memory
        # Each source view is frozen at interaction start, even if a later import occurs.
        self.context: dict[str, list[dict[str, Any]]] = {}
        self.group_paths = {}
        for group in sorted(set(group_ids)):
            path = f"memory_units/{fingerprint(group)}.jsonl"
            self.group_paths[path] = group
            self.context[path] = [self._record(message) for message in store.messages(group)]
        self._source_lines = {
            path: [dumps(row) for row in rows] for path, rows in self.context.items()
        }
        self._source_refs = {
            dumps(row): row["ref"] for rows in self.context.values() for row in rows
        }
        # Keep durable notes separate from per-question scratch state. Long-term notes are scoped
        # to the authorized groups and survive question sessions; working memory is session-scoped
        # when a caller supplies ``memory_session``.
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
        for root, prefix in (
            (self.working_root, "memory_store/working_memory/"),
            (self.long_term_root, "memory_store/long_term_memory/"),
        ):
            for path in root.rglob("*"):
                if path.is_file() and not path.is_symlink():
                    relative = path.relative_to(root).as_posix()
                    safe_path(root, relative)
                    notes.append(prefix + relative)
        skills = [name for name in self.harness.contents if name.startswith("skills/")]
        return sorted(
            [
                *self.context,
                *notes,
                *skills,
                *self.results.paths,
                *("workspace/" + name for name in self.harness.contents),
            ]
        )

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

    def _lines(self, path: str) -> list[str]:
        if (
            path.startswith("workspace/")
            and path.removeprefix("workspace/") in self.harness.contents
        ):
            return self.harness.contents[path.removeprefix("workspace/")].splitlines()
        if path in self.results.paths:
            actual = safe_path(self.results.root, path.removeprefix("tool_results/"))
            return actual.read_text(encoding="utf-8").splitlines()
        if path in self.context:
            return self._source_lines[path]
        if path.startswith("memory_units/") and path in self.context:
            return self._source_lines[path]
        if path.startswith("skills/") and path in self.harness.contents:
            return self.harness.contents[path].splitlines()
        root = None
        relative = ""
        if path.startswith("memory_store/working_memory/"):
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

    def _allowed(self, path: str, roots: list[str]) -> bool:
        if path.startswith("tool_results/"):
            path = "workspace/" + path
        return any(
            path == root.rstrip("/") or path.startswith(root.rstrip("/") + "/") for root in roots
        )

    def _capability(self, entry: dict, operations: Any, name: str, payload: dict) -> Any:
        if self.component_deadline is not None and time.monotonic() >= self.component_deadline:
            raise DeadlineExceeded("Interaction deadline reached during tool capability access")
        readable = entry.get("readable_roots", [])
        writable = entry.get("writable_roots", [])
        if any(
            root.split("/")[0] not in {"workspace", "memory_units", "memory_store", "skills"}
            for root in readable
        ):
            raise ContractError("Tool readable roots exceed product capabilities")
        if any(
            root != "memory_store" and not root.startswith("memory_store/") for root in writable
        ):
            raise ContractError("Tool writable roots exceed product capabilities")
        if name == "operations":
            return operations.model_dump(mode="json")
        if name == "files":
            return [path for path in self.files() if self._allowed(path, readable)]
        if name == "archive_paths":
            return [path for path in self.results.paths if self._allowed(path, readable)]
        if name == "group_paths":
            return {
                path: group
                for path, group in self.group_paths.items()
                if self._allowed(path, readable)
            }
        if name in {"read_lines", "read_document", "source_record", "source_context"}:
            path = payload.get("path", "")
            if not isinstance(path, str) or not self._allowed(path, readable):
                raise ContractError("Tool read capability is outside registry scope")
            if name == "read_lines":
                return self._lines(path)
            source = path
            rows = self.context.get(source, [])
            if name == "read_document":
                lines = self._lines(path)
                offset = payload.get("offset", 0)
                if not isinstance(offset, int) or offset < 0:
                    raise ContractError("Document offset must be nonnegative")
                page_lines, page_records, size = [], [], 0
                for index in range(offset, len(lines)):
                    record = rows[index] if rows else None
                    cost = len(dumps({"line": lines[index], "record": record}).encode("utf-8"))
                    if page_lines and size + cost > 240000:
                        break
                    page_lines.append(lines[index])
                    if record is not None:
                        page_records.append(record)
                    size += cost
                next_offset = offset + len(page_lines)
                return {
                    "lines": page_lines,
                    "records": page_records,
                    "offset": offset,
                    "total_lines": len(lines),
                    "next_offset": next_offset if next_offset < len(lines) else None,
                }
            index = payload.get("index", 0)
            if not isinstance(index, int) or index < 0:
                raise ContractError("Source index must be nonnegative")
            if name == "source_record":
                return rows[index] if index < len(rows) else None
            window = payload.get("window", 0)
            if not isinstance(window, int) or not 0 <= window <= 5:
                raise ContractError("Source context window exceeds capability limit")
            return rows[max(0, index - window) : index + window + 1]
        if name == "note_state":
            arguments = payload.get("arguments", {})
            args = WriteArgs.model_validate(arguments)
            logical = self._logical_path(args.target, args.resource_path)
            if not self._allowed(logical, readable):
                raise ContractError("Tool note-read capability is outside registry scope")
            resource = args.resource_path
            root = (
                self.long_term_root
                if resource.startswith("long_term_memory/")
                else self.working_root
                if resource.startswith("working_memory/")
                else None
            )
            if root is None:
                raise ContractError("Notes require a writable memory layer")
            path = safe_path(root, resource.partition("/")[2])
            if path.name == "search_ledger.jsonl":
                raise ContractError("The search ledger is runtime-owned and read-only")
            if path.exists() and path.stat().st_size > 64000:
                raise ContractError("Note exceeds the read limit")
            return {
                "exists": path.exists(),
                "content": path.read_text(encoding="utf-8") if path.exists() else "",
                "staged": self.frozen_memory is not None and root == self.long_term_root,
            }
        if name == "write_note":
            arguments = payload.get("arguments", {})
            create = payload.get("create", False)
            model = CreateArgs if create else WriteArgs
            args = model.model_validate(arguments)
            logical = self._logical_path(args.target, args.resource_path)
            if not self._allowed(logical, writable):
                raise ContractError("Tool write capability is outside registry scope")
            return self._write(args, create=bool(create))
        raise ContractError("Unknown fixed tool capability")

    def _visible_refs(self, result: dict) -> set[str]:
        """Grant evidence only for complete original records present in final output."""
        strings = set()

        def collect(value):
            if isinstance(value, str):
                strings.add(value)
            elif isinstance(value, dict):
                strings.add(dumps(value))
                for item in value.values():
                    collect(item)
            elif isinstance(value, list):
                for item in value:
                    collect(item)

        collect(result)
        refs = {self._source_refs[text] for text in strings if text in self._source_refs}
        # An archived result is evidence only after authentic complete lines/chunks
        # are returned to the model, never merely requested by candidate code.
        path = result.get("path")
        lines = result.get("lines")
        if isinstance(path, str) and path in self.results.paths and isinstance(lines, list):
            source = self._lines(path)
            indices = []
            for row in lines:
                if not isinstance(row, dict):
                    return refs
                index = row.get("line")
                if (
                    not isinstance(index, int)
                    or not 1 <= index <= len(source)
                    or row.get("text") != source[index - 1]
                ):
                    return refs
                indices.append(index - 1)
            if indices and indices == list(range(indices[0], indices[-1] + 1)):
                refs.update(self.results.delivered(path, indices[0], indices[-1] + 1, len(source)))
        return refs

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        from evog.harness.executor import ComponentExecutionError, execute_component

        self.last_full_result = None
        self.last_result_path = None
        if self.component_deadline is not None and time.monotonic() >= self.component_deadline:
            raise DeadlineExceeded("Interaction deadline reached before tool execution")
        contents, registry = _registry(self.harness)
        entry = next((row for row in registry if row["name"] == name), None)
        operations = self.harness.operations
        if entry is None:
            query = next((row for row in operations.query_tools if row.name == name), None)
            if query is None:
                raise ContractError("Unknown tool")
            entry = next((row for row in registry if row["name"] == "grep_search"), None)
            if entry is None:
                raise ContractError("Query aliases require enabled grep_search")
            args = SearchArgs.model_validate(
                {
                    **QueryToolArgs.model_validate(arguments).model_dump(),
                    "target": query.target,
                    "resource_path": query.resource_path,
                }
            )
            operations = operations.for_query_tool(query)
        elif name in ARGUMENTS:
            description_path = entry["description_file"]
            baseline = _default_bundle().get(description_path)
            if contents[description_path] != baseline:
                description = yaml.safe_load(contents[description_path])
                _validate_arguments(arguments, description["parameters"])
                model = ARGUMENTS[name][0]
                args = model.model_validate(
                    {key: value for key, value in arguments.items() if key in model.model_fields}
                )
                payload_extension = dict(arguments)
            else:
                args = ARGUMENTS[name][0].model_validate(arguments)
                payload_extension = {}
        else:
            # Candidate-added tools are described and constrained by their schema.
            description = yaml.safe_load(contents[entry["description_file"]])
            _validate_arguments(arguments, description["parameters"])
            args = None
        payload = dict(args.__dict__) if args is not None else dict(arguments)
        if name in ARGUMENTS and entry["name"] == name:
            payload.update(payload_extension)
        capability_errors = []

        def capability(name, data):
            try:
                return self._capability(entry, operations, name, data)
            except (ContractError, DeadlineExceeded) as exc:
                capability_errors.append(exc)
                raise
            except FileExistsError:
                error = FileExistsError("Note already exists")
                capability_errors.append(error)
                raise error from None

        remaining = (
            self.component_deadline - time.monotonic()
            if self.component_deadline is not None
            else 10
        )
        if remaining <= 0:
            raise DeadlineExceeded("Interaction deadline reached before tool execution")
        try:
            result = execute_component(
                contents, entry["handler"], payload, capability, timeout_seconds=min(10, remaining)
            )
        except ComponentExecutionError as exc:
            if self.component_deadline is not None and time.monotonic() >= self.component_deadline:
                raise DeadlineExceeded(
                    "Interaction deadline reached during tool execution"
                ) from exc
            if capability_errors:
                raise capability_errors[-1] from exc
            raise ContractError(str(exc)) from exc
        if not isinstance(result, dict):
            raise ContractError("Tool implementation must return a JSON object")
        if self.output_transform is not None:
            result = self.output_transform(result)
            if not isinstance(result, dict):
                raise ContractError("Tool output middleware must return a JSON object")
        archive_reads = {
            path: set(archive["read"]) for path, archive in self.results.archives.items()
        }
        refs = self._visible_refs(result)
        self.last_full_result = result
        self._append_ledger(
            {
                "operation": {"read_file": "read", "grep_search": "search"}.get(name, name),
                "arguments": arguments,
                "returned_refs": sorted(refs),
                "truncated": bool(result.get("truncated")),
            }
        )
        if len(dumps(result)) <= self.results.max_chars:
            if self.defer_delivery:
                for path, read in archive_reads.items():
                    self.results.archives[path]["read"] = read
            else:
                self.delivered_refs.update(refs)
            return result
        for path, read in archive_reads.items():
            self.results.archives[path]["read"] = read
        preview = self.results.persist(result, refs)
        self.last_result_path = preview["full_result_path"]
        return preview

    def confirm_delivery(self, rendered: dict[str, Any]) -> None:
        """Commit evidence from a tool result actually submitted in model context.

        Deferred execution never grants evidence or advances archived chunk coverage.
        The runtime calls this after middleware and hard context trimming, preserving
        any refs already delivered in earlier model calls.
        """
        if not isinstance(rendered, dict):
            raise ContractError("Delivered tool result must be a JSON object")
        self.delivered_refs.update(self._visible_refs(rendered))

    def _write(self, args: WriteArgs | CreateArgs, create: bool) -> dict[str, Any]:
        resource = args.resource_path
        relative = resource.removeprefix("memory_store/")
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
