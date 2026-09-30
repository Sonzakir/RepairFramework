from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from apr_framework.core.models import (
    BugIdentifier,
    CheckoutResult,
    PatchCandidate,
    RepairAttemptResult,
)

if TYPE_CHECKING:
    from apr_framework.repair.run_loop import LoopOutcome


class RepairAlgorithm(ABC):
    """
    Interface for repair algorithms that generate and validate candidate patches.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """
        Stable repair algorithm name used in logs, reports, and run metadata.
        """
        raise NotImplementedError

    @abstractmethod
    def generate_patches(
        self, bug: BugIdentifier, checkout: CheckoutResult
    ) -> list[PatchCandidate]:
        """
        Generate candidate patches for a checked-out buggy program.

        Args:
            bug: Identifier of the bug for which patches are generated.
            checkout: Checkout result containing the buggy project worktree.

        Returns:
            Candidate patches proposed by the repair algorithm. An empty list
            means the algorithm did not produce a patch for this bug.
        """
        raise NotImplementedError

    @abstractmethod
    def validate_patch(
        self, bug: BugIdentifier, checkout: CheckoutResult, patch: PatchCandidate
    ) -> RepairAttemptResult:
        """
        Validate a generated patch candidate for a checked-out bug.

        Args:
            bug: Identifier of the bug being repaired.
            checkout: Checkout result containing the target worktree.
            patch: Candidate patch to validate.

        Returns:
            Validation outcome for the given candidate patch.
        """
        raise NotImplementedError

    def repair_loop(
        self,
        bug: BugIdentifier,
        checkout: CheckoutResult,
        *,
        budget: int,
        stop_on_first: bool,
    ) -> "LoopOutcome":
        """Run the budget-bounded validation loop for this algorithm.

        Default implementation delegates to the shared generate-and-validate loop
        (``run_validation_loop``). Override this
        method for algorithms whose generation and validation are interleaved (e.g.
        iterative LLM repair with test-failure feedback), while keeping
        ``generate_patches()`` and ``validate_patch()`` as the primitives every
        backend still implements.

        Args:
            bug:           Bug under repair.
            checkout:      Checked-out worktree to validate against.
            budget:        Maximum number of patch validations (test-suite runs).
            stop_on_first: Stop as soon as a plausible patch is found.

        Returns:
            A ``LoopOutcome`` with the summary result, per-candidate results, and
            metrics.
        """
        # Deferred import avoids a circular import: run_loop imports RepairAlgorithm.
        from apr_framework.repair.run_loop import run_validation_loop

        return run_validation_loop(
            self, bug, checkout, budget=budget, stop_on_first=stop_on_first
        )

    def llm_query_count(self) -> int | None:
        """Number of LLM API calls this algorithm made during the last repair run.

        Default is ``None`` for algorithms that issue no LLM queries (e.g. the
        template backend), so their result files carry no query-count field.
        Backends driven by a language model override this to report their count.
        """
        return None
