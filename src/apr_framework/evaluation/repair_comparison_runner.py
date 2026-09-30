"""Drive the full repair pipeline across a matrix of bugs x fault-localization
modes, apply the patch ranker, and aggregate the Task-5 metrics.

This is the Assignment-3 Task-5 counterpart of ``LocalizationComparisonRunner``
(Assignment-2 evaluation). Where that runner compared *localizers*, this one
compares *FL modes* for the **repair** technique: for every bug it runs the
generate-validate-correctness-rank pipeline once under automated FL and once
under perfect (oracle) FL, then writes a single ``results.json`` plus a
human-readable ``README.md`` with per-bug tables, an aggregate table, and a
discussion.

The heavy lifting (generate-and-validate loop, correctness check, ranking,
per-run ``repair_results.json`` + ``execution.log`` artifacts) is delegated to
``RepairEvaluationRunner`` — each (bug, FL mode) cell gets its own ``run_NNN``
directory so the raw logs remain available as evaluation artifacts. This runner
only orchestrates the matrix and aggregates the numbers.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from apr_framework.benchmarks.base import BenchmarkAdapter
from apr_framework.core.models import BugIdentifier, LocalizationResult
from apr_framework.repair.base import RepairAlgorithm
from apr_framework.repair.ranking.base import PatchRanker

# A provider builds the localization input for one (bug, FL mode) cell. It is
# supplied by the CLI handler, which owns the concrete FauxPy / perfect-FL
# construction; raising is allowed and is recorded as an error cell.
LocalizationProvider = Callable[[BugIdentifier, str], LocalizationResult]

# A repair-algorithm factory builds a fresh repair algorithm bound to one cell's
# localization result.
RepairAlgorithmFactory = Callable[[LocalizationResult], RepairAlgorithm]


@dataclass
class RepairCellResult:
    """Metrics for one (bug, FL mode) cell of the comparison matrix."""

    bug: BugIdentifier
    fl_mode: str
    fl_backend: str
    status: str
    total_candidates_generated: int = 0
    candidates_validated: int = 0
    plausible_count: int = 0
    correct_count: int = 0
    time_to_first_plausible_seconds: float | None = None
    total_wall_clock_seconds: float = 0.0
    # Position (1-based) of the first correct patch in generation order — the
    # unranked baseline Task 4 asks us to compare against.
    generation_rank_of_first_correct: int | None = None
    # Position (1-based) of the first correct patch after the ranker reorders the
    # plausible set.
    ranked_rank_of_first_correct: int | None = None
    run_dir: str | None = None
    error: str | None = None


class RepairComparisonRunner:
    """Run the repair pipeline for every (bug, FL mode) pair and aggregate results."""

    def __init__(
        self,
        project_root: Path,
        runs_dir: Path,
        ranker: PatchRanker | None,
        repair_config_data: dict[str, Any],
        budget: int,
        stop_on_first: bool,
    ) -> None:
        self._project_root = Path(project_root)
        self._runs_dir = Path(runs_dir)
        self._ranker = ranker
        self._repair_config_data = repair_config_data
        self._budget = budget
        self._stop_on_first = stop_on_first

    def run(
        self,
        bugs: list[BugIdentifier],
        fl_modes: list[str],
        benchmark: BenchmarkAdapter,
        localization_provider: LocalizationProvider,
        repair_algorithm_factory: RepairAlgorithmFactory,
    ) -> list[RepairCellResult]:
        """Execute the full matrix.

        Args:
            bugs:                    Bugs to repair.
            fl_modes:                FL modes to run per bug (e.g. ``["auto", "perfect"]``).
            benchmark:               Adapter used for checkouts, tests and the
                                     reference patch (correctness ground truth).
            localization_provider:   ``(bug, fl_mode) -> LocalizationResult`` —
                                     builds the suspicious-location input. May raise;
                                     the failure is captured as an error cell.
            repair_algorithm_factory: ``(localization_result) -> RepairAlgorithm`` —
                                     builds a repair algorithm bound to that input.
        """
        # Imported here to avoid a heavy import at module load time.
        from apr_framework.evaluation.repair_runner import RepairEvaluationRunner
        from apr_framework.evaluation.run_writer import RunWriter

        cells: list[RepairCellResult] = []
        for bug in bugs:
            for fl_mode in fl_modes:
                print(f"  [{bug.project} #{bug.bug_id}] FL={fl_mode} ...", flush=True)
                try:
                    localization_result = localization_provider(bug, fl_mode)
                except Exception as exc:  # noqa: BLE001 — one bad cell must not abort the matrix
                    print(f"    LOCALIZATION ERROR: {exc}", flush=True)
                    cells.append(
                        RepairCellResult(
                            bug=bug,
                            fl_mode=fl_mode,
                            fl_backend=self._backend_label(fl_mode),
                            status="error",
                            error=f"{type(exc).__name__}: {exc}",
                        )
                    )
                    continue

                writer = RunWriter.create(self._runs_dir)
                cell_config_data = {
                    **self._repair_config_data,
                    "project": bug.project,
                    "bug_id": bug.bug_id,
                    "fl_mode": fl_mode,
                    "fl_backend": localization_result.backend,
                    "ranker": self._ranker.name if self._ranker else "none",
                }
                writer.write_json(
                    "config.json", {"runner": "repair-comparison", **cell_config_data}
                )
                writer.log(
                    f"Repair comparison cell: {bug.project}#{bug.bug_id} fl_mode={fl_mode} "
                    f"backend={localization_result.backend}"
                )

                try:
                    repair_algorithm = repair_algorithm_factory(localization_result)
                    evaluation_runner = RepairEvaluationRunner(
                        project_root=self._project_root,
                        runs_dir=self._runs_dir,
                        budget=self._budget,
                        stop_on_first=self._stop_on_first,
                        config_data=cell_config_data,
                        writer=writer,
                        ranker=self._ranker,
                        localization_result=localization_result,
                    )
                    evaluation_runner.run([bug], benchmark, repair_algorithm)
                except Exception as exc:  # noqa: BLE001
                    print(f"    REPAIR ERROR: {exc}", flush=True)
                    cells.append(
                        RepairCellResult(
                            bug=bug,
                            fl_mode=fl_mode,
                            fl_backend=localization_result.backend,
                            status="error",
                            run_dir=str(writer.run_dir),
                            error=f"{type(exc).__name__}: {exc}",
                        )
                    )
                    continue

                cells.append(
                    self._cell_from_run_dir(
                        bug, fl_mode, localization_result.backend, writer.run_dir
                    )
                )

        return cells

    # ------------------------------------------------------------------
    # Reading one cell's metrics back from the run it produced
    # ------------------------------------------------------------------

    def _cell_from_run_dir(
        self,
        bug: BugIdentifier,
        fl_mode: str,
        fl_backend: str,
        run_dir: Path,
    ) -> RepairCellResult:
        payload = json.loads(
            (run_dir / "repair_results.json").read_text(encoding="utf-8")
        )
        metrics = payload.get("metrics", {})
        plausible_patches = payload.get("plausible_patches", []) or []

        # Generation-order baseline: first correct patch position in the
        # unranked plausible list (1-based), or None.
        generation_rank_of_first_correct: int | None = None
        for position, patch_payload in enumerate(plausible_patches, start=1):
            if patch_payload.get("is_correct"):
                generation_rank_of_first_correct = position
                break

        return RepairCellResult(
            bug=bug,
            fl_mode=fl_mode,
            fl_backend=fl_backend,
            status=payload.get("status", "unknown"),
            total_candidates_generated=metrics.get("total_candidates_generated", 0),
            candidates_validated=metrics.get("candidates_validated", 0),
            plausible_count=metrics.get("plausible_count", 0),
            correct_count=metrics.get("correct_count", 0),
            time_to_first_plausible_seconds=metrics.get(
                "time_to_first_plausible_seconds"
            ),
            total_wall_clock_seconds=metrics.get("total_wall_clock_seconds", 0.0),
            generation_rank_of_first_correct=generation_rank_of_first_correct,
            ranked_rank_of_first_correct=metrics.get("rank_of_first_correct"),
            run_dir=str(run_dir),
        )

    @staticmethod
    def _backend_label(fl_mode: str) -> str:
        return "perfect-fl" if fl_mode == "perfect" else "auto-fl"

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def write_results(self, cells: list[RepairCellResult], output_dir: Path) -> Path:
        """Write ``results.json`` and ``README.md`` to *output_dir*; return README path."""
        output_dir.mkdir(parents=True, exist_ok=True)
        self._write_json(cells, output_dir / "results.json")
        readme_path = output_dir / "README.md"
        readme_path.write_text(self._build_readme(cells), encoding="utf-8")
        return readme_path

    def _write_json(self, cells: list[RepairCellResult], path: Path) -> None:
        rows: list[dict[str, Any]] = []
        for cell in cells:
            rows.append(
                {
                    "bug": {
                        "benchmark": cell.bug.benchmark,
                        "project": cell.bug.project,
                        "bug_id": cell.bug.bug_id,
                    },
                    "fl_mode": cell.fl_mode,
                    "fl_backend": cell.fl_backend,
                    "status": cell.status,
                    "total_candidates_generated": cell.total_candidates_generated,
                    "candidates_validated": cell.candidates_validated,
                    "plausible_count": cell.plausible_count,
                    "correct_count": cell.correct_count,
                    "time_to_first_plausible_seconds": cell.time_to_first_plausible_seconds,
                    "total_wall_clock_seconds": cell.total_wall_clock_seconds,
                    "generation_rank_of_first_correct": cell.generation_rank_of_first_correct,
                    "ranked_rank_of_first_correct": cell.ranked_rank_of_first_correct,
                    "run_dir": cell.run_dir,
                    "error": cell.error,
                }
            )
        path.write_text(
            json.dumps(
                {
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "ranker": self._ranker.name if self._ranker else "none",
                    "budget": self._budget,
                    "results": rows,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    def _build_readme(self, cells: list[RepairCellResult]) -> str:
        bugs = _unique_ordered([cell.bug for cell in cells])
        index: dict[tuple[BugIdentifier, str], RepairCellResult] = {
            (cell.bug, cell.fl_mode): cell for cell in cells
        }
        fl_modes = _unique_ordered([cell.fl_mode for cell in cells])

        lines: list[str] = []
        lines.append("# Repair Evaluation Results (Assignment 3 — Task 5)\n")
        lines.append(
            "Full template-based repair pipeline (fault localization → patch "
            "generation → validation → ranking) run on "
            f"{len(bugs)} BugsInPy bug(s), each under both automated FL and perfect "
            "(oracle) FL, with the patch ranker applied.\n"
        )
        ranker_name = self._ranker.name if self._ranker else "none"
        lines.append(
            f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} · "
            f"ranker: `{ranker_name}` · per-validation budget: {self._budget}\n"
        )

        # --- Per-bug tables ---
        for bug in bugs:
            lines.append(f"\n## {bug.project} bug #{bug.bug_id}\n")
            header = (
                "| FL mode | Backend | Generated | Validated | Plausible | Correct "
                "| 1st plausible (s) | Total (s) | Correct rank (gen) | Correct rank (ranked) | Notes |"
            )
            lines.append(header)
            lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
            for fl_mode in fl_modes:
                cell = index.get((bug, fl_mode))
                if cell is None:
                    continue
                lines.append(_cell_row(cell))

        # --- Aggregate table ---
        lines.append("\n## Aggregate (per FL mode)\n")
        lines.append(
            "Totals across all evaluated bugs. *Number of Distinct Bugs with Correct Patch* "
            "counts bugs with at least one correct patch; *bugs with plausible* counts bugs "
            "with at least one plausible patch.\n"
        )
        lines.append(
            "| FL mode | Bugs | Generated | Plausible | Correct | Bugs with plausible | Number of Distinct Bugs with Correct Patch |"
        )
        lines.append("|---|---|---|---|---|---|---|")
        for fl_mode in fl_modes:
            mode_cells = [index.get((bug, fl_mode)) for bug in bugs]
            mode_cells = [cell for cell in mode_cells if cell is not None]
            generated_total = sum(
                cell.total_candidates_generated for cell in mode_cells
            )
            plausible_total = sum(cell.plausible_count for cell in mode_cells)
            correct_total = sum(cell.correct_count for cell in mode_cells)
            bugs_with_plausible = sum(
                1 for cell in mode_cells if cell.plausible_count > 0
            )
            bugs_repaired = sum(1 for cell in mode_cells if cell.correct_count > 0)
            lines.append(
                f"| {fl_mode} | {len(mode_cells)} | {generated_total} | {plausible_total} "
                f"| {correct_total} | {bugs_with_plausible} | {bugs_repaired} |"
            )

        # --- Discussion ---
        lines.append("\n## Discussion\n")
        lines.append(self._build_discussion(cells, bugs, fl_modes, index))

        return "\n".join(lines) + "\n"

    def _build_discussion(
        self,
        cells: list[RepairCellResult],
        bugs: list[BugIdentifier],
        fl_modes: list[str],
        index: dict[tuple[BugIdentifier, str], RepairCellResult],
    ) -> str:
        paras: list[str] = []

        repaired_perfect = [
            bug
            for bug in bugs
            if (cell := index.get((bug, "perfect"))) and cell.correct_count > 0
        ]
        repaired_auto = [
            bug
            for bug in bugs
            if (cell := index.get((bug, "auto"))) and cell.correct_count > 0
        ]

        def _label(bug: BugIdentifier) -> str:
            return f"{bug.project}#{bug.bug_id}"

        paras.append(
            "**Which bugs were repaired?**  "
            + (
                "Under perfect FL, a correct patch was produced for: "
                + ", ".join(f"`{_label(bug)}`" for bug in repaired_perfect)
                + "."
                if repaired_perfect
                else "No bug produced a correct patch under perfect FL."
            )
            + (
                "  Under automated FL, a correct patch was produced for: "
                + ", ".join(f"`{_label(bug)}`" for bug in repaired_auto)
                + "."
                if repaired_auto
                else "  No bug produced a correct patch under automated FL."
            )
        )

        paras.append(
            "**Did the repair technique benefit from better FL?**  "
            "Perfect FL feeds the repair loop the exact developer-fix line(s), so the "
            "mutation operators are applied precisely where the fault is. Automated "
            "(SBFL) FL instead supplies the top-N suspicious lines, which only contain "
            "the faulty line when localization is accurate. Where the developer fix is "
            "an operator-level change (a comparison/boolean/arithmetic swap, an "
            "off-by-one, or a return-value change), perfect FL therefore turns the fix "
            "into a single reachable mutation, while automated FL succeeds only if the "
            "faulty line is ranked highly enough to fall inside the top-N window. Where "
            "the developer fix is *out of operator reach* (e.g. wrapping code in "
            "try/except, adding a parameter, or renaming a variable), neither FL mode "
            "can yield a correct patch — the bottleneck is the template operator set, "
            "not the fault location."
        )

        # Ranking effect
        moved_forward = [
            cell
            for cell in cells
            if cell.generation_rank_of_first_correct is not None
            and cell.ranked_rank_of_first_correct is not None
            and cell.ranked_rank_of_first_correct
            < cell.generation_rank_of_first_correct
        ]
        correct_cells = [cell for cell in cells if cell.correct_count > 0]
        multi_plausible_correct = [
            cell for cell in correct_cells if cell.plausible_count > 1
        ]
        if moved_forward:
            paras.append(
                "**Did the ranker surface correct patches earlier?**  "
                "Yes — for "
                + ", ".join(
                    f"`{_label(cell.bug)}` ({cell.fl_mode} FL: generation rank "
                    f"{cell.generation_rank_of_first_correct} → ranked rank "
                    f"{cell.ranked_rank_of_first_correct})"
                    for cell in moved_forward
                )
                + " the weighted ranker moved the correct patch ahead of its "
                "generation-order position."
            )
        elif correct_cells and not multi_plausible_correct:
            paras.append(
                "**Did the ranker surface correct patches earlier?**  "
                "On the cells that produced a correct patch the plausible set contained "
                "only a single patch, so generation order and ranked order coincide "
                "(the correct patch is at rank 1 in both). The ranker correctly places "
                "that patch first, but a larger plausible set is needed to *demonstrate* "
                "reordering. This matches the known limitation that ranking only "
                "distinguishes patches when at least two are plausible."
            )
        else:
            paras.append(
                "**Did the ranker surface correct patches earlier?**  "
                "No correct patch was produced on any evaluated cell, so the ranker had "
                "nothing to reorder. It still annotates every plausible patch with a "
                "composite score in the per-run JSON for inspection."
            )

        paras.append(
            "**Limitations.**  "
            "(1) The template operator set only covers single-token operator swaps, "
            "off-by-one nudges, condition negation and return-value changes; the "
            "majority of real BugsInPy developer fixes add or restructure statements "
            "and are therefore unreachable regardless of FL quality. "
            "(2) Automated FL depends on FauxPy, which requires a pytest-compatible "
            "test harness and a compatible Python; some bugs cannot run automated FL "
            "for environment reasons, which appears as an error cell. "
            "(3) Plausibility is judged on the bug's trigger test plus a regression "
            "check, not the project's full suite, so a patch counted as plausible may "
            "still be an overfit. (4) Correctness is a strict syntactic diff-level "
            "match to the single-file developer fix and will not credit a "
            "semantically-equivalent but textually-different patch."
        )

        return "\n\n".join(paras)


def _summarize_error(error_text: str, limit: int = 160) -> str:
    """Collapse a multi-line error to a single, table-safe, truncated note.

    The full error is preserved verbatim in ``results.json``; this is only the
    human-readable cell note, so newlines and pipes (which would break the
    markdown table) are stripped and the text is capped.
    """
    collapsed = " ".join(
        part.strip() for part in error_text.splitlines() if part.strip()
    )
    collapsed = collapsed.replace("|", "/")
    # Prefer the first sentence (often the wrapped exception message) when it is
    # self-contained and short enough; otherwise fall back to a hard truncation.
    first_sentence = collapsed.split(". ", 1)[0].strip()
    if 0 < len(first_sentence) <= limit and first_sentence != collapsed:
        return first_sentence + " (full error in results.json)"
    if len(collapsed) > limit:
        collapsed = collapsed[:limit].rstrip() + " …"
    return collapsed


def _cell_row(cell: RepairCellResult) -> str:
    if cell.error:
        return (
            f"| {cell.fl_mode} | {cell.fl_backend} | — | — | — | — | — | — | — | — | "
            f"ERROR: {_summarize_error(cell.error)} |"
        )
    ttfp = (
        f"{cell.time_to_first_plausible_seconds:.1f}"
        if cell.time_to_first_plausible_seconds is not None
        else "—"
    )
    generation_rank = (
        str(cell.generation_rank_of_first_correct)
        if cell.generation_rank_of_first_correct is not None
        else "—"
    )
    ranked_rank = (
        str(cell.ranked_rank_of_first_correct)
        if cell.ranked_rank_of_first_correct is not None
        else "—"
    )
    return (
        f"| {cell.fl_mode} | {cell.fl_backend} | {cell.total_candidates_generated} "
        f"| {cell.candidates_validated} | {cell.plausible_count} | {cell.correct_count} "
        f"| {ttfp} | {cell.total_wall_clock_seconds:.1f} | {generation_rank} "
        f"| {ranked_rank} | {cell.status} |"
    )


def _unique_ordered(items: list) -> list:
    seen: set = set()
    out: list = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out
