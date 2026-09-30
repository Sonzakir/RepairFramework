# Evaluation harnesses

Repair Framework ships reproducible experiment matrices. Each harness runs the real
pipeline across a grid of bugs × configurations and writes:

- one `runs/run_NNN/` directory per cell (config, log, patch diffs, retrieval traces);
- an aggregated `results.json` and a generated `README.md` report.

A failure in one cell (e.g. FauxPy can't install on a bug's Python) is recorded as an
**error cell** and doesn't abort the matrix. The LLM harnesses flush `results.json`
after every cell, so a long API run survives an interruption.

| Harness | Grid | Default output |
|---|---|---|
| [`evaluate-localization`](#localization-comparison) | bugs × 8 FL techniques | `experiment_results/` |
| [`evaluate-repair`](#template-repair-matrix) | bugs × FL modes (template repair) | `experiment_results/repair/` |
| [`evaluate-llm-repair`](#llm-repair-matrix) | bugs × LLM variants × FL modes | `experiment_results/llm_repair/task5/` |
| [`evaluate-course-comparison`](#four-approach-comparison) | bugs × 4 approaches × FL modes | `experiment_results/course_comparison/` |
| `evaluate-dummy` | 3 black bugs × dummy repair | `runs/` |

Published numbers from these harnesses are in [Results](results.md).

> **Every bug must be checked out and compiled first.** The harnesses don't check
> anything out again:
>
> ```bash
> for b in "tornado 14" "scrapy 2" "black 1"; do
>   python -m apr_framework bugsinpy checkout $b
>   python -m apr_framework bugsinpy compile  $b
> done
> ```

---

## Localization comparison

`bugsinpy evaluate-localization` runs all **8 techniques** on each bug:

- SBFL: Ochiai, Tarantula, D* (baselines) and Jaccard, WSBI (extensions);
- MBFL: Metallaxis (exhaustive baseline) and Metallaxis-Random (budget-capped extension);
- Hybrid SBFL+MBFL.

It compares each ranking against the ground-truth faulty lines parsed from
`bug_patch.txt`.

```bash
python -m apr_framework bugsinpy evaluate-localization \
  --bugs black:1,black:3,black:7 \
  --granularity statement \
  --budget 50 --seed 0 \
  --top-ks 1,5,10 \
  --output-dir experiment_results
```

The report has per-bug tables (rank of the faulty line, Top-1/5/10 hits, total
ranked), an aggregate Top-k accuracy table, and a generated discussion.

Ground-truth matching (`_files_match`) is path-flexible. It handles FauxPy's short
relative paths against git-diff absolute paths by comparing suffixes and, as a last
resort, filenames.

**Rebuilding without Docker.** `scripts/generate_experiment_results.py` rebuilds the
report from FauxPy CSVs already cached under `.workspace/` by earlier Docker runs. It
uses the same `LocalizationComparisonRunner` code path but runs no containers. Use it
to refresh the report after changing scoring or reporting logic. It can't produce
results for bugs whose CSVs were never generated.

```bash
python scripts/generate_experiment_results.py   # from the repo root
```

---

## Template repair matrix

`bugsinpy evaluate-repair` runs the **whole template repair pipeline** (fault
localization → patch generation → validation → ranking) across *bugs × FL modes*, and
puts automated and perfect FL side by side.

Each *(bug, FL mode)* cell is a complete repair run executed by
`RepairEvaluationRunner`. `RepairComparisonRunner` only orchestrates and aggregates.

```bash
python -m apr_framework bugsinpy evaluate-repair \
    --bugs "tornado:14,scrapy:2,black:1" \
    --fl-modes "auto,perfect" \
    --fl-family sbfl --localization-metric ochiai \
    --ranker weighted \
    --output-dir experiment_results/repair
```

Every per-cell flag of `repair` is accepted too (`--budget`, `--top-n`, `--operators`,
`--timeout`, `--granularity`, `--ranker-weights`, and the MBFL/hybrid knobs). Here
`--ranker` defaults to `weighted`.

**Per-cell metrics** (`results.json`):

| Field | Meaning |
|---|---|
| `total_candidates_generated` | patches the template generator produced |
| `candidates_validated` | patches actually run against the test suite (≤ budget) |
| `plausible_count` | patched programs that passed the trigger test + regression check |
| `correct_count` | plausible patches that also match the developer fix at the diff level |
| `time_to_first_plausible_seconds` | wall-clock time to the first plausible patch, or `null` |
| `total_wall_clock_seconds` | wall-clock time for the whole cell |
| `generation_rank_of_first_correct` | 1-based position of the first correct patch in **generation order** (unranked baseline) |
| `ranked_rank_of_first_correct` | 1-based position of the first correct patch after the **ranker** reorders |

**Artifacts**

```
experiment_results/repair/
  results.json                 # machine-readable matrix (one row per cell)
  README.md                    # generated per-bug tables + aggregate + discussion
  run_artifacts/run_NNN/       # per-cell logs, config, repair_results.json, patch diffs
```

---

## LLM repair matrix

`bugsinpy evaluate-llm-repair` runs the LLM repair pipeline across
**bugs × variants × FL modes**. The variants are *isolated axes*: each differs from
the bare baseline by exactly one factor, so the effects of enrichment and of the
iterative loop can be read off cleanly.

| Variant | Context enrichment | Iterative loop | What it isolates |
|---|---|---|---|
| `single-shot` | off | off | the bare prompt (baseline) |
| `context-enriched` | on (failing-test source + error traceback) | off | the effect of context enrichment |
| `iterative` | off | on (`--max-iterations`) | the effect of the feedback loop |

None of the three variants enable few-shot or retrieval. Few-shot is only reachable
through `repair --few-shot N`, and keeping the matrix retrieval-free keeps it a valid
baseline.

```bash
export OPENAI_API_KEY="<key>"     # or put it in .env
python -m apr_framework bugsinpy evaluate-llm-repair \
  --bugs black:1,tornado:14,scrapy:2,fastapi:3 \
  --variants single-shot,context-enriched,iterative \
  --fl-modes auto,perfect \
  --model gpt-5.4 --llm-base-url https://api.openai.com/v1 \
  --llm-api-key-env OPENAI_API_KEY --temperature 1 \
  --top-n 3 --max-candidates 3 --max-iterations 5
```

This harness defaults to OpenAI (`--llm-base-url https://api.openai.com/v1`,
`--llm-api-key-env OPENAI_API_KEY`, `--model gpt-5.4`, `--temperature 1.0`),
`--top-n 3`, `--max-candidates 3` and `--ranker none`.

A fresh `LLMRepairAlgorithm` and client are built for each cell, so each cell's
`llm_query_count` stays isolated.

**Metrics**, per cell and aggregated per (variant × FL mode):

- **LLM queries made** (`llm_query_count`). Queries can exceed candidates, because
  format retries and unparsable replies cost a query but produce no candidate.
- candidates generated;
- **plausible** and **exact-diff** patch counts;
- **time to first plausible** and **total repair time**;
- the rank of the first correct patch (generation order → ranked order).

The generated report adds three analyses:

- the effect of iterative repair: did the loop recover patches single-shot missed?
- the effect of context enrichment: did the extra context help?
- a comparison with template repair: the best LLM outcome per bug against
  `experiment_results/repair/results.json`.

**Interface note.** The `RepairAlgorithm` interface has a non-abstract
`llm_query_count() -> int | None`. It defaults to `None`, and the LLM engine overrides
it to report its client's call count. Template runs return `None`, so the field is
absent from their result files.

---

## Four-approach comparison

`bugsinpy evaluate-course-comparison` runs every repair approach on the same bugs and
writes one aggregated report. The approaches are data
([`evaluation/course_approaches.py`](../src/apr_framework/evaluation/course_approaches.py)),
not code branches:

| Approach ID | Technique | FL source | Toggles |
|---|---|---|---|
| `a3-template` | template | auto / perfect | — |
| `a4-single-shot` | LLM | auto / perfect | no enrichment, no retrieval |
| `a4-iterative` | LLM | auto / perfect | iterative feedback loop |
| `a5-full-llm` | LLM (full pipeline) | **LLM-FL** | enrichment + context retrieval + assessment |

```bash
python -m apr_framework bugsinpy evaluate-course-comparison \
  --bugs black:1,black:3 \
  --approaches a3-template,a4-single-shot,a4-iterative,a5-full-llm \
  --fl-modes auto,perfect --retrieval-budget 3 \
  --model gpt-5.4 --temperature 1.0 \
  --output-dir experiment_results/course_comparison --runs-dir runs
```

- The first three approaches run under both FL modes. `a5-full-llm` localizes with the
  LLM, so it has a single FL source and ignores the `--fl-modes` axis.
- **Pick FL modes per bug.** Bugs that FauxPy can localize (e.g. `black`) take
  `--fl-modes auto,perfect`. Bugs it can't (e.g. `tornado:14`, `scrapy:2`) take
  `--fl-modes perfect`, so no phantom auto cell gets scored as a zero. See
  [Which bugs FauxPy can localize](fault-localization.md#which-bugs-fauxpy-can-localize).
- A cell whose localizer returns no ranked location is short-circuited to
  `no_fl_locations`, not scored as `no_patch`, and it is kept out of every
  cross-approach comparison. Its zeros would describe the localizer, not the repair
  approach.

**Every cell is measured the same way.** Every cell runs with the assessor attached
and similarity scoring on. That is why the command **re-runs** all four approaches
instead of loading earlier results. Both metrics need the patch objects: the
similarity score rebuilds a reformatting-neutral diff from `patched_source`, which is
stripped from serialized results to keep them small. They can't be reconstructed from
old artifacts. The model is sampled at temperature 1.0, so numbers won't exactly
reproduce older reports. Each report stands on its own.

**Why exact diff is not the headline.** `Exact diff` counts byte-for-byte matches
with the developer fix, nothing more. A semantically correct fix written differently
scores 0, so a 0 is *not* a claim that the patch is wrong; the column is the
framework's data-contamination signal. Two graded metrics carry the quality judgment:

| Metric | What it adds |
|---|---|
| **Assessment quality score** (`0.0`–`1.0`) | The LLM assessor's judgment of whether the patch genuinely fixes the bug or just overfits the test suite: the semantic signal a pass/fail oracle can't give. |
| **Context similarity score** (`0.0`–`1.0`) | How close the patch's edit is to the developer's, including surrounding context. It is scored on **every candidate**, plausible or not, so an approach whose patches all failed still shows how close it came. A high-but-below-1.0 score is a near-miss that `Exact diff` reports as a flat zero. |

The generated report contains, per bug:

- a comparison table plus the two extra metric rows;
- an auditable per-cell table underneath.

It ends with four discussion sections: LLM-FL vs. SBFL/MBFL, the effect of context
retrieval, the usefulness of assessment, and where the full pipeline improved or
regressed.

**Helper scripts (no Docker, no API key):**

```bash
# Re-render the report from the committed results.json after changing tables/discussion
python scripts/regenerate_course_comparison_readme.py [output_dir]

# Backfill similarity scores for every candidate in older committed artifacts
python scripts/backfill_similarity_scores.py [run_artifacts_dir]
```

---

## Run artifacts

Every single run (`localize`, `repair`, or a harness cell) writes a numbered
directory:

```
runs/run_NNN/
  config.json            # every configuration parameter, incl. fl_mode / fl_backend
  results.json           # localization runs: ranked locations + metadata
  repair_results.json    # repair runs: per-candidate outcomes, metrics, plausible/ranked/assessed lists
  execution.log          # timestamped step log (explains every early stop)
  patches/               # <patch_id>.diff and <patch_id>.patched.py for each plausible patch
```

`localize` and `evaluate-dummy` runs also get a `report.md` summary and a zipped copy
of the artifacts (`ArchiveReportGenerator`). `evaluate-dummy` runs the dummy repair
component on three `black` bugs (1/3/23).
