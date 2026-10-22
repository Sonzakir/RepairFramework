# CLI reference

```bash
python -m apr_framework <command>
apr-framework <command>          # installed script alias
```

All BugsInPy-backed commands run inside the framework container (see
[Getting started](getting-started.md)). Flags are grouped below by command; each
feature page explains them in depth.

## Setup and configuration

```bash
python -m apr_framework list-benchmarks                       # registered benchmarks (bugsinpy)
python -m apr_framework configure [--llm-api-key-env OPENAI_API_KEY]   # store an API key in .env
```

## Benchmark (BugsInPy)

```bash
python -m apr_framework bugsinpy setup                        # clone fork, build image, start executor
python -m apr_framework bugsinpy list-projects
python -m apr_framework bugsinpy list-bugs <project>
python -m apr_framework bugsinpy checkout <project> <bug_id>
python -m apr_framework bugsinpy compile  <project> <bug_id>  # safe multi-Python compile
python -m apr_framework bugsinpy test     <project> <bug_id>  # checkout + compile + run failing tests
```

## Fault localization: `localize`

→ [Fault localization](fault-localization.md)

```bash
# SBFL (default family)
python -m apr_framework localize --project <project> --bug <bug_id> \
  [--backend fauxpy] [--family sbfl] [--granularity statement|function] \
  [--metric ochiai|tarantula|dstar|jaccard|wsbi] [--wsbi-alpha ALPHA] [--top-n N] \
  [--src <pkg>] [--failing_tests "test::id"] [--test-target "test::id"] \
  [--show-raw-output]

# MBFL
python -m apr_framework localize --project <project> --bug <bug_id> \
  --mbfl [--granularity statement|function] \
  [--metric metallaxis|muse] [--top-n N] \
  [--mutation-strategy random] [--budget N] [--seed N]

# Hybrid (weighted merge of SBFL + MBFL)
python -m apr_framework localize --project <project> --bug <bug_id> \
  --family hybrid [--granularity statement|function] \
  [--sbfl-metric ochiai] [--mbfl-metric metallaxis] \
  [--sbfl-weight 0.5] [--mbfl-weight 0.5] [--top-n N]

# LLM-based
python -m apr_framework localize --project <project> --bug <bug_id> \
  --backend llm [--model gpt-5.4] [--temperature 0.0] [--top-n N] \
  [--llm-provider openai-compatible] [--llm-base-url https://api.openai.com/v1] \
  [--llm-api-key-env OPENAI_API_KEY] [--fl-system-prompt fl_prompt1] \
  [--max-source-lines 400] [--source-window 40]
```

- `--test-target` is repeatable: pass it once per pytest target.
- Without `--metric`, the family default applies: `ochiai` for SBFL, `metallaxis` for
  MBFL. Hybrid runs use `--sbfl-metric` / `--mbfl-metric` instead.
- `--backend {fauxpy,llm}` is independent of `--family` / `--metric`, which apply only
  to `fauxpy`.

## Repair: `repair`

→ [Template repair](template-repair.md) · [LLM repair](llm-repair.md) · [Validation & scoring](validation-and-scoring.md)

