"""Targets: where versions of a project are applied and their tests run.

``BugsInPyTarget`` is a compiled checkout of one BugsInPy bug. Commands run in the
executor container, in the bug's own Python environment, through
``BugsInPyAdapter.run_tests(command=...)``. ``bugsinpy-test`` runs that command by
plain word splitting (no shell), so commands for this target are a program and
plain path arguments only: no quoting, no ``VAR=value`` prefixes.

``LocalTarget`` is a project on this machine with its own interpreter. It runs the
project's tests and candidate code without isolation, so it refuses to exist
without ``allow_local_exec`` (the same rule as Strict Repro's local mode).

Both describe the test suite as a ``SuiteCommand``: the evidence driver runs the
suite in-process to observe it, so it needs the runner and its arguments rather
than a shell line.
"""

import os
import re
import shlex
import shutil
import subprocess
import tempfile
import uuid
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from apr_framework.benchmarks.bugsinpy import BugsInPyAdapter
from apr_framework.benchmarks.executor_view import remove_host_tree
from apr_framework.core.exceptions import ConfigurationError
from apr_framework.core.models import BugIdentifier, CheckoutResult
from apr_framework.repair.evaluator_integrity import read_declared_test_files

TIMEOUT_RETURN_CODE = 124
EVIDENCE_CHECKOUT_PREFIX = "evidence_"
EVIDENCE_SCRATCH_NAME = "_evidence"
SUITE_RUNNERS = ("pytest", "unittest")

_PYTHON_EXECUTABLE = re.compile(r"(^|/)python[\d.]*$")


@dataclass(frozen=True)
class SuiteCommand:
    """A test command split into its runner and the arguments that select tests.

    Fields:
        runner:    ``pytest`` or ``unittest``.
        arguments: Everything after the runner (test paths, node ids, dotted
                   unittest names, flags such as ``-q``).
    """

    runner: str
    arguments: tuple[str, ...]

    @classmethod
    def parse(cls, command: str) -> "SuiteCommand":
        """Parse ``pytest ...``, ``python -m pytest ...`` or ``python -m unittest ...``.

        Raises:
            ConfigurationError: The command uses another runner (or none).
        """
        tokens = shlex.split(command)
        while tokens and _PYTHON_EXECUTABLE.search(tokens[0]):
            tokens = tokens[1:]
        if tokens[:1] == ["-m"]:
            tokens = tokens[1:]
        if tokens and tokens[0] in ("pytest", "py.test"):
            return cls(runner="pytest", arguments=tuple(tokens[1:]))
        if tokens and tokens[0] == "unittest":
            return cls(runner="unittest", arguments=tuple(tokens[1:]))
        raise ConfigurationError(
            f"Cannot tell which test runner {command!r} uses. Use `pytest ...`, "
            "`python -m pytest ...` or `python -m unittest ...`."
        )

    @property
    def selectors(self) -> tuple[str, ...]:
        """The arguments that name tests, modules or files (flags dropped)."""
        return tuple(
            argument for argument in self.arguments if not argument.startswith("-")
        )

    def describe(self) -> str:
        prefix = "python -m unittest" if self.runner == "unittest" else "pytest"
        return " ".join([prefix, *self.arguments])

    def selects(self, test_id: str) -> bool:
        """Whether one of this command's selectors names ``test_id``.

        unittest ids are dotted (``tests.test_x.Case.test_a``); a selector names
        a test, a class or a module. pytest ids are node ids
        (``tests/test_x.py::Case::test_a[1]``); a selector names a node, a file
        or a directory.
        """
        for selector in self.selectors:
            if self.runner == "unittest":
                if test_id == selector or test_id.startswith(selector + "."):
                    return True
            elif test_id == selector or test_id.startswith(
                (selector + "::", selector + "[", selector.rstrip("/") + "/")
            ):
                return True
        return False


@dataclass(frozen=True)
class CommandResult:
    """What one command run produced."""

    return_code: int
    output: str

    @property
    def timed_out(self) -> bool:
        return self.return_code == TIMEOUT_RETURN_CODE


