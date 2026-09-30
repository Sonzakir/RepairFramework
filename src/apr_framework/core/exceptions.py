"""
Framework wide custom exceptions
"""


class APRFrameworkError(Exception):
    "Base exception for the APR framework"


class BenchmarkError(APRFrameworkError):
    """Raised for benchmark integration failures"""


class ConfigurationError(APRFrameworkError):
    """Raised for invalid framework configuration"""


class EvaluationError(APRFrameworkError):
    """Raised when an evaluation run cannot be completed."""
