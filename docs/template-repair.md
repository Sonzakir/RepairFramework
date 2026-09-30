# Template-based repair

`repair --technique template` (the default) is a classic generate-and-validate
repair engine driven by fault localization. It is fast and fully offline, and it
needs no API key.

## How it works

1. **Where to fix.** The top-N suspicious locations from fault localization (ranked by
   suspiciousness) become repair targets.
2. **What to try.** AST mutation operators generate syntactically valid program variants
   at each suspicious line.
3. **Which variants pass.** Each variant is applied to the checkout worktree. The
   BugsInPy test suite then runs inside the executor container, and the file is
   restored unconditionally afterwards.

A patch is **plausible** only when all of these hold:

- the trigger test command exits cleanly (`return_code == 0`) with zero failures and
  zero errors, and at least one test actually passed. The exit-code and passed-count
  guards reject patches that break test collection or import, which would otherwise
  look like "0 failed, 0 error";
- the regression suite shows no new failures (see [Validation](validation-and-scoring.md)).

## Mutation operators

| Key | Description |
|---|---|
| `arith` | Swaps arithmetic operators in `BinOp` nodes: `+↔−`, `*↔/`, `//↔%` |
| `comp` | Swaps comparison operators in `Compare` nodes: `>↔>=`, `<↔<=`, `==↔!=`, `is↔is not`, `in↔not in` |
| `obo` | Off-by-one: emits `n+1` and `n-1` variants for integer constants and the upper bound of `range(n)` calls |
| `bool` | Swaps `and↔or` in `BoolOp` nodes |
| `negate` | Wraps the `test` of `if`/`while` statements in `not (...)` |
| `return` | Mutates `return True → return False` (and vice versa), and `return <expr> → return None` |

All operators take a `target_line` parameter and only mutate AST nodes whose source
range covers that line. Each mutation touches one location at a time.

## Example invocations

```bash
# Full repair run (localize + repair) with default settings:
python -m apr_framework repair --project black --bug 1

# Cap the budget at 20 validations and try only the top 3 locations:
python -m apr_framework repair --project black --bug 1 --budget 20 --top-n 3

# Only apply arithmetic and comparison operators:
python -m apr_framework repair --project tornado --bug 14 --operators arith,comp --fl-mode perfect

# Stop as soon as the first plausible patch is found:
python -m apr_framework repair --project black --bug 1 --stop-on-first

# Choose the fault-localization family that drives the repair targets:
python -m apr_framework repair --project black --bug 1 --fl-family mbfl --mbfl-metric metallaxis
python -m apr_framework repair --project black --bug 1 --fl-family hybrid --sbfl-weight 0.5 --mbfl-weight 0.5

# Skip re-running localization and load the most recent cached result:
python -m apr_framework repair --project black --bug 1 --skip-localize

# Use a different SBFL metric for localization:
python -m apr_framework repair --project black --bug 1 --localization-metric tarantula

# Full sequence from scratch:
python -m apr_framework bugsinpy setup
python -m apr_framework bugsinpy test black 1       # checkout + compile + test
python -m apr_framework localize --project black --bug 1 --metric ochiai --top-n 10
python -m apr_framework repair --project black --bug 1 --budget 20 --top-n 3 --skip-localize
```

