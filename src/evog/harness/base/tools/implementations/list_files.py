"""Editable list_files tool algorithm."""

from __future__ import annotations

from types import SimpleNamespace

from tools.implementations.search_helpers import logical_path


def execute(payload, ctx):
    args = SimpleNamespace(**payload)
    prefix = logical_path(args.target, args.resource_path)
    names = [name for name in ctx.call("files", {}) if name.startswith(prefix)]
    if args.target == "workspace":
        names = sorted(
            [
                *names,
                *(
                    name
                    for name in ctx.call("archive_paths", {})
                    if name.startswith(args.resource_path)
                ),
            ]
        )
    if args.target == "memory_units":
        names = [name for name in names if name.startswith("memory_units/")]
    names = [
        name
        for name in names
        if len(name.removeprefix(prefix).strip("/").split("/")) <= args.max_depth
    ]
    start = args.offset
    rows = [
        {
            "path": name,
            "target": args.target,
            "resource_path": name.removeprefix(args.target + "/"),
            **(
                {"group_id": ctx.call("group_paths", {})[name]}
                if args.target == "memory_units" and name in ctx.call("group_paths", {})
                else {}
            ),
        }
        for name in names[start : start + args.limit]
    ]
    next_offset = start + len(rows)
    truncated = next_offset < len(names)
    return {
        "files": rows,
        "total": len(names),
        "truncated": truncated,
        "next_offset": next_offset if truncated else None,
    }
