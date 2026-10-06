"""Candidate-only file editing with evidence-linked, host-derived diffs."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from pydantic import Field

from evog.core.errors import ContractError
from evog.core.io import atomic_write, safe_path
from evog.core.models import Change, Interface, PlanDraft, Record
from evog.harness.schema import Harness, component_for


class FileArgs(Record):
    path: str = Field(min_length=1, max_length=512)


class ReadFileArgs(FileArgs):
    start_line: int = Field(default=1, ge=1)
    end_line: int | None = Field(default=None, ge=1)


class WriteFileArgs(FileArgs):
    content: str = Field(max_length=128000)


class EditFileArgs(FileArgs):
    old_text: str = Field(min_length=1, max_length=128000)
    new_text: str = Field(max_length=128000)


class SearchFilesArgs(Record):
    query: str = Field(min_length=1, max_length=512)
    prefix: str = Field(default="", max_length=512)


class RegisterChangeArgs(Record):
    paths: list[str] = Field(min_length=1, max_length=32)
    interface: Interface
    finding_ids: list[str] = Field(min_length=1, max_length=32)
    rationale: str = Field(min_length=1, max_length=4000)
    expected_effect: str = Field(min_length=1, max_length=4000)
    regression_risk: str = Field(min_length=1, max_length=4000)
    validation: str = Field(min_length=1, max_length=4000)
    predicted_fix_runs: list[str] = Field(default_factory=list, max_length=200)
    risk_runs: list[str] = Field(default_factory=list, max_length=200)


WORKSPACE_TOOLS = [
    ("read_workspace", "Read a candidate component file using a relative path", ReadFileArgs),
    ("write_workspace", "Create or replace a candidate component file", WriteFileArgs),
    (
        "edit_workspace",
        "Replace one exact, unique text occurrence in a candidate file",
        EditFileArgs,
    ),
    ("delete_workspace", "Delete an optional candidate component file", FileArgs),
    ("search_workspace", "Find literal text in candidate component files", SearchFilesArgs),
    (
        "register_change",
        "Register the change's behavioral interface (Representation, Operation, Intervention or Policy), findings, predicted impact, risk and validation",
        RegisterChangeArgs,
    ),
]


class CandidateWorkspace:
    def __init__(self, workspace: Path, parent: Harness):
        self.parent = parent
        self.root = safe_path(workspace, f"candidates/{uuid4().hex}/workspace")
        self.root.mkdir(parents=True, mode=0o700)
        self.contents = dict(parent.contents)
        self.metadata: dict[str, dict] = {}
        for path, content in self.contents.items():
            atomic_write(safe_path(self.root, path), content)

    def _path(self, path: str) -> Path:
        component_for(path)
        return safe_path(self.root, path)

    def execute(self, name: str, arguments: dict) -> dict:
        if name == "read_workspace":
            args = ReadFileArgs.model_validate(arguments)
            self._path(args.path)
            if args.path not in self.contents:
                raise ContractError("Candidate file does not exist")
            lines = self.contents[args.path].splitlines()
            end = min(args.end_line or args.start_line + 199, len(lines))
            if end < args.start_line:
                raise ContractError("Invalid candidate line range")
            text = "\n".join(lines[args.start_line - 1 : end])
            # No unreported truncation: large lines require an edit or smaller file.
            if len(text) > 128000:
                raise ContractError("Candidate read exceeds the response budget")
            return {
                "path": args.path,
                "start_line": args.start_line,
                "end_line": end,
                "total_lines": len(lines),
                "content": text,
            }
        if name in {"write_workspace", "edit_workspace", "delete_workspace"}:
            if name == "write_workspace":
                args = WriteFileArgs.model_validate(arguments)
                text = args.content
            elif name == "edit_workspace":
                args = EditFileArgs.model_validate(arguments)
                prior = self.contents.get(args.path, "")
                if prior.count(args.old_text) != 1:
                    raise ContractError("Edit requires exactly one matching occurrence")
                text = prior.replace(args.old_text, args.new_text, 1)
            else:
                args = FileArgs.model_validate(arguments)
                path = self._path(args.path)
                if args.path not in self.contents:
                    raise ContractError("Candidate file does not exist")
                path.unlink()
                del self.contents[args.path]
                self.metadata.pop(args.path, None)
                return {"deleted": args.path}
            path = self._path(args.path)
            updated = {**self.contents, args.path: text}
            if len(updated) > 128 or sum(len(v.encode()) for v in updated.values()) > 1000000:
                raise ContractError("Candidate exceeds the component bundle budget")
            if not text.strip() or len(text.encode()) > 128000:
                raise ContractError("Candidate file is empty or oversized")
            atomic_write(path, text)
            self.contents = updated
            # Edits invalidate earlier validation and descriptions for that file.
            self.metadata.pop(args.path, None)
            return {"written": args.path, "chars": len(text)}
        if name == "search_workspace":
            args = SearchFilesArgs.model_validate(arguments)
            hits = []
            for path, content in sorted(self.contents.items()):
                if not path.startswith(args.prefix):
                    continue
                for index, line in enumerate(content.splitlines(), 1):
                    if args.query in line:
                        hits.append({"path": path, "line": index, "text": line[:1000]})
            return {"matches": hits[:50], "total_matches": len(hits), "truncated": len(hits) > 50}
        if name == "register_change":
            args = RegisterChangeArgs.model_validate(arguments)
            if len(args.paths) != len(set(args.paths)):
                raise ContractError("Changed paths must be unique")
            for path in args.paths:
                self._path(path)
                if self.parent.contents.get(path) == self.contents.get(path):
                    raise ContractError("Registered change must reference a modified file")
            metadata = args.model_dump(exclude={"paths"})
            for path in args.paths:
                self.metadata[path] = metadata
            return {"registered": args.paths}
        raise ContractError("Unknown candidate workspace tool")

    def draft(self, summary: str) -> PlanDraft:
        changes = []
        for path in sorted(set(self.parent.contents) | set(self.contents)):
            if self.parent.contents.get(path) == self.contents.get(path):
                continue
            if path not in self.metadata:
                raise ContractError(f"Changed file has no evidence-linked registration: {path}")
            changes.append(
                Change(
                    path=path,
                    content=self.contents.get(path, ""),
                    operation="write" if path in self.contents else "delete",
                    **self.metadata[path],
                )
            )
        return PlanDraft(summary=summary[:8000], changes=changes)
