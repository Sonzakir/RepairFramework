# LLM-based repair

`repair --technique llm` generates free-form patches with any OpenAI-compatible model.
It implements the same `RepairAlgorithm` interface as the template engine, and it
plugs into the same validation, scoring and ranking pipeline unchanged.

The LLM engine is a set of composable strategies. Each is a flag and each can be
turned on or off on its own:

| Strategy | Flag | What it adds |
|---|---|---|
| [Single-shot](#single-shot-repair) | *(default)* | Buggy function + fault location → corrected function |
| [Context enrichment](#context-enrichment) | `--context-enrichment` (on by default) | Failing test source + traceback of the unpatched failure |
| [Few-shot examples](#few-shot-fix-examples) | `--few-shot N` | N real `(buggy → fixed)` pairs from other bugs of the same project |
| [Iterative feedback](#iterative-repair-with-test-failure-feedback) | `--iterative` | Multi-turn conversation that feeds each failed validation back to the model |
| [Context retrieval](#context-retrieval) | `--retrieval-budget N` | The model can call code-search tools before writing a patch |
| [Patch assessment](#llm-based-patch-assessment) | `--assess` | LLM quality score + rationale for each plausible patch |
| [LLM fault localization](fault-localization.md#llm-based-fault-localization) | `--fl-backend llm` | The LLM picks the repair targets too |

Chained together, they form the [end-to-end LLM pipeline](#end-to-end-llm-pipeline).

> **Provider setup.** See [Getting started → Configure an LLM provider](getting-started.md#configure-an-llm-provider).
> The examples below pass `--llm-base-url https://api.openai.com/v1 --llm-api-key-env OPENAI_API_KEY`
> explicitly; the `repair` command's built-in defaults still point at the legacy
> GPT@RUB gateway.

---

## Single-shot repair

### How it works

1. **Fault location.** The top-N suspicious locations from the FL step (automated or
   perfect) become repair targets, exactly as in template repair.
2. **Prompt construction.** For each location, `extract_function_source` isolates the
   enclosing function via `ast.parse`, falling back to a ±25-line window. A structured
   prompt then asks the LLM for a corrected version of that function.
3. **Patch extraction.** `extract_patch_with_source` finds a fenced code block in the
   response (` ```python … ``` `, falling back to a generic ` ``` … ``` ` block) and
   checks it parses with `ast.parse`. It splices the replacement into the original
   file lines and produces a `difflib` unified diff plus the full `patched_source`.
   The source is stashed in `PatchCandidate.metadata` so the correctness metrics can
   diff LLM patches.
4. **Validation.** Each diff is applied to the checkout with `patch -p0`, the test
   suite runs inside the executor container, and the file is restored
   unconditionally. This is the same flow as template validation.

Up to `--max-candidates` patches are sampled per location.

### Prompt design

The prompt is a two-message OpenAI-style conversation.

**System message**
([`repair/llm/prompts/prompt1.txt`](../src/apr_framework/repair/llm/prompts/prompt1.txt),
selected with `--system-prompt prompt1`):

```
You are an automated program repair tool. Your task is to fix a bug in a Python program.
You will be given the buggy code region and the fault location identified by a fault
localization tool. You may also be given the failing test and the traceback it produces
on the current code; when present, use them to infer the behaviour the fix must satisfy
and the concrete failure to eliminate. Return ONLY the corrected version of the provided function inside a
Python fenced code block (```python ... ```). Do not include any explanation, commentary,
or code outside the fenced block. Do not change the function signature.
The buggy code is shown with leading line numbers and a "-->" marker on the suspicious
line; these are annotations, NOT part of the source. Do NOT reproduce the line numbers or
the marker. Return only real Python source, preserving the original indentation exactly.
```

The "may also be given the failing test…" clause is used by
[context enrichment](#context-enrichment), which fills the `failing_test_source` and
`error_traceback` slots of `build_repair_prompt`. Without enrichment, the prompt shows
only the buggy function.

**User message** (built per location, three sections):

````
## Fault Location
File: <location.file_path>
Suspicious line: <location.line> (rank <location.rank>, score <score:.4f>)

## Buggy Code (lines <start>–<end>)
```python
<start>      def foo(x, y):
<start+1>        …
<N>  -->         <suspicious line, marked with -->
…
```

## Task
Fix the bug at line <location.line>. Return the corrected function in a Python fenced
code block. Keep the fix minimal — change as few lines as necessary.
````

**Design rationale**

| Choice | Reason |
|---|---|
| Return the full corrected function, not a diff | LLM-written diffs are often malformed. With a complete function, the framework builds the diff itself via `difflib`, which is always well-formed. |
| Line-number prefix on every line | Anchors the model to the exact line from the fault location and reduces off-by-one confusion. |
| `-->` marker on the suspicious line | Points the model's attention without removing the context it needs to reason about the fix. |
| Minimal-edit instruction | Discourages unneeded refactoring. Smaller patches are easier to review and less likely to add new bugs. |
| Structured `##` sections | Makes the prompt easy to extend: enrichment inserts new sections without changing existing ones. |

### Example invocations

```bash
# Perfect FL (oracle locations), OpenAI
export OPENAI_API_KEY="<your-openai-key>"
python -m apr_framework repair \
    --project black \
    --bug 1 \
    --technique llm \
    --model gpt-5.4 \
    --llm-base-url https://api.openai.com/v1 \
    --llm-api-key-env OPENAI_API_KEY \
    --fl-mode perfect \
    --temperature 1

# Automated SBFL localization drives the targets
python -m apr_framework repair \
    --project black \
    --bug 1 \
    --technique llm \
    --model gpt-4.1-2025-04-14 \
    --llm-base-url https://api.openai.com/v1 \
    --llm-api-key-env OPENAI_API_KEY \
    --fl-mode auto \
    --fl-family sbfl \
    --localization-metric ochiai \
    --top-n 3 \
    --max-candidates 5 \
    --budget 30

# Any other OpenAI-compatible endpoint and key variable
python -m apr_framework repair \
    --project black \
    --bug 1 \
    --technique llm \
    --model gpt-4.1-2025-04-14 \
    --llm-base-url https://my.endpoint/api \
    --llm-api-key-env MY_LLM_KEY \
    --fl-mode perfect
```

Example output:

```text
$ python -m apr_framework repair --project black --bug 1 --technique llm --model gpt-5.4 \
    --llm-base-url https://api.openai.com/v1 --llm-api-key-env OPENAI_API_KEY \
    --fl-mode perfect --temperature 1

Run directory: /workspace/runs/run_224
Project:       black
Bug ID:        1
Status:        plausible
Generated:     15 candidate(s)
Validated:     15 candidate(s)
Plausible:     6 patch(es)
Correct:       0 patch(es)
1st plausible: 63.3s
Total time:    142.7s
```

### CLI flags (LLM-specific)

| Flag | Default | Description |
|---|---|---|
| `--technique llm` | `template` | Selects the LLM engine. All existing FL/budget flags still apply |
| `--model` | `gpt-4.1-2025-04-14` | Model name sent to the API |
| `--temperature` | `0.8` | Sampling temperature in `[0.0, 2.0]` |
| `--max-candidates` | `5` | LLM calls per suspicious location (total candidates ≤ `top-n × max-candidates`) |
| `--llm-provider` | `openai-compatible` | Client implementation; currently only `openai-compatible` |
| `--llm-base-url` | legacy GPT@RUB gateway | API endpoint URL, e.g. `https://api.openai.com/v1` |
| `--llm-api-key-env` | `GPT_AT_RUB_API_KEY` | Environment variable holding the API key |
| `--system-prompt` | `prompt1` | System-prompt file stem under `repair/llm/prompts/` |
| `--context-enrichment` / `--no-context-enrichment` | *enabled* | Include the failing test's source + traceback in each prompt |
| `--few-shot N` | `0` | Prepend N `(buggy → fixed)` example pairs from other bugs of the same project; `0` disables |
| `--iterative` / `--no-iterative` | *disabled* | Multi-turn test-failure feedback loop |
| `--max-iterations N` | `5` | Max conversation turns per location (iterative only) |
| `--retrieval-budget N` | `0` | Allow up to N `RETRIEVE:` tool calls before patch generation; `0` disables |
| `--fl-backend {fauxpy,llm}` | `fauxpy` | Which tool localizes under `--fl-mode auto` |
| `--assess` / `--no-assess` | *disabled* | Assess plausible patches with the LLM and emit `assessed_plausible_patches` |
| `--assess-max-patches N` | *all* | Cap how many plausible patches are assessed |
| `--assess-system-prompt` | `assess_prompt1` | Prompt stem under `repair/assessment/prompts/` |

The shared flags (`--fl-mode`, `--fl-family`, `--budget`, `--top-n`, `--timeout`,
`--stop-on-first`, `--no-regression-check`, `--ranker`, `--similarity-score`, …)
behave the same for `--technique template` and `--technique llm`. `--operators` and
`--skip-localize` are ignored for LLM repair. LLM-specific flags are ignored for
template repair.

### Architecture

```
repair/llm/
    config.py             # LLMRepairConfig dataclass (model, temperature, budget, …)
    client.py             # LLMClient interface + OpenAICompatibleClient
    prompt_builder.py     # extract_function_source + build_repair_prompt
    context_enricher.py   # failing-test source + traceback gathering
    few_shot.py           # (buggy → fixed) example pairs from sibling bugs
    feedback.py           # iterative-loop feedback turns and stop conditions
    traceback_utils.py    # extract_last_traceback (shared)
    retrieval_tools.py    # get_function_definition, get_class_definition, find_usages
    retrieval_protocol.py # RETRIEVE parser and tool-result message builder
    retrieval_loop.py     # bounded retrieval pre-phase
    patch_extractor.py    # extract_patch_with_source → ExtractedPatch(diff_text, patched_source)
    algorithm.py          # LLMRepairAlgorithm (implements RepairAlgorithm)
repair/assessment/        # LLM patch assessor
repair/
    patch_applier.py      # apply_patch_and_validate: shared apply/test/restore helper
```

**Key design decisions**

- **Provider-agnostic `LLMClient`.** All provider-specific HTTP calls live in
  `OpenAICompatibleClient`, which reads the API key from the environment at call time
  and uses `stream=False`. A new provider only needs a new `LLMClient` subclass;
  `LLMRepairAlgorithm` doesn't change.
- **The prompt builder is a separate module.** `generate_patches` calls it and does not
  inline prompt logic. That keeps the algorithm readable, and context enrichment
  becomes a matter of extending `prompt_builder.py`, not editing the algorithm.
- **Shared `apply_patch_and_validate` helper.** The try/finally restoration guarantee
  and the two-half plausibility check (trigger + regression) are shared infrastructure
  in `repair/patch_applier.py`. The caller supplies backend-specific `apply_fn` and
  `restore_fn` callables. Template repair predates the helper and keeps its own
  equivalent.
- **`patch -p0` to apply diffs.** Diffs are generated by `difflib.unified_diff` with
  the absolute source path as `fromfile`/`tofile`. `patch -p0` applies them without
  stripping any path components, which is right for absolute paths. (`-p1` would strip
  the leading `/` and fail to find the file.)
- **Injected `LLMClient`.** The client is passed into `LLMRepairAlgorithm.__init__`
  rather than built internally, so tests can substitute a stub without network access.
- **Loop shape is a hook.** `generate_patches` and `validate_patch` are the primitives
  every backend implements. Single-shot runs use the default `repair_loop` (the shared
  `run_validation_loop`); [iterative repair](#iterative-repair-with-test-failure-feedback)
  overrides `repair_loop` only.

---

## Context enrichment

### Why it is needed

The bare prompt shows the model only the **buggy function**, with a `-->` marker on
the suspicious line. It never says *what is actually failing*. For any bug whose fix
can't be inferred from otherwise clean-looking code, the model is left guessing.

That is exactly what happened on **black#1** under perfect FL. The developer fix wraps
a call in `try/except OSError` and falls back to a mono-process executor:

```python
try:
    executor = ProcessPoolExecutor(max_workers=worker_count)
except OSError:
    executor = None          # system has no multiprocessing (e.g. AWS Lambda)
```

The failing test (`test_works_in_mono_process_only_environment`) forces that
`OSError`, but nothing in the buggy function *looks* wrong unless you know the failure
mode. Across a full run of 15 candidates, the model produced only cosmetic edits
(`os.cpu_count() or 1`, making a parameter `Optional`). **Not one** candidate
contained the `except OSError` fallback. The pipeline worked end-to-end; the model
simply had no way to know what to fix.

The root cause was diagnostic, not mechanical: **the prompt withheld the failure**.
Context enrichment closes that gap by showing the failure to the model.

### What it adds

Two prompt sections, each rendered only when its data is available:

| Section | Content | Why it helps |
|---|---|---|
| `## Failing Test` | Source of the bug-triggering test function | Shows the *behaviour the fix must satisfy*: the contract being repaired against |
| `## Failure Traceback` | The traceback that test produces on the **unpatched** checkout | Shows the *concrete failure mode* (e.g. `OSError` from `ProcessPoolExecutor`), so the model targets the real defect |

Both are gathered by
[`repair/llm/context_enricher.py`](../src/apr_framework/repair/llm/context_enricher.py):

- **Failing-test source.** The bug's `run_test.sh` is converted to a pytest node id via
  `load_pytest_targets`. The test file and method name are parsed from it, and the
  method's source is AST-extracted from the worktree.
- **Failure traceback.** The trigger test runs **once** on the unpatched checkout
  (cached for the whole repair run, like the regression baseline). The last
  `Traceback …` block is extracted from its output and capped in length.

Gathering happens once per run in `LLMRepairAlgorithm._failing_test_context_for`,
following the same lazy caching pattern as `_regression_context`. The two fields are
passed into `build_repair_prompt`.

### Why it is safe

Context enrichment can't regress the bare-prompt path or the template engine:

- **Best-effort, degrades to `None`.** Every gathering step (missing `run_test.sh`,
  unsupported test runner, no traceback) logs a warning and returns `None` instead of
  aborting the repair.
- **Byte-identical when off.** `build_repair_prompt` appends a section *only* when its
  value is non-`None`. With `--no-context-enrichment`, or when nothing could be
  gathered, the prompt is identical to the bare prompt.
- **No shared code touched.** The enricher runs its own trigger-test call rather than
  extending the shared `repair/regression.py`, so the template engine is unaffected.

### CLI

Enrichment is **on by default**. Turn it off for A/B comparisons:

```bash
# Default: enriched prompt (failing test + traceback included)
python -m apr_framework repair --project black --bug 1 --technique llm --fl-mode perfect

# Bare prompt (buggy function only), for comparison
python -m apr_framework repair --project black --bug 1 --technique llm --fl-mode perfect \
    --no-context-enrichment
```

The effective setting is recorded as `context_enrichment` in each run's `config.json`.

> **Cost.** Enrichment adds exactly **one** extra test run per repair run (to capture
> the trigger traceback), not one per candidate. That is negligible next to validating
> N candidates.

**Measured effect:** under perfect FL, enrichment took plausible patches from 4 to 16
across four bugs. See [Results](results.md#llm-repair-variants).

## Few-shot fix examples

The second enrichment strategy is *fix examples*. It prepends `N` real
`(buggy → fixed)` pairs from **other bugs of the same project**, so the model sees how
bugs in this codebase are typically fixed (style, size, output format) before tackling
the current one. It has its own flag, independent of `--context-enrichment`:

```bash
# Few-shot only (2 sibling examples), no failing-test/traceback enrichment:
python -m apr_framework repair --project black --bug 1 --technique llm --fl-mode perfect \
    --few-shot 2 --no-context-enrichment

# Both strategies together:
python -m apr_framework repair --project black --bug 1 --technique llm --fl-mode perfect \
    --few-shot 2
```

`--few-shot 0` (the default) disables it. The value is recorded as `few_shot_count` in
each run's `config.json`.

**How the examples are built** ([`repair/llm/few_shot.py`](../src/apr_framework/repair/llm/few_shot.py)):

- The project's other bug ids are listed and taken in **ascending order**, which is
  deterministic and reproducible. The bug under repair is **excluded**, so no answer
  leaks into the prompt.
- Each candidate's developer fix is read **offline** from its `bugs/<id>/bug_patch.txt`
  via `get_reference_patch`. There is **no checkout, compile or test run**, so the
  strategy has no side effects and no execution cost.
- The `(buggy, fixed)` snippets are rebuilt from the diff hunks (context + `-` lines →
  buggy, context + `+` lines → fixed) and rendered as a prepended
  `## Reference Fixes From This Project` section.
- Examples whose diff changes more than 40 lines are **skipped** because they bloat the
  prompt, and long snippets are trimmed. The loop keeps scanning until it has `N`
  usable examples. If it can't build any, the section is left out and the prompt is
  unchanged.

The examples are built **once per run** and cached
(`LLMRepairAlgorithm._few_shot_examples_for`). `--few-shot` and `--context-enrichment`
can each be on or off in any combination.

> Example: `--few-shot 2` on black#1 selects black#3 and black#4. It **skips black#2**,
> whose fix exceeds the 40-line cap.

## Inspecting the exact prompt

To see **exactly** what is sent to the LLM (system + user message, including any
enrichment, few-shot and retrieval sections), set `APR_LLM_DEBUG_PROMPT`. It is **off
by default** and does nothing when unset. It never changes the messages, the request
or the result, so it is safe to leave in place.

This run writes every prompt to `runs/prompt_dumps/prompt_NNNN.txt` (it assumes
`black 1` is already checked out and compiled):

```bash
APR_LLM_DEBUG_PROMPT=runs/prompt_dumps \
python -m apr_framework repair --project black --bug 1 \
  --technique llm \
  --fl-mode perfect \
  --model gpt-5.4 \
  --llm-base-url https://api.openai.com/v1 --llm-api-key-env OPENAI_API_KEY \
  --few-shot 2 \
  --max-candidates 5 --top-n 3 --budget 100
```

Then inspect the captured prompts:

```bash
ls runs/prompt_dumps/                                                 # prompt_0001.txt, ...
grep -l "Reference Fixes From This Project" runs/prompt_dumps/*.txt   # few-shot present
cat runs/prompt_dumps/prompt_0001.txt                                 # the full prompt
```

To stream prompts to the terminal instead, use `APR_LLM_DEBUG_PROMPT=stderr`.

> **Put the variable on the same line as the command.** A bare
> `APR_LLM_DEBUG_PROMPT=runs/prompt_dumps` on its own line does **not** apply to the
> next command; it is set and immediately discarded. Either prefix it inline with a
> trailing `\` as above, or `export` it first so it persists for the shell session.
> Check with `echo "$APR_LLM_DEBUG_PROMPT"` (empty means not set).

| `APR_LLM_DEBUG_PROMPT` value | Effect |
|---|---|
| *(unset / empty)* | Nothing; normal behaviour |
| `1`, `stderr`, `true`, `yes` | Print each prompt to **stderr** |
| any other value | Treat it as a **directory** and write one `prompt_NNNN.txt` per LLM call |

The hook sits at the single send point in
[`repair/llm/client.py`](../src/apr_framework/repair/llm/client.py)
(`OpenAICompatibleClient._dump_prompt_if_debugging`), so it captures the real messages
for every call: enriched, few-shot, retrieval, localization or plain. A failed write
(e.g. a bad path) is logged and ignored and never aborts the run.

---

## Iterative repair with test-failure feedback

### Why one query often isn't enough

A single LLM query often produces a patch that parses but still fails the test suite:
the model guessed wrong, or fixed only part of the fault. It can't learn from that
failure because it never sees it.

Inspired by **ChatRepair**, iterative mode turns the single query into a
*conversation*. After a patch fails validation, the exact test-failure output goes
back to the model as the next turn, and it is asked to revise its fix. The model can
then react to the concrete failure (the assertion that tripped, the exception raised)
instead of guessing blind a second time.

### How it works

Iterative mode runs **one fresh multi-turn conversation per FL location**, just as
single-shot mode queries the top-N locations independently. Each location starts from
the same `[system, user]` repair prompt (all enrichment flags still apply) and grows
turn by turn:

1. Ask the model for a corrected function; extract the patch and validate it.
2. If it fails, append the model's reply **and** a structured test-failure feedback
   turn (see below), then ask again.
3. Repeat until the location's conversation ends (stop conditions below), then move
   on to the next location if the global budget allows.

**Two independent counters** control how long the loop runs:

| Counter | Flag | Scope | Meaning |
|---|---|---|---|
| **Conversation budget** | `--max-iterations` | **Per location** | Max turns (LLM calls) spent on one location before giving up on it. Format-retry turns count too, so a location can never loop forever. |
| **Validation budget** | `--budget` | **Global** | Max test-suite executions across the whole bug, as in template and single-shot repair. It goes down only when a parseable patch is actually validated. |

**Interface extension: `repair_loop`.** The evaluation path (`RepairEvaluationRunner`)
originally called the module-level `run_validation_loop` directly. That function
generates *all* candidates up front, then validates them one by one. Iterative repair
breaks that split by design, because generating turn *N+1* requires knowing why turn
*N* failed. Overriding `repair()` alone would have been invisible to the real
evaluation and CLI path.

The fix is a **one-method extension of the `RepairAlgorithm` interface**: a
non-abstract `repair_loop(bug, checkout, *, budget, stop_on_first) -> LoopOutcome`.

- Its default implementation delegates to `run_validation_loop`, so the template
  engine and non-iterative LLM runs are **byte-for-byte unchanged**.
- `LLMRepairAlgorithm` overrides `repair_loop` only when `--iterative` is set.
- `RepairEvaluationRunner` calls `repair.repair_loop(...)`.
- `generate_patches()` and `validate_patch()` remain the primitives every backend
  implements, and both loop shapes reuse them.
- Both loop shapes finish through the same `build_loop_summary`, so the JSON output
  has the same shape either way.

### Feedback message design

Every turn after the first carries the previous attempt's failure, as a structured
prompt section built by [`repair/llm/feedback.py`](../src/apr_framework/repair/llm/feedback.py):

````
## Previous Attempt Failed (turn 2 of 5)
Your last fix did not pass validation — passed=12, failed=1, errors=0.

Traceback:
```
Traceback (most recent call last):
  File ".../test_foo.py", line 42, in test_bar
    assert result == 3
AssertionError: assert 4 == 3
```

Please analyze this failure and provide a revised fix. Return the corrected function
in a Python fenced code block, as before.
````

The traceback is extracted from the failed run's raw pytest output by the shared
`extract_last_traceback` helper, the same one context enrichment uses. If there is no
traceback, the message still carries the pass/fail/error counts. The raw output
travels in memory from `validate_patch` to the loop via the candidate's metadata. It
is filtered out of `repair_results.json`, so it never bloats run artifacts.

**Two failure kinds, two messages.** A patch can fail validation in two ways:

- a **trigger** failure: the bug-triggering test still fails;
- a **regression** failure: the patch *fixes* the target test but breaks a previously
  passing one.

For a regression failure, the trigger run is all green, so its counts and (absent)
traceback would mislead the model. `validate_patch` therefore classifies the failure
(`_classify_failure_kind`) and the feedback adapts:

- trigger failures get the traceback and counts shown above;
- regression failures get an explicit note that a previously passing test was broken,
  and a request to fix the bug *without* changing other behaviour.

### Stop conditions

A location's conversation ends as soon as **any** of these holds:

- **A plausible patch is found** (all tests pass, no regressions).
- **`--max-iterations` is used up** (the per-location turn budget).
- **`--budget` is used up** (the global test-suite execution budget).
- **The model signals it can't improve further**, detected by two cheap, explainable
  heuristics (not a semantic judgment):
  - **Identical diff twice in a row**: the model is repeating itself and making no
    progress.
  - **Refusal phrase**: a short case-insensitive substring list (e.g. *"cannot fix"*,
    *"unable to fix"*, *"no further changes"*) matched against the response.

An **unparsable reply** (no fenced code block) triggers a bounded *format-retry* turn:
a short "please reply with a valid fenced code block" nudge. After 2 consecutive
retries the location is abandoned, so prose-wrapped answers don't waste the whole
`--max-iterations` budget. Every early exit is logged at INFO/WARNING level with its
reason, so `runs/run_NNN/execution.log` explains why each conversation ended.

### CLI

| Flag | Default | Description |
|---|---|---|
| `--iterative` / `--no-iterative` | *disabled* | Enable the multi-turn feedback loop. Ignored with `--technique template`. |
| `--max-iterations N` | `5` | Max conversation turns per FL location. Only used with `--iterative`. |

These compose with **all** other LLM flags and shared flags. Both are recorded in each
run's `config.json` and in the config embedded in `repair_results.json`.

> `--max-candidates` and `--max-iterations` are different. Single-shot mode generates
> `--max-candidates` *independent* patches per location. Iterative mode runs up to
> `--max-iterations` *dependent* turns, each reacting to the last failure, and doesn't
> use `--max-candidates`.

```bash
# Iterative repair with automated FL:
python -m apr_framework repair --project black --bug 1 --technique llm \
    --iterative --max-iterations 5 --fl-mode auto

# Iterative repair with perfect FL and a capped test-suite budget:
python -m apr_framework repair --project black --bug 1 --technique llm \
    --iterative --max-iterations 5 --fl-mode perfect --budget 20

# Iterative, combined with context enrichment and few-shot:
python -m apr_framework repair --project black --bug 1 --technique llm \
    --iterative --max-iterations 5 --context-enrichment --few-shot 2
```

(Add `--model`, `--llm-base-url` and `--llm-api-key-env` for your provider.)

Iterative patches get an `llm-iter-<rank>-<turn>` `patch_id`, while single-shot
patches get `llm-<rank>-<attempt>`, so the mode is obvious in artifacts and logs.

### What test-failure information helps most

The expectation going in was that the **traceback** would matter most: specifically,
the final exception type and message, and the failing test's assertion line. It names
the concrete symptom (`AssertionError: assert 4 == 3`, an `IndexError`, a `TypeError`)
that a bare *"passed=12, failed=1"* cannot convey, and it shows the model the exact
expected-vs-actual mismatch. The pass/fail/error **counts** were expected to be
secondary, but still useful as a coarse progress signal (did the last edit fix some
tests while breaking others?).

The full pytest transcript is deliberately **not** forwarded; it is large and mostly
noise. Only the last traceback block (capped at 60 lines) is included, on the
hypothesis that the trailing failure is the actionable one.

The evaluation bore this out. The most useful piece of feedback was the
**assertion/traceback** from the failing trigger test; bare pass/fail counts alone
rarely moved the model. See [Results](results.md#llm-repair-variants).

---

## Context retrieval

`--retrieval-budget N` adds an optional retrieval pre-phase. Before generating a
patch, the model may ask for focused codebase information with one of three text
commands:

```text
RETRIEVE: get_function_definition("name")
RETRIEVE: get_class_definition("name")
RETRIEVE: find_usages("name")
```

The framework parses the command, runs static analysis over the checked-out BugsInPy
worktree, appends the result to the conversation, and lets the model continue. The
loop ends when the model stops asking for retrieval or the budget runs out. The
default is `0`, which leaves prompts and output unchanged.

```bash
python -m apr_framework repair \
  --project black \
  --bug 1 \
  --technique llm \
  --fl-mode perfect \
  --retrieval-budget 3 \
  --model gpt-5.4 \
  --llm-base-url https://api.openai.com/v1 \
  --llm-api-key-env OPENAI_API_KEY \
  --temperature 1
```

With retrieval on, each affected patch in `repair_results.json` gains a `retrieval`
block:

```json
{
  "retrieval": {
    "steps": [
      {
        "tool_name": "find_usages",
        "argument": "ProcessPoolExecutor",
        "result_summary": "black.py:621: executor = ProcessPoolExecutor(...)"
      }
    ],
    "step_count": 1,
    "stop_reason": "model_ready"
  }
}
```

Single-shot and iterative repair share the same pre-phase, because it runs inside the
shared `_build_location_prompt(...)` path before either mode asks for a patch.

Retrieval only pays off when the fault region depends on code the model can't see.
For self-contained regions, the model correctly declines to retrieve and patches
directly. For example, on `ansible#3` the model retrieved
`get_class_definition("DistributionFactCollector")`. Use
`APR_LLM_DEBUG_PROMPT=<dir>` (or `stderr`) to dump every prompt and inspect the
retrieval turns.

**Measured effect** on black#1: 4 plausible patches from 10 candidates with
retrieval, versus 3 from 15 without. See [Results](results.md#context-retrieval).

---

## LLM-based patch assessment

`--assess` adds an LLM assessor for plausible patches. The normal pipeline still
decides plausibility by running the tests, and the correctness metrics still compare
plausible patches with the developer fix.

On top of that, the assessor sends each plausible patch to the LLM together with:

- the original buggy function, or a source window when one is available;
- the unified diff;
- the previously failing test;
- the original traceback.

The model returns a score in `[0, 1]` and a short rationale.

The assessor is separate from the [weighted ranker](validation-and-scoring.md#patch-ranking).
The ranker uses static signals such as suspiciousness and patch size; the assessor
judges semantic quality. Both can run in one command, and their orderings are
recorded independently.

```bash
# Assess plausible template-repair patches
python -m apr_framework repair \
  --project black \
  --bug 1 \
  --technique template \
  --assess \
  --model gpt-5.4 \
  --llm-base-url https://api.openai.com/v1 \
  --llm-api-key-env OPENAI_API_KEY

# Assess plausible LLM-repair patches, capping how many are assessed
python -m apr_framework repair \
    --project black --bug 1 \
    --technique llm \
    --fl-mode perfect \
    --assess \
    --assess-max-patches 8 \
    --model gpt-5.4 \
    --llm-base-url https://api.openai.com/v1 \
    --llm-api-key-env OPENAI_API_KEY \
    --temperature 1 \
    --top-n 3 \
    --budget 20
```

With `--assess`, `repair_results.json` gains:

| Field | Meaning |
|---|---|
| `assessed_plausible_patches` | plausible patches sorted by descending `quality_score`, each with `rank_position` |
| `metadata.quality_score` | LLM quality score for an assessed patch |
| `metadata.assessment_rationale` | short explanation of the score |
| `rank_of_first_correct_by_assessment` | 1-based rank of the first exact-diff match after assessment sorting, or `null` |
| `metrics.assessment_query_count` | number of assessment LLM calls |

Without `--assess`, these fields are absent and the result schema is unchanged.

> **The assessor is evidence, not an oracle.** In the published comparison, it gave
> 0.18 to a template patch that exactly reproduces the developer fix, because a terse
> single-operator change can read as unconvincing in isolation. The exact-diff metric
> is kept alongside it for that reason. See [Results](results.md#four-approach-comparison).

---

## End-to-end LLM pipeline

The full pipeline chains the three LLM components into one fully LLM-driven run:

```text
LLM fault localization  ->  LLM repair with context retrieval  ->  LLM assessment
```

`--fl-backend llm` supplies the localization step, so the whole pipeline runs from one
command:

```bash
python -m apr_framework repair --project black --bug 1 \
  --technique llm --fl-backend llm --retrieval-budget 3 --assess --similarity-score \
  --model gpt-5.4 --temperature 1.0 \
  --llm-base-url https://api.openai.com/v1 --llm-api-key-env OPENAI_API_KEY \
  --max-candidates 3 --top-n 3
```

One model serves all three stages. `--model`, `--temperature`, `--llm-base-url` and
`--llm-api-key-env` are shared by the localizer, the repair engine and the assessor.
The localizer's own knobs (`--fl-system-prompt`, `--max-source-lines`,
`--source-window`) are accepted by `repair` too.

For how `--fl-backend` interacts with `--fl-mode`, see
[Choosing the FL source for `repair`](fault-localization.md#choosing-the-fl-source-for-repair).
In short: `--fl-mode perfect` always wins, and the default `--fl-backend fauxpy` keeps
every existing command line behaving as before.

To run the full pipeline against every other approach on the same bugs, use
[`bugsinpy evaluate-course-comparison`](evaluation.md#four-approach-comparison).

---

## FL-guided LLM repair and the perfect-FL baseline

The LLM engine runs under the same two fault-localization conditions as the template
engine:

1. **Automated FL.** Locations come from a localizer (FauxPy SBFL/Ochiai by default,
   `--fl-family sbfl|mbfl|hybrid`, or `--fl-backend llm`), and the top-N ranked lines go
   into the prompt.
2. **Perfect FL.** The ground-truth location is parsed from the BugsInPy developer fix
   (`bug_patch.txt`), bypassing FL entirely.

Both modes are recorded in every result file (`fl_mode` / `fl_backend`, also at the
top level of `repair_results.json`).

```bash
# Perfect FL (oracle): repair targets are the exact developer-fix lines
python -m apr_framework repair --project black --bug 1 --technique llm --fl-mode perfect

# Automated FL (SBFL / Ochiai) drives the repair targets
python -m apr_framework repair --project black --bug 1 --technique llm --fl-mode auto --fl-family sbfl
```

(Add `--model`, `--llm-base-url` and `--llm-api-key-env` for your provider.)

A hand-written single-bug baseline lives in
[`experiment_results/llm_repair/`](../experiment_results/llm_repair/). The full
multi-bug matrix is in [Results](results.md#llm-repair-variants).

---

## Command cookbook (OpenAI API)

```bash
# Single-shot, automated FL, bare prompt
python -m apr_framework repair --project scrapy --bug 2 \
  --technique llm --fl-mode auto --fl-family sbfl --localization-metric ochiai \
  --no-context-enrichment --few-shot 0 \
  --model gpt-5.4 --llm-base-url https://api.openai.com/v1 \
  --llm-api-key-env OPENAI_API_KEY --temperature 1 \
  --top-n 3 --max-candidates 3 --budget 200

# Single-shot, perfect FL, bare prompt
python -m apr_framework repair --project scrapy --bug 2 \
  --technique llm --fl-mode perfect \
  --no-context-enrichment --few-shot 0 \
  --model gpt-5.4 --llm-base-url https://api.openai.com/v1 \
  --llm-api-key-env OPENAI_API_KEY --temperature 1 \
  --top-n 3 --max-candidates 3 --budget 200

# Context enrichment
python -m apr_framework repair --project scrapy --bug 2 \
  --technique llm --fl-mode perfect \
  --context-enrichment --few-shot 0 \
  --model gpt-5.4 --llm-base-url https://api.openai.com/v1 \
  --llm-api-key-env OPENAI_API_KEY --temperature 1 \
  --top-n 3 --max-candidates 3 --budget 200

# Few-shot only
python -m apr_framework repair --project scrapy --bug 2 \
  --technique llm --fl-mode perfect \
  --no-context-enrichment --few-shot 2 \
  --model gpt-5.4 --llm-base-url https://api.openai.com/v1 \
  --llm-api-key-env OPENAI_API_KEY --temperature 1 \
  --top-n 3 --max-candidates 3 --budget 200

# Iterative, automated FL
python -m apr_framework repair --project black --bug 2 \
  --technique llm --fl-mode auto --fl-family sbfl \
  --iterative --max-iterations 5 --no-context-enrichment \
  --model gpt-5.4 --llm-base-url https://api.openai.com/v1 \
  --llm-api-key-env OPENAI_API_KEY --temperature 1 \
  --top-n 3 --budget 200

# Iterative, perfect FL
python -m apr_framework repair --project black --bug 1 \
  --technique llm --fl-mode perfect \
  --iterative --max-iterations 5 --no-context-enrichment \
  --model gpt-5.4 --llm-base-url https://api.openai.com/v1 \
  --llm-api-key-env OPENAI_API_KEY --temperature 1 \
  --top-n 3 --budget 200

# Retrieval, bare prompt
python -m apr_framework repair --project black --bug 1 \
  --technique llm --fl-mode perfect \
  --no-context-enrichment --few-shot 0 --retrieval-budget 3 \
  --model gpt-5.4 --llm-base-url https://api.openai.com/v1 \
  --llm-api-key-env OPENAI_API_KEY --temperature 1 \
  --top-n 3 --max-candidates 3 --budget 200

# Retrieval with enrichment
python -m apr_framework repair --project black --bug 1 \
  --technique llm --fl-mode perfect \
  --retrieval-budget 3 \
  --model gpt-5.4 --temperature 1 \
  --llm-base-url https://api.openai.com/v1 \
  --llm-api-key-env OPENAI_API_KEY \
  --top-n 3 --max-candidates 3 --budget 200

# Full pipeline: LLM-FL -> LLM repair + retrieval -> assessment + similarity
python -m apr_framework repair --project black --bug 1 \
  --technique llm --fl-backend llm \
  --retrieval-budget 3 --assess --similarity-score \
  --model gpt-5.4 --temperature 1 \
  --llm-base-url https://api.openai.com/v1 \
  --llm-api-key-env OPENAI_API_KEY \
  --top-n 3 --max-candidates 10 --budget 200
```
