# Tool implementations

The registry in `registry.yaml` selects each tool's Python entrypoint and permissions.
The matching file in `descriptions/` is the authoritative model-visible tool name,
description, and JSON Schema. Edit the implementation and description together when
changing arguments. `operations.json` supplies shared search and read settings.

Each implementation exports a synchronous function with exactly two positional
parameters and returns a finite JSON object:

```python
def execute(payload, ctx):
    settings = ctx.call("operations", {})
    return {"read_limit": settings["read_limit"]}
```

`payload` contains validated model arguments. The five standard tools receive their
declared defaults, and a newly registered tool receives its declared arguments. JSON
Schema validation supports local `$defs` and
`$ref`, compositions, nullable types, and object/array constraints. External schema
resources are unavailable.

Implementations run in a fresh Python subprocess with an exported component bundle,
a private working directory, a minimal environment, and a bounded execution deadline.
They can import the Python standard library and bundled helper modules, for example
`from tools.implementations.search_helpers import documents`. Third-party packages
and the host EvoGroup package are not part of this interpreter. Imports and package
state do not survive the call. This process separation is not an operating-system
security sandbox: implementations are trusted Python code.

## Host capabilities

Use `ctx.call(name, arguments)` for product data and note I/O. Calls return JSON.
The host checks registry permissions and the current interaction's authorized group
scope on every call. Source records are frozen when the interaction starts.

| Name | Arguments | Result |
| --- | --- | --- |
| `operations` | `{}` | Validated shared operations settings; declarative query aliases may override these settings. |
| `files` | `{}` | Logical paths readable by this tool, including source units, scoped notes, skills, component files, and this interaction's result archives. |
| `archive_paths` | `{}` | Readable result archive paths for this interaction. |
| `group_paths` | `{}` | Authorized source path to group identifier mapping. |
| `read_document` | `{"path": "memory_units/example.jsonl", "offset": 0}` | A bounded page with `lines`, corresponding `records` for source units, `offset`, `total_lines`, and `next_offset`; repeat while `next_offset` is non-null. |
| `read_lines` | `{"path": "..."}` | Complete text lines from an authorized logical resource. Prefer paged `read_document` for corpus files. |
| `source_record` | `{"path": "...", "index": 0}` | One original source record at a zero-based index, or null for a non-source path/out-of-range index. |
| `source_context` | `{"path": "...", "index": 0, "window": 1}` | Original neighboring records, with a window between 0 and 5. |
| `note_state` | `{"arguments": {"target": "memory_store", "resource_path": "working_memory/note.md", "content": ""}}` | Whether the note exists, its current content, and whether long-term writes require round staging. Requires readable memory-store permission. |
| `write_note` | `{"arguments": {"target": "memory_store", "resource_path": "working_memory/note.md", "content": "...", "mode": "replace"}, "create": false}` | A scoped note-write receipt. `create: true` uses exclusive creation unless `overwrite: true` is provided. |

`workspace/<component path>` provides read-only access to bundled component files.
`memory_units/` is the read-only source view. `memory_store/working_memory/` is
question-scoped scratch state; `memory_store/long_term_memory/` is authorized durable
or round-staged memory. `tool_results/` contains
read-only archives of tool outputs omitted from the model context.

Registry permissions can narrow these surfaces, but cannot add host directories or
write source units, component files, archives, or runtime state. Writes are restricted
to text notes under the two memory-store layers. `search_ledger.jsonl` belongs to the
runtime and cannot be modified by a tool. Frozen long-term memory is validated and
staged by the host; the implementation cannot bypass source or scope validation.

## Evidence delivery

A capability read alone does not authorize a citation. The host grants evidence
only when the exact, complete original record appears in a tool result actually
submitted to the model after middleware and context trimming. Returning a fabricated
`ref`, changing source content, or returning a truncated excerpt grants no source
reference. Preserve the original record serialization when presenting evidence.

Large results are archived by the host. Their previews do not grant delivery;
`read_file` must recover the original complete result or all of its archive chunks.
Candidate code cannot write archives or advance their delivery bookkeeping. Prompt
and middleware revisions remain subject to the same host-owned final answer checks.
