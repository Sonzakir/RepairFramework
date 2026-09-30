"""Compare every repair approach built over the course on one set of bugs.

Where ``RepairComparisonRunner`` compares FL modes for the template technique and
``LLMRepairComparisonRunner`` compares LLM repair variants, this runner compares
the *approaches themselves* — traditional template repair, single-shot LLM
repair, iterative LLM repair, and the fully LLM-driven pipeline (LLM fault
localization -> LLM repair with context retrieval -> LLM patch assessment).

Every cell runs with the LLM patch assessor attached and context-similarity
scoring enabled, so all four columns are measured the same way. That matters
because the pass/fail oracle alone is a blunt instrument: a patch is "correct"
only when its diff matches the developer fix exactly, which buckets a
semantically equivalent fix and a nonsense one that happens to pass the tests
together. The assessor's quality score and the graded context-similarity score
separate those cases, so the report can say *how close* an approach got rather
than only whether it landed a byte-exact match.

Each cell is one ``(bug, approach, fl_mode)`` triple — except approaches that
localize with the LLM, which have exactly one FL source and therefore contribute
one cell per bug. The generate-validate-correctness-assess loop and the per-run
``repair_results.json`` / ``execution.log`` artifacts are delegated to
``RepairEvaluationRunner``, so every cell gets its own ``run_NNN`` directory.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from apr_framework.benchmarks.base import BenchmarkAdapter
from apr_framework.core.models import BugIdentifier, LocalizationResult
from apr_framework.evaluation.course_approaches import CourseApproach
from apr_framework.repair.assessment.base import PatchAssessor
from apr_framework.repair.base import RepairAlgorithm
from apr_framework.repair.ranking.base import PatchRanker

if TYPE_CHECKING:
    from apr_framework.evaluation.run_writer import RunWriter

# Builds the localization input for one cell. The CLI handler owns the concrete
# perfect / FauxPy / LLM-FL construction; raising is allowed and recorded as an
# error cell rather than aborting the matrix.
LocalizationProvider = Callable[
    [BugIdentifier, CourseApproach, str], LocalizationResult
]

# Builds a fresh repair algorithm bound to one cell's approach and localization
# result. Fresh per cell so each cell's LLM query counter starts at zero.
RepairAlgorithmFactory = Callable[[CourseApproach, LocalizationResult], RepairAlgorithm]

# Builds a fresh patch assessor per cell, for the same counter-isolation reason.
AssessorFactory = Callable[[], PatchAssessor | None]

# FL-mode label used for approaches whose FL source is the LLM, so the single
# cell they produce is distinguishable from the auto/perfect cells.
LLM_FL_MODE_LABEL = "llm-fl"

# A localizer can succeed (no exception, no error) and still rank nothing: FauxPy
# exits clean but writes empty score tables, or LLM-FL anchors on no project
# source. Repair then has no target and generates zero candidates. Reporting that
# as "no_patch" is indistinguishable from "FL worked, repair found nothing" — a
# far stronger claim — so an empty ranking gets a status of its own.
NO_FL_LOCATIONS_STATUS = "no_fl_locations"


@dataclass
class CourseCellResult:
    """Metrics for one (bug, approach, FL mode) cell of the comparison matrix."""

    bug: BugIdentifier
    approach_label: str
    fl_mode: str
    fl_backend: str
    status: str

    # Repair loop
    llm_query_count: int | None = None
    total_candidates_generated: int = 0
    candidates_validated: int = 0
    plausible_count: int = 0
    correct_count: int = 0
    time_to_first_plausible_seconds: float | None = None
    total_wall_clock_seconds: float = 0.0
    generation_rank_of_first_correct: int | None = None
    ranked_rank_of_first_correct: int | None = None

    # LLM patch assessment (semantic quality, 0.0-1.0)
    assessment_query_count: int | None = None
    assessed_patch_count: int = 0
    best_quality_score: float | None = None
    mean_quality_score: float | None = None
    rank_of_first_correct_by_assessment: int | None = None
    is_top_assessed_patch_correct: bool = False
    # Quality the assessor gave the patch that actually matches the developer fix.
    # A low value here is an assessor *false negative* — it under-rated a real fix.
    quality_score_of_first_correct: float | None = None

    # Graded closeness to the developer fix (0.0-1.0)
    best_context_similarity_score: float | None = None
    mean_context_similarity_score: float | None = None
    similarity_band_of_best: str | None = None

    # Context retrieval (only non-zero for approaches with a retrieval budget)
    retrieval_step_total: int = 0
    retrieval_tool_call_counts: dict[str, int] = field(default_factory=dict)
    patches_with_retrieval_count: int = 0

    # LLM fault-localization evidence (only for LLM-FL approaches)
    fl_location_count: int | None = None
    fl_files_shown: list[str] = field(default_factory=list)

    run_dir: str | None = None
    error: str | None = None


class CourseComparisonRunner:
    """Run every requested approach on every bug and aggregate the outcome."""

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
        approaches: list[CourseApproach],
        fl_modes: list[str],
        benchmark: BenchmarkAdapter,
        localization_provider: LocalizationProvider,
        repair_algorithm_factory: RepairAlgorithmFactory,
        assessor_factory: AssessorFactory,
        output_dir: Path,
    ) -> list[CourseCellResult]:
        """Execute the whole matrix, flushing ``results.json`` after every cell.

        Approaches that localize with the LLM ignore *fl_modes* and contribute a
        single cell per bug; the others are run once per requested FL mode.
        """
        output_dir.mkdir(parents=True, exist_ok=True)
        cells: list[CourseCellResult] = []

        for bug in bugs:
            for approach in approaches:
                for fl_mode in self._resolve_fl_modes_for_approach(approach, fl_modes):
                    print(
                        f"  [{bug.project} #{bug.bug_id}] approach={approach.label} "
                        f"FL={fl_mode} ...",
                        flush=True,
                    )
                    cell = self._run_one_cell(
                        bug,
                        approach,
                        fl_mode,
                        benchmark,
                        localization_provider,
                        repair_algorithm_factory,
                        assessor_factory,
                    )
                    cells.append(cell)
                    # Crash-safe flush: a long API run keeps its finished cells.
                    self._write_json(cells, output_dir / "results.json")

        return cells

    @staticmethod
    def _resolve_fl_modes_for_approach(
        approach: CourseApproach, fl_modes: list[str]
    ) -> list[str]:
        """An LLM-FL approach has one FL source; the others span the FL-mode axis."""
        if approach.uses_llm_fault_localization:
            return [LLM_FL_MODE_LABEL]
        return fl_modes

    def _run_one_cell(
        self,
        bug: BugIdentifier,
        approach: CourseApproach,
        fl_mode: str,
        benchmark: BenchmarkAdapter,
        localization_provider: LocalizationProvider,
        repair_algorithm_factory: RepairAlgorithmFactory,
        assessor_factory: AssessorFactory,
    ) -> CourseCellResult:
        """Localize, repair, assess, and read back the metrics for one matrix cell."""
        from apr_framework.evaluation.repair_runner import RepairEvaluationRunner
        from apr_framework.evaluation.run_writer import RunWriter

        try:
            localization_result = localization_provider(bug, approach, fl_mode)
        except Exception as localization_error:  # noqa: BLE001 — one bad cell must not abort the matrix
            print(f"    LOCALIZATION ERROR: {localization_error}", flush=True)
            return CourseCellResult(
                bug=bug,
                approach_label=approach.label,
                fl_mode=fl_mode,
                fl_backend=self._backend_label(fl_mode),
                status="error",
                error=f"{type(localization_error).__name__}: {localization_error}",
            )

        writer = RunWriter.create(self._runs_dir)
        cell_config_data = self._build_cell_config_data(
            bug, approach, fl_mode, localization_result
        )
        writer.write_json(
            "config.json", {"runner": "course-comparison", **cell_config_data}
        )
        writer.log(
            f"Course comparison cell: {bug.project}#{bug.bug_id} "
            f"approach={approach.label} fl_mode={fl_mode} "
            f"backend={localization_result.backend}"
        )

        if not localization_result.ranked_locations:
            return self._build_cell_for_empty_ranking_and_log(
                bug, approach, fl_mode, localization_result, writer
            )

        try:
            repair_algorithm = repair_algorithm_factory(approach, localization_result)
            evaluation_runner = RepairEvaluationRunner(
                project_root=self._project_root,
                runs_dir=self._runs_dir,
                budget=self._budget,
                stop_on_first=self._stop_on_first,
                config_data=cell_config_data,
                writer=writer,
                ranker=self._ranker,
                assessor=assessor_factory(),
                localization_result=localization_result,
                score_similarity=True,
            )
            evaluation_runner.run([bug], benchmark, repair_algorithm)
        except Exception as repair_error:  # noqa: BLE001
            print(f"    REPAIR ERROR: {repair_error}", flush=True)
            return CourseCellResult(
                bug=bug,
                approach_label=approach.label,
                fl_mode=fl_mode,
                fl_backend=localization_result.backend,
                status="error",
                run_dir=str(writer.run_dir),
                error=f"{type(repair_error).__name__}: {repair_error}",
            )

        return self._build_cell_from_run_dir_and_localization(
            bug, approach, fl_mode, localization_result, writer.run_dir
        )

    def _build_cell_for_empty_ranking_and_log(
        self,
        bug: BugIdentifier,
        approach: CourseApproach,
        fl_mode: str,
        localization_result: LocalizationResult,
        writer: RunWriter,
    ) -> CourseCellResult:
        """Record a cell whose localizer ranked nothing, without running repair.

        Running the repair loop against an empty ranking would burn a cell's
        budget to prove the obvious — no location, no candidate — and would then
        report the outcome as ``no_patch``, which reads as a statement about the
        *repair* technique. The failure belongs to fault localization, so the
        cell says so and stops here.
        """
        note = (
            f"{localization_result.backend} produced no ranked location for "
            f"{bug.project}#{bug.bug_id}; repair has no target. Recorded as "
            f"{NO_FL_LOCATIONS_STATUS} — this is a fault-localization failure, "
            "not a repair failure."
        )
        print(f"    NO FL LOCATIONS: {note}", flush=True)
        writer.log(note)
        return CourseCellResult(
            bug=bug,
            approach_label=approach.label,
            fl_mode=fl_mode,
            fl_backend=localization_result.backend,
            status=NO_FL_LOCATIONS_STATUS,
            fl_location_count=0,
            fl_files_shown=list(localization_result.metadata.get("files_shown", [])),
            run_dir=str(writer.run_dir),
        )

    def _build_cell_config_data(
        self,
        bug: BugIdentifier,
        approach: CourseApproach,
        fl_mode: str,
        localization_result: LocalizationResult,
    ) -> dict[str, Any]:
        """Build the config.json payload recording exactly how this cell was run."""
        return {
            **self._repair_config_data,
            "project": bug.project,
            "bug_id": bug.bug_id,
            "approach": approach.label,
            "technique": approach.technique,
            "fl_mode": fl_mode,
            "fl_backend": localization_result.backend,
            "context_enrichment": approach.context_enrichment,
            "iterative": approach.iterative,
            "retrieval_budget": approach.retrieval_budget,
            "assess": True,
            "similarity_score": True,
            "ranker": self._ranker.name if self._ranker else "none",
        }

    # ------------------------------------------------------------------
    # Reading one cell's metrics back from the run it produced
    # ------------------------------------------------------------------

    def _build_cell_from_run_dir_and_localization(
        self,
        bug: BugIdentifier,
        approach: CourseApproach,
        fl_mode: str,
        localization_result: LocalizationResult,
        run_dir: Path,
    ) -> CourseCellResult:
        payload = json.loads(
            (run_dir / "repair_results.json").read_text(encoding="utf-8")
        )
        metrics = payload.get("metrics", {})
        plausible_patches = payload.get("plausible_patches", []) or []
        assessed_patches = payload.get("assessed_plausible_patches", []) or []
        all_results = payload.get("all_results", []) or []

        assessment_summary = _summarise_assessment_from_patches(assessed_patches)
        # Similarity is summarised over *every* candidate, not just the plausible
        # ones: an approach whose patches all failed the tests still generated
        # edits, and how close they came to the developer fix is exactly what the
        # graded metric exists to say. Restricting it to plausible patches left
        # such approaches with an empty column that looked like "not measured".
        similarity_summary = _summarise_similarity_from_patches(all_results)
        retrieval_summary = _summarise_retrieval_from_patches(all_results)

        return CourseCellResult(
            bug=bug,
            approach_label=approach.label,
            fl_mode=fl_mode,
            fl_backend=localization_result.backend,
            status=payload.get("status", "unknown"),
            llm_query_count=metrics.get("llm_query_count"),
            total_candidates_generated=metrics.get("total_candidates_generated", 0),
            candidates_validated=metrics.get("candidates_validated", 0),
            plausible_count=metrics.get("plausible_count", 0),
            correct_count=metrics.get("correct_count", 0),
            time_to_first_plausible_seconds=metrics.get(
                "time_to_first_plausible_seconds"
            ),
            total_wall_clock_seconds=metrics.get("total_wall_clock_seconds", 0.0),
            generation_rank_of_first_correct=_find_rank_of_first_correct(
                plausible_patches
            ),
            ranked_rank_of_first_correct=metrics.get("rank_of_first_correct"),
            assessment_query_count=metrics.get("assessment_query_count"),
            assessed_patch_count=assessment_summary.assessed_patch_count,
            best_quality_score=assessment_summary.best_quality_score,
            mean_quality_score=assessment_summary.mean_quality_score,
            rank_of_first_correct_by_assessment=(
                assessment_summary.rank_of_first_correct
            ),
            is_top_assessed_patch_correct=assessment_summary.is_top_patch_correct,
            quality_score_of_first_correct=(
                assessment_summary.quality_score_of_first_correct
            ),
            best_context_similarity_score=similarity_summary.best_score,
            mean_context_similarity_score=similarity_summary.mean_score,
            similarity_band_of_best=similarity_summary.band_of_best,
            retrieval_step_total=retrieval_summary.step_total,
            retrieval_tool_call_counts=retrieval_summary.tool_call_counts,
            patches_with_retrieval_count=retrieval_summary.patches_with_retrieval_count,
            fl_location_count=len(localization_result.ranked_locations),
            fl_files_shown=list(localization_result.metadata.get("files_shown", [])),
            run_dir=str(run_dir),
        )

    @staticmethod
    def _backend_label(fl_mode: str) -> str:
        if fl_mode == "perfect":
            return "perfect-fl"
        if fl_mode == LLM_FL_MODE_LABEL:
            return "llm-fl"
        return "auto-fl"

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def write_results(self, cells: list[CourseCellResult], output_dir: Path) -> Path:
        """Write ``results.json`` and ``README.md`` to *output_dir*; return README path."""
        output_dir.mkdir(parents=True, exist_ok=True)
        self._write_json(cells, output_dir / "results.json")
        readme_path = output_dir / "README.md"
        readme_path.write_text(self._build_readme(cells), encoding="utf-8")
        return readme_path

    def copy_run_artifacts(
        self, cells: list[CourseCellResult], output_dir: Path
    ) -> None:
        """Copy each cell's ``run_NNN`` directory into ``output_dir/run_artifacts``.

        Makes the committed report self-contained: per-cell logs, retrieval
        traces, and patch diffs travel with the numbers.
        """
        artifacts_dir = output_dir / "run_artifacts"
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        for cell in cells:
            if not cell.run_dir:
                continue
            source_run_dir = Path(cell.run_dir)
            if not source_run_dir.exists():
                continue
            destination = artifacts_dir / source_run_dir.name
            if destination.exists():
                shutil.rmtree(destination)
            shutil.copytree(source_run_dir, destination)

    def _write_json(self, cells: list[CourseCellResult], path: Path) -> None:
        rows = [
            {
                "bug": {
                    "benchmark": cell.bug.benchmark,
                    "project": cell.bug.project,
                    "bug_id": cell.bug.bug_id,
                },
                "approach": cell.approach_label,
                "fl_mode": cell.fl_mode,
                "fl_backend": cell.fl_backend,
                "status": cell.status,
                "llm_query_count": cell.llm_query_count,
                "total_candidates_generated": cell.total_candidates_generated,
                "candidates_validated": cell.candidates_validated,
                "plausible_count": cell.plausible_count,
                "correct_count": cell.correct_count,
                "time_to_first_plausible_seconds": cell.time_to_first_plausible_seconds,
                "total_wall_clock_seconds": cell.total_wall_clock_seconds,
                "generation_rank_of_first_correct": cell.generation_rank_of_first_correct,
                "ranked_rank_of_first_correct": cell.ranked_rank_of_first_correct,
                "assessment_query_count": cell.assessment_query_count,
                "assessed_patch_count": cell.assessed_patch_count,
                "best_quality_score": cell.best_quality_score,
                "mean_quality_score": cell.mean_quality_score,
                "rank_of_first_correct_by_assessment": cell.rank_of_first_correct_by_assessment,
                "is_top_assessed_patch_correct": cell.is_top_assessed_patch_correct,
                "quality_score_of_first_correct": cell.quality_score_of_first_correct,
                "best_context_similarity_score": cell.best_context_similarity_score,
                "mean_context_similarity_score": cell.mean_context_similarity_score,
                "similarity_band_of_best": cell.similarity_band_of_best,
                "retrieval_step_total": cell.retrieval_step_total,
                "retrieval_tool_call_counts": cell.retrieval_tool_call_counts,
                "patches_with_retrieval_count": cell.patches_with_retrieval_count,
                "fl_location_count": cell.fl_location_count,
                "fl_files_shown": cell.fl_files_shown,
                "run_dir": cell.run_dir,
                "error": cell.error,
            }
            for cell in cells
        ]
        path.write_text(
            json.dumps(
                {
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "model": self._repair_config_data.get("model"),
                    "ranker": self._ranker.name if self._ranker else "none",
                    "budget": self._budget,
                    "results": rows,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    # ------------------------------------------------------------------
    # Report rendering
    # ------------------------------------------------------------------

    def _build_readme(self, cells: list[CourseCellResult]) -> str:
        bugs = _unique_ordered([cell.bug for cell in cells])
        approach_labels = _unique_ordered([cell.approach_label for cell in cells])
        index = _index_cells_by_bug_approach_and_fl_mode(cells)

        lines: list[str] = []
        lines.append("# Course-Wide Comparison: All Repair Approaches\n")
        model_name = self._repair_config_data.get("model", "?")
        lines.append(
            f"Every approach built over the course, run on {len(bugs)} BugsInPy bug(s): "
            "traditional template repair, single-shot LLM repair, iterative LLM repair, "
            "and the fully LLM-driven pipeline "
            "(LLM-FL -> LLM repair with context retrieval -> LLM assessment).\n"
        )
        lines.append(
            f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} · "
            f"model: `{model_name}` · per-cell validation budget: {self._budget}\n"
        )
        lines.append(self._build_metric_legend())

        lines.append("\n## Course-wide comparison (per bug)\n")
        for bug in bugs:
            lines.append(
                self._build_course_wide_table_for_bug(bug, approach_labels, index)
            )
            lines.append("")

        lines.append(self._build_per_cell_detail_tables(bugs, approach_labels, index))

        lines.append("\n## Analysis\n")
        lines.append(self._build_fault_localization_availability_analysis(cells))
        lines.append("")
        lines.append(self._build_llm_fl_quality_analysis(bugs, index))
        lines.append("")
        lines.append(self._build_retrieval_effect_analysis(cells))
        lines.append("")
        lines.append(self._build_assessment_quality_analysis(cells))
        lines.append("")
        lines.append(
            self._build_overall_pipeline_analysis(bugs, approach_labels, index)
        )

        return "\n".join(lines) + "\n"

    @staticmethod
    def _build_metric_legend() -> str:
        """Explain why the table carries more than plausible/exact-diff counts."""
        return (
            "\n**On the metrics.**  **`Exact diff`** counts patches whose diff matches "
            "the developer fix byte-for-byte. It is deliberately *not* called "
            "*correct*: a semantically correct fix written differently from the "
            "developer's scores 0 here, so a 0 in this column is not a claim that the "
            "patch is wrong. It is the framework's data-contamination signal, and "
            "nothing more. Two graded metrics carry the actual quality judgment, and "
            "every cell of every column is measured with both:\n\n"
            "- **Assessment quality score** (`0.0`-`1.0`) — the LLM assessor's judgment "
            "of whether the patch genuinely fixes the bug or just overfits the test "
            "suite. This is the semantic signal the pass/fail oracle cannot give.\n"
            "- **Context similarity score** (`0.0`-`1.0`) — how close the patch's edit "
            "is to the developer's, including the surrounding context lines. `1.00` is "
            "a byte-exact match; a high-but-sub-1.0 score is a near-miss that `Exact "
            "diff` reports as a flat 0. It is scored on **every candidate an approach "
            "generated, plausible or not**, so an approach whose patches all failed the "
            "test suite still reports how close it came — otherwise its column would be "
            "empty and it could not be compared at all.\n"
        )

    def _build_course_wide_table_for_bug(
        self,
        bug: BugIdentifier,
        approach_labels: list[str],
        index: dict[tuple[BugIdentifier, str, str], CourseCellResult],
    ) -> str:
        """Render the assignment's four-column table for one bug."""
        from apr_framework.evaluation.course_approaches import (
            COURSE_APPROACHES_BY_LABEL,
        )

        best_cell_by_approach: dict[str, CourseCellResult | None] = {
            approach_label: _select_best_cell_for_approach(bug, approach_label, index)
            for approach_label in approach_labels
        }

        header_cells = []
        for approach_label in approach_labels:
            approach = COURSE_APPROACHES_BY_LABEL.get(approach_label)
            header_cells.append(approach.column_title if approach else approach_label)

        lines = [f"### {bug.project} bug #{bug.bug_id}\n"]
        lines.append("| | " + " | ".join(header_cells) + " |")
        lines.append("|---|" + "---|" * len(approach_labels))

        def row(title: str, render_cell: Callable[[str], str]) -> str:
            return (
                f"| {title} | "
                + " | ".join(render_cell(label) for label in approach_labels)
                + " |"
            )

        def approach_of(approach_label: str) -> CourseApproach | None:
            return COURSE_APPROACHES_BY_LABEL.get(approach_label)

        def fl_source(approach_label: str) -> str:
            """Name the FL modes this bug was *actually* run under, not the ideal set.

            The approach's own description says "auto/perfect", but on a bug FauxPy
            cannot localize only the perfect cell exists — printing "auto/perfect"
            there would advertise a baseline that never ran.
            """
            approach = approach_of(approach_label)
            if approach is None:
                return "—"
            if approach.uses_llm_fault_localization:
                return approach.fault_localization_description
            fl_modes_run = [
                cell_fl_mode
                for (cell_bug, cell_approach_label, cell_fl_mode) in index
                if cell_bug == bug and cell_approach_label == approach_label
            ]
            if "auto" not in fl_modes_run:
                return "perfect only (auto: FauxPy cannot localize)"
            return approach.fault_localization_description

        def repair_kind(approach_label: str) -> str:
            approach = approach_of(approach_label)
            return approach.repair_description if approach else "—"

        def assessment_kind(approach_label: str) -> str:
            # Every column is assessed here; the row records what each approach
            # *originally* used as its acceptance oracle, per the assignment table.
            approach = approach_of(approach_label)
            if approach and approach.uses_llm_fault_localization:
                return "LLM assessor"
            return "test pass/fail"

        lines.append(row("FL source", fl_source))
        lines.append(row("Repair", repair_kind))
        lines.append(row("Assessment", assessment_kind))
        lines.append(
            row(
                "Plausible patches",
                lambda label: _render_count_with_fl_mode(
                    best_cell_by_approach[label], "plausible_count"
                ),
            )
        )
        lines.append(
            row(
                "Exact-diff matches (not a correctness verdict)",
                lambda label: _render_count_with_fl_mode(
                    best_cell_by_approach[label], "correct_count"
                ),
            )
        )
        lines.append(
            row(
                "**Best assessment quality score**",
                lambda label: _render_score(
                    best_cell_by_approach[label], "best_quality_score"
                ),
            )
        )
        lines.append(
            row(
                "**Best context similarity score** (any candidate)",
                lambda label: _render_score(
                    best_cell_by_approach[label], "best_context_similarity_score"
                ),
            )
        )
        lines.append(
            row(
                "Time to first plausible",
                lambda label: _render_time_to_first_plausible(
                    best_cell_by_approach[label]
                ),
            )
        )

        lines.append(
            "\n_Columns whose FL source is auto/perfect were run under every FL mode "
            "available for this bug — `auto` is skipped entirely on bugs FauxPy cannot "
            "localize, rather than recorded as a zero. The cell shown is the best of "
            "those runs, chosen by max exact-diff matches → max plausible → fastest "
            "time to first plausible, with the winning FL mode in parentheses. `—` "
            "means the approach produced nothing, the cell errored, or its localizer "
            f"ranked nothing at all (`{NO_FL_LOCATIONS_STATUS}`); the per-cell tables "
            "below say which._"
        )
        return "\n".join(lines)

    def _build_per_cell_detail_tables(
        self,
        bugs: list[BugIdentifier],
        approach_labels: list[str],
        index: dict[tuple[BugIdentifier, str, str], CourseCellResult],
    ) -> str:
        """Render every individual cell, so the collapsed table above is auditable."""
        lines = ["\n## Every cell (per bug × approach × FL mode)\n"]
        for bug in bugs:
            lines.append(f"### {bug.project} bug #{bug.bug_id}\n")
            lines.append(
                "| Approach | FL mode | Queries | Generated | Plausible | Exact diff "
                "| Best quality | Best similarity (any cand.) | Retrieval steps "
                "| 1st plausible (s) | Total (s) | Outcome |"
            )
            lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
            for approach_label in approach_labels:
                for (
                    cell_bug,
                    cell_approach_label,
                    _fl_mode,
                ), cell in index.items():
                    if cell_bug == bug and cell_approach_label == approach_label:
                        lines.append(_render_detail_row(cell))
            lines.append("")
        return "\n".join(lines)

    def _build_fault_localization_availability_analysis(
        self, cells: list[CourseCellResult]
    ) -> str:
        """State up front which cells never got a fault location to repair.

        A cell whose FL errored or ranked nothing scores zero on every metric,
        and those zeros look exactly like "the repair technique tried and
        failed". They are not the same claim, and the report must not let a
        reader conflate them.
        """
        unusable_cells = [cell for cell in cells if not _is_cell_usable(cell)]
        bugs_without_auto_cells = _unique_ordered(
            [
                cell.bug
                for cell in cells
                if not any(
                    other.bug == cell.bug and other.fl_mode == "auto" for other in cells
                )
            ]
        )
        skipped_auto_note = ""
        if bugs_without_auto_cells:
            skipped_bugs_text = ", ".join(
                f"`{bug.project}#{bug.bug_id}`" for bug in bugs_without_auto_cells
            )
            skipped_auto_note = (
                f" Automated FL was **not run at all** on {skipped_bugs_text}: FauxPy "
                "0.7.0 cannot localize those bugs (Python 3.7 pins it cannot install "
                "on, or a dependency conflict with the project's own pins). Those bugs "
                "are reported under perfect FL and LLM-FL only — an approach that "
                "cannot run is left out, not scored as a zero."
            )

        header = "**Which cells actually had a fault location to repair?**  "
        if not unusable_cells:
            return (
                header
                + "Every cell that was run did. No cell errored and no localizer "
                "came back empty, so each column's numbers reflect the *repair* "
                "approach rather than a missing or broken localizer."
                + skipped_auto_note
            )

        failure_lines = [
            f"- `{cell.bug.project}#{cell.bug.bug_id}` ({cell.approach_label}, "
            f"FL={cell.fl_mode}): "
            + (
                f"FL ranked nothing (`{NO_FL_LOCATIONS_STATUS}`) — the localizer "
                "exited cleanly but proposed no location, so repair had no target."
                if cell.status == NO_FL_LOCATIONS_STATUS
                else f"errored — {_summarize_error(cell.error or '')}"
            )
            for cell in unusable_cells
        ]
        return (
            header
            + f"**{len(unusable_cells)} of {len(cells)} cell(s) did not.** Their zeros "
            "measure the localizer, not the repair approach, and are shown as `—` in "
            "the per-bug tables rather than being compared against cells that ran:\n\n"
            + "\n".join(failure_lines)
            + (f"\n\n{skipped_auto_note.strip()}" if skipped_auto_note else "")
        )

    def _build_llm_fl_quality_analysis(
        self,
        bugs: list[BugIdentifier],
        index: dict[tuple[BugIdentifier, str, str], CourseCellResult],
    ) -> str:
        """Did LLM-FL outperform SBFL/MBFL, and where?"""
        from apr_framework.evaluation.course_approaches import FULL_LLM_APPROACH_LABEL

        observations: list[str] = []
        for bug in bugs:
            llm_fl_cell = index.get((bug, FULL_LLM_APPROACH_LABEL, LLM_FL_MODE_LABEL))
            if llm_fl_cell is None:
                continue
            auto_cells = [
                cell
                for (cell_bug, _approach, fl_mode), cell in index.items()
                if cell_bug == bug and fl_mode == "auto"
            ]
            bug_key = f"`{bug.project}#{bug.bug_id}`"

            # An empty ranking is a *failure* of LLM-FL, not a quiet zero: symbol
            # anchoring found nothing to show the model, so no location was ever
            # proposed and repair had nothing to attempt. Report it before any
            # comparison, so a misfire is never dressed up as "LLM-FL ran".
            if _has_llm_fault_localization_misfired(llm_fl_cell):
                auto_worked = any(_is_cell_usable(cell) for cell in auto_cells)
                observation = (
                    f"- {bug_key}: LLM-FL **produced no ranked location at all** "
                    f"(files shown to the model: {llm_fl_cell.fl_files_shown or 'none'}). "
                    "Symbol anchoring found no project source to show — the failing test "
                    "neither mocks a project symbol nor yields a traceback with a project "
                    "frame — so the pipeline generated zero candidates. This is a genuine "
                    "failure mode of LLM-FL"
                )
                observation += (
                    ", and here automated FL's coverage-based ranking was the better "
                    "tool: it ranked locations on this bug where the LLM localizer "
                    "ranked none."
                    if auto_worked
                    else "; automated FL was no help on this bug either."
                )
                observations.append(observation)
                continue

            # No auto cell at all means automated FL was deliberately not run on
            # this bug (FauxPy cannot localize it). Comparing LLM-FL against an
            # absent baseline as if the baseline had scored 0 would invent a win.
            if not auto_cells:
                observations.append(
                    f"- {bug_key}: automated (FauxPy) FL **was not run** — this bug is "
                    "outside FauxPy 0.7.0's reach (see the bug-set note in the "
                    "repository README), so there is no SBFL/MBFL baseline to compare "
                    f"against. LLM-FL localized it and reached "
                    f"{llm_fl_cell.plausible_count} plausible / "
                    f"{llm_fl_cell.correct_count} exact-diff match(es); that the LLM "
                    "localizer runs at all here is itself the difference."
                )
                continue

            auto_all_errored = bool(auto_cells) and all(
                cell.error for cell in auto_cells
            )
            if auto_all_errored and not llm_fl_cell.error:
                observations.append(
                    f"- {bug_key}: every automated (FauxPy) FL cell **errored**, while "
                    f"LLM-FL ran and reached {llm_fl_cell.plausible_count} plausible / "
                    f"{llm_fl_cell.correct_count} exact-diff match(es) — the LLM "
                    "localizer has no FauxPy install step, so it reaches bugs whose "
                    "pinned Python FauxPy cannot support."
                )
                continue

            auto_plausible_best = max(
                (cell.plausible_count for cell in auto_cells), default=0
            )
            auto_correct_best = max(
                (cell.correct_count for cell in auto_cells), default=0
            )
            if llm_fl_cell.correct_count > auto_correct_best:
                observations.append(
                    f"- {bug_key}: LLM-FL reached **{llm_fl_cell.correct_count} "
                    f"exact-diff match(es)** vs {auto_correct_best} under automated FL."
                )
            elif llm_fl_cell.plausible_count > auto_plausible_best:
                observations.append(
                    f"- {bug_key}: LLM-FL reached **{llm_fl_cell.plausible_count} "
                    f"plausible** patch(es) vs {auto_plausible_best} under automated FL."
                )
            elif llm_fl_cell.plausible_count < auto_plausible_best:
                observations.append(
                    f"- {bug_key}: LLM-FL **underperformed** automated FL "
                    f"({llm_fl_cell.plausible_count} vs {auto_plausible_best} plausible)."
                )
            else:
                observations.append(
                    f"- {bug_key}: LLM-FL and automated FL reached the same outcome "
                    f"({llm_fl_cell.plausible_count} plausible)."
                )

            if llm_fl_cell.fl_files_shown and all(
                "test" in file_shown for file_shown in llm_fl_cell.fl_files_shown
            ):
                observations.append(
                    f"  - Caveat for {bug_key}: the only source shown to the localizer "
                    f"was test code ({llm_fl_cell.fl_files_shown}), so its ranking could "
                    "not point at project source — symbol anchoring found no target."
                )

        header = (
            "**Did LLM-FL outperform SBFL/MBFL?**  Comparing the LLM-FL cell against the "
            "automated-FL (FauxPy) cells for the same bug:"
        )
        if not observations:
            return (
                header + "\n\n_No LLM-FL cells were run, so this cannot be assessed._"
            )
        return header + "\n\n" + "\n".join(observations)

    def _build_retrieval_effect_analysis(self, cells: list[CourseCellResult]) -> str:
        """What did the model retrieve, and did retrieval cells fare better?"""
        retrieval_cells = [cell for cell in cells if cell.retrieval_step_total > 0]
        if not retrieval_cells:
            return (
                "**Did context retrieval help?**  The model requested **no** retrieval "
                "steps in any cell. With a non-zero budget available, declining to "
                "retrieve is itself a signal: the fault regions in this bug set were "
                "self-contained enough that the model judged the prompt sufficient."
            )

        tool_call_totals: dict[str, int] = {}
        for cell in retrieval_cells:
            for tool_name, call_count in cell.retrieval_tool_call_counts.items():
                tool_call_totals[tool_name] = (
                    tool_call_totals.get(tool_name, 0) + call_count
                )
        tools_text = ", ".join(
            f"`{tool_name}` ×{call_count}"
            for tool_name, call_count in sorted(
                tool_call_totals.items(), key=lambda pair: -pair[1]
            )
        )
        step_total = sum(cell.retrieval_step_total for cell in retrieval_cells)
        plausible_total = sum(cell.plausible_count for cell in retrieval_cells)
        correct_total = sum(cell.correct_count for cell in retrieval_cells)

        per_cell_lines = [
            f"- `{cell.bug.project}#{cell.bug.bug_id}` ({cell.approach_label}): "
            f"{cell.retrieval_step_total} retrieval step(s) across "
            f"{cell.patches_with_retrieval_count} prompt(s) — "
            + (
                ", ".join(
                    f"`{tool_name}` ×{call_count}"
                    for tool_name, call_count in cell.retrieval_tool_call_counts.items()
                )
                or "no tools recorded"
            )
            + f"; {cell.plausible_count} plausible, {cell.correct_count} exact-diff."
            for cell in retrieval_cells
        ]

        return (
            "**Did context retrieval help?**  The model spent "
            f"**{step_total} retrieval step(s)** across {len(retrieval_cells)} cell(s) "
            f"({tools_text}), which produced {plausible_total} plausible patch(es) and "
            f"{correct_total} exact-diff match(es). Retrieval pays off only when the "
            "fault region depends on code the model cannot already see; for "
            "self-contained regions it correctly declines to retrieve and patches "
            "directly.\n\n" + "\n".join(per_cell_lines)
        )

    def _build_assessment_quality_analysis(self, cells: list[CourseCellResult]) -> str:
        """Did the assessor surface correct patches and separate near-misses?"""
        assessed_cells = [cell for cell in cells if cell.assessed_patch_count > 0]
        if not assessed_cells:
            return (
                "**Was patch assessment useful?**  No plausible patch was produced in "
                "any cell, so the assessor had nothing to score."
            )

        cells_with_correct = [cell for cell in assessed_cells if cell.correct_count > 0]
        top_assessed_hits = [
            cell for cell in cells_with_correct if cell.is_top_assessed_patch_correct
        ]

        overfitting_cells = [
            cell
            for cell in assessed_cells
            if cell.correct_count == 0
            and cell.best_quality_score is not None
            and cell.best_quality_score < 0.5
        ]
        near_miss_cells = [
            cell
            for cell in assessed_cells
            if cell.correct_count == 0
            and cell.best_context_similarity_score is not None
            and cell.best_context_similarity_score >= 0.85
        ]

        # The assessor under-rating a patch that *is* the developer fix is its most
        # important failure mode — it is the counterweight to the "assessment is
        # useful" claim and must be reported, not averaged away.
        assessor_false_negative_cells = [
            cell
            for cell in cells_with_correct
            if cell.quality_score_of_first_correct is not None
            and cell.quality_score_of_first_correct < 0.5
        ]
        # Ranking a correct patch "first" is trivial when the cell held only one
        # plausible patch; say so rather than counting it as evidence.
        non_trivial_ranking_cells = [
            cell for cell in cells_with_correct if cell.assessed_patch_count > 1
        ]

        paragraphs = [
            "**Was patch assessment useful?**  The assessor scored "
            f"{sum(cell.assessed_patch_count for cell in assessed_cells)} plausible "
            f"patch(es) across {len(assessed_cells)} cell(s)."
        ]
        if cells_with_correct:
            ranking_sentence = (
                f"In {len(top_assessed_hits)} of {len(cells_with_correct)} cell(s) that "
                "contained an exact-diff match, the assessor ranked that patch "
                "**first**."
            )
            if len(non_trivial_ranking_cells) < len(cells_with_correct):
                trivial_count = len(cells_with_correct) - len(non_trivial_ranking_cells)
                ranking_sentence += (
                    f" That is weak evidence, though: {trivial_count} of those cell(s) "
                    "held only a single plausible patch, so ranking it first was "
                    "unavoidable rather than a judgment."
                )
            paragraphs.append(ranking_sentence)
        else:
            paragraphs.append(
                "No cell produced an exact-diff match, so the assessor's ability to rank "
                "one first could not be tested directly on this bug set."
            )
        if assessor_false_negative_cells:
            false_negative_text = ", ".join(
                f"`{cell.bug.project}#{cell.bug.bug_id}` ({cell.approach_label}, "
                f"quality {cell.quality_score_of_first_correct:.2f})"
                for cell in assessor_false_negative_cells
            )
            paragraphs.append(
                "**The assessor is not itself an oracle.** It scored *below 0.5* a patch "
                f"that exactly reproduces the developer fix in: {false_negative_text}. "
                "A low quality score is therefore evidence, not a verdict: the model "
                "judges a fix on how the edit reads in isolation, and a terse "
                "single-operator change (the kind template repair emits) can look "
                "unconvincing to it even when it is precisely what the developer wrote. "
                "This is the honest limit of LLM-based assessment, and the reason the "
                "exact-diff verdict is retained rather than replaced."
            )
        if overfitting_cells:
            overfitting_text = ", ".join(
                f"`{cell.bug.project}#{cell.bug.bug_id}` ({cell.approach_label}, "
                f"best quality {cell.best_quality_score:.2f})"
                for cell in overfitting_cells
            )
            paragraphs.append(
                "The assessor flagged likely **test-suite overfitting** — patches that "
                "pass every test yet score below 0.5 on semantic quality — in: "
                f"{overfitting_text}. The pass/fail oracle rates these identically to a "
                "genuine fix; the assessor does not."
            )
        if near_miss_cells:
            near_miss_text = ", ".join(
                f"`{cell.bug.project}#{cell.bug.bug_id}` ({cell.approach_label}, "
                f"similarity {cell.best_context_similarity_score:.2f})"
                for cell in near_miss_cells
            )
            paragraphs.append(
                "The similarity score also caught **near-misses the strict verdict hides**: "
                f"{near_miss_text} scored ≥0.85 against the developer fix while still "
                "counting as 0 exact-diff matches — the fix landed in the right place, in "
                "nearly the right form."
            )
        return "\n\n".join(paragraphs)

    def _build_overall_pipeline_analysis(
        self,
        bugs: list[BugIdentifier],
        approach_labels: list[str],
        index: dict[tuple[BugIdentifier, str, str], CourseCellResult],
    ) -> str:
        """Where did the full LLM pipeline improve on prior approaches, and where not?"""
        from apr_framework.evaluation.course_approaches import FULL_LLM_APPROACH_LABEL

        improvements: list[str] = []
        regressions: list[str] = []
        for bug in bugs:
            full_pipeline_cell = index.get(
                (bug, FULL_LLM_APPROACH_LABEL, LLM_FL_MODE_LABEL)
            )
            if full_pipeline_cell is None:
                continue
            prior_best_cells = [
                _select_best_cell_for_approach(bug, approach_label, index)
                for approach_label in approach_labels
                if approach_label != FULL_LLM_APPROACH_LABEL
            ]
            prior_cells = [cell for cell in prior_best_cells if cell is not None]
            prior_correct_best = max(
                (cell.correct_count for cell in prior_cells), default=0
            )
            prior_plausible_best = max(
                (cell.plausible_count for cell in prior_cells), default=0
            )
            bug_key = f"`{bug.project}#{bug.bug_id}`"

            if full_pipeline_cell.correct_count > prior_correct_best:
                improvements.append(
                    f"{bug_key} (exact-diff {prior_correct_best} → "
                    f"{full_pipeline_cell.correct_count})"
                )
            elif full_pipeline_cell.plausible_count > prior_plausible_best:
                improvements.append(
                    f"{bug_key} (plausible {prior_plausible_best} → "
                    f"{full_pipeline_cell.plausible_count})"
                )
            elif (
                full_pipeline_cell.correct_count < prior_correct_best
                or full_pipeline_cell.plausible_count < prior_plausible_best
            ):
                regressions.append(
                    f"{bug_key} (plausible {prior_plausible_best} → "
                    f"{full_pipeline_cell.plausible_count}, exact-diff "
                    f"{prior_correct_best} → {full_pipeline_cell.correct_count})"
                )

        paragraph = (
            "**Where did the full LLM pipeline improve, and where did it regress?**  "
        )
        if improvements:
            paragraph += (
                "It improved on the best prior approach for: "
                + "; ".join(improvements)
                + ". "
            )
        if regressions:
            paragraph += (
                "It regressed against the best prior approach for: "
                + "; ".join(regressions)
                + ". The most common cause is fault-location precision — the pipeline "
                "trades the oracle's exact lines for the LLM's ranking, so a misranked "
                "location costs it patches the perfect-FL cells reach for free. "
            )
        if not improvements and not regressions:
            paragraph += (
                "It matched the best prior approach on every bug in this set — no bug "
                "was won or lost by switching to the fully LLM-driven pipeline."
            )
        return paragraph


