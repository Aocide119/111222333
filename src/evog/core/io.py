"""Canonical JSON and bounded filesystem writes."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from evog.core.errors import ContractError


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint(value: Any) -> str:
    return hashlib.sha256(dumps(value).encode()).hexdigest()[:24]


def safe_path(root: Path, relative: str) -> Path:
    if not relative or "\\" in relative:
        raise ContractError("A nonempty relative path is required")
    name = Path(relative)
    if name.is_absolute() or ".." in name.parts:
        raise ContractError("Path must stay inside its logical root")
    root = root.resolve()
    result = (root / name).resolve()
    if result == root or root not in result.parents:
        raise ContractError("Path escapes its logical root")
    return result


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".evog-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
