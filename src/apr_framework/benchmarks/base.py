"""
Abstract interfaces for benchmark integrations.
"""

from abc import ABC, abstractmethod
from pathlib import Path

from apr_framework.core.models import (
    BugIdentifier,
    BugInfo,
    CheckoutResult,
    TestRunResult,
)


class BenchmarkAdapter(ABC):
    """
    Common interface that benchmark integrations implement for APR experiments.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Return the benchmark name used by the framework"""
        raise NotImplementedError

    @abstractmethod
    def list_projects(self) -> list[str]:
        """
        List project names available in the benchmark.

        Returns:
            Project names that can be passed to `list_bugs` or `checkout`.
        """
        raise NotImplementedError

    @abstractmethod
    def list_bugs(self, project: str) -> list[BugInfo]:
        """
        Return all known bugs for a project.

        Args:
            project: Name of the benchmark project, such as `black`.

        Returns:
            Bug metadata records for the requested project.
        """
        raise NotImplementedError

    @abstractmethod
    def checkout(self, bug: BugIdentifier, destination: Path) -> CheckoutResult:
        """
        Check out a selected buggy program version into a destination directory.

        Args:
            bug: Identifier for the benchmark, project, and bug id to check out.
            destination: Parent directory where the buggy project should be created.

        Returns:
            Checkout result describing the created worktree.
        """
        raise NotImplementedError

    @abstractmethod
    def prepare_environment(self, checkout: CheckoutResult) -> None:
        """
        Prepare dependencies or build artifacts for a checked-out bug.

        Args:
            checkout: Checkout result whose worktree should be made testable.
        """
        raise NotImplementedError

    @abstractmethod
    def run_tests(
        self, checkout: CheckoutResult, timeout: float | None = None
    ) -> TestRunResult:
        """
        Run tests for a checked-out bug and return structured results.

        Args:
            checkout: Prepared checkout to test.
            timeout: Optional wall-clock limit (seconds) for the test command.

        Returns:
            Test results and raw command output for the checkout.
        """
        raise NotImplementedError
