"""Editable grep_search tool algorithm."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from tools.implementations.search_helpers import (
    contains,
    documents,
    dumps,
    logical_path,
    searchable_fields,
)


def execute(payload, ctx):
    args = SimpleNamespace(**payload)
    selected = args.selected_patterns or args.patterns
    if not selected and not args.required_pattern_groups and args.query:
        selected = [args.query]
    terms = [term.casefold().strip() for term in selected]
    groups = [[term.casefold().strip() for term in group] for group in args.required_pattern_groups]
    optional = [term.casefold().strip() for term in args.optional_patterns]
    if any(
        not term or len(term) > 256 for term in [*terms, *optional, *(t for g in groups for t in g)]
    ):
        raise ValueError("Search terms must contain 1–256 characters")
    if not terms and not groups:
        raise ValueError("Search needs a query, selected pattern or required group")
    matches = []
    operations = SimpleNamespace(**ctx.call("operations", {}))

    search_path = logical_path(args.target, args.resource_path)
    for path in ctx.call("files", {}):
        if not path.startswith(search_path):
            continue
        # Select one spelling of each source and working-memory path for broad searches.
        if args.target != "memory_units" and path.startswith("memory_units/"):
            continue
        # The ledger is runtime-owned metadata, not conversation evidence;
        # excluding it also prevents a broad search from feeding previous
        # search terms back into later searches.
        if path.endswith(("search_ledger.jsonl", "state.json", "evidence_links.jsonl")):
            continue
        for document in documents(ctx, path):
            for local_index, line in enumerate(document["lines"]):
                index = document["offset"] + local_index
                source_path = path
                record = document["records"][local_index] if document["records"] else None
                fields = searchable_fields(record, line, operations.search_fields)
                hits = [
                    any(contains(term, field, operations.match_mode) for field in fields)
                    for term in terms
                ]
                base_match = not terms or (
                    all(hits) if operations.search_mode == "all" else any(hits)
                )
                required_match = all(
                    any(
                        contains(term, field, operations.match_mode)
                        for term in group
                        for field in fields
                    )
                    for group in groups
                )
                if base_match and required_match:
                    rank = sum(
                        any(contains(term, field, operations.match_mode) for field in fields)
                        for term in optional
                    )
                    matches.append((path, index, line, rank))
    matches.sort(key=lambda match: (-match[3], match[0], match[1]))
    selected = matches[args.offset : args.offset + operations.search_limit]
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
        if (record := ctx.call("source_record", {"path": source_path, "index": index})) is not None:
            row["ref"] = record["ref"]
            row["group_id"] = record["group_id"]
            # Only complete delivered source records qualify for citations.
            if len(line) <= operations.excerpt_chars:
                row_refs.add(record["ref"])
            window = operations.context_window
            if window:
                neighborhood = ctx.call(
                    "source_context", {"path": source_path, "index": index, "window": window}
                )
                row["context"] = [
                    {
                        "ref": item["ref"],
                        "text": item["source_line"][: operations.excerpt_chars],
                        "truncated": len(item["source_line"]) > operations.excerpt_chars,
                    }
                    for item in neighborhood
                ]
                row_refs.update(
                    item["ref"]
                    for item in neighborhood
                    if len(item["source_line"]) <= operations.excerpt_chars
                )
        size = len(dumps(row))
        if response_chars + size > 60000:
            break
        rows.append(row)
        returned_refs.update(row_refs)
        response_chars += size
    next_offset = args.offset + len(rows)
    truncated = next_offset < len(matches)
    return {
        "matches": rows,
        "total_matches": len(matches),
        "mode": operations.search_mode,
        "truncated": truncated,
        "next_offset": next_offset if truncated else None,
    }
