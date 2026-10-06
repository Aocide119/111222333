"""Editable create workflow; exclusive creation is enforced by scoped host I/O."""

from tools.implementations.search_helpers import note_arguments


def execute(payload, ctx):
    arguments = note_arguments(payload)
    return ctx.call("write_note", {"arguments": arguments, "create": True})
