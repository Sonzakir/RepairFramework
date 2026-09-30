"""Apply a patch candidate to the worktree, run tests, and revert unconditionally."""

import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

from apr_framework.benchmarks.bugsinpy import BugsInPyAdapter
from apr_framework.core.models import CheckoutResult, PatchCandidate, TestRunResult
from apr_framework.repair.regression import RegressionContext, parse_failing_test_ids
from apr_framework.repair.template.config import TemplateRepairConfig

logger = logging.getLogger(__name__)


@contextmanager
def patched_file(source_path: Path, new_source: str) -> Generator[None, None, None]:
    """Apply new_source to source_path, yield, then restore the original unconditionally."""
    original = source_path.read_text(encoding="utf-8")
    try:
        source_path.write_text(new_source, encoding="utf-8")
        yield
    finally:
        source_path.write_text(original, encoding="utf-8")


def validate_patch(
    candidate: PatchCandidate,
    checkout: CheckoutResult,
    adapter: BugsInPyAdapter,
    config: TemplateRepairConfig,
    regression: RegressionContext | None = None,
) -> tuple[PatchCandidate, bool]:
    """Apply candidate to the worktree, run tests, revert, and return plausibility.

    Plausibility has two halves :

    1. *The failing test now passes.* The bug's trigger test command must exit
       cleanly (``return_code == 0``), report zero failures and zero errors, and run
       at least one passing test. The ``return_code`` and ``passed_count > 0`` guards
       close a false-positive hole: if a patch breaks import/collection so the runner
       aborts before producing a summary line, the parsed failure/error counts are
       both 0 — which a bare "failed == 0 and error == 0" check would wrongly treat
       as plausible.
    2. *No previously passing test is broken.* When a ``regression`` context is
       supplied and enabled, the bug's whole regression suite is run with the patch
       applied and the patch is rejected if its set of failing tests is not a subset
       of the baseline failing set (i.e. it introduced a new failure).

    The patched_file context manager guarantees the source file is always restored
    to its original content even if an exception occurs during testing.

    Args:
        candidate:  Patch candidate whose metadata["patched_source"] holds the
                    modified file content and metadata["source_path"] the target.
        checkout:   Checked-out worktree to test against.
        adapter:    BugsInPyAdapter used to invoke the test suite.
        config:     Repair configuration (supplies the per-test timeout).
        regression: Optional regression baseline; when enabled, adds the
                    no-new-failures half of the plausibility check.

    Returns:
        (updated_candidate, is_plausible) — is_plausible is True iff the trigger test
        passes and (when enabled) no regression is introduced.
    """
    patched_source: str | None = candidate.metadata.get("patched_source")
    source_path_str: str | None = candidate.metadata.get("source_path")

    if patched_source is None or source_path_str is None:
        logger.warning(
            "Candidate %s is missing patched_source or source_path in metadata — skipping",
            candidate.patch_id,
        )
        return candidate, False

    source_path = Path(source_path_str)
    if not source_path.exists():
        logger.warning(
            "Source path %s does not exist — skipping candidate %s",
            source_path,
            candidate.patch_id,
        )
        return candidate, False

    test_result: TestRunResult | None = None
    is_plausible = False
    trigger_passed = False
    regression_ran = False
    regression_ok = True
    new_failures: list[str] = []

    with patched_file(source_path, patched_source):
        try:
            test_result = adapter.run_tests(checkout, timeout=config.timeout_per_test)
        except Exception as exc:
            logger.error(
                "Test run failed for candidate %s: %s", candidate.patch_id, exc
            )
            return candidate, False

        # Half 1 — the failing test now passes. Exit cleanly, zero failures/errors,
        # and at least one passing test. The return-code and passed-count guards
        # reject runs that aborted before producing a summary (e.g. the patch broke
        # collection), which would otherwise look like "0 failed, 0 error".
        trigger_passed = (
            test_result.return_code == 0
            and test_result.failed_count == 0
            and test_result.error_count == 0
            and test_result.passed_count > 0
        )

        # Half 2 — no previously passing test is broken. Only worth running when the
        # trigger already passes; the patch's regression failing set must be a subset
        # of the baseline failing set (no new failures).
        if trigger_passed and regression is not None and regression.enabled:
            try:
                regression_result = adapter.run_tests(
                    checkout,
                    timeout=config.timeout_per_test,
                    command=regression.command,
                )
            except Exception as exc:
                logger.error(
                    "Regression run failed for candidate %s: %s",
                    candidate.patch_id,
                    exc,
                )
                return candidate, False
            regression_ran = True
            patched_suite_failing_tests = parse_failing_test_ids(
                regression_result.raw_output
            )
            new_failures = sorted(
                patched_suite_failing_tests - regression.baseline_failing
            )
            regression_ok = not new_failures

        is_plausible = trigger_passed and regression_ok

        if not is_plausible:
            logger.debug(
                "candidate %s not plausible (rc=%d, passed=%d, failed=%d, error=%d, "
                "trigger_passed=%s, regression_ok=%s, new_failures=%s)",
                candidate.patch_id,
                test_result.return_code,
                test_result.passed_count,
                test_result.failed_count,
                test_result.error_count,
                trigger_passed,
                regression_ok,
                new_failures,
            )

    # Attach the test outcome to the candidate's metadata for reporting.
    updated_metadata = dict(candidate.metadata)
    if test_result is not None:
        updated_metadata["test_passed_count"] = test_result.passed_count
        updated_metadata["test_failed_count"] = test_result.failed_count
        updated_metadata["test_error_count"] = test_result.error_count
        updated_metadata["test_total_count"] = test_result.total_count
        updated_metadata["test_return_code"] = test_result.return_code
        updated_metadata["is_plausible"] = is_plausible
        updated_metadata["trigger_passed"] = trigger_passed
        updated_metadata["regression_checked"] = regression_ran
        if regression_ran:
            updated_metadata["regression_new_failures"] = new_failures

    updated_candidate = PatchCandidate(
        bug=candidate.bug,
        patch_id=candidate.patch_id,
        summary=candidate.summary,
        diff_text=candidate.diff_text,
        metadata=updated_metadata,
    )
    return updated_candidate, is_plausible
