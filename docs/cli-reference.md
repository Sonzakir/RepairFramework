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

## Strict Repro: `repro`

→ [Strict Repro](repro.md)

```bash
# BugsInPy bug: issue -> verified test, probes, one hardening round (needs Docker + an API key)
python -m apr_framework repro new --project <project> --bug <bug_id> \
  [--model gpt-5.4] [--temperature 1.0] [--judge-temperature 0.0] [--max-rounds 3] \
  [--strategy direct|assertflip] [--retrieval-budget 4] [--repeats 3] [--timeout 300] \
  [--max-probes 8] [--max-llm-probes 12] [--no-probes] [--no-harden] [--max-usd 2.00] \
  [--runs-dir runs] [--reuse-checkout] [--export-witness DIR]

# Local pytest project (runs LLM-written code on this machine)
python -m apr_framework repro new --repo PATH --python PATH/.venv/bin/python --allow-local-exec \
  (--issue-file issue.md | --issue https://github.com/OWNER/REPO/issues/N) \
  [--regression-cmd "python -m pytest -q tests"] [--id ID] [...]

# Judge a fix with an existing repro test
python -m apr_framework repro check <runs/run_NNN | REPO/.apr/repro/ID> --patch fix.diff \
  [--harden] [--allow-local-exec] [--regression-cmd CMD] [...]

# Evaluation over an explicit list (both flags required)
python -m apr_framework bugsinpy evaluate-repro --bugs tqdm:1,httpie:4 --max-usd 5 \
  [--output-dir experiment_results/strict_repro_<date>] [...]
```

| Flag | Command | Meaning |
|---|---|---|
| `--project`, `--bug` | `new` | BugsInPy mode: the bug's linked issue; the developer fix is ground truth |
| `--repo`, `--python` | `new` | local mode: your project and its interpreter |
| `--issue-file`, `--issue` | `new` | local mode: the bug report (a file, first line = title; or a GitHub issue URL) |
| `--allow-local-exec` | `new`, `check` | local mode only: accept running LLM-written code on this machine (refused without it) |
| `--regression-cmd CMD` | `new`, `check` | local mode: a command that passes on a correct fix; probes that fail it count as confirmed wrong |
| `--id` | `new` | local mode: directory name under `.apr/repro/` (default `issue_<N>` or the file stem) |
| `--patch PATH` | `check` | the fix to judge; evaluator-file sections are dropped and reported |
| `--model`, `--llm-base-url`, `--llm-api-key-env` | all | model and OpenAI-compatible endpoint (default GPT@RUB, `GPT_AT_RUB_API_KEY`) |
| `--temperature`, `--judge-temperature` | all | generation/probes/hardening (1.0) and the right-reason judge (0.0) |
| `--max-usd` | all | LLM spend cap; default 2.00 per run, **required** for `evaluate-repro` |
| `--usd-per-mtok-in/out` | all | list price for a model the price table does not know |
| `--max-rounds` | all | test-writing rounds with execution feedback (default 3) |
| `--strategy` | all | `direct` or `assertflip` (passing test of the buggy behaviour, then inverted) |
| `--retrieval-budget` | all | source retrievals per round (default 4) |
| `--repeats`, `--timeout` | all | buggy-code repeats that must agree (3); seconds per test command (300) |
| `--max-probes`, `--max-llm-probes` | all | deterministic probes (8) and test-aware LLM probes (12, the maximum) |
| `--probes/--no-probes`, `--harden/--no-harden` | all | probes on by default; hardening on for `new`/`evaluate-repro`, off for `check` |
| `--reuse-checkout` | BugsInPy | reuse a compiled `strictrepro_` checkout |
| `--export-witness DIR` | `new`, `check` | write a Break This Fix bundle draft (status `candidate`); no case is created |
| `--bugs` | `evaluate-repro` | required, explicit `project:bug` list; there is no default |
| `--output-dir` | `evaluate-repro` | where `results.json` and `README.md` go; an existing `results.json` is never overwritten |

Exit codes: `repro new` exits 0 when the test is labelled "reproduces" (and, with a
fix, is fail-to-pass); `repro check` exits 0 when the patch turns the test green.

## Behaviour Diff: `behaviour-diff`

