"""Shared screen → apply → run tests → restore → verify infrastructure.

Design decision:
    Patch *application* is backend-specific — each backend provides its own
    ``apply_fn`` and ``restore_fn`` callables that close over whatever state
    they need (file paths, original bytes, etc.).  Patch *validation* is shared
    infrastructure here: evaluator-integrity screening, test execution, the
    regression check, unconditional file restoration and the evaluator-file
    verification.  Every backend (template, LLM, and the challenge replay of
    external patches) calls ``apply_patch_and_validate`` rather than duplicating
    the try/finally logic, so they all apply the same integrity rules.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from apr_framework.benchmarks.bugsinpy import BugsInPyAdapter
from apr_framework.core.models import (
    BugIdentifier,
    CheckoutResult,
    PatchCandidate,
    RepairAttemptResult,
    RepairStatus,
    TestRunResult,
)
from apr_framework.repair.evaluator_integrity import (
    EvaluatorGuard,
    IntegrityVerdict,
    purge_bytecode_caches,
)
from apr_framework.repair.unified_diff import touched_relative_paths
from apr_framework.repair.regression import (
    RegressionContext,
    is_trusted_suite_run,
    parse_failing_test_ids,
)

logger = logging.getLogger(__name__)


@dataclass
class ValidationOutcome:
    """Everything one candidate's validation established.

    Fields:
        is_plausible:            Integrity ok, trigger test passes and (when the
            regression check is enabled) no new regression failure.
        trigger_test_run_result: The trigger-test run, or a synthetic error result
            when the candidate was rejected, failed to apply, or the runner raised.
        trigger_passed:          Whether the trigger test passed.
        regression_checked:      True only when a trusted patched regression run
            was compared with the baseline. False when the baseline is disabled;
            reports must then show that plausibility rests on the trigger alone.
        regression_untrusted:    The patched regression run produced no usable
            result (timeout, collection crash, nothing executed): not plausible.
        regression_new_failures: Failing test ids absent from the baseline.
        integrity:               Integrity verdict; a rejection is never plausible.
        applied:                 Whether ``apply_fn`` succeeded (so the checks ran
            against the patched checkout).
        apply_error:             Why ``apply_fn`` failed, when it did.
    """

    is_plausible: bool
    trigger_test_run_result: TestRunResult
    trigger_passed: bool = False
    regression_checked: bool = False
    regression_untrusted: bool = False
    regression_new_failures: list[str] = field(default_factory=list)
    integrity: IntegrityVerdict = field(default_factory=IntegrityVerdict)
    applied: bool = False
    apply_error: str = ""

    @property
    def failure_kind(self) -> str:
        """``none``, ``integrity``, ``trigger`` or ``regression``."""
        if self.is_plausible:
            return "none"
        if self.integrity.is_rejected:
            return "integrity"
        return "regression" if self.trigger_passed else "trigger"

    def as_metadata(self) -> dict[str, object]:
        """Return the validation facts stored on the candidate and in reports."""
        return {
            "trigger_passed": self.trigger_passed,
            "regression_checked": self.regression_checked,
            "regression_untrusted": self.regression_untrusted,
            "regression_new_failures": list(self.regression_new_failures),
            **self.integrity.as_metadata(),
        }

    def describe_failure(self, patch_id: str) -> str:
        """Return the validation summary for a candidate that is not plausible."""
        if self.integrity.is_rejected:
            return (
                f"Patch {patch_id} rejected ({self.integrity.integrity}): "
                f"{self.integrity.detail}."
            )
        if self.regression_untrusted:
            return (
                f"Patch {patch_id} passed the trigger test, but the regression suite "
                "produced no usable result, so plausibility cannot be established."
            )
        if self.regression_new_failures:
            return (
                f"Patch {patch_id} passed the trigger test but broke previously "
                f"passing tests: {', '.join(self.regression_new_failures)}."
            )
        run_result = self.trigger_test_run_result
        return (
            f"Patch {patch_id} did not fix all tests "
            f"(passed={run_result.passed_count}, failed={run_result.failed_count}, "
            f"errors={run_result.error_count})."
        )

    def describe_success(self, patch_id: str) -> str:
        """Return the validation summary for a plausible candidate."""
        if self.regression_checked:
            return f"All tests passed with patch {patch_id}."
        return (
            f"Trigger test passed with patch {patch_id} (regression suite not checked)."
        )


def apply_patch_and_validate(
    apply_fn: Callable[[], None],
    restore_fn: Callable[[], None],
    adapter: BugsInPyAdapter,
    checkout: CheckoutResult,
    regression_context: RegressionContext,
    timeout_seconds: int,
    *,
    evaluator_guard: EvaluatorGuard,
    diff_text: str,
    touched_paths: tuple[Path | str, ...] = (),
    run_while_applied: Callable[[], None] | None = None,
) -> ValidationOutcome:
    """Screen a candidate, apply it, run the checks, restore, then verify evaluators.

    Integrity — screened before anything is applied:
        A candidate whose diff or explicit target files (``touched_paths``) touch
        an evaluator file, or whose added source detects the test runner, is
        rejected with a structured ``integrity`` verdict and never applied.

    Half 1 — trigger test passes:
        The adapter's default test command exits with return_code == 0,
        reports zero failures and zero errors, and ran at least one passing
        test.  The return_code and passed_count > 0 guards reject runs where
        the patch broke import/collection (which would otherwise show 0
        failures and 0 errors while actually being broken).

    Half 2 — no regression (only when regression_context.enabled is True):
        The full regression suite is run with the patch applied.  The patch
        is rejected if that run produced no usable result, or if its failing-test
        set is not a subset of the baseline failing set.

    restore_fn() is called inside a ``finally`` block and is therefore
    unconditional.  The bytecode caches of every touched file are purged after
    applying and after restoring, so no run can execute a stale cached version.
    Afterwards every locked evaluator file is compared with its snapshot; a change
    is restored and the candidate is rejected as ``evaluator_files_mutated``.

    Args:
        apply_fn:           Zero-argument callable that writes the patched
                            file(s) to disk.
        restore_fn:         Zero-argument callable that restores the original
                            content (and removes created files).
        adapter:            BugsInPyAdapter used to invoke the test suite.
        checkout:           Checked-out worktree to test against.
        regression_context: Baseline state for the regression check; pass a
                            disabled context to skip the second half.
        timeout_seconds:    Wall-clock seconds allowed per test-suite call.
        evaluator_guard:    The checkout's evaluator guard (snapshot taken once).
        diff_text:          The candidate's diff, screened for evaluator edits.
        touched_paths:      Files ``apply_fn`` writes, when not all of them are
                            named by the diff headers.
        run_while_applied:  Optional extra check run after both plausibility
                            halves, while the patch is still applied and the
                            evaluator files are still locked (the challenge
                            replay runs its witness here). Runs whatever the
                            plausibility verdict, as long as the patch applied.

    Returns:
        A ValidationOutcome; ``is_plausible`` is True only when the candidate
        passed integrity screening, both plausibility halves and verification.

    Raises:
        EvaluatorIntegrityError: evaluator files differ from the snapshot before
            the candidate is applied, or cannot be restored afterwards.
    """
    screening_verdict = evaluator_guard.screen_candidate(diff_text, touched_paths)
    if screening_verdict.is_rejected:
        logger.warning("Candidate rejected: %s", screening_verdict.detail)
        return ValidationOutcome(
            is_plausible=False,
            trigger_test_run_result=_make_error_result(checkout.bug),
            integrity=screening_verdict,
        )

    touched_files = _touched_files(checkout.worktree, diff_text, touched_paths)

    def apply_and_purge_bytecode() -> None:
        apply_fn()
        purge_bytecode_caches(touched_files)

    evaluator_guard.assert_evaluator_files_unchanged()
    try:
        outcome = _apply_and_run_checks(
            apply_and_purge_bytecode,
            adapter,
            checkout,
            regression_context,
            timeout_seconds,
        )
        if outcome.applied and run_while_applied is not None:
            run_while_applied()
    finally:
        restore_fn()
        purge_bytecode_caches(touched_files)

    mutation_verdict = evaluator_guard.verify_evaluator_files()
    if mutation_verdict.is_rejected:
        outcome.is_plausible = False
        outcome.integrity = mutation_verdict
    return outcome


def build_attempt_result(
    bug: BugIdentifier, candidate: PatchCandidate, outcome: ValidationOutcome
) -> RepairAttemptResult:
    """Stamp the outcome onto the candidate and return its RepairAttemptResult.

    REJECTED for integrity rejections, PLAUSIBLE when plausible, else FAILED.
    """
    candidate.metadata.update(outcome.as_metadata())
    if outcome.integrity.is_rejected:
        status = RepairStatus.REJECTED
        validation_summary = outcome.describe_failure(candidate.patch_id)
    elif outcome.is_plausible:
        logger.info("Plausible patch found: %s", candidate.patch_id)
        status = RepairStatus.PLAUSIBLE
        validation_summary = outcome.describe_success(candidate.patch_id)
    else:
        status = RepairStatus.FAILED
        validation_summary = outcome.describe_failure(candidate.patch_id)
    return RepairAttemptResult(
        bug=bug, patch=candidate, status=status, validation_summary=validation_summary
    )


def is_passing_trigger_run(trigger_test_run_result: TestRunResult) -> bool:
    """Report whether a trigger-test run counts as "the failing test now passes"."""
    return (
        trigger_test_run_result.return_code == 0
        and trigger_test_run_result.failed_count == 0
        and trigger_test_run_result.error_count == 0
        and trigger_test_run_result.passed_count > 0
    )


def _apply_and_run_checks(
    apply_fn: Callable[[], None],
    adapter: BugsInPyAdapter,
    checkout: CheckoutResult,
    regression_context: RegressionContext,
    timeout_seconds: int,
) -> ValidationOutcome:
    """Apply the candidate and run both plausibility halves (no restoration here)."""
    try:
        apply_fn()
    except Exception as apply_error:
        logger.error("apply_fn raised an exception: %s", apply_error)
        return ValidationOutcome(
            is_plausible=False,
            trigger_test_run_result=_make_error_result(checkout.bug),
            apply_error=str(apply_error),
        )

    outcome = _run_plausibility_checks(
        adapter, checkout, regression_context, timeout_seconds
    )
    outcome.applied = True
    return outcome


def _run_plausibility_checks(
    adapter: BugsInPyAdapter,
    checkout: CheckoutResult,
    regression_context: RegressionContext,
    timeout_seconds: int,
) -> ValidationOutcome:
    """Run the trigger test, then (if it passes) the regression comparison."""
    try:
        trigger_test_run_result = adapter.run_tests(checkout, timeout=timeout_seconds)
    except Exception as test_error:
        logger.error("Trigger test run raised an exception: %s", test_error)
        return ValidationOutcome(
            is_plausible=False, trigger_test_run_result=_make_error_result(checkout.bug)
        )

    if not is_passing_trigger_run(trigger_test_run_result):
        return ValidationOutcome(
            is_plausible=False, trigger_test_run_result=trigger_test_run_result
        )

    if not regression_context.enabled:
        return ValidationOutcome(
            is_plausible=True,
            trigger_test_run_result=trigger_test_run_result,
            trigger_passed=True,
            regression_checked=False,
        )

    return _compare_regression_suite_with_baseline(
        trigger_test_run_result, adapter, checkout, regression_context, timeout_seconds
    )


def _compare_regression_suite_with_baseline(
    trigger_test_run_result: TestRunResult,
    adapter: BugsInPyAdapter,
    checkout: CheckoutResult,
    regression_context: RegressionContext,
    timeout_seconds: int,
) -> ValidationOutcome:
    """Run the regression suite on the patched checkout and compare failing sets."""
    try:
        regression_run_result = adapter.run_tests(
            checkout,
            timeout=timeout_seconds,
            command=regression_context.command,
        )
    except Exception as regression_error:
        logger.error("Regression test run raised an exception: %s", regression_error)
        return ValidationOutcome(
            is_plausible=False,
            trigger_test_run_result=trigger_test_run_result,
            trigger_passed=True,
            regression_untrusted=True,
        )

    if not is_trusted_suite_run(regression_run_result):
        logger.warning(
            "Patched regression run produced no usable result (rc=%d)",
            regression_run_result.return_code,
        )
        return ValidationOutcome(
            is_plausible=False,
            trigger_test_run_result=trigger_test_run_result,
            trigger_passed=True,
            regression_untrusted=True,
        )

    patched_suite_failing_tests = parse_failing_test_ids(
        regression_run_result.raw_output
    )
    new_failures = sorted(
        patched_suite_failing_tests - regression_context.baseline_failing
    )
    if new_failures:
        logger.debug("Regression check failed — new failures: %s", new_failures)
    return ValidationOutcome(
        is_plausible=not new_failures,
        trigger_test_run_result=trigger_test_run_result,
        trigger_passed=True,
        regression_checked=True,
        regression_new_failures=new_failures,
    )


def _touched_files(
    worktree: Path, diff_text: str, touched_paths: tuple[Path | str, ...]
) -> list[Path]:
    """Every file the candidate writes: its diff's paths plus explicit targets."""
    touched_files = [
        worktree / relative_path
        for relative_path in touched_relative_paths(diff_text, worktree.name)
    ]
    touched_files.extend(
        Path(path) if Path(path).is_absolute() else worktree / path
        for path in touched_paths
    )
    return touched_files


def _make_error_result(bug: BugIdentifier) -> TestRunResult:
    """Return a minimal TestRunResult signalling that apply or testing failed."""
    return TestRunResult(bug=bug, results=[], return_code=-1)