class EvidenceTarget(ABC):
    """A project checkout plus a way to run Python commands in its environment."""

    @property
    @abstractmethod
    def worktree(self) -> Path:
        """The checkout every version is applied to."""

    @property
    @abstractmethod
    def label(self) -> str:
        """A short name for reports (``scrapy#2``, a directory name)."""

    @property
    @abstractmethod
    def suite(self) -> SuiteCommand:
        """The test suite the evidence commands observe."""

    @property
    @abstractmethod
    def trigger_tests(self) -> tuple[SuiteCommand, ...]:
        """The commands that run the bug's failing test(s); may be empty."""

    @property
    @abstractmethod
    def declared_test_files(self) -> frozenset[str]:
        """Test files the target declares (BugsInPy metadata); may be empty."""

    @abstractmethod
    def reference_diff(self) -> str | None:
        """The reference (developer) fix, when the target has one."""

    @abstractmethod
    def python_command(self, script: Path, *arguments: Path) -> str:
        """A command that runs ``script`` with this target's interpreter."""

    @abstractmethod
    def runner_path(self, path: Path) -> str:
        """``path`` (on this machine) as the target's test runner sees it."""

    @abstractmethod
    def run(self, command: str, timeout_seconds: float) -> CommandResult:
        """Run ``command`` from the worktree in the target's environment."""

    @abstractmethod
    def describe(self) -> dict[str, object]:
        """What a second person needs to reproduce a run on this target."""

    @contextmanager
    def scratch_directory(self, purpose: str) -> Iterator[Path]:
        """A fresh directory the target's runner can read and write, removed after."""
        directory = self._scratch_root() / f"{purpose}-{uuid.uuid4().hex[:8]}"
        directory.mkdir(parents=True)
        try:
            yield directory
        finally:
            self._remove_scratch(directory)

    def is_trigger_test(self, test_id: str) -> bool:
        return any(command.selects(test_id) for command in self.trigger_tests)

    @abstractmethod
    def _scratch_root(self) -> Path:
        """Where scratch directories are created."""

    def _remove_scratch(self, directory: Path) -> None:
        shutil.rmtree(directory, ignore_errors=True)


class BugsInPyTarget(EvidenceTarget):
    """A compiled checkout of one BugsInPy bug, run in the executor container."""

    def __init__(
        self,
        adapter: BugsInPyAdapter,
        checkout: CheckoutResult,
        project_root: Path,
        *,
        reused_checkout: bool = False,
    ) -> None:
        self._adapter = adapter
        self._checkout = checkout
        self._project_root = project_root
        self._reused_checkout = reused_checkout
        self._trigger_tests = _read_trigger_commands(checkout.worktree)
        self._suite = _derive_suite(checkout.worktree)
        self._declared_test_files = read_declared_test_files(checkout.worktree)

    @classmethod
    def open(
        cls,
        adapter: BugsInPyAdapter,
        project_root: Path,
        bug: BugIdentifier,
        *,
        reuse_checkout: bool = False,
    ) -> "BugsInPyTarget":
        """Check the bug out into its own evidence directory and compile it.

        The evidence commands temporarily rewrite test files, so they never work
        in the checkout ``bugsinpy checkout`` or ``repair`` use.
        """
        from apr_framework.challenge.replay import check_out_and_compile_into

        destination = (
            project_root
            / ".workspace"
            / "bugsinpy"
            / f"{EVIDENCE_CHECKOUT_PREFIX}{bug.project}_{bug.bug_id}"
        )
        checkout = check_out_and_compile_into(bug, adapter, destination, reuse_checkout)
        return cls(adapter, checkout, project_root, reused_checkout=reuse_checkout)

    @property
    def worktree(self) -> Path:
        return self._checkout.worktree

    @property
    def checkout(self) -> CheckoutResult:
        return self._checkout

    @property
    def label(self) -> str:
        return f"{self._checkout.bug.project}#{self._checkout.bug.bug_id}"

    @property
    def suite(self) -> SuiteCommand:
        return self._suite

    @property
    def trigger_tests(self) -> tuple[SuiteCommand, ...]:
        return self._trigger_tests

    @property
    def declared_test_files(self) -> frozenset[str]:
        return self._declared_test_files

    def reference_diff(self) -> str | None:
        return self._adapter.get_reference_patch(self._checkout.bug)

    def python_command(self, script: Path, *arguments: Path) -> str:
        parts = [self.runner_path(path) for path in (script, *arguments)]
        if any(re.search(r"\s", part) for part in parts):
            raise ConfigurationError(
                "BugsInPy runs test commands without a shell, so paths must not "
                f"contain whitespace: {parts}"
            )
        return " ".join(["python", *parts])

    def runner_path(self, path: Path) -> str:
        return self._adapter.toolchain.executor_path(path)

    def run(self, command: str, timeout_seconds: float) -> CommandResult:
        run_result = self._adapter.run_tests(
            self._checkout, timeout=timeout_seconds, command=command
        )
        return CommandResult(
            return_code=run_result.return_code, output=run_result.raw_output
        )

    def describe(self) -> dict[str, object]:
        bug_info = (self.worktree / "bugsinpy_bug.info").read_text(encoding="utf-8")
        python_version = re.search(r'python_version="([^"]+)"', bug_info)
        return {
            "kind": "bugsinpy",
            "bug": self.label,
            "python_version": python_version.group(1) if python_version else "?",
            "checkout": "reused"
            if self._reused_checkout
            else "fresh checkout and compile",
            "worktree": str(self.worktree),
            "suite": self._suite.describe(),
            "trigger": [command.describe() for command in self._trigger_tests],
        }

    def _scratch_root(self) -> Path:
        # Under the BugsInPy workspace, so the executor container can see it.
        return self._project_root / ".workspace" / "bugsinpy" / EVIDENCE_SCRATCH_NAME

    def _remove_scratch(self, directory: Path) -> None:
        remove_host_tree(directory)