# ----------------------------------------------------------------------
# Per-cell metric summarisation (read back from repair_results.json)
# ----------------------------------------------------------------------


@dataclass
class _AssessmentSummary:
    assessed_patch_count: int = 0
    best_quality_score: float | None = None
    mean_quality_score: float | None = None
    rank_of_first_correct: int | None = None
    is_top_patch_correct: bool = False
    quality_score_of_first_correct: float | None = None


@dataclass
class _SimilaritySummary:
    best_score: float | None = None
    mean_score: float | None = None
    band_of_best: str | None = None


@dataclass
class _RetrievalSummary:
    step_total: int = 0
    tool_call_counts: dict[str, int] = field(default_factory=dict)
    patches_with_retrieval_count: int = 0


def _summarise_assessment_from_patches(
    assessed_patches: list[dict[str, Any]],
) -> _AssessmentSummary:
    """Reduce ``assessed_plausible_patches`` (quality-sorted) to cell-level figures."""
    if not assessed_patches:
        return _AssessmentSummary()

    quality_scores = [
        patch_payload.get("metadata", {}).get("quality_score")
        for patch_payload in assessed_patches
    ]
    known_scores = [score for score in quality_scores if score is not None]

    rank_of_first_correct: int | None = None
    quality_score_of_first_correct: float | None = None
    for position, patch_payload in enumerate(assessed_patches, start=1):
        if patch_payload.get("is_correct"):
            rank_of_first_correct = position
            quality_score_of_first_correct = patch_payload.get("metadata", {}).get(
                "quality_score"
            )
            break

    return _AssessmentSummary(
        assessed_patch_count=len(assessed_patches),
        best_quality_score=max(known_scores) if known_scores else None,
        mean_quality_score=(
            sum(known_scores) / len(known_scores) if known_scores else None
        ),
        rank_of_first_correct=rank_of_first_correct,
        is_top_patch_correct=bool(assessed_patches[0].get("is_correct")),
        quality_score_of_first_correct=quality_score_of_first_correct,
    )


