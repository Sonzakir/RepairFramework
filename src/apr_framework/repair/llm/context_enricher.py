"""Gathering of failing-test context to enrich LLM repair prompts.

The base LLM prompt (Task 1) shows the model only the buggy function and the
suspicious line. It never tells the model *what* is actually failing, so for bugs
whose fix cannot be inferred from clean-looking code alone the model can only
guess. This module gathers two extra pieces of context that make the failure
observable to the model:

  1. ``failing_test_source`` — the source of the bug-triggering test, so the model
     can see the behaviour the fix must satisfy.
  2. ``error_traceback``     — the traceback the trigger test produces on the
     *unpatched* checkout, so the model can see the concrete failure mode.

Every step here is best-effort: any failure to gather a piece degrades to ``None``
(logged at WARNING). When both are ``None`` the enrichment is invisible —
``build_repair_prompt`` renders exactly the same prompt as before. This keeps the
feature fully isolated from the un-enriched path and from the template backend.
"""

import ast
import logging
from dataclasses import dataclass
from pathlib import Path

from apr_framework.benchmarks.bugsinpy import BugsInPyAdapter
from apr_framework.core.exceptions import ConfigurationError
from apr_framework.core.models import CheckoutResult
from apr_framework.localization.fauxpy import load_pytest_targets
from apr_framework.repair.llm.traceback_utils import extract_last_traceback

logger = logging.getLogger(__name__)

_RUN_TEST_SCRIPT_NAME = "bugsinpy_run_test.sh"


@dataclass(frozen=True)
class FailingTestContext:
    """Best-effort failing-test context used to enrich the LLM repair prompt.

    Both fields are ``None`` when the corresponding piece could not be gathered, so
    a caller can forward them straight into ``build_repair_prompt`` without any
    branching — a ``None`` section is simply omitted from the rendered prompt.

    Fields:
        failing_test_source: Source of the bug-triggering test function, or None.
        error_traceback:     Traceback produced by the trigger test on the
                             unpatched checkout, or None.
    """

    failing_test_source: str | None = None
    error_traceback: str | None = None

    @property
    def is_empty(self) -> bool:
        return self.failing_test_source is None and self.error_traceback is None


def build_failing_test_context(
    adapter: BugsInPyAdapter,
    checkout: CheckoutResult,
    *,
    enabled: bool,
    timeout: float | None = None,
) -> FailingTestContext:
    """Gather the failing-test source and its traceback for one bug (best-effort).

    Runs the bug-triggering test once on the unpatched checkout to capture its
    traceback and reads the trigger test's source from the worktree. Intended to be
    called once per repair run and cached by the caller, since the trigger run is a
    real test invocation.

    Args:
        adapter:  BugsInPyAdapter used to run the trigger test.
        checkout: Checkout result whose worktree holds the test file and script.
        enabled:  When False, returns an empty context without any work (opt-out).
        timeout:  Optional wall-clock limit for the trigger-test run.

    Returns:
        A FailingTestContext; either field may be None if it could not be gathered.
    """
    if not enabled:
        return FailingTestContext()

    failing_test_source = _extract_failing_test_source(checkout)
    error_traceback = _capture_trigger_traceback(adapter, checkout, timeout)
    return FailingTestContext(
        failing_test_source=failing_test_source,
        error_traceback=error_traceback,
    )


# ---------------------------------------------------------------------------
# Failing-test source
# ---------------------------------------------------------------------------


def _extract_failing_test_source(checkout: CheckoutResult) -> str | None:
    """Read the source of the bug-triggering test function from the worktree."""
    first_pytest_target = _load_first_pytest_target(checkout)
    if first_pytest_target is None:
        return None

    test_file_relpath, _, node_id_suffix = first_pytest_target.partition("::")
    method_name = _parse_method_name_from_node_id(node_id_suffix)
    if method_name is None:
        logger.warning(
            "Failing-test target %r has no method component — skipping test source",
            first_pytest_target,
        )
        return None

    test_file_path = (checkout.worktree / test_file_relpath).resolve()
    return _extract_function_source_by_name(test_file_path, method_name)


def _load_first_pytest_target(checkout: CheckoutResult) -> str | None:
    """Return the first pytest node id declared by the bug's run_test.sh, or None."""
    run_test_script_path = checkout.worktree / _RUN_TEST_SCRIPT_NAME
    try:
        pytest_targets = load_pytest_targets(run_test_script_path)
    except ConfigurationError as target_error:
        logger.warning(
            "Cannot derive failing-test target from %s: %s",
            run_test_script_path,
            target_error,
        )
        return None
    return pytest_targets[0] if pytest_targets else None


def _parse_method_name_from_node_id(node_id_suffix: str) -> str | None:
    """Extract the test method name from a pytest node-id suffix (after the file).

    Handles ``Class::method`` and bare ``method`` forms and strips any pytest
    parametrization bracket (``method[param]`` → ``method``).
    """
    if not node_id_suffix:
        return None
    method_name = node_id_suffix.split("::")[-1].split("[")[0].strip()
    return method_name or None


def _extract_function_source_by_name(
    source_file_path: Path, function_name: str
) -> str | None:
    """Return the source text of the named top-level-or-nested function, or None."""
    try:
        source_text = source_file_path.read_text(encoding="utf-8")
    except OSError as read_error:
        logger.warning("Cannot read test file %s: %s", source_file_path, read_error)
        return None

    try:
        tree = ast.parse(source_text, filename=str(source_file_path))
    except SyntaxError as parse_error:
        logger.warning("Cannot parse test file %s: %s", source_file_path, parse_error)
        return None

    source_lines = source_text.splitlines(keepends=True)
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == function_name
        ):
            start_line: int = node.lineno
            end_line: int = node.end_lineno  # type: ignore[assignment]
            return "".join(source_lines[start_line - 1 : end_line])

    logger.warning(
        "Test method %r not found in %s — skipping test source",
        function_name,
        source_file_path,
    )
    return None


# ---------------------------------------------------------------------------
# Trigger-test traceback
# ---------------------------------------------------------------------------


def _capture_trigger_traceback(
    adapter: BugsInPyAdapter,
    checkout: CheckoutResult,
    timeout: float | None,
) -> str | None:
    """Run the bug's trigger test unpatched and extract its traceback, or None."""
    trigger_run_result = adapter.run_tests(checkout, timeout=timeout)
    error_traceback = extract_last_traceback(trigger_run_result.raw_output)
    if error_traceback is None:
        logger.warning(
            "No traceback found in trigger-test output for %s#%d — skipping traceback",
            checkout.bug.project,
            checkout.bug.bug_id,
        )
    return error_traceback
