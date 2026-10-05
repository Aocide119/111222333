<div align="center">

# EvoGroup

### Self-Evolving Memory Harness for Shared-Context Group Agents

**Interact · Analyze · Evolve**

<img alt="Python 3.11+" src="https://img.shields.io/badge/Python-3.11%2B-3776AB?style=flat&logo=python&logoColor=white">
<img alt="Interface: CLI" src="https://img.shields.io/badge/Interface-CLI-0891B2?style=flat">
<img alt="Version 0.1.0" src="https://img.shields.io/badge/Version-0.1.0-7C3AED?style=flat">

[Paper results](#paper-results) · [Method](#method) · [Quick start](#quick-start) · [中文](README_zh.md)

</div>

---

EvoGroup is a memory harness for agents that answer questions over shared, multi-user context. It keeps source messages as evidence, scopes learned memory to authorized groups, and improves the surrounding harness from interaction feedback while the base model remains fixed.

The release provides:

- group-scoped memory with traceable citations;
- five bounded evidence and navigation tools;
- confidence-guided trajectory analysis with Adaptive Progressive Disclosure (APD);
- bucketed diagnosis and four declarative revision interfaces;
- a CLI for local runs and adapters for EverMemBench and GroupMemBench.

## Method

<p align="center">
  <img src="assets/evog-framework.png" alt="EvoGroup self-evolution loop" width="1000">
</p>

1. **Interact.** The current harness $H_t$ answers a question over explicitly selected groups. Original conversation records remain the evidence source; long-term notes are scoped to the exact authorized group set and working memory is isolated per question. The initial harness exposes <code>list_files</code>, <code>read_file</code>, <code>grep_search</code>, <code>write_file</code>, and <code>create_file</code>.

2. **Select experience.** External correctness and model confidence are combined at a 0.5 threshold. The resulting CC, CW, UC, and UW cases distinguish routine success, unrecognized failure, fragile success, and recognized failure for analysis.

3. **Analyze evidence.** APD presents a compact trace structure before detailed events. The analysis agent opens only the relevant observations and records evidence ranges for each diagnosis. Bucketed analysis groups diagnoses by question semantics and retains coverage, counterevidence, and uncertainty.

4. **Evolve the harness.** The evolution agent emits bounded, evidence-linked revisions across Representation, Operation, Policy, and Intervention. Revisions are declarative, versioned, atomically activated, and reversible. The selected evolved checkpoint is denoted $H_{*}$.

## Paper results

The following values are reported in the EvoGroup paper. They are the paper's aggregate results, not a rerun performed by this repository snapshot.

### EverMemBench

Pass@1 (%); $H_0$ is the initial harness and $H_{*}$ is the selected evolved checkpoint.

| Method | GPT-5.5 | DeepSeek-V4-Flash | GLM-5.1 |
| --- | ---: | ---: | ---: |
| Mem0 | 56.50 | 43.71 | 52.42 |
| MemOS | 49.25 | 39.17 | 47.04 |
| A-MEM | 61.71 | 47.00 | 58.75 |
| MemRL | 61.54 | 55.92 | 57.33 |
| Codex | **85.67** | 82.63 | 77.88 |
| Claude Code | 84.58 | 85.75 | 76.46 |
| EvoGroup $H_0$ | 68.33 | 71.07 | 68.27 |
| EvoGroup $H_{*}$ | 84.52 | **89.58** | **83.63** |
| **$H_0 \rightarrow H_{*}$** | **+16.19 pp** | **+18.51 pp** | **+15.36 pp** |

The paper uses 2,400 questions, with 720 questions for evolution and 1,680 held out for evaluation.

### Frozen transfer to GroupMemBench

The evolved checkpoint is evaluated without further evolution or tuning.

| Backbone | $H_0$ | Frozen $H_{*}$ | Change |
| --- | ---: | ---: | ---: |
| GPT-5.5 | 61.88% | 63.89% | +2.01 pp |
| DeepSeek-V4-Flash | 74.09% | 71.14% | −2.95 pp |

### Analysis efficiency and ablation

Across five matched analysis iterations, APD reduces analysis time by 42.5% and token use by 41.5% relative to full-trace analysis.

<p align="center">
  <img src="assets/harness-ablation.png" alt="EvoGroup harness component ablation" width="850">
</p>

## Quick start

### Installation

Python 3.11+ and [uv](https://docs.astral.sh/uv/) are required.

~~~
uv sync --locked --extra dev
uv run evog --help
~~~

### Offline demo

The demo uses a deterministic fixture provider and makes no network requests. It runs the complete local flow: ingest, answer, feedback, analysis, revision proposal, and activation.

~~~
uv run evog --workspace /tmp/evog-demo demo
~~~

### Run on group messages

Set an OpenAI-compatible endpoint and model in the shell. The API key is read from <code>EVOG_API_KEY</code> and is not written to the repository.

~~~
export EVOG_BASE_URL=https://your-provider.example/v1
export EVOG_MODEL=your-model
export EVOG_API_KEY=your-key

uv run evog init
uv run evog ingest examples/messages.jsonl
uv run evog groups
uv run evog ask "What is the latest release schedule?" --group product
~~~

Each input record contains <code>group_id</code>, <code>message_id</code>, <code>sender</code>, a timezone-aware <code>timestamp</code>, and <code>text</code>. <code>reply_to</code> and string metadata are optional. <code>examples/messages.jsonl</code> is a complete minimal input.

### Analyze and evolve

~~~
uv run evog feedback RUN_ID rejected --source user
uv run evog select
uv run evog analyze
uv run evog propose ANALYSIS_ID
uv run evog apply PLAN_ID
~~~

The identifiers are emitted by the preceding commands. <code>revisions</code> lists checkpoints and <code>rollback REVISION_ID</code> restores a previous checkpoint.

## Paper benchmark adapters

Benchmark data is not included in this repository. The EverMemBench checkout must contain <code>dataset/</code>; the GroupMemBench checkout must contain <code>data/final/</code> and <code>questions/</code>.

List a deterministic EverMemBench sample:

~~~
uv run evog benchmark list evermembench \
  --data-root /path/to/EverMemBench --topic 01 --limit 2
~~~

Run the product cycle on a GroupMemBench slice:

~~~
uv run evog benchmark cycle groupmembench \
  --data-root /path/to/GroupMemBench \
  --domain Finance --question-type multi_hop --limit 2 \
  --output results.json
~~~

For the paper cohort, create the fixed 720/1,680 split and run the six-round evolution campaign:

~~~
uv run evog benchmark split evermembench \
  --data-root /path/to/EverMemBench --split-seed 0 \
  --output splits/evermem

uv run evog --config examples/paper.toml \
  --workspace .evog-campaign-0 benchmark campaign evermembench \
  --data-root /path/to/EverMemBench \
  --manifest splits/evermem/manifest.json \
  --evaluation-rounds 6 --seed 0 \
  --output results/campaign-0.json
~~~

<code>examples/benchmark.toml</code> contains the product profile. <code>examples/paper.toml</code> contains the research profile; provider endpoints and keys are supplied through environment variables.

## Repository layout

~~~
src/evog/     CLI, runtime, memory, analysis, evolution, and model transport
src/evog/prompts/  model-facing prompt templates
examples/     demo input and product/research configuration templates
assets/       paper figures
tests/        offline regression tests
~~~

## Verification

~~~
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
uv build
~~~
