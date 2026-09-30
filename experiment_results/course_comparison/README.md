# Course-Wide Comparison: All Repair Approaches

Every approach built over the course, run on 4 BugsInPy bug(s): traditional template repair, single-shot LLM repair, iterative LLM repair, and the fully LLM-driven pipeline (LLM-FL -> LLM repair with context retrieval -> LLM assessment).

Generated with the model: `gpt-5.4` · per-cell validation budget: 200


**On the metrics.**  **`Exact diff`** counts patches whose diff matches the developer fix byte-for-byte. It is deliberately *not* called *correct*: a semantically correct fix written differently from the developer's scores 0 here, so a 0 in this column is not a claim that the patch is wrong. It is the framework's data-contamination signal, and nothing more (if the fix is not extremely atomic). Two graded metrics carry the actual quality judgment, and every cell of every column is measured with both:

- **Assessment quality score** (`0.0`-`1.0`) -> the LLM assessor's judgment of whether the patch genuinely fixes the bug or just overfits the test suite. This is the semantic signal the pass/fail oracle cannot give.
- **Context similarity score** (`0.0`-`1.0`) -> how close the patch's edit is to the developer's, including the surrounding context lines. `1.00` is a byte-exact match; a high-but-sub-1.0 score is a near-miss that `Exact diff` reports as a flat 0. It is scored on **every candidate an approach generated, plausible or not**, so an approach whose patches all failed the test suite still reports how close it came, otherwise its column would be empty and it could not be compared at all.


## Course-wide comparison (per bug)

### black bug #1

| | A3 (template) | A4 simple | A4 iterative | A5 full LLM |
|---|---|---|---|---|
| FL source | auto/perfect | auto/perfect | auto/perfect | LLM-FL |
| Repair | traditional | LLM | LLM (iterative) | LLM + retrieval |
| Assessment | test pass/fail | test pass/fail | test pass/fail | LLM assessor |
| Plausible patches | 0 (auto) | 0 (auto) | 0 (auto) | 6 |
| Exact-diff matches (not a correctness verdict) | 0 (auto) | 0 (auto) | 0 (auto) | 0 |
| **Best assessment quality score** | — | — | — | 0.93 |
| **Best context similarity score** (any candidate) | 0.13 | 0.18 | 0.18 | 0.40 |
| Time to first plausible | — | — | — | 41.1s |



### black bug #3

| | A3 (template) | A4 simple | A4 iterative | A5 full LLM |
|---|---|---|---|---|
| FL source | auto/perfect | auto/perfect | auto/perfect | LLM-FL |
| Repair | traditional | LLM | LLM (iterative) | LLM + retrieval |
| Assessment | test pass/fail | test pass/fail | test pass/fail | LLM assessor |
| Plausible patches | 0 (auto) | 0 (auto) | 0 (auto) | 4 |
| Exact-diff matches (not a correctness verdict) | 0 (auto) | 0 (auto) | 0 (auto) | 0 |
| **Best assessment quality score** | — | — | — | 0.12 |
| **Best context similarity score** (any candidate) | 0.03 | 0.04 | 0.04 | 0.06 |
| Time to first plausible | — | — | — | 31.0s |



### tornado bug #14

| | A3 (template) | A4 simple | A4 iterative | A5 full LLM |
|---|---|---|---|---|
| FL source | perfect only (auto: FauxPy cannot localize) | perfect only (auto: FauxPy cannot localize) | perfect only (auto: FauxPy cannot localize) | LLM-FL |
| Repair | traditional | LLM | LLM (iterative) | LLM + retrieval |
| Assessment | test pass/fail | test pass/fail | test pass/fail | LLM assessor |
| Plausible patches | 1 (perfect) | 3 (perfect) | 1 (perfect) | 9 |
| Exact-diff matches (not a correctness verdict) | 1 (perfect) | 3 (perfect) | 1 (perfect) | 9 |
| **Best assessment quality score** | 0.18 | 0.99 | 0.99 | 1.00 |
| **Best context similarity score** (any candidate) | 1.00 | 1.00 | 1.00 | 1.00 |
| Time to first plausible | 0.7s | 5.9s | 1.8s | 14.5s |



