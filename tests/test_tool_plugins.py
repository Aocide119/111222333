"""Executable bundle behavior and host-owned evidence boundary."""

from __future__ import annotations

import time

import pytest
import yaml

from evog.core.errors import ContractError, DeadlineExceeded
from evog.harness.schema import Harness
from evog.harness.tools import Tools, tool_definitions


def patched_tools(store, **patch):
    contents = dict(store.harness().contents)
    contents.update(patch)
    return Tools(store, Harness(contents), ["demo-team"])


def test_candidate_tool_implementation_changes_executed_behavior(store):
    original = patched_tools(store).execute(
        "grep_search", {"target": "memory_units", "query": "User_1"}
    )
    assert original["total_matches"] > 0
    tools = patched_tools(
        store,
        **{
            "tools/implementations/grep_search.py": 'def execute(payload, ctx):\n    return {"matches": [], "total_matches": 0, "candidate_behavior": True}\n'
        },
    )
    result = tools.execute("grep_search", {"query": "User_1"})
    assert {key: result[key] for key in ("matches", "total_matches", "candidate_behavior")} == {
        "matches": [],
        "total_matches": 0,
        "candidate_behavior": True,
    }
    assert tools.delivered_refs == set()


def test_candidate_description_is_authoritative_and_extensible(store):
    contents = dict(store.harness().contents)
    path = "tools/descriptions/list_files.yaml"
    descriptor = yaml.safe_load(contents[path])
    descriptor["description"] = "Return a caller-provided test value."
    descriptor["parameters"] = {
        "type": "object",
        "properties": {"marker": {"type": "string"}},
        "required": ["marker"],
        "additionalProperties": False,
    }
    contents[path] = yaml.safe_dump(descriptor)
    contents["tools/implementations/list_files.py"] = (
        'def execute(payload, ctx):\n    return {"marker": payload["marker"]}\n'
    )
    harness = Harness(contents)
    assert tool_definitions(harness)[0]["function"]["description"] == descriptor["description"]
    tools = Tools(store, harness, ["demo-team"])
    assert tools.execute("list_files", {"marker": "edited"}) == {"marker": "edited"}
    with pytest.raises(ContractError):
        tools.execute("list_files", {"marker": 1})


@pytest.mark.parametrize(
    "implementation",
    [
        'def execute(payload, ctx):\n    return {"ref": "demo-team/001", "text": "fabricated content"}\n',
        'def execute(payload, ctx):\n    path = ctx.call("files", {})[0]\n    ctx.call("read_document", {"path": path})\n    return {"ref": "demo-team/001"}\n',
    ],
)
def test_ref_or_private_capability_read_does_not_grant_delivery(store, implementation):
    tools = patched_tools(store, **{"tools/implementations/grep_search.py": implementation})
    tools.execute("grep_search", {"query": "User_1"})
    assert not tools.delivered_refs


def test_candidate_full_original_record_grants_delivery(store):
    implementation = """def execute(payload, ctx):
    path = next(path for path in ctx.call("files", {}) if path.startswith("memory_units/"))
    document = ctx.call("read_document", {"path": path})
    return {"original_record": document["records"][0]}
"""
    tools = patched_tools(store, **{"tools/implementations/grep_search.py": implementation})
    tools.execute("grep_search", {"query": "User_1"})
    assert tools.delivered_refs == {"demo-team/001"}


def test_plugin_cannot_expand_registry_read_scope(store):
    contents = dict(store.harness().contents)
    registry = yaml.safe_load(contents["tools/registry.yaml"])
    search = next(row for row in registry["tools"] if row["name"] == "grep_search")
    search["readable_roots"] = ["memory_store"]
    contents["tools/registry.yaml"] = yaml.safe_dump(registry)
    contents["tools/implementations/grep_search.py"] = (
        'def execute(payload, ctx):\n    return {"record": ctx.call("read_document", {"path": "memory_units/hidden.jsonl"})}\n'
    )
    tools = Tools(store, Harness(contents), ["demo-team"])
    with pytest.raises(ContractError, match="scope"):
        tools.execute("grep_search", {"query": "User_1"})
    assert not tools.delivered_refs


def test_workspace_contains_editable_components_and_memory_policy(store):
    tools = patched_tools(store)
    listed = tools.execute(
        "list_files", {"target": "workspace", "resource_path": "memory", "max_depth": 3}
    )
    assert any(row["resource_path"] == "memory/policy.md" for row in listed["files"])
    result = tools.execute(
        "read_file", {"target": "workspace", "resource_path": "memory/policy.md"}
    )
    assert result["lines"]


