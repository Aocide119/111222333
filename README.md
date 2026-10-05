<div align="center">

# EvoGroup

### Self-Evolving Memory Harness for Shared-Context Group Agents

EvoGroup is a self-evolving memory harness that improves evidence-grounded group-agent behavior from interaction feedback while keeping the base model fixed.

[Paper](https://anonymous.4open.science/r/37883-8DC0/) · [Code](.) · [Models](#data-preparation) · [Dataset](#data-preparation) · [中文](README_zh.md)

<img src="assets/evog-framework.png" alt="EvoGroup framework" width="1000">

</div>

## News

- Anonymous release: paper-aligned CLI, benchmark adapters, offline regression tests, and reproducible configuration templates.

## Overview

EvoGroup operates over shared, multi-user context. Source messages remain the evidence layer, learned memory is scoped to authorized groups, and working memory is isolated per question. The initial harness exposes five bounded tools: `list_files`, `read_file`, `grep_search`, `write_file`, and `create_file`.

The method has four stages:

1. **Interact:** answer a question over explicitly selected groups and record the answer, confidence, citations, and tool trace.
2. **Select:** combine external correctness with a 0.5 confidence threshold to identify CC, CW, UC, and UW experience cases.
3. **Analyze:** use Adaptive Progressive Disclosure (APD) and bucketed diagnosis to inspect relevant evidence and identify recurring problems.
4. **Evolve:** emit evidence-linked, declarative revisions across Representation, Operation, Policy, and Intervention; version, activate, validate, and roll back each checkpoint.

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
src/evog/          CLI, runtime, memory, analysis, evolution, and model transport
src/evog/prompts/  model-facing prompt templates
examples/          demo input and product/research configuration templates
assets/            paper figures
tests/             offline regression tests
dist/              built wheel and source archive
~~~

~~~bash
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
uv build
~~~