def _summarise_similarity_from_patches(
    plausible_patches: list[dict[str, Any]],
) -> _SimilaritySummary:
    """Reduce the per-patch context-similarity scores to cell-level figures."""
    scored_patches = [
        patch_payload
        for patch_payload in plausible_patches
        if patch_payload.get("context_similarity_score") is not None
    ]
    if not scored_patches:
        return _SimilaritySummary()

    best_patch = max(
        scored_patches, key=lambda payload: payload["context_similarity_score"]
    )
    scores = [
        patch_payload["context_similarity_score"] for patch_payload in scored_patches
    ]
    return _SimilaritySummary(
        best_score=best_patch["context_similarity_score"],
        mean_score=sum(scores) / len(scores),
        band_of_best=best_patch.get("similarity_band"),
    )


def _summarise_retrieval_from_patches(
    all_results: list[dict[str, Any]],
) -> _RetrievalSummary:
    """Aggregate the per-patch retrieval traces over every candidate in the cell.

    Aggregating over ``all_results`` (not just the plausible ones) counts every
    prompt's retrieval, including the ones whose patch failed validation — the
    retrieval happened either way and its cost should be reported.
    """
    summary = _RetrievalSummary()
    for patch_payload in all_results:
        retrieval_payload = patch_payload.get("retrieval")
        if not retrieval_payload:
            continue
        steps = retrieval_payload.get("steps", []) or []
        if not steps:
            continue
        summary.patches_with_retrieval_count += 1
        summary.step_total += retrieval_payload.get("step_count", len(steps))
        for step in steps:
            tool_name = step.get("tool_name")
            if tool_name:
                summary.tool_call_counts[tool_name] = (
                    summary.tool_call_counts.get(tool_name, 0) + 1
                )
    return summary


