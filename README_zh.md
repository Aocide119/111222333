<div align="center">

# EvoGroup

### 面向共享上下文群体智能体的自演化记忆 Harness

EvoGroup 是一个在保持基础模型不变的条件下，根据交互反馈改进证据驱动群体智能体行为的自演化记忆 Harness。

<img src="assets/evog-framework.png" alt="EvoGroup 框架图" width="1000">

</div>

## 动态

- 匿名发布：包含与论文对齐的 CLI、Benchmark 适配器、离线回归测试和可复现配置模板。

## 概述

EvoGroup 面向共享、多用户上下文运行。原始消息始终是证据层，学习记忆限定在授权群组内，工作记忆按题目隔离。初始 Harness 提供五个有界工具：`list_files`、`read_file`、`grep_search`、`write_file` 和 `create_file`。

## 🔄 自演化循环

论文将 EvoGroup 定义为四阶段的 self-evolution loop：interaction、trajectory selection、analysis 和 harness revision。基础模型保持不变，Harness 从 $H_0$ 开始逐步产生并评测新的 checkpoint。

### 1. 🧭 Interaction

Group Agent 使用当前 Harness $H_t$ 在共享群组上下文上回答问题。交互记录问题、观察、推理或工具操作、最终回答和主观置信度。初始 cold start 只包含五个工具——`list_files`、`read_file`、`grep_search`、`write_file` 和 `create_file`——技能库与记忆均为空。

当置信度不高于论文设定的 0.5 阈值时，Group Agent 会在外部监督结果揭示前生成 self-reflection。该反思只作为诊断证据，不会改变正式正确性标签。

### 2. 🎯 Trajectory Selection

官方评测器提供回答正确性信号。EvoGroup 将它与置信度结合，按论文定义把轨迹分为四组：

| 组别 | 定义 | 演化用途 |
| --- | --- | --- |
| CC | confident-correct | 常规成功行为 |
| CW | confident-wrong | 未识别的推理失败或回答偏差 |
| UC | unconfident-correct | 需要额外支持的脆弱成功 |
| UW | unconfident-wrong | 带有反思证据的已识别失败 |

进入分析集合的是 CW、UC 和 UW。CC 作为成功参考保留，不作为优先诊断对象。筛选结果会在分析开始前写入本轮归档。

### 3. 🔬 Analysis

论文将这一阶段称为 **Trajectory Analysis from Details to Patterns**。Analysis Agent 首先使用 **Adaptive Progressive Disclosure（APD）**：先查看紧凑的结构视图，定位可能出问题的阶段，再按需打开对应的观察、操作、工具结果或通信记录。

每条入选轨迹都会生成 detailed rationale 和 condensed rationale，保留失败位置、归因原因和可执行含义。随后，**Bucketed Analysis** 按推断出的查询语义组织 condensed rationale，先分析桶内模式，再比较跨桶模式，形成供修订使用的 high-level findings。

### 4. 🛠️ Harness Revision

论文将这一阶段称为 **Multi-Interface Harness Revision**。Evolve Agent 根据 condensed rationale 和 high-level findings，在四类接口上提出变更：

- **Representation：** 信息如何保存、关联和呈现；
- **Operation：** 可执行动作及其实现；
- **Intervention：** 执行边界上的检查、转换和约束；
- **Policy：** 控制选择什么、何时执行以及执行顺序的规则。

Harness 由 **Memory、Tools、Skills、Prompt、Middleware** 五类文件组件组成。Evolve Agent 在候选工作目录中编辑组件，包括工具和中间件的 Python 实现，并为每项修改关联已检查的证据。组件在独立工作目录和子进程中运行；目录隔离不等同于操作系统权限沙箱。

候选 $H_{t+1}$ 在下一轮执行并评测前都只是提案。每项变更都带有证据、预期修复和回归风险清单。最终从已评测 checkpoint 中选择 $H_{*}$；没有候选通过验证时，保留当前 Harness。

## 安装

