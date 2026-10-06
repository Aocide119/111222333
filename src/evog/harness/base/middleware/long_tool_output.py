"""Request source-preserving archival when a tool result exceeds its budget."""

import json


def execute(payload, context):
    result = payload["result"]
    size = len(json.dumps(result, ensure_ascii=False))
    # The core owns archive storage, delivery ledgers, and the final output cap.
    # A middleware request cannot establish that omitted evidence was delivered.
    return {"result": result, "archive_requested": size > int(payload["max_chars"])}
