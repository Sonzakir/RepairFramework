"""Drive the LLM repair pipeline across a matrix of bugs x repair-variant x
fault-localization mode, and aggregate the evaluation metrics.

This is the LLM counterpart of ``RepairComparisonRunner``. Where that runner
compares FL modes for the *template* technique, this one adds a third axis — the
LLM repair *variant* (single-shot / context-enriched / iterative) — and reports
an LLM-specific figure the template runner does not: the number of LLM queries
made per cell.

Each cell is one ``(bug, variant, fl_mode)`` triple. The heavy lifting
(generate-validate-correctness-rank loop, per-run ``repair_results.json`` +
``execution.log`` artifacts) is delegated to ``RepairEvaluationRunner`` exactly
as in ``RepairComparisonRunner`` — every cell gets its own ``run_NNN`` directory.
This runner only orchestrates the matrix, flushes an incremental ``results.json``
after every cell (so a long API run survives a mid-matrix failure), and renders a
human-readable ``README.md`` with per-bug tables, an aggregate table, and three
analyses: the effect of the iterative loop, the effect of context enrichment, and
a side-by-side comparison with the template technique.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
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

# A repair-algorithm factory builds a fresh LLM repair algorithm bound to one
# cell's localization result and repair variant. A fresh algorithm (and a fresh
# LLM client) per cell keeps the per-cell query counter isolated.
RepairAlgorithmFactory = Callable[[LocalizationResult, str], RepairAlgorithm]


@dataclass
class LLMRepairCellResult:
    """Metrics for one (bug, variant, FL mode) cell of the comparison matrix."""

    bug: BugIdentifier
    variant_label: str
    fl_mode: str
    fl_backend: str
    status: str
    llm_query_count: int | None = None
    total_candidates_generated: int = 0
    candidates_validated: int = 0
    plausible_count: int = 0
    correct_count: int = 0
    time_to_first_plausible_seconds: float | None = None
    total_wall_clock_seconds: float = 0.0
    # Position (1-based) of the first correct patch in generation order.
    generation_rank_of_first_correct: int | None = None
    # Position (1-based) of the first correct patch after the ranker reorders.
    ranked_rank_of_first_correct: int | None = None
    run_dir: str | None = None
    error: str | None = None


class LLMRepairComparisonRunner:
    """Run the LLM repair pipeline for every (bug, variant, FL mode) triple."""

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
        variants: list[str],
        fl_modes: list[str],
        benchmark: BenchmarkAdapter,
        localization_provider: LocalizationProvider,
        repair_algorithm_factory: RepairAlgorithmFactory,
        output_dir: Path,
    ) -> list[LLMRepairCellResult]:
        """Execute the full ``bugs x variants x fl_modes`` matrix.

        ``results.json`` is flushed to *output_dir* after every cell so completed
        cells survive a mid-matrix failure on a long API run.
        """
        from apr_framework.evaluation.repair_runner import RepairEvaluationRunner
        from apr_framework.evaluation.run_writer import RunWriter

        output_dir.mkdir(parents=True, exist_ok=True)
        cells: list[LLMRepairCellResult] = []
        for bug in bugs:
            for variant_label in variants:
                for fl_mode in fl_modes:
                    print(
                        f"  [{bug.project} #{bug.bug_id}] variant={variant_label} "
                        f"FL={fl_mode} ...",
                        flush=True,
                    )
                    cell = self._run_one_cell(
                        bug,
                        variant_label,
                        fl_mode,
                        benchmark,
                        localization_provider,
                        repair_algorithm_factory,
                        RepairEvaluationRunner,
                        RunWriter,
                    )
                    cells.append(cell)
                    # Crash-safe flush after every completed cell.
                    self._write_json(cells, output_dir / "results.json")

        return cells

    def _run_one_cell(
        self,
        bug: BugIdentifier,
        variant_label: str,
        fl_mode: str,
        benchmark: BenchmarkAdapter,
        localization_provider: LocalizationProvider,
        repair_algorithm_factory: RepairAlgorithmFactory,
        repair_evaluation_runner_class: type,
        run_writer_class: type,
    ) -> LLMRepairCellResult:
        """Localize, repair, and read back the metrics for one matrix cell."""
        try:
            localization_result = localization_provider(bug, fl_mode)
        except Exception as localization_error:  # noqa: BLE001 — one bad cell must not abort the matrix
            print(f"    LOCALIZATION ERROR: {localization_error}", flush=True)
            return LLMRepairCellResult(
                bug=bug,
                variant_label=variant_label,
                fl_mode=fl_mode,
                fl_backend=self._backend_label(fl_mode),
                status="error",
                error=f"{type(localization_error).__name__}: {localization_error}",
            )

        writer = run_writer_class.create(self._runs_dir)
        cell_config_data = {
            **self._repair_config_data,
            "project": bug.project,
            "bug_id": bug.bug_id,
            "variant": variant_label,
            "fl_mode": fl_mode,
            "fl_backend": localization_result.backend,
            "ranker": self._ranker.name if self._ranker else "none",
        }
        writer.write_json(
            "config.json", {"runner": "llm-repair-comparison", **cell_config_data}
        )
        writer.log(
            f"LLM repair comparison cell: {bug.project}#{bug.bug_id} "
            f"variant={variant_label} fl_mode={fl_mode} "
            f"backend={localization_result.backend}"
        )

        try:
            repair_algorithm = repair_algorithm_factory(
                localization_result, variant_label
            )
            evaluation_runner = repair_evaluation_runner_class(
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
        except Exception as repair_error:  # noqa: BLE001
            print(f"    REPAIR ERROR: {repair_error}", flush=True)
            return LLMRepairCellResult(
                bug=bug,
                variant_label=variant_label,
                fl_mode=fl_mode,
                fl_backend=localization_result.backend,
                status="error",
                run_dir=str(writer.run_dir),
                error=f"{type(repair_error).__name__}: {repair_error}",
            )

        return self._cell_from_run_dir(
            bug, variant_label, fl_mode, localization_result.backend, writer.run_dir
        )

    # ------------------------------------------------------------------
    # Reading one cell's metrics back from the run it produced
    # ------------------------------------------------------------------

    def _cell_from_run_dir(
        self,
        bug: BugIdentifier,
        variant_label: str,
        fl_mode: str,
        fl_backend: str,
        run_dir: Path,
    ) -> LLMRepairCellResult:
        payload = json.loads(
            (run_dir / "repair_results.json").read_text(encoding="utf-8")
        )
        metrics = payload.get("metrics", {})
        plausible_patches = payload.get("plausible_patches", []) or []

        generation_rank_of_first_correct: int | None = None
        for position, patch_payload in enumerate(plausible_patches, start=1):
            if patch_payload.get("is_correct"):
                generation_rank_of_first_correct = position
                break

        return LLMRepairCellResult(
            bug=bug,
            variant_label=variant_label,
            fl_mode=fl_mode,
            fl_backend=fl_backend,
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

    def write_results(self, cells: list[LLMRepairCellResult], output_dir: Path) -> Path:
        """Write ``results.json`` and ``README.md`` to *output_dir*; return README path."""
        output_dir.mkdir(parents=True, exist_ok=True)
        self._write_json(cells, output_dir / "results.json")
        readme_path = output_dir / "README.md"
        readme_path.write_text(self._build_readme(cells), encoding="utf-8")
        return readme_path

    def copy_run_artifacts(
        self, cells: list[LLMRepairCellResult], output_dir: Path
    ) -> None:
        """Copy each cell's ``run_NNN`` directory into ``output_dir/run_artifacts``.

        Makes the committed report self-contained (logs + patch diffs preserved),
        mirroring ``experiment_results/repair/run_artifacts/``.
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

    def _write_json(self, cells: list[LLMRepairCellResult], path: Path) -> None:
        rows: list[dict[str, Any]] = []
        for cell in cells:
            rows.append(
                {
                    "bug": {
                        "benchmark": cell.bug.benchmark,
                        "project": cell.bug.project,
                        "bug_id": cell.bug.bug_id,
                    },
                    "variant": cell.variant_label,
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

    # ------------------------------------------------------------------
    # Report rendering
    # ------------------------------------------------------------------

    def _build_readme(self, cells: list[LLMRepairCellResult]) -> str:
        bugs = _unique_ordered([cell.bug for cell in cells])
        variants = _unique_ordered([cell.variant_label for cell in cells])
        fl_modes = _unique_ordered([cell.fl_mode for cell in cells])
        index: dict[tuple[BugIdentifier, str, str], LLMRepairCellResult] = {
            (cell.bug, cell.variant_label, cell.fl_mode): cell for cell in cells
        }

        lines: list[str] = []
        lines.append("# LLM Repair Evaluation Results (Assignment 4 — Task 5)\n")
        model_name = self._repair_config_data.get("model", "?")
        lines.append(
            "Full LLM repair pipeline run on "
            f"{len(bugs)} BugsInPy bug(s), each under "
            f"{len(variants)} variant(s) × {len(fl_modes)} FL mode(s) "
            f"= {len(bugs) * len(variants) * len(fl_modes)} cells.\n"
        )
        lines.append(
            f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} · "
            f"model: `{model_name}` · per-validation budget: {self._budget}\n"
        )
        lines.append(
            "Variants (isolated axes): **single-shot** = bare prompt (no enrichment), "
            "**context-enriched** = failing-test source + error traceback added, "
            "**iterative** = multi-turn test-failure feedback loop.\n"
        )

        # --- Per-bug tables ---
        for bug in bugs:
            lines.append(f"\n## {bug.project} bug #{bug.bug_id}\n")
            lines.append(
                "| Variant | FL mode | Queries | Generated | Plausible | Correct "
                "| 1st plausible (s) | Total (s) | Correct rank (gen → ranked) | Outcome |"
            )
            lines.append("|---|---|---|---|---|---|---|---|---|---|")
            for variant_label in variants:
                for fl_mode in fl_modes:
                    cell = index.get((bug, variant_label, fl_mode))
                    if cell is None:
                        continue
                    lines.append(_cell_row(cell))

        # --- Aggregate table ---
        lines.append("\n## Aggregate (per variant × FL mode)\n")
        lines.append(
            "Totals across all evaluated bugs. *Number of Distinct Bugs with Correct Patch* "
            "counts bugs with at least one correct patch in that cell type.\n"
        )
        lines.append(
            "| Variant | FL mode | Bugs | Queries | Generated | Plausible | Correct | Number of Distinct Bugs with Correct Patch |"
        )
        lines.append("|---|---|---|---|---|---|---|---|")
        for variant_label in variants:
            for fl_mode in fl_modes:
                group_cells = [index.get((bug, variant_label, fl_mode)) for bug in bugs]
                group_cells = [cell for cell in group_cells if cell is not None]
                query_total = sum((cell.llm_query_count or 0) for cell in group_cells)
                generated_total = sum(
                    cell.total_candidates_generated for cell in group_cells
                )
                plausible_total = sum(cell.plausible_count for cell in group_cells)
                correct_total = sum(cell.correct_count for cell in group_cells)
                bugs_repaired = sum(1 for cell in group_cells if cell.correct_count > 0)
                lines.append(
                    f"| {variant_label} | {fl_mode} | {len(group_cells)} | "
                    f"{query_total} | {generated_total} | {plausible_total} | "
                    f"{correct_total} | {bugs_repaired} |"
                )

        # --- Analyses ---
        lines.append("\n## Analysis\n")
        lines.append(self._build_iterative_effect(bugs, fl_modes, index, variants))
        lines.append("")
        lines.append(self._build_enrichment_effect(bugs, fl_modes, index, variants))
        lines.append("")
        lines.append(
            self._build_assignment3_comparison(bugs, variants, fl_modes, index)
        )

        return "\n".join(lines) + "\n"

    def _build_iterative_effect(
        self,
        bugs: list[BugIdentifier],
        fl_modes: list[str],
        index: dict[tuple[BugIdentifier, str, str], LLMRepairCellResult],
        variants: list[str],
    ) -> str:
        """Did the iterative loop recover patches single-shot missed?"""
        if "iterative" not in variants or "single-shot" not in variants:
            return (
                "**Effect of iterative repair.**  Not evaluated — the run did not "
                "include both the `single-shot` and `iterative` variants."
            )
        recoveries: list[str] = []
        for bug in bugs:
            for fl_mode in fl_modes:
                single_cell = index.get((bug, "single-shot", fl_mode))
                iterative_cell = index.get((bug, "iterative", fl_mode))
                if single_cell is None or iterative_cell is None:
                    continue
                gained_plausible = (
                    single_cell.plausible_count == 0
                    and iterative_cell.plausible_count > 0
                )
                gained_correct = (
                    single_cell.correct_count == 0 and iterative_cell.correct_count > 0
                )
                if gained_correct:
                    recoveries.append(
                        f"`{bug.project}#{bug.bug_id}` ({fl_mode} FL): "
                        "iterative reached a **correct** patch where single-shot did not"
                    )
                elif gained_plausible:
                    recoveries.append(
                        f"`{bug.project}#{bug.bug_id}` ({fl_mode} FL): "
                        "iterative reached a **plausible** patch where single-shot did not"
                    )
        if recoveries:
            return (
                "**Effect of iterative repair.**  The feedback loop recovered "
                "outcomes single-shot missed in: " + "; ".join(recoveries) + "."
            )
        return (
            "**Effect of iterative repair.**  On these bugs the iterative loop did "
            "not recover any plausible/correct patch that single-shot missed. The "
            "loop still costs extra LLM queries per location (see the Queries column), "
            "so its value here is bounded by whether the model can act on the "
            "test-failure feedback for the given fault."
        )

    def _build_enrichment_effect(
        self,
        bugs: list[BugIdentifier],
        fl_modes: list[str],
        index: dict[tuple[BugIdentifier, str, str], LLMRepairCellResult],
        variants: list[str],
    ) -> str:
        """Did context enrichment change the outcome vs the bare baseline?"""
        if "context-enriched" not in variants or "single-shot" not in variants:
            return (
                "**Effect of context enrichment.**  Not evaluated — the run did not "
                "include both the `single-shot` and `context-enriched` variants."
            )
        improvements: list[str] = []
        regressions: list[str] = []
        for bug in bugs:
            for fl_mode in fl_modes:
                single_cell = index.get((bug, "single-shot", fl_mode))
                enriched_cell = index.get((bug, "context-enriched", fl_mode))
                if single_cell is None or enriched_cell is None:
                    continue
                if (
                    enriched_cell.correct_count > single_cell.correct_count
                    or enriched_cell.plausible_count > single_cell.plausible_count
                ):
                    improvements.append(
                        f"`{bug.project}#{bug.bug_id}` ({fl_mode} FL): "
                        f"plausible {single_cell.plausible_count}→{enriched_cell.plausible_count}, "
                        f"correct {single_cell.correct_count}→{enriched_cell.correct_count}"
                    )
                elif (
                    enriched_cell.plausible_count < single_cell.plausible_count
                    or enriched_cell.correct_count < single_cell.correct_count
                ):
                    regressions.append(
                        f"`{bug.project}#{bug.bug_id}` ({fl_mode} FL): "
                        f"plausible {single_cell.plausible_count}→{enriched_cell.plausible_count}, "
                        f"correct {single_cell.correct_count}→{enriched_cell.correct_count}"
                    )
        paragraph = "**Effect of context enrichment.**  "
        if improvements:
            paragraph += (
                "Adding the failing test source + error traceback improved: "
                + "; ".join(improvements)
                + ". "
            )
        if regressions:
            paragraph += (
                "It did *not* help (fewer plausible/correct) in: "
                + "; ".join(regressions)
                + ". "
            )
        if not improvements and not regressions:
            paragraph += (
                "On these bugs enrichment left the plausible/correct counts unchanged "
                "relative to the bare prompt — the extra context neither unlocked nor "
                "blocked a fix, though it does change the prompt the model sees."
            )
        return paragraph

    def _build_assignment3_comparison(
        self,
        bugs: list[BugIdentifier],
        variants: list[str],
        fl_modes: list[str],
        index: dict[tuple[BugIdentifier, str, str], LLMRepairCellResult],
    ) -> str:
        """Compare the best LLM outcome per bug against the template technique."""
        template_correct = self._load_template_correct_by_bug()
        per_bug_lines: list[str] = []
        for bug in bugs:
            bug_cells = [
                index[(bug, variant_label, fl_mode)]
                for variant_label in variants
                for fl_mode in fl_modes
                if (bug, variant_label, fl_mode) in index
            ]
            llm_correct = any(cell.correct_count > 0 for cell in bug_cells)
            llm_plausible = any(cell.plausible_count > 0 for cell in bug_cells)
            best_llm = (
                "correct"
                if llm_correct
                else "plausible"
                if llm_plausible
                else "no plausible patch"
            )
            bug_key = f"{bug.project}#{bug.bug_id}"
            template_outcome = template_correct.get(bug_key)
            if template_outcome is None:
                template_text = "not in the Assignment-3 template results"
            elif template_outcome:
                template_text = "**correct** (template)"
            else:
                template_text = "no correct patch (template)"
            per_bug_lines.append(
                f"- `{bug_key}`: LLM best = {best_llm}; template = {template_text}."
            )

        header = (
            "**Comparison with Assignment 3 (template repair).**  Best LLM outcome "
            "across all variants/FL modes per bug, next to the template technique's "
            "result from `experiment_results/repair/results.json`:"
        )
        if not template_correct:
            header += (
                "\n\n_(Assignment-3 template results.json was not found; template "
                "column omitted.)_"
            )
        return header + "\n\n" + "\n".join(per_bug_lines)

    def _load_template_correct_by_bug(self) -> dict[str, bool]:
        """Map ``project#bug`` → whether template repair produced a correct patch.

        Reads the Assignment-3 comparison ``results.json`` if present; returns an
        empty mapping when it is missing so the report degrades gracefully.
        """
        template_results_path = (
            self._project_root / "experiment_results" / "repair" / "results.json"
        )
        if not template_results_path.exists():
            return {}
        try:
            payload = json.loads(template_results_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        correct_by_bug: dict[str, bool] = {}
        for row in payload.get("results", []):
            bug_info = row.get("bug", {})
            bug_key = f"{bug_info.get('project')}#{bug_info.get('bug_id')}"
            has_correct = row.get("correct_count", 0) > 0
            correct_by_bug[bug_key] = correct_by_bug.get(bug_key, False) or has_correct
        return correct_by_bug


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


def _cell_row(cell: LLMRepairCellResult) -> str:
    if cell.error:
        return (
            f"| {cell.variant_label} | {cell.fl_mode} | — | — | — | — | — | — | — | "
            f"ERROR: {_summarize_error(cell.error)} |"
        )
    queries = str(cell.llm_query_count) if cell.llm_query_count is not None else "—"
    time_to_first_plausible = (
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
        f"| {cell.variant_label} | {cell.fl_mode} | {queries} "
        f"| {cell.total_candidates_generated} | {cell.plausible_count} "
        f"| {cell.correct_count} | {time_to_first_plausible} "
        f"| {cell.total_wall_clock_seconds:.1f} | {generation_rank} → {ranked_rank} "
        f"| {cell.status} |"
    )


def _unique_ordered(items: list) -> list:
    seen: set = set()
    ordered: list = []
    for item in items:
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered
