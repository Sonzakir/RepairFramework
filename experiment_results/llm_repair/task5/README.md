# LLM Repair Evaluation Results (Assignment 4 — Task 5)

Full LLM repair pipeline run on 4 BugsInPy bug(s), each under 3 variant(s) × 2 FL mode(s) = 24 cells.

Generated: 2026-07-07 11:10 UTC · model: `gpt-5.4` · per-validation budget: 200

Variants (isolated axes): **single-shot** = bare prompt (no enrichment), **context-enriched** = failing-test source + error traceback added, **iterative** = multi-turn test-failure feedback loop.


## black bug #1

| Variant | FL mode | Queries | Generated | Plausible | Correct | 1st plausible (s) | Total (s) | Outcome |
|---|---|---|---|---|---|---|---|---|
| single-shot | auto | 9 | 9 | 0 | 0 | — | 31.0 | failed |
| single-shot | perfect | 9 | 9 | 0 | 0 | — | 34.9 | failed |
| context-enriched | auto | 9 | 9 | 0 | 0 | — | 28.2 | failed |
| context-enriched | perfect | 9 | 9 | 3 | 0 | 52.5 | 88.0 | plausible |
| iterative | auto | 15 | 15 | 0 | 0 | — | 81.5 | failed |
| iterative | perfect | 15 | 15 | 0 | 0 | — | 59.5 | failed |

## tornado bug #14

| Variant | FL mode | Queries | Generated | Plausible | Correct | 1st plausible (s) | Total (s) | Outcome |
|---|---|---|---|---|---|---|---|---|
| single-shot | auto | — | — | — | — | — | — | ERROR: dependency |
| single-shot | perfect | 3 | 3 | 3 | 3 | 4.9 | 5.6 | correct |
| context-enriched | auto | — | — | — | — | — | — | ERROR: dependency |
| context-enriched | perfect | 3 | 3 | 3 | 3 | 4.1 | 4.7 | correct |
| iterative | auto | — | — | — | — | — | — | ERROR: dependency |
| iterative | perfect | 1 | 1 | 1 | 1 | 1.9 | 1.9 | correct |

## scrapy bug #2

| Variant | FL mode | Queries | Generated | Plausible | Correct | 1st plausible (s) | Total (s) | Outcome |
|---|---|---|---|---|---|---|---|---|
| single-shot | auto | 0 | 0 | 0 | 0 | — | 0.0 | no_patch |
| single-shot | perfect | 6 | 6 | 1 | 0 | 10.0 | 10.9 | plausible |
| context-enriched | auto | 0 | 0 | 0 | 0 | — | 0.0 | no_patch |
| context-enriched | perfect | 6 | 6 | 6 | 0 | 10.5 | 12.4 | plausible |
| iterative | auto | 0 | 0 | 0 | 0 | — | 0.0 | no_patch |
| iterative | perfect | 4 | 4 | 2 | 0 | 6.3 | 13.2 | plausible |

## fastapi bug #3

| Variant | FL mode | Queries | Generated | Plausible | Correct | 1st plausible (s) | Total (s) | Outcome |
|---|---|---|---|---|---|---|---|---|
| single-shot | auto | 9 | 9 | 0 | 0 | — | 81.6 | failed |
| single-shot | perfect | 9 | 8 | 0 | 0 | — | 69.2 | failed |
| context-enriched | auto | 9 | 8 | 0 | 0 | — | 74.2 | failed |
| context-enriched | perfect | 9 | 8 | 4 | 0 | 47.4 | 74.0 | plausible |
| iterative | auto | 15 | 15 | 0 | 0 | — | 133.6 | failed |
| iterative | perfect | 12 | 12 | 1 | 0 | 98.7 | 98.7 | plausible |

## Aggregate (per variant × FL mode)

Totals across all evaluated bugs. *Number of Distinct Bugs with Correct Patch* counts bugs with at least one correct patch in that cell type.

| Variant | FL mode | Bugs | Queries | Generated | Plausible | Correct | Number of Distinct Bugs with Correct Patch |
|---|---|---|---|---|---|---|---|
| single-shot | auto | 4 | 18 | 18 | 0 | 0 | 0 |
| single-shot | perfect | 4 | 27 | 26 | 4 | 3 | 1 |
| context-enriched | auto | 4 | 18 | 17 | 0 | 0 | 0 |
| context-enriched | perfect | 4 | 27 | 26 | 16 | 3 | 1 |
| iterative | auto | 4 | 30 | 30 | 0 | 0 | 0 |
| iterative | perfect | 4 | 32 | 32 | 4 | 1 | 1 |

## Analysis

**Effect of iterative repair.**  The feedback loop recovered outcomes single-shot missed in: `fastapi#3` (perfect FL): iterative reached a **plausible** patch where single-shot did not.

**Effect of context enrichment.**  Adding the failing test source + error traceback improved: `black#1` (perfect FL): plausible 0→3, correct 0→0; `scrapy#2` (perfect FL): plausible 1→6, correct 0→0; `fastapi#3` (perfect FL): plausible 0→4, correct 0→0. 

**Comparison with Assignment 3 (template repair).**  Best LLM outcome across all variants/FL modes per bug, next to the template technique's result from `experiment_results/repair/results.json`:

- `black#1`: LLM best = plausible; template = no correct patch (template).
- `tornado#14`: LLM best = correct; template = **correct** (template).
- `scrapy#2`: LLM best = plausible; template = no correct patch (template).
- `fastapi#3`: LLM best = plausible; template = not in the Assignment-3 template results.

In short, compared with the template-based repair algorithm used in Assignment 3, the LLM-based repair approach produced more plausible patches on this evaluation set.
