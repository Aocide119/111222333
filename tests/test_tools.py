import json

import pytest

from evog.demo import DEMO_MESSAGES
from evog.errors import ContractError
from evog.harness import Harness
from evog.io import dumps, fingerprint
from evog.models import Message
from evog.tools import Tools, tool_definitions


def tools_for(store, groups=None, **patches):
    contents = store.harness().contents
    contents.update(patches)
    return Tools(store, Harness(contents), groups or ["demo-team"])


def test_five_tools_and_scoped_read_view(store):
    store.ingest(iter([Message.model_validate({**DEMO_MESSAGES[0], "group_id": "private"})]))
    tools = tools_for(store)
    assert len(tool_definitions()) == 5
    files = tools.execute("list_files", {})["files"]
    assert len(files) == 1
    assert files[0]["group_id"] == "demo-team"
    result = tools.execute("read_file", {"path": files[0]["path"], "limit": 1})
    assert result["truncated"] and result["next_start_line"] == 2
    assert tools.delivered_refs == {"demo-team/001"}
    with pytest.raises(ContractError):
        Tools(store, store.harness(), [])
    with pytest.raises(ContractError):
        Tools(store, store.harness(), ["unknown"])


def test_source_view_is_frozen_at_run_start(store):
    tools = tools_for(store)
    store.ingest(iter([Message.model_validate({**DEMO_MESSAGES[0], "message_id": "003"})]))
    assert tools.execute("grep_search", {"terms": ["release"]})["total_matches"] == 2


def test_source_aliases_do_not_duplicate_broad_search_results(store):
    tools = tools_for(store)
    broad = tools.execute("grep_search", {"terms": ["release"]})
    alias = tools.execute("grep_search", {"terms": ["release"], "path": "memory_units/"})
    assert broad["total_matches"] == alias["total_matches"] == 2
    assert {r["ref"] for r in broad["matches"]} == {r["ref"] for r in alias["matches"]}


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
    result = tools.execute("read_file", {"path": path, "start_line": line, "limit": 1})
    assert result["truncated"] and result["chunk_count"] > 1
    assert message.ref not in tools.delivered_refs
    chunks = sorted(p for p in tools.results.paths if "chunk_" in p)
    for chunk in chunks[:-1]:
        tools.execute("read_file", {"path": chunk})
        assert message.ref not in tools.delivered_refs
    tools.execute("read_file", {"path": chunks[-1]})
    assert message.ref in tools.delivered_refs


@pytest.mark.parametrize(
    "path", ["context/source.jsonl", "../secret.md", "memory/../../secret.md", "/tmp/secret.md"]
)
def test_write_cannot_escape_notes(store, path):
    with pytest.raises(ContractError):
        tools_for(store).execute("write_file", {"path": path, "content": "no"})


def test_create_exclusive_write_replace_and_scope_isolation(store):
    tools = tools_for(store)
    tools.execute("create_file", {"path": "memory/notes.md", "content": "first"})
    with pytest.raises(FileExistsError):
        tools.execute("create_file", {"path": "memory/notes.md", "content": "second"})
    tools.execute("write_file", {"path": "memory/notes.md", "content": "updated"})
    assert tools.execute("read_file", {"path": "memory/notes.md"})["lines"][0]["text"] == "updated"
    store.ingest(iter([Message.model_validate({**DEMO_MESSAGES[0], "group_id": "another"})]))
    other = tools_for(store, ["demo-team", "another"])
    assert "memory/notes.md" not in other.files()


def test_question_session_notes_remain_bound_to_the_authorized_groups(store):
    first = Tools(store, store.harness(), ["demo-team"], memory_session="same-session")
    first.execute("create_file", {"path": "memory/note.md", "content": "navigation"})
    second = Tools(store, store.harness(), ["demo-team"], memory_session="another-question")
    assert "memory/note.md" not in second.files()
    store.ingest(iter([Message.model_validate({**DEMO_MESSAGES[0], "group_id": "another"})]))
    wider = Tools(store, store.harness(), ["demo-team", "another"], memory_session="same-session")
    assert "memory/note.md" not in wider.files()


def test_note_symlink_escape(store, tmp_path):
    tools = tools_for(store)
    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    try:
        (tools.memory_root / "escape.md").symlink_to(outside)
    except OSError:
        pytest.skip("Symlinks unavailable")
    with pytest.raises(ContractError):
        tools.execute("read_file", {"path": "memory/escape.md"})


def test_search_is_literal_and_paginated_and_operation_is_effective(store):
    tools = tools_for(
        store, **{"operations.json": '{"search_mode":"any","search_limit":1,"context_window":0}'}
    )
    first = tools.execute("grep_search", {"terms": ["release"]})
    assert first["truncated"] and first["next_offset"] == 1
    assert (
        tools.execute("grep_search", {"terms": ["release"], "offset": 1})["matches"][0]["ref"]
        == "demo-team/002"
    )
    assert tools.execute("grep_search", {"terms": [".*"]})["total_matches"] == 0
    contents = {"operations.json": '{"search_mode":"all","search_limit":8,"context_window":1}'}
    conjunctive = tools_for(store, **contents)
    result = conjunctive.execute("grep_search", {"terms": ["release", "changed"]})
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
    result = tools.execute("grep_search", {"terms": ["xxx"]})
    assert result["matches"][0]["excerpt_truncated"]
    assert "demo-team/long" not in tools.delivered_refs
    match = result["matches"][0]
    tools.execute("read_file", {"path": match["path"], "start_line": match["line"], "limit": 1})
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
    expanded = tools_for(store, **{"representation.json": '{"include_metadata":true}'})
    assert any(row.get("metadata", {}).get("topic") == "launch" for row in expanded.context[path])


