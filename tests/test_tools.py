import json

import pytest

from evog.agents.demo import DEMO_MESSAGES
from evog.core.errors import ContractError
from evog.core.io import dumps, fingerprint
from evog.core.models import Message
from evog.harness.schema import Harness
from evog.harness.tools import Tools, tool_definitions


def read_args(path, **extra):
    target, _, resource_path = path.partition("/")
    if target == "tool_results":
        target, resource_path = "workspace", path
    return {"target": target, "resource_path": resource_path, **extra}


def tools_for(store, groups=None, **patches):
    contents = store.harness().contents
    contents.update(patches)
    return Tools(store, Harness(contents), groups or ["demo-team"])


def test_five_tools_and_scoped_read_view(store):
    store.ingest(iter([Message.model_validate({**DEMO_MESSAGES[0], "group_id": "private"})]))
    tools = tools_for(store)
    assert len(tool_definitions()) == 5
    files = tools.execute("list_files", {"target": "memory_units"})["files"]
    assert len(files) == 1
    assert files[0]["group_id"] == "demo-team"
    result = tools.execute("read_file", read_args(files[0]["path"], end_line=1))
    assert result["truncated"] and result["next_start_line"] == 2
    assert tools.delivered_refs == {"demo-team/001"}
    with pytest.raises(ContractError):
        Tools(store, store.harness(), [])
    with pytest.raises(ContractError):
        Tools(store, store.harness(), ["unknown"])


def test_operation_can_register_a_declarative_query_tool(store):
    operations = json.dumps(
        {
            "query_tools": [
                {
                    "name": "query_sender",
                    "description": "Find source records by sender.",
                    "search_fields": ["sender"],
                    "match_mode": "word",
                }
            ]
        }
    )
    tools = tools_for(store, **{"tools/operations.json": operations})
    definitions = tool_definitions(tools.harness)
    assert len(definitions) == 6
    assert definitions[-1]["function"]["name"] == "query_sender"
    result = tools.execute("query_sender", {"query": "User_1"})
    assert result["total_matches"] == 1
    assert result["matches"][0]["ref"] == "demo-team/001"


def test_operation_query_tools_remain_declarative_and_scoped(store):
    with pytest.raises(ValueError):
        tools_for(
            store,
            **{
                "tools/operations.json": json.dumps(
                    {
                        "query_tools": [
                            {
                                "name": "query_escape",
                                "description": "invalid",
                                "resource_path": "../outside",
                            }
                        ]
                    }
                )
            },
        )
    with pytest.raises(ValueError):
        tools_for(
            store,
            **{
                "tools/operations.json": json.dumps(
                    {
                        "query_tools": [
                            {
                                "name": "query_code",
                                "description": "invalid",
                                "implementation": "python:os.system",
                            }
                        ]
                    }
                )
            },
        )


def test_source_view_is_frozen_at_run_start(store):
    tools = tools_for(store)
    store.ingest(iter([Message.model_validate({**DEMO_MESSAGES[0], "message_id": "003"})]))
    assert (
        tools.execute("grep_search", {"target": "memory_units", "query": "release"})[
            "total_matches"
        ]
        == 2
    )


def test_required_alias_groups_and_optional_ranking_are_executed(store):
    tools = tools_for(store)
    result = tools.execute(
        "grep_search",
        {
            "target": "memory_units",
            "query": "unused natural language",
            "required_pattern_groups": [["release", "launch"], ["January", "February"]],
            "optional_patterns": ["changed"],
        },
    )
    assert result["total_matches"] == 2
    assert result["matches"][0]["ref"] == "demo-team/002"
    assert result["matches"][0]["optional_match_count"] == 1
    impossible = tools.execute(
        "grep_search",
        {
            "target": "memory_units",
            "query": "",
            "required_pattern_groups": [["missing"]],
            "optional_patterns": ["release"],
        },
    )
    assert impossible["total_matches"] == 0
    constrained = tools.execute(
        "grep_search",
        {
            "query": "",
            "patterns": ["changed"],
            "required_pattern_groups": [["release"]],
        },
    )
    assert constrained["total_matches"] == 1
    with pytest.raises(ValueError):
        tools.execute("grep_search", {"required_pattern_groups": [[]], "query": "release"})