```bash
# Template repair
python -m apr_framework repair --project <project> --bug <bug_id> \
  [--technique template] [--budget N] [--top-n N] \
  [--operators arith,comp,obo,bool,negate,return] [--timeout N] \
  [--stop-on-first] [--no-regression-check] \
  [--fl-mode auto|perfect] [--fl-family sbfl|mbfl|hybrid] \
  [--localization-metric ochiai] [--mbfl-metric metallaxis] \
  [--skip-localize] [--granularity statement|function] \
  [--ranker weighted|none] [--ranker-weights w1,w2,w3] \
  [--similarity-score | --no-similarity-score] [--assess] \
  [--runs-dir runs]

# LLM repair
python -m apr_framework repair --project <project> --bug <bug_id> \
  --technique llm \
  [--model gpt-5.4] [--temperature 0.8] [--max-candidates 5] \
  [--llm-provider openai-compatible] [--llm-base-url https://api.openai.com/v1] \
  [--llm-api-key-env OPENAI_API_KEY] [--system-prompt prompt1] \
  [--context-enrichment | --no-context-enrichment] [--few-shot N] \
  [--iterative | --no-iterative] [--max-iterations 5] \
  [--retrieval-budget N] [--fl-backend fauxpy|llm] \
  [--assess] [--assess-max-patches N] [--assess-system-prompt assess_prompt1] \
  [--budget N] [--top-n N] [--timeout N] \
  [--stop-on-first] [--no-regression-check] \
  [--fl-mode auto|perfect] [--fl-family sbfl|mbfl|hybrid] \
  [--localization-metric ochiai] [--mbfl-metric metallaxis] \
  [--ranker weighted|none] [--ranker-weights w1,w2,w3] \
  [--similarity-score | --no-similarity-score] \
  [--runs-dir runs]

# Full LLM pipeline in one command: LLM-FL -> LLM repair with retrieval -> LLM assessment
python -m apr_framework repair --project <project> --bug <bug_id> \
  --technique llm --fl-backend llm --retrieval-budget 3 --assess --similarity-score \
  --model gpt-5.4 --temperature 1.0 \
  --llm-base-url https://api.openai.com/v1 --llm-api-key-env OPENAI_API_KEY
```

- `--ranker none` is the default for `repair`. `--ranker weighted` turns ranking on,
  and `--ranker-weights` overrides the three component weights (suspiciousness,
  simplicity, operator priority). Only relative sizes matter.
- `--similarity-score` is off by default. With it off, `context_similarity_score` /
  `similarity_band` are omitted from the JSON entirely.
- For `--technique llm`, `--operators` and `--skip-localize` are ignored. LLM-specific
  flags are ignored for `--technique template`.
- `--few-shot N` is independent of `--context-enrichment` / `--iterative` and works in
  single-shot mode too.
- `--fl-mode perfect` wins over `--fl-backend llm`.

## Evaluation matrices: `bugsinpy evaluate-*`

→ [Evaluation harnesses](evaluation.md)

```bash
# Localization comparison: 8 techniques vs. ground truth
python -m apr_framework bugsinpy evaluate-localization \
  [--bugs black:1,black:3,black:7] [--granularity statement|function] \
  [--budget N] [--seed N] [--top-ks 1,5,10] \
  [--output-dir experiment_results]

# Template repair matrix: bugs x {auto, perfect} FL + ranker
python -m apr_framework bugsinpy evaluate-repair \
  [--bugs project:id,project:id,...] [--fl-modes auto,perfect] \
  [--fl-family sbfl|mbfl|hybrid] [--localization-metric ochiai] \
  [--operators ...] [--budget N] [--top-n N] [--ranker weighted|none] \
  [--output-dir experiment_results/repair] [--runs-dir runs]

# LLM repair matrix: bugs x {single-shot, context-enriched, iterative} x {auto, perfect}
python -m apr_framework bugsinpy evaluate-llm-repair \
  [--bugs black:1,tornado:14,scrapy:2,fastapi:3] \
  [--variants single-shot,context-enriched,iterative] \
  [--fl-modes auto,perfect] \
  [--model gpt-5.4] [--temperature 1.0] \
  [--llm-provider openai-compatible] [--llm-base-url https://api.openai.com/v1] \
  [--llm-api-key-env OPENAI_API_KEY] [--system-prompt prompt1] \
  [--max-candidates 3] [--top-n 3] [--max-iterations 5] \
  [--budget 200] [--timeout 120] \
  [--fl-family sbfl|mbfl|hybrid] [--localization-metric ochiai] [--mbfl-metric metallaxis] \
  [--mutation-budget 50] [--seed 0] [--granularity statement|function] \
  [--stop-on-first] [--no-regression-check] \
  [--ranker weighted|none] [--ranker-weights w1,w2,w3] \
  [--output-dir experiment_results/llm_repair/task5] [--runs-dir runs]

# Four-approach comparison (template, single-shot, iterative, full LLM pipeline)
python -m apr_framework bugsinpy evaluate-course-comparison \
  [--bugs black:1,black:3] \
  [--approaches a3-template,a4-single-shot,a4-iterative,a5-full-llm] \
  [--fl-modes auto,perfect] [--retrieval-budget 3] \
  [--model gpt-5.4] [--temperature 1.0] \
  [--output-dir experiment_results/course_comparison] [--runs-dir runs]

# Dummy pipeline on three black bugs
python -m apr_framework bugsinpy evaluate-dummy [--seed 123] [--runs-dir runs]
```

