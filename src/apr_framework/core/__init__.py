"""Shared framework models and exceptions."""

from .exceptions import (
    APRFrameworkError,
    BenchmarkError,
    ConfigurationError,
    EvaluationError,
)
from .models import (
    BugIdentifier,
    BugInfo,
    CheckoutResult,
    EvaluationResult,
    LocalizationResult,
    PatchCandidate,
    RepairAttemptResult,
    RepairStatus,
    TestCaseResult,
    TestRunResult,
    TestStatus,
)

__all__ = [
    "APRFrameworkError",
    "BenchmarkError",
    "ConfigurationError",
    "EvaluationError",
    "BugIdentifier",
    "BugInfo",
    "CheckoutResult",
    "EvaluationResult",
    "LocalizationResult",
    "PatchCandidate",
    "RepairAttemptResult",
    "RepairStatus",
    "TestCaseResult",
    "TestRunResult",
    "TestStatus",
]
