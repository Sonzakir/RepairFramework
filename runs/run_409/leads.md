# Behaviour Diff leads · scrapy#2

Each lead is a concrete test on which a candidate and the reference behave
differently. It is **not** a Break This Fix case: a case needs a witness whose
expectation is justified from the issue, the docs or a docstring, never from
"the developer fix does X" (docs/challenges.md). A divergence can also mean the
candidate is the better fix.

## run_173:llm-1-0 vs developer-fix

```text
tests.test_utils_datatypes.LocalCacheTest.test_cache_with_limit__bdiff_v01  [variant v01: tests/test_utils_datatypes.py:249 2 → 0]
  developer-fix   → failed: AssertionError: 3 != 2
  run_173:llm-1-0 → error: KeyError: 'dictionary is empty'
```
Variant of `LocalCacheTest.test_cache_with_limit` in `tests/test_utils_datatypes.py`: line 249 changes `2` to `0`.

## run_173:llm-2-1 vs developer-fix

```text
tests.test_utils_datatypes.LocalCacheTest.test_cache_with_limit__bdiff_v01  [variant v01: tests/test_utils_datatypes.py:249 2 → 0]
  developer-fix   → failed: AssertionError: 3 != 2
  run_173:llm-2-1 → error: KeyError: 'dictionary is empty'
```
Variant of `LocalCacheTest.test_cache_with_limit` in `tests/test_utils_datatypes.py`: line 249 changes `2` to `0`.

## run_240:llm-1-0 vs developer-fix

```text
tests.test_utils_datatypes.LocalCacheTest.test_cache_with_limit__bdiff_v01  [variant v01: tests/test_utils_datatypes.py:249 2 → 0]
  developer-fix   → failed: AssertionError: 3 != 2
  run_240:llm-1-0 → error: KeyError: 'dictionary is empty'
```
Variant of `LocalCacheTest.test_cache_with_limit` in `tests/test_utils_datatypes.py`: line 249 changes `2` to `0`.

```text
tests.test_utils_datatypes.LocalCacheTest.test_cache_with_limit__bdiff_v11  [variant v11: tests/test_utils_datatypes.py:252 'c' → 'a']
  developer-fix   → failed: AssertionError: 'a' unexpectedly found in LocalCache([('b', 2), ('a', 3)])
  run_240:llm-1-0 → failed: AssertionError: 'a' unexpectedly found in LocalCache([('a', 3), ('b', 2)])
```
Variant of `LocalCacheTest.test_cache_with_limit` in `tests/test_utils_datatypes.py`: line 252 changes `'c'` to `'a'`.

## run_244:llm-iter-1-1 vs developer-fix

```text
tests.test_utils_datatypes.LocalCacheTest.test_cache_with_limit__bdiff_v01  [variant v01: tests/test_utils_datatypes.py:249 2 → 0]
  developer-fix        → failed: AssertionError: 3 != 2
  run_244:llm-iter-1-1 → error: KeyError: 'dictionary is empty'
```
Variant of `LocalCacheTest.test_cache_with_limit` in `tests/test_utils_datatypes.py`: line 249 changes `2` to `0`.

```text
tests.test_utils_datatypes.LocalCacheTest.test_cache_with_limit__bdiff_v11  [variant v11: tests/test_utils_datatypes.py:252 'c' → 'a']
  developer-fix        → failed: AssertionError: 'a' unexpectedly found in LocalCache([('b', 2), ('a', 3)])
  run_244:llm-iter-1-1 → failed: AssertionError: 'a' unexpectedly found in LocalCache([('a', 3), ('b', 2)])
```
Variant of `LocalCacheTest.test_cache_with_limit` in `tests/test_utils_datatypes.py`: line 252 changes `'c'` to `'a'`.

## run_244:llm-iter-2-1 vs developer-fix

```text
tests.test_utils_datatypes.LocalCacheTest.test_cache_with_limit__bdiff_v01  [variant v01: tests/test_utils_datatypes.py:249 2 → 0]
  developer-fix        → failed: AssertionError: 3 != 2
  run_244:llm-iter-2-1 → error: KeyError: 'dictionary is empty'
```
Variant of `LocalCacheTest.test_cache_with_limit` in `tests/test_utils_datatypes.py`: line 249 changes `2` to `0`.

