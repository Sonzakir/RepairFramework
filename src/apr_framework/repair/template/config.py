"""Configuration dataclass for the template-based repair algorithm."""

from dataclasses import dataclass, field

from apr_framework.core.exceptions import ConfigurationError

VALID_OPERATORS: frozenset[str] = frozenset(
    {"arith", "comp", "obo", "bool", "negate", "return"}
)
_DEFAULT_OPERATORS: list[str] = sorted(VALID_OPERATORS)


@dataclass
class TemplateRepairConfig:
    """Configuration for TemplateRepairAlgorithm.

    Fields:
        budget:            Max patch validations before halting.
        top_n_locations:   How many top-ranked suspicious locations to attempt.
        enabled_operators: Mutation family keys to apply; defaults to all.
        timeout_per_test:  Wall-clock seconds allowed per test-suite invocation.
                           Enforced for real: passed down to the Docker exec call,
                           and a timed-out run is treated as non-plausible.
        stop_on_first:     If True, stop the repair loop after the first plausible
                           patch is found.
        regression_check:  If True (default), plausibility also requires that no
                           previously passing test in the bug's whole test_file
                           regresses, not just that the trigger test passes. Adds
                           one extra suite run per candidate (plus a one-off
                           baseline run); disable for speed.
    """

    budget: int = 200
    top_n_locations: int = 5
    enabled_operators: list[str] = field(
        default_factory=lambda: list(_DEFAULT_OPERATORS)
    )
    timeout_per_test: int = 120
    stop_on_first: bool = False
    regression_check: bool = True

    def __post_init__(self) -> None:
        unknown = set(self.enabled_operators) - VALID_OPERATORS
        if unknown:
            raise ConfigurationError(
                f"Unknown operator key(s): {sorted(unknown)}. "
                f"Valid keys: {sorted(VALID_OPERATORS)}"
            )
        if self.budget < 1:
            raise ConfigurationError("budget must be at least 1.")
        if self.top_n_locations < 1:
            raise ConfigurationError("top_n_locations must be at least 1.")
