"""Filesystem snapshots with exact file manifests for component checkpoints."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any

from evog.core.errors import ContractError
from evog.core.io import dumps
from evog.harness.schema import MAX_BUNDLE_BYTES, MAX_FILE_BYTES, MAX_FILES, Harness, component_for

MANIFEST_NAME = ".evog-bundle.json"
MANIFEST_VERSION = 1
MAX_MANIFEST_BYTES = 131072


def _read_text(path: Path, limit: int) -> str:
    """Do not follow a leaf symlink or consume a device/FIFO as bundle text."""
    try:
        descriptor = os.open(
            path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        )
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                raise ContractError(f"Bundle file is not bounded regular text: {path.name}")
            data = stream.read(limit + 1)
            if len(data) > limit:
                raise ContractError(f"Bundle file exceeds byte budget: {path.name}")
        text = data.decode("utf-8")
        if "\x00" in text:
            raise ContractError(f"Bundle contains nontext data: {path.name}")
        return text
    except (OSError, UnicodeDecodeError) as exc:
        raise ContractError(f"Cannot read bundle text file: {path.name}") from exc


def _unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ContractError("Bundle manifest has duplicate fields")
        result[key] = value
    return result


def bundle_manifest(harness: Harness) -> dict[str, Any]:
    """The hash covers every component byte, including executable implementations."""
    return {
        "manifest_version": MANIFEST_VERSION,
        "format_version": harness.format_version,
        "harness_id": harness.id,
        "files": {
            name: {
                "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "bytes": len(text.encode("utf-8")),
            }
            for name, text in sorted(harness.contents.items())
        },
    }


def load_bundle(
    directory: str | Path,
    *,
    require_manifest: bool = False,
    verify_manifest: bool = True,
) -> Harness:
    """Read a candidate or checkpoint without importing its Python implementations.

    A sealed checkpoint requires its manifest. An editable candidate may omit one;
    if a candidate is copied from a checkpoint, its stale manifest must explicitly
    be ignored while editing, then replaced by materializing a new snapshot.
    """
    root = Path(directory).expanduser()
    if root.is_symlink() or not root.is_dir():
        raise ContractError("Bundle root must be an existing directory, not a symlink")
    root = root.resolve()
    contents: dict[str, str] = {}
    manifest_text = None
    total = 0

    def walk_error(exc):
        raise ContractError("Cannot enumerate every bundle directory") from exc

    for current, directories, names in os.walk(root, followlinks=False, onerror=walk_error):
        for name in directories:
            path = Path(current) / name
            if path.is_symlink():
                raise ContractError("Bundle directories cannot be symlinks")
        for name in sorted(names):
            path = Path(current) / name
            if path.is_symlink():
                raise ContractError("Bundle files cannot be symlinks")
            relative = path.relative_to(root).as_posix()
            if relative == MANIFEST_NAME:
                manifest_text = _read_text(path, MAX_MANIFEST_BYTES)
                continue
            component_for(relative)
            if len(contents) >= MAX_FILES:
                raise ContractError("Bundle exceeds file budget")
            text = _read_text(path, max(MAX_FILE_BYTES, 160000))
            total += len(text.encode("utf-8"))
            if total > MAX_BUNDLE_BYTES:
                raise ContractError("Bundle exceeds byte budget")
            contents[relative] = text
    harness = Harness(contents)
    if manifest_text is None:
        if require_manifest:
            raise ContractError("Checkpoint is missing its file manifest")
    elif verify_manifest or require_manifest:
        try:
            manifest = json.loads(manifest_text, object_pairs_hook=_unique_json)
        except (json.JSONDecodeError, RecursionError) as exc:
            raise ContractError("Invalid bundle manifest JSON") from exc
        if dumps(manifest) != dumps(bundle_manifest(harness)):
            raise ContractError("Bundle file manifest does not match its component contents")
    return harness


def verify_bundle(directory: str | Path) -> Harness:
    """Require every component's digest, size, and snapshot ID to match."""
    return load_bundle(directory, require_manifest=True)


def _write_synced(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(text.encode("utf-8"))
        stream.flush()
        os.fsync(stream.fileno())


def materialize_bundle(harness: Harness, destination: str | Path) -> Path:
    """Atomically publish one complete immutable snapshot; never merge old files.

    Repeating an export to the same verified snapshot is idempotent. A nonempty
    destination with another version is rejected, preserving checkpoint history.
    """
    # Revalidate the snapshot in case a caller mutated .contents after construction.
    validated = Harness(dict(harness.contents))
    if validated.id != harness.id:
        raise ContractError("Harness contents changed after validation")
    requested = Path(destination).expanduser()
    if not requested.name or requested.name in {".", ".."}:
        raise ContractError("Bundle destination must name a new snapshot directory")
    if requested.is_symlink():
        raise ContractError("Bundle destination cannot be a symlink")
    parent = requested.absolute().parent.resolve()
    parent.mkdir(parents=True, exist_ok=True)
    target = parent / requested.name
    if target.exists():
        if not target.is_dir():
            raise ContractError("Bundle destination must be a directory")
        if any(target.iterdir()):
            existing = verify_bundle(target)
            if existing.id != validated.id:
                raise ContractError("Cannot overwrite an immutable bundle with another revision")
            return target
    temporary = Path(tempfile.mkdtemp(prefix=".evog-bundle-", dir=parent))
    try:
        for relative, text in sorted(validated.contents.items()):
            _write_synced(temporary / relative, text)
        _write_synced(temporary / MANIFEST_NAME, dumps(bundle_manifest(validated)) + "\n")
        verify_bundle(temporary)
        # fsync nested directories before publishing the root, on hosts that support it.
        for current, _, _ in os.walk(temporary, topdown=False):
            descriptor = os.open(current, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        try:
            os.rename(temporary, target)
        except OSError as exc:
            # A concurrent exporter may have published the same immutable snapshot.
            if (
                target.is_dir()
                and not target.is_symlink()
                and verify_bundle(target).id == validated.id
            ):
                return target
            raise ContractError("Cannot atomically publish bundle destination") from exc
        descriptor = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return target
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
