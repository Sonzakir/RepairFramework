# Repair Framework documentation

| Guide | What's inside |
|---|---|
| [Getting started](getting-started.md) | Install with Docker, configure an LLM provider, first run, troubleshooting |
| [Fault localization](fault-localization.md) | SBFL (incl. Jaccard & WSBI), MBFL with budgeted mutants, Hybrid, LLM-FL, the perfect-FL oracle |
| [Template repair](template-repair.md) | AST mutation operators, cost controls, output layout |
| [LLM repair](llm-repair.md) | Prompt design, context enrichment, few-shot, iterative feedback, context retrieval, patch assessment, the end-to-end pipeline |
| [Validation, scoring & ranking](validation-and-scoring.md) | Plausibility (trigger + regression), exact diff vs. context similarity, tracked metrics, the weighted ranker |
| [Evaluation harnesses](evaluation.md) | The four experiment matrices, run artifacts, report regeneration scripts |
| [Results](results.md) | Every published number: localization, template repair, LLM variants, retrieval, four-approach comparison |
| [Architecture](architecture.md) | Interfaces, source layout, design decisions, extension points |
| [CLI reference](cli-reference.md) | Every command and flag at a glance |

Raw experiment output (`results.json`, generated reports, per-cell run artifacts)
lives in [`experiment_results/`](../experiment_results/).
