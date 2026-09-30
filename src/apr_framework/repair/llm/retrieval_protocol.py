"""Text protocol for optional code retrieval turns during LLM repair."""

from dataclasses import dataclass
import re


_RETRIEVE_COMMAND_PATTERN = re.compile(
    r"RETRIEVE:\s*"
    r"(get_function_definition|get_class_definition|find_usages)"
    r"\(\s*[\"']([^\"']+)[\"']\s*\)"
)


@dataclass(frozen=True)
class RetrieveCommand:
    """A parsed retrieval command emitted by the model."""

    tool_name: str
    argument: str


def parse_retrieve_command(assistant_text: str) -> RetrieveCommand | None:
    """Return the first supported retrieval command in assistant_text, if any."""
    match = _RETRIEVE_COMMAND_PATTERN.search(assistant_text)
    if match is None:
        return None
    return RetrieveCommand(tool_name=match.group(1), argument=match.group(2))


def build_retrieval_result_message(
    retrieve_command: RetrieveCommand, result_text: str
) -> dict[str, str]:
    """Build the conversation turn that carries tool output back to the model."""
    return {
        "role": "user",
        "content": (
            f"Result for {retrieve_command.tool_name}"
            f"({retrieve_command.argument!r}):\n\n"
            f"{result_text}\n\n"
            "You may request another RETRIEVE command, or return the patch as usual "
            "when you have enough information."
        ),
    }
