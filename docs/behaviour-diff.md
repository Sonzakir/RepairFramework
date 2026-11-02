# Behaviour Diff

Several fixes pass the same tests. Do they do the same thing? Behaviour Diff runs
the project's own tests, plus variants of them with one literal changed, against
the buggy code, the reference fix and every candidate fix. While the tests run it
records what the changed functions receive, return, raise and leave behind. Then
it shows where the versions behave differently, on a concrete input.

No model is involved: the same versions always give the same report.

```bash
# BugsInPy: candidates from earlier runs and/or patch files; the developer fix is the reference
python -m apr_framework behaviour-diff --project scrapy --bug 2 \
  --from-run runs/run_173 --from-run runs/run_326 --patch my_fix.diff

# Local project (pytest or unittest); runs the tests and the candidates' code on this machine
python -m apr_framework behaviour-diff --repo PATH --python PATH/.venv/bin/python \
  --allow-local-exec --tests "python -m pytest tests/test_cache.py" \
  --patch agent.diff [--reference dev_fix.diff]
```

## Example: eight fixes that pass the tests

`runs/` holds 8 distinct test-passing scrapy#2 fixes from earlier LLM runs (40+
recorded plausible patches, most of them duplicates). scrapy#2 is
`LocalCache.__setitem__` crashing when `limit` is `None`. Every one of the 8 passes
all 23 original tests in the bug's test file. Behaviour Diff (`runs/run_409`,
shortened) still puts them into four classes, none of which matches the developer
fix:

```text
Behaviour classes (identical on all 39 compared tests):
  A  buggy
  B  developer-fix
  C  run_173:llm-1-0, run_173:llm-2-1
  D  run_240:llm-1-0, run_244:llm-iter-1-1, run_244:llm-iter-2-1
  E  run_255:llm-2-2
  F  run_326:llm-2-0, run_380:llm-3-0

Divergences from the reference fix:
  run_173:llm-1-0: differs on 1 test(s)
    ...LocalCacheTest.test_cache_with_limit__bdiff_v01  [variant v01: tests/test_utils_datatypes.py:249 2 → 0]
      developer-fix   → failed: AssertionError: 3 != 2
      run_173:llm-1-0 → error: KeyError: 'dictionary is empty'
  run_255:llm-2-2: differs on 1 test(s)
    ...LocalCacheTest.test_cache_with_limit__bdiff_v11  [variant v11: tests/test_utils_datatypes.py:252 'c' → 'a']
      developer-fix   → failed: AssertionError: 'a' unexpectedly found in LocalCache([('b', 2), ('a', 3)])
      run_255:llm-2-2 → failed: AssertionError: 'a' unexpectedly found in LocalCache([('a', 3), ('b', 2)])
  run_326:llm-2-0: differs on 10 test(s)
    ...LocalCacheTest.test_cache_without_limit
      call LocalCache.__init__(self=LocalCache(), limit=None)
      developer-fix   → returned None; after: self=LocalCache() {'limit': None}
      run_326:llm-2-0 → returned None; after: self=LocalCache() {'limit': inf}
```

Read as plain facts:

- **Class C** (`while self.limit is not None and ...`) raises `KeyError` for
  `LocalCache(limit=0)`, where the developer's `if self.limit:` treats 0 as "no
  limit".
- **Class E** (`... and key not in self`) does not evict when an existing key is
  set at capacity. The developer fix evicts the oldest entry and re-inserts the key
  at the end.
- **Class F** (`self.limit = ... float('inf')`) stores `inf` instead of `None`,
  which anything reading `cache.limit` sees.

None of these is a verdict: the variant's assertions are the original test's, so
both sides "fail" the variant. What matters is that they do *different* things.

## How it works

1. **Versions.** The buggy checkout, the reference fix (BugsInPy: the developer
   fix; local: `--reference`) and the candidates:
   - from `--patch` files;
   - from `--from-run` directories, either the plausible patches of a `repair` run
     or the diff of a `challenge agent-run`.

   Candidates that leave every file with the same bytes are merged (the report
   lists the merged labels). Sections that edit tests, fixtures or test
   configuration are dropped and listed: Behaviour Diff compares source behaviour.
2. **Focus functions.** Every function or method any version changes, by
   `file::Qualname`. Changes outside a function (module level, class body) can't
   be traced as calls; the report names them.
3. **Discovery.** The buggy version's suite runs once under the driver. The tests
   that call a focus function are the *reaching* tests.
4. **Amplification.** For each reaching test, variants that change one literal
   in its body.

   | Literal | Becomes |
   |---|---|
   | int | `0`, `1`, `-1`, `n+1`, `n-1` |
   | float | `0.0`, `-x`, `x+1.0` |
   | str | another string literal of the same test (a key collision), then `""` |
   | bool | its negation |
   | bytes | `b""` |

   Literals inside `assert` statements, `assert*`/`fail*` calls, docstrings and
   f-strings are left alone. Each variant is the original function with the
   literal spliced in place and a new name (`<test>__bdiff_vNN`), inserted right
   after the original. Decorators, class scope and `super()` therefore keep
   working. The test files get their original bytes back at the end.
5. **Observation.** Every version is applied in turn and the suite runs under the
   driver (`behaviour/driver.py`), in the target's own interpreter (Python 3.6+).
   The driver records:
   - per test: the outcome and the first line of the failure;
   - per focus call: the arguments at call time, the return value or the
     exception that propagated, and the arguments (`self` included) after the
     call when they changed.

   Values are rendered so that two runs agree:
   - memory addresses and temporary paths are masked;
   - sets are sorted;
   - objects without a `__repr__` show their attributes;
   - thread, event-loop, executor, I/O and mock objects show only their repr,
     because their internals depend on timing;
   - generators and coroutines are recorded once, with the arguments of their
     first call: they "return" at every `yield`/`await`, and how often a coroutine
     suspends can depend on timing;
   - long values are cut and carry a checksum of the rest;
   - `PYTHONHASHSEED` is pinned to 0.
6. **Noise calibration.** The buggy version and the reference are each observed
   twice, first and last. A test whose record differs between two runs of the
   same code depends on time, threads, randomness or ordering. It is left out of
   every comparison and listed as unstable.
7. **Compare.** For each candidate against the reference (or, without one,
   against the buggy code), the first difference per test: a different outcome
   or the first differing call. Versions with identical records on every compared
   test form one *behaviour class*.

**Restore guarantee.** After every version the checkout must fingerprint exactly
as it did before that version was applied. At the end it must fingerprint as
before the run, and the evaluator guard must find every test, fixture and
configuration file unchanged; otherwise the run stops with an error. Behaviour
Diff never edits files outside the checkout and its own scratch directory.

## Reading the report

| Field | Meaning |
|---|---|
| ORIGINAL TESTS | passed/failed/error/skipped on the project's own tests |
| VARIANTS | the same for the one-literal variants (they often fail on every version, because their assertions still expect the original input) |
| CLASS | the behaviour class; the same letter means no difference on any compared test |
| `differs on N test(s)` | tests where this candidate and the reference behave differently |
| `(k on the failing test, m elsewhere)` | without a reference: divergences from the buggy code inside the bug's failing test versus everywhere else. Changes elsewhere are behaviour the fix changes outside the reported bug; check they are intended |
| `no divergence on N tests` | the strongest statement Behaviour Diff makes: identical on these N tests. It is evidence, not proof of equivalence |

**Wording rule.** A divergence is a concrete input on which two versions behave
differently. It is not a verdict on which one is right, and it can mean the
candidate is the better fix.

## Outputs

| Path | Content |
|---|---|
| `runs/run_NNN/config.json` | command, target, versions with their SHA-256, options, the driver's SHA-256 |
| `behaviour_diff.json` | versions, focus, variants, unstable tests, classes, every divergence (up to 200 per comparison) |
| `observations/NN-<version>.json` | the driver's full record per version (outcomes and calls) |
| `leads.md` | divergences from the reference written up as Break This Fix *leads* |
| `execution.log` | progress, with per-version timings |

## Behaviour Diff and Break This Fix

A Break This Fix case needs a witness whose expectation is justified by the
issue, the docs or a docstring, never by "the developer fix does X"
([case contract](challenges.md#the-case-acceptance-contract)). Behaviour Diff
finds the inputs; a person decides which side the documented behaviour supports.
`leads.md` is the hand-off: each lead carries the variant's source location and
both sides' behaviour.

## Limits

- **Bounded by the project's tests.** Inputs come from the tests that reach the
  changed code and one-literal variants of them. If no test calls a changed
  function, the report says so and can only compare outcomes.
- **Only calls in the test process are traced.** Child processes
  (`multiprocessing`) are not traced. Neither are module-level or class-body
  changes.
- **Records are capped** (per test, per function, per value) to keep runs fast;
  the caps are in `config.json`.
- **Methods re-defined as variants** run with the original's class and
  decorators. A decorator that registers functions by name will also register the
  variant.
- **Local mode runs the tests and the candidates' code on your machine** without
  isolation, hence `--allow-local-exec`.
