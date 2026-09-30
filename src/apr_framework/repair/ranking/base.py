from abc import ABC, abstractmethod

from apr_framework.core.models import LocalizationResult, RepairAttemptResult


class PatchRanker(ABC):
    """Interface for strategies that reorder plausible patches before presentation."""

    @property
    @abstractmethod
    def name(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def rank(
        self,
        plausible_results: list[RepairAttemptResult],
        localization_result: LocalizationResult | None = None,
    ) -> list[RepairAttemptResult]:
        """Return plausible_results sorted by descending ranking score.

        Implementations attach ``ranking_score`` and ``ranking_score_components``
        to each ``patch.metadata`` dict as a side effect so callers can serialise
        per-patch scores without re-computing them.
        """
        raise NotImplementedError