| Flag | Default | Meaning |
|---|---|---|
| `--technique` | `template` | repair engine (`template` or `llm`) |
| `--budget` | `200` | max patch validations (each test-suite run counts as one) |
| `--top-n` | `5` | suspicious locations to attempt |
| `--operators` | `arith,comp,obo,bool,negate,return` | operators to apply |
| `--timeout` | `120` | seconds allowed per test-suite invocation |
| `--stop-on-first` | off | stop at the first plausible patch |
| `--no-regression-check` | off | skip the regression half of plausibility |
| `--fl-mode` | `auto` | `auto` (a localizer) or `perfect` (oracle) |
| `--fl-family` | `sbfl` | `sbfl`, `mbfl` or `hybrid` for auto FL |
| `--localization-metric` / `--mbfl-metric` | `ochiai` / `metallaxis` | FL metrics |
| `--mutation-budget` / `--seed` | `50` / `0` | MBFL mutant budget and seed |
| `--sbfl-weight` / `--mbfl-weight` | `0.5` / `0.5` | hybrid weights |
| `--granularity` | `statement` | `statement` or `function` |
| `--skip-localize` | off | reuse the most recent cached localization |
| `--ranker` / `--ranker-weights` | `none` | patch ranking (see [Patch ranking](validation-and-scoring.md#patch-ranking)) |
| `--similarity-score` | off | grade plausible patches against the developer fix |
| `--assess` | off | LLM quality assessment of plausible patches |
| `--runs-dir` | `runs` | where `run_NNN/` directories go |

## Design decisions

**AST-based, not text-based mutations.** Mutations are made on the Python AST with
`ast.NodeTransformer` subclasses, and `ast.unparse()` rebuilds the source text. The
benefit is that every generated variant parses. The cost is that `ast.unparse()`
reformats the whole file (it normalizes whitespace and removes redundant parentheses),
so the raw unified diff includes cosmetic changes beyond the mutation. The
`patched_source` stored in `PatchCandidate.metadata` is written directly to avoid
re-parsing. The correctness metrics cancel the cosmetic noise (see
[Validation](validation-and-scoring.md#the-two-correctness-metrics)).

**Line-to-AST-node mapping.** Every operator checks
`node.lineno <= target_line <= node.end_lineno` before mutating. That maps the FL
line number to the AST subtree covering that line, and leaves unrelated parts of the
file alone.

**Cost control: budget, stop-on-first, timeout.**

- `--budget` caps the total number of patch validations; each `adapter.run_tests()`
  call counts as one. Generating mutations is free; validating them (running tests)
  is expensive.
- `--stop-on-first` halts as soon as a plausible patch is found.
- `--timeout` is a real wall-clock limit per test run. It is passed down to the Docker
  `exec` call, and a timed-out run counts as a failed, non-plausible candidate (exit
  code 124). The loop never hangs.

**Selectable fault-localization family.** `--fl-family sbfl|mbfl|hybrid` picks the
localizer that ranks the repair targets:

- SBFL, via `--localization-metric`;
- MBFL, via `--mbfl-metric`, `--mutation-budget` and `--seed`;
- a weighted `HybridFaultLocalizer`, via `--sbfl-weight` and `--mbfl-weight`.

**No changes to the `RepairAlgorithm` interface.** `TemplateRepairAlgorithm`
implements two existing methods unchanged:

- `generate_patches(bug, checkout)` generates candidates without testing them;
- `validate_patch(bug, checkout, patch)` applies, tests and reverts one candidate.

An extra convenience method, `repair(bug, checkout)`, runs the full budget loop. The
CLI drives the pipeline through `RepairEvaluationRunner` instead. Both share the
single loop in `run_validation_loop`.

## Output

Each run creates the next `runs/run_NNN/` directory and writes:

- `config.json`: all configuration parameters
- `repair_results.json`: per-candidate validation outcomes (valid unified diffs, test
  counts including `test_return_code`) and the plausible-patch list
- `execution.log`: a timestamped step log
- `patches/<patch_id>.diff` and `patches/<patch_id>.patched.py`: written for each
  **plausible** patch, so the fix can be recovered outside the JSON

## Known limitations

- Only projects whose `run_test.sh` invokes pytest directly are supported (the same
  restriction as `localize`).
- The framework container needs Python 3.9+ (for `ast.unparse()`).
- **Operator reach dominates.** The six operators only match fixes that are single
  operator-level edits. Most BugsInPy developer fixes add or restructure statements,
  and no FL mode can bring those within reach. See [Results](results.md#template-repair).
