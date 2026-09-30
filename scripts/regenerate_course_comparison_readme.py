"""Rebuild the course-comparison README from an existing results.json.

Every cell of ``evaluate-course-comparison`` costs LLM calls and test-suite runs,
so the report must be re-renderable without re-running the matrix. This script
reloads the committed ``results.json`` into ``CourseCellResult`` objects and hands
them back to ``CourseComparisonRunner.write_results`` — the same code path the
live command uses — so tweaks to the tables or the discussion sections can be
verified against the real numbers for free.

    python scripts/regenerate_course_comparison_readme.py [output_dir]

Mirrors ``scripts/generate_experiment_results.py``: no Docker, no API key.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from apr_framework.core.models import BugIdentifier  # noqa: E402
from apr_framework.evaluation.course_comparison_runner import (  # noqa: E402
    CourseCellResult,
    CourseComparisonRunner,
    _summarise_assessment_from_patches,
    _summarise_retrieval_from_patches,
    _summarise_similarity_from_patches,
)

DEFAULT_OUTPUT_DIR = Path("experiment_results/course_comparison")


def rescore_cell_from_run_artifacts(
    cell: CourseCellResult, artifacts_dir: Path
) -> CourseCellResult:
    """Recompute a cell's assessment/similarity/retrieval figures from its artifacts.

    ``repair_results.json`` is the single source of truth for per-patch data, so
    re-deriving from it keeps a regenerated report correct even when the summary
    schema in ``results.json`` predates a newly added field.
    """
    if not cell.run_dir:
        return cell
    repair_results_path = (
        artifacts_dir / Path(cell.run_dir).name / "repair_results.json"
    )
    if not repair_results_path.exists():
        return cell

    payload = json.loads(repair_results_path.read_text(encoding="utf-8"))
    assessment = _summarise_assessment_from_patches(
        payload.get("assessed_plausible_patches") or []
    )
    # Every candidate, not just the plausible ones — see the runner's note: an
    # approach whose patches all failed the tests still generated edits, and how
    # close they came is what the graded metric exists to report.
    similarity = _summarise_similarity_from_patches(payload.get("all_results") or [])
    retrieval = _summarise_retrieval_from_patches(payload.get("all_results") or [])

    cell.assessed_patch_count = assessment.assessed_patch_count
    cell.best_quality_score = assessment.best_quality_score
    cell.mean_quality_score = assessment.mean_quality_score
    cell.rank_of_first_correct_by_assessment = assessment.rank_of_first_correct
    cell.is_top_assessed_patch_correct = assessment.is_top_patch_correct
    cell.quality_score_of_first_correct = assessment.quality_score_of_first_correct
    cell.best_context_similarity_score = similarity.best_score
    cell.mean_context_similarity_score = similarity.mean_score
    cell.similarity_band_of_best = similarity.band_of_best
    cell.retrieval_step_total = retrieval.step_total
    cell.retrieval_tool_call_counts = retrieval.tool_call_counts
    cell.patches_with_retrieval_count = retrieval.patches_with_retrieval_count
    return cell


def load_cells_from_results_json(
    results_path: Path,
) -> tuple[list[CourseCellResult], dict]:
    """Rebuild the cell list a report was rendered from, plus the run's header."""
    payload = json.loads(results_path.read_text(encoding="utf-8"))
    cells: list[CourseCellResult] = []
    for row in payload.get("results", []):
        bug_payload = row["bug"]
        cells.append(
            CourseCellResult(
                bug=BugIdentifier(
                    benchmark=bug_payload.get("benchmark", "bugsinpy"),
                    project=bug_payload["project"],
                    bug_id=bug_payload["bug_id"],
                ),
                approach_label=row["approach"],
                fl_mode=row["fl_mode"],
                fl_backend=row["fl_backend"],
                status=row["status"],
                llm_query_count=row.get("llm_query_count"),
                total_candidates_generated=row.get("total_candidates_generated", 0),
                candidates_validated=row.get("candidates_validated", 0),
                plausible_count=row.get("plausible_count", 0),
                correct_count=row.get("correct_count", 0),
                time_to_first_plausible_seconds=row.get(
                    "time_to_first_plausible_seconds"
                ),
                total_wall_clock_seconds=row.get("total_wall_clock_seconds", 0.0),
                generation_rank_of_first_correct=row.get(
                    "generation_rank_of_first_correct"
                ),
                ranked_rank_of_first_correct=row.get("ranked_rank_of_first_correct"),
                assessment_query_count=row.get("assessment_query_count"),
                assessed_patch_count=row.get("assessed_patch_count", 0),
                best_quality_score=row.get("best_quality_score"),
                mean_quality_score=row.get("mean_quality_score"),
                rank_of_first_correct_by_assessment=row.get(
                    "rank_of_first_correct_by_assessment"
                ),
                is_top_assessed_patch_correct=row.get(
                    "is_top_assessed_patch_correct", False
                ),
                quality_score_of_first_correct=row.get(
                    "quality_score_of_first_correct"
                ),
                best_context_similarity_score=row.get("best_context_similarity_score"),
                mean_context_similarity_score=row.get("mean_context_similarity_score"),
                similarity_band_of_best=row.get("similarity_band_of_best"),
                retrieval_step_total=row.get("retrieval_step_total", 0),
                retrieval_tool_call_counts=row.get("retrieval_tool_call_counts", {}),
                patches_with_retrieval_count=row.get("patches_with_retrieval_count", 0),
                fl_location_count=row.get("fl_location_count"),
                fl_files_shown=row.get("fl_files_shown", []),
                run_dir=row.get("run_dir"),
                error=row.get("error"),
            )
        )
    return cells, payload


def main() -> int:
    output_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUTPUT_DIR
    results_path = output_dir / "results.json"
    if not results_path.exists():
        print(f"No results.json at {results_path}", file=sys.stderr)
        return 1

    cells, payload = load_cells_from_results_json(results_path)
    artifacts_dir = output_dir / "run_artifacts"
    cells = [rescore_cell_from_run_artifacts(cell, artifacts_dir) for cell in cells]
    runner = CourseComparisonRunner(
        project_root=Path.cwd(),
        runs_dir=Path("runs"),
        ranker=None,
        repair_config_data={"model": payload.get("model", "?")},
        budget=payload.get("budget", 0),
        stop_on_first=False,
    )
    readme_path = runner.write_results(cells, output_dir)
    print(f"Rebuilt {readme_path} from {len(cells)} cell(s) in {results_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
