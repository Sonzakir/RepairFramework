# Results

Every number on this page comes from a real run of the framework inside Docker. The
full machine-readable output and per-cell run artifacts are committed under
[`experiment_results/`](../experiment_results/). To reproduce or extend these runs,
see [Evaluation harnesses](evaluation.md).

> **Read these numbers with care.** The bug sets are small (3–4 BugsInPy bugs per
> experiment). LLM runs are single samples at temperature 1.0, and repeat runs vary by
> a few patches either way. The **exact-diff** column counts byte-for-byte matches
> with the developer fix and is *not* a correctness verdict (see
> [Validation](validation-and-scoring.md#the-two-correctness-metrics)). Treat margins
> as indicative, not statistically significant.

## Highlights

| Finding | Evidence |
|---|---|
| The full LLM pipeline found plausible patches on **every** bug in the comparison, including two where no earlier approach found one | black#1: 0 → 6, black#3: 0 → 4 plausible ([details](#four-approach-comparison)) |
| Localizing on its own, the full pipeline beat approaches that were **given the oracle fault location** | tornado#14: 9 exact-diff matches vs. 3; scrapy#2: 9 plausible vs. 2 |
| Context enrichment (failing test + traceback) **quadrupled** plausible patches | 4 → 16 across four bugs under perfect FL ([details](#llm-repair-variants)) |
| On fastapi#6, hybrid SBFL+MBFL localization beat both families alone | SBFL rank 82, MBFL rank 6, hybrid rank **3**. The gain isn't uniform: on the other two bugs, SBFL ranked better ([details](#fault-localization)) |
| Context retrieval gets more plausible patches from fewer candidates | black#1: 4 plausible from 10 candidates vs. 3 from 15 ([details](#context-retrieval)) |
| Fault-location quality dominates repair outcomes | every exact-diff match in the LLM-variant matrix came from perfect FL; automated SBFL produced 0 plausible patches there |

---

## Fault localization

Eight techniques were run on three BugsInPy bugs (**fastapi#3**, **fastapi#6**,
**luigi#33**) and ranked against the ground-truth faulty lines from each bug's patch.
Full report: [`experiment_results/localization/`](../experiment_results/localization/README.md).

"Rank" is the best rank of any ground-truth line; lower is better.

| Bug | Technique | Type | Rank | Top-10 |
|---|---|---|---|---|
| fastapi#3 | SBFL-Ochiai | baseline | 8 | ✓ |
| fastapi#3 | SBFL-Tarantula | baseline | 11 | ✗ |
| fastapi#3 | SBFL-DStar | baseline | 8 | ✓ |
| fastapi#3 | SBFL-Jaccard | **extension** | 8 | ✓ |
| fastapi#3 | SBFL-WSBI | **extension** | 11 | ✗ |
| fastapi#3 | MBFL-Metallaxis | baseline | 18 | ✗ |
| fastapi#3 | MBFL-Metallaxis-Random | **extension** | 11 | ✗ |
| fastapi#3 | Hybrid SBFL+MBFL | **extension** | 11 | ✗ |
| fastapi#6 | SBFL-Ochiai | baseline | 82 | ✗ |
| fastapi#6 | SBFL-Tarantula | baseline | 82 | ✗ |
| fastapi#6 | SBFL-DStar | baseline | 82 | ✗ |
| fastapi#6 | SBFL-Jaccard | **extension** | 82 | ✗ |
| fastapi#6 | SBFL-WSBI | **extension** | 82 | ✗ |
| fastapi#6 | MBFL-Metallaxis | baseline | 6 | ✓ |
| fastapi#6 | MBFL-Metallaxis-Random | **extension** | — | ✗ |
| fastapi#6 | **Hybrid SBFL+MBFL** | **extension** | **3** | **✓ (top-5)** |
| luigi#33 | SBFL-Ochiai | baseline | 11 | ✗ |
| luigi#33 | SBFL-Tarantula | baseline | 191 | ✗ |
| luigi#33 | SBFL-DStar | baseline | 11 | ✗ |
| luigi#33 | SBFL-Jaccard | **extension** | 11 | ✗ |
| luigi#33 | SBFL-WSBI | **extension** | 191 | ✗ |
| luigi#33 | MBFL-Metallaxis | baseline | — | ✗ |
| luigi#33 | MBFL-Metallaxis-Random | **extension** | — | ✗ |
| luigi#33 | Hybrid SBFL+MBFL | **extension** | 26 | ✗ |

SBFL ranked 76 (fastapi#3), 137 (fastapi#6) and 490 (luigi#33) locations; MBFL ranked far fewer.

**Aggregate Top-k accuracy** (fraction of the 3 bugs whose faulty line was in the
top k):

| Technique | Top-1 | Top-5 | Top-10 |
|---|---|---|---|
| SBFL-Ochiai (baseline) | 0/3 | 0/3 | 1/3 |
| SBFL-Tarantula (baseline) | 0/3 | 0/3 | 0/3 |
| SBFL-DStar (baseline) | 0/3 | 0/3 | 1/3 |
| SBFL-Jaccard (extension) | 0/3 | 0/3 | 1/3 |
| SBFL-WSBI (extension) | 0/3 | 0/3 | 0/3 |
| MBFL-Metallaxis (baseline) | 0/3 | 0/3 | 1/3 |
| MBFL-Metallaxis-Random (extension) | 0/3 | 0/3 | 0/3 |
| Hybrid SBFL+MBFL (extension) | 0/3 | **1/3** | 1/3 |

### Key findings

- **Jaccard (extension) matches the best SBFL baseline on fastapi#3.** Ochiai, D* and
  Jaccard all rank the faulty line at position 8. Tarantula and WSBI fall to rank 11,
  so the choice of formula matters.
- **Hybrid reaches rank 3 on fastapi#6**, the only technique to enter the top 5. SBFL
  alone is stuck at rank 82 and MBFL alone reaches rank 6. Combining the two with the
  weighted hybrid improves that to rank 3.
- **WSBI degenerates on luigi#33** (rank 191, the same as Tarantula). luigi#33 has no
  passing tests. When `ep = 0`, WSBI and SBI both give every executed line `1.0`, so
  they can't tell lines apart. Ochiai (`sqrt(ef/F)`) keeps a gradient across the four
  failing tests and ranks the faulty line at 11. This is a real limitation of WSBI and
  a pointer for future work on the zero-passing-test case.
- **Runtime trade-off.** SBFL runs finish in seconds, because they need a single test
  execution per bug. MBFL has to generate and run mutants, which takes minutes even
  with the random-budget extension. The hybrid inherits MBFL's cost in exchange for
  better accuracy.

> **A failed experiment worth knowing about.** An earlier comparison on
> `black:1/3/7` was discarded. Each of those bugs has a single failing test and no
> passing tests run, so every SBFL formula collapses to the same ranking. The write-up
> is in
> [`experiment_results/localization/FAILED_EXPERIMENT.md`](../experiment_results/localization/FAILED_EXPERIMENT.md).
> Bug selection matters as much as the metric.

---

## Template repair

One `evaluate-repair` run over `tornado:14, scrapy:2, black:1`, each under automated
and perfect FL, with the weighted ranker. Full report:
[`experiment_results/repair/`](../experiment_results/repair/README.md).

| Bug | FL mode | Generated | Plausible | Exact diff | Rank of exact match (gen → ranked) | Time to 1st plausible | Total | Outcome |
|---|---|---|---|---|---|---|---|---|
| tornado#14 | auto | — | — | — | — | — | — | **error**: FauxPy 0.7.0 cannot install on Python 3.7.0 |
| tornado#14 | perfect | 6 | 1 | **1** | 1 → 1 | 0.65s | 1.14s | **exact developer fix** |
| scrapy#2 | auto | 0 | 0 | 0 | — | — | ~0s | no operator-reachable node in the SBFL top-N |
| scrapy#2 | perfect | 5 | 0 | 0 | — | — | 1.12s | 5 candidates at the oracle line, none plausible |
| black#1 | auto | 8 | 0 | 0 | — | — | 12.09s | SBFL ran; 8 candidates, none plausible |
| black#1 | perfect | 0 | 0 | 0 | — | — | 1.08s | the developer fix (`try/except`) is out of operator reach |

| FL mode | Generated | Plausible | Exact diff | Bugs with an exact-diff match |
|---|---|---|---|---|
| auto | 8 | 0 | 0 | 0 |
| perfect | 11 | 1 | 1 | 1 |

### Discussion

**Which bugs were repaired?** One: `tornado#14`, under perfect FL. Its developer fix
changes `if IOLoop.current(instance=False) is None:` to `... is not None:`, a single
`Is`→`IsNot` swap, which is exactly what the `comp` operator emits. With the oracle
pointing at the fault line, the generator reproduces the developer fix verbatim, the
patch passes validation, and the diff-level check confirms the match. No bug was
repaired under automated FL.

**Did better FL help?** Yes, but the effect is **mediated by operator reach**, and the
three bugs show all three cases:

- `tornado#14` is the clearest case. Perfect FL turns the fix into one reachable
  mutation and yields the developer's patch. Automated FL never runs, because FauxPy
  can't install on the bug's Python 3.7.0.
- `scrapy#2` shows better FL producing *more attempts*. Perfect FL targets the
  operator-reachable oracle line and generates 5 candidates; automated FL finds no
  reachable node in its top-N and generates 0.
- `black#1` shows the reverse. The developer fix wraps code in `try/except`, which no
  operator can produce, so perfect FL generates 0 candidates. Automated FL's top-N
  happens to include operator-reachable lines and generates 8, none correct.

FL quality decides *whether the fault line is even attempted*, but the **template
operator set is the main bottleneck**. When the real fix isn't an operator-level edit,
no FL mode can repair it. For fixes that restructure control flow or call external
services, the single-operator template technique produces nothing plausible. That is
the class of bug the LLM engine is built for.

**Did the ranker surface correct patches earlier?** In the only cell with a correct
patch (`tornado#14`, perfect FL), the plausible set had one element, so generation
order and ranked order coincide (rank 1 in both). *Showing* reordering needs at least
two plausible patches in one cell, which none of these bugs produced.

**Limitations**

- **Operator reach dominates.** Most BugsInPy developer fixes add or restructure
  statements. Across the packaged benchmark, `tornado#14` is effectively the only
  pure operator-swap fix.
- **Automated FL is environment-sensitive.** FauxPy 0.7.0 depends (via `pyllmut` →
  `openai`) on Python ≥ 3.7.1. Such cells are reported honestly as error cells.
  Perfect FL has no such dependency and always runs.
- **Exact diff is strict and syntactic.** It is a diff-level match of the normalized
  added/removed lines against the single-file developer fix.

---

## LLM repair variants

One `evaluate-llm-repair` run over `black:1, tornado:14, scrapy:2, fastapi:3` ×
{single-shot, context-enriched, iterative} × {auto, perfect} = **24 cells**. It used
OpenAI `gpt-5.4`, `top_n=3`, `max_candidates=3` and `max_iterations=5`. Full report:
[`experiment_results/llm_repair/task5/`](../experiment_results/llm_repair/task5/README.md).

**Aggregate (per variant × FL mode, 4 bugs each):**

| Variant | FL mode | Queries | Generated | Plausible | Exact diff | Bugs with an exact-diff match |
|---|---|---|---|---|---|---|
| single-shot | auto | 18 | 18 | 0 | 0 | 0 |
| single-shot | perfect | 27 | 26 | 4 | 3 | 1 |
| context-enriched | auto | 18 | 17 | 0 | 0 | 0 |
| context-enriched | perfect | 27 | 26 | **16** | 3 | 1 |
| iterative | auto | 30 | 30 | 0 | 0 | 0 |
| iterative | perfect | 32 | 32 | 4 | 1 | 1 |

**Plausible patches per bug under perfect FL:**

| Bug | single-shot | context-enriched | iterative |
|---|---|---|---|
| black#1 | 0 | **3** | 0 |
| tornado#14 | 3 (3 exact) | 3 (3 exact) | 1 (1 exact, in a single query) |
| scrapy#2 | 1 | **6** | 2 |
| fastapi#3 | 0 | **4** | 1 |

### What the numbers say

- **Perfect FL dominates.** Every exact-diff match came from perfect FL. Automated
  (SBFL) FL produced **zero** plausible patches on these four bugs. `tornado#14`'s auto
  cells are error cells, because FauxPy 0.7.0 can't install on its pinned Python
  3.7.0 (the same limitation seen with template repair). Fault-location precision is
  the dominant factor.
- **Context enrichment helps plausibility a lot.** Adding the failing-test source and
  error traceback **quadrupled** the plausible count under perfect FL (4 → 16):
  `black#1` 0→3, `scrapy#2` 1→6, `fastapi#3` 0→4. It did not raise the exact-diff
  count, so it surfaces more test-passing patches but not more developer-identical
  ones.
- **Iterative repair** solved `tornado#14` (perfect) in a **single** query. It also
  recovered a plausible patch on `fastapi#3` (perfect) that single-shot missed. On
  `black#1` the extra turns didn't produce a plausible patch. The most useful feedback
  was the **assertion/traceback** from the failing trigger test; bare pass/fail counts
  alone rarely moved the model.

**Compared with template repair:** the LLM engine **matches** the template technique
on `tornado#14` (both reproduce the developer fix under perfect FL). It also has
**broader reach**, producing plausible patches on `black#1`, `scrapy#2` and
`fastapi#3`, where the template operators produced nothing plausible. On this bug set,
neither technique goes beyond one exactly repaired bug; the LLM's advantage here is
*plausibility coverage*.

### Single-bug FL baseline

An earlier hand-run baseline on black#1 (`gpt-4o-2024-05-13`, temperature 0.8, 5
candidates per location, top-5 locations, budget 200) isolated the effect of the FL
source alone:

| Run | FL mode | Status | Generated | Plausible | Exact diff | Time |
|---|---|---|---|---|---|---|
| run_167 | perfect | plausible | 14 | 2 | 0 | 80.9s (1st plausible 52.4s) |
| run_168 | auto (SBFL/Ochiai) | failed | 25 | 0 | 0 | 49.9s |

Perfect FL found 2 plausible patches that pass the trigger test and regression check
without matching the developer fix. Automated FL generated more candidates (SBFL
surfaces several suspicious lines to prompt against) but no plausible patch, because
the developer-fix line wasn't localized precisely enough. Details:
[`experiment_results/llm_repair/FL-Guided Repair and Perfect Fault Localization Baseline/`](../experiment_results/llm_repair/FL-Guided%20Repair%20and%20Perfect%20Fault%20Localization%20Baseline/README.md).

---

## Context retrieval

`black#1`, perfect FL, `--temperature 0.8 --max-candidates 5`:

| Retrieval | Tools called by the model | Candidates generated | Plausible | Exact diff |
|---|---|---|---|---|
| off (`--retrieval-budget 0`) | — | 15 | 3 | 0 |
| on (`--retrieval-budget 3`) | `get_function_definition("get_cache_file")` | 10 | **4** | 0 |

With retrieval, the model reached more plausible patches from fewer candidates: it
pulled in the definitions it needed instead of guessing at them. These are single runs,
and repeated `black#1` runs vary by a few patches either way, so treat the margin as
indicative. On `ansible#3`, the model retrieved
`get_class_definition("DistributionFactCollector")`.

---

## Four-approach comparison

One `evaluate-course-comparison` run: 22 cells covering 4 bugs × 4 approaches. The
three earlier approaches ran under both FL modes on the two `black` bugs, and under
perfect FL only on the two bugs FauxPy can't localize. Settings: OpenAI `gpt-5.4`,
`temperature 1.0`, `top_n=3`, `max_candidates=3`, `--retrieval-budget 3`, validation
budget 200. Every cell ran with the LLM assessor and similarity scoring. Full report
with every cell:
[`experiment_results/course_comparison/`](../experiment_results/course_comparison/README.md).

| Approach | What it is |
|---|---|
| **Template** (`a3-template`) | AST-mutation repair, FauxPy or oracle FL |
| **Single-shot** (`a4-single-shot`) | one LLM query per location, no enrichment, no retrieval |
| **Iterative** (`a4-iterative`) | multi-turn LLM repair with test-failure feedback |
| **Full LLM pipeline** (`a5-full-llm`) | LLM-FL → LLM repair with retrieval → LLM assessment |

The tables below are the report's per-bug summaries. For `black`, the earlier
approaches' summary column shows the auto-FL cell (both FL modes produced 0 plausible;
see the per-cell tables in the report). For `tornado#14` and `scrapy#2`, the earlier
approaches were given the **oracle** location.

**black#1**

| | Template | Single-shot | Iterative | **Full LLM pipeline** |
|---|---|---|---|---|
| FL source | auto/perfect | auto/perfect | auto/perfect | LLM-FL |
| Plausible patches | 0 | 0 | 0 | **6** |
| Exact-diff matches | 0 | 0 | 0 | 0 |
| Best assessment quality | — | — | — | 0.93 |
| Best context similarity (any candidate) | 0.13 | 0.18 | 0.18 | 0.40 |
| Time to first plausible | — | — | — | 41.1s |

**black#3**

| | Template | Single-shot | Iterative | **Full LLM pipeline** |
|---|---|---|---|---|
| FL source | auto/perfect | auto/perfect | auto/perfect | LLM-FL |
| Plausible patches | 0 | 0 | 0 | **4** |
| Exact-diff matches | 0 | 0 | 0 | 0 |
| Best assessment quality | — | — | — | 0.12 |
| Best context similarity (any candidate) | 0.03 | 0.04 | 0.04 | 0.06 |
| Time to first plausible | — | — | — | 31.0s |

**tornado#14**

| | Template | Single-shot | Iterative | **Full LLM pipeline** |
|---|---|---|---|---|
| FL source | perfect (oracle) | perfect (oracle) | perfect (oracle) | LLM-FL |
| Plausible patches | 1 | 3 | 1 | **9** |
| Exact-diff matches | 1 | 3 | 1 | **9** |
| Best assessment quality | 0.18 | 0.99 | 0.99 | 1.00 |
| Best context similarity (any candidate) | 1.00 | 1.00 | 1.00 | 1.00 |
| Time to first plausible | 0.7s | 5.9s | 1.8s | 14.5s |

**scrapy#2**

| | Template | Single-shot | Iterative | **Full LLM pipeline** |
|---|---|---|---|---|
| FL source | perfect (oracle) | perfect (oracle) | perfect (oracle) | LLM-FL |
| Plausible patches | 0 | 1 | 2 | **9** |
| Exact-diff matches | 0 | 0 | 0 | 0 |
| Best assessment quality | — | 0.98 | 0.98 | 0.98 |
| Best context similarity (any candidate) | 0.88 | 0.92 | 0.92 | 0.92 |
| Time to first plausible | — | 7.6s | 3.1s | 14.7s |

### Takeaways

- **The full pipeline improved on the best earlier approach on every bug:** black#1
  plausible 0 → 6, black#3 plausible 0 → 4, tornado#14 exact-diff 3 → 9, scrapy#2
  plausible 2 → 9. On tornado#14 and scrapy#2, it did this while localizing on its
  own, against approaches that were handed the oracle location.
- **LLM-FL reaches bugs FauxPy can't.** On scrapy and tornado, FauxPy is blocked by
  Python pins or dependency conflicts. The LLM localizer has no such dependency.
- **Plausible isn't the same as good, and the graded metrics show it.** On black#3,
  the pipeline's 4 plausible patches got a best assessment quality of only **0.12**
  and a best similarity of 0.06. They pass the tests but likely don't fix the bug the
  developer's way.
- **Similarity catches near-misses that exact diff hides.** On scrapy#2, single-shot,
  iterative and the full pipeline all scored **0.92** similarity while getting 0
  exact-diff matches. The fix landed in the right place, in nearly the right form.
- **The assessor is evidence, not an oracle.** In all 4 cells that contained an
  exact-diff match, the assessor ranked that patch first, but 2 of those cells had
  only one plausible patch. It also scored the template's exact developer fix on
  tornado#14 at just **0.18**, because a terse single-operator change can read as
  unconvincing in isolation. That is why the exact-diff metric is kept alongside it.
- **Retrieval was used sparingly.** Given the tools, the model chose to retrieve only
  once across the whole run (`get_function_definition` ×1, on black#1), in a cell
  that produced 6 plausible patches.
- **Cost.** The full pipeline makes more LLM calls (12–13 per cell here) and takes
  longer (17–81 s per cell, vs. 1–23 s for template repair on the same bugs).
