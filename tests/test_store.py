import json

import pytest
from pydantic import ValidationError

from evog.demo import DEMO_MESSAGES
from evog.errors import ConflictError, ContractError
from evog.models import Message


def test_import_is_idempotent_and_preserves_timezone(store):
    messages = [Message.model_validate(row) for row in DEMO_MESSAGES]
    assert store.ingest(iter(messages)) == 0
    assert store.messages("demo-team")[0].timestamp.isoformat() == "2026-01-05T09:00:00+08:00"


def test_source_offsets_and_fractional_seconds_do_not_disturb_chronological_order(store):
    rows = [
        ("a", "2026-01-07T00:10:00+08:00"),
        ("b", "2026-01-06T16:05:00Z"),
        ("c", "2026-01-06T16:05:00.000001Z"),
    ]
    store.ingest(
        iter(
            Message.model_validate({**DEMO_MESSAGES[0], "message_id": ref, "timestamp": stamp})
            for ref, stamp in rows
        )
    )
    messages = store.messages("demo-team")
    assert [message.message_id for message in messages][-3:] == ["b", "c", "a"]
    assert messages[-1].model_dump(mode="json")["timestamp"] == "2026-01-07T00:10:00+08:00"


def test_legacy_time_index_is_upgraded_without_rewriting_sources(store):
    from evog.store import Store

    with store.connect() as db:
        original = [
            row[0] for row in db.execute("SELECT payload FROM messages ORDER BY message_id")
        ]
        db.execute("DELETE FROM state WHERE key='timestamp_index_v2'")
        db.execute("UPDATE messages SET timestamp='2026-01-05T01:00:00+00:00'")
    Store(store.workspace)
    with store.connect() as db:
        assert (
            db.execute("SELECT timestamp FROM messages LIMIT 1")
            .fetchone()[0]
            .endswith(".000000+00:00")
        )
        assert original == [
            row[0] for row in db.execute("SELECT payload FROM messages ORDER BY message_id")
        ]


def test_conflicting_record_rolls_back_whole_batch(store):
    fresh = Message.model_validate({**DEMO_MESSAGES[0], "message_id": "003"})
    changed = Message.model_validate({**DEMO_MESSAGES[0], "text": "A conflicting update"})
    with pytest.raises(ConflictError):
        store.ingest(iter([fresh, changed]))
    assert len(store.messages("demo-team")) == 2


def test_invalid_jsonl_rolls_back_prior_valid_rows(store, tmp_path):
    imported = tmp_path / "import.jsonl"
    imported.write_text(
        json.dumps({**DEMO_MESSAGES[0], "message_id": "004"}) + "\n{bad json}", encoding="utf-8"
    )
    with pytest.raises(ContractError, match="line 2"):
        store.ingest_jsonl(imported)
    assert len(store.messages("demo-team")) == 2


@pytest.mark.parametrize(
    "patch",
    [{"timestamp": "2026-01-05T09:00:00"}, {"group_id": "../outside"}, {"message_id": "two words"}],
)
def test_invalid_source_coordinates_are_rejected(patch):
    with pytest.raises(ValidationError):
        Message.model_validate({**DEMO_MESSAGES[0], **patch})


def test_missing_run_is_explicit(store):
    with pytest.raises(ContractError, match="Unknown run"):
        store.run("missing")


@pytest.mark.parametrize(
    "patch",
    [
        {"metadata": {"key": "x" * 4001}},
        {"text": "   "},
        {"sender": " "},
        {"reply_to": "other/message"},
    ],
)
def test_source_payload_size_and_attribution_contract(patch):
    with pytest.raises(ValidationError):
        Message.model_validate({**DEMO_MESSAGES[0], **patch})