### scrapy bug #2

| | A3 (template) | A4 simple | A4 iterative | A5 full LLM |
|---|---|---|---|---|
| FL source | perfect only (auto: FauxPy cannot localize) | perfect only (auto: FauxPy cannot localize) | perfect only (auto: FauxPy cannot localize) | LLM-FL |
| Repair | traditional | LLM | LLM (iterative) | LLM + retrieval |
| Assessment | test pass/fail | test pass/fail | test pass/fail | LLM assessor |
| Plausible patches | 0 (perfect) | 1 (perfect) | 2 (perfect) | 9 |
| Exact-diff matches (not a correctness verdict) | 0 (perfect) | 0 (perfect) | 0 (perfect) | 0 |
| **Best assessment quality score** | — | 0.98 | 0.98 | 0.98 |
| **Best context similarity score** (any candidate) | 0.88 | 0.92 | 0.92 | 0.92 |
| Time to first plausible | — | 7.6s | 3.1s | 14.7s |




## Every cell (per bug × approach × FL mode)

### black bug #1

| Approach | FL mode | Queries | Generated | Plausible | Exact diff | Best quality | Best similarity (any cand.) | Retrieval steps | 1st plausible (s) | Total (s) | Outcome |
|---|---|---|---|---|---|---|---|---|---|---|---|
| a3-template | auto | — | 4 | 0 | 0 | — | 0.13 | 0 | — | 10.3 | failed |
| a3-template | perfect | — | 0 | 0 | 0 | — | — | 0 | — | 1.2 | no_patch |
| a4-single-shot | auto | 9 | 9 | 0 | 0 | — | 0.18 | 0 | — | 27.4 | failed |
| a4-single-shot | perfect | 9 | 9 | 0 | 0 | — | 0.45 | 0 | — | 31.1 | failed |
| a4-iterative | auto | 15 | 15 | 0 | 0 | — | 0.18 | 0 | — | 37.0 | failed |
| a4-iterative | perfect | 15 | 15 | 0 | 0 | — | 0.45 | 0 | — | 50.3 | failed |
| a5-full-llm | llm-fl | 13 | 7 | 6 | 0 | 0.93 | 0.40 | 1 | 41.1 | 80.9 | plausible |

### black bug #3

| Approach | FL mode | Queries | Generated | Plausible | Exact diff | Best quality | Best similarity (any cand.) | Retrieval steps | 1st plausible (s) | Total (s) | Outcome |
|---|---|---|---|---|---|---|---|---|---|---|---|
| a3-template | auto | — | 1 | 0 | 0 | — | 0.03 | 0 | — | 7.3 | failed |
| a3-template | perfect | — | 10 | 0 | 0 | — | 0.21 | 0 | — | 23.2 | failed |
| a4-single-shot | auto | 9 | 9 | 0 | 0 | — | 0.04 | 0 | — | 22.4 | failed |
| a4-single-shot | perfect | 3 | 0 | 0 | 0 | — | — | 0 | — | 25.2 | no_patch |
| a4-iterative | auto | 15 | 15 | 0 | 0 | — | 0.04 | 0 | — | 39.7 | failed |
| a4-iterative | perfect | 2 | 0 | 0 | 0 | — | — | 0 | — | 11.2 | no_patch |
| a5-full-llm | llm-fl | 12 | 9 | 4 | 0 | 0.12 | 0.06 | 0 | 31.0 | 68.9 | plausible |

### tornado bug #14

| Approach | FL mode | Queries | Generated | Plausible | Exact diff | Best quality | Best similarity (any cand.) | Retrieval steps | 1st plausible (s) | Total (s) | Outcome |
|---|---|---|---|---|---|---|---|---|---|---|---|
| a3-template | perfect | — | 6 | 1 | 1 | 0.18 | 1.00 | 0 | 0.7 | 1.2 | correct |
| a4-single-shot | perfect | 3 | 3 | 3 | 3 | 0.99 | 1.00 | 0 | 5.9 | 6.5 | correct |
| a4-iterative | perfect | 1 | 1 | 1 | 1 | 0.99 | 1.00 | 0 | 1.8 | 1.8 | correct |
| a5-full-llm | llm-fl | 12 | 9 | 9 | 9 | 1.00 | 1.00 | 0 | 14.5 | 17.2 | correct |