def test_redacted_middleware_result_cannot_grant_delivery(store):
    tools = Tools(
        store,
        store.harness(),
        ["demo-team"],
        output_transform=lambda result: {"ref": "demo-team/001", "redacted": True},
    )
    result = tools.execute("grep_search", {"query": "User_1"})
    assert result["redacted"]
    assert not tools.delivered_refs


def test_deferred_tool_evidence_granted_only_after_wire_confirmation(store):
    tools = Tools(store, store.harness(), ["demo-team"], defer_delivery=True)
    result = tools.execute("grep_search", {"query": "User_1"})
    assert not tools.delivered_refs
    tools.confirm_delivery({"redacted": True})
    assert not tools.delivered_refs
    tools.confirm_delivery(result)
    assert tools.delivered_refs == {"demo-team/001"}
    tools.confirm_delivery({"redacted": True})
    assert tools.delivered_refs == {"demo-team/001"}


@pytest.mark.parametrize("value", [None, "abc"])
def test_edited_tool_schema_supports_local_refs_composition_and_nullable_types(store, value):
    contents = dict(store.harness().contents)
    path = "tools/descriptions/custom_probe.yaml"
    descriptor = yaml.safe_load(contents["tools/descriptions/list_files.yaml"])
    descriptor["name"] = "custom_probe"
    descriptor["parameters"] = {
        "type": "object",
        "properties": {
            "value": {"anyOf": [{"$ref": "#/$defs/Nonempty"}, {"type": "null"}]},
            "items": {"type": "array", "items": {"type": ["integer", "null"]}, "maxItems": 2},
        },
        "required": ["value", "items"],
        "additionalProperties": False,
        "$defs": {"Nonempty": {"type": "string", "minLength": 3}},
    }
    contents[path] = yaml.safe_dump(descriptor)
    contents["tools/implementations/custom_probe.py"] = (
        'def execute(payload, ctx):\n    return {"value": payload["value"]}\n'
    )
    registry = yaml.safe_load(contents["tools/registry.yaml"])
    registry["tools"].append(
        {
            "name": "custom_probe",
            "description_file": path,
            "handler": "python:tools/implementations/custom_probe.py:execute",
            "enabled": True,
            "readable_roots": [],
            "writable_roots": [],
        }
    )
    contents["tools/registry.yaml"] = yaml.safe_dump(registry)
    tools = Tools(store, Harness(contents), ["demo-team"])
    assert tools.execute("custom_probe", {"value": value, "items": [1, None]}) == {"value": value}
    for invalid in [
        {"value": 123, "items": []},
        {"value": "a", "items": []},
        {"value": "abc", "items": [True]},
        {"value": None, "items": [1, 2, 3]},
        {"value": None, "items": [], "extra": True},
    ]:
        with pytest.raises(ContractError):
            tools.execute("custom_probe", invalid)


def test_line_range_reader_starts_at_requested_offset_and_stops_after_one_page(store):
    tools = Tools(store, store.harness(), ["demo-team"])
    calls = []
    original = tools._capability

    def tracked(entry, operations, name, arguments):
        if name == "read_document":
            calls.append(arguments)
        return original(entry, operations, name, arguments)

    tools._capability = tracked
    path = next(iter(tools.context))
    result = tools.execute(
        "read_file",
        {
            "target": "memory_units",
            "resource_path": path.removeprefix("memory_units/"),
            "start_line": 2,
            "end_line": 2,
        },
    )
    assert calls == [{"path": path, "offset": 1}]
    assert result["lines"][0]["ref"] == "demo-team/002"


def test_tool_worker_obeys_remaining_interaction_deadline(store):
    contents = dict(store.harness().contents)
    contents["tools/implementations/grep_search.py"] = (
        "import time\ndef execute(payload, ctx):\n    time.sleep(3)\n    return {}\n"
    )
    started = time.monotonic()
    tools = Tools(store, Harness(contents), ["demo-team"], component_deadline=started + 0.2)
    with pytest.raises(DeadlineExceeded):
        tools.execute("grep_search", {"query": "release"})
    assert time.monotonic() - started < 1
    assert not tools.delivered_refs


def test_expired_deadline_does_not_start_tool_worker(store, monkeypatch):
    tools = Tools(store, store.harness(), ["demo-team"], component_deadline=time.monotonic() - 1)

    def fail(*args, **kwargs):
        raise AssertionError("Expired tools must not start a worker")

    monkeypatch.setattr("evog.harness.executor.execute_component", fail)
    with pytest.raises(DeadlineExceeded):
        tools.execute("grep_search", {"query": "release"})
