import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from evog.errors import ContractError
from evog.memory import MemoryRound
from evog.tools import Tools


def note(ref, excerpt):
    return json.dumps({"entries": [{"ref": ref, "excerpt": excerpt}]})


def test_round_freezes_reads_and_merges_in_canonical_order(store):
    groups = [store.groups()[0]["group_id"]]
    message = store.messages(groups[0])[0]
    round_one = MemoryRound(store.workspace, None, "campaign/0")

    def stage(key):
        tool = Tools(
            store,
            store.harness(),
            groups,
            memory_session=key,
            isolated_long_term=True,
            frozen_memory=round_one.binding(groups, key),
        )
        tool.delivered_refs.add(message.ref)
        tool.execute(
            "create_file",
            {
                "target": "memory_store",
                "resource_path": "long_term_memory/source.json",
                "content": note(message.ref, message.text),
            },
        )
        return tool

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(stage, ["a", "b"]))
    observer = Tools(
        store,
        store.harness(),
        groups,
        memory_session="c",
        isolated_long_term=True,
        frozen_memory=round_one.binding(groups, "c"),
    )
    assert not any("long_term_memory/source.json" in p for p in observer.files())
    next_id = round_one.merge()
    assert next_id == round_one.merge()
    round_two = MemoryRound(store.workspace, next_id, "campaign/1")
    next_tools = Tools(
        store,
        store.harness(),
        groups,
        memory_session="next",
        isolated_long_term=True,
        frozen_memory=round_two.binding(groups, "next"),
    )
    result = next_tools.execute(
        "read_file", {"target": "memory_store", "resource_path": "long_term_memory/source.json"}
    )
    assert message.ref in result["lines"][0]["text"]
    snapshot = round_two.snapshot
    entries = next(iter(snapshot["scopes"].values()))["files"]["source.json"]
    assert len(entries) == 1
    assert first.long_term_root != second.long_term_root


def test_long_term_notes_require_exact_delivered_source_and_scope(store):
    groups = [store.groups()[0]["group_id"]]
    message = store.messages(groups[0])[0]
    frozen = MemoryRound(store.workspace, None, "one")
    tool = Tools(
        store,
        store.harness(),
        groups,
        memory_session="a",
        isolated_long_term=True,
        frozen_memory=frozen.binding(groups, "a"),
    )
    args = {
        "target": "memory_store",
        "resource_path": "long_term_memory/source.json",
        "content": note(message.ref, message.text),
    }
    with pytest.raises(ContractError, match="fully delivered"):
        tool.execute("create_file", args)
    tool.delivered_refs.add(message.ref)
    with pytest.raises(ContractError):
        tool.execute("create_file", {**args, "content": note(message.ref, "invented answer")})
    with pytest.raises(ContractError):
        tool.execute("create_file", {**args, "content": "gold answer or judge feedback"})
    tool.execute("create_file", args)
    snapshot = frozen.merge()
    other = MemoryRound(store.workspace, snapshot, "two").binding(["other-group"], "b")
    other.seed(store.workspace / "other")
    assert not (store.workspace / "other/source.json").exists()


def test_snapshot_changes_are_detected(store):
    frozen = MemoryRound(store.workspace, None, "one")
    path = frozen.root / "snapshots" / f"{frozen.snapshot_id}.json"
    path.write_text('{"schema":"evog.memory.v1","parent":null,"scopes":{"fake":{}}}')
    with pytest.raises(ContractError, match="fingerprint"):
        MemoryRound(store.workspace, frozen.snapshot_id, "two")
