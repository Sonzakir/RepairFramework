# Failed Experiment Report: black:1, black:3, black:7

**Status:** FAILED — An example of the failed experiments on used bugs in dummy evaluation

---

## What Was Attempted

Ran FauxPy 0.7.0 inside Docker for three BugsInPy bugs from the `black` Python formatter
(`black:1`, `black:3`, `black:7`) to compare five SBFL metrics, two MBFL variants, and
the Hybrid technique against ground-truth faulty lines.

Results were real (produced by actual FauxPy runs inside Docker(sibling)), but the bug selection
was fatally wrong. See `results.json` and `README.md` for the raw data.

---

## Why the Experiment Failed

### Root Cause 1: Single Failing Test Per Bug

Every BugsInPy `run_test.sh` for the selected black bugs contained exactly ONE failing test:

| Bug | Failing test |
|---|---|
| `black:1` | `test_works_in_mono_process_only_environment` |
| `black:3` | `test_invalid_config_return_code` |
| `black:7` | test for `testlist_star_expr` parenthesis normalisation |

When there is a single failing test and no passing tests are run, **every SBFL formula
collapses to the same ranking**. The mathematical reason:

For any statement covered by the failing test:
- `ef = 1`, `ep = 0`, `nf = 0`, `np = 0`
- Ochiai = `ef / sqrt((ef+nf)*(ef+ep))` = `1 / sqrt(1*1)` = **1.0**
- Tarantula = `(ef/(ef+nf)) / (ef/(ef+nf) + ep/(ep+np))` = `1 / (1+0)` = **1.0**
- D* = `ef² / ((nf+ep)·…)` = **maximum**
- Jaccard = **maximum**; SBI = **maximum**

All five SBFL metrics assign the same maximum score to every covered statement.
Ranking is then determined only by FauxPy's internal tie-breaking (line number order),
which has nothing to do with actual fault suspicion. The extensions (Jaccard, SBI) appear
identical to the baselines (Ochiai, Tarantula, D*) — a structural tie, not a real result.

**Observed outcome:** All 5 SBFL metrics gave `rank=11` for black:1 and `rank=165` for
black:7 — identical across all metrics because the single-test condition makes the SBFL
formula irrelevant.

### Root Cause 2: Mutation-Inadequate Test Suites (MBFL gives 0 rankings)


For all three black bugs, **every generated mutant survived the test suite**:

- **black:1** — The failing test checks that multiprocessing is *disabled*. Cosmic Ray /
  mutmut mutations to the `ProcessPoolExecutor` constructor (lines 621/636/646) use
  arithmetic and logical substitution operators, not process-spawning logic. No mutation
  changes whether multiprocessing is disabled.

- **black:3** — The faulty line (`click.Path(exists=False)`) is evaluated at **module
  import time** (it is a decorator argument). FauxPy's statement-level coverage never
  records it. With zero coverage for that line, no mutants are generated for it.

- **black:7** — The failing test checks the final formatted string output. Mutations to
  the covered 536 statements (mostly temporary variables, branches, helper calls) do not
  affect the output string. Random-budget selection of 200 (baseline) and 50 (extension)
  statements did not reach the critical output-altering lines.

**Observed outcome:** MBFL returned 0 ranked locations for all 3 bugs, for both the
baseline (budget 200) and the extension (budget 50).

### Root Cause 3: Module-Level Fault (black:3 is invisible to SBFL)

The fault in `black:3` (`click.Path(exists=False)`) is a decorator argument that Python
evaluates **during module import**, before any test runs. FauxPy's dynamic coverage
instrumentation only records lines executed *during test execution*. Line 397 never
appears in any coverage trace, so it cannot be ranked by either SBFL or MBFL.

**Observed outcome:** SBFL ranked only 13 statements (function bodies reached during the
test); line 397 was not among them. `faulty_rank = null`.

---