```text
tests.test_utils_datatypes.LocalCacheTest.test_cache_with_limit__bdiff_v11  [variant v11: tests/test_utils_datatypes.py:252 'c' → 'a']
  developer-fix        → failed: AssertionError: 'a' unexpectedly found in LocalCache([('b', 2), ('a', 3)])
  run_244:llm-iter-2-1 → failed: AssertionError: 'a' unexpectedly found in LocalCache([('a', 3), ('b', 2)])
```
Variant of `LocalCacheTest.test_cache_with_limit` in `tests/test_utils_datatypes.py`: line 252 changes `'c'` to `'a'`.

## run_255:llm-2-2 vs developer-fix

```text
tests.test_utils_datatypes.LocalCacheTest.test_cache_with_limit__bdiff_v11  [variant v11: tests/test_utils_datatypes.py:252 'c' → 'a']
  developer-fix   → failed: AssertionError: 'a' unexpectedly found in LocalCache([('b', 2), ('a', 3)])
  run_255:llm-2-2 → failed: AssertionError: 'a' unexpectedly found in LocalCache([('a', 3), ('b', 2)])
```
Variant of `LocalCacheTest.test_cache_with_limit` in `tests/test_utils_datatypes.py`: line 252 changes `'c'` to `'a'`.

## run_326:llm-2-0 vs developer-fix

```text
tests.test_utils_datatypes.LocalCacheTest.test_cache_with_limit__bdiff_v01  [variant v01: tests/test_utils_datatypes.py:249 2 → 0]
  developer-fix   → failed: AssertionError: 3 != 2
  run_326:llm-2-0 → error: KeyError: 'dictionary is empty'
```
Variant of `LocalCacheTest.test_cache_with_limit` in `tests/test_utils_datatypes.py`: line 249 changes `2` to `0`.

```text
tests.test_utils_datatypes.LocalCacheTest.test_cache_without_limit
  call LocalCache.__init__(self=LocalCache(), limit=None)
  developer-fix   → returned None; after: self=LocalCache() {'limit': None}
  run_326:llm-2-0 → returned None; after: self=LocalCache() {'limit': inf}
```

```text
tests.test_utils_datatypes.LocalCacheTest.test_cache_without_limit__bdiff_v02  [variant v02: tests/test_utils_datatypes.py:261 10 → 0]
  call LocalCache.__init__(self=LocalCache(), limit=None)
  developer-fix   → returned None; after: self=LocalCache() {'limit': None}
  run_326:llm-2-0 → returned None; after: self=LocalCache() {'limit': inf}
```
Variant of `LocalCacheTest.test_cache_without_limit` in `tests/test_utils_datatypes.py`: line 261 changes `10` to `0`.

```text
tests.test_utils_datatypes.LocalCacheTest.test_cache_without_limit__bdiff_v04  [variant v04: tests/test_utils_datatypes.py:261 4 → 0]
  call LocalCache.__init__(self=LocalCache(), limit=None)
  developer-fix   → returned None; after: self=LocalCache() {'limit': None}
  run_326:llm-2-0 → returned None; after: self=LocalCache() {'limit': inf}
```
Variant of `LocalCacheTest.test_cache_without_limit` in `tests/test_utils_datatypes.py`: line 261 changes `4` to `0`.

```text
tests.test_utils_datatypes.LocalCacheTest.test_cache_without_limit__bdiff_v06  [variant v06: tests/test_utils_datatypes.py:261 10 → 1]
  call LocalCache.__init__(self=LocalCache(), limit=None)
  developer-fix   → returned None; after: self=LocalCache() {'limit': None}
  run_326:llm-2-0 → returned None; after: self=LocalCache() {'limit': inf}
```
Variant of `LocalCacheTest.test_cache_without_limit` in `tests/test_utils_datatypes.py`: line 261 changes `10` to `1`.