def test_unknown_tool_arguments_rejected(store):
    with pytest.raises(ValueError):
        tools_for(store).execute("grep_search", {"terms": ["release"], "shell": "rm"})


def test_source_lines_have_stable_refs(store):
    tools = tools_for(store)
    path = next(iter(tools.context))
    result = tools.execute("read_file", {"path": path})
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
    assert first.execute("read_file", {"path": source, "limit": 1})["lines"][0]["ref"]
    first.execute(
        "create_file",
        {"path": "memory_store/long_term_memory/facts.md", "content": "durable"},
    )
    first.execute(
        "create_file",
        {"path": "memory_store/working_memory/scratch.md", "content": "temporary"},
    )
    ledger = first.execute("read_file", {"path": "memory/search_ledger.jsonl"})
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
    tools.execute("grep_search", {"terms": ["release"]})
    with pytest.raises(ContractError, match="ledger"):
        tools.execute("write_file", {"path": "memory/search_ledger.jsonl", "content": "forged"})


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
    result = tools_for(store).execute("grep_search", {"terms": [term]})
    assert {match["ref"] for match in result["matches"]} == refs


def test_search_conjunction_combines_sender_date_and_body(store):
    tools = tools_for(store, **{"operations.json": '{"search_mode":"all"}'})
    result = tools.execute("grep_search", {"terms": ["User_2", "2026-01-06", "changed"]})
    assert [match["ref"] for match in result["matches"]] == ["demo-team/002"]
    assert tools.execute("grep_search", {"terms": ["sender"]})["total_matches"] == 0


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
        store, **{"operations.json": '{"search_mode":"any","search_limit":30,"context_window":5}'}
    )
    result = tools.execute("grep_search", {"terms": ["release"]})
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
        page = tools.execute("read_file", {"path": path})
        assert len(dumps(page)) <= 12000
        assert not tools.delivered_refs
    tools.execute("read_file", {"path": chunks[-1]})
    assert tools.delivered_refs


def test_result_archives_are_scoped_read_only_and_cannot_be_overwritten(store):
    from evog.tools import Tools

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
    first.execute("grep_search", {"terms": ["release"]})
    assert first.results.paths
    path = next(iter(first.results.paths))
    other = Tools(store, store.harness(), ["demo-team"])
    with pytest.raises(ContractError, match="scope"):
        other.execute("read_file", {"path": path})
    with pytest.raises(ContractError, match="writable"):
        first.execute("write_file", {"path": path, "content": "forged source"})


def test_archived_chunks_are_readable_through_public_tool_arguments(store):
    store.ingest(
        iter(
            [
                Message.model_validate(
                    {**DEMO_MESSAGES[0], "message_id": "large", "text": "release " + "x" * 3000}
                )
            ]
        )
    )
    tools = Tools(store, store.harness(), ["demo-team"], max_output_chars=1024)
    source = tools.execute("list_files", {"target": "memory_units"})["files"][0]
    preview = tools.execute(
        "read_file", {"target": "memory_units", "resource_path": source["resource_path"]}
    )
    assert preview["truncated"] and not tools.delivered_refs
    listing = tools.execute(
        "list_files", {"target": "workspace", "resource_path": preview["first_chunk"]}
    )
    assert listing["files"]
    chunks = sorted(path for path in tools.results.paths if "chunk_" in path)
    for path in chunks:
        result = tools.execute("read_file", {"target": "workspace", "resource_path": path})
        assert result["lines"] and not result["truncated"]
        assert len(dumps(result)) <= 1024
    assert "demo-team/large" in tools.delivered_refs
    other = Tools(store, store.harness(), ["demo-team"])
    with pytest.raises(ContractError, match="scope"):
        other.execute("read_file", {"target": "workspace", "resource_path": chunks[0]})
    with pytest.raises(ContractError, match="scope"):
        tools.execute("read_file", {"target": "workspace", "resource_path": "config.toml"})


def test_representation_and_operation_extensions_change_executed_views(store):
    tools = tools_for(
        store,
        **{
            "representation.json": '{"timestamp_view":"both","include_reply_refs":true}',
            "operations.json": '{"search_fields":["sender"],"match_mode":"word","read_limit":1}',
        },
    )
    assert tools.execute("grep_search", {"terms": ["release"]})["total_matches"] == 0
    assert tools.execute("grep_search", {"terms": ["User"]})["total_matches"] == 0
    found = tools.execute("grep_search", {"terms": ["User_1"]})
    assert found["total_matches"] == 1
    path = next(iter(tools.context))
    assert len(tools.execute("read_file", {"path": path})["lines"]) == 1
    second = tools.execute("read_file", {"path": path, "start_line": 2, "limit": 1})
    record = json.loads(second["lines"][0]["text"])
    assert record["utc_timestamp"] and record["reply_ref"] == "demo-team/001"
