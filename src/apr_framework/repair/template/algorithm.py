"""Template-based APR algorithm: SBFL/MBFL-guided AST mutation repair."""

import logging
from pathlib import Path

from apr_framework.benchmarks.bugsinpy import BugsInPyAdapter
from apr_framework.core.models import (
    BugIdentifier,
    CheckoutResult,
    LocalizationResult,
    PatchCandidate,
    RankedLocation,
    RepairAttemptResult,
    RepairStatus,
)
from apr_framework.repair.base import RepairAlgorithm
from apr_framework.repair.regression import RegressionContext, build_regression_context
from apr_framework.repair.template.config import TemplateRepairConfig
from apr_framework.repair.template.patch_generator import generate_patches
from apr_framework.repair.template.validator import validate_patch

logger = logging.getLogger(__name__)


class TemplateRepairAlgorithm(RepairAlgorithm):
    """SBFL/MBFL-guided template-based repair algorithm.

    Uses ranked suspicious locations from a prior localization run to select
    *where* to apply fix templates, and AST mutation operators to determine
    *what* to try at each location.

    Constructor Args:
        localization_result: Ranked locations from a FauxPy (or hybrid) run.
        adapter:             BugsInPyAdapter used to invoke the test suite.
        config:              Repair configuration (budget, operators, etc.).
    """

    def __init__(
        self,
        localization_result: LocalizationResult,
        adapter: BugsInPyAdapter,
        config: TemplateRepairConfig | None = None,
    ) -> None:
        self._localization_result = localization_result
        self._adapter = adapter
        self._config = config or TemplateRepairConfig()
        # Regression baseline is established once, lazily, on first validation
        # (it needs the checkout) and reused for every candidate.
        self._regression: RegressionContext | None = None

    @property
    def name(self) -> str:
        return "template-repair"

    # ------------------------------------------------------------------
    # RepairAlgorithm ABC
    # ------------------------------------------------------------------

    def generate_patches(
        self, bug: BugIdentifier, checkout: CheckoutResult
    ) -> list[PatchCandidate]:
        """Generate candidate patches for the top-N suspicious locations.

        Iterates over the top-N ranked locations from the localization result,
        resolves each to an absolute source path inside the checkout worktree,
        and applies all enabled mutation operators.  No test runs occur here.

        Args:
            bug:      Bug identifier.
            checkout: Checkout result containing the project worktree.

        Returns:
            All generated PatchCandidate objects (may be empty).
        """
        config = self._config
        locations = self._localization_result.ranked_locations[: config.top_n_locations]

        all_candidates: list[PatchCandidate] = []
        for location in locations:
            source_path = self._resolve_source_path(location, checkout)
            if source_path is None:
                logger.warning(
                    "Cannot resolve source path for location %s — skipping",
                    location.file_path,
                )
                continue

            target_line = location.line
            if target_line is None:
                logger.warning(
                    "Location %s has no line number — skipping", location.file_path
                )
                continue

            logger.info(
                "Generating patches for %s:%d (rank %d, score %s)",
                source_path,
                target_line,
                location.rank,
                location.score,
            )

            try:
                candidates = generate_patches(
                    source_path=source_path,
                    target_line=target_line,
                    operators=config.enabled_operators,
                    location=location,
                    bug=bug,
                )
            except Exception as exc:
                logger.error(
                    "Patch generation failed for %s:%d: %s",
                    source_path,
                    target_line,
                    exc,
                )
                continue

            logger.info(
                "Generated %d candidate(s) for %s:%d",
                len(candidates),
                source_path.name,
                target_line,
            )
            all_candidates.extend(candidates)

        return all_candidates

    def validate_patch(
        self, bug: BugIdentifier, checkout: CheckoutResult, patch: PatchCandidate
    ) -> RepairAttemptResult:
        """Validate a single patch candidate by applying it and running the test suite.

        Args:
            bug:      Bug identifier.
            checkout: Checkout result containing the target worktree.
            patch:    Candidate patch to validate.

        Returns:
            RepairAttemptResult with status PLAUSIBLE, FAILED, or NO_PATCH.
        """
        regression = self._regression_context(checkout)
        try:
            updated_patch, is_plausible = validate_patch(
                candidate=patch,
                checkout=checkout,
                adapter=self._adapter,
                config=self._config,
                regression=regression,
            )
        except Exception as exc:
            logger.error(
                "Validation raised an unexpected error for %s: %s",
                patch.patch_id,
                exc,
            )
            return RepairAttemptResult(
                bug=bug,
                patch=patch,
                status=RepairStatus.FAILED,
                validation_summary=f"Validation error: {exc}",
            )

        if is_plausible:
            logger.info("Plausible patch found: %s", updated_patch.patch_id)
            return RepairAttemptResult(
                bug=bug,
                patch=updated_patch,
                status=RepairStatus.PLAUSIBLE,
                validation_summary=f"All tests passed with patch {updated_patch.patch_id}.",
            )

        passed_count = updated_patch.metadata.get("test_passed_count", "?")
        failed_count = updated_patch.metadata.get("test_failed_count", "?")
        error_count = updated_patch.metadata.get("test_error_count", "?")
        return RepairAttemptResult(
            bug=bug,
            patch=updated_patch,
            status=RepairStatus.FAILED,
            validation_summary=(
                f"Patch {updated_patch.patch_id} did not fix all tests "
                f"(passed={passed_count}, failed={failed_count}, errors={error_count})."
            ),
        )

    # ------------------------------------------------------------------
    # Orchestration — full repair loop with budget
    # ------------------------------------------------------------------

    def repair(
        self, bug: BugIdentifier, checkout: CheckoutResult
    ) -> tuple[RepairAttemptResult, list[RepairAttemptResult]]:
        """Run the full repair loop with budget and optional early stopping.

        Thin convenience wrapper around :meth:`repair_loop` (the shared,
        backend-agnostic loop). The summary_result uses the first plausible patch
        (status=PLAUSIBLE), or RepairStatus.FAILED if candidates existed but none
        passed, or RepairStatus.NO_PATCH if no candidates were generated.

        For the full set of validation metrics (counts, time-to-first-plausible,
        correctness), use :class:`RepairEvaluationRunner`, which drives the same
        loop and additionally compares against the developer fix.

        Args:
            bug:      Bug identifier.
            checkout: Checkout result for the worktree to repair.

        Returns:
            (summary_result, all_validation_results)
        """
        config = self._config

        logger.info(
            "Starting template repair for %s#%d — budget=%d, top_n=%d, operators=%s",
            bug.project,
            bug.bug_id,
            config.budget,
            config.top_n_locations,
            config.enabled_operators,
        )

        outcome = self.repair_loop(
            bug,
            checkout,
            budget=config.budget,
            stop_on_first=config.stop_on_first,
        )
        return outcome.summary, outcome.all_results

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _regression_context(self, checkout: CheckoutResult) -> RegressionContext:
        """Return the regression baseline, establishing it once on first use.

        The baseline (failing-set of the whole test_file on the unpatched checkout)
        is identical for every candidate, so it is computed a single time and
        cached. Disabled configs short-circuit to an inert context.
        """
        if self._regression is None:
            self._regression = build_regression_context(
                self._adapter,
                checkout,
                enabled=self._config.regression_check,
                timeout=self._config.timeout_per_test,
            )
        return self._regression

    def _resolve_source_path(
        self, location: RankedLocation, checkout: CheckoutResult
    ) -> Path | None:
        """Resolve a RankedLocation's file_path to an absolute Path in the worktree.

        FauxPy outputs paths relative to the project root (the checkout worktree),
        sometimes prefixed with `./`.  This method strips the prefix and joins with
        the worktree to produce a concrete, verifiable Path.
        """
        file_path_str = location.file_path
        if not file_path_str:
            return None

        file_path = Path(file_path_str)

        # Absolute path — verify it sits under the worktree.
        if file_path.is_absolute():
            if file_path.exists():
                return file_path
            return None

        # Relative path — join with worktree.
        resolved = (checkout.worktree / file_path).resolve()
        if resolved.exists():
            return resolved

        # Try stripping a leading "./" component that FauxPy sometimes emits.
        parts = file_path.parts
        if parts and parts[0] in (".", "./"):
            stripped_file_path = (
                Path(*parts[1:]) if len(parts) > 1 else Path(file_path_str)
            )
            worktree_stripped_path = (checkout.worktree / stripped_file_path).resolve()
            if worktree_stripped_path.exists():
                return worktree_stripped_path

        logger.warning(
            "Source file not found: %s (worktree: %s)", file_path_str, checkout.worktree
        )
        return None