```text
tests.test_utils_datatypes.LocalCacheTest.test_cache_without_limit__bdiff_v08  [variant v08: tests/test_utils_datatypes.py:261 4 → 1]
  call LocalCache.__init__(self=LocalCache(), limit=None)
  developer-fix   → returned None; after: self=LocalCache() {'limit': None}
  run_326:llm-2-0 → returned None; after: self=LocalCache() {'limit': inf}
```
Variant of `LocalCacheTest.test_cache_without_limit` in `tests/test_utils_datatypes.py`: line 261 changes `4` to `1`.

```text
tests.test_utils_datatypes.LocalCacheTest.test_cache_without_limit__bdiff_v10  [variant v10: tests/test_utils_datatypes.py:261 10 → -1]
  call LocalCache.__init__(self=LocalCache(), limit=None)
  developer-fix   → returned None; after: self=LocalCache() {'limit': None}
  run_326:llm-2-0 → returned None; after: self=LocalCache() {'limit': inf}
```
Variant of `LocalCacheTest.test_cache_without_limit` in `tests/test_utils_datatypes.py`: line 261 changes `10` to `-1`.

```text
tests.test_utils_datatypes.LocalCacheTest.test_cache_without_limit__bdiff_v12  [variant v12: tests/test_utils_datatypes.py:261 4 → -1]
  call LocalCache.__init__(self=LocalCache(), limit=None)
  developer-fix   → returned None; after: self=LocalCache() {'limit': None}
  run_326:llm-2-0 → returned None; after: self=LocalCache() {'limit': inf}
```
Variant of `LocalCacheTest.test_cache_without_limit` in `tests/test_utils_datatypes.py`: line 261 changes `4` to `-1`.

```text
tests.test_utils_datatypes.LocalCacheTest.test_cache_without_limit__bdiff_v14  [variant v14: tests/test_utils_datatypes.py:261 10 → 11]
  call LocalCache.__init__(self=LocalCache(), limit=None)
  developer-fix   → returned None; after: self=LocalCache() {'limit': None}
  run_326:llm-2-0 → returned None; after: self=LocalCache() {'limit': inf}
```
Variant of `LocalCacheTest.test_cache_without_limit` in `tests/test_utils_datatypes.py`: line 261 changes `10` to `11`.

```text
tests.test_utils_datatypes.LocalCacheTest.test_cache_without_limit__bdiff_v16  [variant v16: tests/test_utils_datatypes.py:261 4 → 5]
  call LocalCache.__init__(self=LocalCache(), limit=None)
  developer-fix   → returned None; after: self=LocalCache() {'limit': None}
  run_326:llm-2-0 → returned None; after: self=LocalCache() {'limit': inf}
```
Variant of `LocalCacheTest.test_cache_without_limit` in `tests/test_utils_datatypes.py`: line 261 changes `4` to `5`.

## run_380:llm-3-0 vs developer-fix

```text
tests.test_utils_datatypes.LocalCacheTest.test_cache_with_limit__bdiff_v01  [variant v01: tests/test_utils_datatypes.py:249 2 → 0]
  developer-fix   → failed: AssertionError: 3 != 2
  run_380:llm-3-0 → error: KeyError: 'dictionary is empty'
```
Variant of `LocalCacheTest.test_cache_with_limit` in `tests/test_utils_datatypes.py`: line 249 changes `2` to `0`.

```text
tests.test_utils_datatypes.LocalCacheTest.test_cache_without_limit
  call LocalCache.__init__(self=LocalCache(), limit=None)
  developer-fix   → returned None; after: self=LocalCache() {'limit': None}
  run_380:llm-3-0 → returned None; after: self=LocalCache() {'limit': inf}
```

```text
tests.test_utils_datatypes.LocalCacheTest.test_cache_without_limit__bdiff_v02  [variant v02: tests/test_utils_datatypes.py:261 10 → 0]
  call LocalCache.__init__(self=LocalCache(), limit=None)
  developer-fix   → returned None; after: self=LocalCache() {'limit': None}
  run_380:llm-3-0 → returned None; after: self=LocalCache() {'limit': inf}
```
Variant of `LocalCacheTest.test_cache_without_limit` in `tests/test_utils_datatypes.py`: line 261 changes `10` to `0`.