→ [Behaviour Diff](behaviour-diff.md)

```bash
# BugsInPy: the developer fix is the reference (no API key needed)
python -m apr_framework behaviour-diff --project <project> --bug <bug_id> \
  [--patch fix.diff ...] [--from-run runs/run_NNN ...] [--no-reference] \
  [--max-variants 40] [--variants-per-test 8] [--timeout 900] [--reuse-checkout] [--runs-dir runs]

# Local pytest/unittest project (runs the tests and the candidates' code on this machine)
python -m apr_framework behaviour-diff --repo PATH --python PATH/.venv/bin/python --allow-local-exec \
  --tests "python -m pytest tests/test_x.py" --patch agent.diff [--reference dev_fix.diff] \
  [--trigger "python -m pytest tests/test_x.py::test_bug"]
```

| Flag | Meaning |
|---|---|
| `--project`, `--bug` | BugsInPy target: fresh checkout + compile in `.workspace/bugsinpy/evidence_<project>_<bug>` |
| `--reuse-checkout` | reuse that compiled checkout |
| `--repo`, `--python`, `--tests` | local target: the project, its interpreter, and the suite as a pytest or unittest command (files or modules) |
| `--trigger CMD` | local: the failing test's command, so divergences from the buggy code are split into "on the failing test" and "elsewhere" (repeatable) |
| `--allow-local-exec` | local: accept running the tests and candidate code without isolation (refused without it) |
| `--patch PATH` | a candidate fix (repeatable) |
| `--from-run DIR` | candidates from an earlier run: a `repair` run's plausible patches or a `challenge agent-run` diff (repeatable) |
| `--reference PATH` | local: the reference fix; BugsInPy always uses the developer fix |
| `--no-reference` | compare against the buggy code only |
| `--max-variants`, `--variants-per-test` | one-literal test variants in total (40; 0 = original tests only) and per reaching test (8) |
| `--timeout` | seconds for one run of the suite (900) |

Exit code 0 when the comparison ran; any divergence is reported, not turned into
an exit code.

## Tamper Check: `tamper-check`

→ [Tamper Check](tamper-check.md)

```bash
# Static: a diff against the tree it applies to, or a git working tree against a base
python -m apr_framework tamper-check --diff agent.diff --repo PATH [--json report.json] [--strict]
python -m apr_framework tamper-check --repo . --git-base origin/main

# With the fail-before / pass-after runs (needs a target)
python -m apr_framework tamper-check --project <project> --bug <bug_id> --diff agent.diff --run-tests
python -m apr_framework tamper-check --diff agent.diff --repo PATH --python PY --tests "python -m pytest tests" \
  --allow-local-exec --run-tests
```

| Flag | Meaning |
|---|---|
| `--diff PATH` | the change, applied in memory to `--repo` or the BugsInPy checkout |
| `--git-base REV` | with `--repo`: the working tree against `REV` (tracked files; static only) |
| `--run-tests` | also run the added and edited tests: tests-only, full change, and the original tests on the source-only change |
| `--strict` | exit 1 on suspicious findings too |
| `--json PATH` | write the report as JSON |
| target flags | as for `behaviour-diff`; `--python`, `--tests`, `--allow-local-exec` only with `--run-tests` |

Exit codes:
- `0`: clean, or suspicious without `--strict`;
- `1`: a weakening finding (or, with `--strict`, a suspicious one);
- `2`: the command could not run.

## Environment variables

| Variable | Purpose |
|---|---|
| `OPENAI_API_KEY` (or any name passed to `--llm-api-key-env`) | LLM API key; can also live in `.env` |
| `APR_HOST_PROJECT_ROOT` | host path of the repository, so the executor container can mount it |
| `APR_LLM_DEBUG_PROMPT` | dump every LLM prompt: `stderr`, or a directory path (see [LLM repair](llm-repair.md#inspecting-the-exact-prompt)) |
| `GITHUB_TOKEN` | optional; Strict Repro uses it for GitHub issue requests when set (higher rate limit) |
| `BUGSINPY_CONTAINER` / `BUGSINPY_IMAGE` | override the executor container / image names (defaults `apr-bugsinpy-executor` / `apr-bugsinpy:local`) |
