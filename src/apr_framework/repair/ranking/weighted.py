"""Weighted composite patch ranker combining three complementary signals."""

from apr_framework.core.models import LocalizationResult, RepairAttemptResult
from apr_framework.repair.ranking.base import PatchRanker

# Fixed priority scores per operator key, reflecting empirical fix-pattern frequency.
# According to my experiences :( Off-by-one and comparison fixes are the most common single-statement bugs in projects ; arithmetic and return-value changes are more speculative.
_OPERATOR_PRIORITY: dict[str, float] = {
    "obo": 1.0,
    "comp": 0.9,
    "bool": 0.7,
    "negate": 0.6,
    "arith": 0.5,
    "return": 0.4,
}
_FALLBACK_OPERATOR_PRIORITY = 0.2

DEFAULT_SUSPICIOUSNESS_WEIGHT = 0.6
DEFAULT_SIMPLICITY_WEIGHT = 0.25
DEFAULT_OPERATOR_PRIORITY_WEIGHT = 0.15


class WeightedCompositeRanker(PatchRanker):
    """Rank plausible patches by a weighted sum of three normalised signals.

    ranking_score = w1 * suspiciousness + w2 * patch_simplicity + w3 * operator_priority

    All three components are normalised to [0, 1] before weighting.  Weights are
    normalised internally so callers may pass any non-negative values — only their
    relative magnitudes matter.

    Components:
        suspiciousness:     FL score of the targeted location, normalised by the
                            maximum score across all plausible patches in the set.
        patch_simplicity:   1 − (changed_line_count / max_changed_line_count).
                            Prefers patches that touch fewer lines.
        operator_priority:  Fixed priority tier for the mutation operator used,
                            reflecting empirical fix-pattern likelihood.
    """

    def __init__(
        self,
        suspiciousness_weight: float = DEFAULT_SUSPICIOUSNESS_WEIGHT,
        simplicity_weight: float = DEFAULT_SIMPLICITY_WEIGHT,
        operator_priority_weight: float = DEFAULT_OPERATOR_PRIORITY_WEIGHT,
    ) -> None:
        if (
            suspiciousness_weight < 0
            or simplicity_weight < 0
            or operator_priority_weight < 0
        ):
            raise ValueError("Ranker weights must be non-negative.")
        total_weight = (
            suspiciousness_weight + simplicity_weight + operator_priority_weight
        )
        if total_weight == 0:
            raise ValueError("At least one ranker weight must be positive.")
        self._suspiciousness_weight = suspiciousness_weight / total_weight
        self._simplicity_weight = simplicity_weight / total_weight
        self._operator_priority_weight = operator_priority_weight / total_weight

    @property
    def name(self) -> str:
        return "weighted-composite"

    def rank(
        self,
        plausible_results: list[RepairAttemptResult],
        localization_result: LocalizationResult | None = None,
    ) -> list[RepairAttemptResult]:
        if not plausible_results:
            return []

        suspiciousness_scores = self._extract_suspiciousness_scores(plausible_results)
        changed_line_counts = [
            _count_changed_lines(attempt_result.patch.diff_text)
            if attempt_result.patch is not None
            else 0
            for attempt_result in plausible_results
        ]
        operator_priority_scores = [
            _OPERATOR_PRIORITY.get(
                attempt_result.patch.metadata.get("operator", "")
                if attempt_result.patch
                else "",
                _FALLBACK_OPERATOR_PRIORITY,
            )
            for attempt_result in plausible_results
        ]

        normalised_suspiciousness = _normalise(suspiciousness_scores, fallback=0.5)
        normalised_simplicity = _invert_and_normalise(changed_line_counts)
        normalised_operator_priority = _normalise(
            operator_priority_scores, fallback=0.5
        )

        scored_results: list[tuple[float, RepairAttemptResult]] = []
        for index, attempt_result in enumerate(plausible_results):
            suspiciousness_component = normalised_suspiciousness[index]
            simplicity_component = normalised_simplicity[index]
            operator_priority_component = normalised_operator_priority[index]
            ranking_score = (
                self._suspiciousness_weight * suspiciousness_component
                + self._simplicity_weight * simplicity_component
                + self._operator_priority_weight * operator_priority_component
            )
            if attempt_result.patch is not None:
                attempt_result.patch.metadata["ranking_score"] = round(ranking_score, 6)
                attempt_result.patch.metadata["ranking_score_components"] = {
                    "suspiciousness": round(suspiciousness_component, 6),
                    "patch_simplicity": round(simplicity_component, 6),
                    "operator_priority": round(operator_priority_component, 6),
                }
            scored_results.append((ranking_score, attempt_result))

        scored_results.sort(key=lambda pair: pair[0], reverse=True)
        return [attempt_result for _, attempt_result in scored_results]

    @staticmethod
    def _extract_suspiciousness_scores(
        plausible_results: list[RepairAttemptResult],
    ) -> list[float]:
        raw_scores: list[float] = []
        for attempt_result in plausible_results:
            if attempt_result.patch is None:
                raw_scores.append(0.0)
                continue
            score = attempt_result.patch.metadata.get("suspiciousness_score")
            raw_scores.append(float(score) if score is not None else 0.0)
        return raw_scores


def _count_changed_lines(diff_text: str) -> int:
    """Count non-header +/- lines in a unified diff."""
    changed_line_count = 0
    for line in diff_text.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+") or line.startswith("-"):
            changed_line_count += 1
    return changed_line_count


def _normalise(values: list[float], fallback: float = 0.5) -> list[float]:
    """Min-max normalise values to [0, 1]; return fallback for all-equal inputs."""
    minimum = min(values)
    maximum = max(values)
    if maximum == minimum:
        return [fallback] * len(values)
    return [(value - minimum) / (maximum - minimum) for value in values]


def _invert_and_normalise(values: list[float]) -> list[float]:
    """Normalise then invert so the smallest value maps to 1.0 (best simplicity)."""
    normalised = _normalise(values, fallback=0.5)
    return [1.0 - normalised_value for normalised_value in normalised]
