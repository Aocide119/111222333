"""Deterministic stratified cohorts without publishing questions or reference answers."""

from __future__ import annotations

import hashlib
import json
import os
import random
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from evog.core.errors import ContractError
from evog.core.io import dumps
from evog.evaluation.data import Episode

SCHEMA = "evog.stratified_split"
VERSION = 1
ALGORITHM = "largest-remainder-python-random-v1"


def _hash(value: Any) -> str:
    return hashlib.sha256(dumps(value).encode("utf-8")).hexdigest()


def create_manifest(
    episodes: Sequence[Episode], *, evolution_size: int = 720, seed: int = 0
) -> dict[str, Any]:
    """Allocate by type with largest remainders; ties use lexicographic type order.

    IDs and types are sorted before the seeded shuffle. The source digest includes every
    Episode field, including private question/gold content, but only digests and selection
    metadata are exported. This creates a new cohort, not a reconstruction of a historical one.
    """
    if type(seed) is not int or type(evolution_size) is not int:
        raise ContractError("Split size and seed must be integers")
    if not episodes or not 1 <= evolution_size <= len(episodes):
        raise ContractError("Evolution size must be between one and the source question count")
    ordered = sorted(episodes, key=lambda episode: episode.episode_id)
    ids = [episode.episode_id for episode in ordered]
    if len(ids) != len(set(ids)):
        raise ContractError("Split source has duplicate episode IDs")
    if any(
        value != value.strip() or value.startswith("#") or "\n" in value or "\r" in value
        for value in ids
    ):
        raise ContractError("Split IDs must round-trip through episode-ID text files")
    if len({episode.benchmark for episode in ordered}) != 1:
        raise ContractError("A split must contain exactly one benchmark")
    groups: dict[str, list[str]] = defaultdict(list)
    for episode in ordered:
        if not episode.question_type.strip():
            raise ContractError("Every split episode needs a question type")
        groups[episode.question_type].append(episode.episode_id)
    total = len(ordered)
    allocations = {kind: len(values) * evolution_size // total for kind, values in groups.items()}
    remainder_order = sorted(
        groups, key=lambda kind: (-(len(groups[kind]) * evolution_size % total), kind)
    )
    for kind in remainder_order[: evolution_size - sum(allocations.values())]:
        allocations[kind] += 1
    generator = random.Random(seed)
    selected: set[str] = set()
    for kind in sorted(groups):
        shuffled = groups[kind].copy()
        generator.shuffle(shuffled)
        selected.update(shuffled[: allocations[kind]])
    manifest = {
        "schema": SCHEMA,
        "version": VERSION,
        "algorithm": ALGORITHM,
        "benchmark": ordered[0].benchmark,
        "source_fingerprint": _hash([episode.model_dump(mode="json") for episode in ordered]),
        "split_seed": seed,
        "counts": {
            "total": total,
            "evolution": evolution_size,
            "held_out": total - evolution_size,
            "by_type": {
                kind: {
                    "total": len(groups[kind]),
                    "evolution": allocations[kind],
                    "held_out": len(groups[kind]) - allocations[kind],
                }
                for kind in sorted(groups)
            },
        },
        "episodes": [
            {
                "episode_id": episode.episode_id,
                "question_type": episode.question_type,
                "scope": episode.scope,
                "episode_fingerprint": _hash(episode.model_dump(mode="json")),
            }
            for episode in ordered
        ],
        "evolution_ids": [value for value in ids if value in selected],
        "held_out_ids": [value for value in ids if value not in selected],
    }
    manifest["manifest_fingerprint"] = _hash(manifest)
    return manifest


def validate_manifest(manifest: dict[str, Any], episodes: Sequence[Episode]) -> None:
    """Rebuild all metadata and assignment; a recomputed digest cannot legitimize edits."""
    if not isinstance(manifest, dict):
        raise ContractError("Split manifest must be an object")
    try:
        expected = create_manifest(
            episodes,
            evolution_size=manifest["counts"]["evolution"],
            seed=manifest["split_seed"],
        )
    except (KeyError, TypeError) as error:
        raise ContractError("Split manifest has invalid or missing metadata") from error
    # Canonical JSON also distinguishes true from 1; Python dictionary equality does not.
    try:
        matches = dumps(manifest) == dumps(expected)
    except (TypeError, ValueError) as error:
        raise ContractError("Split manifest is not valid JSON data") from error
    if not matches:
        raise ContractError("Split manifest does not match the source and declared algorithm")


def write_manifest(
    manifest: dict[str, Any], output_dir: Path, episodes: Sequence[Episode]
) -> dict[str, Path]:
    """Validate then exclusively create the manifest and both reusable ID lists."""
    validate_manifest(manifest, episodes)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    contents = {
        "manifest.json": json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        "evolution.txt": "".join(f"{value}\n" for value in manifest["evolution_ids"]),
        "held_out.txt": "".join(f"{value}\n" for value in manifest["held_out_ids"]),
    }
    paths = {name: output_dir / name for name in contents}
    if any(path.exists() or path.is_symlink() for path in paths.values()):
        raise ContractError("Split output already exists; choose a new directory")
    created: list[Path] = []
    try:
        for name, content in contents.items():
            with paths[name].open("x", encoding="utf-8") as stream:
                created.append(paths[name])
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
    except OSError as error:
        for path in created:
            path.unlink(missing_ok=True)
        raise ContractError("Could not create split outputs without overwriting files") from error
    return paths
