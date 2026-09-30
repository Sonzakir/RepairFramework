"""Configuration dataclass for the LLM-based repair algorithm."""

from dataclasses import dataclass

from apr_framework.core.exceptions import ConfigurationError
from apr_framework.repair.llm.prompt_builder import (
    RETRIEVAL_INSTRUCTIONS_PROMPT_NAME,
    RETRIEVAL_SYSTEM_ADDENDUM_PROMPT_NAME,
    load_system_prompt,
)

_VALID_LLM_PROVIDERS: frozenset[str] = frozenset({"openai-compatible"})


@dataclass
class LLMRepairConfig:
    """Configuration for LLMRepairAlgorithm.

    Fields:
        model_name:       LLM model identifier sent to the API (e.g. "gpt-4.1-2025-04-14").
        temperature:      Sampling temperature in [0.0, 2.0].
        max_patch_count:  Number of LLM completion calls per suspicious location.
        top_n_locations:  How many top-ranked FL locations to attempt repair on.
        llm_provider:     Client implementation to use (currently "openai-compatible").
        base_url:         API endpoint URL. None → use GPT_AT_RUB_DEFAULT_BASE_URL.
        api_key_env_var:  Name of the environment variable holding the API key.
        system_prompt_name: File stem under repair/llm/prompts/ (e.g. "prompt1")
                            supplying the system message sent with every request.
        timeout_seconds:  Wall-clock seconds allowed per test-suite invocation.
        iterative:        For loops
        max_iterations:   Max conversation turns for the iterative loop
        budget:           Max patch validations before halting (mirrors TemplateRepairConfig).
        stop_on_first:    Stop after first plausible patch.
        regression_check: Whether to run the regression half of plausibility.
        context_enrichment: Whether to enrich each prompt with the failing test's
                            source and traceback. When False the prompt contains only
                            the buggy function (the original Task-1 behaviour).
        few_shot_count:     Number of (buggy -> fixed) example pairs from sibling bugs
                            to prepend to each prompt. 0 disables the strategy. Toggled
                            independently of ``context_enrichment``.
        retrieval_budget:   Maximum retrieval commands the model may issue before
                            patch generation. 0 disables retrieval.
    """

    model_name: str
    temperature: float = 0.8
    max_patch_count: int = 5
    top_n_locations: int = 3
    llm_provider: str = "openai-compatible"
    base_url: str | None = None
    api_key_env_var: str = "GPT_AT_RUB_API_KEY"
    system_prompt_name: str = "prompt1"
    timeout_seconds: int = 120
    # Iterative repair: multi-turn conversation that feeds each failed patch's test
    # output back to the model (repair_loop override in LLMRepairAlgorithm).
    iterative: bool = False
    max_iterations: int = 5
    # Repair-loop control (mirrors TemplateRepairConfig)
    budget: int = 200
    stop_on_first: bool = False
    regression_check: bool = True
    context_enrichment: bool = True
    few_shot_count: int = 0
    retrieval_budget: int = 0

    def __post_init__(self) -> None:
        if not (0.0 <= self.temperature <= 2.0):
            raise ConfigurationError(
                f"temperature must be in [0.0, 2.0], got {self.temperature}"
            )
        if self.max_patch_count < 1:
            raise ConfigurationError(
                f"max_patch_count must be >= 1, got {self.max_patch_count}"
            )
        if self.top_n_locations < 1:
            raise ConfigurationError(
                f"top_n_locations must be >= 1, got {self.top_n_locations}"
            )
        if self.llm_provider not in _VALID_LLM_PROVIDERS:
            raise ConfigurationError(
                f"Unknown llm_provider {self.llm_provider!r}. "
                f"Valid options: {sorted(_VALID_LLM_PROVIDERS)}"
            )
        if self.budget < 1:
            raise ConfigurationError(f"budget must be >= 1, got {self.budget}")
        if self.max_iterations < 1:
            raise ConfigurationError(
                f"max_iterations must be >= 1, got {self.max_iterations}"
            )
        if self.few_shot_count < 0:
            raise ConfigurationError(
                f"few_shot_count must be >= 0, got {self.few_shot_count}"
            )
        if self.retrieval_budget < 0:
            raise ConfigurationError(
                f"retrieval_budget must be >= 0, got {self.retrieval_budget}"
            )
        # Fail fast if the named prompt file does not exist, rather than at first LLM call.
        load_system_prompt(self.system_prompt_name)
        if self.retrieval_budget > 0:
            load_system_prompt(RETRIEVAL_INSTRUCTIONS_PROMPT_NAME)
            load_system_prompt(RETRIEVAL_SYSTEM_ADDENDUM_PROMPT_NAME)
