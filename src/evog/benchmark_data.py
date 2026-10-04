"""EverMemBench and GroupMemBench readers for source messages and evaluation episodes."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from datetime import datetime
from itertools import islice
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field

from evog.errors import ContractError
from evog.io import dumps, fingerprint, safe_path
from evog.models import Message, Record

BenchmarkName = Literal["evermembench", "groupmembench"]
GROUP_DOMAINS = ("Finance", "Technology", "Healthcare", "Manufacturing")
QUESTION_TYPES = (
    "multi_hop",
    "knowledge_update",
    "temporal",
    "user_implicit",
    "term_ambiguity",
    "abstention",
)


class Episode(Record):
    benchmark: BenchmarkName
    episode_id: str = Field(min_length=1, max_length=256)
    question: str = Field(min_length=1, max_length=10000)
    gold: str = Field(min_length=1, max_length=20000)
    options: dict[str, str] = Field(default_factory=dict, max_length=8)
    scope: str
    question_type: str
    asking_user_id: str = ""

    def prompt(self) -> str:
        # Explicit allowlist: no gold, evidence ranges, type labels or judge output.
        parts = [self.question]
        if self.asking_user_id:
            parts.append(f"The user asking this question is {self.asking_user_id}.")
        if self.options:
            parts.append("Options:\n" + "\n".join(f"{k}: {v}" for k, v in self.options.items()))
            parts.append("Put only the chosen option letter in the JSON answer's text field.")
        return "\n\n".join(parts)


class Corpus:
    def __init__(self, messages: list[Message], source: Path):
        if not messages:
            raise ContractError("Benchmark corpus has no source messages")
        self.messages = messages
        self.group_ids = sorted({message.group_id for message in messages})
        self.source = source
        self.fingerprint = fingerprint([message.model_dump(mode="json") for message in messages])


def resolve_root(name: BenchmarkName, explicit: Path | None) -> Path:
    value = explicit or os.environ.get(f"{name.upper()}_ROOT")
    if not value:
        raise ContractError(f"Pass --data-root or set {name.upper()}_ROOT")
    root = Path(value).expanduser().resolve()
    expected = "dataset" if name == "evermembench" else "data/final"
    if not (root / expected).is_dir():
        raise ContractError(f"{name} data root must contain {expected}/")
    return root


def read_episode_ids(path: Path | None) -> set[str] | None:
    if path is None:
        return None
    return {
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }


def _qa_rows(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        for key in ("qars", "questions", "episodes"):
            if key in payload:
                payload = payload[key]
                break
    if not isinstance(payload, list) or any(not isinstance(row, dict) for row in payload):
        raise ContractError("Unsupported EverMemBench QA layout")
    return payload


def _options(raw: Any) -> dict[str, str]:
    # List entries may already include an option-letter prefix.
    if raw in (None, [], {}):
        return {}
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    if isinstance(raw, list) and len(raw) <= 8:
        parsed = {}
        for index, entry in enumerate(raw):
            text = str(entry)
            if len(text) > 1 and text[0] in "ABCDEFGH" and text[1] in ".):":
                parsed[text[0]] = text[2:].strip()
            else:
                parsed[chr(65 + index)] = text.strip()
        return parsed
    raise ContractError("Unsupported options layout")


def load_episodes(
    name: BenchmarkName,
    root: Path,
    *,
    topics: list[str] | None = None,
    domains: list[str] | None = None,
    question_types: list[str] | None = None,
    limit: int | None = None,
    episode_file: Path | None = None,
) -> list[Episode]:
    if limit is not None and limit < 1:
        raise ContractError("--limit must be positive")
    ids = read_episode_ids(episode_file)
    found_ids: set[str] = set()
    seen: set[str] = set()

    def rows() -> Iterator[Episode]:
        if name == "evermembench":
            scopes = topics or sorted(
                path.name for path in (root / "dataset").iterdir() if path.is_dir()
            )
            for topic in scopes:
                folder = safe_path(root, f"dataset/{topic}")
                qa = folder / f"qa_{topic}.json"
                if not qa.is_file():
                    qa = folder / "qa.json"
                for row in _qa_rows(qa):
                    row_id = str(row.get("id", row.get("question_id", "")))
                    if not row_id:
                        raise ContractError("Question is missing a stable ID")
                    aliases = {row_id, f"{topic}__{row_id}"}
                    if ids is not None and not aliases.intersection(ids):
                        continue
                    found_ids.update(aliases.intersection(ids or set()))
                    kind = "_".join(row_id.split("_")[:2])
                    if question_types and kind not in question_types:
                        continue
                    yield Episode(
                        benchmark=name,
                        episode_id=row_id,
                        scope=topic,
                        question_type=kind,
                        question=row.get("Q", row.get("question")),
                        gold=row.get("A", row.get("answer")),
                        options=_options(row.get("options")),
                    )
        else:
            if domains and not set(domains).issubset(GROUP_DOMAINS):
                raise ContractError("Unknown GroupMemBench domain")
            if question_types and not set(question_types).issubset(QUESTION_TYPES):
                raise ContractError("Unknown GroupMemBench question type")
            for domain in domains or GROUP_DOMAINS:
                for kind in question_types or QUESTION_TYPES:
                    path = safe_path(root, f"questions/{domain}/{kind}.jsonl")
                    if not path.is_file():
                        if domains or question_types:
                            raise ContractError(
                                f"Missing GroupMemBench question cell: {domain}/{kind}"
                            )
                        continue
                    for line in path.read_text(encoding="utf-8").splitlines():
                        if not line.strip():
                            continue
                        row = json.loads(line)
                        row_id = str(row.get("id", ""))
                        if not row_id:
                            raise ContractError("Question is missing a stable ID")
                        episode_id = f"{domain}__{kind}__{row_id}"
                        aliases = {
                            episode_id,
                            f"{domain}/{kind}/{row_id}",
                            f"{domain}__{row_id}",
                            row_id,
                        }
                        if ids is not None and not aliases.intersection(ids):
                            continue
                        found_ids.update(aliases.intersection(ids or set()))
                        yield Episode(
                            benchmark=name,
                            episode_id=episode_id,
                            scope=domain,
                            question_type=kind,
                            question=row["question"],
                            gold=row["answer"],
                            asking_user_id=str(row.get("asking_user_id") or ""),
                        )

    # Resolve all IDs before limiting, so a bad split never silently becomes a different sample.
    selected = list(rows())
    if ids is not None and ids - found_ids:
        raise ContractError("Episode file contains IDs outside the selected benchmark slices")
    for episode in selected:
        if episode.episode_id in seen:
            raise ContractError("Duplicate benchmark episode ID")
        seen.add(episode.episode_id)
    selected = list(islice(selected, limit)) if limit is not None else selected
    if not selected:
        raise ContractError("Benchmark selection is empty")
    return selected


def _timestamp(value: Any, assumed_timezone: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ContractError("Source message is missing a timestamp")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        result = result.replace(tzinfo=ZoneInfo(assumed_timezone))
    return result


def load_corpus(
    name: BenchmarkName, root: Path, scope: str, assumed_timezone: str = "UTC"
) -> Corpus:
    try:
        ZoneInfo(assumed_timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ContractError("--timezone must be a valid IANA timezone") from exc
    relative = (
        f"dataset/{scope}/dialogue.json"
        if name == "evermembench"
        else f"data/final/{scope}/synthetic_domain_channels_rolevariants_{scope}.json"
    )
    source = safe_path(root, relative)
    payload = json.loads(source.read_text(encoding="utf-8"))
    messages: list[Message] = []
    # EverMemBench nests messages by date and group; GroupMemBench nests by channel.
    scopes = payload.get("dialogues", payload) if name == "evermembench" else {scope: payload}
    if not isinstance(scopes, dict):
        raise ContractError("Corpus must be a scope/group/message mapping")
    for date_or_domain, groups in scopes.items():
        if groups is None:
            continue
        if not isinstance(groups, dict):
            raise ContractError("Source scope must contain a group mapping")
        for group, rows in groups.items():
            if rows is None:
                continue
            if not isinstance(rows, list):
                raise ContractError("Source group must contain a message list")
            group_id = f"{name}:{quote(scope, safe='')}:{quote(group, safe='')}"
            for ordinal, row in enumerate(rows, 1):
                if not isinstance(row, dict):
                    raise ContractError("Source message must be an object")
                if name == "evermembench":
                    index = row.get("message_index", ordinal)
                    message_id = f"{quote(date_or_domain, safe='')}:{quote(str(index), safe='')}"
                    text, sender, stamp = row["dialogue"], row["speaker"], row["time"]
                    reply_to = None
                    metadata = {
                        "group": group,
                        "topic": scope,
                        "date": date_or_domain,
                        "message_index": str(index),
                        "source_timestamp": stamp,
                    }
                else:
                    message_id = str(row["msg_node"])
                    text, sender, stamp = row["content"], row["author"], row["timestamp"]
                    reply_to = row.get("reply_to")
                    # Preserve domain-specific fields as searchable metadata.
                    metadata = {
                        k: v if isinstance(v, str) else dumps(v)
                        for k, v in row.items()
                        if k not in {"msg_node", "content", "author", "timestamp", "reply_to"}
                    }
                    metadata.update({"group": group, "domain": scope, "source_timestamp": stamp})
                if datetime.fromisoformat(stamp.replace("Z", "+00:00")).tzinfo is None:
                    metadata["assumed_timezone"] = assumed_timezone
                messages.append(
                    Message(
                        group_id=group_id,
                        message_id=message_id,
                        sender=sender,
                        timestamp=_timestamp(stamp, assumed_timezone),
                        text=text,
                        reply_to=reply_to,
                        metadata=metadata,
                    )
                )
    return Corpus(messages, source)
