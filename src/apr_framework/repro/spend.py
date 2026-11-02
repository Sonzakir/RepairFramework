"""LLM spend accounting and the USD cap.

Every Strict Repro LLM call goes through ``MeteredLLMClient``. It records the call
(purpose, tokens, cost, a hash of the messages) and refuses a call when the
estimated cost would push the run over its cap. Cost is tokens times the model's
list price per million tokens. When the endpoint reports no usage, tokens are
estimated from characters (4 per token) and the record says so.

The price table holds list prices as of 2026-10 (OpenAI API, no cached-input
discount assumed, so the figures are an upper bound). An unknown model needs
explicit ``--usd-per-mtok-in``/``--usd-per-mtok-out``, because a cap cannot be
enforced without a price.
"""

import json
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from typing import Iterator

from apr_framework.core.exceptions import APRFrameworkError, ConfigurationError
from apr_framework.repair.llm.client import LLMClient, TokenUsage
from apr_framework.repro.models import sha256_text

# USD per million tokens: (input, output).
MODEL_LIST_PRICES_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    "gpt-5.4": (2.50, 15.00),
    "gpt-5.4-mini": (0.75, 4.50),
    "gpt-5.4-nano": (0.20, 1.25),
    "gpt-5.5": (5.00, 30.00),
}
_CHARACTERS_PER_TOKEN = 4
# Output budget assumed for the pre-call estimate.
_ASSUMED_OUTPUT_TOKENS = 4_000


class SpendCapReached(APRFrameworkError):
    """The next LLM call would exceed the USD cap; the run stops here."""


@dataclass(frozen=True)
class ModelPrice:
    model_name: str
    usd_per_mtok_in: float
    usd_per_mtok_out: float

    def cost(self, prompt_tokens: int, completion_tokens: int) -> float:
        return (
            prompt_tokens * self.usd_per_mtok_in
            + completion_tokens * self.usd_per_mtok_out
        ) / 1_000_000


def resolve_model_price(
    model_name: str, usd_per_mtok_in: float | None, usd_per_mtok_out: float | None
) -> ModelPrice:
    """The list price for ``model_name``, or the explicit override."""
    if usd_per_mtok_in is not None and usd_per_mtok_out is not None:
        return ModelPrice(model_name, usd_per_mtok_in, usd_per_mtok_out)
    if model_name not in MODEL_LIST_PRICES_USD_PER_MTOK:
        raise ConfigurationError(
            f"No list price known for model {model_name!r}. Pass --usd-per-mtok-in "
            "and --usd-per-mtok-out so the USD cap can be enforced."
        )
    price_in, price_out = MODEL_LIST_PRICES_USD_PER_MTOK[model_name]
    return ModelPrice(model_name, price_in, price_out)


@dataclass(frozen=True)
class LLMCallRecord:
    """One metered call. The messages are stored as a hash only (issue text)."""

    purpose: str
    model: str
    temperature: float
    prompt_tokens: int
    completion_tokens: int
    usd: float
    usage_estimated: bool
    messages_sha256: str


@dataclass
class SpendMeter:
    """Running total of LLM spend against a cap, shared by every client of a run."""

    cap_usd: float
    records: list[LLMCallRecord] = field(default_factory=list)

    @property
    def spent_usd(self) -> float:
        return sum(record.usd for record in self.records)

    @property
    def call_count(self) -> int:
        return len(self.records)

    def ensure_affordable(self, estimated_usd: float) -> None:
        if self.spent_usd + estimated_usd > self.cap_usd:
            raise SpendCapReached(
                f"LLM spend cap reached: spent ${self.spent_usd:.4f}, the next call "
                f"is estimated at ${estimated_usd:.4f}, cap ${self.cap_usd:.2f}."
            )

    def record(self, call: LLMCallRecord) -> None:
        self.records.append(call)

    def to_json(self) -> str:
        payload = {
            "cap_usd": self.cap_usd,
            "spent_usd": round(self.spent_usd, 6),
            "calls": [asdict(record) for record in self.records],
        }
        return json.dumps(payload, indent=2) + "\n"


class MeteredLLMClient(LLMClient):
    """Wraps a client: checks the cap before a call, records the cost after it."""

    def __init__(
        self,
        inner: LLMClient,
        meter: SpendMeter,
        price: ModelPrice,
        temperature: float,
    ) -> None:
        self._inner = inner
        self._meter = meter
        self._price = price
        self._temperature = temperature
        self._purpose = "unlabelled"

    @property
    def completion_count(self) -> int:
        return self._inner.completion_count

    @property
    def last_usage(self) -> TokenUsage | None:
        return self._inner.last_usage

    @property
    def meter(self) -> SpendMeter:
        return self._meter

    @contextmanager
    def purpose(self, purpose: str) -> Iterator["MeteredLLMClient"]:
        """Label every call made inside the block (``generation``, ``judge``, ...)."""
        previous_purpose, self._purpose = self._purpose, purpose
        try:
            yield self
        finally:
            self._purpose = previous_purpose

    def complete(self, messages: list[dict[str, str]]) -> str:
        estimated_prompt_tokens = estimate_tokens(messages)
        self._meter.ensure_affordable(
            self._price.cost(estimated_prompt_tokens, _ASSUMED_OUTPUT_TOKENS)
        )
        response_text = self._inner.complete(messages)
        self._meter.record(
            self._build_call_record(messages, response_text, estimated_prompt_tokens)
        )
        return response_text

    def _build_call_record(
        self,
        messages: list[dict[str, str]],
        response_text: str,
        estimated_prompt_tokens: int,
    ) -> LLMCallRecord:
        usage = self._inner.last_usage
        if usage is None:
            prompt_tokens = estimated_prompt_tokens
            completion_tokens = len(response_text) // _CHARACTERS_PER_TOKEN + 1
        else:
            prompt_tokens, completion_tokens = (
                usage.prompt_tokens,
                usage.completion_tokens,
            )
        return LLMCallRecord(
            purpose=self._purpose,
            model=self._price.model_name,
            temperature=self._temperature,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            usd=round(self._price.cost(prompt_tokens, completion_tokens), 6),
            usage_estimated=usage is None,
            messages_sha256=sha256_text(json.dumps(messages, sort_keys=True)),
        )


def estimate_tokens(messages: list[dict[str, str]]) -> int:
    characters = sum(len(message.get("content", "")) for message in messages)
    return characters // _CHARACTERS_PER_TOKEN + 1
