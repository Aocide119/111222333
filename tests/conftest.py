from __future__ import annotations

import pytest

from evog.config import Settings
from evog.demo import DEMO_MESSAGES
from evog.models import Message
from evog.providers import ModelReply
from evog.store import Store


class ScriptedProvider:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def complete(self, messages, tools):
        self.calls.append((messages.copy(), tools))
        if not self.replies:
            raise AssertionError("Unexpected provider call")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply(messages, tools) if callable(reply) else reply


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path / "workspace")
    result.ingest(iter(Message.model_validate(row) for row in DEMO_MESSAGES))
    return result


@pytest.fixture
def settings():
    return Settings(max_turns=4, analysis_turns=3)


@pytest.fixture
def empty_reply():
    return ModelReply(
        content='{"text":"No supporting information found.","confidence":0.2,"status":"insufficient","citations":[]}'
    )