def test_scoped_source_search_is_deterministic(store):
    tools = tools_for(store)
    broad = tools.execute("grep_search", {"target": "memory_units", "query": "release"})
    scoped = tools.execute(
        "grep_search",
        {"target": "memory_units", "resource_path": "", "query": "release"},
    )
    assert broad["total_matches"] == scoped["total_matches"] == 2
    assert {r["ref"] for r in broad["matches"]} == {r["ref"] for r in scoped["matches"]}


def test_oversized_source_line_can_be_fully_recovered_before_citation(store):
    message = Message.model_validate(
        {
            **DEMO_MESSAGES[0],
            "message_id": "oversized",
            "text": "release " + chr(1) * 10000,
        }
    )
    store.ingest(iter([message]))
    tools = tools_for(store)
    path = next(iter(tools.context))
    line = next(i for i, r in enumerate(tools.context[path], 1) if r["ref"] == message.ref)
    result = tools.execute("read_file", read_args(path, start_line=line, end_line=line))
    assert result["truncated"] and result["chunk_count"] > 1
    assert message.ref not in tools.delivered_refs
    chunks = sorted(p for p in tools.results.paths if "chunk_" in p)
    for chunk in chunks[:-1]:
        tools.execute("read_file", read_args(chunk))
        assert message.ref not in tools.delivered_refs
    tools.execute("read_file", read_args(chunks[-1]))
    assert message.ref in tools.delivered_refs


@pytest.mark.parametrize(
    "path",
    ["memory_units/source.jsonl", "../secret.md", "memory_store/../../secret.md", "/tmp/secret.md"],
)
def test_write_cannot_escape_notes(store, path):
    with pytest.raises(ContractError):
        tools_for(store).execute(
            "write_file", {"target": "memory_store", "resource_path": path, "content": "no"}
        )


def test_create_exclusive_write_replace_and_scope_isolation(store):
    tools = tools_for(store)
    tools.execute(
        "create_file",
        {"target": "memory_store", "resource_path": "working_memory/notes.md", "content": "first"},
    )
    with pytest.raises(FileExistsError):
        tools.execute(
            "create_file",
            {
                "target": "memory_store",
                "resource_path": "working_memory/notes.md",
                "content": "second",
            },
        )
    tools.execute(
        "write_file",
        {
            "target": "memory_store",
            "resource_path": "working_memory/notes.md",
            "content": "updated",
            "mode": "replace",
        },
    )
    assert (
        tools.execute(
            "read_file", {"target": "memory_store", "resource_path": "working_memory/notes.md"}
        )["lines"][0]["text"]
        == "updated"
    )
    store.ingest(iter([Message.model_validate({**DEMO_MESSAGES[0], "group_id": "another"})]))
    other = tools_for(store, ["demo-team", "another"])
    assert "memory_store/working_memory/notes.md" not in other.files()


def test_question_session_notes_remain_bound_to_the_authorized_groups(store):
    first = Tools(store, store.harness(), ["demo-team"], memory_session="same-session")
    first.execute(
        "create_file",
        {
            "target": "memory_store",
            "resource_path": "working_memory/note.md",
            "content": "navigation",
        },
    )
    second = Tools(store, store.harness(), ["demo-team"], memory_session="another-question")
    assert "memory_store/working_memory/note.md" not in second.files()
    store.ingest(iter([Message.model_validate({**DEMO_MESSAGES[0], "group_id": "another"})]))
    wider = Tools(store, store.harness(), ["demo-team", "another"], memory_session="same-session")
    assert "memory_store/working_memory/note.md" not in wider.files()


def test_note_symlink_escape(store, tmp_path):
    tools = tools_for(store)
    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    try:
        (tools.working_root / "escape.md").symlink_to(outside)
    except OSError:
        pytest.skip("Symlinks unavailable")
    with pytest.raises(ContractError):
        tools.execute(
            "read_file", {"target": "memory_store", "resource_path": "working_memory/escape.md"}
        )