```text
tests.test_utils_datatypes.LocalCacheTest.test_cache_without_limit__bdiff_v04  [variant v04: tests/test_utils_datatypes.py:261 4 → 0]
  call LocalCache.__init__(self=LocalCache(), limit=None)
  developer-fix   → returned None; after: self=LocalCache() {'limit': None}
  run_380:llm-3-0 → returned None; after: self=LocalCache() {'limit': inf}
```
Variant of `LocalCacheTest.test_cache_without_limit` in `tests/test_utils_datatypes.py`: line 261 changes `4` to `0`.

```text
tests.test_utils_datatypes.LocalCacheTest.test_cache_without_limit__bdiff_v06  [variant v06: tests/test_utils_datatypes.py:261 10 → 1]
  call LocalCache.__init__(self=LocalCache(), limit=None)
  developer-fix   → returned None; after: self=LocalCache() {'limit': None}
  run_380:llm-3-0 → returned None; after: self=LocalCache() {'limit': inf}
```
Variant of `LocalCacheTest.test_cache_without_limit` in `tests/test_utils_datatypes.py`: line 261 changes `10` to `1`.

```text
tests.test_utils_datatypes.LocalCacheTest.test_cache_without_limit__bdiff_v08  [variant v08: tests/test_utils_datatypes.py:261 4 → 1]
  call LocalCache.__init__(self=LocalCache(), limit=None)
  developer-fix   → returned None; after: self=LocalCache() {'limit': None}
  run_380:llm-3-0 → returned None; after: self=LocalCache() {'limit': inf}
```
Variant of `LocalCacheTest.test_cache_without_limit` in `tests/test_utils_datatypes.py`: line 261 changes `4` to `1`.

```text
tests.test_utils_datatypes.LocalCacheTest.test_cache_without_limit__bdiff_v10  [variant v10: tests/test_utils_datatypes.py:261 10 → -1]
  call LocalCache.__init__(self=LocalCache(), limit=None)
  developer-fix   → returned None; after: self=LocalCache() {'limit': None}
  run_380:llm-3-0 → returned None; after: self=LocalCache() {'limit': inf}
```
Variant of `LocalCacheTest.test_cache_without_limit` in `tests/test_utils_datatypes.py`: line 261 changes `10` to `-1`.

```text
tests.test_utils_datatypes.LocalCacheTest.test_cache_without_limit__bdiff_v12  [variant v12: tests/test_utils_datatypes.py:261 4 → -1]
  call LocalCache.__init__(self=LocalCache(), limit=None)
  developer-fix   → returned None; after: self=LocalCache() {'limit': None}
  run_380:llm-3-0 → returned None; after: self=LocalCache() {'limit': inf}
```
Variant of `LocalCacheTest.test_cache_without_limit` in `tests/test_utils_datatypes.py`: line 261 changes `4` to `-1`.

```text
tests.test_utils_datatypes.LocalCacheTest.test_cache_without_limit__bdiff_v14  [variant v14: tests/test_utils_datatypes.py:261 10 → 11]
  call LocalCache.__init__(self=LocalCache(), limit=None)
  developer-fix   → returned None; after: self=LocalCache() {'limit': None}
  run_380:llm-3-0 → returned None; after: self=LocalCache() {'limit': inf}
```
Variant of `LocalCacheTest.test_cache_without_limit` in `tests/test_utils_datatypes.py`: line 261 changes `10` to `11`.

```text
tests.test_utils_datatypes.LocalCacheTest.test_cache_without_limit__bdiff_v16  [variant v16: tests/test_utils_datatypes.py:261 4 → 5]
  call LocalCache.__init__(self=LocalCache(), limit=None)
  developer-fix   → returned None; after: self=LocalCache() {'limit': None}
  run_380:llm-3-0 → returned None; after: self=LocalCache() {'limit': inf}
```
Variant of `LocalCacheTest.test_cache_without_limit` in `tests/test_utils_datatypes.py`: line 261 changes `4` to `5`.

