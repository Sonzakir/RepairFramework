# Fix Grip

A fix passes the tests. Which parts of it do the tests actually depend on? Fix
Grip reverts each hunk of the fix, and mutates each line the fix adds, one at a
time. It runs the project's tests on every variant and reports which changes a
test notices. The parts no test notices are where a reviewer has to look by hand.

Coverage tools report which lines *run*. Fix Grip reports which lines are
*checked*: a line can run in every test and still be free to change without any
test failing.

```bash
python -m apr_framework grip --project black --bug 1 --patch runs/run_342/patches/llm-1-0.diff
python -m apr_framework grip --project scrapy --bug 2 --developer-fix
python -m apr_framework grip --repo PATH --python PATH/.venv/bin/python --allow-local-exec \
  --tests "python -m pytest tests" --patch fix.diff [--max-probes 24] [--seed grip]
```

## Examples

scrapy#2, one of the LLM fixes in `runs/` (`runs/run_415`, in Docker):

```text
Fix Grip · scrapy#2 · llm-iter-1-1
Suite: python -m unittest tests.test_utils_datatypes; with the fix 23/0/0 (pass/fail/error), buggy 22/0/1 (pass/fail/error); 23 stable passing test(s), 0 unstable left out
Failing test, run alone: python -m unittest tests.test_utils_datatypes.LocalCacheTest.test_cache_without_limit → with the fix passed
Tests the fix turns green: tests.test_utils_datatypes.LocalCacheTest.test_cache_without_limit (alone)

HUNK  WHERE                          VERDICT  EXECUTED  NOTICED BY
h1    scrapy/utils/datatypes.py:318  pinned   yes       test_cache_without_limit, test_cache_without_limit (alone)

Line and clause variants: 9 run, 8 noticed by a test, 1 not noticed
  c03 clause/drop_clause scrapy/utils/datatypes.py:318 (h1)
    - while self.limit is not None and len(self) >= self.limit and key not in self:
    + while self.limit is not None and len(self) >= self.limit:
```

The tests pin the fix, but not its `and key not in self` clause: drop it and every
test still passes. [Behaviour Diff](behaviour-diff.md) shows the input where that
clause matters. Re-setting an existing key at capacity behaves differently from
the developer fix.

black#1, the developer fix (`runs/run_416`):

```text
Fix Grip · black#1 · developer-fix
Suite: python -m unittest tests.test_black; with the fix 128/1/0 (pass/fail/error), buggy 128/1/0 (pass/fail/error); 128 stable passing test(s), 0 unstable left out
Failing test, run alone: python -m unittest tests.test_black.BlackTestCase.test_works_in_mono_process_only_environment → with the fix passed
Note: tests.test_black.BlackTestCase.test_works_in_mono_process_only_environment passes alone but fails inside its whole test file (test-order dependence); Fix Grip judges it alone.
Tests the fix turns green: tests.test_black.BlackTestCase.test_works_in_mono_process_only_environment (alone)

HUNK  WHERE             VERDICT          EXECUTED  NOTICED BY
h1    black.py:621-628  pinned           yes       test_works_in_mono_process_only_environment (alone)
h2    black.py:643-644  pinned           yes       test_works_in_mono_process_only_environment (alone)
h3    black.py:654      non-behavioural  -

Line and clause variants: 8 run, 7 noticed by a test, 1 not noticed
  p08 drop/drop black.py:644 (h2)
    - executor.shutdown()
    + pass
```

Both behavioural hunks are pinned, by the failing test run alone, and the type
annotation is recognised as non-behavioural. No test notices if the process pool
is never shut down.

## What it reports

**Per hunk** (a change block of the fix):

| Verdict | Meaning |
|---|---|
| `pinned` | reverting the hunk makes a test fail; the report names the tests |
| `NOT PINNED` | the hunk's lines run during the tests, but reverting it fails no test |
| `NOT EXECUTED` | no test runs the hunk's lines |
| `non-behavioural` | only annotations or docstrings differ; reverting cannot change behaviour, so it is not run |
| `unknown` | the revert could not be judged (it timed out or did not apply) |

**Per clause.** For every `and`/`or` on a line the fix adds or replaces, the fix
with one operand removed. This always runs. It shows whether each condition the
fix adds is one a test depends on. For example, scrapy#2's AI fix
`while self.limit is not None and len(self) >= self.limit and key not in self:`
keeps passing every test without `and key not in self`.

**Per line variant.** The variants are Strict Repro's deterministic probes:

- one of the template engine's AST operators on a line the fix adds or replaces;
- the statement replaced by `pass`.

At most `--max-probes` (24) are chosen, with a seeded round-robin. Each clause or
line variant is
`noticed` (a test that passes with the fix fails with the variant), `not noticed`,
or could not be judged. A variant that hangs is stopped after five times the
fix's own suite time (at least 60 s) and reported as a timeout, never as noticed.
The driver ends itself at that deadline, so no hanging process is left behind in
the executor.
`runs/run_NNN/not_noticed/<id>.diff` holds every variant no test noticed.

A variant no test notices is a change to the fix that the tests would accept. It
may be equivalent to the fix; Fix Grip never calls it wrong (the same rule as
Strict Repro's *unconfirmed* probes).

## How it works

1. The fix's sections that edit tests or test configuration are dropped and
   listed. Fix Grip measures the project's tests against the fix's source.
2. The bug's failing test runs **alone**, the way plausibility checks it: twice
   with the fix, once on the buggy code. A test can pass alone and fail inside
   its whole file. Under the developer fix, black#1's failing test does exactly
   that in Python 3.8, because an earlier test leaves asyncio state behind. Fix
   Grip reports that and judges the test alone.
3. The fix is observed twice with the evidence driver
   ([Behaviour Diff](behaviour-diff.md)), which also records which of the fix's
   changed lines run (inside functions). The buggy version is observed once.
   Tests that pass in both runs are the *stable passing* tests; the others are
   unstable and ignored.
4. Every hunk revert, clause drop and line variant is applied in turn. The suite
   runs once, and the failing test runs alone once. A variant is noticed when a
   test that passes with the fix fails, either in the suite or alone. An import
   or collection failure counts as noticed.
5. The checkout must fingerprint exactly as before after every variant, and the
   evaluator guard must find every test, fixture and configuration file unchanged
   at the end. SIGTERM and SIGHUP end the command through the normal restore
   path, so an interrupted run leaves the project as it found it.

## Relation to Strict Repro

Strict Repro measures a *generated* repro test against wrong fixes that an
oracle has confirmed. Fix Grip measures the *project's existing tests* against
variants of *any* fix (agent, LLM or developer). It adds hunk-level reverts and
line execution, and needs neither a model nor an oracle.

## Outputs

| Path | Content |
|---|---|
| `runs/run_NNN/config.json` | command, target, the fix with its SHA-256, options |
| `grip.json` | hunks with verdicts, every variant with its status and the tests that noticed it, executed lines, unstable tests |
| `not_noticed/<id>.diff` | each variant no test noticed, as a diff against the fix |
| `execution.log` | progress and the variant timeout used |

## Limits

- **Only changes inside functions are watched for execution.** A module-level hunk
  is reported as pinned or not pinned, never as "not executed".
- **One variant, one suite run.** The cost grows with `--max-probes`. The suite is
  the bug's test file (BugsInPy) or your `--tests`.
- **Child processes are not traced**, so line execution inside them is not seen.
  Their test outcomes still count.
