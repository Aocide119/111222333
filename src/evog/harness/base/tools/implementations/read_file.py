"""Editable bounded line-range reader over authorized document pages."""

from types import SimpleNamespace

from tools.implementations.search_helpers import logical_path


def execute(payload, ctx):
    args = SimpleNamespace(**payload)
    path = logical_path(args.target, args.resource_path)
    start = args.start_line - 1
    requested_end = args.end_line or (start + ctx.call("operations", {})["read_limit"])
    selected, size = [], 0
    offset = start
    total = None
    while True:
        page = ctx.call("read_document", {"path": path, "offset": offset})
        total = page["total_lines"]
        if start >= total and total:
            raise ValueError("start_line exceeds file length")
        for local_index, text in enumerate(page["lines"]):
            index = page["offset"] + local_index
            if index >= requested_end or (selected and size + len(text) > 60000):
                break
            row = {"line": index + 1, "text": text}
            if page["records"]:
                row["ref"] = page["records"][local_index]["ref"]
            selected.append(row)
            size += len(text)
        end = start + len(selected)
        if end >= requested_end or page["next_offset"] is None or end < page["next_offset"]:
            break
        offset = page["next_offset"]
    truncated = end < total
    return {
        "path": path,
        "target": args.target,
        "resource_path": args.resource_path,
        "lines": selected,
        "total_lines": total,
        "truncated": truncated,
        "next_start_line": end + 1 if truncated else None,
    }
