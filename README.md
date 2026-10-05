<div align="center">

# EvoGroup

### Self-Evolving Memory Harness for Shared-Context Group Agents

EvoGroup is a self-evolving memory harness that improves evidence-grounded group-agent behavior from interaction feedback while keeping the base model fixed.

<img src="assets/evog-framework.png" alt="EvoGroup framework" width="1000">

</div>

## News

- Anonymous release: paper-aligned CLI, benchmark adapters, offline regression tests, and reproducible configuration templates.

## Overview

EvoGroup operates over shared, multi-user context. Source messages remain the evidence layer, learned memory is scoped to authorized groups, and working memory is isolated per question. The initial harness exposes five bounded tools: `list_files`, `read_file`, `grep_search`, `write_file`, and `create_file`.

## 🔄 Self-Evolution Loop

The paper defines EvoGroup as a four-stage self-evolution loop: interaction, trajectory selection, analysis, and harness revision. The base model stays fixed while the Harness changes from `$H_0$` through evaluated checkpoints.

### 1. 🧭 Interaction

The Group Agent uses the current Harness `$H_t$` to answer a query over shared group context. The interaction records the query, observations, reasoning or tool-use actions, final answer, and subjective confidence. The initial cold start contains five tools—`list_files`, `read_file`, `grep_search`, `write_file`, and `create_file`—with empty skills and memory.

When confidence is at or below the paper's threshold of 0.5, the Group Agent produces a self-reflection before external supervision is revealed. This reflection is diagnostic evidence; it does not change the official correctness label.

### 2. 🎯 Trajectory Selection

The official evaluator supplies the answer-correctness signal. EvoGroup combines it with confidence-guided experience selection and partitions trajectories into four paper-defined groups:

| Group | Definition | Use in evolution |
| --- | --- | --- |
| CC | confident-correct | routine successful behavior |
| CW | confident-wrong | undetected reasoning failure or answer bias |
| UC | unconfident-correct | fragile success that needs support |
| UW | unconfident-wrong | recognized failure with reflection evidence |

The selected set contains CW, UC, and UW trajectories. CC remains a success reference and is not prioritized for diagnosis. The selection result is stored with the round archive before analysis begins.

### 3. 🔬 Analysis

The paper calls this stage **Trajectory Analysis from Details to Patterns**. The Analysis Agent first applies **Adaptive Progressive Disclosure (APD)**: it sees a compact structural view, locates a potentially problematic stage, and opens the corresponding observations, actions, tool results, or communication records only when needed.

Each selected trajectory receives a detailed rationale and a condensed rationale that retain the failure location, attributed cause, and actionable implication. **Bucketed Analysis** then groups condensed rationales by inferred query semantics, analyzes patterns within each bucket, and compares patterns across buckets to produce high-level findings for revision.

### 4. 🛠️ Harness Revision

The paper calls this stage **Multi-Interface Harness Revision**. The Evolve Agent converts condensed rationales and high-level findings into changes at four interfaces:

- **Representation:** how information is stored, associated, or presented;
- **Operation:** executable actions and their implementations;
- **Intervention:** checks, transformations, or constraints at execution boundaries;
- **Policy:** rules controlling what to select, when to execute it, and in what order.

Operation revisions may register additional bounded query tools through `operations.json`. Each
entry is compiled by the fixed runtime from a declared search schema; it cannot execute Python,
shell commands, imports, or arbitrary runtime code.

A candidate `$H_{t+1}$` remains a proposal until it is executed and evaluated in the next round. Each change carries a manifest with its evidence, predicted repair, and regression risks. The best evaluated checkpoint is selected as `$H_{*}$`; if no candidate passes validation, the current Harness is retained.

## Installation

