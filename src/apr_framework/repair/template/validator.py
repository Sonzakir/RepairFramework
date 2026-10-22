"""Apply a template patch candidate to the worktree, run tests, and revert.

Template candidates carry the whole patched file (``metadata["patched_source"]``)
rather than an applicable diff, so this module supplies the write/restore pair and
delegates everything else — integrity screening, both plausibility halves,
unconditional restoration and evaluator verification — to the shared
``apply_patch_and_validate``.
"""

import logging
from pathlib import Path

from apr_framework.benchmarks.bugsinpy import BugsInPyAdapter
from apr_framework.core.models import CheckoutResult, PatchCandidate, TestRunResult
from apr_framework.repair.evaluator_integrity import EvaluatorGuard
from apr_framework.repair.patch_applier import (
    ValidationOutcome,
    apply_patch_and_validate,
)
from apr_framework.repair.regression import RegressionContext
from apr_framework.repair.template.config import TemplateRepairConfig

logger = logging.getLogger(__name__)


def validate_patch(
    candidate: PatchCandidate,
    checkout: CheckoutResult,
    adapter: BugsInPyAdapter,
    config: TemplateRepairConfig,
    evaluator_guard: EvaluatorGuard,
    regression: RegressionContext | None = None,
) -> tuple[PatchCandidate, ValidationOutcome | None]:
    """Apply candidate to the worktree, run tests, revert, and return the outcome.

    Plausibility (see ``apply_patch_and_validate``) requires the candidate to pass
    evaluator-integrity screening, the trigger test and — when ``regression`` is
    enabled — the no-new-failures regression check. The target file is restored
    unconditionally, and evaluator files are verified against their snapshot
    afterwards.

    Args:
        candidate:       Patch candidate whose metadata["patched_source"] holds the
                         modified file content and metadata["source_path"] the target.
        checkout:        Checked-out worktree to test against.
        adapter:         BugsInPyAdapter used to invoke the test suite.
        config:          Repair configuration (supplies the per-test timeout).
        evaluator_guard: The checkout's evaluator guard.
        regression:      Optional regression baseline; when enabled, adds the
                         no-new-failures half of the plausibility check.

    Returns:
        (updated_candidate, outcome) — the candidate carries its test counts and
        validation facts in metadata; outcome is None when the candidate could
        not be validated at all (missing metadata or target file).
    """
    patched_source: str | None = candidate.metadata.get("patched_source")
    source_path_str: str | None = candidate.metadata.get("source_path")

    if patched_source is None or source_path_str is None:
        logger.warning(
            "Candidate %s is missing patched_source or source_path in metadata — skipping",
            candidate.patch_id,
        )
        return candidate, None

    source_path = Path(source_path_str)
    if not source_path.exists():
        logger.warning(
            "Source path %s does not exist — skipping candidate %s",
            source_path,
            candidate.patch_id,
        )
        return candidate, None

    original_source = source_path.read_text(encoding="utf-8")

    def apply_fn() -> None:
        source_path.write_text(patched_source, encoding="utf-8")

    def restore_fn() -> None:
        source_path.write_text(original_source, encoding="utf-8")

    outcome = apply_patch_and_validate(
        apply_fn,
        restore_fn,
        adapter,
        checkout,
        regression or RegressionContext(enabled=False),
        config.timeout_per_test,
        evaluator_guard=evaluator_guard,
        diff_text=candidate.diff_text,
        touched_paths=(source_path,),
    )
    if not outcome.is_plausible:
        logger.debug(
            "candidate %s not plausible (failure_kind=%s, integrity=%s, "
            "new_failures=%s)",
            candidate.patch_id,
            outcome.failure_kind,
            outcome.integrity.integrity,
            outcome.regression_new_failures,
        )

    return _candidate_with_test_counts(candidate, outcome), outcome


def _candidate_with_test_counts(
    candidate: PatchCandidate, outcome: ValidationOutcome
) -> PatchCandidate:
    """Return a copy of the candidate carrying its test outcome for reporting."""
    test_result: TestRunResult = outcome.trigger_test_run_result
    updated_metadata = dict(candidate.metadata)
    updated_metadata["test_passed_count"] = test_result.passed_count
    updated_metadata["test_failed_count"] = test_result.failed_count
    updated_metadata["test_error_count"] = test_result.error_count
    updated_metadata["test_total_count"] = test_result.total_count
    updated_metadata["test_return_code"] = test_result.return_code
    updated_metadata["is_plausible"] = outcome.is_plausible
    return PatchCandidate(
        bug=candidate.bug,
        patch_id=candidate.patch_id,
        summary=candidate.summary,
        diff_text=candidate.diff_text,
        metadata=updated_metadata,
    )