- `evaluate-repair` defaults to `--ranker weighted`; `evaluate-llm-repair` defaults to
  `--ranker none`.
- For `evaluate-course-comparison`, pass `--fl-modes auto,perfect` for bugs FauxPy can
  localize (e.g. `black`) and `--fl-modes perfect` for bugs it can't (e.g.
  `tornado:14`, `scrapy:2`).

## Break This Fix cases: `challenge`

→ [Break This Fix](challenges.md)

```bash
# List every case bundle under challenges/cases/
python -m apr_framework challenge list

# Replay a case three ways (buggy, candidate, developer-fixed) with the evaluator files
# locked; exit 0 only if the observed outcomes match case.json. No API key needed.
python -m apr_framework challenge run <case-id> \
  [--patch my_fix.diff] [--repeats 3] [--timeout 300] \
  [--reuse-checkout] [--json-out result.json] [--record]

# One fresh, isolated coding-agent attempt (runs on the HOST, where `claude` is logged in)
python -m apr_framework challenge agent-run --project <project> --bug <bug_id> \
  [--agent claude-code] [--model MODEL] [--max-turns 40] [--timeout-minutes 20] \
  [--max-budget-usd 3.00] [--test-timeout 300] [--runs-dir runs] [--keep-workspace]
```

| Flag | Command | Meaning |
|---|---|---|
| `--patch PATH` | `run` | replay this diff as the candidate (e.g. your own repair); the case's candidate rows are then reported but not compared with `case.json` |
| `--repeats N` | `run` | clean repeats of every variant (default 3); every repeat must agree |
| `--reuse-checkout` | `run` | keep an already compiled challenge checkout instead of a fresh checkout + compile (recorded in the result) |
| `--json-out PATH` | `run` | where to write the JSON result (default `.workspace/challenge_results/<case-id>/<timestamp>.json`) |
| `--record` | `run` | append a summary to the case's `replay_log`; never changes its status |
| `--model` | `agent-run` | the agent's model (default: the CLI's own default; recorded per run) |
| `--max-turns`, `--timeout-minutes`, `--max-budget-usd` | `agent-run` | per-attempt caps: agentic turns, wall clock, Claude Code's estimated spend |
| `--test-timeout` | `agent-run` | seconds per test command the agent's shims or the evaluation run |
| `--keep-workspace` | `agent-run` | keep the agent's temporary workspace after the run |

Scripts that go with it (run from the repo root):

```bash
python scripts/audit_test_edits.py [experiment_results/INTEGRITY_AUDIT.md]   # no Docker: committed patches that edit evaluator files
python scripts/revalidate_saved_patches.py <run_dir> [...]                    # executor: replay saved LLM patches through today's gate
python scripts/preflight_bugs.py fastapi:2 luigi:1 [...] [--out preflight.json]  # executor: checkout, compile, failing trigger, test count
```

## Environment variables

| Variable | Purpose |
|---|---|
| `OPENAI_API_KEY` (or any name passed to `--llm-api-key-env`) | LLM API key; can also live in `.env` |
| `APR_HOST_PROJECT_ROOT` | host path of the repository, so the executor container can mount it |
| `APR_LLM_DEBUG_PROMPT` | dump every LLM prompt: `stderr`, or a directory path (see [LLM repair](llm-repair.md#inspecting-the-exact-prompt)) |
| `BUGSINPY_CONTAINER` / `BUGSINPY_IMAGE` | override the executor container / image names (defaults `apr-bugsinpy-executor` / `apr-bugsinpy:local`) |
