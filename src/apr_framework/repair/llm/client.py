"""LLM client abstraction and GPT@RUB (OpenAI-compatible) implementation."""

import logging
import os
import sys
import time
from abc import ABC, abstractmethod
from collections import deque
from pathlib import Path
from typing import Protocol, runtime_checkable

from apr_framework.core.exceptions import APRFrameworkError, ConfigurationError

logger = logging.getLogger(__name__)


@runtime_checkable
class LLMConnectionConfig(Protocol):
    """The connection fields ``OpenAICompatibleClient`` needs to reach an endpoint.

    This is the *only* thing the client depends on. Both ``LLMRepairConfig`` and the
    localization-side ``LLMLocalizationConfig`` satisfy it structurally, so the same
    client transport is shared across repair and fault localization without either
    config knowing about the other's task-specific fields.

    Attributes:
        model_name:      Model identifier sent to the API.
        temperature:     Sampling temperature in [0.0, 2.0].
        base_url:        Endpoint URL, or None to use the GPT@RUB default.
        api_key_env_var: Name of the environment variable holding the API key.
    """

    model_name: str
    temperature: float
    base_url: str | None
    api_key_env_var: str


GPT_AT_RUB_DEFAULT_BASE_URL = "https://gpt.ruhr-uni-bochum.de/external/v1"

# Opt-in prompt debugging (off by default). When this env var is set, every prompt
# sent to the LLM is dumped so a developer can inspect exactly what was sent:
#   unset / empty  -> no-op (normal behaviour)
#   "1" / "stderr" -> print each prompt to stderr
#   <directory>    -> write each prompt to <dir>/prompt_NNNN.txt
# It never alters the messages, the request, or the return value.
PROMPT_DEBUG_ENV_VAR = "APR_LLM_DEBUG_PROMPT"
_PROMPT_DEBUG_STDERR_VALUES = frozenset({"1", "stderr", "true", "yes"})

# GPT@RUB caps external-API requests at 60 per minute (see the "BETA: External API
# Endpoint" documentation). The client throttles itself to that limit rather than
# relying on the server to reject overflow requests.
GPT_AT_RUB_RATE_LIMIT_REQUESTS_PER_MINUTE = 60
GPT_AT_RUB_RATE_LIMIT_WINDOW_SECONDS = 60.0


class LLMClient(ABC):
    """Provider-agnostic interface for LLM completion calls."""

    @property
    def completion_count(self) -> int:
        """Number of completions issued so far. Default 0 for clients that do not count."""
        return 0

    @abstractmethod
    def complete(self, messages: list[dict[str, str]]) -> str:
        """Send an OpenAI-style messages list and return the response text.

        Args:
            messages: Conversation turns, e.g.
                [{"role": "system", "content": "..."}, {"role": "user", "content": "..."}]

        Returns:
            The model's reply as a plain string.
        """