def test_search_is_literal_and_paginated_and_operation_is_effective(store):
    tools = tools_for(
        store,
        **{"tools/operations.json": '{"search_mode":"any","search_limit":1,"context_window":0}'},
    )
    first = tools.execute("grep_search", {"target": "memory_units", "query": "release"})
    assert first["truncated"] and first["next_offset"] == 1
    assert (
        tools.execute("grep_search", {"target": "memory_units", "query": "release", "offset": 1})[
            "matches"
        ][0]["ref"]
        == "demo-team/002"
    )
    assert (
        tools.execute("grep_search", {"target": "memory_units", "query": ".*"})["total_matches"]
        == 0
    )
    contents = {
        "tools/operations.json": '{"search_mode":"all","search_limit":8,"context_window":1}'
    }
    conjunctive = tools_for(store, **contents)
    result = conjunctive.execute(
        "grep_search",
        {"target": "memory_units", "query": "", "selected_patterns": ["release", "changed"]},
    )
    assert result["total_matches"] == 1
    assert len(result["matches"][0]["context"]) == 2


def test_truncated_source_excerpt_does_not_validate_citation(store):
    store.ingest(
        iter(
            [
                Message.model_validate(
                    {**DEMO_MESSAGES[0], "message_id": "long", "text": "release " + "x" * 3000}
                )
            ]
        )
    )
    tools = tools_for(store)
    result = tools.execute("grep_search", {"target": "memory_units", "query": "xxx"})
    assert result["matches"][0]["excerpt_truncated"]
    assert "demo-team/long" not in tools.delivered_refs
    match = result["matches"][0]
    tools.execute(
        "read_file", read_args(match["path"], start_line=match["line"], end_line=match["line"])
    )
    assert "demo-team/long" in tools.delivered_refs


def test_representation_controls_metadata(store):
    store.ingest(
        iter(
            [
                Message.model_validate(
                    {**DEMO_MESSAGES[0], "message_id": "meta", "metadata": {"topic": "launch"}}
                )
            ]
        )
    )
    default = tools_for(store)
    path = next(iter(default.context))
    assert all("metadata" not in row for row in default.context[path])
    expanded = tools_for(store, **{"memory/representation.json": '{"include_metadata":true}'})
    assert any(row.get("metadata", {}).get("topic") == "launch" for row in expanded.context[path])


def test_unknown_tool_arguments_rejected(store):
    with pytest.raises(ValueError):
        tools_for(store).execute(
            "grep_search", {"target": "memory_units", "query": "release", "shell": "rm"}
        )


def test_source_lines_have_stable_refs(store):
    tools = tools_for(store)
    path = next(iter(tools.context))
    result = tools.execute("read_file", read_args(path))
    assert json.loads(result["lines"][1]["text"])["reply_to"] == "001"


def test_memory_layers_and_search_ledger_are_scoped(store):
    first = Tools(
        store,
        store.harness(),
        ["demo-team"],
        memory_session="question-1",
        isolated_long_term=True,
    )
    source = next(path for path in first.files() if path.startswith("memory_units/"))
    assert first.execute("read_file", read_args(source, end_line=1))["lines"][0]["ref"]
    first.execute(
        "create_file",
        {
            "target": "memory_store",
            "resource_path": "long_term_memory/facts.md",
            "content": "durable",
        },
    )
    first.execute(
        "create_file",
        {
            "target": "memory_store",
            "resource_path": "working_memory/scratch.md",
            "content": "temporary",
        },
    )
    ledger = first.execute(
        "read_file",
        {"target": "memory_store", "resource_path": "working_memory/search_ledger.jsonl"},
    )
    assert ledger["lines"] and '"operation":"read"' in ledger["lines"][0]["text"]

    second = Tools(
        store,
        store.harness(),
        ["demo-team"],
        memory_session="question-2",
        isolated_long_term=True,
    )
    assert "memory_store/long_term_memory/facts.md" not in second.files()
    assert "memory_store/working_memory/scratch.md" not in second.files()


def test_search_ledger_is_runtime_owned(store):
    tools = tools_for(store)
    tools.execute("grep_search", {"target": "memory_units", "query": "release"})
    with pytest.raises(ContractError, match="ledger"):
        tools.execute(
            "write_file",
            {
                "target": "memory_store",
                "resource_path": "working_memory/search_ledger.jsonl",
                "content": "forged",
            },
        )


@pytest.mark.parametrize(
    "term,refs",
    [
        ("USER_1", {"demo-team/001"}),
        ("2026-01-06", {"demo-team/002"}),
        ("+08:00", {"demo-team/001", "demo-team/002"}),
        ("demo-team", {"demo-team/001", "demo-team/002"}),
        ("002", {"demo-team/002"}),
        ("001", {"demo-team/001", "demo-team/002"}),
    ],
)
def test_search_finds_attribution_local_date_and_message_links(store, term, refs):
    result = tools_for(store).execute("grep_search", {"target": "memory_units", "query": term})
    assert {match["ref"] for match in result["matches"]} == refs


