# Benchmark evaluation

EvoG evaluates group-context answering on EverMemBench and GroupMemBench. The CLI supports
baseline runs and evolution cycles with official scoring, isolated memory sessions and paired
candidate validation.

## Data and isolation

The readers support the checked out layouts below.

| Benchmark | Source corpus | Questions | Question kinds |
| --- | --- | ---: | --- |
| EverMemBench | `dataset/<topic>/dialogue.json` + `qa_<topic>.json` | 2,400 | single hop, multi hop, temporal, constraint, proactivity, update, style, skill, role |
| GroupMemBench | `data/final/<Domain>/*.json` + `questions/<Domain>/*.jsonl` | 745 | multi hop, knowledge update, temporal, implicit user, term ambiguity, abstention |

Gold answers, evidence coordinates and judge output stay in the benchmark runner.
The answer prompt receives the question, an optional asking-user identity and answer options.
Every trial and repeated attempt after interruption gets a fresh session for both note layers, so navigation
notes cannot leak between questions, trials, baseline and candidate stages. The product's
ordinary queries keep group-scoped long-term notes between queries.

The source conversion retains speaker, timestamp, reply links and benchmark metadata. EverMemBench
keeps its dated/group/message layout; GroupMemBench keeps channel, role, phase and decision fields.
Naive timestamps use `--timezone` (default `UTC`); original offsets are retained when present.

The repository ships adapters only; it does not redistribute either benchmark. The EverMemBench
dataset card declares Apache-2.0 for its downloaded dataset. GroupMemBench's checked-out release
does not carry an explicit license file, so obtain permission or confirm the upstream terms before
redistributing its data or derived traces. Model access is also external: no model weights are
included, and each provider's terms apply to prompts, outputs and retained traces.

## Commands

List a deterministic sample without calling a model:

```bash
uv run evog --workspace .evog --config benchmark.toml benchmark list evermembench \
  --data-root /path/to/EverMemBench --topic 01 --question-type F_MH --limit 2
```

Run a baseline sample with an OpenAI-compatible endpoint:

```bash
export EVOG_API_KEY='…'
uv run evog --workspace .evog --config examples/benchmark.toml benchmark run evermembench \
  --data-root /path/to/EverMemBench --topic 01 --limit 4 \
  --episode-file splits/evermem-smoke.txt --output results/evermem-baseline.json
```

Run the full product loop. `cycle` performs baseline interaction, official benchmark scoring,
confidence/feedback selection, APD diagnosis, finding synthesis, revision proposal, candidate
validation and paired candidate evaluation. `candidate_acceptance = "net_improvement"` requires
more repaired than regressed interactions; `"no_regression"` requires repairs and zero regressions.
Both reject unresolved judge/infrastructure outcomes and unchanged scores. Budget and output-contract
failures are explicit execution failures for transitions, while official verdicts remain unchanged.
`benchmark_concurrency` sets answer/scoring workers; set it and `analysis_concurrency` to 4 for
four-worker execution. Results are checkpointed in deterministic episode/trial order.

```bash
uv run evog --workspace .evog --config benchmark.toml benchmark cycle groupmembench \
  --data-root /path/to/GroupMemBench-main --domain Finance \
  --question-type multi_hop --limit 2 \
  --validation-episode-file splits/groupmem-heldout.txt \
  --output results/groupmem-cycle.json
```

Use `--all` only after a smoke run. The 2,400 EverMemBench questions and 745 GroupMemBench
questions are model- and budget-intensive. Pass an episode-ID file with `--episode-file`, using
one ID per line; blank lines and lines starting with `#` are ignored. IDs are checked before
limiting so a typo cannot silently select a different set.

Use `--iterations N` (1–10) with `cycle` for bounded successive revisions on the same evolution
sample. Each accepted candidate feeds the next analysis; rejection or no supported change stops
the loop. Disjoint validation files are supported for a single cycle. Repeated cycles require fresh
validation runs instead of reusing a held-out set. Same-sample regression is not held-out evaluation.
Per-iteration reports preserve attempted changes, outcomes and associated effects.

Use `--trials N` (1–10) with `run` or `cycle` for independent repetitions of every selected
question. Analysis creates one job per exact question and authorized group scope, with all
its trials available for inspection. Repeated findings require evidence from different questions.
Baseline and candidate comparison pairs `(episode_id, trial_index)` exactly. Aggregate pass rate
uses trials; `question_pass_rate` counts questions for which every trial passed and is not pass@k.

Resume an interrupted run using the same command, output path and `--resume`:

```bash
uv run evog --workspace .evog --config benchmark.toml benchmark cycle evermembench \
  --data-root /path/to/EverMemBench --topic 01 --limit 4 --trials 2 \
  --output results/evermem-cycle.json --resume
```

Resume retains checkpointed trial results, diagnoses, plans and completed iterations. Work that
was in flight but not checkpointed can run again in a fresh session; its earlier traces remain
available in SQLite. Changing datasets, selection, trial count, settings, runtime or workspace
rejects the checkpoint. API credentials are excluded from the signature. Completed checkpoints
are returned without new model calls. An unrelated revision change requires a new run.

After rejection with an observed regression, recovery chooses the highest fully scored comparable
version that was previously activated, with an atomic active-state check. A staged candidate is
not recoverable. The result records the chosen version and per-change associations; evaluation
retries preserve a single measurement record.

## Judge protocols

EverMemBench multiple-choice answers are normalized to option letters and scored locally.
Other EverMemBench rows use its official `CORRECT`/`WRONG` JSON judge prompt. GroupMemBench
uses the official `Final: Correct` / `Final: Incorrect` parser and prompt. A judge transport failure
or unparseable reply is recorded as `passed: null`, never as a wrong answer.

Judge prompts follow EverMemBench's `eval/config/prompts.yaml` and GroupMemBench's
`prompts/hipporag_judge_system.txt`.

Each result JSON records the complete non-secret deployment settings and runtime source
fingerprint alongside the exact corpus fingerprint and model name. It also contains baseline/candidate run
identifiers, token usage, tool-call count, per-question status, judge response and paired
transition counts. It deliberately omits the question text and gold answer; the checkpoint is
still operationally sensitive because answers, raw model responses and run identifiers can expose
private benchmark content. Keep result files private unless the dataset terms permit release.
API keys are read from `EVOG_API_KEY` and are never persisted in the result or trace.
