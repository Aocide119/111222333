<div align="center">

# EvoGroup

### 面向共享上下文群体智能体的自演化记忆 Harness

**交互 · 分析 · 演化**

<img alt="Python 3.11+" src="https://img.shields.io/badge/Python-3.11%2B-3776AB?style=flat&logo=python&logoColor=white">
<img alt="使用方式：CLI" src="https://img.shields.io/badge/Interface-CLI-0891B2?style=flat">
<img alt="版本 0.1.0" src="https://img.shields.io/badge/Version-0.1.0-7C3AED?style=flat">

[论文结果](#论文结果) · [方法](#方法) · [快速开始](#快速开始) · [English](README.md)

</div>

---

EvoGroup 是一个面向共享、多用户上下文的记忆 Harness。系统保留原始消息作为证据，将学习记忆限定在已授权群组内，并根据交互反馈改进外围 Harness，同时保持基础模型不变。

当前发布包含：

- 带引用的群组级记忆；
- 五个有界的证据与导航工具；
- 结合置信度的轨迹分析与自适应渐进披露（APD）；
- 归桶诊断与四类声明式修订接口；
- 用于本地运行以及 EverMemBench、GroupMemBench 的命令行适配器。

## 方法

<p align="center">
  <img src="assets/evog-framework.png" alt="EvoGroup 自演化流程" width="1000">
</p>

1. **交互。** 当前 Harness $H_t$ 在明确选择的群组上回答问题。原始对话记录始终是事实证据；长期记忆限定在完全相同的授权群组集合内，工作记忆按题目隔离。初始 Harness 提供 <code>list_files</code>、<code>read_file</code>、<code>grep_search</code>、<code>write_file</code> 和 <code>create_file</code>。

2. **筛选经验。** 系统以 0.5 为置信度阈值，组合外部正确性与模型置信度，将样本分为 CC、CW、UC、UW，分别表示常规成功、未识别失败、脆弱成功和已识别失败。

3. **分析证据。** APD 先展示轨迹结构，再按需打开相关事件。每个诊断都记录实际检查的证据范围。归桶分析按问题语义组织诊断，并保留覆盖范围、反例和不确定性。

4. **演化 Harness。** 演化智能体在 Representation、Operation、Policy 和 Intervention 四类接口内生成有证据关联的有界修订。修订采用声明式配置，支持版本化、原子激活和回滚；选出的演化 checkpoint 记为 $H_{*}$。

## 论文结果

以下数值来自 EvoGroup 论文，是论文报告的聚合结果，不是当前仓库重新运行得到的结果。

### EverMemBench

Pass@1（%）；$H_0$ 为初始 Harness，$H_{*}$ 为选出的演化 checkpoint。

| 方法 | GPT-5.5 | DeepSeek-V4-Flash | GLM-5.1 |
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

论文使用 2,400 道题目，其中 720 道用于演化，1,680 道留作评测。

### 冻结迁移至 GroupMemBench

演化 checkpoint 在没有继续演化或调参的条件下进行评测。

| 模型 | $H_0$ | 冻结后的 $H_{*}$ | 增量 |
| --- | ---: | ---: | ---: |
| GPT-5.5 | 61.88% | 63.89% | +2.01 pp |
| DeepSeek-V4-Flash | 74.09% | 71.14% | −2.95 pp |

### 分析效率与消融

在五轮匹配分析中，APD 相比完整轨迹分析平均减少 42.5% 的分析时间和 41.5% 的 token 使用量。

<p align="center">
  <img src="assets/harness-ablation.png" alt="EvoGroup Harness 组件消融" width="850">
</p>

## 快速开始

### 安装

需要 Python 3.11+ 和 [uv](https://docs.astral.sh/uv/)。

~~~
uv sync --locked --extra dev
uv run evog --help
~~~

### 离线演示

演示使用确定性的 fixture provider，不发送网络请求，完整执行导入、回答、反馈、分析、修订提案和激活流程。

~~~
uv run evog --workspace /tmp/evog-demo demo
~~~

### 运行群消息

在 shell 中设置 OpenAI 兼容端点和模型。API Key 从 <code>EVOG_API_KEY</code> 读取，不会写入仓库。

~~~
export EVOG_BASE_URL=https://your-provider.example/v1
export EVOG_MODEL=your-model
export EVOG_API_KEY=your-key

uv run evog init
uv run evog ingest examples/messages.jsonl
uv run evog groups
uv run evog ask "最新的发布计划是什么？" --group product
~~~

每条输入记录包含 <code>group_id</code>、<code>message_id</code>、<code>sender</code>、带时区的 <code>timestamp</code> 和 <code>text</code>；<code>reply_to</code> 与字符串类型的 metadata 为可选字段。<code>examples/messages.jsonl</code> 是完整的最小输入示例。

### 分析与演化

~~~
uv run evog feedback RUN_ID rejected --source user
uv run evog select
uv run evog analyze
uv run evog propose ANALYSIS_ID
uv run evog apply PLAN_ID
~~~

各 ID 由前一条命令输出。<code>revisions</code> 列出 checkpoint，<code>rollback REVISION_ID</code> 恢复指定 checkpoint。

## 论文 Benchmark 适配

仓库不包含 Benchmark 数据。EverMemBench 数据目录必须包含 <code>dataset/</code>；GroupMemBench 数据目录必须包含 <code>data/final/</code> 和 <code>questions/</code>。

列出一个确定性的 EverMemBench 小样本：

~~~
uv run evog benchmark list evermembench \
  --data-root /path/to/EverMemBench --topic 01 --limit 2
~~~

对 GroupMemBench 的 Finance/multi_hop 子集运行产品循环：

~~~
uv run evog benchmark cycle groupmembench \
  --data-root /path/to/GroupMemBench \
  --domain Finance --question-type multi_hop --limit 2 \
  --output results.json
~~~

复现论文规模时，先生成固定的 720/1,680 划分，再运行六轮演化：

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

<code>examples/benchmark.toml</code> 是产品配置模板；<code>examples/paper.toml</code> 是研究配置模板，端点和密钥通过环境变量提供。

## 仓库结构

~~~
src/evog/     CLI、运行时、记忆、分析、演化与模型传输
src/evog/prompts/  模型提示模板
examples/     演示输入及产品／研究配置模板
assets/       论文图片
tests/        离线回归测试
~~~

## 验证

~~~
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
uv build
~~~
