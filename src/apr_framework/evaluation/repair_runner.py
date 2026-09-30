"""Patch-validation evaluation runner (A-3, T-2).

``RepairEvaluationRunner`` implements the :class:`EvaluationRunner` ABC and turns a
repair algorithm into a proper *patch-validation pipeline*:

  1. drive the shared generate-and-validate loop (plausibility judgment),
  2. compare each plausible patch to the developer fix (correctness judgment), and
  3. record and persist the required metrics — total candidates generated,
     plausible count, correct count, time-to-first-plausible, and total wall-clock
     time — into the structured ``repair_results.json`` output.

"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from apr_framework.benchmarks.base import BenchmarkAdapter
from apr_framework.core.exceptions import BenchmarkError
from apr_framework.core.models import (
    BugIdentifier,
    CheckoutResult,
    EvaluationResult,
    LocalizationResult,
    RepairAttemptResult,
    RepairRunMetrics,
    RepairStatus,
)
from apr_framework.evaluation.base import EvaluationRunner
from apr_framework.evaluation.run_writer import RunWriter
from apr_framework.localization.base import FaultLocalizer
from apr_framework.repair.base import RepairAlgorithm
from apr_framework.repair.correctness import (
    context_similarity_score,
    describe_similarity_score,
    is_correct_patch,
)
from apr_framework.repair.assessment.base import PatchAssessor
from apr_framework.repair.ranking.base import PatchRanker
from apr_framework.repair.run_loop import LoopOutcome

logger = logging.getLogger(__name__)


@dataclass
class _BugRunResult:
    """Internal bundle for one bug's repair run, before serialization."""

    bug: BugIdentifier
    outcome: LoopOutcome
    metrics: RepairRunMetrics
    status: RepairStatus
    started_at: datetime
    finished_at: datetime
    ranked_plausible_results: list[RepairAttemptResult] = field(default_factory=list)
    assessed_plausible_results: list[RepairAttemptResult] = field(default_factory=list)


