from abc import ABC, abstractmethod

from apr_framework.benchmarks.base import BenchmarkAdapter
from apr_framework.core.models import BugIdentifier, EvaluationResult
from apr_framework.localization.base import FaultLocalizer
from apr_framework.repair.base import RepairAlgorithm


class EvaluationRunner(ABC):
    """
    Interface for components that execute APR experiments over bugs.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """
        Stable runner name used in logs, reports, and serialized run metadata.
        """
        raise NotImplementedError

    @abstractmethod
    def run(
        self,
        bugs: list[BugIdentifier],
        benchmark: BenchmarkAdapter,
        repair: RepairAlgorithm,
        localizer: FaultLocalizer | None = None,
    ) -> list[EvaluationResult]:
        """
        Execute the evaluation pipeline for a sequence of bugs.

        Args:
            bugs: Bugs to process in this evaluation run.
            benchmark: Adapter used to check out bugs and run benchmark tests.
            repair: Repair algorithm used to generate and validate patches.
            localizer: Optional fault localizer used before repair.

        Returns:
            One evaluation result per processed bug.
        """
        raise NotImplementedError
