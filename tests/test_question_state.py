import json

import pytest
import yaml
from conftest import ScriptedProvider

from evog.agents.interaction import interact
from evog.core.errors import ContractError
from evog.core.io import dumps
from evog.core.providers import ModelReply, ToolCall
from evog.harness.schema import Harness
from evog.harness.tools import Tools, tool_definitions


def tools_for(store, session="q1", **kwargs):
    return Tools(
        store, store.harness(), ["demo-team"], memory_session=session, question=session, **kwargs
    )


def resolve(identity, refs=None, searches=None, status="satisfied"):
    return {
        "id": identity,
        "status": status,
        "evidence_refs": refs or [],
        "search_ids": searches or [],
        "reason": "The recorded observations establish the bridge fact.",
    }


def test_memory_routes_and_ledgers_are_readable_and_protected(store):
    tools = tools_for(store)
    expected = {
        "README.md",
        "working_memory/README.md",
        "long_term_memory/README.md",
        "long_term_memory/people/README.md",
        "long_term_memory/events/README.md",
        "working_memory/state.json",
        "working_memory/evidence_links.jsonl",
    }
    assert {"memory_store/" + name for name in expected}.issubset(tools.files())
    for path in expected:
        tools.execute("read_file", {"target": "memory_store", "resource_path": path})
        for name in ("write_file", "create_file"):
            with pytest.raises(ContractError, match="read-only"):
                tools.execute(name, {"resource_path": path, "content": "overwrite"})
    with pytest.raises(ContractError, match="read-only"):
        tools.execute(
            "write_file", {"resource_path": "working_memory/./state.json", "content": "x"}
        )
    tools.execute(
        "write_file", {"resource_path": "working_memory/notes.md", "content": "Navigation"}
    )
    assert tools._lines("memory_store/working_memory/notes.md") == ["Navigation"]


def test_state_schema_is_exposed_by_all_five_tools():
    definitions = tool_definitions()
    assert len(definitions) == 5
    assert all("state_update" in row["function"]["parameters"]["properties"] for row in definitions)
    search = next(
        row["function"] for row in definitions if row["function"]["name"] == "grep_search"
    )
    assert {"obligation_id", "parent_id"}.issubset(search["parameters"]["properties"])


def test_dependencies_require_delivered_bridge_evidence(store):
    tools = tools_for(store)
    declarations = [
        {"id": "bridge", "description": "Identify the release task"},
        {"id": "endpoint", "description": "Find the task's latest date", "parent_id": "bridge"},
    ]
    tools.execute("list_files", {"state_update": {"obligations": declarations}})
    with pytest.raises(ContractError, match="parent obligation"):
        tools.execute("grep_search", {"query": "changed", "obligation_id": "endpoint"})
    assert tools.state.data["searches"] == {}
    search = tools.execute("grep_search", {"query": "release", "obligation_id": "bridge"})
    assert search["search_id"] == "search-1"
    with pytest.raises(ContractError, match="delivered"):
        tools.execute(
            "list_files", {"state_update": {"resolutions": [resolve("bridge", ["invented/ref"])]}}
        )
    tools.execute(
        "grep_search",
        {
            "query": "changed",
            "obligation_id": "endpoint",
            "parent_id": "bridge",
            "state_update": {
                "resolutions": [resolve("bridge", ["demo-team/001"])],
                "anchors": ["release"],
            },
        },
    )
    assert tools.state.open_ids == ["endpoint"]
    assert tools.state.data["anchors"] == ["release"]
    tools.execute(
        "list_files", {"state_update": {"resolutions": [resolve("endpoint", ["demo-team/002"])]}}
    )
    assert tools.state.open_ids == []
    assert tools_for(store, "q2").state.data["obligations"] == {}


def test_waiver_requires_a_real_search_for_that_obligation(store):
    tools = tools_for(store)
    first = tools.execute("grep_search", {"query": "absent anchor", "obligation_id": "missing"})
    tools.execute("grep_search", {"query": "another absent anchor", "obligation_id": "other"})
    for searches in (["made-up"], ["search-2"], []):
        with pytest.raises(ContractError):
            tools.execute(
                "list_files",
                {
                    "state_update": {
                        "resolutions": [resolve("missing", searches=searches, status="waived")],
                    }
                },
            )
    tools.execute(
        "list_files",
        {
            "state_update": {
                "resolutions": [resolve("missing", searches=[first["search_id"]], status="waived")],
            }
        },
    )
    assert tools.state.data["obligations"]["missing"]["status"] == "waived"
    with pytest.raises(ContractError, match="rewritten"):
        tools.execute(
            "list_files",
            {
                "state_update": {
                    "resolutions": [
                        {
                            **resolve("missing", searches=[first["search_id"]], status="waived"),
                            "reason": "Rewrite history",
                        }
                    ],
                }
            },
        )


@pytest.mark.parametrize(
    "declarations",
    [
        [{"id": "child", "description": "unknown parent", "parent_id": "missing"}],
        [
            {"id": "a", "description": "A", "parent_id": "b"},
            {"id": "b", "description": "B", "parent_id": "a"},
        ],
    ],
)
def test_invalid_dependency_graph_is_atomic(store, declarations):
    tools = tools_for(store)
    before = tools.state.path.read_bytes()
    with pytest.raises(ContractError, match="acyclic"):
        tools.execute("list_files", {"state_update": {"obligations": declarations}})
    assert tools.state.path.read_bytes() == before


def test_truncated_and_deferred_results_do_not_grant_state_evidence(store):
    tools = tools_for(store, defer_delivery=True)
    result = tools.execute("grep_search", {"query": "release", "obligation_id": "task"})
    assert tools.delivered_refs == set()
    assert tools.state.evidence_path.read_text() == ""
    with pytest.raises(ContractError, match="delivered"):
        tools.execute(
            "list_files", {"state_update": {"resolutions": [resolve("task", ["demo-team/001"])]}}
        )
    tools.confirm_delivery({"matches": [{"ref": "demo-team/001", "excerpt": "cut"}]})
    assert tools.state.evidence_path.read_text() == ""
    tools.confirm_delivery(result)
    evidence = [json.loads(line) for line in tools.state.evidence_path.read_text().splitlines()]
    assert {row["ref"] for row in evidence} == {"demo-team/001", "demo-team/002"}
    assert all(
        row["path"].startswith("memory_units/") and row["line"] in (1, 2) for row in evidence
    )
    tools.execute(
        "list_files", {"state_update": {"resolutions": [resolve("task", ["demo-team/001"])]}}
    )


def test_short_excerpts_require_a_complete_source_read(store):
    message = store.messages("demo-team")[0].model_copy(
        update={"message_id": "003", "text": "long-anchor " + "x" * 1000}
    )
    store.ingest(iter([message]))
    contents = dict(store.harness().contents)
    contents["tools/operations.json"] = dumps({"excerpt_chars": 256})
    tools = Tools(store, Harness(contents), ["demo-team"], memory_session="short")
    search = tools.execute("grep_search", {"query": "long-anchor"})
    assert all(row["excerpt_truncated"] for row in search["matches"])
    assert tools.delivered_refs == set() and tools.state.evidence_path.read_text() == ""
    tools.execute(
        "read_file",
        {
            "target": "memory_units",
            "resource_path": search["matches"][0]["resource_path"],
            "start_line": search["matches"][0]["line"],
            "end_line": search["matches"][0]["line"],
        },
    )
    assert tools.delivered_refs == {"demo-team/003"}


def test_context_compaction_and_tool_hooks_preserve_obligation_dependencies(store, settings):
    contents = dict(store.harness().contents)
    contents["middleware/context_compaction.py"] = (
        "def execute(payload, ctx): return {'messages': payload['messages'][:2]}"
    )
    contents["middleware/before_tool.py"] = (
        "def execute(payload, ctx): return {'arguments': {'query': 'release', 'target': 'memory_units'}}"
    )
    registry = yaml.safe_load(contents["middleware/registry.yaml"])
    registry["middleware"].append(
        {
            "name": "before_tool",
            "handler": "python:middleware/before_tool.py:execute",
            "hook": "before_tool",
            "enabled": True,
            "config": {},
        }
    )
    contents["middleware/registry.yaml"] = yaml.safe_dump(registry)
    provider = ScriptedProvider(
        ModelReply(
            tool_calls=[
                ToolCall(
                    id="parent",
                    name="grep_search",
                    arguments={
                        "query": "release",
                        "obligation_id": "parent",
                        "state_update": {
                            "obligations": [
                                {"id": "parent", "description": "Establish the bridge"},
                                {
                                    "id": "child",
                                    "description": "Resolve the next hop",
                                    "parent_id": "parent",
                                },
                            ],
                            "anchors": ["release"],
                        },
                    },
                )
            ]
        ),
        ModelReply(
            tool_calls=[
                ToolCall(
                    id="child",
                    name="grep_search",
                    arguments={
                        "query": "changed",
                        "obligation_id": "child",
                        "parent_id": "parent",
                    },
                )
            ]
        ),
        ModelReply(
            content='{"text":"Missing bridge evidence.","confidence":0.2,"status":"insufficient","citations":[]}'
        ),
        ModelReply(
            content='{"searched":["release"],"evidence_found":"none delivered","unresolved":["bridge"],"limitation":"retrieval"}'
        ),
    )
    answer = interact(
        store, provider, settings, "Latest release?", ["demo-team"], Harness(contents)
    )
    current = json.loads(provider.calls[1][0][1]["content"])
    assert current["question"] == "Latest release?" and current["question_state"]["anchors"] == [
        "release"
    ]
    assert {item["id"] for item in current["question_state"]["obligations"]} == {"parent", "child"}
    events = [event for event in store.events(answer.run_id) if event.kind == "tool"]
    assert events[1].data["result"]["ok"] is False
    assert events[1].data["effective_arguments"]["obligation_id"] == "child"
