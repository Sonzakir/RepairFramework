"""The runner seam: where Strict Repro runs a test, BugsInPy or a local project.

``TestCommandRunner`` is deliberately shaped so the shared validation path can
drive it: ``run_tests(checkout, timeout=..., command=...)`` is the method
``apply_patch_and_validate`` calls on its ``adapter``. A runner is therefore
passed to it in place of a ``BugsInPyAdapter``, and probes and fixes go through
the one validation path in both modes; nothing in the 15 modules that hard-code
``BugsInPyAdapter`` changes.

- ``run_tests`` with no ``command`` runs the **oracle**: the bug's trigger test on
  BugsInPy, the user's ``--regression-cmd`` in local mode.
- ``run_repro_test`` runs one version of the generated test.

``BugsInPyCommandRunner`` copies the test outside the checkout (like the Break
This Fix witness), so the project's ``conftest.py`` and test configuration never
apply to it, and runs it through ``adapter.run_tests(command=...)`` from the
checkout. ``LocalPytestRunner`` runs the user's interpreter in a subprocess with a
timeout; it refuses to exist without ``allow_local_exec``, because it executes
LLM-written code on this machine.
"""

import os
import re
import shlex
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Protocol

from apr_framework.benchmarks.bugsinpy import BugsInPyAdapter, parse_test_run_counts
from apr_framework.benchmarks.executor_view import remove_host_tree
from apr_framework.core.exceptions import ConfigurationError
from apr_framework.core.models import BugIdentifier, CheckoutResult, TestRunResult
from apr_framework.repro.models import TestVersion

REPRO_WORKSPACE_NAME = "_repro"
TIMEOUT_RETURN_CODE = 124
_PYTEST_ISOLATION_INI = "[pytest]\n"
_PYTEST_FLAGS = "-q -rfE -p no:cacheprovider"


class TestCommandRunner(Protocol):
    """Runs the oracle and the repro test for one checkout or local project."""

    checkout: CheckoutResult

    @property
    def has_oracle(self) -> bool:
        """Whether ``run_tests(command=None)`` runs a real oracle."""

    @property
    def oracle_label(self) -> str:
        """``trigger`` (BugsInPy) or ``regression_cmd`` (local)."""

    def run_tests(
        self,
        checkout: CheckoutResult,
        timeout: float | None = None,
        command: str | None = None,
    ) -> TestRunResult:
        """Run the oracle (or ``command``) in the checkout."""

    def run_repro_test(self, test: TestVersion, timeout: float) -> TestRunResult:
        """Run one version of the generated test against the checkout as it is."""


def repro_test_file_name(test_id: str) -> str:
    """``test_repro_<id>.py`` with the id made importable."""
    return "test_repro_" + re.sub(r"\W", "_", test_id) + ".py"


class BugsInPyCommandRunner:
    """Runs tests in the BugsInPy executor through the adapter."""

    def __init__(
        self,
        adapter: BugsInPyAdapter,
        checkout: CheckoutResult,
        project_root: Path,
        test_id: str,
    ) -> None:
        self.adapter = adapter
        self.checkout = checkout
        self._project_root = project_root
        self._test_id = test_id

    @property
    def has_oracle(self) -> bool:
        return True

    @property
    def oracle_label(self) -> str:
        return "trigger"

    def run_tests(
        self,
        checkout: CheckoutResult,
        timeout: float | None = None,
        command: str | None = None,
    ) -> TestRunResult:
        return self.adapter.run_tests(checkout, timeout=timeout, command=command)

    def run_repro_test(self, test: TestVersion, timeout: float) -> TestRunResult:
        """Copy the test outside the checkout, run it from the checkout, delete it."""
        isolation_directory = (
            self._project_root
            / ".workspace"
            / "bugsinpy"
            / REPRO_WORKSPACE_NAME
            / f"{self._test_id}-{uuid.uuid4().hex[:8]}"
        )
        test_file_name = repro_test_file_name(self._test_id)
        write_isolated_test_copy(isolation_directory, test_file_name, test.source)
        try:
            executor_directory = self.adapter.toolchain.executor_path(
                isolation_directory
            )
            return self.adapter.run_tests(
                self.checkout,
                timeout=timeout,
                command=build_isolated_pytest_command(
                    "python", executor_directory, test_file_name
                ),
            )
        finally:
            remove_host_tree(isolation_directory)


