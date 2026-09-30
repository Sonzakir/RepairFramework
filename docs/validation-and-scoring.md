# Validation, scoring and ranking

"The tests pass" is where most repair tools stop, and it rewards patches that overfit
the test suite. Repair Framework judges a patch at several levels and records all of
them in the structured JSON output:

| Level | Question | Signal |
|---|---|---|
| **Plausible** | Does it fix the failing test *without breaking anything else*? | trigger test + regression-suite subset check |
| **Exact diff** | Is it byte-for-byte the developer's fix? | `is_correct` / `correct_count` (boolean) |
| **Context similarity** | How close is it to the developer's fix? | `context_similarity_score` (`0.0`–`1.0`) |
| **Assessed quality** | Does it genuinely fix the bug, or just overfit the tests? | LLM assessor `quality_score` (`0.0`–`1.0`), see [LLM repair](llm-repair.md#llm-based-patch-assessment) |
| **Rank** | Which patch should a developer look at first? | weighted ranker and/or assessor order |

## Plausibility: two halves

A patch is **plausible** only when both halves hold
([`repair/template/validator.py`](../src/apr_framework/repair/template/validator.py)):

1. **The failing test now passes.** The bug's trigger test command exits cleanly,
   with zero failures/errors and at least one passing test. The return-code and
   passed-count guards also reject patches that break import or collection.
2. **No previously passing test is broken.** A regression run of the bug's whole
   `test_file` introduces no new failures
   ([`repair/regression.py`](../src/apr_framework/repair/regression.py)).

In short: plausible = trigger passed + regression OK.

### The regression check

BugsInPy's `run_test.sh` usually contains only the single bug-triggering test. Running
it alone can't catch a patch that fixes the trigger but breaks something else. To
enforce the second half, the framework:

1. once per repair run, runs the bug's whole regression suite (the `test_file` from
   `bugsinpy_bug.info`, broadened from the trigger command) on the **unpatched**
   checkout, and records the **baseline failing set**;
2. for a candidate that already passes the trigger, runs the same suite **with the
   patch** and records its failing set;
3. accepts the patch only if its failing set is a **subset** of the baseline, meaning
   it introduces no new failure.

Comparing failing **sets** rather than counts is what makes this correct. Suppose a
patch fixes the trigger but breaks a different, previously passing test. Its failure
*count* matches the baseline, but its failing *set* is not a subset, so it is
rejected.

The baseline run happens once and is reused for every candidate. The same prepared
environment is reused by temporarily swapping the checkout's `bugsinpy_run_test.sh`
(`BugsInPyAdapter.run_tests(..., command=...)`). The check is on by default. Skip it
for speed with `--no-regression-check`; plausibility then falls back to the trigger
test alone.

## The two correctness metrics

Both metrics compare the candidate to the reference fix
(`projects/<project>/bugs/<id>/bug_patch.txt`, read via
`BugsInPyAdapter.get_reference_patch`). Both start from the same
**reformatting-neutral minimal diff**. The template generator rebuilds source with
`ast.unparse`, which reformats whole files, so a raw textual diff would never match.
Both the original and the patched source therefore go through `ast.unparse`, and the
cosmetic noise cancels out. The metrics differ in what they do with that diff.

Both live in [`repair/correctness.py`](../src/apr_framework/repair/correctness.py).

### Metric 1: exact diff match

`is_correct_patch` returns a **boolean**. It reduces each side (candidate and
reference) to its set of whitespace-normalized added/removed lines, and requires the
two sets to be **equal** for the touched file. The check is deliberately strict and
purely syntactic. It sets `RepairStatus.CORRECT` and feeds `correct_count`.

> **Why keep such a strict metric?** A byte-exact match is itself a useful signal.
> When an LLM keeps reproducing the developer's fix *1:1*, that suggests it is
> **overfitting to, or has memorised (data contamination of), the benchmark's public
> fixes** rather than reasoning its way to a fix. A high exact-match rate is
> something to *watch for*, not only to celebrate.

This is also why reports label the column **`Exact diff`** and never "correct". A
semantically correct fix written differently from the developer's scores 0 there, so
a 0 does not mean the patch is wrong.

### Metric 2: context similarity score

`context_similarity_score` is a **float in `[0.0, 1.0]`** that measures *how close*
the candidate's edit is to the developer's. It **includes the surrounding context
lines** that the exact metric discards.

The exact metric drops the unchanged lines around an edit and asks only "are the
changed lines identical?". The similarity metric keeps each edit as a **hunk**: its
added/removed lines plus a few unchanged context lines around them. It lines up the
candidate's hunk against the developer's hunk and measures their textual overlap with
Python's `difflib.SequenceMatcher`.

Comparison is *hunk-to-hunk*, not whole-file. A one-line fix in a 2,000-line file is
judged on its neighbourhood, not drowned out by thousands of identical untouched
lines. When the developer fix has several hunks, each candidate hunk is paired with
its most similar reference hunk, and the best pairing wins.

