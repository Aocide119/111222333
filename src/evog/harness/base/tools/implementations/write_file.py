"""Editable note replacement and append workflow over scoped host I/O."""

from tools.implementations.search_helpers import note_arguments


def execute(payload, ctx):
    arguments = note_arguments(payload)
    if arguments.get("mode", "append") == "append":
        state = ctx.call("note_state", {"arguments": arguments})
        if not state["staged"]:
            arguments["content"] = state["content"] + arguments["content"]
            arguments["mode"] = "replace"
    result = ctx.call("write_note", {"arguments": arguments, "create": False})
    # The caller's requested mode and payload size remain the user-facing result.
    result["written_chars"] = len(payload["content"])
    if not result.get("staged_for_next_round"):
        result["mode"] = payload.get("mode", "append")
    return result
