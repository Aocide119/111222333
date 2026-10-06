"""Scoped, read-only archives of tool outputs omitted from model context."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from evog.core.io import atomic_write, dumps, safe_path


class ToolResults:
    def __init__(self, root: Path, max_chars: int):
        self.root = root
        # Leave space for the runtime's success flag in the serialized response.
        self.max_chars = max_chars - 32
        self.paths: dict[str, Path] = {}
        self.archives: dict[str, dict[str, Any]] = {}
        self.chunk_owners: dict[str, str] = {}

    def persist(self, result: dict, refs: set[str]) -> dict:
        text = dumps(result)
        prefix = f"tool_results/{len(self.archives) + 1:04d}"
        full_path = prefix + "/full.txt"
        chunk_size = max(self.max_chars // 4, 1)
        chunks = [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)]
        chunk_paths = [prefix + f"/chunk_{i:04d}.txt" for i in range(1, len(chunks) + 1)]
        for path, content in zip([full_path, *chunk_paths], [text, *chunks], strict=True):
            actual = safe_path(self.root, path.removeprefix("tool_results/"))
            atomic_write(actual, content)
            self.paths[path] = actual
        self.archives[full_path] = {"refs": set(refs), "chunks": set(chunk_paths), "read": set()}
        self.chunk_owners.update(dict.fromkeys(chunk_paths, full_path))
        preview = {
            "truncated": True,
            "total_chars": len(text),
            "full_result_path": full_path,
            "first_chunk": chunk_paths[0],
            "last_chunk": chunk_paths[-1],
            "chunk_count": len(chunks),
            "notice": "Use read_file on chunks to recover omitted content. A preview alone "
            "does not establish full source delivery for citations.",
            "preview": "",
        }
        low, high = 0, min(len(text), self.max_chars)
        while low < high:
            middle = (low + high + 1) // 2
            preview["preview"] = text[:middle]
            if len(dumps(preview)) <= self.max_chars:
                low = middle
            else:
                high = middle - 1
        preview["preview"] = text[:low]
        return preview

    def delivered(self, path: str, start: int, end: int, total: int) -> set[str]:
        if start != 0 or end != total:
            return set()
        if path in self.archives:
            return self.archives[path]["refs"]
        owner = self.chunk_owners.get(path)
        if owner:
            archive = self.archives[owner]
            archive["read"].add(path)
            if archive["read"] == archive["chunks"]:
                return archive["refs"]
        return set()
