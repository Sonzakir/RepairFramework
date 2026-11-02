"""Apply a fix or probe through the one validation path and run the repro test.

Fixes and probes are held as file states (``FileStates``: original and new bytes
per file), as in the Strict Repro audit. ``validate_file_states_with_repro``
writes them through ``apply_patch_and_validate``: the evaluator-integrity screen,
the oracle (trigger test or ``--regression-cmd``), the optional regression
comparison, unconditional restore, bytecode purge and evaluator verification all
come from there. The repro test runs in ``run_while_applied``, while the change is
still applied and the evaluator files are still locked.
"""

import difflib
from dataclasses import dataclass
from pathlib import Path

from apr_framework.challenge.multi_file_patch import (
    MultiFilePatch,
    without_evaluator_files,
)
from apr_framework.core.models import TestRunResult
from apr_framework.repair.evaluator_integrity import (
    EvaluatorGuard,
    classify_evaluator_path,
)
from apr_framework.repair.patch_applier import (
    ValidationOutcome,
    apply_patch_and_validate,
)
from apr_framework.repair.regression import RegressionContext
from apr_framework.repair.unified_diff import touched_relative_paths
from apr_framework.repro.models import FileStates, TestVersion
from apr_framework.repro.runner import TestCommandRunner

NO_REGRESSION_CHECK = RegressionContext(
    enabled=False, disabled_reason="not needed for this check"
)


@dataclass(frozen=True)
class FixUnderTest:
    """A fix as file states, with what was stripped from it.

    Fields:
        label:             ``developer fix`` or the patch file name.
        files:             Non-evaluator files the fix changes.
        stripped_paths:    Evaluator files the fix touched (dropped: BugsInPy
                           checkouts already carry the fixed tests; a user patch
                           is judged without its test edits).
        diff_text:         The fix as applied (evaluator sections removed).
    """

    label: str
    files: FileStates
    stripped_paths: tuple[str, ...]
    diff_text: str


def plan_fix_without_evaluator_files(
    label: str, diff_text: str, worktree: Path, declared_test_files: frozenset[str]
) -> FixUnderTest:
    """Plan a diff against the worktree, dropping every evaluator-file section."""

    def is_evaluator_path(relative_path: str) -> bool:
        return classify_evaluator_path(relative_path, declared_test_files) is not None

    source_only_diff = without_evaluator_files(
        diff_text, worktree.name, is_evaluator_path
    )
    all_paths = touched_relative_paths(diff_text, worktree.name)
    plan = MultiFilePatch.plan(source_only_diff, worktree)
    files: FileStates = {}
    for change in plan.changes:
        original_path = worktree / change.relative_path
        original = original_path.read_bytes() if original_path.is_file() else None
        files[change.relative_path] = (original, change.new_bytes)
    return FixUnderTest(
        label=label,
        files=files,
        stripped_paths=tuple(path for path in all_paths if is_evaluator_path(path)),
        diff_text=source_only_diff,
    )


def validate_file_states_with_repro(
    files: FileStates,
    runner: TestCommandRunner,
    guard: EvaluatorGuard,
    regression_context: RegressionContext,
    test: TestVersion,
    timeout_seconds: int,
) -> tuple[ValidationOutcome, TestRunResult | None]:
    """Apply ``files`` via the shared path; return its outcome and the repro run."""
    worktree = runner.checkout.worktree
    repro_runs: list[TestRunResult] = []

    def apply_new_states() -> None:
        for relative_path, (_, new_bytes) in files.items():
            write_file_state(worktree / relative_path, new_bytes)

    def restore_original_states() -> None:
        for relative_path, (original_bytes, _) in files.items():
            write_file_state(worktree / relative_path, original_bytes)

    outcome = apply_patch_and_validate(
        apply_new_states,
        restore_original_states,
        runner,  # the runner seam is shaped like the adapter this path drives
        runner.checkout,
        regression_context,
        timeout_seconds,
        evaluator_guard=guard,
        diff_text=render_file_states_diff(files),
        touched_paths=tuple(files),
        run_while_applied=lambda: repro_runs.append(
            runner.run_repro_test(test, timeout_seconds)
        ),
    )
    return outcome, (repro_runs[0] if repro_runs else None)


def write_file_state(path: Path, content: bytes | None) -> None:
    if content is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def render_file_states_diff(files: FileStates) -> str:
    """Unified diff of the file states (screened for evaluator edits, archived)."""
    sections: list[str] = []
    for relative_path, (original, new) in files.items():
        diff_lines = difflib.unified_diff(
            decode(original).splitlines(keepends=True),
            decode(new).splitlines(keepends=True),
            fromfile=f"a/{relative_path}",
            tofile=f"b/{relative_path}",
        )
        sections.extend(
            line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
            for line in diff_lines
        )
    return "".join(sections)


def decode(content: bytes | None) -> str:
    return "" if content is None else content.decode("utf-8", errors="surrogateescape")
