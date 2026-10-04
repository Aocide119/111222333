# Deployment configuration

Optional TOML settings:

```toml
base_url = "https://your-provider.example/v1"
model = "your-model"
reasoning_effort = "none"
max_turns = 60
max_tool_calls = 20
max_tool_rounds = 20
max_parallel_tool_calls = 3
max_tool_output_chars = 12000
repeated_tool_limit = 3
max_output_tokens = 8192
reflection_timeout_seconds = 60
reflection_max_output_tokens = 8192
request_timeout = 120
call_timeout_seconds = 60
call_attempts = 3
question_timeout_seconds = 500
max_context_chars = 800000
context_trim_ratio = 0.8
benchmark_concurrency = 4
confidence_threshold = 0.5
analysis_max_runs = 30
analysis_concurrency = 4
analysis_turns = 15
analysis_timeout_seconds = 900
analysis_max_output_tokens = 20000
analysis_max_context_chars = 800000
analysis_min_valid = 10
analysis_min_coverage = 0.7
finding_min_support = 2
synthesis_timeout_seconds = 300
synthesis_max_output_tokens = 20000
synthesis_max_context_chars = 800000
evolution_turns = 15
evolution_timeout_seconds = 900
evolution_max_output_tokens = 32000
evolution_max_context_chars = 800000
candidate_acceptance = "net_improvement"
```

Use `evog --config settings.toml ask '...' --group product`. Set credentials through
`EVOG_API_KEY`; `EVOG_BASE_URL` and `EVOG_MODEL` override their TOML fields. Unknown settings
are rejected. Keys are secrets and are excluded from persisted settings.

`max_tool_calls` counts individual executed tool requests. `max_tool_rounds` counts model
responses with executed tools; a response can execute at most `max_parallel_tool_calls` requests.
The defaults allow at most 20 executed tool requests across 20 rounds. The fixed runtime also
enforces the 20-request ceiling, and configuration values above 20 are rejected. Requests denied
before execution do not consume this counter. Denied requests remain paired
with responses in the trace. The default `max_turns` allows 60 ordinary model turns plus one reserved
answer call without tools when a turn, tool or repetition boundary is reached. Output checks apply
to that final answer. The model-facing answer wire format is plain text: `FINAL ANSWER: ...`,
`CONFIDENCE: ...`, and, at or below 0.5 confidence, `ANSWER BIAS: ...`. The SQLite record still
stores typed fields, the raw response and the bias text. No additional answer call starts after
the question clock expires. Low-confidence reflection uses at most one call with its own
60-second deadline and 8192-token output budget, bounded by the question deadline.

Model context is bounded in characters. At `context_trim_ratio * max_context_chars`, earlier
exchanges are removed while preserving the system prompt, original question and complete tool
request/response pairs. 800,000 characters roughly correspond to 200,000 tokens under a characters
/ 4 estimate; this is not the model tokenizer. Full traces remain intact. Tool responses exceeding
`max_tool_output_chars` are archived inside the interaction scope with bounded previews and readable
chunks. Omitted sources cannot be cited until fully delivered by source reads or archive recovery.

Analysis, synthesis and revision have independent output, context and task-clock budgets.
Concurrent diagnoses use separate evidence scopes. Providers must support concurrent calls when
concurrency exceeds 1; the built-in HTTP provider does. Defaults use serial execution unless
`analysis_concurrency` or `benchmark_concurrency` is configured. Failures take selection priority
over uncertain successes. Diagnosis format, quality and evidence errors receive at most two
corrections within the turn cap. `analysis_max_runs` limits question jobs, grouping trials by
revision, exact question and authorized groups. A job can inspect all recorded trials of that
question within the selected batch. Unavailable or invalid diagnoses remain in `incomplete`;
this field does not assert that every trace event must be read.

Default synthesis uses failed-answer and execution-failure jobs. Low-confidence accepted or
unjudged answers remain calibration diagnoses. The required failure-job count is
`min(analysis_min_valid, selected_failure_jobs)`, with at least `analysis_min_coverage` of those
jobs valid and included in synthesis. These thresholds govern synthesis. With insufficient
coverage, revision can independently inspect selected traces and register a supported finding.
Repeated findings require distinct questions within their group scopes; multiple trials of
one question count once. Isolated observations remain limited to one case.

Synthesis receives whole condensed diagnoses that fit its context, with included/omitted coverage.
It allows at most `call_attempts` attempts. Revision can inspect supplied analyses/traces and
register independently inspected findings and validate drafts; it allows three invalid draft
responses within `evolution_turns`. These tools
cannot activate revisions, edit deployment files, run generated Python or access judge answers.

HTTP attempts cap their timeout by `request_timeout`, `call_timeout_seconds` and the remaining
task clock. Timeouts and selected HTTP errors receive bounded retries; other transport errors
fail promptly. Deadline checks prevent starting new calls or accepting late interaction answers.
HTTP timeouts bound individual network phases and inactivity, not a strict wall-clock duration;
an in-flight synchronous request can finish after the task clock. Cancellation is cooperative
and prevents further interaction and judge attempts once observed.
Injected providers can implement `complete_before(..., deadline=...)` to bound blocking calls;
otherwise the runtime checks before and after their call and cannot interrupt its internals.
Provider usage is observed telemetry, not billing. Set `reasoning_effort = "none"` to send both
`reasoning_effort: "none"` and `thinking: {"type": "disabled"}` on every stage, including
reflection and scoring. Omitting this setting leaves reasoning behavior to the provider.
Support for these parameters depends on the endpoint; receiving reasoning content does not by
itself establish which parameter the backend honored.

Ordinary queries keep long-term notes within the exact authorized group set and allocate fresh
working memory per run. Benchmark trials isolate both layers by attempt and stage.
`benchmark --trials N` repeats each question independently. `--resume` reuses an interrupted
checkpoint only with matching inputs, workspace, deployment settings and runtime; credentials
can change without invalidating the checkpoint. See [benchmark evaluation](benchmarks.md).

Deployment and scoring settings are fixed inputs. Reusable harness files have separate bounded
schemas; see [architecture](architecture.md). A workspace must not be shared across unrelated
tenants without deployment-level access control.
