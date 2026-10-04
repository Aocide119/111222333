# Release validation: 0.1.0

Local validation on October 3–4, 2026, on macOS.

## Software checks

| Check | Observed result |
| --- | --- |
| Python 3.11.15 | Runtime, analysis, revision, isolation and CLI regression suite passed |
| Ruff lint and formatting | Passed |
| CLI offline demo | Complete experience/revision loop completed |
| Wheel installed in a clean Python 3.11 environment | CLI offline demo completed using packaged prompt resources |
| Wheel and source distribution | Built successfully; all five role prompts included |
| Archive contents | No private workspaces, credentials, experimental archives, virtual environments or bytecode |
| Credential-shaped string scan | No provider-token or GitHub-token patterns found in release source |
| Runtime and revision regression suite | 184 tests passed |

The regression suite checks atomic/idempotent ingestion, source conflicts, timezone handling,
explicit group scope, note isolation, traversal and symlink escapes, pagination and truncated
evidence, delivered-citation validation, reflection failures, budget boundaries, feedback cells,
unfinished/incident/contract-failure separation, timezone preservation and UTC ordering,
metadata-aware search, progressive trace ranges, forged evidence, four revision surfaces,
stale plans, business-validator failure, rollback and safe provider failures. Benchmark checks
cover corpus conversion, answer-key isolation, episode selection, official verdict parsing,
judge failures, paired transitions and candidate activation.

Additional checks cover the fixed 20-request ceiling, both memory layers, bounded search ledgers,
multi-trial question diagnosis, included-evidence synthesis, independent revision-stage findings,
per-change manifests, evaluation idempotence, fully scored best-ever selection, atomic recovery
and resumable benchmark checkpoints.

## Live validation scope

An EverMemBench baseline evaluated 100 selected questions with `deepseek-v4.1-flash`
and four workers on October 4. It completed with 52 official passes, 18 wrong answers and 30
unscored questions: 25 output-contract failures, four provider failures and one unavailable
judge. No question exited on the context budget. Its duration was approximately 38 minutes
41 seconds. That run used a 60-tool-request configuration. It does not validate the current
20-request ceiling or the subsequent analysis, revision and recovery changes. This selected
sample is not a full-dataset result, and live analysis or evolution was not run on this batch.
All requests carried disable-reasoning parameters, but 648 responses returned reasoning content;
the backend's reasoning behavior remains unverified. That historical run used the former JSON
answer contract; current runs use the plain-text answer protocol and retain the raw response for
audit.

Current analysis, candidate activation and recovery are verified with deterministic offline
providers. The historical 100-question JSON-contract run did not exercise live analysis or
evolution; the separate current-protocol smoke cycle below does. Software tests and paper results
are separate evidence; neither establishes deployment quality.

Separately, the current plain-text protocol was exercised in a live EverMemBench topic-01 cycle
with 50 questions, four workers, `deepseek-v4.1-flash`, a 20-request ceiling and reasoning
disable parameters. The baseline passed 36/50 (two contract failures); the paired candidate
passed 41/50 (two contract failures), with seven fail-to-pass and two pass-to-fail transitions.
The candidate was activated under the same-sample `net_improvement` policy. This is a smoke-scale
product-flow check, not a reproduction of the paper's 720-question, three-seed results, and the
two contract failures remain a release-quality limitation.

The release source scan found no provider-token, private-key or authenticated-URL pattern. Git
history still records the repository owner's commit name and email; an anonymous submission must
use a separately rewritten/publication-safe history before publishing.

The release contains no benchmark data, question files or gold answers. Dataset and model terms
must be reviewed at download/deployment time; only the adapter and the stated third-party notices
are distributed here.

The CI matrix targets Python 3.11–3.13 on Ubuntu and Windows. Hosted CI has not been run during
local validation. Application quality requires representative deployment checks; arbitrary
Python code evolution is outside the declarative revision surfaces.
