from dataclasses import dataclass

from apr_framework.core.exceptions import ConfigurationError
from apr_framework.core.models import (
    BugIdentifier,
    CheckoutResult,
    LocalizationResult,
    RankedLocation,
    TestRunResult,
)
from apr_framework.localization.base import FaultLocalizer


@dataclass
class _LocationScores:
    location: RankedLocation
    sbfl: RankedLocation | None = None
    mbfl: RankedLocation | None = None


class HybridFaultLocalizer(FaultLocalizer):
    """Combine SBFL and MBFL rankings with a weighted normalized score."""

    def __init__(
        self,
        sbfl_localizer: FaultLocalizer,
        mbfl_localizer: FaultLocalizer,
        *,
        sbfl_weight: float = 0.5,
        mbfl_weight: float = 0.5,
        top_n: int | None = None,
    ) -> None:
        _validate_weights(sbfl_weight, mbfl_weight)
        if top_n is not None and top_n <= 0:
            raise ConfigurationError(
                "Hybrid localization top_n must be positive or None."
            )

        total_weight = sbfl_weight + mbfl_weight
        self._sbfl_localizer = sbfl_localizer
        self._mbfl_localizer = mbfl_localizer
        self._raw_sbfl_weight = sbfl_weight
        self._raw_mbfl_weight = mbfl_weight
        self._sbfl_weight = sbfl_weight / total_weight
        self._mbfl_weight = mbfl_weight / total_weight
        self._top_n = top_n

    @property
    def name(self) -> str:
        return "hybrid-fauxpy"

    def localize(
        self,
        bug: BugIdentifier,
        checkout: CheckoutResult,
        test_result: TestRunResult | None = None,
    ) -> LocalizationResult:
        sbfl_result = self._sbfl_localizer.localize(bug, checkout, test_result)
        mbfl_result = self._mbfl_localizer.localize(bug, checkout, test_result)

        ranked_locations = self.combine_rankings(
            bug=bug,
            sbfl_result=sbfl_result,
            mbfl_result=mbfl_result,
            sbfl_weight=self._sbfl_weight,
            mbfl_weight=self._mbfl_weight,
            top_n=self._top_n,
        )

        sbfl_formula = sbfl_result.metadata.get("score_formula", "SBFL")
        mbfl_formula = mbfl_result.metadata.get("score_formula", "MBFL")
        score_formula = (
            f"{self._sbfl_weight:.4f} * normalized({sbfl_formula}) + "
            f"{self._mbfl_weight:.4f} * normalized({mbfl_formula})"
        )

        return LocalizationResult(
            bug=bug,
            backend=self.name,
            ranked_locations=ranked_locations,
            metadata={
                "family": "hybrid",
                "score_formula": score_formula,
                "sbfl_weight": self._sbfl_weight,
                "mbfl_weight": self._mbfl_weight,
                "raw_sbfl_weight": self._raw_sbfl_weight,
                "raw_mbfl_weight": self._raw_mbfl_weight,
                "sbfl_metric": sbfl_formula,
                "mbfl_metric": mbfl_formula,
                "sbfl_metadata": sbfl_result.metadata,
                "mbfl_metadata": mbfl_result.metadata,
            },
        )

    @staticmethod
    def combine_rankings(
        *,
        bug: BugIdentifier,
        sbfl_result: LocalizationResult,
        mbfl_result: LocalizationResult,
        sbfl_weight: float,
        mbfl_weight: float,
        top_n: int | None = None,
    ) -> list[RankedLocation]:
        _validate_weights(sbfl_weight, mbfl_weight)
        if top_n is not None and top_n <= 0:
            raise ConfigurationError(
                "Hybrid localization top_n must be positive or None."
            )

        total_weight = sbfl_weight + mbfl_weight
        normalized_sbfl_weight = sbfl_weight / total_weight
        normalized_mbfl_weight = mbfl_weight / total_weight
        sbfl_max = _max_score(sbfl_result.ranked_locations)
        mbfl_max = _max_score(mbfl_result.ranked_locations)

        merged: dict[tuple, _LocationScores] = {}
        for location in sbfl_result.ranked_locations:
            key = _location_key(location)
            merged.setdefault(key, _LocationScores(location=location)).sbfl = location

        for location in mbfl_result.ranked_locations:
            key = _location_key(location)
            if key not in merged:
                merged[key] = _LocationScores(location=location)
            merged[key].mbfl = location

        combined: list[RankedLocation] = []
        for item in merged.values():
            sbfl_score = _score_or_zero(item.sbfl)
            mbfl_score = _score_or_zero(item.mbfl)
            normalized_sbfl_score = _normalized_score(sbfl_score, sbfl_max)
            normalized_mbfl_score = _normalized_score(mbfl_score, mbfl_max)
            hybrid_score = (
                normalized_sbfl_weight * normalized_sbfl_score
                + normalized_mbfl_weight * normalized_mbfl_score
            )

            source = _source_name(item.sbfl is not None, item.mbfl is not None)
            base_location = item.sbfl or item.mbfl or item.location
            combined.append(
                RankedLocation(
                    rank=0,
                    file_path=base_location.file_path,
                    location=base_location.location,
                    score=hybrid_score,
                    line=base_location.line,
                    end_line=base_location.end_line,
                    function=base_location.function,
                    raw_location=base_location.raw_location,
                    metadata={
                        "score_formula": "Hybrid SBFL + MBFL",
                        "source": source,
                        "sbfl_score": sbfl_score if item.sbfl is not None else None,
                        "mbfl_score": mbfl_score if item.mbfl is not None else None,
                        "normalized_sbfl_score": normalized_sbfl_score,
                        "normalized_mbfl_score": normalized_mbfl_score,
                        "sbfl_rank": item.sbfl.rank if item.sbfl is not None else None,
                        "mbfl_rank": item.mbfl.rank if item.mbfl is not None else None,
                    },
                )
            )

        combined.sort(key=_ranking_key)
        if top_n is not None:
            combined = combined[:top_n]

        return [
            RankedLocation(
                rank=index,
                file_path=location.file_path,
                location=location.location,
                score=location.score,
                line=location.line,
                end_line=location.end_line,
                function=location.function,
                raw_location=location.raw_location,
                metadata=location.metadata,
            )
            for index, location in enumerate(combined, start=1)
        ]


