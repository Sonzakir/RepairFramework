"""Configuration for LLM-based patch assessment."""

from dataclasses import dataclass
from pathlib import Path

from apr_framework.core.exceptions import ConfigurationError

_PROMPTS_DIRECTORY_PATH = Path(__file__).parent / "prompts"


@dataclass
class LLMAssessmentConfig:
    """Configuration for the LLM patch assessor.

    The first four fields satisfy the shared LLM connection protocol used by
    ``OpenAICompatibleClient``.
    """

    model_name: str
    temperature: float = 0.8
    base_url: str | None = None
    api_key_env_var: str = "GPT_AT_RUB_API_KEY"
    system_prompt_name: str = "assess_prompt1"
    max_patches_assessed: int | None = None
    timeout_seconds: int = 120

    def __post_init__(self) -> None:
        if not (0.0 <= self.temperature <= 2.0):
            raise ConfigurationError(
                f"temperature must be in [0.0, 2.0], got {self.temperature}"
            )
        if self.max_patches_assessed is not None and self.max_patches_assessed < 1:
            raise ConfigurationError(
                "max_patches_assessed must be >= 1 or None, "
                f"got {self.max_patches_assessed}"
            )
        if self.timeout_seconds < 1:
            raise ConfigurationError(
                f"timeout_seconds must be >= 1, got {self.timeout_seconds}"
            )
        load_assessment_system_prompt(self.system_prompt_name)


def load_assessment_system_prompt(prompt_name: str) -> str:
    """Load an assessment system prompt by file stem."""
    prompt_file_path = _PROMPTS_DIRECTORY_PATH / f"{prompt_name}.txt"
    if not prompt_file_path.is_file():
        available_prompt_names = sorted(
            candidate_path.stem
            for candidate_path in _PROMPTS_DIRECTORY_PATH.glob("*.txt")
        )
        raise ConfigurationError(
            f"Assessment system prompt {prompt_name!r} not found at {prompt_file_path}. "
            f"Available prompts: {available_prompt_names}"
        )
    return prompt_file_path.read_text(encoding="utf-8").strip()
