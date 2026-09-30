"""LLM-based repair algorithm implementation."""

import logging
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from apr_framework.benchmarks.bugsinpy import BugsInPyAdapter
from apr_framework.core.exceptions import APRFrameworkError
from apr_framework.core.models import (
    BugIdentifier,
    CheckoutResult,
    LocalizationResult,
    PatchCandidate,
    RankedLocation,
    RepairAttemptResult,
    RepairStatus,
    RetrievalTrace,
    TestRunResult,
)
from apr_framework.repair.base import RepairAlgorithm
from apr_framework.repair.llm.client import LLMClient
from apr_framework.repair.llm.config import LLMRepairConfig
from apr_framework.repair.llm.context_enricher import (
    FailingTestContext,
    build_failing_test_context,
)
from apr_framework.repair.llm.feedback import (
    build_format_retry_message,
    build_test_failure_feedback_message,
    is_no_improvement_signal,
)
from apr_framework.repair.llm.few_shot import build_few_shot_examples
from apr_framework.repair.llm.patch_extractor import extract_patch_with_source
from apr_framework.repair.llm.prompt_builder import (
    RETRIEVAL_INSTRUCTIONS_PROMPT_NAME,
    build_repair_prompt,
    build_system_message_with_retrieval_permission,
    extract_function_source,
    load_system_prompt,
)
from apr_framework.repair.llm.retrieval_loop import run_retrieval_loop
from apr_framework.repair.patch_applier import apply_patch_and_validate
from apr_framework.repair.regression import RegressionContext, build_regression_context
from apr_framework.repair.run_loop import LoopOutcome, build_loop_summary

logger = logging.getLogger(__name__)

# Max consecutive unparsable LLM replies tolerated for one location before the
# iterative loop gives up on it (bounded format-retry, see _run_conversation_for_location).
_MAX_FORMAT_RETRIES = 2


@dataclass(frozen=True)
class _LocationPrompt:
    """Everything needed to start — and continue — an LLM conversation at a location.

    Bundles the resolved source file, the enclosing function's line bounds (used
    later to splice the LLM's replacement back in), and the initial ``[system,
    user]`` message list. The single-shot and iterative paths both build one of
    these via :meth:`LLMRepairAlgorithm._build_location_prompt`.
    """

    source_file_path: Path
    function_start_line: int
    function_end_line: int
    messages: list[dict[str, str]]
    retrieval_trace: RetrievalTrace | None = None


