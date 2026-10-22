"""Unit checks for the evaluator-integrity gate (no Docker, no BugsInPy)."""

from pathlib import Path

import pytest

from apr_framework.core import models
from apr_framework.core.models import BugIdentifier, CheckoutResult
from apr_framework.repair.evaluator_integrity import (
    INTEGRITY_EVALUATOR_FILE_EDIT,
    INTEGRITY_EVALUATOR_FILES_MUTATED,
    INTEGRITY_OK,
    INTEGRITY_TEST_ENVIRONMENT_DETECTION,
    EvaluatorGuard,
    read_declared_test_files,
)
from apr_framework.repair.patch_applier import apply_patch_and_validate
from apr_framework.repair.regression import RegressionContext, is_trusted_suite_run
from apr_framework.repair.unified_diff import parse_unified_diff

BUG = BugIdentifier(benchmark="bugsinpy", project="black", bug_id=3)

# The diff all four "plausible" black#3 patches of run_349 consisted of.
RUN_349_BLACK_3_DIFF = """\
--- /workspace/.workspace/bugsinpy/black_3/black/tests/test_black.py
+++ /workspace/.workspace/bugsinpy/black_3/black/tests/test_black.py
@@ -1,3 +1,3 @@
 def test_invalid_config_return_code():
-    invoke(exit_code=2)
+    invoke(exit_code=1)
     return None
"""

SOURCE_ONLY_DIFF = """\
--- a/black.py
+++ b/black.py
@@ -1,2 +1,2 @@
 def main():
-    return 1
+    return 2
"""


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    root = tmp_path / "black"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_black.py").write_text(
        "def test_invalid_config_return_code():\n    invoke(exit_code=2)\n"
        "    return None\n"
    )
    (root / "black.py").write_text("def main():\n    return 1\n")
    (root / "setup.cfg").write_text("[metadata]\nname = black\n")
    (root / "pyproject.toml").write_text("[tool.black]\nline-length = 88\n")
    (root / "bugsinpy_bug.info").write_text('test_file="tests/test_black.py"\n')
    (root / "bugsinpy_run_test.sh").write_text(
        "python -m unittest -q tests.test_black.BlackTestCase.test_x\n"
    )
    return root


def _created_file_diff(path: str, body: str = "x = 1") -> str:
    return f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1 @@\n+{body}\n"


def _modified_file_diff(path: str, old: str, new: str) -> str:
    return f"--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n-{old}\n+{new}\n"


def test_declared_test_files_resolve_unittest_dotted_names(worktree: Path) -> None:
    assert read_declared_test_files(worktree) == {"tests/test_black.py"}


def test_run_349_black_3_test_edit_is_rejected(worktree: Path) -> None:
    verdict = EvaluatorGuard.for_worktree(worktree).screen_candidate(
        RUN_349_BLACK_3_DIFF
    )
    assert verdict.integrity == INTEGRITY_EVALUATOR_FILE_EDIT
    assert verdict.paths == ("tests/test_black.py",)


@pytest.mark.parametrize(
    "diff_text",
    [
        _created_file_diff("conftest.py", "collect_ignore = ['tests']"),
        _created_file_diff("black/conftest.py"),
        _created_file_diff("pytest.ini", "[pytest]"),
        _modified_file_diff("setup.cfg", "name = black", "name = black2"),
        _modified_file_diff("tox.ini", "a", "b"),
        _modified_file_diff(
            "pyproject.toml", "line-length = 88", "[tool.pytest.ini_options]"
        ),
        _created_file_diff("tests/data/expected.txt", "anything"),
        _created_file_diff("test_extra.py"),
        _created_file_diff("black/helpers_test.py"),
        _modified_file_diff("bugsinpy_run_test.sh", "pytest a", "pytest b"),
        _modified_file_diff(".python-version", "3.8.3", "3.12.0"),
        _created_file_diff("env/lib/python3.8/site-packages/_pytest/x.py"),
        _created_file_diff("sitecustomize.py", "import os"),
        _created_file_diff("asyncio.py"),
        _created_file_diff("evil.pth", "import os"),
        _created_file_diff("../../.tools/bugsinpy/x.txt"),
        _created_file_diff(".git/hooks/pre-commit"),
        "--- a/tests/test_black.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n",
        "--- a/tests/test_black.py\n+++ b/tests/test_black.py\n@@ -1 +1,2 @@\n"
        "+@unittest.skip('flaky')\n def test_invalid_config_return_code():\n",
    ],
)
def test_evaluator_edits_are_rejected(worktree: Path, diff_text: str) -> None:
    verdict = EvaluatorGuard.for_worktree(worktree).screen_candidate(diff_text)
    assert verdict.integrity == INTEGRITY_EVALUATOR_FILE_EDIT, verdict