class OpenAICompatibleClient(LLMClient):
    """LLM client for any OpenAI-compatible endpoint, including GPT@RUB.

    The API key is read from the environment at call time so the env var can be
    set after this object is constructed (useful in tests and Docker environments).
    Streaming is explicitly disabled — GPT@RUB does not support it.
    """

    def __init__(self, connection_config: LLMConnectionConfig) -> None:
        self._connection_config = connection_config
        self._endpoint_url = connection_config.base_url or GPT_AT_RUB_DEFAULT_BASE_URL
        self._recent_request_timestamps: deque[float] = deque()
        # Sequential index used only to name dumped prompt files when the
        # PROMPT_DEBUG_ENV_VAR debug mode is active. Inert otherwise.
        self._prompt_debug_count = 0
        # Number of completed API calls this client has made — the "queries made"
        # figure the evaluation reports. One client is built per repair run, so the
        # count is naturally scoped to that run.
        self._completion_count = 0

    @property
    def completion_count(self) -> int:
        """Number of successful ``complete`` calls issued by this client."""
        return self._completion_count

    def _wait_for_rate_limit_slot(self) -> None:
        """Block until issuing another request would stay within the per-minute cap."""
        while True:
            now = time.monotonic()
            while (
                self._recent_request_timestamps
                and now - self._recent_request_timestamps[0]
                >= GPT_AT_RUB_RATE_LIMIT_WINDOW_SECONDS
            ):
                self._recent_request_timestamps.popleft()

            if (
                len(self._recent_request_timestamps)
                < GPT_AT_RUB_RATE_LIMIT_REQUESTS_PER_MINUTE
            ):
                self._recent_request_timestamps.append(now)
                return

            sleep_seconds = GPT_AT_RUB_RATE_LIMIT_WINDOW_SECONDS - (
                now - self._recent_request_timestamps[0]
            )
            logger.info(
                "GPT@RUB rate limit (%d req/min) reached; sleeping %.1fs",
                GPT_AT_RUB_RATE_LIMIT_REQUESTS_PER_MINUTE,
                sleep_seconds,
            )
            time.sleep(sleep_seconds)

    def _dump_prompt_if_debugging(self, messages: list[dict[str, str]]) -> None:
        """Dump the exact prompt for inspection when prompt debugging is enabled.

        Controlled entirely by the ``PROMPT_DEBUG_ENV_VAR`` environment variable and
        a strict no-op when it is unset — the messages, request, and return value are
        never touched. Any failure to write is swallowed (logged) so debugging can
        never break a repair run.
        """
        debug_target = os.environ.get(PROMPT_DEBUG_ENV_VAR, "").strip()
        if not debug_target:
            return

        self._prompt_debug_count += 1
        rendered_prompt = _render_messages_for_debug(messages)

        if debug_target.lower() in _PROMPT_DEBUG_STDERR_VALUES:
            print(
                f"\n===== LLM PROMPT #{self._prompt_debug_count} "
                f"({PROMPT_DEBUG_ENV_VAR}) =====\n{rendered_prompt}\n"
                f"===== END LLM PROMPT #{self._prompt_debug_count} =====",
                file=sys.stderr,
            )
            return

        try:
            debug_directory_path = Path(debug_target)
            debug_directory_path.mkdir(parents=True, exist_ok=True)
            prompt_file_path = (
                debug_directory_path / f"prompt_{self._prompt_debug_count:04d}.txt"
            )
            prompt_file_path.write_text(rendered_prompt, encoding="utf-8")
            logger.info("Dumped LLM prompt to %s", prompt_file_path)
        except OSError as dump_error:
            logger.warning(
                "Could not write debug prompt to %r: %s", debug_target, dump_error
            )

    def complete(self, messages: list[dict[str, str]]) -> str:
        """Call the OpenAI-compatible chat completions endpoint.

        Args:
            messages: Conversation turns in OpenAI format.

        Returns:
            The model's text response.

        Raises:
            ConfigurationError: If the API key env var is not set.
            APRFrameworkError: On any OpenAI API error.
        """
        import openai

        self._dump_prompt_if_debugging(messages)

        api_key = os.environ.get(self._connection_config.api_key_env_var)
        if not api_key:
            raise ConfigurationError(
                f"API key not found. Set the environment variable "
                f"{self._connection_config.api_key_env_var!r} before running."
            )

        openai_client = openai.OpenAI(
            base_url=self._endpoint_url,
            api_key=api_key,
        )

        logger.debug(
            "Calling %s model=%s temperature=%s",
            self._endpoint_url,
            self._connection_config.model_name,
            self._connection_config.temperature,
        )

        self._wait_for_rate_limit_slot()

        try:
            response = openai_client.chat.completions.create(
                model=self._connection_config.model_name,
                messages=messages,
                temperature=self._connection_config.temperature,
                stream=False,
            )
        except openai.OpenAIError as error:
            raise APRFrameworkError(
                f"LLM API call failed ({type(error).__name__}): {error}"
            ) from error

        response_text = response.choices[0].message.content
        self._completion_count += 1
        logger.debug("LLM response received (%d chars)", len(response_text or ""))
        return response_text or ""


def _render_messages_for_debug(messages: list[dict[str, str]]) -> str:
    """Render an OpenAI-style messages list as readable text for prompt debugging."""
    rendered_blocks: list[str] = []
    for message in messages:
        role_name = message.get("role", "unknown").upper()
        message_content = message.get("content", "")
        rendered_blocks.append(f"----- {role_name} -----\n{message_content}")
    return "\n\n".join(rendered_blocks)
