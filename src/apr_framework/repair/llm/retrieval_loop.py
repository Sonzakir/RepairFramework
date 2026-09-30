"""Bounded retrieval loop for enriching LLM repair conversations."""

import logging

from apr_framework.core.models import CheckoutResult, RetrievalStep, RetrievalTrace
from apr_framework.repair.llm.client import LLMClient
from apr_framework.repair.llm.retrieval_protocol import (
    build_retrieval_result_message,
    parse_retrieve_command,
)
from apr_framework.repair.llm.retrieval_tools import execute_retrieval_command

logger = logging.getLogger(__name__)

_MAX_RECORDED_RESULT_CHARS = 1_000


def run_retrieval_loop(
    llm_client: LLMClient,
    messages: list[dict[str, str]],
    checkout: CheckoutResult,
    retrieval_budget: int,
) -> RetrievalTrace:
    """Let the model request bounded static-analysis results before patching."""
    trace = RetrievalTrace()

    for step_index in range(retrieval_budget):
        assistant_text = llm_client.complete(messages)
        retrieve_command = parse_retrieve_command(assistant_text)
        if retrieve_command is None:
            trace.stop_reason = (
                "parse_error" if "RETRIEVE:" in assistant_text else "model_ready"
            )
            if trace.stop_reason == "parse_error":
                logger.warning(
                    "Stopping retrieval after malformed command at step %d",
                    step_index + 1,
                )
            return trace

        result_text = execute_retrieval_command(retrieve_command, checkout)
        messages.append({"role": "assistant", "content": assistant_text})
        messages.append(build_retrieval_result_message(retrieve_command, result_text))
        trace.steps.append(
            RetrievalStep(
                tool_name=retrieve_command.tool_name,
                argument=retrieve_command.argument,
                result_summary=_summarize_retrieval_result(result_text),
            )
        )

    trace.stop_reason = "budget_exhausted"
    return trace


def _summarize_retrieval_result(result_text: str) -> str:
    if len(result_text) <= _MAX_RECORDED_RESULT_CHARS:
        return result_text
    return (
        result_text[:_MAX_RECORDED_RESULT_CHARS].rstrip()
        + f"\n\n[truncated after {_MAX_RECORDED_RESULT_CHARS} characters]"
    )
