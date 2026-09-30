"""LLM-based patch assessment."""

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from apr_framework.core.models import (
    CheckoutResult,
    LocalizationResult,
    PatchAssessment,
    RepairAttemptResult,
)
from apr_framework.repair.assessment.base import PatchAssessor
from apr_framework.repair.assessment.config import (
    LLMAssessmentConfig,
    load_assessment_system_prompt,
)
from apr_framework.repair.assessment.response_parser import parse_assessment_response
from apr_framework.repair.llm.client import LLMClient
from apr_framework.repair.llm.context_enricher import build_failing_test_context
from apr_framework.repair.llm.prompt_builder import extract_function_source

if TYPE_CHECKING:
    from apr_framework.benchmarks.bugsinpy import BugsInPyAdapter

logger = logging.getLogger(__name__)


class LLMPatchAssessor(PatchAssessor):
    """Assess plausible patches with an LLM and sort by semantic quality."""

    def __init__(
        self,
        config: LLMAssessmentConfig,
        client: LLMClient,
        adapter: "BugsInPyAdapter",
    ) -> None:
        self._config = config
        self._client = client
        self._adapter = adapter
        self._assessment_query_count = 0

    @property
    def name(self) -> str:
        return "llm-patch-assessor"

    def assess(
        self,
        plausible_results: list[RepairAttemptResult],
        checkout: CheckoutResult,
        localization_result: LocalizationResult | None = None,
    ) -> list[RepairAttemptResult]:
        self._assessment_query_count = 0
        if not plausible_results:
            return []
        # Gather the Failure Evidence once (they describe the bug not the fix)
        failing_test_source, error_traceback = self._gather_failure_evidence(checkout)
        patches_to_assess = self._select_patches_within_cap(plausible_results)
        for attempt_result in patches_to_assess:
            patch_assessment = self._assess_one_patch(
                attempt_result,
                failing_test_source,
                error_traceback,
            )
            self._attach_assessment_to_patch_metadata(attempt_result, patch_assessment)
        return self._reorder_by_descending_quality(plausible_results)

    def llm_query_count(self) -> int | None:
        return self._assessment_query_count

    def _gather_failure_evidence(
        self, checkout: CheckoutResult
    ) -> tuple[str | None, str | None]:
        failure_context = build_failing_test_context(
            self._adapter,
            checkout,
            enabled=True,
            timeout=self._config.timeout_seconds,
        )
        return failure_context.failing_test_source, failure_context.error_traceback

    def _select_patches_within_cap(
        self, plausible_results: list[RepairAttemptResult]
    ) -> list[RepairAttemptResult]:
        max_patches_assessed = self._config.max_patches_assessed
        if max_patches_assessed is None:
            return plausible_results
        return plausible_results[:max_patches_assessed]

    def _assess_one_patch(
        self,
        attempt_result: RepairAttemptResult,
        failing_test_source: str | None,
        error_traceback: str | None,
    ) -> PatchAssessment:
        if attempt_result.patch is None:
            return PatchAssessment(
                patch_id="",
                quality_score=0.0,
                rationale="No patch was available to assess.",
            )

        original_code_region = self._extract_original_code_region(attempt_result)
        messages = build_assessment_prompt(
            attempt_result.patch.diff_text,
            failing_test_source,
            error_traceback,
            system_message_text=load_assessment_system_prompt(
                self._config.system_prompt_name
            ),
            original_code_region=original_code_region,
        )
        response_text = self._client.complete(messages)
        self._assessment_query_count += 1
        patch_assessment = parse_assessment_response(response_text)
        patch_assessment.patch_id = attempt_result.patch.patch_id
        return patch_assessment

    def _extract_original_code_region(
        self, attempt_result: RepairAttemptResult
    ) -> str | None:
        patch = attempt_result.patch
        if patch is None:
            return None
        source_path_value = patch.metadata.get("source_path")
        target_line_value = patch.metadata.get("target_line")
        if source_path_value is None or target_line_value is None:
            return None
        try:
            source_file_path = Path(str(source_path_value))
            target_line_number = int(target_line_value)
            function_source, start_line_number, end_line_number = (
                extract_function_source(source_file_path, target_line_number)
            )
        except Exception as extraction_error:  # noqa: BLE001 - best-effort prompt enrichment
            logger.warning(
                "Could not extract original code for %s: %s",
                patch.patch_id,
                extraction_error,
            )
            return None
        return (
            f"File: {source_file_path}\n"
            f"Lines: {start_line_number}-{end_line_number}\n"
            f"```python\n{function_source.rstrip()}\n```"
        )

    @staticmethod
    def _attach_assessment_to_patch_metadata(
        attempt_result: RepairAttemptResult,
        patch_assessment: PatchAssessment,
    ) -> None:
        if attempt_result.patch is None:
            return
        attempt_result.patch.metadata["quality_score"] = round(
            patch_assessment.quality_score, 6
        )
        attempt_result.patch.metadata["assessment_rationale"] = (
            patch_assessment.rationale
        )
        attempt_result.patch.metadata["assessment_raw_response"] = (
            patch_assessment.raw_response
        )

    @staticmethod
    def _reorder_by_descending_quality(
        plausible_results: list[RepairAttemptResult],
    ) -> list[RepairAttemptResult]:
        indexed_results = list(enumerate(plausible_results))

        def sort_key(
            indexed_result: tuple[int, RepairAttemptResult],
        ) -> tuple[int, float, int]:
            original_index, attempt_result = indexed_result
            if attempt_result.patch is None:
                return (1, 0.0, original_index)
            score_value = attempt_result.patch.metadata.get("quality_score")
            if score_value is None:
                return (1, 0.0, original_index)
            return (0, -float(score_value), original_index)

        return [
            attempt_result
            for _, attempt_result in sorted(indexed_results, key=sort_key)
        ]


def build_assessment_prompt(
    diff_text: str,
    failing_test_source: str | None,
    error_traceback: str | None,
    *,
    system_message_text: str | None = None,
    original_code_region: str | None = None,
) -> list[dict[str, str]]:
    """Build the [system, user] messages for one patch-assessment request."""
    if system_message_text is None:
        system_message_text = load_assessment_system_prompt("assess_prompt1")

    user_sections = []
    if original_code_region is not None:
        user_sections.append("## Original Buggy Code\n" + original_code_region)
    user_sections.append(_build_diff_section(diff_text))
    if failing_test_source is not None:
        user_sections.append(_build_failing_test_section(failing_test_source))
    if error_traceback is not None:
        user_sections.append(_build_error_traceback_section(error_traceback))
    user_sections.append(_build_assessment_instruction_section())

    return [
        {"role": "system", "content": system_message_text},
        {"role": "user", "content": "\n\n".join(user_sections)},
    ]


def _build_diff_section(diff_text: str) -> str:
    return (
        "## Candidate Patch\n"
        "Unified diff from the buggy program to the candidate fix:\n"
        f"```diff\n{diff_text.strip()}\n```"
    )


def _build_failing_test_section(failing_test_source: str) -> str:
    return (
        "## Previously Failing Test\n"
        "This test now passes after applying the candidate patch:\n"
        f"```python\n{failing_test_source.strip()}\n```"
    )


def _build_error_traceback_section(error_traceback: str) -> str:
    return (
        "## Original Failure Traceback\n"
        "The unpatched program produced this failure:\n"
        f"```\n{error_traceback.strip()}\n```"
    )


def _build_assessment_instruction_section() -> str:
    return (
        "## Assessment Task\n"
        "Judge whether the candidate patch fixes the root cause or only satisfies "
        "the visible failing test. Return only the strict JSON object requested by "
        "the system message."
    )
