"""Patch assessment interface."""

from abc import ABC, abstractmethod

from apr_framework.core.models import (
    CheckoutResult,
    LocalizationResult,
    RepairAttemptResult,
)


class PatchAssessor(ABC):
    """LLM as a judge: Interface for strategies that judge plausible patch quality."""

    @property
    @abstractmethod
    def name(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def assess(
        self,
        plausible_results: list[RepairAttemptResult],
        checkout: CheckoutResult,
        localization_result: LocalizationResult | None = None,
    ) -> list[RepairAttemptResult]:
        """Return plausible_results re-ordered by descending quality_score.

        Implementations attach ``quality_score`` and ``assessment_rationale`` to
        each assessed patch's metadata so callers can serialize per-patch judgments.
        """
        raise NotImplementedError

    def llm_query_count(self) -> int | None:
        return None
