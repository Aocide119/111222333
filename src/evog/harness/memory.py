"""Frozen round memory with extractive, source-verified staged notes.

Round writes never change another question's read view. Merge order is canonical;
question/gold/verdict data is not an input to the snapshot builder.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from evog.core.errors import ContractError
from evog.core.io import atomic_write, dumps, fingerprint, safe_path


class MemoryRound:
    def __init__(self, workspace: Path, snapshot_id: str | None, round_key: str):
        self.root = safe_path(workspace, "round_memory")
        self.workspace = workspace
        self.root.mkdir(parents=True, exist_ok=True)
        self.stage_root = safe_path(self.root, "staging/" + fingerprint(round_key))
        self.stage_root.mkdir(parents=True, exist_ok=True)
        self.round_key = round_key
        if snapshot_id is None:
            self.snapshot = {"schema": "evog.memory.v1", "parent": None, "scopes": {}}
            self.snapshot_id = self._save(self.snapshot)
        else:
            self.snapshot_id = snapshot_id
            path = safe_path(self.root, f"snapshots/{snapshot_id}.json")
            self.snapshot = json.loads(path.read_text(encoding="utf-8"))
            if fingerprint(self.snapshot) != snapshot_id:
                raise ContractError("Memory snapshot fingerprint mismatch")

    def _save(self, snapshot: dict) -> str:
        identity = fingerprint(snapshot)
        path = safe_path(self.root, f"snapshots/{identity}.json")
        if path.exists():
            if path.read_text(encoding="utf-8") != dumps(snapshot):
                raise ContractError("Memory snapshot is inconsistent")
        else:
            atomic_write(path, dumps(snapshot))
        return identity

    def binding(self, group_ids: list[str], episode_id: str) -> QuestionMemory:
        return QuestionMemory(self, sorted(set(group_ids)), episode_id)

    def merge(self) -> str:
        scopes = json.loads(dumps(self.snapshot["scopes"]))
        origins = []
        statuses = {}
        if (self.workspace / "evog.sqlite3").exists():
            with sqlite3.connect(self.workspace / "evog.sqlite3") as database:
                statuses = dict(database.execute("SELECT id,status FROM runs"))
        for path in sorted(self.stage_root.glob("*.json")):
            row = json.loads(path.read_text(encoding="utf-8"))
            status = statuses.get(row.get("run_id"), "direct_tool_session")
            if row.get("run_id") and status == "direct_tool_session":
                raise ContractError("Memory contribution has an unknown source run")
            # Only terminal, completed answering sessions contribute reusable notes.
            # Failed or interrupted stages stay archived but do not enter future snapshots.
            if row.get("run_id") and status != "completed":
                origins.append(
                    {
                        "episode_id": row["episode_id"],
                        "run_id": row["run_id"],
                        "status": status,
                        "merge": "excluded",
                    }
                )
                continue
            scope = scopes.setdefault(row["scope_id"], {"groups": row["groups"], "files": {}})
            if scope["groups"] != row["groups"]:
                raise ContractError("Memory scope mismatch")
            for name, entries in row["files"].items():
                known = {dumps(entry): entry for entry in scope["files"].get(name, [])}
                known.update({dumps(entry): entry for entry in entries})
                scope["files"][name] = [known[key] for key in sorted(known)]
            origins.append(
                {
                    "episode_id": row["episode_id"],
                    "scope_id": row["scope_id"],
                    "run_id": row.get("run_id"),
                    "status": status,
                    "source_fingerprint": row["source_fingerprint"],
                    "staging_fingerprint": fingerprint(row),
                    "merge": "included",
                }
            )
        if len(dumps(scopes)) > 800000:
            raise ContractError("Merged memory exceeds its bounded snapshot budget")
        return self._save(
            {
                "schema": "evog.memory.v1",
                "parent": self.snapshot_id,
                "round_key": self.round_key,
                "scopes": scopes,
                "origins": origins,
            }
        )


class QuestionMemory:
    def __init__(self, parent: MemoryRound, groups: list[str], episode_id: str):
        self.parent, self.groups, self.episode_id = parent, groups, episode_id
        self.run_id: str | None = None
        self.scope_id = fingerprint(groups)
        self.stage_path = safe_path(
            parent.stage_root,
            fingerprint(
                {
                    "scope": self.scope_id,
                    "episode": episode_id,
                }
            )
            + ".json",
        )

    def bind_run(self, run_id: str) -> None:
        if self.stage_path.exists():
            previous = json.loads(self.stage_path.read_text())
            if previous.get("run_id") != run_id:
                raise ContractError("Another attempt already owns this question's memory staging")
        self.run_id = run_id

    def seed(self, destination: Path) -> None:
        """Copy only this authorized scope into the question's private read/write layer."""
        scope = self.parent.snapshot["scopes"].get(self.scope_id, {})
        if scope and scope["groups"] != self.groups:
            raise ContractError("Memory groups do not match the authorized scope")
        for name, entries in scope.get("files", {}).items():
            atomic_write(safe_path(destination, name), dumps({"entries": entries}))

    def validate_and_stage(
        self,
        name: str,
        content: str,
        delivered: set[str],
        sources: dict[str, str],
        *,
        append: bool = False,
    ) -> str:
        """Long-term notes are JSON entries of {ref, excerpt}, not model-generated facts."""
        try:
            data = json.loads(content)
            if set(data) != {"entries"} or not isinstance(data["entries"], list):
                raise ValueError
            entries = data["entries"]
            if not 1 <= len(entries) <= 64:
                raise ValueError
            for entry in entries:
                if not isinstance(entry, dict) or set(entry) != {"ref", "excerpt"}:
                    raise ValueError
                ref, excerpt = entry["ref"], entry["excerpt"]
                if not isinstance(ref, str) or not isinstance(excerpt, str) or not excerpt.strip():
                    raise ValueError
                if ref not in delivered or ref not in sources or excerpt not in sources[ref]:
                    raise ValueError
        except (ValueError, TypeError, KeyError):
            raise ContractError(
                "Frozen long-term memory accepts JSON entries with ref and an exact excerpt "
                "from a fully delivered authorized source record"
            ) from None
        # This file is private to a single question; no shared concurrent updates.
        row = (
            json.loads(self.stage_path.read_text())
            if self.stage_path.exists()
            else {
                "scope_id": self.scope_id,
                "groups": self.groups,
                "episode_id": self.episode_id,
                "files": {},
                "run_id": self.run_id,
                "source_fingerprint": fingerprint(sources),
            }
        )
        if row["source_fingerprint"] != fingerprint(sources) or row.get("run_id") != self.run_id:
            raise ContractError("Memory staging source or attempt changed")
        existing = row["files"].get(name, []) if append else []
        unique = {dumps(entry): entry for entry in [*existing, *entries]}
        row["files"][name] = [unique[key] for key in sorted(unique)]
        if len(dumps(row)) > 64000:
            raise ContractError("Staged long-term memory exceeds its per-question budget")
        atomic_write(self.stage_path, dumps(row))
        return dumps({"entries": entries})
