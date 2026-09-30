from abc import ABC, abstractmethod

from apr_framework.core.models import (
    BugIdentifier,
    CheckoutResult,
    LocalizationResult,
    TestRunResult,
)


class FaultLocalizer(ABC):
    """
    Interface for components that rank suspicious source locations for a bug.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """
        Stable localizer name used in logs, reports, and run metadata.
        """
        raise NotImplementedError

    @abstractmethod
    def localize(
        self,
        bug: BugIdentifier,
        checkout: CheckoutResult,
        test_result: TestRunResult | None = None,
    ) -> LocalizationResult:
        """
        Analyze a checked-out buggy program and rank suspicious code locations.

        Receives the bug identity, the checked-out project state, and the result of
        the executed tests, and produces a structured localization result containing
        the ranked suspicious locations and optional metadata.

        Args:
            bug: Identifier of the bug being analyzed.
            checkout: Checkout result containing the buggy project worktree.
            test_result: Test output and counts used as localization evidence.

        Returns:
            Ranked suspicious locations and optional localizer metadata.
        """
        raise NotImplementedError
