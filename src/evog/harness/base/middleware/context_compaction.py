"""Drop old complete exchanges while preserving system and question anchors."""

import json


def execute(payload, context):
    messages = payload["messages"]
    head, tail = messages[:2], messages[2:]
    target = max(int(payload.get("target_chars", payload["max_chars"])), 1)
    while tail and len(json.dumps(head + tail, ensure_ascii=False)) > target:
        tail = tail[1:]
        while tail and tail[0].get("role") == "tool":
            tail = tail[1:]
    return {"messages": head + tail}
