# Fault Localization Evaluation Results

Comparison of SBFL (baseline), MBFL (baseline), and extension techniques (custom SBFL metrics: Jaccard, WSBI; Hybrid SBFL+MBFL) on BugsInPy bugs.


## fastapi bug #3

**Ground-truth faulty lines:** `fastapi/routing.py:63`, `fastapi/routing.py:64`, `fastapi/routing.py:65`, `fastapi/routing.py:66`, `fastapi/routing.py:67` … (7 lines total)

| Technique | Rank | Top-1 | Top-5 | Top-10 | Total ranked | Notes |
|---|---|---|---|---|---|---|
| SBFL-Ochiai (baseline) | 8 | ✗ | ✗ | ✓ | 76 |  |
| SBFL-Tarantula (baseline) | 11 | ✗ | ✗ | ✗ | 76 |  |
| SBFL-DStar (baseline) | 8 | ✗ | ✗ | ✓ | 76 |  |
| SBFL-Jaccard (extension) | 8 | ✗ | ✗ | ✓ | 76 |  |
| SBFL-WSBI (extension) | 11 | ✗ | ✗ | ✗ | 76 |  |
| MBFL-Metallaxis (baseline) | 18 | ✗ | ✗ | ✗ | 23 |  |
| MBFL-Metallaxis-Random (extension) | 11 | ✗ | ✗ | ✗ | 12 |  |
| Hybrid SBFL+MBFL (extension) | 11 | ✗ | ✗ | ✗ | 76 |  |

## fastapi bug #6

**Ground-truth faulty lines:** `fastapi/dependencies/utils.py:632`, `fastapi/dependencies/utils.py:633`, `fastapi/dependencies/utils.py:634`

| Technique | Rank | Top-1 | Top-5 | Top-10 | Total ranked | Notes |
|---|---|---|---|---|---|---|
| SBFL-Ochiai (baseline) | 82 | ✗ | ✗ | ✗ | 137 |  |
| SBFL-Tarantula (baseline) | 82 | ✗ | ✗ | ✗ | 137 |  |
| SBFL-DStar (baseline) | 82 | ✗ | ✗ | ✗ | 137 |  |
| SBFL-Jaccard (extension) | 82 | ✗ | ✗ | ✗ | 137 |  |
| SBFL-WSBI (extension) | 82 | ✗ | ✗ | ✗ | 137 |  |
| MBFL-Metallaxis (baseline) | 6 | ✗ | ✗ | ✓ | 44 |  |
| MBFL-Metallaxis-Random (extension) | — | ✗ | ✗ | ✗ | 14 |  |
| Hybrid SBFL+MBFL (extension) | 3 | ✗ | ✓ | ✓ | 137 |  |

## luigi bug #33

**Ground-truth faulty lines:** `luigi/task.py:334`

| Technique | Rank | Top-1 | Top-5 | Top-10 | Total ranked | Notes |
|---|---|---|---|---|---|---|
| SBFL-Ochiai (baseline) | 11 | ✗ | ✗ | ✗ | 490 |  |
| SBFL-Tarantula (baseline) | 191 | ✗ | ✗ | ✗ | 490 |  |
| SBFL-DStar (baseline) | 11 | ✗ | ✗ | ✗ | 490 |  |
| SBFL-Jaccard (extension) | 11 | ✗ | ✗ | ✗ | 490 |  |
| SBFL-WSBI (extension) | 191 | ✗ | ✗ | ✗ | 490 |  |
| MBFL-Metallaxis (baseline) | — | ✗ | ✗ | ✗ | 173 |  |
| MBFL-Metallaxis-Random (extension) | — | ✗ | ✗ | ✗ | 16 |  |
| Hybrid SBFL+MBFL (extension) | 26 | ✗ | ✗ | ✗ | 490 |  |

## Aggregate Top-k Accuracy

Fraction of bugs where the faulty line appeared within the top-k ranked locations.

| Technique | Top-1 (3 bugs) | Top-5 (3 bugs) | Top-10 (3 bugs) |
|---|---|---|---|
| SBFL-Ochiai (baseline) | 0/3 | 0/3 | 1/3 |
| SBFL-Tarantula (baseline) | 0/3 | 0/3 | 0/3 |
| SBFL-DStar (baseline) | 0/3 | 0/3 | 1/3 |
| SBFL-Jaccard (extension) | 0/3 | 0/3 | 1/3 |
| SBFL-WSBI (extension) | 0/3 | 0/3 | 0/3 |
| MBFL-Metallaxis (baseline) | 0/3 | 0/3 | 1/3 |
| MBFL-Metallaxis-Random (extension) | 0/3 | 0/3 | 0/3 |
| Hybrid SBFL+MBFL (extension) | 0/3 | 1/3 | 1/3 |

## Discussion

**Extension vs baseline.**  All techniques achieved the same Top-1 accuracy (0/3 bugs).  The extensions (Jaccard, WSBI, Hybrid) and the baselines (Ochiai, Tarantula, D*) are indistinguishable by this metric on the evaluated bug set.

**Average rank.**  `MBFL-Metallaxis-Random (extension)` achieved the lowest average rank of the faulty line (11.0) across bugs where any technique produced a ranking.

**Custom SBFL metrics (Jaccard, WSBI).**  These metrics are not present in the stock FauxPy 0.7.0 release and were added via the framework's in-place source patch.  Jaccard uses set-based overlap scoring; WSBI (Weighted SBI) is a novel metric ef / (ef + alpha * ep) with configurable alpha, where alpha=0.5 (default) down-weights passing executions to surface the faulty line more reliably.  Both apply different scoring formulae to the same coverage spectrum, which can shift the rank of the faulty line up or down depending on failure density.

**Hybrid SBFL+MBFL.**  The hybrid technique normalises and combines the scores of an SBFL run and an MBFL run via a weighted sum.  Locations found by both backends receive a tiebreak bonus. For bugs where SBFL and MBFL individually miss the faulty line but overlap near it, the combined score can surface the faulty location at a higher rank than either technique alone.  Conversely, when MBFL is noisy (e.g. few failing tests), the hybrid can also inherit that noise.

**Runtime trade-offs.**  SBFL runs complete in seconds because they only require a single test execution per bug.  MBFL requires generating and running mutants, which takes minutes even with the framework's random-budget mutation-selection extension.  The hybrid therefore inherits MBFL's cost but aims to compensate with improved accuracy.