class RepairEvaluationRunner(EvaluationRunner):
    """Run the full patch-validation pipeline for one or more bugs.

    Constructor Args:
        project_root:  Repository root; used to resolve checkouts and the runs dir.
        runs_dir:      Output directory for run artifacts (default: ``runs``).
        budget:        Max patch validations per bug (loop control).
        stop_on_first: Stop validating a bug after the first plausible patch.
        config_data:   Optional configuration snapshot embedded in each per-bug
                       result (and written to ``config.json`` when this runner owns
                       the run directory); lets the CLI record the exact flags used.
        writer:        Optional pre-created RunWriter to reuse. When provided, the
                       caller owns ``config.json`` and ``execution.log`` (e.g. the
                       CLI logs localization there before repair); otherwise the
                       runner creates a fresh run directory and writes config.json.
        score_similarity: When True, also score each plausible patch's graded
                       closeness to the developer fix (0.0-1.0, see
                       ``repair/correctness.py``) and record it alongside the
                       unchanged exact-diff ``is_correct`` verdict. Off by default —
                       when disabled, output is identical to a build that never had
                       this metric.
    """

    def __init__(
        self,
        project_root: Path,
        runs_dir: Path | str = "runs",
        budget: int = 200,
        stop_on_first: bool = False,
        config_data: dict[str, Any] | None = None,
        writer: RunWriter | None = None,
        ranker: PatchRanker | None = None,
        assessor: PatchAssessor | None = None,
        localization_result: LocalizationResult | None = None,
        score_similarity: bool = False,
    ) -> None:
        self._project_root = Path(project_root)
        self._runs_dir = self._resolve_runs_dir(runs_dir)
        self._budget = budget
        self._stop_on_first = stop_on_first
        self._config_data = config_data or {}
        self._writer = writer
        self._ranker = ranker
        self._assessor = assessor
        self._localization_result = localization_result
        self._score_similarity = score_similarity
        self._last_run_dir: Path | None = None

    @property
    def name(self) -> str:
        return "repair-evaluation-runner"

    @property
    def last_run_dir(self) -> Path | None:
        """Directory created for the most recent run, or None before `run`."""
        return self._last_run_dir

    # ------------------------------------------------------------------
    # EvaluationRunner ABC
    # ------------------------------------------------------------------

    def run(
        self,
        bugs: list[BugIdentifier],
        benchmark: BenchmarkAdapter,
        repair: RepairAlgorithm,
        localizer: FaultLocalizer | None = None,
    ) -> list[EvaluationResult]:
        """Validate generated patches for each bug and persist metrics.

        The repair algorithm is expected to already carry its localization input
        (the CLI builds it that way); ``localizer`` is accepted for ABC
        compatibility but not required here.

        Returns:
            One EvaluationResult per bug, each carrying its RepairRunMetrics and
            the run directory.
        """
        if self._writer is not None:
            writer = self._writer
        else:
            writer = RunWriter.create(self._runs_dir)
            writer.write_json("config.json", {"runner": self.name, **self._config_data})
        self._last_run_dir = writer.run_dir
        writer.log(f"Started {self.name} for {len(bugs)} bug(s)")

        bug_run_results: list[_BugRunResult] = []

        # Run each bug in the `bugs` and store the BugRunResult's
        for bug in bugs:
            writer.log(f"Repairing {bug.project}#{bug.bug_id}")
            bug_run_results.append(self._run_one_bug(writer, bug, benchmark, repair))

        self._persist(writer, bug_run_results)

        return [
            EvaluationResult(
                bug=bug_result.bug,
                status=bug_result.status.value,
                started_at=bug_result.started_at,
                finished_at=bug_result.finished_at,
                metrics=bug_result.metrics,
                run_dir=str(writer.run_dir),
                assessed_plausible_results=(
                    bug_result.assessed_plausible_results
                    if self._assessor is not None
                    else None
                ),
                # Every candidate, not just the plausible ones: a run whose patches
                # all failed the tests still has near-misses worth showing.
                similarity_scored_results=(
                    bug_result.outcome.all_results if self._score_similarity else None
                ),
            )
            for bug_result in bug_run_results
        ]

    # ------------------------------------------------------------------
    # Per-bug pipeline
    # ------------------------------------------------------------------

    def _run_one_bug(
        self,
        writer: RunWriter,
        bug: BugIdentifier,
        benchmark: BenchmarkAdapter,
        repair: RepairAlgorithm,
    ) -> _BugRunResult:
        started_at = datetime.now(timezone.utc)
        checkout = self._resolve_checkout(bug, benchmark)

        # Plausibility —> the algorithm's budget-bounded loop (with timing/counts).
        # Default backends delegate to the shared generate-and-validate loop; the
        # iterative LLM backend overrides repair_loop for multi-turn feedback.
        outcome = repair.repair_loop(
            bug,
            checkout,
            budget=self._budget,
            stop_on_first=self._stop_on_first,
        )
        self._log_retrieval_traces(writer, outcome.all_results)

        # Correctness — compare each plausible patch to the developer fix.
        reference = self._reference_patch(benchmark, bug)
        if reference is None:
            writer.log(
                f"No reference fix (bug_patch.txt) found for {bug.project}#{bug.bug_id}; "
                "correctness cannot be assessed."
            )

        # Metric 2 (context similarity) — graded closeness to the developer fix,
        # recorded alongside the exact-diff verdict without altering it. Scored over
        # *every* generated candidate, not just the plausible ones: an approach whose
        # patches all fail the test suite would otherwise report no similarity at all,
        # which reads as "not measured" and makes it incomparable to an approach that
        # got something past the tests. Grading its near-misses is the whole point of
        # a graded metric. Opt-in only: skipped unless --similarity-score was passed.
        if self._score_similarity and reference is not None:
            for attempt_result in outcome.all_results:
                if attempt_result.patch is None:
                    continue
                similarity_score = context_similarity_score(
                    attempt_result.patch, reference
                )
                attempt_result.patch.metadata["context_similarity_score"] = (
                    similarity_score
                )
                attempt_result.patch.metadata["similarity_band"] = (
                    describe_similarity_score(similarity_score)
                )

        correct_count = 0
        for result in outcome.plausible_results:
            if result.patch is None or reference is None:
                continue
            # Metric 1 (exact diff match) — unchanged.
            if is_correct_patch(result.patch, reference):
                result.status = RepairStatus.CORRECT
                result.validation_summary += " Matches the developer fix (correct)."
                correct_count += 1

        # Ranking — reorder plausible patches by the configured heuristic (Task 4).
        if self._ranker is not None:
            ranked_plausible_results = self._ranker.rank(
                outcome.plausible_results, self._localization_result
            )
            rank_of_first_correct: int | None = None
            for rank_position, attempt_result in enumerate(
                ranked_plausible_results, start=1
            ):
                if attempt_result.status == RepairStatus.CORRECT:
                    rank_of_first_correct = rank_position
                    break
        else:
            ranked_plausible_results = outcome.plausible_results
            rank_of_first_correct = None

        if self._assessor is not None:
            assessed_plausible_results = self._assessor.assess(
                outcome.plausible_results,
                checkout,
                self._localization_result,
            )
            assessment_query_count = self._assessor.llm_query_count()
        else:
            assessed_plausible_results = []
            assessment_query_count = None

        metrics = RepairRunMetrics(
            total_candidates_generated=outcome.total_candidates_generated,
            candidates_validated=len(outcome.all_results),
            plausible_count=len(outcome.plausible_results),
            correct_count=correct_count,
            time_to_first_plausible_seconds=outcome.time_to_first_plausible_seconds,
            total_wall_clock_seconds=outcome.total_wall_clock_seconds,
            rank_of_first_correct=rank_of_first_correct,
            llm_query_count=repair.llm_query_count(),
            assessment_query_count=assessment_query_count,
        )

        # Overall bug status: correct > plausible > (failed/no_patch from the loop).
        if correct_count > 0:
            status = RepairStatus.CORRECT
        else:
            status = outcome.summary.status

        finished_at = datetime.now(timezone.utc)
        writer.log(
            f"Finished {bug.project}#{bug.bug_id}: status={status.value}, "
            f"generated={metrics.total_candidates_generated}, "
            f"validated={metrics.candidates_validated}, "
            f"plausible={metrics.plausible_count}, correct={metrics.correct_count}, "
            f"ttfp={metrics.time_to_first_plausible_seconds}, "
            f"wall_clock={metrics.total_wall_clock_seconds:.1f}s"
            + (
                f", rank_of_first_correct={rank_of_first_correct}"
                if self._ranker is not None
                else ""
            )
            + (
                f", assessment_llm_calls={assessment_query_count}"
                if self._assessor is not None
                else ""
            )
        )

        return _BugRunResult(
            bug=bug,
            outcome=outcome,
            metrics=metrics,
            status=status,
            started_at=started_at,
            finished_at=finished_at,
            ranked_plausible_results=ranked_plausible_results,
            assessed_plausible_results=assessed_plausible_results,
        )

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _persist(self, writer: RunWriter, results: list[_BugRunResult]) -> None:
        bug_payloads = [
            self._serialise_bug(writer, bug_result) for bug_result in results
        ]

        # The repair CLI runs one bug at a time -> keep the long-standing single-bug
        # repair_results.json schema (consumed by ranking/evaluation in later
        # tasks). For multiple bugs, wrap them in a "bugs" list.
        if len(bug_payloads) == 1:
            payload = {"run_id": writer.run_dir.name, **bug_payloads[0]}
        else:
            payload = {"run_id": writer.run_dir.name, "bugs": bug_payloads}

        writer.write_json("repair_results.json", payload)

    def _serialise_bug(
        self, writer: RunWriter, bug_result: _BugRunResult
    ) -> dict[str, Any]:
        plausible_results = bug_result.outcome.plausible_results
        self._write_patch_artifacts(writer, plausible_results)

        ranked_plausible_patches = None
        if self._ranker is not None:
            ranked_plausible_patches = [
                {
                    **self._serialise_result(attempt_result),
                    "rank_position": rank_position,
                }
                for rank_position, attempt_result in enumerate(
                    bug_result.ranked_plausible_results, start=1
                )
            ]

        assessed_plausible_patches = None
        if self._assessor is not None:
            assessed_plausible_patches = [
                {
                    **self._serialise_result(attempt_result),
                    "rank_position": rank_position,
                }
                for rank_position, attempt_result in enumerate(
                    bug_result.assessed_plausible_results, start=1
                )
            ]

        bug_payload = {
            "project": bug_result.bug.project,
            "bug_id": bug_result.bug.bug_id,
            # Hoisted from config so the FL mode used is recorded at the top level
            # of every result file, not only nested under "config".
            "fl_mode": self._config_data.get("fl_mode"),
            "fl_backend": self._config_data.get("fl_backend"),
            "status": bug_result.status.value,
            "validation_summary": bug_result.outcome.summary.validation_summary,
            "started_at": bug_result.started_at.isoformat(),
            "finished_at": bug_result.finished_at.isoformat(),
            "elapsed_seconds": bug_result.metrics.total_wall_clock_seconds,
            "config": self._config_data,
            "candidates_validated": bug_result.metrics.candidates_validated,
            "plausible_count": bug_result.metrics.plausible_count,
            "correct_count": bug_result.metrics.correct_count,
            "metrics": {
                "total_candidates_generated": bug_result.metrics.total_candidates_generated,
                "candidates_validated": bug_result.metrics.candidates_validated,
                "plausible_count": bug_result.metrics.plausible_count,
                "correct_count": bug_result.metrics.correct_count,
                "time_to_first_plausible_seconds": bug_result.metrics.time_to_first_plausible_seconds,
                "total_wall_clock_seconds": bug_result.metrics.total_wall_clock_seconds,
                "rank_of_first_correct": bug_result.metrics.rank_of_first_correct,
            },
            "plausible_patches": [
                self._serialise_result(attempt_result)
                for attempt_result in plausible_results
            ],
            "ranked_plausible_patches": ranked_plausible_patches,
            "rank_of_first_correct": bug_result.metrics.rank_of_first_correct,
            "all_results": [
                self._serialise_result(attempt_result)
                for attempt_result in bug_result.outcome.all_results
            ],
        }

        # LLM backends report an API-call count; template runs report None, in which
        # case the key is omitted so their result files are unchanged.
        if bug_result.metrics.llm_query_count is not None:
            bug_payload["metrics"]["llm_query_count"] = (
                bug_result.metrics.llm_query_count
            )

        if self._assessor is not None:
            bug_payload["assessed_plausible_patches"] = assessed_plausible_patches
            if bug_result.metrics.assessment_query_count is not None:
                bug_payload["metrics"]["assessment_query_count"] = (
                    bug_result.metrics.assessment_query_count
                )

        return bug_payload

    def _serialise_result(self, result: RepairAttemptResult) -> dict[str, Any]:
        patch = result.patch
        metadata = patch.metadata if patch else {}
        payload = {
            "patch_id": patch.patch_id if patch else None,
            "status": result.status.value,
            "is_correct": result.status == RepairStatus.CORRECT,
            "summary": patch.summary if patch else None,
            "validation_summary": result.validation_summary,
            "diff_text": patch.diff_text if patch else None,
            "metadata": {
                k: v
                for k, v in metadata.items()
                # omit large blobs and the similarity fields (surfaced above as
                # top-level keys when enabled): full patched source, raw pytest
                # output
                if k
                not in (
                    "patched_source",
                    "raw_test_output",
                    "retrieval_trace",
                    "context_similarity_score",
                    "similarity_band",
                )
            },
        }
        if self._score_similarity:
            # Graded closeness to the developer fix (0.0-1.0) plus a human-readable
            # band, carried by every generated candidate — plausible or not.
            # Independent of the is_correct verdict. Omitted entirely (not just
            # null) unless --similarity-score was passed.
            payload["context_similarity_score"] = metadata.get(
                "context_similarity_score"
            )
            payload["similarity_band"] = metadata.get("similarity_band")
        retrieval_trace = metadata.get("retrieval_trace")
        if retrieval_trace is not None:
            payload["retrieval"] = self._serialise_retrieval_trace(retrieval_trace)
        return payload

    @staticmethod
    def _serialise_retrieval_trace(retrieval_trace: Any) -> dict[str, Any]:
        steps = list(getattr(retrieval_trace, "steps", []))
        return {
            "steps": [
                {
                    "tool_name": step.tool_name,
                    "argument": step.argument,
                    "result_summary": step.result_summary,
                }
                for step in steps
            ],
            "step_count": getattr(retrieval_trace, "step_count", len(steps)),
            "stop_reason": getattr(retrieval_trace, "stop_reason", ""),
        }

    @staticmethod
    def _write_patch_artifacts(
        writer: RunWriter, plausible_results: list[RepairAttemptResult]
    ) -> None:
        if not plausible_results:
            return
        patches_dir = writer.run_dir / "patches"
        patches_dir.mkdir(exist_ok=True)
        for result in plausible_results:
            patch = result.patch
            if patch is None:
                continue
            (patches_dir / f"{patch.patch_id}.diff").write_text(
                patch.diff_text, encoding="utf-8"
            )
            patched_source = patch.metadata.get("patched_source")
            if patched_source:
                (patches_dir / f"{patch.patch_id}.patched.py").write_text(
                    patched_source, encoding="utf-8"
                )
        writer.log(
            f"Wrote {len(plausible_results)} plausible patch artifact(s) to {patches_dir}"
        )

    @staticmethod
    def _log_retrieval_traces(
        writer: RunWriter, attempt_results: list[RepairAttemptResult]
    ) -> None:
        for attempt_result in attempt_results:
            patch = attempt_result.patch
            if patch is None:
                continue
            retrieval_trace = patch.metadata.get("retrieval_trace")
            if retrieval_trace is None:
                continue
            tool_calls = [
                f'{step.tool_name}("{step.argument}")' for step in retrieval_trace.steps
            ]
            tool_summary = ", ".join(tool_calls) if tool_calls else "none"
            writer.log(
                f"RETRIEVE trace for {patch.patch_id}: "
                f"step_count={retrieval_trace.step_count}, "
                f"stop_reason={retrieval_trace.stop_reason}, tools={tool_summary}"
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve_checkout(
        self, bug: BugIdentifier, benchmark: BenchmarkAdapter
    ) -> CheckoutResult:
        """Locate the existing checked-out + compiled worktree for the bug.

        Repair requires a prior ``checkout``/``compile`` (or ``test``); this runner
        does not re-check-out. Raises if the worktree is missing.
        """
        canonical = (
            benchmark.resolve_project(bug.project)
            if hasattr(benchmark, "resolve_project")
            else bug.project
        )
        worktree = (
            self._project_root
            / ".workspace"
            / "bugsinpy"
            / f"{canonical}_{bug.bug_id}"
            / canonical
        )
        if not worktree.exists():
            raise BenchmarkError(
                f"No checkout found at {worktree}. Run "
                f"`python -m apr_framework bugsinpy checkout {bug.project} {bug.bug_id}` "
                "first."
            )
        return CheckoutResult(
            bug=BugIdentifier(
                benchmark="bugsinpy", project=canonical, bug_id=bug.bug_id
            ),
            worktree=worktree,
            success=True,
            prepared=True,
        )

    @staticmethod
    def _reference_patch(benchmark: BenchmarkAdapter, bug: BugIdentifier) -> str | None:
        getter = getattr(benchmark, "get_reference_patch", None)
        if getter is None:
            return None
        return getter(bug)

    def _resolve_runs_dir(self, runs_dir: Path | str) -> Path:
        path = Path(runs_dir)
        return path if path.is_absolute() else self._project_root / path
