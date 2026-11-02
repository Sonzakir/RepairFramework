# Behaviour Diff leads · black#1

Each lead is a concrete test on which a candidate and the reference behave
differently. It is **not** a Break This Fix case: a case needs a witness whose
expectation is justified from the issue, the docs or a docstring, never from
"the developer fix does X" (docs/challenges.md). A divergence can also mean the
candidate is the better fix.

## run_342:llm-3-1 vs developer-fix

```text
tests.test_black.BlackTestCase.test_works_in_mono_process_only_environment__bdiff_v18  [variant v18: tests/test_black.py:1283 "print('hello')" → 'one.py']
  developer-fix   → failed: AssertionError: 1 != 0 :
  run_342:llm-3-1 → failed: AssertionError: 'one.py' != "print('hello')"
```
Variant of `BlackTestCase.test_works_in_mono_process_only_environment` in `tests/test_black.py`: line 1283 changes `"print('hello')"` to `'one.py'`.

```text
tests.test_black.BlackTestCase.test_works_in_mono_process_only_environment__bdiff_v36  [variant v36: tests/test_black.py:1286 "print('hello')" → 'one.py']
  developer-fix   → failed: AssertionError: 1 != 0 :
  run_342:llm-3-1 → failed: AssertionError: 'one.py\n' != 'print("hello")\n'
```
Variant of `BlackTestCase.test_works_in_mono_process_only_environment` in `tests/test_black.py`: line 1286 changes `"print('hello')"` to `'one.py'`.

```text
tests.test_black.BlackTestCase.test_works_in_mono_process_only_environment
  developer-fix   → 2 focus call(s)
  run_342:llm-3-1 → 1 focus call(s)
```

## run_402:llm-2-0 vs developer-fix

```text
tests.test_black.BlackTestCase.test_works_in_mono_process_only_environment__bdiff_v18  [variant v18: tests/test_black.py:1283 "print('hello')" → 'one.py']
  developer-fix   → failed: AssertionError: 1 != 0 :
  run_402:llm-2-0 → failed: AssertionError: 'one.py' != "print('hello')"
```
Variant of `BlackTestCase.test_works_in_mono_process_only_environment` in `tests/test_black.py`: line 1283 changes `"print('hello')"` to `'one.py'`.

```text
tests.test_black.BlackTestCase.test_works_in_mono_process_only_environment__bdiff_v36  [variant v36: tests/test_black.py:1286 "print('hello')" → 'one.py']
  developer-fix   → failed: AssertionError: 1 != 0 :
  run_402:llm-2-0 → failed: AssertionError: 'one.py\n' != 'print("hello")\n'
```
Variant of `BlackTestCase.test_works_in_mono_process_only_environment` in `tests/test_black.py`: line 1286 changes `"print('hello')"` to `'one.py'`.

```text
tests.test_black.BlackTestCase.test_works_in_mono_process_only_environment
  developer-fix   → 2 focus call(s)
  run_402:llm-2-0 → 1 focus call(s)
```

## run_402:llm-2-1 vs developer-fix

```text
tests.test_black.BlackTestCase.test_works_in_mono_process_only_environment__bdiff_v18  [variant v18: tests/test_black.py:1283 "print('hello')" → 'one.py']
  developer-fix   → failed: AssertionError: 1 != 0 :
  run_402:llm-2-1 → failed: AssertionError: 'one.py' != "print('hello')"
```
Variant of `BlackTestCase.test_works_in_mono_process_only_environment` in `tests/test_black.py`: line 1283 changes `"print('hello')"` to `'one.py'`.

```text
tests.test_black.BlackTestCase.test_works_in_mono_process_only_environment__bdiff_v36  [variant v36: tests/test_black.py:1286 "print('hello')" → 'one.py']
  developer-fix   → failed: AssertionError: 1 != 0 :
  run_402:llm-2-1 → failed: AssertionError: 'one.py\n' != 'print("hello")\n'
```
Variant of `BlackTestCase.test_works_in_mono_process_only_environment` in `tests/test_black.py`: line 1286 changes `"print('hello')"` to `'one.py'`.

```text
tests.test_black.BlackTestCase.test_works_in_mono_process_only_environment
  developer-fix   → 2 focus call(s)
  run_402:llm-2-1 → 1 focus call(s)
```

