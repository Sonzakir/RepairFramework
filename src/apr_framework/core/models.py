"""
Define the shated domain models (shared dataclasses and enums) used across subsystems
Every other subsystem should be able to exchange data through these models
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any


################## Statuses ####################
class TestStatus(str, Enum):
    """
    Outcome of the test execution
    Values:
        PASSED: Test execution succeeded and no failures occurred
        FAILED: Tests ran successfully, but at least one test failed
        ERROR: The test execution broke
    """

    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"


class RepairStatus(str, Enum):
    """
    Outcome of the repair attempt
    Values:
        PLAUSIBLE: A candidate patch passes the available test suite.
        CORRECT: A candidate patch matches the ground-truth fix or is otherwise
            verified as semantically correct.
        FAILED: Candidate patches were generated, but none passed validation.
        NO_PATCH: The repair algorithm did not produce any candidate patch.
    """

    PLAUSIBLE = "plausible"
    CORRECT = "correct"
    FAILED = "failed"
    NO_PATCH = "no_patch"


# TODO: Benchmarks as enums? (Since we have a limited number of benchmarks)

################## Models ######################


@dataclass(frozen=True)
class BugIdentifier:
    """
    Reference to a Bug
    """

    benchmark: str
    project: str
    bug_id: int


@dataclass(frozen=True)
class BugInfo:
    """
    Descriptive metadata for the Bug
    """

    identifier: BugIdentifier
    description: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class CheckoutResult:
    """
    Represents the outcome of a benchmark checkout operation.

    Records whether the checkout succeeded and provides a
    human-readable message about the operation result.
    """

    bug: BugIdentifier
    worktree: Path
    success: bool  # Checkout success
    message: str = ""
    prepared: bool = False  # whether the checked-out version is compiled


@dataclass
class TestCaseResult:
    """
    Structured result of a single executed test case
    """

    name: str
    status: TestStatus
    message: str = ""


@dataclass
class TestRunResult:
    """
    Structured result of a complete test execution for a specific bug

    Stores the bug under test, outcomes of the executed test cases and optional textual output produced by the test command
    """

    bug: BugIdentifier
    results: list[TestCaseResult]
    raw_output: str = ""
    total_count: int = 0
    passed_count: int = 0
    failed_count: int = 0
    error_count: int = 0
    # Process exit code of the underlying test command. 0 means the runner exited
    # cleanly; any non-zero value (collection error, crash, timeout) signals that
    # the run cannot be trusted as "all tests passed" even if no failures parsed.
    return_code: int = 0

    @property
    def total(self) -> int:
        """
        Returns:
            int: Total number of executed test cases in this run
        """
        return len(self.results)

    @property
    def passed(self) -> int:
        """
        Returns:
            int: Number of test cases with PASSED status.
        """
        return sum(result.status == TestStatus.PASSED for result in self.results)

    @property
    def failed(self) -> int:
        """
        Returns:
            int: Number of test cases with FAILED status.
        """
        return sum(result.status == TestStatus.FAILED for result in self.results)


@dataclass(frozen=True)
class RankedLocation:
    """
    Represents one suspicious code location identified by a fault localization tool, including its rank, score and optional metadata
    """

    rank: int
    file_path: str
    location: str
    score: float | None = None
    line: int | None = None
    # for function-level granularity
    end_line: int | None = None
    function: str | None = None
    raw_location: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class LocalizationResult:
    """Structured result of fault localization, containing the ranked suspicious code locations for a specific bug and optional metadata"""

    bug: BugIdentifier
    backend: str
    ranked_locations: list[RankedLocation]
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class PatchCandidate:
    """
    One proposed fix produced by a repair algorithm
    """

    bug: BugIdentifier
    patch_id: str
    summary: str
    diff_text: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class RepairAttemptResult:
    """
    Outcome of validating or applying one PatchCandidate
    """

    bug: BugIdentifier
    patch: PatchCandidate | None
    status: RepairStatus
    validation_summary: str = ""


@dataclass
class PatchAssessment:
    """LLM judgment for one plausible patch."""

    patch_id: str
    quality_score: float
    rationale: str
    raw_response: str = ""


@dataclass
class RetrievalStep:
    """One retrieval command executed before asking the model for a patch."""

    tool_name: str
    argument: str
    result_summary: str


@dataclass
class RetrievalTrace:
    """Recorded retrieval commands for one repair prompt."""

    steps: list[RetrievalStep] = field(default_factory=list)
    stop_reason: str = ""

    @property
    def step_count(self) -> int:
        """Return how many retrieval commands were executed."""
        return len(self.steps)


@dataclass
class RepairRunMetrics:
    """
    Patch-validation metrics for one repair run on a single bug (Task 2).

    These are the quantities the patch-validation pipeline must track and report:
    how many candidates were produced, how many were plausible/correct, how quickly
    the first plausible patch appeared, and how long the whole run took.

    Fields:
        total_candidates_generated:        Candidate patches produced by the
            repair algorithm before any validation/budget capping.
        candidates_validated:              Candidates actually executed against
            the test suite (≤ generated, bounded by the budget / early stop).
        plausible_count:                   Candidates whose patched program passed
            the whole test suite.
        correct_count:                     Plausible candidates that also match the
            developer fix at the diff level (semantic ground truth proxy).
        time_to_first_plausible_seconds:   Wall-clock seconds from the start of the
            validation loop until the first plausible patch, or None if none was
            found.
        total_wall_clock_seconds:          Wall-clock seconds for the whole repair
            run (generation + validation).
        llm_query_count:                   Number of LLM API calls the run made, or
            None for backends that issue no LLM queries (e.g. template repair).
    """

    total_candidates_generated: int = 0
    candidates_validated: int = 0
    plausible_count: int = 0
    correct_count: int = 0
    time_to_first_plausible_seconds: float | None = None
    total_wall_clock_seconds: float = 0.0
    rank_of_first_correct: int | None = None
    llm_query_count: int | None = None
    assessment_query_count: int | None = None


@dataclass
class EvaluationResult:
    """
    Result of one evaluation run for a specific bug, including its execution status and start/end timestamps
    """

    bug: BugIdentifier
    status: str
    started_at: datetime
    finished_at: datetime
    # Optional, populated by repair evaluation runs (Task 2). Left None by simpler
    # runners (e.g. the dummy runner) so the dataclass stays backward compatible.
    metrics: "RepairRunMetrics | None" = None
    run_dir: str | None = None
    # Optional, populated by repair runs when an LLM patch assessor is enabled:
    # the plausible patches in assessment order, each carrying quality_score and
    # assessment_rationale in its patch metadata. Left None when no assessor ran.
    assessed_plausible_results: "list[RepairAttemptResult] | None" = None
    # Optional, populated by repair runs when --similarity-score is enabled: *every*
    # generated candidate in generation order — not just the plausible ones, since a
    # patch that failed the tests can still be a near-miss on the developer's edit —
    # each carrying context_similarity_score and similarity_band in its patch
    # metadata. Left None when the flag is off.
    similarity_scored_results: "list[RepairAttemptResult] | None" = None


@dataclass(frozen=True)
class LocalizationConfig:
    backend: str = "fauxpy"
    family: str = "sbfl"
    granularity: str = "statement"
    top_n: int | None = None
    src: str = "."
    exclude: list[str] = field(default_factory=list)
    mutation: str | None = None