class LLMRepairAlgorithm(RepairAlgorithm):
    """LLM-based repair algorithm: FL-guided prompting with unified-diff output.

    For each suspicious location the algorithm asks the LLM to return a
    corrected version of the enclosing function.  The response is parsed into
    a unified diff and validated against the test suite.  The generate /
    validate steps conform to the RepairAlgorithm ABC so the algorithm slots
    into RepairEvaluationRunner without any changes.

    Constructor Args:
        localization_result: Ranked suspicious locations from a prior FL run.
        adapter:             BugsInPyAdapter used to invoke the test suite.
        repair_config:       LLM-specific repair configuration (required).
        llm_client:          Injected LLM client; substitute a stub in tests.
    """

    def __init__(
        self,
        localization_result: LocalizationResult,
        adapter: BugsInPyAdapter,
        repair_config: LLMRepairConfig,
        llm_client: LLMClient,
    ) -> None:
        self._localization_result = localization_result
        self._adapter = adapter
        self._repair_config = repair_config
        self._llm_client = llm_client
        base_system_message_text = load_system_prompt(repair_config.system_prompt_name)
        retrieval_is_enabled = repair_config.retrieval_budget > 0
        # With retrieval on, the system prompt must permit a bare RETRIEVE reply —
        # its "fenced code block only" rule otherwise overrides the user-message
        # protocol and the model never calls a tool.
        self._system_message_text = (
            build_system_message_with_retrieval_permission(base_system_message_text)
            if retrieval_is_enabled
            else base_system_message_text
        )
        self._retrieval_instructions_text = (
            load_system_prompt(RETRIEVAL_INSTRUCTIONS_PROMPT_NAME)
            if retrieval_is_enabled
            else None
        )
        # Regression baseline is established once, lazily, on the first
        # validate_patch call; reused for every subsequent candidate.
        self._regression: RegressionContext | None = None
        # Failing-test context (test source + traceback) is gathered once, lazily,
        # on the first generate_patches call and reused for every prompt.
        self._failing_test_context: FailingTestContext | None = None
        # Few-shot examples are built once, lazily, and reused for every prompt.
        # _few_shot_ready distinguishes "not built yet" from "built, no examples".
        self._few_shot_examples: str | None = None
        self._few_shot_ready: bool = False

    @property
    def name(self) -> str:
        return "llm-repair"

    def llm_query_count(self) -> int | None:
        """Report how many LLM API calls the client has made for this run."""
        return self._llm_client.completion_count

    # ------------------------------------------------------------------
    # RepairAlgorithm ABC
    # ------------------------------------------------------------------

    def generate_patches(
        self, bug: BugIdentifier, checkout: CheckoutResult
    ) -> list[PatchCandidate]:
        """Query the LLM for every top-N suspicious location.

        For each location, extracts the enclosing function, builds the repair
        prompt, and calls the LLM up to max_patch_count times.  Only responses
        that yield a valid, non-empty unified diff become PatchCandidates.

        Args:
            bug:      Bug identifier.
            checkout: Checkout result containing the project worktree.

        Returns:
            All generated PatchCandidate objects across all locations.
        """
        top_locations = self._localization_result.ranked_locations[
            : self._repair_config.top_n_locations
        ]
        all_patch_candidates: list[PatchCandidate] = []

        for location in top_locations:
            location_candidates = self._generate_patches_for_location(
                bug, checkout, location
            )
            all_patch_candidates.extend(location_candidates)

        logger.info(
            "LLM generated %d patch candidate(s) across %d location(s)",
            len(all_patch_candidates),
            len(top_locations),
        )
        return all_patch_candidates

    def validate_patch(
        self,
        bug: BugIdentifier,
        checkout: CheckoutResult,
        patch_candidate: PatchCandidate,
    ) -> RepairAttemptResult:
        """Apply a diff candidate to the worktree, run tests, and revert.

        Parses the target file path from the diff header, applies the patch
        with the system ``patch`` utility, delegates the test-run and
        regression check to ``apply_patch_and_validate``, then restores
        the file unconditionally.

        Args:
            bug:             Bug identifier.
            checkout:        Checkout result containing the target worktree.
            patch_candidate: Candidate patch whose diff_text holds a unified diff
                             with an absolute ``fromfile`` path.

        Returns:
            RepairAttemptResult with status PLAUSIBLE or FAILED.
        """
        source_file_path = _parse_source_file_path_from_diff(patch_candidate.diff_text)
        if source_file_path is None:
            logger.warning(
                "Cannot parse source path from diff for %s", patch_candidate.patch_id
            )
            return RepairAttemptResult(
                bug=bug,
                patch=patch_candidate,
                status=RepairStatus.FAILED,
                validation_summary=(
                    f"Cannot parse source path from diff for {patch_candidate.patch_id}."
                ),
            )

        try:
            original_file_content = source_file_path.read_bytes()
        except OSError as read_error:
            logger.error(
                "Cannot read %s for candidate %s: %s",
                source_file_path,
                patch_candidate.patch_id,
                read_error,
            )
            return RepairAttemptResult(
                bug=bug,
                patch=patch_candidate,
                status=RepairStatus.FAILED,
                validation_summary=(
                    f"Cannot read source file {source_file_path}: {read_error}"
                ),
            )

        diff_text = patch_candidate.diff_text

        def apply_fn() -> None:
            # Diffs carry absolute header paths, but GNU patch (>=2.8) refuses to
            # derive a target from an absolute name ("Ignoring potentially
            # dangerous file name"). Pass the resolved file explicitly so patch
            # applies to it directly; -p0 keeps the header untouched otherwise.
            proc = subprocess.run(
                ["patch", "-p0", str(source_file_path)],
                input=diff_text,
                capture_output=True,
                text=True,
                cwd=checkout.worktree,
            )
            if proc.returncode != 0:
                raise RuntimeError(
                    f"patch -p0 failed for {patch_candidate.patch_id}: "
                    f"{proc.stderr.strip()}"
                )

        def restore_fn() -> None:
            source_file_path.write_bytes(original_file_content)

        regression_context = self._regression_context(checkout)

        is_plausible, test_run_result = apply_patch_and_validate(
            apply_fn,
            restore_fn,
            self._adapter,
            checkout,
            regression_context,
            self._repair_config.timeout_seconds,
        )

        passed_count = test_run_result.passed_count
        failed_count = test_run_result.failed_count
        error_count = test_run_result.error_count

        # Iterative mode hands the failed test run's output back to the LLM as the
        # next conversation turn. Stash it (and the counts) on the candidate's
        # metadata — the same side-channel already used for ``patched_source``.
        # Gated on ``iterative`` so single-shot runs stay byte-for-byte unchanged;
        # RepairEvaluationRunner filters ``raw_test_output`` out of the JSON output.
        if self._repair_config.iterative:
            patch_candidate.metadata["raw_test_output"] = test_run_result.raw_output
            patch_candidate.metadata["passed_count"] = passed_count
            patch_candidate.metadata["failed_count"] = failed_count
            patch_candidate.metadata["error_count"] = error_count
            patch_candidate.metadata["failure_kind"] = _classify_failure_kind(
                is_plausible, test_run_result
            )

        if is_plausible:
            logger.info("Plausible patch found: %s", patch_candidate.patch_id)
            return RepairAttemptResult(
                bug=bug,
                patch=patch_candidate,
                status=RepairStatus.PLAUSIBLE,
                validation_summary=(
                    f"All tests passed with patch {patch_candidate.patch_id}."
                ),
            )

        return RepairAttemptResult(
            bug=bug,
            patch=patch_candidate,
            status=RepairStatus.FAILED,
            validation_summary=(
                f"Patch {patch_candidate.patch_id} did not fix all tests "
                f"(passed={passed_count}, failed={failed_count}, errors={error_count})."
            ),
        )

    # ------------------------------------------------------------------
    # Orchestration
    # ------------------------------------------------------------------

    def repair(
        self, bug: BugIdentifier, checkout: CheckoutResult
    ) -> tuple[RepairAttemptResult, list[RepairAttemptResult]]:
        """Run the full repair loop with budget and optional early stopping.

        Delegates to :meth:`repair_loop` (the shared backend-agnostic loop),
        identical to TemplateRepairAlgorithm.repair(). ``generate_patches()`` and
        ``validate_patch()`` remain the primitives; iterative mode overrides
        ``repair_loop`` without touching either.

        Args:
            bug:      Bug identifier.
            checkout: Checkout result for the worktree to repair.

        Returns:
            (summary_result, all_validation_results)
        """
        logger.info(
            "Starting LLM repair for %s#%d — budget=%d, top_n=%d, model=%s",
            bug.project,
            bug.bug_id,
            self._repair_config.budget,
            self._repair_config.top_n_locations,
            self._repair_config.model_name,
        )
        outcome = self.repair_loop(
            bug,
            checkout,
            budget=self._repair_config.budget,
            stop_on_first=self._repair_config.stop_on_first,
        )
        return outcome.summary, outcome.all_results

    def repair_loop(
        self,
        bug: BugIdentifier,
        checkout: CheckoutResult,
        *,
        budget: int,
        stop_on_first: bool,
    ) -> LoopOutcome:
        """Run the budget-bounded loop, iterative or single-shot.

        In single-shot mode this defers to the shared generate-and-validate loop
        (the ABC default). In iterative mode (``--iterative``) it instead runs a
        multi-turn conversation per location, feeding each failed patch's test
        output back to the model — Task 3's core behavior.
        """
        if not self._repair_config.iterative:
            return super().repair_loop(
                bug, checkout, budget=budget, stop_on_first=stop_on_first
            )
        return self._run_iterative_loop(
            bug, checkout, budget=budget, stop_on_first=stop_on_first
        )

    # ------------------------------------------------------------------
    # Iterative repair (T-3) — multi-turn conversation per location
    # ------------------------------------------------------------------

    def _run_iterative_loop(
        self,
        bug: BugIdentifier,
        checkout: CheckoutResult,
        *,
        budget: int,
        stop_on_first: bool,
    ) -> LoopOutcome:
        """Drive one multi-turn conversation per top-N location under a global budget.

        Each location gets a fresh conversation (:meth:`_run_conversation_for_location`);
        the global ``budget`` (test-suite executions) is shared across all locations,
        while ``max_iterations`` caps the turns spent on any single one.
        """
        started_at = time.monotonic()
        top_locations = self._localization_result.ranked_locations[
            : self._repair_config.top_n_locations
        ]

        all_results: list[RepairAttemptResult] = []
        total_candidates_generated = 0
        time_to_first_plausible: float | None = None
        budget_remaining = budget
        found_plausible = False

        for location in top_locations:
            if budget_remaining <= 0:
                logger.info(
                    "Budget exhausted — stopping iterative loop before location rank %d",
                    location.rank,
                )
                break
            if found_plausible and stop_on_first:
                break

            location_results, candidates_used = self._run_conversation_for_location(
                bug, checkout, location, budget_remaining
            )
            all_results.extend(location_results)
            total_candidates_generated += candidates_used
            budget_remaining -= candidates_used

            for result in location_results:
                if (
                    result.status == RepairStatus.PLAUSIBLE
                    and time_to_first_plausible is None
                ):
                    time_to_first_plausible = time.monotonic() - started_at
                    found_plausible = True

            if found_plausible and stop_on_first:
                break

        elapsed = time.monotonic() - started_at
        summary = build_loop_summary(
            bug, all_results, total_candidates_generated, budget, elapsed
        )
        logger.info(
            "Iterative repair loop finished in %.1fs: %d validated, %d plausible",
            elapsed,
            len(all_results),
            len(
                [
                    result
                    for result in all_results
                    if result.status == RepairStatus.PLAUSIBLE
                ]
            ),
        )
        return LoopOutcome(
            summary=summary,
            all_results=all_results,
            total_candidates_generated=total_candidates_generated,
            time_to_first_plausible_seconds=time_to_first_plausible,
            total_wall_clock_seconds=elapsed,
        )

    def _run_conversation_for_location(
        self,
        bug: BugIdentifier,
        checkout: CheckoutResult,
        location: RankedLocation,
        budget_remaining: int,
    ) -> tuple[list[RepairAttemptResult], int]:
        """Run a bounded multi-turn conversation to repair one suspicious location.

        Sends the initial repair prompt, then after each failed validation appends
        the test-failure feedback turn and asks again — up to ``max_iterations``
        times, never exceeding ``budget_remaining`` validations. Stops early on a
        plausible patch, a no-improvement signal, or repeated unparsable replies.

        Returns:
            ``(results, candidates_validated)`` — the per-turn validation results and
            how many test-suite executions this location consumed from the budget.
        """
        prepared_prompt = self._build_location_prompt(bug, checkout, location)
        if prepared_prompt is None:
            return [], 0

        source_file_path = prepared_prompt.source_file_path
        function_start_line = prepared_prompt.function_start_line
        function_end_line = prepared_prompt.function_end_line
        messages = prepared_prompt.messages

        results: list[RepairAttemptResult] = []
        previous_diff_text: str | None = None
        consecutive_format_failures = 0
        candidates_validated = 0

        for iteration_index in range(self._repair_config.max_iterations):
            if budget_remaining - candidates_validated <= 0:
                logger.info(
                    "Budget exhausted mid-conversation for %s:%d",
                    source_file_path.name,
                    location.line,
                )
                break

            try:
                llm_response_text = self._llm_client.complete(messages)
            except APRFrameworkError as llm_error:
                logger.warning(
                    "LLM call failed for %s:%d turn %d: %s",
                    source_file_path.name,
                    location.line,
                    iteration_index,
                    llm_error,
                )
                break

            extracted_patch = extract_patch_with_source(
                llm_response_text,
                source_file_path,
                function_start_line,
                function_end_line,
            )

            if extracted_patch is None:
                consecutive_format_failures += 1
                if consecutive_format_failures >= _MAX_FORMAT_RETRIES:
                    logger.warning(
                        "Giving up on %s:%d after %d unparsable replies",
                        source_file_path.name,
                        location.line,
                        consecutive_format_failures,
                    )
                    break
                messages.append({"role": "assistant", "content": llm_response_text})
                messages.append(
                    {"role": "user", "content": build_format_retry_message()}
                )
                continue
            consecutive_format_failures = 0

            if is_no_improvement_signal(
                extracted_patch.diff_text, previous_diff_text, llm_response_text
            ):
                logger.info(
                    "No-improvement signal for %s:%d at turn %d — moving to next location",
                    source_file_path.name,
                    location.line,
                    iteration_index + 1,
                )
                break

            patch_candidate = PatchCandidate(
                bug=bug,
                patch_id=f"llm-iter-{location.rank}-{iteration_index}",
                summary=(
                    f"LLM iterative patch for {location.file_path}:{location.line} "
                    f"(turn {iteration_index + 1})"
                ),
                diff_text=extracted_patch.diff_text,
                metadata={
                    "location_rank": location.rank,
                    "location_score": location.score,
                    "llm_response": llm_response_text,
                    "model": self._repair_config.model_name,
                    "source_path": str(source_file_path),
                    "target_line": location.line,
                    "patched_source": extracted_patch.patched_source,
                    "iteration": iteration_index + 1,
                },
            )
            if prepared_prompt.retrieval_trace is not None:
                patch_candidate.metadata["retrieval_trace"] = (
                    prepared_prompt.retrieval_trace
                )

            # Mirror run_validation_loop's guard: one patch's unexpected failure
            # must never abort the whole run. A validation error (e.g. establishing
            # the regression baseline) yields no test output to feed back, so record
            # FAILED, count it against budget, and end this location's conversation.
            try:
                result = self.validate_patch(bug, checkout, patch_candidate)
            except Exception as validation_error:  # noqa: BLE001 — never abort the loop on one patch
                logger.error(
                    "Unexpected error validating %s: %s",
                    patch_candidate.patch_id,
                    validation_error,
                )
                results.append(
                    RepairAttemptResult(
                        bug=bug,
                        patch=patch_candidate,
                        status=RepairStatus.FAILED,
                        validation_summary=(
                            f"Unexpected validation error: {validation_error}"
                        ),
                    )
                )
                candidates_validated += 1
                break

            results.append(result)
            candidates_validated += 1
            previous_diff_text = extracted_patch.diff_text

            if result.status == RepairStatus.PLAUSIBLE:
                break

            feedback_message = build_test_failure_feedback_message(
                patch_candidate.metadata.get("raw_test_output", ""),
                iteration_index=iteration_index + 1,
                max_iterations=self._repair_config.max_iterations,
                passed_count=patch_candidate.metadata.get("passed_count", 0),
                failed_count=patch_candidate.metadata.get("failed_count", 0),
                error_count=patch_candidate.metadata.get("error_count", 0),
                failure_kind=patch_candidate.metadata.get("failure_kind", "trigger"),
            )
            messages.append({"role": "assistant", "content": llm_response_text})
            messages.append({"role": "user", "content": feedback_message})

        return results, candidates_validated

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _generate_patches_for_location(
        self,
        bug: BugIdentifier,
        checkout: CheckoutResult,
        location: RankedLocation,
    ) -> list[PatchCandidate]:
        """Query the LLM up to max_patch_count times for one suspicious location."""
        prepared_prompt = self._build_location_prompt(bug, checkout, location)
        if prepared_prompt is None:
            return []

        source_file_path = prepared_prompt.source_file_path
        function_start_line = prepared_prompt.function_start_line
        function_end_line = prepared_prompt.function_end_line
        messages = prepared_prompt.messages

        logger.info(
            "Querying LLM for %s:%d (rank %d, score %s) — up to %d attempt(s)",
            source_file_path.name,
            location.line,
            location.rank,
            location.score,
            self._repair_config.max_patch_count,
        )

        location_patch_candidates: list[PatchCandidate] = []

        for attempt_index in range(self._repair_config.max_patch_count):
            try:
                llm_response_text = self._llm_client.complete(messages)
            except APRFrameworkError as llm_error:
                logger.warning(
                    "LLM call failed for %s:%d attempt %d: %s",
                    source_file_path.name,
                    location.line,
                    attempt_index,
                    llm_error,
                )
                continue

            extracted_patch = extract_patch_with_source(
                llm_response_text,
                source_file_path,
                function_start_line,
                function_end_line,
            )

            if extracted_patch is None:
                logger.warning(
                    "No valid diff extracted from LLM response for %s:%d attempt %d",
                    source_file_path.name,
                    location.line,
                    attempt_index,
                )
                continue

            patch_candidate = PatchCandidate(
                bug=bug,
                patch_id=f"llm-{location.rank}-{attempt_index}",
                summary=(
                    f"LLM patch for {location.file_path}:{location.line} "
                    f"(attempt {attempt_index + 1})"
                ),
                diff_text=extracted_patch.diff_text,
                metadata={
                    "location_rank": location.rank,
                    "location_score": location.score,
                    "llm_response": llm_response_text,
                    "model": self._repair_config.model_name,
                    # Used by run_validation_loop for progress logging
                    "source_path": str(source_file_path),
                    "target_line": location.line,
                    # Full patched file — enables the diff-level correctness check
                    # (is_correct_patch) to run for LLM patches, as it does for
                    # template patches. Omitted from JSON output by RepairEvaluationRunner.
                    "patched_source": extracted_patch.patched_source,
                },
            )
            if prepared_prompt.retrieval_trace is not None:
                patch_candidate.metadata["retrieval_trace"] = (
                    prepared_prompt.retrieval_trace
                )
            location_patch_candidates.append(patch_candidate)

        logger.info(
            "Generated %d valid patch candidate(s) for %s:%d",
            len(location_patch_candidates),
            source_file_path.name,
            location.line,
        )
        return location_patch_candidates

    def _build_location_prompt(
        self,
        bug: BugIdentifier,
        checkout: CheckoutResult,
        location: RankedLocation,
    ) -> _LocationPrompt | None:
        """Resolve the source file, extract the enclosing function, and build the
        initial ``[system, user]`` message list for one location.

        Shared by the single-shot (:meth:`_generate_patches_for_location`) and
        iterative (:meth:`_run_conversation_for_location`) paths so both apply the
        same enrichment. Returns None when the location cannot be prompted — no
        line number, unresolvable path, or function-extraction failure.
        """
        if location.line is None:
            logger.warning(
                "Location %s has no line number — skipping", location.file_path
            )
            return None

        source_file_path = self._resolve_source_file_path(location, checkout)
        if source_file_path is None:
            logger.warning(
                "Cannot resolve source path for %s — skipping", location.file_path
            )
            return None

        try:
            function_source_text, function_start_line, function_end_line = (
                extract_function_source(source_file_path, location.line)
            )
        except Exception as extraction_error:
            logger.error(
                "Function extraction failed for %s:%d: %s",
                source_file_path,
                location.line,
                extraction_error,
            )
            return None

        failing_test_context = self._failing_test_context_for(checkout)
        messages = build_repair_prompt(
            location,
            function_source_text,
            function_start_line,
            system_message_text=self._system_message_text,
            few_shot_examples=self._few_shot_examples_for(bug),
            failing_test_source=failing_test_context.failing_test_source,
            error_traceback=failing_test_context.error_traceback,
            retrieval_instructions=self._retrieval_instructions_text,
        )
        retrieval_trace: RetrievalTrace | None = None
        if self._repair_config.retrieval_budget > 0:
            retrieval_trace = run_retrieval_loop(
                self._llm_client,
                messages,
                checkout,
                self._repair_config.retrieval_budget,
            )
            logger.info(
                "Retrieval for %s:%d stopped with %s after %d step(s)",
                source_file_path.name,
                location.line,
                retrieval_trace.stop_reason,
                retrieval_trace.step_count,
            )
        return _LocationPrompt(
            source_file_path=source_file_path,
            function_start_line=function_start_line,
            function_end_line=function_end_line,
            messages=messages,
            retrieval_trace=retrieval_trace,
        )

    def _regression_context(self, checkout: CheckoutResult) -> RegressionContext:
        """Return the regression baseline, establishing it once on first use."""
        if self._regression is None:
            self._regression = build_regression_context(
                self._adapter,
                checkout,
                enabled=self._repair_config.regression_check,
                timeout=self._repair_config.timeout_seconds,
            )
        return self._regression

    def _failing_test_context_for(self, checkout: CheckoutResult) -> FailingTestContext:
        """Return the failing-test context, gathering it once on first use."""
        if self._failing_test_context is None:
            self._failing_test_context = build_failing_test_context(
                self._adapter,
                checkout,
                enabled=self._repair_config.context_enrichment,
                timeout=self._repair_config.timeout_seconds,
            )
        return self._failing_test_context

    def _few_shot_examples_for(self, bug: BugIdentifier) -> str | None:
        """Return prepended few-shot examples, building them once on first use."""
        if not self._few_shot_ready:
            self._few_shot_examples = build_few_shot_examples(
                self._adapter,
                bug,
                few_shot_count=self._repair_config.few_shot_count,
            )
            self._few_shot_ready = True
        return self._few_shot_examples

    # TODO: If a third repair backend is added, extract this into a module-level
    # utility shared by all backends (currently duplicated from TemplateRepairAlgorithm).
    def _resolve_source_file_path(
        self, location: RankedLocation, checkout: CheckoutResult
    ) -> Path | None:
        """Resolve a RankedLocation's file_path to an absolute Path in the worktree."""
        file_path_str = location.file_path
        if not file_path_str:
            return None

        file_path = Path(file_path_str)

        if file_path.is_absolute():
            if file_path.exists():
                return file_path
            return None

        resolved_file_path = (checkout.worktree / file_path).resolve()
        if resolved_file_path.exists():
            return resolved_file_path

        # FauxPy sometimes emits a leading "./" component — strip and retry.
        parts = file_path.parts
        if parts and parts[0] in (".", "./"):
            stripped_file_path = (
                Path(*parts[1:]) if len(parts) > 1 else Path(file_path_str)
            )
            worktree_stripped_path = (checkout.worktree / stripped_file_path).resolve()
            if worktree_stripped_path.exists():
                return worktree_stripped_path

        logger.warning(
            "Source file not found: %s (worktree: %s)", file_path_str, checkout.worktree
        )
        return None