| Score | Meaning |
|---|---|
| `1.0` | identical edit in an identical neighbourhood (same fix, same place) |
| high, below `1.0` | the fix lands in the same place and is *nearly* the same, e.g. a renamed local variable. The exact metric says `False`; the score rewards the near-miss |
| low | a plausible patch that fixes the bug a *different* way. It shares only the surrounding context, so the score stays small |

A worked example (`total > self.limit` → `total >= self.limit`):

| Candidate | exact match | context similarity |
|---|:---:|:---:|
| identical to developer fix | `True` | `1.00` |
| same `>=` fix, local variable renamed | `False` | `~0.85` |
| different valid fix (early-return guard) | `False` | `~0.71` |

The exact metric puts the last two in the same `False` bucket; the similarity score
**separates** them. Across a run you can then report, for example, "exact matches
20% of the time, but ≥ 0.85 similarity 60% of the time". A **cluster of exact
`1.0`s** is the memorisation/contamination signal. A **spread of high-but-below-1.0
scores** is what independent reasoning towards the same region looks like.

Both metrics degrade gracefully: missing patch metadata, unreadable source or a
missing hunk yields `False` / `0.0`, never an exception. Neither is fooled by
`ast.unparse` cosmetics.

### Turning similarity scoring on

`--similarity-score` is **off by default**. With it off, the output is byte-for-byte
what it would be without the metric: the `context_similarity_score` and
`similarity_band` keys are *omitted* from `repair_results.json`, not set to `null`.

**Baseline, exact diff only (no flag):**

```text
$ python -m apr_framework repair --project scrapy --bug 2 \
    --technique llm --model gpt-5.4 --temperature 1.0 \
    --max-candidates 3 --top-n 3 --llm-provider openai-compatible \
    --llm-base-url https://api.openai.com/v1 --llm-api-key-env OPENAI_API_KEY \
    --system-prompt prompt1 --context-enrichment --no-iterative \
    --fl-mode perfect --budget 200 --timeout 120 --runs-dir runs

Run directory: /workspace/runs/run_xxx
Project:       scrapy
Bug ID:        2
Status:        plausible
Generated:     6 candidate(s)
Validated:     6 candidate(s)
Plausible:     6 patch(es)
Correct:       0 patch(es)
1st plausible: 12.1s
Total time:    14.0s
```

**With `--similarity-score`:**

```text
$ python -m apr_framework repair --project scrapy --bug 2 \
    --technique llm --model gpt-5.4 --temperature 1.0 \
    --max-candidates 3 --top-n 3 --llm-provider openai-compatible \
    --llm-base-url https://api.openai.com/v1 --llm-api-key-env OPENAI_API_KEY \
    --system-prompt prompt1 --context-enrichment --no-iterative \
    --fl-mode perfect --budget 200 --timeout 120 --similarity-score --runs-dir runs

Run directory: /workspace/runs/run_xxx
Project:       scrapy
Bug ID:        2
Status:        plausible
Generated:     6 candidate(s)
Validated:     6 candidate(s)
Plausible:     6 patch(es)
Correct:       0 patch(es)
1st plausible: 9.1s
Total time:    10.9s
###################################################################################
# Similarity scores for plausible patches (closeness to the developer fix, 0.0-1.0):
#  1.00        identical to the developer fix
#  0.85-0.99   very similar (nearly the same edit)
#  0.60-0.84   similar (recognizable overlap)
#  0.30-0.59   loosely similar
#  0.00-0.29   different (little in common)
###################################################################################
  Patch 1 (llm-1-0) -> 0.84  (similar (recognizable overlap))
  Patch 2 (llm-1-1) -> 0.84  (similar (recognizable overlap))
  Patch 3 (llm-1-2) -> 0.84  (similar (recognizable overlap))
  Patch 4 (llm-2-0) -> 0.84  (similar (recognizable overlap))
  Patch 5 (llm-2-1) -> 0.92  (very similar (nearly the same edit))
  Patch 6 (llm-2-2) -> 0.92  (very similar (nearly the same edit))
```

In this example, 0 of 6 plausible patches match the developer fix exactly, but two
score 0.92. The fix is in the right place, in nearly the right form.

The similarity flag adds `context_similarity_score` / `similarity_band` to every
plausible, ranked, assessed and all-results entry in the JSON. It never changes the
exact-diff `is_correct` / `correct_count` verdict.

## Tracked metrics

Every repair run records these in `repair_results.json`, in a `"metrics"` block, with
the headline counts also at the top level:

| Metric | Meaning |
|---|---|
| `total_candidates_generated` | candidate patches produced before budget capping |
| `candidates_validated` | candidates actually run against the test suite |
| `plausible_count` | candidates whose patched program passed all tests |
| `correct_count` | plausible candidates that **exactly** match the developer fix (Metric 1) |
| `time_to_first_plausible_seconds` | wall-clock time to the first plausible patch (`null` if none) |
| `total_wall_clock_seconds` | wall-clock time for the whole repair run |
| `rank_of_first_correct` | 1-based position of the first exact-diff match after ranking (when a ranker is active) |
| `llm_query_count` | LLM calls made (LLM backend only; absent for template runs) |
| `assessment_query_count` | assessor LLM calls (only with `--assess`) |

Each entry in `all_results` / `plausible_patches` also carries an `is_correct` flag
(Metric 1). With `--similarity-score`, it also carries a `context_similarity_score`
(Metric 2).

The CLI prints the headline numbers:

```text
Generated:     2 candidate(s)
Validated:     2 candidate(s)
Plausible:     0 patch(es)
Correct:       0 patch(es)
1st plausible: n/a
Total time:    1.0s
```

### Implementation: `RepairEvaluationRunner`

The pipeline is a dedicated
[`RepairEvaluationRunner`](../src/apr_framework/evaluation/repair_runner.py) that
implements the `EvaluationRunner` interface. It:

- drives the shared generate-and-validate loop ([`repair/run_loop.py`](../src/apr_framework/repair/run_loop.py));
- runs the correctness check on plausible patches;
- assembles the metrics (`RepairRunMetrics` in `core/models.py`);
- writes all run artifacts.

> **API decision.** The budget/early-stop loop was extracted from
> `TemplateRepairAlgorithm.repair()` into the standalone `run_validation_loop`. That
> function uses **only** the `RepairAlgorithm` methods (`generate_patches` /
> `validate_patch`). The algorithm's convenience `repair()` and the runner both call
> it, so there is a single loop implementation, and every backend (template or LLM)
> works with the runner unchanged.

---

## Patch ranking

When a repair run produces more than one plausible patch, the order a developer sees
them in matters. The **patch ranker** reorders plausible patches by a composite score
and keeps the original generation-order list as a baseline, so the two orderings can
be compared.

### Ranking formula

```
ranking_score = w1 * suspiciousness + w2 * patch_simplicity + w3 * operator_priority
```

All three components are normalized to `[0, 1]` before weighting. A higher score
surfaces the patch earlier.

| Component | Source | Rationale |
|---|---|---|
| `suspiciousness` | FL score of the targeted line (`patch.metadata["suspiciousness_score"]`), min-max normalized across the plausible batch (`(score − min) / (max − min)`) | the most evidence-based signal: it comes from running the actual tests |
| `patch_simplicity` | `1 − (changed_lines − min_changed_lines) / (max_changed_lines − min_changed_lines)` (min-max normalized, then inverted). A two-line change scores higher than a multi-line one | smaller patches overfit less; simpler is more trustworthy |
| `operator_priority` | fixed tier per operator key (`obo`=1.0, `comp`=0.9, `bool`=0.7, `negate`=0.6, `arith`=0.5, `return`=0.4) | off-by-one and comparison bugs are the most common single-statement Python fixes |

Default weights are **w1 = 0.6, w2 = 0.25, w3 = 0.15**. They are normalized
internally, so only relative sizes matter: `--ranker-weights 6,2.5,1.5` is identical
to the defaults.

### CLI

```bash
# Default: no ranking, generation order
python -m apr_framework repair --project black --bug 1

# Opt in to ranking with the default weights (0.6 / 0.25 / 0.15)
python -m apr_framework repair --project black --bug 1 --ranker weighted

# Custom weights
python -m apr_framework repair --project black --bug 1 \
    --ranker weighted --ranker-weights 0.7,0.2,0.1
```

`repair` and `evaluate-llm-repair` default to `--ranker none`; `evaluate-repair`
defaults to `--ranker weighted`. When a ranker is active, the CLI summary prints one
extra line:

```text
Rank of 1st correct (ranked): 2
```

### Output

`repair_results.json` always contains `plausible_patches` in generation order. With
a ranker active, it also contains `ranked_plausible_patches`: the same patches,
reordered. Each one is annotated with `rank_position`, plus a per-patch
`ranking_score` / `ranking_score_components` in `metadata`. `rank_of_first_correct`
(1-based) appears both at the top level and in the `metrics` block, so comparison
scripts can read it directly. With ranking off, `ranked_plausible_patches` is absent
(`null`).

### Architecture

The ranker is optional. `RepairEvaluationRunner` accepts
`ranker: PatchRanker | None = None` in its constructor; with `None`, the pipeline
runs unranked. The `PatchRanker` interface lives in `repair/ranking/base.py`, and
`WeightedCompositeRanker` is the current implementation. `create_ranker("weighted", ...)`
is the factory entry point for adding more strategies.

The LLM [assessor](llm-repair.md#llm-based-patch-assessment) is a separate, semantic
ranking signal. Both can run in the same command, and their orderings are recorded
independently.
