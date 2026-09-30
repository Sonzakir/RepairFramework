"""Regression component of the plausibility check (Assignment 3, Task 2).

The Task 2 plausibility definition has two halves: *the failing test now passes*
**and** *no previously passing test is broken*. BugsInPy's ``run_test.sh`` usually
contains only the bug-triggering test, so running it alone verifies the first half
but not the second. This module adds the regression half.

Approach — compare *sets of failing tests*, not counts:

  1. Once per repair run, run the bug's whole regression suite (the ``test_file``
     from ``bugsinpy_bug.info``) on the unpatched checkout and record the set of
     failing test ids — the *baseline failing set* (which includes the trigger).
  2. For a candidate that already makes the trigger test pass, run the same suite
     with the patch applied and record its failing set.
  3. The patch passes the regression check iff its failing set is a **subset** of
     the baseline failing set — i.e. it introduces no new failure.

Comparing sets (rather than failure counts) is what makes this correct: a patch
that fixes the trigger but breaks a different, previously-passing test produces the
same failure *count* as the baseline, yet its failing *set* is not a subset, so it
is correctly rejected.
"""

import logging
import re
from dataclasses import dataclass

from apr_framework.benchmarks.bugsinpy import BugsInPyAdapter
from apr_framework.core.models import CheckoutResult

logger = logging.getLogger(__name__)

# pytest:   "FAILED tests/test_x.py::test_a - AssertionError" / "ERROR tests/..."
# The negative lookahead skips unittest's "FAILED (failures=1, errors=1)" summary.
_PYTEST_FAIL = re.compile(r"^(?:FAILED|ERROR)\s+(?!\()(\S+)", re.MULTILINE)
# unittest: "FAIL: test_a (tests.test_x.Case)" / "ERROR: test_a (tests.test_x.Case)"
_UNITTEST_FAIL = re.compile(r"^(?:FAIL|ERROR):\s+(\w+)\s+\(([\w.]+)\)", re.MULTILINE)


@dataclass(frozen=True)
class RegressionContext:
    """Baseline state needed to run the regression half of plausibility.

    Fields:
        enabled:          Whether the regression check should run. False when it was
            switched off or could not be established (e.g. no derivable suite); in
            that case plausibility falls back to the trigger-test result only.
        command:          The test command that runs the whole regression suite.
        baseline_failing: Failing test ids on the *unpatched* suite run.
    """

    enabled: bool
    command: str = ""
    baseline_failing: frozenset[str] = frozenset()


def parse_failing_test_ids(raw_output: str) -> set[str]:
    """Extract the set of failing/errored test identifiers from test output.

    Handles both pytest (``FAILED``/``ERROR <nodeid>``) and unittest
    (``FAIL:``/``ERROR: <name> (<dotted.path>)``). Ids are returned in a normalized,
    parser-stable form so baseline and patched runs compare cleanly.
    """
    failing: set[str] = set()
    for node_id in _PYTEST_FAIL.findall(raw_output):
        failing.add(node_id.rstrip(":"))
    for name, dotted in _UNITTEST_FAIL.findall(raw_output):
        # Normalize to "<dotted.path>.<name>", de-duplicating when newer unittest
        # already appends the test name inside the parentheses.
        failing.add(dotted if dotted.endswith("." + name) else f"{dotted}.{name}")
    return failing


def derive_regression_command(
    trigger_command: str, test_file: str | None
) -> str | None:
    """Build a command that runs the whole regression suite for the bug.

    Broadens the bug's single-test trigger command to the entire ``test_file``,
    keeping the same framework (pytest vs unittest). Returns None when no suite can
    be derived (e.g. unknown framework or missing ``test_file``).
    """
    if not test_file:
        return None

    if "pytest" in trigger_command:
        # -rfE makes pytest print a FAILED/ERROR line per test in the summary.
        return f"pytest -rfE -q {test_file}"

    if "unittest" in trigger_command:
        module = test_file.removesuffix(".py").replace("/", ".")
        return f"python -m unittest {module}"

    return None


def _read_checkout_file(checkout: CheckoutResult, name: str) -> str | None:
    path = checkout.worktree / name
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8")


def _read_test_file(checkout: CheckoutResult) -> str | None:
    info = _read_checkout_file(checkout, "bugsinpy_bug.info")
    if not info:
        return None
    match = re.search(r'test_file="([^"]+)"', info)
    return match.group(1) if match else None


def build_regression_context(
    adapter: BugsInPyAdapter,
    checkout: CheckoutResult,
    *,
    enabled: bool,
    timeout: float | None = None,
) -> RegressionContext:
    """Establish the regression baseline for a bug (run once per repair run).

    Reads the trigger command and ``test_file`` from the checkout, derives the
    whole-suite command, runs it on the unpatched checkout, and records the failing
    set. Degrades gracefully to a disabled context (with a warning) when the suite
    cannot be derived or the baseline run is unusable, so the pipeline never blocks
    on it.
    """
    if not enabled:
        return RegressionContext(enabled=False)

    trigger_command_content = _read_checkout_file(checkout, "bugsinpy_run_test.sh")
    test_file = _read_test_file(checkout)
    if not trigger_command_content:
        logger.warning(
            "Regression check disabled: no bugsinpy_run_test.sh in %s",
            checkout.worktree,
        )
        return RegressionContext(enabled=False)

    command = derive_regression_command(trigger_command_content, test_file)
    if command is None:
        logger.warning(
            "Regression check disabled: could not derive a suite command from "
            "trigger=%r test_file=%r",
            trigger_command_content.strip(),
            test_file,
        )
        return RegressionContext(enabled=False)

    logger.info("Establishing regression baseline via: %s", command)
    baseline_run_result = adapter.run_tests(checkout, timeout=timeout, command=command)
    baseline_failing = parse_failing_test_ids(baseline_run_result.raw_output)

    # If the baseline suite produced no parseable result at all (e.g. collection
    # crash on the buggy checkout), the subset test would be meaningless — disable.
    if baseline_run_result.passed_count == 0 and not baseline_failing:
        logger.warning(
            "Regression check disabled: baseline suite produced no usable results "
            "(rc=%d). Falling back to trigger-test plausibility only.",
            baseline_run_result.return_code,
        )
        return RegressionContext(enabled=False)

    logger.info(
        "Regression baseline established: %d failing test(s) on the buggy suite.",
        len(baseline_failing),
    )
    return RegressionContext(
        enabled=True,
        command=command,
        baseline_failing=frozenset(baseline_failing),
    )
