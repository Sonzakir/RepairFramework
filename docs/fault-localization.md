# Fault localization

Fault localization (FL) answers *where is the bug?* It ranks suspicious lines (or
functions) in the program under repair. Every backend emits the same
`LocalizationResult` of ranked `RankedLocation`s, so the repair engines use any of
them without special cases.

| Backend | How it ranks | Selected with |
|---|---|---|
| **SBFL** (spectrum-based) | coverage of passing vs. failing tests: Ochiai, Tarantula, D*, plus the **Jaccard** and **WSBI** extensions | `localize` (default), `--metric` |
| **MBFL** (mutation-based) | how mutants at a line change test outcomes: Metallaxis, MUSE, with an optional **budgeted random** selector | `localize --mbfl` |
| **Hybrid** | min-max-normalized, weighted merge of SBFL + MBFL | `localize --family hybrid` |
| **LLM** | an LLM reads the failing test, its traceback and anchored source, then returns a JSON ranking | `localize --backend llm` |
| **Perfect (oracle)** | lines changed by the developer fix (`bug_patch.txt`); an upper bound, used as a baseline | `repair --fl-mode perfect` |

All localization commands need a checked-out, compiled bug:

```bash
python -m apr_framework bugsinpy setup
python -m apr_framework bugsinpy checkout black 1
python -m apr_framework bugsinpy compile black 1
```

---

## SBFL / MBFL / Hybrid with FauxPy

