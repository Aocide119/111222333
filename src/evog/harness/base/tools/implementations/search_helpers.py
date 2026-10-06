"""Reusable candidate-side serialization and logical path helpers."""

from __future__ import annotations

import json


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def logical_path(target, resource_path):
    if target == "workspace" and resource_path.startswith("tool_results/"):
        return resource_path
    if target not in {"workspace", "memory_units", "memory_store", "skills"}:
        raise ValueError("Unknown logical resource root")
    return target + "/" + resource_path if resource_path else target + "/"


def documents(ctx, path):
    offset = 0
    while True:
        page = ctx.call("read_document", {"path": path, "offset": offset})
        yield page
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]


def contains(term, field, match_mode):
    import re

    return (
        term in field
        if match_mode == "literal"
        else re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", field) is not None
    )


def searchable_fields(record, line, field_names):
    if record is None:
        return [line.casefold()]
    fields = [
        str(record[key]).casefold()
        for key in field_names
        if key in record and key not in {"metadata", "reply_to"}
    ]
    if "reply_to" in field_names and record.get("reply_to") is not None:
        fields.append(record["reply_to"].casefold())
    if "metadata" in field_names and "metadata" in record:
        fields.extend(value.casefold() for value in record["metadata"].values())
    return fields


def note_arguments(payload):
    arguments = dict(payload)
    return arguments