def _max_score(locations: list[RankedLocation]) -> float:
    scores = [location.score for location in locations if location.score is not None]
    if not scores:
        return 0.0
    return max(scores)


def _validate_weights(sbfl_weight: float, mbfl_weight: float) -> None:
    if sbfl_weight < 0 or mbfl_weight < 0:
        raise ConfigurationError("Hybrid localization weights must be non-negative.")
    if sbfl_weight == 0 and mbfl_weight == 0:
        raise ConfigurationError("Hybrid localization weights cannot both be zero.")


def _score_or_zero(location: RankedLocation | None) -> float:
    if location is None or location.score is None:
        return 0.0
    return location.score


def _normalized_score(score: float, max_score: float) -> float:
    if max_score <= 0:
        return 0.0
    return score / max_score


def _location_key(location: RankedLocation) -> tuple:
    if location.file_path and (
        location.line is not None or location.function is not None
    ):
        return ("structured", location.file_path, location.line, location.function)
    return ("raw", location.location)


def _source_name(has_sbfl: bool, has_mbfl: bool) -> str:
    if has_sbfl and has_mbfl:
        return "both"
    if has_sbfl:
        return "sbfl"
    return "mbfl"


def _ranking_key(location: RankedLocation) -> tuple:
    metadata = location.metadata
    in_both = metadata.get("source") == "both"
    ranks = [
        rank
        for rank in [metadata.get("sbfl_rank"), metadata.get("mbfl_rank")]
        if isinstance(rank, int)
    ]
    best_rank = min(ranks) if ranks else 10**9
    line = location.line if location.line is not None else 10**9
    function = location.function or ""
    score = location.score if location.score is not None else 0.0
    return (
        -score,
        not in_both,
        best_rank,
        location.file_path,
        line,
        function,
        location.location,
    )
