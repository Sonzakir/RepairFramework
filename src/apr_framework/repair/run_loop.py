"""Shared generate-and-validate loop for the patch-validation pipeline.

This is the single source of truth for the budget-bounded validation loop. It is
written against the ``RepairAlgorithm`` ABC only (``generate_patches`` +
``validate_patch``), so it works for any repair backend — the template algorithm
today, an LLM backend tomorrow — and can be driven both by an algorithm's own
``repair()`` convenience method and by the ``RepairEvaluationRunner``.

It also captures the timing/counting metrics the Task 2 validation pipeline must
report: total candidates generated, candidates validated, and time to the first
plausible patch.
"""

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from apr_framework.core.models import (
    BugIdentifier,
    CheckoutResult,
    RepairAttemptResult,
    RepairStatus,
)
from apr_framework.repair.base import RepairAlgorithm

logger = logging.getLogger(__name__)


@dataclass
class LoopOutcome:
    """Result of one budget-bounded validation loop over a bug's candidates.

    Fields:
        summary:                          A single representative result — the first
            plausible patch, else FAILED (candidates existed, none passed), else
            NO_PATCH (nothing was generated).
        all_results:                      Per-candidate validation results, in
            validation order.
        total_candidates_generated:       Candidates produced before budget capping.
        time_to_first_plausible_seconds:  Seconds from loop start to first plausible
            patch, or None if none was found.
        total_wall_clock_seconds:         Seconds for the whole loop (generation +
            validation).
    """

    summary: RepairAttemptResult
    all_results: list[RepairAttemptResult] = field(default_factory=list)
    total_candidates_generated: int = 0
    time_to_first_plausible_seconds: float | None = None
    total_wall_clock_seconds: float = 0.0

    @property
    def plausible_results(self) -> list[RepairAttemptResult]:
        return [
            attempt_result
            for attempt_result in self.all_results
            if attempt_result.status in (RepairStatus.PLAUSIBLE, RepairStatus.CORRECT)
        ]


def build_loop_summary(
    bug: BugIdentifier,
    all_results: list[RepairAttemptResult],
    total_candidates_generated: int,
    budget: int,
    elapsed_seconds: float,
) -> RepairAttemptResult:
    """Build the single representative result for a finished validation loop.

    Mirrors the branching used at the end of ``run_validation_loop``: the first
    plausible result if any exist, else FAILED (candidates existed, none passed),
    else NO_PATCH (nothing was generated/validated). The iterative LLM loop calls
    this same helper so both loop shapes produce identically-formatted summaries.
    """
    if total_candidates_generated == 0:
        return RepairAttemptResult(
            bug=bug,
            patch=None,
            status=RepairStatus.NO_PATCH,
            validation_summary=(
                "No candidate patches were generated at the top suspicious locations."
            ),
        )

    validated_count = len(all_results)
    plausible_results = [
        result for result in all_results if result.status == RepairStatus.PLAUSIBLE
    ]

    if plausible_results:
        first_plausible_result = plausible_results[0]
        return RepairAttemptResult(
            bug=bug,
            patch=first_plausible_result.patch,
            status=RepairStatus.PLAUSIBLE,
            validation_summary=(
                f"Found {len(plausible_results)} plausible patch(es) out of "
                f"{validated_count} validated (budget: {budget}, "
                f"elapsed: {elapsed_seconds:.1f}s). "
                f"First plausible: {first_plausible_result.patch.patch_id if first_plausible_result.patch else '?'}."
            ),
        )

    return RepairAttemptResult(
        bug=bug,
        patch=None,
        status=RepairStatus.FAILED,
        validation_summary=(
            f"No plausible patch found. Validated {validated_count}/"
            f"{total_candidates_generated} candidates (budget: {budget}, "
            f"elapsed: {elapsed_seconds:.1f}s)."
        ),
    )


def run_validation_loop(
    repair: RepairAlgorithm,
    bug: BugIdentifier,
    checkout: CheckoutResult,
    *,
    budget: int,
    stop_on_first: bool,
) -> LoopOutcome:
    """Generate candidates, then validate them under a budget, recording metrics.

    Args:
        repair:        Repair algorithm (ABC methods only are used).
        bug:           Bug under repair.
        checkout:      Checked-out worktree to validate against.
        budget:        Maximum number of patch validations (test-suite runs).
        stop_on_first: Stop validating as soon as a plausible patch is found.

    Returns:
        A LoopOutcome with the summary result, per-candidate results, and metrics.
    """
    started_at = time.monotonic()

    # Generate all candidate patches
    candidates = repair.generate_patches(bug, checkout)
    total_generated = len(candidates)
    logger.info("Total candidates generated: %d", total_generated)

    # No patch generated
    if not candidates:
        elapsed = time.monotonic() - started_at
        return LoopOutcome(
            summary=RepairAttemptResult(
                bug=bug,
                patch=None,
                status=RepairStatus.NO_PATCH,
                validation_summary=(
                    "No mutation operators matched at the top suspicious locations."
                ),
            ),
            all_results=[],
            total_candidates_generated=0,
            time_to_first_plausible_seconds=None,
            total_wall_clock_seconds=elapsed,
        )

    budget_remaining = budget
    all_results: list[RepairAttemptResult] = []
    time_to_first_plausible: float | None = None

    for candidate in candidates:
        if budget_remaining <= 0:
            logger.info("Budget exhausted — stopping validation loop.")
            break

        operator_key = candidate.metadata.get("operator", "?")
        source_path_str = candidate.metadata.get("source_path", "?")
        target_line_str = candidate.metadata.get("target_line", "?")
        logger.info(
            "Validating %s (op=%s, file=%s:%s) — budget remaining: %d",
            candidate.patch_id,
            operator_key,
            Path(source_path_str).name if source_path_str != "?" else source_path_str,
            target_line_str,
            budget_remaining,
        )

        # Execute the test suite to assess whether candidate patch is plausible
        try:
            result = repair.validate_patch(bug, checkout, candidate)
        except Exception as exc:  # noqa: BLE001 — never abort the loop on one patch
            logger.error("Unexpected error validating %s: %s", candidate.patch_id, exc)
            result = RepairAttemptResult(
                bug=bug,
                patch=candidate,
                status=RepairStatus.FAILED,
                validation_summary=f"Unexpected validation error: {exc}",
            )

        all_results.append(result)
        budget_remaining -= 1

        if result.status == RepairStatus.PLAUSIBLE:
            if time_to_first_plausible is None:
                time_to_first_plausible = time.monotonic() - started_at
            logger.info(
                "Plausible patch: %s",
                result.patch.patch_id if result.patch else "?",
            )
            if stop_on_first:
                logger.info(
                    "stop_on_first=True — stopping after first plausible patch."
                )
                break

    elapsed = time.monotonic() - started_at
    validated_count = len(all_results)
    plausible_results = [
        result for result in all_results if result.status == RepairStatus.PLAUSIBLE
    ]

    logger.info(
        "Repair loop finished in %.1fs: %d validated, %d plausible",
        elapsed,
        validated_count,
        len(plausible_results),
    )

    summary = build_loop_summary(bug, all_results, total_generated, budget, elapsed)

    return LoopOutcome(
        summary=summary,
        all_results=all_results,
        total_candidates_generated=total_generated,
        time_to_first_plausible_seconds=time_to_first_plausible,
        total_wall_clock_seconds=elapsed,
    )