def _is_cell_usable(cell: CourseCellResult) -> bool:
    """Did this cell actually exercise repair, i.e. did FL hand it any target?

    A cell is unusable when it errored (FauxPy could not install or run) or when
    its localizer ranked nothing. Either way its zeros say nothing about the
    repair approach, so they must never be compared against a cell that ran.
    """
    return not cell.error and cell.status != NO_FL_LOCATIONS_STATUS


def _has_llm_fault_localization_misfired(cell: CourseCellResult) -> bool:
    """Did LLM-FL fail to anchor on any project source at all?

    The localizer picks the source it shows the model from traceback frames and
    from the symbols the failing test mocks. When both signals come up empty it
    has nothing to show, ranks nothing, and repair gets no target — a silent zero
    that must not be read as "the approach simply found no patch".
    """
    if cell.error:
        return False
    return not cell.fl_files_shown or cell.fl_location_count == 0


def _find_rank_of_first_correct(patches: list[dict[str, Any]]) -> int | None:
    """1-based position of the first correct patch in the given (ordered) list."""
    for position, patch_payload in enumerate(patches, start=1):
        if patch_payload.get("is_correct"):
            return position
    return None


# ----------------------------------------------------------------------
# Report cell rendering
# ----------------------------------------------------------------------


def _index_cells_by_bug_approach_and_fl_mode(
    cells: list[CourseCellResult],
) -> dict[tuple[BugIdentifier, str, str], CourseCellResult]:
    return {(cell.bug, cell.approach_label, cell.fl_mode): cell for cell in cells}


def _select_best_cell_for_approach(
    bug: BugIdentifier,
    approach_label: str,
    index: dict[tuple[BugIdentifier, str, str], CourseCellResult],
) -> CourseCellResult | None:
    """Pick the approach's best FL-mode cell for one bug.

    Approaches spanning the auto/perfect axis produce two cells; the course-wide
    table has one column per approach, so the better cell represents it: most
    exact-diff matches, then most plausible, then fastest to the first plausible.
    Cells that never received a fault location (errored, or FL ranked nothing)
    lose to any cell that actually ran.
    """
    approach_cells = [
        cell
        for (cell_bug, cell_approach_label, _fl_mode), cell in index.items()
        if cell_bug == bug and cell_approach_label == approach_label
    ]
    usable_cells = [cell for cell in approach_cells if _is_cell_usable(cell)]
    if not usable_cells:
        return approach_cells[0] if approach_cells else None

    def ranking_key(cell: CourseCellResult) -> tuple[int, int, float]:
        time_to_first_plausible = (
            cell.time_to_first_plausible_seconds
            if cell.time_to_first_plausible_seconds is not None
            else float("inf")
        )
        return (cell.correct_count, cell.plausible_count, -time_to_first_plausible)

    return max(usable_cells, key=ranking_key)


def _render_count_with_fl_mode(cell: CourseCellResult | None, field_name: str) -> str:
    """Render a count, annotating which FL mode the winning cell used.

    A cell that never received a fault location renders as ``—``: its 0 would
    otherwise be read as "the approach tried and produced nothing".
    """
    if cell is None or not _is_cell_usable(cell):
        return "—"
    count = getattr(cell, field_name)
    if cell.fl_mode == LLM_FL_MODE_LABEL:
        return str(count)
    return f"{count} ({cell.fl_mode})"


def _render_score(cell: CourseCellResult | None, field_name: str) -> str:
    if cell is None or not _is_cell_usable(cell):
        return "—"
    score = getattr(cell, field_name)
    return f"{score:.2f}" if score is not None else "—"


def _render_time_to_first_plausible(cell: CourseCellResult | None) -> str:
    if (
        cell is None
        or not _is_cell_usable(cell)
        or cell.time_to_first_plausible_seconds is None
    ):
        return "—"
    return f"{cell.time_to_first_plausible_seconds:.1f}s"


def _render_detail_row(cell: CourseCellResult) -> str:
    if cell.error:
        return (
            f"| {cell.approach_label} | {cell.fl_mode} | — | — | — | — | — | — | — | — "
            f"| — | ERROR: {_summarize_error(cell.error)} |"
        )
    if cell.status == NO_FL_LOCATIONS_STATUS:
        return (
            f"| {cell.approach_label} | {cell.fl_mode} | — | — | — | — | — | — | — | — "
            f"| — | {NO_FL_LOCATIONS_STATUS}: FL ranked nothing, repair never ran |"
        )
    queries = str(cell.llm_query_count) if cell.llm_query_count is not None else "—"
    best_quality = (
        f"{cell.best_quality_score:.2f}" if cell.best_quality_score is not None else "—"
    )
    best_similarity = (
        f"{cell.best_context_similarity_score:.2f}"
        if cell.best_context_similarity_score is not None
        else "—"
    )
    time_to_first_plausible = (
        f"{cell.time_to_first_plausible_seconds:.1f}"
        if cell.time_to_first_plausible_seconds is not None
        else "—"
    )
    return (
        f"| {cell.approach_label} | {cell.fl_mode} | {queries} "
        f"| {cell.total_candidates_generated} | {cell.plausible_count} "
        f"| {cell.correct_count} | {best_quality} | {best_similarity} "
        f"| {cell.retrieval_step_total} | {time_to_first_plausible} "
        f"| {cell.total_wall_clock_seconds:.1f} | {cell.status} |"
    )


def _summarize_error(error_text: str, limit: int = 160) -> str:
    """Collapse a multi-line error to a single, table-safe, truncated note.

    The full error is preserved verbatim in ``results.json``; this is only the
    human-readable cell note, so newlines and pipes are stripped and it is capped.
    """
    collapsed = " ".join(
        part.strip() for part in error_text.splitlines() if part.strip()
    )
    collapsed = collapsed.replace("|", "/")
    first_sentence = collapsed.split(". ", 1)[0].strip()
    if 0 < len(first_sentence) <= limit and first_sentence != collapsed:
        return first_sentence + " (full error in results.json)"
    if len(collapsed) > limit:
        collapsed = collapsed[:limit].rstrip() + " …"
    return collapsed


def _unique_ordered(items: list) -> list:
    seen: set = set()
    ordered: list = []
    for item in items:
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered
