# Middleware components

Register synchronous Python handlers in `registry.yaml`. Each handler implements
`execute(payload, context)` and returns a JSON object. Handlers run in registry
order in fresh processes with a private working directory. The payload also
contains the registration's `config` and the current `hook`.

These hooks belong to the group answering agent. Model access, credentials,
question budgets, source authorization, archives, and scoring remain in the core.
Workspace and process separation is not an OS security sandbox; Python handlers
run with the invoking user's filesystem permissions.

| Hook | Input fields | Response fields |
| --- | --- | --- |
| `before_model` | `messages`, `max_chars`, `target_chars` | `messages` |
| `after_model` | `content` | `content` |
| `before_tool` | `name`, `arguments` | `arguments` |
| `after_tool` | `result`, `max_chars` | `result`, optional `archive_requested` |
| `before_finalize` | `reason`, `tool_calls`, `tool_rounds` | `reminder` |

An empty object leaves the input unchanged. `after_model` and `before_tool`
reject response fields outside the table. Other hooks consume their listed
fields; additional fields do not alter core state.

## Context and evidence

`before_model` may remove complete exchanges. It must retain the first system
and question messages, keep the remaining messages unchanged and in their
original order, and preserve complete tool request/response pairs. It cannot
inject summaries, system messages, tool requests, or fabricated responses.
The core applies its context bound after this hook.

`after_tool` may change a result's presentation. Evidence is recognized only when
the complete original source record remains in the actual result submitted to
the model. A ref alone, a truncated excerpt, or a private capability read does
not establish delivery. Oversized results are archived by the core; omitted
sources become eligible only after their complete archive content is delivered.
`archive_requested` is a presentation hint and cannot override the core's bound
or grant delivery.

## Arguments and model responses

`before_tool` may normalize the arguments of the requested tool. The tool name
remains fixed, and argument schema, registry permissions, and source scope are
checked after the hook. The trace records both original and effective arguments.

`after_model` may return response `content` up to 256,000 characters. It cannot
change tool calls, token usage, model identity, or transport metadata. The trace
retains original content when a transformation occurs. Final answer validation
still applies.

`before_finalize` may return a reminder up to 2,000 characters. It cannot enable
tools or expand any turn, tool, context, or time budget.
