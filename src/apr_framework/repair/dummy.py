import random
from pathlib import Path

from apr_framework.core.models import (
    BugIdentifier,
    CheckoutResult,
    PatchCandidate,
    RepairAttemptResult,
    RepairStatus,
)
from apr_framework.repair.base import RepairAlgorithm


GROUND_TRUTH_OUTCOME = "ground_truth_patch"
NOOP_OUTCOME = "original_unchanged"
SUPPORTED_DUMMY_BUGS = (
    BugIdentifier("bugsinpy", "black", 1),
    BugIdentifier("bugsinpy", "black", 3),
    BugIdentifier("bugsinpy", "black", 23),
)


class DummyRepairAlgorithm(RepairAlgorithm):
    """
    Dummy repair component for Task 3.

    It randomly returns either the BugsInPy ground-truth patch or a no-op patch
    that leaves the checked-out buggy program unchanged.
    """

    def __init__(
        self,
        project_root: Path,
        seed: int | None = None,
        forced_outcome: str | None = None,
    ) -> None:
        self._project_root = project_root
        self._rng = random.Random(seed)
        self._forced_outcome = forced_outcome

        if forced_outcome is not None and forced_outcome not in {
            GROUND_TRUTH_OUTCOME,
            NOOP_OUTCOME,
        }:
            raise ValueError(f"Unsupported dummy repair outcome: {forced_outcome}")

    @property
    def name(self) -> str:
        """
        Stable repair algorithm name used by dummy evaluation runs.
        """
        return "dummy-repair"

    @property
    def supported_bugs(self) -> tuple[BugIdentifier, ...]:
        """
        Bugs for which the dummy repair algorithm can return a patch candidate.
        """
        return SUPPORTED_DUMMY_BUGS

    def generate_patches(
        self, bug: BugIdentifier, checkout: CheckoutResult
    ) -> list[PatchCandidate]:
        """
        Generate one dummy patch candidate for supported BugsInPy bugs.

        Depending on the configured or random outcome, the candidate is either
        the BugsInPy ground-truth diff or a no-op patch with empty diff text.

        Args:
            bug: Bug for which a candidate patch is requested.
            checkout: Checkout result for the buggy project. Accepted for the
                repair interface; the dummy implementation only needs the bug id.

        Returns:
            A single dummy patch candidate for supported bugs, otherwise an empty list.
        """
        if bug not in SUPPORTED_DUMMY_BUGS:
            return []

        outcome = self._choose_outcome()
        is_noop = outcome == NOOP_OUTCOME
        patch_source = self._patch_source(bug)
        diff_text = "" if is_noop else patch_source.read_text(encoding="utf-8")

        return [
            PatchCandidate(
                bug=bug,
                patch_id=f"dummy-{bug.project}-{bug.bug_id}-{outcome}",
                summary=self._summary(outcome),
                diff_text=diff_text,
                metadata={
                    "outcome": outcome,
                    "is_noop": is_noop,
                    "patch_source": str(patch_source),
                },
            )
        ]

    def validate_patch(
        self, bug: BugIdentifier, checkout: CheckoutResult, patch: PatchCandidate
    ) -> RepairAttemptResult:
        """
        Classify a dummy patch as no-patch or correct from its metadata.

        Args:
            bug: Bug being repaired.
            checkout: Checkout result for the target worktree. Accepted for the
                repair interface; this implementation does not inspect it.
            patch: Dummy patch candidate to validate.

        Returns:
            Repair attempt result matching the dummy patch outcome.
        """
        if patch.metadata.get("is_noop"):
            return RepairAttemptResult(
                bug=bug,
                patch=patch,
                status=RepairStatus.NO_PATCH,
                validation_summary="Dummy repair selected original unchanged code.",
            )

        return RepairAttemptResult(
            bug=bug,
            patch=patch,
            status=RepairStatus.CORRECT,
            validation_summary="Dummy repair selected the BugsInPy ground-truth patch.",
        )

    def _choose_outcome(self) -> str:
        if self._forced_outcome is not None:
            return self._forced_outcome
        return self._rng.choice([GROUND_TRUTH_OUTCOME, NOOP_OUTCOME])

    def _patch_source(self, bug: BugIdentifier) -> Path:
        return (
            self._project_root
            / ".tools"
            / "bugsinpy"
            / "projects"
            / bug.project
            / "bugs"
            / str(bug.bug_id)
            / "bug_patch.txt"
        )

    def _summary(self, outcome: str) -> str:
        if outcome == GROUND_TRUTH_OUTCOME:
            return "BugsInPy ground-truth patch"
        return "Original solution unchanged"