def test_search_conjunction_combines_sender_date_and_body(store):
    tools = tools_for(store, **{"tools/operations.json": '{"search_mode":"all"}'})
    result = tools.execute(
        "grep_search",
        {
            "target": "memory_units",
            "query": "",
            "patterns": ["User_2", "2026-01-06", "changed"],
        },
    )
    assert [match["ref"] for match in result["matches"]] == ["demo-team/002"]
    assert (
        tools.execute("grep_search", {"target": "memory_units", "query": "sender"})["total_matches"]
        == 0
    )


def test_scoped_memory_root_symlink_escape_is_rejected(store, tmp_path):
    memory = store.workspace / "memory"
    memory.mkdir(exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (memory / fingerprint(["demo-team"])).symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Symlinks unavailable")
    with pytest.raises(ContractError, match="escapes"):
        tools_for(store)


def test_long_search_windows_are_paginated_before_delivery_validation(store):
    messages = [
        Message.model_validate(
            {**DEMO_MESSAGES[0], "message_id": f"large-{index:02d}", "text": "release " + "x" * 700}
        )
        for index in range(40)
    ]
    store.ingest(iter(messages))
    tools = tools_for(
        store,
        **{"tools/operations.json": '{"search_mode":"any","search_limit":30,"context_window":5}'},
    )
    result = tools.execute("grep_search", {"target": "memory_units", "query": "release"})
    assert len(dumps(result)) <= 12000
    assert result["truncated"]
    full = tools.last_full_result
    assert len(dumps(full)) < 61000
    assert full["next_offset"] == len(full["matches"])
    assert tools.delivered_refs == set()
    archived = tools.results.paths[result["full_result_path"]].read_text()
    assert json.loads(archived) == full
    chunks = sorted(p for p in tools.results.paths if "chunk_" in p)
    for path in chunks[:-1]:
        page = tools.execute("read_file", read_args(path))
        assert len(dumps(page)) <= 12000
        assert not tools.delivered_refs
    tools.execute("read_file", read_args(chunks[-1]))
    assert tools.delivered_refs


def test_result_archives_are_scoped_read_only_and_cannot_be_overwritten(store):
    from evog.harness.tools import Tools

    store.ingest(
        iter(
            [
                Message.model_validate(
                    {**DEMO_MESSAGES[0], "message_id": "large", "text": "release " + "x" * 3000}
                )
            ]
        )
    )
    first = Tools(store, store.harness(), ["demo-team"], max_output_chars=1024)
    first.execute("grep_search", {"target": "memory_units", "query": "release"})
    assert first.results.paths
    path = next(iter(first.results.paths))
    other = Tools(store, store.harness(), ["demo-team"])
    with pytest.raises(ContractError, match="scope"):
        other.execute("read_file", read_args(path))
    with pytest.raises(ContractError, match="writable"):
        first.execute(
            "write_file",
            {"target": "memory_store", "resource_path": path, "content": "forged source"},
        )


def test_representation_and_operation_extensions_change_executed_views(store):
    tools = tools_for(
        store,
        **{
            "memory/representation.json": '{"timestamp_view":"both","include_reply_refs":true}',
            "tools/operations.json": '{"search_fields":["sender"],"match_mode":"word","read_limit":1}',
        },
    )
    assert (
        tools.execute("grep_search", {"target": "memory_units", "query": "release"})[
            "total_matches"
        ]
        == 0
    )
    assert (
        tools.execute("grep_search", {"target": "memory_units", "query": "May"})["total_matches"]
        == 0
    )
    found = tools.execute("grep_search", {"target": "memory_units", "query": "User_1"})
    assert found["total_matches"] == 1
    path = next(iter(tools.context))
    assert len(tools.execute("read_file", read_args(path))["lines"]) == 1
    second = tools.execute("read_file", read_args(path, start_line=2, end_line=2))
    record = json.loads(second["lines"][0]["text"])
    assert record["utc_timestamp"] and record["reply_ref"] == "demo-team/001"