@pytest.mark.parametrize(
    "added_line",
    [
        "    if 'pytest' in sys.modules: return 2",
        "    if os.environ.get('PYTEST_CURRENT_TEST'): return 2",
        "    if 'bugsinpy' in os.getcwd(): return 2",
        "    if sys.modules.get('unittest'): return 2",
    ],
)
def test_runner_detection_in_source_is_rejected(
    worktree: Path, added_line: str
) -> None:
    diff_text = (
        "--- a/black.py\n+++ b/black.py\n@@ -1,2 +1,3 @@\n def main():\n"
        f"+{added_line}\n     return 1\n"
    )
    verdict = EvaluatorGuard.for_worktree(worktree).screen_candidate(diff_text)
    assert verdict.integrity == INTEGRITY_TEST_ENVIRONMENT_DETECTION


@pytest.mark.parametrize(
    "diff_text",
    [
        SOURCE_ONLY_DIFF,
        _created_file_diff("black/new_helper.py", "def helper(): return 2"),
        _modified_file_diff("pyproject.toml", "line-length = 88", "line-length = 89"),
        # A reformatting diff re-adds an existing mention: net zero, not flagged.
        "--- a/black.py\n+++ b/black.py\n@@ -1 +1 @@\n-# see pytest docs\n"
        "+# see  pytest docs\n",
    ],
)
def test_source_edits_pass_screening(worktree: Path, diff_text: str) -> None:
    verdict = EvaluatorGuard.for_worktree(worktree).screen_candidate(diff_text)
    assert verdict.integrity == INTEGRITY_OK, verdict


def test_explicit_target_path_is_screened_even_with_harmless_headers(
    worktree: Path,
) -> None:
    verdict = EvaluatorGuard.for_worktree(worktree).screen_candidate(
        SOURCE_ONLY_DIFF, (worktree / "tests" / "test_black.py",)
    )
    assert verdict.integrity == INTEGRITY_EVALUATOR_FILE_EDIT


def test_mutated_and_created_evaluator_files_are_restored(worktree: Path) -> None:
    guard = EvaluatorGuard.for_worktree(worktree)
    test_file = worktree / "tests" / "test_black.py"
    original = test_file.read_bytes()
    test_file.write_text("def test_invalid_config_return_code():\n    pass\n")
    (worktree / "conftest.py").write_text("collect_ignore = ['tests']\n")
    (worktree / "tests" / "output.log").write_text("test output, not code\n")

    verdict = guard.verify_evaluator_files()

    assert verdict.integrity == INTEGRITY_EVALUATOR_FILES_MUTATED
    assert set(verdict.paths) == {"conftest.py", "tests/test_black.py"}
    assert test_file.read_bytes() == original
    assert not (worktree / "conftest.py").exists()
    assert guard.verify_evaluator_files().integrity == INTEGRITY_OK


class _StubAdapter:
    """Returns queued TestRunResults instead of running BugsInPy."""

    def __init__(self, results: list[models.TestRunResult]) -> None:
        self._results = list(results)
        self.calls: list[str | None] = []

    def run_tests(self, checkout, timeout=None, command=None) -> models.TestRunResult:
        self.calls.append(command)
        return self._results.pop(0)


def _run(passed: int, failed: int = 0, output: str = "", rc: int = 0):
    return models.TestRunResult(
        bug=BUG,
        results=[],
        raw_output=output,
        total_count=passed + failed,
        passed_count=passed,
        failed_count=failed,
        return_code=rc,
    )


def _validate(worktree: Path, diff_text: str, adapter, regression):
    applied: list[str] = []
    return (
        apply_patch_and_validate(
            lambda: applied.append("apply"),
            lambda: applied.append("restore"),
            adapter,
            CheckoutResult(bug=BUG, worktree=worktree, success=True),
            regression,
            timeout_seconds=10,
            evaluator_guard=EvaluatorGuard.for_worktree(worktree),
            diff_text=diff_text,
        ),
        applied,
    )


def test_rejected_candidate_is_never_applied_or_run(worktree: Path) -> None:
    adapter = _StubAdapter([])
    outcome, applied = _validate(
        worktree, RUN_349_BLACK_3_DIFF, adapter, RegressionContext(enabled=False)
    )
    assert not outcome.is_plausible
    assert outcome.failure_kind == "integrity"
    assert applied == [] and adapter.calls == []