Python 3.11+ and [uv](https://docs.astral.sh/uv/) are required.

~~~bash
uv sync --locked --extra dev
uv run evog --help
~~~

The public interface is the `evog` command-line program. The repository does not require an SDK.

## Data Preparation

### Group messages

Prepare JSONL records with the following fields:

- required: `group_id`, `message_id`, `sender`, timezone-aware `timestamp`, `text`;
- optional: `reply_to` and string-valued `metadata`.

`examples/messages.jsonl` is a complete minimal input.

### Paper benchmarks

Benchmark data is not bundled. The adapter reads the dataset checkout directly from `--data-root`:

- EverMemBench requires `dataset/`;
- GroupMemBench requires `data/final/` and `questions/`.

Use the checked-in configuration templates with provider credentials supplied through environment variables. No private endpoint or key is stored in this repository.

~~~bash
export EVOG_BASE_URL=https://your-provider.example/v1
export EVOG_MODEL=your-model
export EVOG_API_KEY=your-key
~~~

Verify that a benchmark checkout is readable:

~~~bash
uv run evog benchmark list evermembench \
  --data-root /path/to/EverMemBench --topic 01 --limit 2

uv run evog benchmark list groupmembench \
  --data-root /path/to/GroupMemBench \
  --domain Finance --question-type multi_hop --limit 2
~~~

## Quick Start

### Offline demo

The deterministic fixture provider runs the complete local flow without network requests.

~~~bash
uv run evog --workspace /tmp/evog-demo demo
~~~

### Query a group context

~~~bash
uv run evog init
uv run evog ingest examples/messages.jsonl
uv run evog groups
uv run evog ask "What is the latest release schedule?" --group product
~~~

## Reproducing Main Results

The paper reports EverMemBench pass@1 for the initial checkpoint `$H_0$` and the selected evolved checkpoint `$H_{*}$`. The paper protocol defines the fixed cohort, split, evaluation rounds, and model settings. Prepare the corresponding manifest and dataset checkout before running the campaign.

~~~bash
uv run evog --config examples/paper.toml \
  --workspace .evog-campaign benchmark campaign evermembench \
  --data-root /path/to/EverMemBench \
  --manifest /path/to/paper-manifest.json \
  --evaluation-rounds 6 --seed 0 \
  --output results/evermem-campaign.json
~~~

Reported EverMemBench pass@1 (%):

| Method | GPT-5.5 | DeepSeek-V4-Flash | GLM-5.1 |
| --- | ---: | ---: | ---: |
| Mem0 | 56.50 | 43.71 | 52.42 |
| MemOS | 49.25 | 39.17 | 47.04 |
| A-MEM | 61.71 | 47.00 | 58.75 |
| MemRL | 61.54 | 55.92 | 57.33 |
| Codex | **85.67** | 82.63 | 77.88 |
| Claude Code | 84.58 | 85.75 | 76.46 |
| EvoGroup `$H_0$` | 68.33 | 71.07 | 68.27 |
| EvoGroup `$H_{*}$` | 84.52 | **89.58** | **83.63** |
| **`$H_0$` → `$H_{*}$`** | **+16.19 pp** | **+18.51 pp** | **+15.36 pp** |

## Self-Evolution

The CLI exposes the paper workflow as a sequence of deterministic state transitions:

~~~bash
uv run evog feedback RUN_ID rejected --source user
uv run evog select
uv run evog analyze
uv run evog propose ANALYSIS_ID
uv run evog apply PLAN_ID
~~~

`select` records the eligible experience set, `analyze` creates evidence-linked findings, `propose` creates a bounded revision plan, and `apply` activates a validated checkpoint. `revisions` lists checkpoints; `rollback REVISION_ID` restores one.

## Evaluation

The benchmark adapters load question episodes and source corpora from the supplied dataset root. Product runs use `benchmark run` or `benchmark cycle`; paper campaigns use `benchmark campaign`. Results are written as JSON checkpoints with the selected episode IDs, deployment settings, per-question outcomes, and aggregate metrics.

The evaluation layer keeps benchmark memory isolated per question and validates candidate checkpoints before activation. Standard answers are used by the evaluator and are not included in agent context.

## Ablation Study

The paper compares the initial harness with individual evolved components. The released figure records the component-level ablation used in the paper.

<p align="center">
  <img src="assets/harness-ablation.png" alt="EvoGroup harness component ablation" width="850">
</p>

## Transfer Experiment

The evolved checkpoint is transferred to GroupMemBench without further evolution or tuning. Reported aggregates are:

| Backbone | `$H_0$` | Frozen `$H_{*}$` | Change |
| --- | ---: | ---: | ---: |
| GPT-5.5 | 61.88% | 63.89% | +2.01 pp |
| DeepSeek-V4-Flash | 74.09% | 71.14% | −2.95 pp |

## Efficiency Analysis

Across five matched analysis iterations, APD reduces analysis time by 42.5% and token use by 41.5% relative to full-trace analysis.

## Repository Structure

~~~text
EvoGroup/
├── src/evog/
│   ├── cli.py                  # evog command-line entry point
│   ├── app.py                  # command orchestration
│   ├── runtime.py              # group interaction and trace creation
│   ├── memory.py               # scoped long-term and working memory
│   ├── tools.py                # bounded evidence and navigation tools
│   ├── analysis.py             # selection, APD, diagnosis, and synthesis
│   ├── evolution.py            # revision plans and checkpoint activation
│   ├── harness.py              # four declarative revision interfaces
│   ├── benchmarks.py           # product benchmark runs and cycles
│   ├── campaign.py             # paper evolution campaigns
│   ├── frozen_evaluation.py    # held-out checkpoint evaluation
│   ├── benchmark_*.py          # loading, judging, metrics, splits, recovery
│   ├── providers.py            # OpenAI-compatible model transport
│   ├── store.py                # messages, traces, feedback, and revisions
│   └── prompts/                # group, analysis, synthesis, and evolution prompts
├── examples/
│   ├── messages.jsonl          # minimal group-message input
│   ├── benchmark.toml          # product configuration template
│   ├── paper.toml              # research configuration template
│   └── run_demo.sh             # offline demo launcher
├── assets/                     # framework and ablation figures
├── tests/                      # offline regression tests
├── dist/                       # built wheel and source archive
├── pyproject.toml              # package metadata and CLI entry point
└── uv.lock                    # locked Python dependencies
~~~

~~~bash
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
uv build
~~~
