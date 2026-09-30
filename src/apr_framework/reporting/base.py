from abc import ABC, abstractmethod
from pathlib import Path

from apr_framework.core.models import EvaluationResult


class ReportGenerator(ABC):
    """
    Interface for components that turn evaluation results into report files.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """
        Stable report generator name used in logs and report metadata.
        """
        raise NotImplementedError

    @abstractmethod
    def write_summary(self, results: list[EvaluationResult], output_dir: Path) -> Path:
        """
        Write a summary report for evaluation results.

        Args:
            results: Evaluation results to include in the summary.
            output_dir: Directory where the report file should be written.

        Returns:
            Path to the generated summary report file.
        """
        raise NotImplementedError