The `localize` command runs [FauxPy](https://pypi.org/project/fauxpy/) 0.7.0 inside
the prepared BugsInPy checkout. It parses the suspicious-location tables and prints
ranked locations for the selected metric.

FauxPy expects BugsInPy `run_test.sh` files that invoke pytest directly. The examples
below use `black 1`, whose BugsInPy test script is pytest-based.

```bash
# Default FauxPy backend (SBFL, statement granularity, Ochiai)
python -m apr_framework localize --project black --bug 1

# The backend flag is explicit too: `fauxpy` for SBFL/MBFL/hybrid, `llm` for LLM-FL
python -m apr_framework localize --backend fauxpy --project black --bug 1
python -m apr_framework localize --backend llm    --project black --bug 1
```

Choose the source root when automatic inference is not enough:

```bash
python -m apr_framework localize --project black --bug 1 --src black   # or --src .
```

Select the metric for the primary ranking:

```bash
python -m apr_framework localize --project black --bug 1 --metric ochiai
```

Limit the number of ranked locations printed:

```bash
python -m apr_framework localize --project black --bug 1 --metric ochiai --top-n 10
```

Function-level localization:

```bash
python -m apr_framework localize --project black --bug 1 --granularity function --metric ochiai --top-n 10
```

Pass FauxPy an explicit list of failing tests:

```bash
python -m apr_framework localize --project black --bug 1 --failing_tests "tests/test_chinese.py::test_chinese"
```

`--test-target` can be repeated, once per pytest target. When `--metric` is omitted,
the family default applies: `ochiai` for SBFL, `metallaxis` for MBFL.

### Custom SBFL metrics: Jaccard and WSBI

Stock FauxPy 0.7.0 computes Tarantula, Ochiai and D* for SBFL. Repair Framework
adds two more:

- **Jaccard** — set-overlap scoring: `ef / (ef + ep + fn)`
- **WSBI (Weighted SBI)** — a new custom metric: `ef / (ef + alpha × ep)`, with configurable `alpha` (default `0.5`)

```bash
python -m apr_framework localize --project black --bug 1 --metric jaccard
python -m apr_framework localize --project black --bug 1 --metric wsbi
```

Use `--wsbi-alpha` to set how much passing tests count:

```bash
# Default alpha=0.5: passing tests count half as much as failing tests
python -m apr_framework localize --project black --bug 1 --metric wsbi

# alpha=1.0 reduces to plain SBI (equal weight)
python -m apr_framework localize --project black --bug 1 --metric wsbi --wsbi-alpha 1.0

# alpha=0.25 discounts passing-test coverage further
python -m apr_framework localize --project black --bug 1 --metric wsbi --wsbi-alpha 0.25
```

**Why WSBI.** Standard SBI is `ef / (ef + ep)`. WSBI weights the denominator:

```
score = ef / (ef + alpha * ep)    where alpha ∈ (0, 1], default 0.5
```

The intuition: a passing test that covers a statement is weaker evidence of
innocence than a failing test is evidence of guilt. At `alpha = 0.5`, passing tests
count half as much as failing tests. That makes the metric more sensitive to
failing-test coverage while still penalizing statements that many passing tests
also cover. `alpha = 1` recovers plain SBI; smaller values make the metric more
aggressive.

> **Known edge case.** When a bug has no passing tests (`ep = 0`), WSBI and SBI both
> give every executed line `1.0`, so they can't tell lines apart. Ochiai
> (`sqrt(ef/F)`) keeps a gradient. See the luigi#33 result in
> [Results](results.md#fault-localization).

**How it is wired in.** Before localization, the framework patches the installed
FauxPy copy in the bug checkout's virtual environment. The patch:

- adds the `MetricJaccard` and `MetricWSBI` formulas;
- registers them in FauxPy's SBFL metric list;
- extends FauxPy's local SQLite score table.

The existing output parser then selects the emitted `Scores for Jaccard` or
`Scores for WSBI` table via `--metric`. The `--wsbi-alpha` value is injected into
the patch at run time and baked into the written `metric_wsbi.py`. The patch helpers
are idempotent, so they are safe to re-apply.

### MBFL

```bash
python -m apr_framework localize --project black --bug 1 --mbfl --granularity statement --metric metallaxis --top-n 10
```

**Budgeted random mutant selection.** Validating mutants is the expensive part of
MBFL. `--mutation-strategy random --budget N` caps how many generated mutants are
validated, which makes MBFL practical on large projects:

```bash
python -m apr_framework localize --project black --bug 1 --mbfl --mutation-strategy random --budget 50 --metric metallaxis
```

- `--budget` limits how many generated mutants are validated.
- `--seed` makes the random selection reproducible. It defaults to `0`.

This works through a second FauxPy source patch. The patch injects the
`--mutation-selection`, `--mutation-budget` and `--mutation-seed` pytest options.
For MBFL runs, `LocalizationResult.metadata` also records cost-control fields such as
`mutants_generated`, `mutants_validated` and `mutation_generation_time_seconds`.

### Hybrid SBFL + MBFL

```bash
python -m apr_framework localize --project black --bug 1 --family hybrid \
  --sbfl-metric ochiai --mbfl-metric metallaxis \
  --sbfl-weight 0.5 --mbfl-weight 0.5 \
  --mutation-strategy random --budget 50 --top-n 10
```

`HybridFaultLocalizer` runs SBFL and MBFL through the FauxPy adapter. It then:

1. min-max-normalizes each backend's scores on its own;
2. combines them with the (normalized) weights and re-ranks;
3. breaks ties toward locations that **both** backends found, then by best per-backend rank;
4. applies `--top-n` to the combined ranking.

MBFL mutation controls apply only to the MBFL half of the run. Hybrid runs take
`--sbfl-metric` / `--mbfl-metric`, not `--metric`. The combined result
(`hybrid-fauxpy`) keeps each location's component scores and ranks in its metadata,
and records the effective weights and per-backend score formulas.

### Internals

- `FauxPyConfig` carries the localization family, granularity, metric, failing tests,
  excludes and MBFL selection options. `__post_init__` rejects unsupported
  family/granularity/mutation combinations before any subprocess runs.
- `FauxPyToolchain` installs pinned FauxPy 0.7.0 when needed, applies the metric and
  mutant-selection patches, and builds the pytest/FauxPy command from the config.
- `parse_fauxpy_output` parses every metric table when `metric_filter=None`, or one
  metric's ranked rows when `metric_filter` is set. It supports statement rows
  (`File | Line | Score`) and function rows (`File | Function | Line | Score`,
  including the optional end line).
- `LocalizationResult.metadata["all_metrics"]` stores the full metric map for reuse
  by later stages.
- `load_pytest_targets` converts BugsInPy `run_test.sh` scripts (pytest or
  `python -m unittest`) into pytest target strings. It raises `ConfigurationError` for
  `unittest discover`.

### Which bugs FauxPy can localize

Three failure modes stop FauxPy 0.7.0 on some BugsInPy bugs:

| Failure mode | Examples | What happens |
|---|---|---|
| **Python 3.7 pins** | `tornado:14`, `youtube-dl:12` | FauxPy (via `cosmic-ray` → `pyllmut` → `openai`) needs Python ≥ 3.7.1, so it cannot install at all |
| **Dependency conflict** | `fastapi:*` | Installing FauxPy pulls `pydantic` 2.x over the project's `pydantic==1.5.1`, and the test modules stop importing. Re-pinning breaks FauxPy's own `typing_extensions>=4.5` requirement. The two dependency sets are irreconcilable |
| **Silent empty ranking** | `scrapy:2` | FauxPy installs and exits 0 but ranks nothing |

In practice, `black` bugs are the reliable auto-FL set. Before adding a bug to an
auto-FL experiment, run `localize --project P --bug N` and confirm that ranked
locations come back. Cached FauxPy reports under `.workspace/` are not proof, because
a later `compile` can rot the venv. For bugs FauxPy cannot reach, use
[LLM-FL](#llm-based-fault-localization) or the [perfect-FL oracle](#perfect-fault-localization-oracle).

---

## LLM-based fault localization

`LLMFaultLocalizer` is a drop-in `FaultLocalizer` that asks an LLM to rank suspicious
lines. It returns the same ranked `file`/`line` locations (`backend="llm-fl"`) as the
other backends, so repair and evaluation use its results without pipeline changes.

**How it gathers evidence.**

1. It runs the failing trigger test **once**, reusing the failing-test context
   builder. That captures the test body and its assertion/traceback output.
2. It picks project source to show the model by combining two signals:
   - **traceback frames**, which work well for exceptions;
   - the **symbols the failing test method patches or mocks** via `unittest.mock.patch`.
     Only that method's AST node is walked; scoping to the whole test file floods
     the anchor set with irrelevant symbols.
3. It renders each anchored file as line-numbered, merged windows, capped by
   `--max-source-lines`, with non-test source first. The token budget goes to the
   fault region rather than the test file.
4. It asks the model for a **strict JSON ranking**.

**How it parses the answer.** `parse_llm_fl_response` extracts a JSON array from the
raw reply, a fenced block, or a bracket-delimited substring. It then:

- normalizes paths;
- drops malformed entries instead of failing;
- keeps the model's rationale in metadata;
- assigns deterministic rank-based scores (`1.0, 0.99, …`).

An unparsable reply yields an empty ranking, not an exception.

```bash
# LLM fault localization through the OpenAI API
python -m apr_framework localize \
  --backend llm \
  --project black \
  --bug 1 \
  --model gpt-5.4 \
  --llm-base-url https://api.openai.com/v1 \
  --llm-api-key-env OPENAI_API_KEY \
  --temperature 1 \
  --top-n 10

# Print the generated FL prompt to stderr while running
APR_LLM_DEBUG_PROMPT=stderr python -m apr_framework localize \
  --backend llm \
  --project black \
  --bug 1 \
  --model gpt-5.4 \
  --llm-base-url https://api.openai.com/v1 \
  --llm-api-key-env OPENAI_API_KEY \
  --temperature 1 \
  --top-n 10

# Custom system prompt and smaller source-context limits
python -m apr_framework localize \
  --backend llm \
  --project black \
  --bug 1 \
  --model gpt-5.4 \
  --llm-base-url https://api.openai.com/v1 \
  --llm-api-key-env OPENAI_API_KEY \
  --temperature 1 \
  --top-n 5 \
  --fl-system-prompt fl_prompt1 \
  --max-source-lines 400 \
  --source-window 40

# Legacy GPT@RUB gateway (the default endpoint when --llm-base-url is omitted)
python -m apr_framework localize \
  --backend llm \
  --project black \
  --bug 1 \
  --model gpt-4.1-2025-04-14 \
  --llm-api-key-env GPT_AT_RUB_API_KEY \
  --temperature 0 \
  --top-n 10
```

| Flag | Default | Meaning |
|---|---|---|
| `--model` | `gpt-4.1-2025-04-14` | model name sent to the endpoint |
| `--temperature` | `0.0` | sampling temperature |
| `--llm-base-url` | legacy GPT@RUB gateway | OpenAI-compatible endpoint, e.g. `https://api.openai.com/v1` |
| `--llm-api-key-env` | `GPT_AT_RUB_API_KEY` | environment variable holding the key |
| `--fl-system-prompt` | `fl_prompt1` | prompt file stem under `localization/prompts/` |
| `--max-source-lines` | `400` | total source lines shown to the model |
| `--source-window` | `40` | lines of context around each anchor |
| `--top-n` | all | locations kept |

**Debugging.** `results.json → metadata.files_shown` lists the files the model saw,
and `metadata.raw_llm_response` holds its raw answer. If the ranking contains only
test-file lines, symbol anchoring found nothing. See
[Troubleshooting](getting-started.md#troubleshooting).

---

## Perfect fault localization (oracle)

Repair quality depends heavily on the fault location it is given. To separate the
repair engine's strength from the localizer's, `repair` can run under **two FL
conditions**, selected with `--fl-mode`:

| Mode | Flag | Fault location source |
|---|---|---|
| **Automated FL** | `--fl-mode auto` (default) | a real localizer: FauxPy (`--fl-family {sbfl,mbfl,hybrid}`) or the LLM (`--fl-backend llm`) |
| **Perfect FL** | `--fl-mode perfect` | the BugsInPy developer fix (`bug_patch.txt`): the *oracle* location, and no localizer runs |

```bash
# Automated FL (e.g. SBFL/Ochiai) drives the repair targets:
python -m apr_framework repair --project black --bug 1 --fl-mode auto --fl-family sbfl

# Perfect FL (oracle): the repair targets are the exact lines the developer changed.
python -m apr_framework repair --project tornado --bug 14 --fl-mode perfect
```

**How it works.** `PerfectFaultLocalizer` (`localization/perfect.py`) implements the
same `FaultLocalizer` interface as the other localizers, so it is a drop-in
replacement. It doesn't analyse the program. Instead it:

1. reads the developer fix via `BugsInPyAdapter.get_reference_patch`;
2. parses the **buggy-side** line numbers of the unified diff (`derive_oracle_locations`).
   Each `@@ -old_start,… @@` hunk header anchors a counter that walks the hunk body;
3. turns every `-` (changed/removed) line into a ranked oracle location, and anchors
   pure insertions to their insertion point.

The resulting `LocalizationResult` (`backend="perfect-fl"`) flows into the unchanged
repair pipeline. For black#1, perfect FL yields exactly the three developer-fix
lines:

```text
rank 1: black.py:621   rank 2: black.py:636   rank 3: black.py:646
```

The selected mode is recorded in the result files. `config.json` and each bug's
`config` block in `repair_results.json` carry `fl_mode` (`auto`/`perfect`) and
`fl_backend` (the FL family, `llm-fl`, or `oracle`), so comparisons can group runs by
mode. `--fl-mode perfect` ignores `--fl-family` and `--skip-localize`, and raises a
clear error if the bug has no `bug_patch.txt`.

> Perfect FL is an *upper bound on localization*, not on repair reach. If a bug's fix
> is out of the template operators' reach (e.g. black#1's `try/except` wrapper), it
> still yields zero exact-diff matches even with perfect locations.

### Choosing the FL source for `repair`

`--fl-mode` asks *where do the locations come from: a tool, or the oracle?*
`--fl-backend` asks *which tool?* The two compose:

| `--fl-mode` | `--fl-backend` | FL source (`fl_backend` in the result files) |
|---|---|---|
| `auto` (default) | `fauxpy` (default) | SBFL / MBFL / hybrid, chosen by `--fl-family` |
| `auto` | `llm` | the LLM localizer (`llm-fl`) |
| `perfect` | *(ignored)* | the BugsInPy developer fix (`oracle`) |

The full precedence is **perfect → cached (`--skip-localize`) → llm → fauxpy**.
`--fl-mode perfect` wins over `--fl-backend llm`, because the oracle is strictly
better information than any localizer. When that happens the run logs it instead of
failing.