### scrapy bug #2

| Approach | FL mode | Queries | Generated | Plausible | Exact diff | Best quality | Best similarity (any cand.) | Retrieval steps | 1st plausible (s) | Total (s) | Outcome |
|---|---|---|---|---|---|---|---|---|---|---|---|
| a3-template | perfect | — | 5 | 0 | 0 | — | 0.88 | 0 | — | 1.2 | failed |
| a4-single-shot | perfect | 6 | 6 | 1 | 0 | 0.98 | 0.92 | 0 | 7.6 | 8.0 | plausible |
| a4-iterative | perfect | 3 | 3 | 2 | 0 | 0.98 | 0.92 | 0 | 3.1 | 4.8 | plausible |
| a5-full-llm | llm-fl | 12 | 9 | 9 | 0 | 0.98 | 0.92 | 0 | 14.7 | 17.7 | plausible |


## Analysis


**Did LLM-FL outperform SBFL/MBFL?**  Overall, fault localization methods based on LLMs performed better than the methods used in FauxPy. Each repair attempt presented above can be considered an example of this trend.

However, one important limitation should be noted. For projects in BugsInPy that rely on older Python versions, FauxPy fault localization methods could not be used in some cases because FauxPy had to be pinned to a specific version or because of conflicting project dependencies. Scrapy and Tornado are examples of projects affected by dependency conflicts.

In addition, the results from the other experiments show that methods using LLM-based fault localization were more successful than those using FauxPy-based fault localization in generating plausible patches and achieving higher similarity scores.

**Did context retrieval help?**   
In this framework's setting, the context retrieval process was left entirely to the LLM. We provided the model with the necessary context retrieval tools, but the model itself decided whether to perform retrieval and which tool to use.

Surprisingly, in our experiments, the model chose to perform context retrieval only once. The model spent **1 retrieval step across 1 cell** (`get_function_definition` ×1), which produced **6 plausible patches** and **0 exact-diff matches**.

Although the context-retrieval setting achieved successful results, the very limited number of tool calls made by the model was noteworthy.

- `black#1` (a5-full-llm): 1 retrieval step(s) across 1 prompt(s) — `get_function_definition` ×1; 6 plausible, 0 exact-diff.

**Was patch assessment useful?**  The assessor scored 36 plausible patch(es) across 9 cell(s).

In 4 of 4 cell(s) that contained an exact-diff match, the assessor ranked that patch **first**. That is weak evidence, though: 2 of those cell(s) held only a single plausible patch, so ranking it first was unavoidable rather than a judgment.

**The assessor is not itself an oracle.** It scored *below 0.5* a patch that exactly reproduces the developer fix in: `tornado#14` (a3-template, quality 0.18). A low quality score is therefore evidence, not a verdict: the model judges a fix on how the edit reads in isolation, and a terse single-operator change (the kind template repair emits) can look unconvincing to it even when it is precisely what the developer wrote. This is the honest limit of LLM-based assessment, and the reason the exact-diff verdict is retained rather than replaced.


The similarity score also caught **near-misses the strict verdict hides**: `scrapy#2` (a4-single-shot, similarity 0.92), `scrapy#2` (a4-iterative, similarity 0.92), `scrapy#2` (a5-full-llm, similarity 0.92) scored ≥0.85 against the developer fix while still counting as 0 exact-diff matches — the fix landed in the right place, in nearly the right form.

**Where did the full LLM pipeline improve, and where did it regress?**  It improved on the best prior approach for: `black#1` (plausible 0 → 6); `black#3` (plausible 0 → 4); `tornado#14` (exact-diff 3 → 9); `scrapy#2` (plausible 2 → 9). Almost in every bug setting in compare to other methods and LLM-single-shot method

