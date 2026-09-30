"""Shared apply → run tests → restore infrastructure for repair backends.

Design decision:
    Patch *application* is backend-specific — each backend provides its own
    ``apply_fn`` and ``restore_fn`` callables that close over whatever state
    they need (file paths, original bytes, etc.).  Patch *validation* (test
    execution, regression check, and unconditional file restoration) is shared
    infrastructure here.  New backends should call ``apply_patch_and_validate``
    rather than duplicating the try/finally logic.

    Template repair predates this helper and is left unchanged; it manages
    apply/restore via its own ``patched_file`` context manager in
    ``repair/template/validator.py``.
"""

import logging
from typing import Callable

from apr_framework.benchmarks.bugsinpy import BugsInPyAdapter
from apr_framework.core.models import BugIdentifier, CheckoutResult, TestRunResult
from apr_framework.repair.regression import RegressionContext, parse_failing_test_ids

logger = logging.getLogger(__name__)


def apply_patch_and_validate(
    apply_fn: Callable[[], None],
    restore_fn: Callable[[], None],
    adapter: BugsInPyAdapter,
    checkout: CheckoutResult,
    regression_context: RegressionContext,
    timeout_seconds: int,
) -> tuple[bool, TestRunResult]:
    """Apply a patch, run the test suite, restore the file unconditionally.

    Plausibility mirrors the two-half definition in template/validator.py:

    Half 1 — trigger test passes:
        The adapter's default test command exits with return_code == 0,
        reports zero failures and zero errors, and ran at least one passing
        test.  The return_code and passed_count > 0 guards reject runs where
        the patch broke import/collection (which would otherwise show 0
        failures and 0 errors while actually being broken).

    Half 2 — no regression (only when regression_context.enabled is True):
        The full regression suite is run with the patch applied.  The patch
        is rejected if its failing-test set is not a subset of the baseline
        failing set, i.e. if it introduced any new failure.

    restore_fn() is called inside a ``finally`` block and is therefore
    unconditional — it runs even if apply_fn(), the test suite, or any other
    step raises an exception.

    Args:
        apply_fn:           Zero-argument callable that writes the patched
                            file to disk.
        restore_fn:         Zero-argument callable that writes the original
                            file content back to disk.
        adapter:            BugsInPyAdapter used to invoke the test suite.
        checkout:           Checked-out worktree to test against.
        regression_context: Baseline state for the regression check; pass a
                            disabled context to skip the second half.
        timeout_seconds:    Wall-clock seconds allowed per test-suite call.

    Returns:
        (is_plausible, trigger_test_run_result)
        is_plausible is True only when both halves of the plausibility
        definition are satisfied.  trigger_test_run_result is the result of
        the trigger-test run (or a synthetic error result when apply_fn or
        the test runner raised before a result could be obtained).
    """
    try:
        try:
            apply_fn()
        except Exception as apply_error:
            logger.error("apply_fn raised an exception: %s", apply_error)
            return False, _make_error_result(checkout.bug)

        # Half 1 — trigger test
        try:
            trigger_test_run_result = adapter.run_tests(
                checkout, timeout=timeout_seconds
            )
        except Exception as test_error:
            logger.error("Trigger test run raised an exception: %s", test_error)
            return False, _make_error_result(checkout.bug)

        trigger_passed = (
            trigger_test_run_result.return_code == 0
            and trigger_test_run_result.failed_count == 0
            and trigger_test_run_result.error_count == 0
            and trigger_test_run_result.passed_count > 0
        )

        # Half 2 — regression check (skipped when trigger fails or context is disabled)
        regression_ok = True
        if trigger_passed and regression_context.enabled:
            try:
                regression_run_result = adapter.run_tests(
                    checkout,
                    timeout=timeout_seconds,
                    command=regression_context.command,
                )
            except Exception as regression_error:
                logger.error(
                    "Regression test run raised an exception: %s", regression_error
                )
                return False, trigger_test_run_result

            patched_suite_failing_tests = parse_failing_test_ids(
                regression_run_result.raw_output
            )
            regression_ok = (
                patched_suite_failing_tests <= regression_context.baseline_failing
            )
            if not regression_ok:
                new_failures = sorted(
                    patched_suite_failing_tests - regression_context.baseline_failing
                )
                logger.debug("Regression check failed — new failures: %s", new_failures)

        return trigger_passed and regression_ok, trigger_test_run_result

    finally:
        restore_fn()


def _make_error_result(bug: BugIdentifier) -> TestRunResult:
    """Return a minimal TestRunResult signalling that apply or testing failed."""
    return TestRunResult(bug=bug, results=[], return_code=-1)