需要 Python 3.11+ 和 [uv](https://docs.astral.sh/uv/)。

~~~bash
uv sync --locked --extra dev
uv run evog --help
~~~

公开接口是 `evog` 命令行程序。

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

论文报告初始 checkpoint $H_0$ 和选出的演化 checkpoint $H_{*}$ 在 EverMemBench 上的 pass@1。固定题目划分、评测轮数和模型配置由论文协议定义；运行 campaign 前准备对应的 manifest 和数据目录。

~~~bash
uv run evog --config examples/paper.toml \
  --workspace .evog-campaign benchmark campaign evermembench \
  --data-root /path/to/EverMemBench \
  --manifest /path/to/paper-manifest.json \
  --evaluation-rounds 6 --seed 0 \
  --output results/evermem-campaign.json
~~~


## 自演化

CLI 将论文流程实现为一组确定性的状态转换：

~~~bash
uv run evog feedback RUN_ID rejected --source user
uv run evog select
uv run evog analyze
uv run evog propose ANALYSIS_ID
uv run evog apply PLAN_ID
~~~

`select` 记录符合条件的经验集合，`analyze` 生成带证据关联的发现，`propose` 编辑并验证候选组件包，`apply` 激活通过验证的 checkpoint。`revisions` 列出 checkpoint，`rollback REVISION_ID` 恢复指定版本。

## 评测

Benchmark 适配器从数据目录读取题目 episode 和源语料。产品运行使用 `benchmark run` 或 `benchmark cycle`，论文 campaign 使用 `benchmark campaign`。结果以 JSON checkpoint 保存，包含题目 ID、部署配置、逐题结果和聚合指标。

评测层按题目隔离 Benchmark 记忆，并在激活前验证候选 checkpoint。标准答案只供评测器使用，不会进入智能体上下文。

## 仓库结构

~~~text
EvoGroup/
├── src/evog/
│   ├── cli.py                    # 命令行入口
│   ├── app.py                    # 命令调度
│   ├── agents/                   # 交互、分析与演化
│   │   ├── interaction.py        # 答题、预算与反馈前反思
│   │   ├── analysis.py           # 轨迹筛选、APD 与归桶综合
│   │   ├── evolution.py          # 基于证据的修订与激活
│   │   └── prompts/              # 反思、分析、综合与演化 prompt
│   ├── harness/                  # 组件契约、快照与执行
│   │   ├── schema.py             # 五组件包校验
│   │   ├── workspace.py          # 候选文件编辑与改动登记
│   │   ├── executor.py           # 独立子进程与工作目录
│   │   ├── memory.py             # 按轮冻结记忆与暂存笔记
│   │   └── base/                 # 初始 Harness
│   │       ├── harness.toml      # 五类组件入口
│   │       ├── memory/           # 策略、布局、表示与模板
│   │       ├── tools/            # 注册表、工具描述与 Python 实现
│   │       ├── skills/           # 初始为空；演化添加 SKILL.md 与参考文件
│   │       ├── prompt/           # system 与 group prompt
│   │       └── middleware/       # 注册表与执行 hooks
│   ├── evaluation/              # Benchmark 适配、裁判、指标与划分
│   │   ├── runner.py             # Benchmark 运行与配对候选评测
│   │   ├── campaign.py           # 演化轮次与 checkpoint 选择
│   │   └── frozen.py             # 留出评测
│   └── core/                    # 配置、模型调用、数据契约与存储
├── examples/                    # 输入数据与配置模板
│   ├── messages.jsonl          # 最小群消息输入
│   ├── benchmark.toml          # 产品配置模板
│   ├── paper.toml              # 论文配置模板
│   └── run_demo.sh             # 离线演示脚本
├── assets/                      # 论文图片
├── tests/                       # 回归测试
├── dist/                        # wheel 与源码归档
├── pyproject.toml               # 包元数据与 CLI 入口
└── uv.lock                      # 依赖锁文件
~~~

~~~bash
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
uv build
~~~
