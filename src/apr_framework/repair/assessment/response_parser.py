"""Parse LLM patch-assessment replies."""

import json
import logging
import re
from typing import Any

from apr_framework.core.models import PatchAssessment

logger = logging.getLogger(__name__)

_FENCED_BLOCK_PATTERN = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def parse_assessment_response(response_text: str) -> PatchAssessment:
    """Parse a model response into a PatchAssessment, never raising on bad output."""
    parsed_response = _extract_json_object(response_text)
    if parsed_response is None:
        logger.warning("Could not parse assessment JSON from the model response.")
        return PatchAssessment(
            patch_id="",
            quality_score=0.0,
            rationale=response_text.strip(),
            raw_response=response_text,
        )

    quality_score = _coerce_quality_score(parsed_response.get("quality_score"))
    rationale_value = parsed_response.get("rationale", "")
    rationale = rationale_value if isinstance(rationale_value, str) else ""
    return PatchAssessment(
        patch_id="",
        quality_score=quality_score,
        rationale=rationale,
        raw_response=response_text,
    )


def _extract_json_object(response_text: str) -> dict[str, Any] | None:
    """Return the first JSON object recoverable from response_text."""
    for candidate_text in _json_object_candidates(response_text):
        try:
            parsed_candidate = json.loads(candidate_text)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed_candidate, dict):
            return parsed_candidate
    return None


def _json_object_candidates(response_text: str) -> list[str]:
    """Yield likely JSON-object substrings, best candidates first."""
    candidates = [response_text.strip()]
    for fenced_match in _FENCED_BLOCK_PATTERN.findall(response_text):
        candidates.append(fenced_match.strip())
    first_brace_index = response_text.find("{")
    last_brace_index = response_text.rfind("}")
    if first_brace_index != -1 and last_brace_index > first_brace_index:
        candidates.append(response_text[first_brace_index : last_brace_index + 1])
    return candidates


def _coerce_quality_score(score_value: object) -> float:
    """Convert score_value to a clamped [0, 1] float."""
    try:
        quality_score = float(score_value)
    except (TypeError, ValueError):
        return 0.0
    return min(1.0, max(0.0, quality_score))