def _classify_failure_kind(
    is_plausible: bool, trigger_test_run_result: TestRunResult
) -> str:
    """Classify why a validated patch was not plausible, for iterative feedback.

    ``apply_patch_and_validate`` returns only the *trigger* test run. When a patch
    makes the bug-triggering test pass but breaks a previously-passing test, that
    trigger run shows all-green — so its counts/traceback tell the model nothing
    about the real failure. This distinguishes the two cases so the feedback turn
    can be honest:

      - "none"       — the patch was plausible (no feedback needed).
      - "regression" — the trigger test passed but the patch broke another test.
      - "trigger"    — the bug's own test still fails (trigger output is useful).
    """
    if is_plausible:
        return "none"
    trigger_passed = (
        trigger_test_run_result.return_code == 0
        and trigger_test_run_result.failed_count == 0
        and trigger_test_run_result.error_count == 0
        and trigger_test_run_result.passed_count > 0
    )
    return "regression" if trigger_passed else "trigger"


def _parse_source_file_path_from_diff(diff_text: str) -> Path | None:
    """Extract the absolute source file path from the first '--- ' line of a diff."""
    for diff_line in diff_text.splitlines():
        if diff_line.startswith("--- "):
            # Strip the '--- ' prefix; handle optional tab-delimited timestamp.
            raw_path = diff_line[4:].split("\t")[0].strip()
            if raw_path:
                return Path(raw_path)
    return None