class LocalTarget(EvidenceTarget):
    """A local project run with its own interpreter, without isolation."""

    def __init__(
        self,
        repo: Path,
        python: Path,
        suite: SuiteCommand,
        *,
        allow_local_exec: bool,
        reference_diff_text: str | None = None,
        trigger_tests: tuple[SuiteCommand, ...] = (),
    ) -> None:
        if not allow_local_exec:
            raise ConfigurationError(
                "This runs the project's tests and the candidate fixes' code on this "
                "machine without container isolation. Pass --allow-local-exec to "
                "accept that."
            )
        repo = repo.resolve()
        if not repo.is_dir():
            raise ConfigurationError(f"No such project directory: {repo}")
        if not python.exists():
            raise ConfigurationError(f"No such interpreter: {python}")
        self._repo = repo
        self._python = python
        self._suite = suite
        self._reference_diff_text = reference_diff_text
        self._trigger_tests = trigger_tests

    @property
    def worktree(self) -> Path:
        return self._repo

    @property
    def label(self) -> str:
        return self._repo.name

    @property
    def suite(self) -> SuiteCommand:
        return self._suite

    @property
    def trigger_tests(self) -> tuple[SuiteCommand, ...]:
        return self._trigger_tests

    @property
    def declared_test_files(self) -> frozenset[str]:
        return frozenset()

    def reference_diff(self) -> str | None:
        return self._reference_diff_text

    def python_command(self, script: Path, *arguments: Path) -> str:
        return " ".join(
            shlex.quote(str(part)) for part in (self._python, script, *arguments)
        )

    def runner_path(self, path: Path) -> str:
        return str(path)

    def run(self, command: str, timeout_seconds: float) -> CommandResult:
        environment = dict(os.environ)
        environment["PATH"] = (
            str(self._python.parent) + os.pathsep + environment.get("PATH", "")
        )
        try:
            completed = subprocess.run(
                command,
                shell=True,
                cwd=self._repo,
                env=environment,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as expired:
            output = _decode(expired.stdout) + _decode(expired.stderr)
            return CommandResult(
                return_code=TIMEOUT_RETURN_CODE,
                output=output + f"\n[timed out after {timeout_seconds}s]",
            )
        return CommandResult(
            return_code=completed.returncode,
            output=(completed.stdout or "") + (completed.stderr or ""),
        )

    def describe(self) -> dict[str, object]:
        return {
            "kind": "local",
            "repo": str(self._repo),
            "python": str(self._python),
            "suite": self._suite.describe(),
            "trigger": [command.describe() for command in self._trigger_tests],
        }

    def _scratch_root(self) -> Path:
        return Path(tempfile.gettempdir()) / "apr-evidence"


def _read_trigger_commands(worktree: Path) -> tuple[SuiteCommand, ...]:
    script = worktree / "bugsinpy_run_test.sh"
    if not script.is_file():
        return ()
    commands: list[SuiteCommand] = []
    for line in script.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            commands.append(SuiteCommand.parse(line.strip()))
        except ConfigurationError:
            continue
    return tuple(commands)


def _derive_suite(worktree: Path) -> SuiteCommand:
    """The bug's whole test file(s), run with the trigger test's runner."""
    triggers = _read_trigger_commands(worktree)
    bug_info = worktree / "bugsinpy_bug.info"
    test_file_match = (
        re.search(r'test_file="([^"]+)"', bug_info.read_text(encoding="utf-8"))
        if bug_info.is_file()
        else None
    )
    test_files = (
        [path.strip() for path in test_file_match.group(1).split(";") if path.strip()]
        if test_file_match
        else []
    )
    if not triggers or not test_files:
        raise ConfigurationError(
            f"Cannot derive the bug's test suite from {worktree}: no trigger "
            "command or no test_file in bugsinpy_bug.info."
        )
    if triggers[0].runner == "unittest":
        modules = tuple(
            path.removesuffix(".py").replace("/", ".") for path in test_files
        )
        return SuiteCommand(runner="unittest", arguments=modules)
    return SuiteCommand(runner="pytest", arguments=tuple(test_files))


def _decode(output: bytes | str | None) -> str:
    if output is None:
        return ""
    if isinstance(output, bytes):
        return output.decode("utf-8", errors="replace")
    return output