class LocalPytestRunner:
    """Runs tests of a local pytest project with the user's interpreter."""

    def __init__(
        self,
        repo: Path,
        python: Path,
        test_id: str,
        oracle_command: str | None,
        *,
        allow_local_exec: bool,
    ) -> None:
        if not allow_local_exec:
            raise ConfigurationError(
                "Local mode runs LLM-written test code on this machine. Re-run with "
                "--allow-local-exec to accept that (no container isolation)."
            )
        self.repo = repo.resolve()
        self.python = python if python.is_absolute() else (Path.cwd() / python)
        if not self.python.exists():
            raise ConfigurationError(f"Interpreter not found: {self.python}")
        self._test_id = test_id
        self._oracle_command = oracle_command
        self.checkout = CheckoutResult(
            bug=BugIdentifier(benchmark="local", project=self.repo.name, bug_id=0),
            worktree=self.repo,
            success=True,
            prepared=True,
        )

    @property
    def has_oracle(self) -> bool:
        return bool(self._oracle_command)

    @property
    def oracle_label(self) -> str:
        return "regression_cmd"

    def run_tests(
        self,
        checkout: CheckoutResult,
        timeout: float | None = None,
        command: str | None = None,
    ) -> TestRunResult:
        """Run ``command`` (default: ``--regression-cmd``) in the project."""
        shell_command = command or self._oracle_command
        if not shell_command:
            return TestRunResult(
                bug=checkout.bug,
                results=[],
                raw_output="no --regression-cmd configured: no oracle in local mode",
                return_code=-1,
            )
        return self._run_shell_command(shell_command, timeout)

    def run_repro_test(self, test: TestVersion, timeout: float) -> TestRunResult:
        """Run the test from a temporary copy outside the project.

        The canonical file under ``.apr/repro/<id>/`` is written once, at the end of
        the run; while probes are applied the project tree must not change except
        by the probe, so runs use a copy outside it.
        """
        isolation_directory = Path(tempfile.mkdtemp(prefix="apr-repro-"))
        test_file_name = repro_test_file_name(self._test_id)
        write_isolated_test_copy(isolation_directory, test_file_name, test.source)
        try:
            return self._run_shell_command(
                build_isolated_pytest_command(
                    shlex.quote(str(self.python)),
                    str(isolation_directory),
                    test_file_name,
                ),
                timeout,
            )
        finally:
            shutil.rmtree(isolation_directory, ignore_errors=True)

    def _run_shell_command(
        self, shell_command: str, timeout: float | None
    ) -> TestRunResult:
        try:
            completed = subprocess.run(
                shell_command,
                shell=True,
                cwd=self.repo,
                env=self._environment_with_interpreter_first(),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            raw_output = (completed.stdout or "") + (completed.stderr or "")
            return_code = completed.returncode
        except subprocess.TimeoutExpired as expired:
            raw_output = _decode(expired.stdout) + _decode(expired.stderr)
            raw_output += f"\n[timed out after {timeout}s]"
            return_code = TIMEOUT_RETURN_CODE
        total, passed, failed, errors = parse_test_run_counts(raw_output)
        return TestRunResult(
            bug=self.checkout.bug,
            results=[],
            raw_output=raw_output,
            total_count=total,
            passed_count=passed,
            failed_count=failed,
            error_count=errors,
            return_code=return_code,
        )

    def _environment_with_interpreter_first(self) -> dict[str, str]:
        """``python`` and ``pytest`` in commands resolve to the user's interpreter."""
        environment = dict(os.environ)
        environment["PATH"] = (
            str(self.python.parent) + os.pathsep + environment.get("PATH", "")
        )
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        return environment


def write_isolated_test_copy(directory: Path, test_file_name: str, source: str) -> None:
    """The test plus an empty ``pytest.ini`` that makes the directory its rootdir."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "pytest.ini").write_text(_PYTEST_ISOLATION_INI, encoding="utf-8")
    (directory / test_file_name).write_text(source, encoding="utf-8")


def build_isolated_pytest_command(
    python: str, test_directory: str, test_file_name: str
) -> str:
    """``python -m pytest`` from the project root, configured only by our ini.

    ``python -m`` puts the working directory (the project) on ``sys.path``, so the
    test imports the project's code as it is on disk.
    """
    return (
        f"{python} -m pytest {_PYTEST_FLAGS} -c {test_directory}/pytest.ini "
        f"--rootdir {test_directory} {test_directory}/{test_file_name}"
    )


def _decode(output: bytes | str | None) -> str:
    if output is None:
        return ""
    if isinstance(output, bytes):
        return output.decode("utf-8", "replace")
    return output