def test_disabled_regression_is_reported_as_unchecked(worktree: Path) -> None:
    outcome, applied = _validate(
        worktree,
        SOURCE_ONLY_DIFF,
        _StubAdapter([_run(passed=1)]),
        RegressionContext(enabled=False),
    )
    assert outcome.is_plausible
    assert outcome.as_metadata()["regression_checked"] is False
    assert applied == ["apply", "restore"]


def test_unparseable_patched_regression_run_is_not_plausible(worktree: Path) -> None:
    regression = RegressionContext(enabled=True, command="pytest -rfE -q tests")
    outcome, _ = _validate(
        worktree,
        SOURCE_ONLY_DIFF,
        _StubAdapter([_run(passed=1), _run(passed=0, output="Segmentation fault")]),
        regression,
    )
    assert not outcome.is_plausible
    assert outcome.regression_untrusted


def test_trusted_suite_run_requires_executed_tests() -> None:
    assert is_trusted_suite_run(_run(passed=3, failed=1))
    assert not is_trusted_suite_run(_run(passed=0))
    assert not is_trusted_suite_run(_run(passed=3, rc=124))
    assert not is_trusted_suite_run(
        _run(passed=1, output="!!! Interrupted: 1 error during collection !!!")
    )


def test_parse_unified_diff_reads_git_multi_file_diffs() -> None:
    diff_text = (
        "diff --git a/pkg/a.py b/pkg/a.py\n--- a/pkg/a.py\n+++ b/pkg/a.py\n"
        "@@ -1 +1 @@\n-x = 1\n+x = 2\n"
        "diff --git a/pkg/new.py b/pkg/new.py\nnew file mode 100644\n"
        "--- /dev/null\n+++ b/pkg/new.py\n@@ -0,0 +1 @@\n+y = 1\n"
        "diff --git a/pkg/old.py b/pkg/old.py\ndeleted file mode 100644\n"
        "--- a/pkg/old.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-z = 1\n"
        "diff --git a/pkg/mode.py b/pkg/mode.py\nold mode 100644\nnew mode 100755\n"
    )
    file_diffs = parse_unified_diff(diff_text)
    assert [file_diff.target_path for file_diff in file_diffs] == [
        "b/pkg/a.py",
        "b/pkg/new.py",
        "a/pkg/old.py",
        "b/pkg/mode.py",
    ]
    assert file_diffs[1].is_creation and file_diffs[2].is_deletion
    assert file_diffs[0].added_lines == ["x = 2"]


def test_dangling_symlinks_are_locked_as_links(worktree: Path) -> None:
    link = worktree / "tests" / "data_link.txt"
    link.symlink_to("missing/target.txt")
    guard = EvaluatorGuard.for_worktree(worktree)

    link.unlink()
    link.write_text("replaced by a regular file\n")
    verdict = guard.verify_evaluator_files()

    assert verdict.integrity == INTEGRITY_EVALUATOR_FILES_MUTATED
    assert link.is_symlink() and str(link.readlink()) == "missing/target.txt"


def test_purge_removes_bytecode_that_would_survive_a_same_size_restore(
    tmp_path: Path,
) -> None:
    import os
    import py_compile

    from apr_framework.repair.evaluator_integrity import purge_bytecode_caches

    module = tmp_path / "expectation.py"
    module.write_text("EXIT_CODE = 1\n")
    py_compile.compile(str(module), cfile=None)
    original_stat = module.stat()
    module.write_text("EXIT_CODE = 2\n")  # same size, same second: cache still "valid"
    os.utime(module, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    assert list((tmp_path / "__pycache__").glob("expectation.*.pyc"))

    purge_bytecode_caches([module])

    assert not list((tmp_path / "__pycache__").glob("expectation.*.pyc"))


@pytest.mark.parametrize(
    "raw_output, expected",
    [
        ("..\n2 passed, 6 warnings in 0.08s", (2, 2, 0, 0)),
        ("F.\nFAILED tests/t.py::a - x\n1 failed, 1 passed in 0.10s", (2, 1, 1, 0)),
        ("===== 1 failed, 3 passed in 0.5s =====", (4, 3, 1, 0)),
        (
            "==== warnings summary ====\n=== 2 passed, 1 warning in 1.2s ===",
            (2, 2, 0, 0),
        ),
        ("1 error in 0.20s", (1, 0, 0, 1)),
        ("Ran 3 tests in 0.001s\n\nFAILED (failures=1)", (3, 2, 1, 0)),
        ("ImportError: no module", (0, 0, 0, 0)),
    ],
)
def test_test_run_counts_parse_pytest_with_and_without_banner(raw_output, expected):
    from apr_framework.benchmarks.bugsinpy import parse_test_run_counts

    assert parse_test_run_counts(raw_output) == expected
