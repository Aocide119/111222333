<div align="center">

# EvoGroup

### 面向共享上下文群体智能体的自演化记忆 Harness

EvoGroup 是一个在保持基础模型不变的条件下，根据交互反馈改进证据驱动群体智能体行为的自演化记忆 Harness。

[论文](https://anonymous.4open.science/r/37883-8DC0/) · [代码](.) · [模型](#数据准备) · [数据集](#数据准备) · [English](README.md)

<img src="assets/evog-framework.png" alt="EvoGroup 框架图" width="1000">

</div>

## 动态

- 匿名发布：包含与论文对齐的 CLI、Benchmark 适配器、离线回归测试和可复现配置模板。

## 概述

EvoGroup 面向共享、多用户上下文运行。原始消息始终是证据层，学习记忆限定在授权群组内，工作记忆按题目隔离。初始 Harness 提供五个有界工具：`list_files`、`read_file`、`grep_search`、`write_file` 和 `create_file`。

整体流程包含四个阶段：

1. **交互：** 在明确选择的群组上回答问题，并记录回答、置信度、引用和工具轨迹。
2. **筛选：** 以 0.5 为置信度阈值组合外部正确性，得到 CC、CW、UC、UW 四类经验。
3. **分析：** 使用自适应渐进披露（APD）和归桶诊断检查相关证据，识别重复问题。
4. **演化：** 在 Representation、Operation、Policy 和 Intervention 四类接口中生成有证据关联的声明式修订，并对 checkpoint 进行版本化、激活、验证和回滚。

## 安装

需要 Python 3.11+ 和 [uv](https://docs.astral.sh/uv/)。

~~~bash
uv sync --locked --extra dev
uv run evog --help
~~~

公开接口是 `evog` 命令行程序，仓库不要求 SDK。

## 数据准备

### 群消息

JSONL 记录包含以下字段：

- 必填：`group_id`、`message_id`、`sender`、带时区的 `timestamp`、`text`；
- 可选：`reply_to` 和字符串类型的 `metadata`。

`examples/messages.jsonl` 是完整的最小输入示例。

### 论文 Benchmark

仓库不包含 Benchmark 数据，适配器直接读取 `--data-root` 指定的数据目录：

- EverMemBench 必须包含 `dataset/`；
- GroupMemBench 必须包含 `data/final/` 和 `questions/`。

使用仓库内的配置模板，并通过环境变量提供端点和密钥。仓库不保存私有端点或密钥。

~~~bash
export EVOG_BASE_URL=https://your-provider.example/v1
export EVOG_MODEL=your-model
export EVOG_API_KEY=your-key
~~~

验证 Benchmark 数据目录可读：

~~~bash
uv run evog benchmark list evermembench \
  --data-root /path/to/EverMemBench --topic 01 --limit 2

uv run evog benchmark list groupmembench \
  --data-root /path/to/GroupMemBench \
  --domain Finance --question-type multi_hop --limit 2
~~~

## 快速开始

### 离线演示

确定性的 fixture provider 会在不发送网络请求的情况下执行完整本地流程。

~~~bash
uv run evog --workspace /tmp/evog-demo demo
~~~

### 查询群组上下文

~~~bash
uv run evog init
uv run evog ingest examples/messages.jsonl
uv run evog groups
uv run evog ask "最新的发布计划是什么？" --group product
~~~

## 主结果复现

论文报告初始 checkpoint `$H_0$` 和选出的演化 checkpoint `$H_{*}$` 在 EverMemBench 上的 pass@1。固定题目划分、评测轮数和模型配置由论文协议定义；运行 campaign 前准备对应的 manifest 和数据目录。

~~~bash
uv run evog --config examples/paper.toml \
  --workspace .evog-campaign benchmark campaign evermembench \
  --data-root /path/to/EverMemBench \
  --manifest /path/to/paper-manifest.json \
  --evaluation-rounds 6 --seed 0 \
  --output results/evermem-campaign.json
~~~

论文报告的 EverMemBench pass@1（%）：

| 方法 | GPT-5.5 | DeepSeek-V4-Flash | GLM-5.1 |
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

## 自演化

CLI 将论文流程实现为一组确定性的状态转换：

~~~bash
uv run evog feedback RUN_ID rejected --source user
uv run evog select
uv run evog analyze
uv run evog propose ANALYSIS_ID
uv run evog apply PLAN_ID
~~~

`select` 记录符合条件的经验集合，`analyze` 生成带证据关联的发现，`propose` 生成有界修订计划，`apply` 激活通过验证的 checkpoint。`revisions` 列出 checkpoint，`rollback REVISION_ID` 恢复指定版本。

## 评测

Benchmark 适配器从数据目录读取题目 episode 和源语料。产品运行使用 `benchmark run` 或 `benchmark cycle`，论文 campaign 使用 `benchmark campaign`。结果以 JSON checkpoint 保存，包含题目 ID、部署配置、逐题结果和聚合指标。

评测层按题目隔离 Benchmark 记忆，并在激活前验证候选 checkpoint。标准答案只供评测器使用，不会进入智能体上下文。

## 消融实验

论文比较初始 Harness 与单个演化组件。仓库中的图片对应论文使用的组件级消融结果。

<p align="center">
  <img src="assets/harness-ablation.png" alt="EvoGroup Harness 组件消融" width="850">
</p>

## 迁移实验

演化 checkpoint 在没有继续演化或调参的条件下迁移到 GroupMemBench。论文报告的聚合结果如下：

| 模型 | `$H_0$` | 冻结后的 `$H_{*}$` | 增量 |
| --- | ---: | ---: | ---: |
| GPT-5.5 | 61.88% | 63.89% | +2.01 pp |
| DeepSeek-V4-Flash | 74.09% | 71.14% | −2.95 pp |

## 效率分析

在五轮匹配分析中，APD 相比完整轨迹分析平均减少 42.5% 的分析时间和 41.5% 的 token 使用量。

## 仓库结构

~~~text
src/evog/          CLI、运行时、记忆、分析、演化与模型传输
src/evog/prompts/  模型提示模板
examples/          演示输入及产品／研究配置模板
assets/            论文图片
tests/             离线回归测试
dist/              wheel 与源码压缩包
~~~

~~~bash
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
uv build
~~~
