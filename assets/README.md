# Paper figures and result sources

Figures from **EvoGroup: Self-Evolving Memory Harness for Shared-Context Group Agents**.

| Asset | Paper source | Content |
| --- | --- | --- |
| [evog-framework.png](evog-framework.png) | `figures/EvoWorkspace_Self_Evolution_Loop.pdf` | Interaction, trajectory selection, analysis and harness revision |
| [harness-ablation.png](harness-ablation.png) | `figures/harness_ablation.pdf` | Component-level ablation results |

| README result | Paper source |
| --- | --- |
| EverMemBench pass@1 | `tables/main_tables.tex`, Average column |
| Frozen GroupMemBench transfer | `tables/ood.tex`, Average column |
| APD time and token reductions | `iclr2027_conference.tex`, Analysis of Adaptive Progressive Disclosure |
| Evaluation split and update count | `iclr2027_conference.tex`, main-results text |

The READMEs use $H_{*}$ for the selected final checkpoint. Original figure labels are preserved.
The framework's correctness signal is supplied by benchmark evaluation or categorical user/business
feedback in the CLI. Low confidence means at or below 0.5 in the implementation.

Reported results retain the main table's aggregation. The ablation figure uses 71.07% → 89.58%,
with Prompt +7.41 pp and Tools +10.90 pp; the corresponding paper prose uses 71.11% → 89.55%,
with Prompt +7.38 pp and Tools +10.86 pp. The READMEs retain the figure and main-table values.
The introduction caption mentions seven iterations, while the main-results text describes five
updates and six evaluated checkpoints; the READMEs follow the main-results text.

Paper results are separate from the repository's offline regression checks.
