"""Transactional local persistence. Sources and trace events are append-only."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from evog.errors import ConflictError, ContractError
from evog.harness import Harness, initial_files
from evog.io import dumps
from evog.models import Answer, Event, Feedback, Message

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
 group_id TEXT NOT NULL, message_id TEXT NOT NULL, timestamp TEXT NOT NULL, payload TEXT NOT NULL,
 PRIMARY KEY (group_id, message_id));
CREATE INDEX IF NOT EXISTS message_time ON messages(group_id, timestamp, message_id);
CREATE TABLE IF NOT EXISTS revisions (
 id TEXT PRIMARY KEY, files TEXT NOT NULL, parent_id TEXT, plan_id TEXT, validation TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs (
 id TEXT PRIMARY KEY, revision_id TEXT NOT NULL, question TEXT NOT NULL, groups TEXT NOT NULL,
 status TEXT NOT NULL, answer TEXT, created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')));
CREATE TABLE IF NOT EXISTS events (
 run_id TEXT NOT NULL REFERENCES runs(id), idx INTEGER NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL,
 PRIMARY KEY (run_id, idx));
CREATE TABLE IF NOT EXISTS feedback (
 id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES runs(id),
 outcome TEXT NOT NULL, source TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS artifacts (id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL);
"""


class Store:
    def __init__(self, workspace: Path):
        self.workspace = workspace.expanduser().resolve()
        self.workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.database = self.workspace / "evog.sqlite3"
        if self.database.is_symlink():
            raise ContractError("Workspace database must not be a symlink")
        with self.connect() as db:
            db.executescript(SCHEMA)
            db.execute("BEGIN IMMEDIATE")
            # The index stores instants in UTC; source payloads retain their original offset.
            if not db.execute("SELECT 1 FROM state WHERE key='timestamp_index_v2'").fetchone():
                rows = db.execute("SELECT group_id,message_id,timestamp FROM messages").fetchall()
                db.executemany(
                    "UPDATE messages SET timestamp=? WHERE group_id=? AND message_id=?",
                    [
                        (
                            datetime.fromisoformat(row["timestamp"])
                            .astimezone(UTC)
                            .isoformat(timespec="microseconds"),
                            row["group_id"],
                            row["message_id"],
                        )
                        for row in rows
                    ],
                )
                db.execute("INSERT INTO state VALUES ('timestamp_index_v2', '1')")
            base = Harness(initial_files())
            db.execute(
                "INSERT OR IGNORE INTO revisions VALUES (?, ?, NULL, NULL, 'structural')",
                (base.id, dumps(base.contents)),
            )
            db.execute("INSERT OR IGNORE INTO state VALUES ('active_revision', ?)", (base.id,))
        self.database.chmod(0o600)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.database, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def ingest(self, messages: Iterator[Message]) -> int:
        added = 0
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for message in messages:
                encoded = dumps(message.model_dump(mode="json"))
                existing = db.execute(
                    "SELECT payload FROM messages WHERE group_id=? AND message_id=?",
                    (message.group_id, message.message_id),
                ).fetchone()
                if existing:
                    if existing["payload"] != encoded:
                        raise ConflictError(
                            f"Source record {message.ref} already has different content"
                        )
                    continue
                db.execute(
                    "INSERT INTO messages VALUES (?, ?, ?, ?)",
                    (
                        message.group_id,
                        message.message_id,
                        message.timestamp.astimezone(UTC).isoformat(timespec="microseconds"),
                        encoded,
                    ),
                )
                added += 1
        return added

    def ingest_jsonl(self, path: Path) -> int:
        def rows() -> Iterator[Message]:
            with path.open(encoding="utf-8") as stream:
                for index, line in enumerate(stream, 1):
                    if not line.strip():
                        continue
                    if len(line) > 64000:
                        raise ContractError(f"Message at line {index} exceeds the import limit")
                    try:
                        yield Message.model_validate_json(line)
                    except ValueError as exc:
                        raise ContractError(f"Invalid message at line {index}") from exc

        return self.ingest(rows())

    def groups(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT group_id, COUNT(*) AS messages FROM messages GROUP BY group_id ORDER BY group_id"
                )
            ]

    def messages(self, group_id: str) -> list[Message]:
        with self.connect() as db:
            return [
                Message.model_validate_json(row["payload"])
                for row in db.execute(
                    "SELECT payload FROM messages WHERE group_id=? ORDER BY timestamp,message_id",
                    (group_id,),
                )
            ]

    def harness(self, revision_id: str | None = None) -> Harness:
        with self.connect() as db:
            if revision_id is None:
                revision_id = db.execute(
                    "SELECT value FROM state WHERE key='active_revision'"
                ).fetchone()[0]
            row = db.execute("SELECT files FROM revisions WHERE id=?", (revision_id,)).fetchone()
        if row is None:
            raise ContractError("Unknown harness revision")
        return Harness(json.loads(row[0]))

    def activate(self, harness: Harness, parent_id: str, plan_id: str, validation: str) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            active = db.execute("SELECT value FROM state WHERE key='active_revision'").fetchone()[0]
            if active != parent_id:
                raise ConflictError(
                    "Active revision changed; analyze and propose against the current revision"
                )
            db.execute(
                "INSERT OR IGNORE INTO revisions VALUES (?, ?, ?, ?, ?)",
                (harness.id, dumps(harness.contents), parent_id, plan_id, validation),
            )
            if validation == "business":
                db.execute("UPDATE revisions SET validation='business' WHERE id=?", (harness.id,))
            db.execute("UPDATE state SET value=? WHERE key='active_revision'", (harness.id,))
            db.execute(
                "INSERT INTO artifacts VALUES(?,?,?)",
                (
                    uuid4().hex,
                    "revision_activation",
                    dumps(
                        {
                            "revision_id": harness.id,
                            "parent_revision_id": parent_id,
                            "plan_id": plan_id,
                            "validation": validation,
                        }
                    ),
                ),
            )

    def rollback(self, revision_id: str) -> None:
        self.harness(revision_id)
        with self.connect() as db:
            db.execute("UPDATE state SET value=? WHERE key='active_revision'", (revision_id,))

    def revisions(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT id,parent_id,plan_id,validation FROM revisions ORDER BY rowid"
                )
            ]

    def start_run(self, run_id: str, revision_id: str, question: str, groups: list[str]) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO runs(id,revision_id,question,groups,status) VALUES(?,?,?,?, 'running')",
                (run_id, revision_id, question, dumps(groups)),
            )

    def event(self, run_id: str, kind: str, data: dict[str, Any]) -> Event:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            index = db.execute(
                "SELECT COALESCE(MAX(idx),-1)+1 FROM events WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            db.execute("INSERT INTO events VALUES(?,?,?,?)", (run_id, index, kind, dumps(data)))
        return Event(index=index, kind=kind, data=data)

    def events(self, run_id: str) -> list[Event]:
        with self.connect() as db:
            return [
                Event(index=row["idx"], kind=row["kind"], data=json.loads(row["data"]))
                for row in db.execute("SELECT * FROM events WHERE run_id=? ORDER BY idx", (run_id,))
            ]

    def finish_run(self, run_id: str, answer: Answer | None, status: str) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE runs SET status=?, answer=? WHERE id=?",
                (status, answer.model_dump_json() if answer else None, run_id),
            )

    def run(self, run_id: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise ContractError("Unknown run ID")
        result = dict(row)
        result["groups"] = json.loads(result["groups"])
        result["answer"] = json.loads(result["answer"]) if result["answer"] else None
        return result

    def feedback(self, feedback: Feedback) -> None:
        if self.run(feedback.run_id)["status"] != "completed":
            raise ContractError("Feedback requires a completed interaction")
        with self.connect() as db:
            db.execute(
                "INSERT INTO feedback(run_id,outcome,source) VALUES(?,?,?)",
                (feedback.run_id, feedback.outcome, feedback.source),
            )

    def runs(self, revision_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """
                SELECT r.*, f.outcome FROM runs r LEFT JOIN feedback f ON f.id=(
                  SELECT MAX(id) FROM feedback WHERE run_id=r.id)
                WHERE revision_id=? ORDER BY r.rowid DESC
            """,
                (revision_id,),
            ).fetchall()
        return [
            {**dict(row), "answer": json.loads(row["answer"]) if row["answer"] else None}
            for row in rows
        ]

    def save_artifact(self, artifact_id: str, kind: str, payload: dict[str, Any]) -> None:
        with self.connect() as db:
            db.execute("INSERT INTO artifacts VALUES(?,?,?)", (artifact_id, kind, dumps(payload)))

    def artifact(self, artifact_id: str, kind: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute(
                "SELECT payload FROM artifacts WHERE id=? AND kind=?", (artifact_id, kind)
            ).fetchone()
        if row is None:
            raise ContractError(f"Unknown {kind} ID")
        return json.loads(row[0])

    def artifacts(self, kind: str, *, limit: int = 20) -> list[dict[str, Any]]:
        if not 1 <= limit <= 100:
            raise ContractError("Artifact history limit must be within 1–100")
        with self.connect() as db:
            rows = db.execute(
                "SELECT payload FROM artifacts WHERE kind=? ORDER BY rowid DESC LIMIT ?",
                (kind, limit),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]
