# Repair Evaluation Results (Assignment 3 — Task 5)

Full template-based repair pipeline (fault localization → patch generation → validation → ranking) run on 3 BugsInPy bug(s), each under both automated FL and perfect (oracle) FL, with the patch ranker applied.

Generated: ranker: `weighted-composite` · per-validation budget: 200


## tornado bug #14

| FL mode | Backend | Generated | Validated | Plausible | Correct | 1st plausible (s) | Total (s) | Correct rank (gen) | Correct rank (ranked) | Notes |
|---|---|---|---|---|---|---|---|---|---|---|
| auto | auto-fl | — | — | — | — | — | — | — | — | ERROR: ConfigurationError: dependency problem |
| perfect | perfect-fl | 6 | 6 | 1 | 1 | 0.7 | 1.2 | 1 | 1 | correct |

## scrapy bug #2

| FL mode | Backend | Generated | Validated | Plausible | Correct | 1st plausible (s) | Total (s) | Correct rank (gen) | Correct rank (ranked) | Notes |
|---|---|---|---|---|---|---|---|---|---|---|
| auto | fauxpy | 0 | 0 | 0 | 0 | — | 0.0 | — | — | no_patch |
| perfect | perfect-fl | 5 | 5 | 0 | 0 | — | 1.3 | — | — | failed |

## black bug #1

| FL mode | Backend | Generated | Validated | Plausible | Correct | 1st plausible (s) | Total (s) | Correct rank (gen) | Correct rank (ranked) | Notes |
|---|---|---|---|---|---|---|---|---|---|---|
| auto | fauxpy | 8 | 8 | 0 | 0 | — | 12.2 | — | — | failed |
| perfect | perfect-fl | 0 | 0 | 0 | 0 | — | 1.1 | — | — | no_patch |

## Aggregate (per FL mode)

Totals across all evaluated bugs. *Number of Distinct Bugs with Correct Patch* counts bugs with at least one correct patch; *bugs with plausible* counts bugs with at least one plausible patch.

| FL mode | Bugs | Generated | Plausible | Correct | Bugs with plausible | Number of Distinct Bugs with Correct Patch |
|---|---|---|---|---|---|---|
| auto | 3 | 8 | 0 | 0 | 0 | 0 |
| perfect | 3 | 11 | 1 | 1 | 1 | 1 |

## Discussion

**Which bugs were repaired?**  One: `tornado#14`, under perfect FL. Its developer fix is `if IOLoop.current(instance=False) is None:` → `... is not None:` — a single `Is`→`IsNot` swap, exactly what the `comp` operator emits. With the oracle pointing at the fault line, the generator reproduces the developer fix verbatim (6 candidates → 1 plausible → 1 correct), it passes validation, and the diff-level check confirms correctness (patch under `run_artifacts/`). No bug was repaired under automated FL.

**Did the repair technique benefit from better FL?**  The effect is real but *mediated by operator reach*, and the three bugs show all three cases:
- `tornado#14` — the clean win: perfect FL turns the fix into one reachable `comp` mutation and yields a correct patch, while automated FL never even runs (FauxPy 0.7.0 is uninstallable on the bug's Python 3.7.0, recorded as the error cell above).
- `scrapy#2` — better FL means *more attempts*: perfect FL targets the operator-reachable oracle line and generates 5 candidates, whereas automated FL surfaces no reachable node in its top-N and generates 0.
- `black#1` — the *opposite inversion*: the developer fix wraps code in `try/except` (out of operator reach), so perfect FL generates 0 candidates, while automated FL's top-N happens to include operator-reachable lines and generates 8 — none correct.

So FL quality decides *whether the fault line is even attempted*, but the template operator set is the dominant bottleneck: when the real fix is not an operator-level edit, no FL mode can repair it. That is why the aggregate shows perfect FL ahead (11 generated, 1 correct vs. 8 generated, 0 correct) but only by the single bug whose fix happens to be operator-shaped.

**Did the ranker surface correct patches earlier?**  On the only cell that produced a correct patch (`tornado#14`, perfect FL) the plausible set had a single element, so generation order and ranked order coincide (rank 1 -> rank 1). The ranker correctly places that patch first, but *demonstrating reordering* needs ≥2 plausible patches in one cell, which none of these bugs produced. This matches the Task-4 limitation that ranking only distinguishes patches when at least two are plausible.

## General notes on the technique (not specific to this run)

These are properties of the approach that hold regardless of which bugs are evaluated:

1. **Operator reach dominates.** The six operators only match fixes that are themselves single operator-level edits; most BugsInPy developer fixes add or restructure statements and are unreachable regardless of FL quality. Across the packaged benchmark, `tornado#14` is effectively the only pure-operator-swap fix.
2. **Automated FL is environment-sensitive.** FauxPy 0.7.0 depends (transitively via `pyllmut` → `openai`) on Python ≥ 3.7.1, so it cannot install for bugs pinned to Python 3.7.0; such cells are reported honestly as error cells. Perfect FL has no such dependency and always runs.
3. **Correctness is strict and syntactic.** It is a diff-level match of the normalized added/removed lines against the single-file developer fix
