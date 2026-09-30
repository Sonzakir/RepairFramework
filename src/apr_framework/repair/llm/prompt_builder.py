"""Prompt construction for LLM-based repair."""

import ast
import logging
from pathlib import Path

from apr_framework.core.exceptions import ConfigurationError
from apr_framework.core.models import RankedLocation

logger = logging.getLogger(__name__)

_FALLBACK_WINDOW_LINES = 25

_PROMPTS_DIRECTORY_PATH = Path(__file__).parent / "prompts"
_DEFAULT_SYSTEM_PROMPT_NAME = "prompt1"
RETRIEVAL_SYSTEM_ADDENDUM_PROMPT_NAME = "retrieval_system_addendum"
RETRIEVAL_INSTRUCTIONS_PROMPT_NAME = "retrieval_instructions"


def load_system_prompt(prompt_name: str) -> str:
    """Load a system-prompt text file from repair/llm/prompts/ by name.

    Args:
        prompt_name: File stem (without ``.txt``) under the prompts/ directory,
                     e.g. ``"prompt1"``.

    Returns:
        The file's contents with surrounding whitespace stripped.

    Raises:
        ConfigurationError: If no matching file exists.
    """
    prompt_file_path = _PROMPTS_DIRECTORY_PATH / f"{prompt_name}.txt"
    if not prompt_file_path.is_file():
        available_prompt_names = sorted(
            candidate_path.stem
            for candidate_path in _PROMPTS_DIRECTORY_PATH.glob("*.txt")
        )
        raise ConfigurationError(
            f"System prompt {prompt_name!r} not found at {prompt_file_path}. "
            f"Available prompts: {available_prompt_names}"
        )
    return prompt_file_path.read_text(encoding="utf-8").strip()


def build_system_message_with_retrieval_permission(
    base_system_message_text: str,
) -> str:
    """Append the retrieval addendum so the system prompt allows RETRIEVE replies.

    The repair system prompts forbid any output other than a fenced code block. A
    retrieval command is not a fenced code block, so without this addendum the model
    obeys the system prompt and never retrieves, no matter what the user message offers.
    """
    retrieval_addendum_text = load_system_prompt(RETRIEVAL_SYSTEM_ADDENDUM_PROMPT_NAME)
    return f"{base_system_message_text}\n\n{retrieval_addendum_text}"


def extract_function_source(
    source_file_path: Path,
    target_line: int,
) -> tuple[str, int, int]:
    """Extract the smallest enclosing function that contains target_line.

    Parses the file with ast.parse and walks all FunctionDef / AsyncFunctionDef
    nodes. Among those whose span contains target_line, returns the one with the
    narrowest span (innermost / most-nested match).

    Falls back to a ±25-line window around target_line when no enclosing function
    is found (logged at WARNING level).

    Args:
        source_file_path: Absolute path to the Python source file.
        target_line:      1-indexed line number of the suspicious location.

    Returns:
        (function_source_text, start_line, end_line) — all 1-indexed, inclusive.

    Raises:
        ConfigurationError: If the file cannot be read or contains a syntax error.
    """
    try:
        source_text = source_file_path.read_text(encoding="utf-8")
    except OSError as error:
        raise ConfigurationError(
            f"Cannot read source file {source_file_path}: {error}"
        ) from error

    try:
        tree = ast.parse(source_text, filename=str(source_file_path))
    except SyntaxError as error:
        raise ConfigurationError(f"Cannot parse {source_file_path}: {error}") from error

    source_lines = source_text.splitlines(keepends=True)

    # Find the innermost (smallest-span) function node enclosing target_line.
    best_node: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    best_span = float("inf")

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        # ast guarantees end_lineno is set for Python 3.8+
        node_start: int = node.lineno
        node_end: int = node.end_lineno  # type: ignore[assignment]
        if node_start <= target_line <= node_end:
            span = node_end - node_start
            if span < best_span:
                best_span = span
                best_node = node

    if best_node is not None:
        start_line: int = best_node.lineno
        end_line: int = best_node.end_lineno  # type: ignore[assignment]
        extracted_text = "".join(source_lines[start_line - 1 : end_line])
        return extracted_text, start_line, end_line

    # Fallback: ±25-line window
    logger.warning(
        "No enclosing function found for %s:%d — falling back to ±%d line window",
        source_file_path,
        target_line,
        _FALLBACK_WINDOW_LINES,
    )
    total_lines = len(source_lines)
    start_line = max(1, target_line - _FALLBACK_WINDOW_LINES)
    end_line = min(total_lines, target_line + _FALLBACK_WINDOW_LINES)
    extracted_text = "".join(source_lines[start_line - 1 : end_line])
    return extracted_text, start_line, end_line


def build_repair_prompt(
    location: RankedLocation,
    function_source_text: str,
    function_start_line: int,
    *,
    system_message_text: str | None = None,
    few_shot_examples: str | None = None,
    failing_test_source: str | None = None,
    error_traceback: str | None = None,
    retrieval_instructions: str | None = None,
) -> list[dict[str, str]]:
    """Build the OpenAI-style messages list for a single repair attempt.

    The user message has three base sections (in order):
      1. Fault location — file path, suspicious line, rank, and score.
      2. Buggy code     — the extracted function with 1-indexed line numbers;
                          the suspicious line is marked with ``-->`` so the model
                          can locate it at a glance.
      3. Task instruction — asks for a minimal fix inside a fenced code block.

    The optional kwargs enrich the prompt. ``few_shot_examples`` (when provided) is
    *prepended* before the fault location so the model sees reference fixes first;
    ``failing_test_source`` / ``error_traceback`` are inserted between the buggy code
    and the task instruction. When all enrichment kwargs are None the rendered prompt
    is byte-identical to the un-enriched version, so the feature is invisible unless a
    caller supplies context.

    Args:
        location:              Suspicious location from the FL result.
        function_source_text:  Source text of the enclosing function (or window).
        function_start_line:   1-indexed line number of the first line of
                               function_source_text inside the original file.
        system_message_text:   System-prompt text (see prompts/*.txt). Defaults to
                               the ``prompt1`` file when not supplied.
        few_shot_examples:      Formatted (buggy -> fixed) example pairs from sibling
                               bugs (see repair/llm/few_shot.py).
        failing_test_source:    Body of a relevant failing test.
        error_traceback:        Exception traceback from the failing run.
        retrieval_instructions: Optional retrieval protocol instructions inserted
                                before the final patch task.

    Returns:
        Two-entry list: [system message dict, user message dict].
    """
    if system_message_text is None:
        system_message_text = load_system_prompt(_DEFAULT_SYSTEM_PROMPT_NAME)

    user_sections: list[str] = []
    if few_shot_examples is not None:
        user_sections.append(_build_few_shot_section(few_shot_examples))
    user_sections.append(_build_fault_location_section(location))
    user_sections.append(
        _build_buggy_code_section(location, function_source_text, function_start_line)
    )
    if failing_test_source is not None:
        user_sections.append(_build_failing_test_section(failing_test_source))
    if error_traceback is not None:
        user_sections.append(_build_error_traceback_section(error_traceback))
    retrieval_is_enabled = retrieval_instructions is not None
    if retrieval_instructions is not None:
        user_sections.append(
            _build_retrieval_instructions_section(retrieval_instructions)
        )
    user_sections.append(
        _build_task_section_with_retrieval_option(location)
        if retrieval_is_enabled
        else _build_task_section(location)
    )

    user_content = "\n\n".join(user_sections)

    return [
        {"role": "system", "content": system_message_text},
        {"role": "user", "content": user_content},
    ]


# ---------------------------------------------------------------------------
# Private section builders
# ---------------------------------------------------------------------------


def _build_few_shot_section(few_shot_examples: str) -> str:
    return (
        "## Reference Fixes From This Project\n"
        "Real (buggy → fixed) examples from other bugs in this project. Use them as "
        "guidance for the fix style and output format — they are not the answer to "
        "the current bug.\n\n"
        f"{few_shot_examples.strip()}"
    )


def _build_fault_location_section(location: RankedLocation) -> str:
    score_str = f"{location.score:.4f}" if location.score is not None else "n/a"
    return (
        "## Fault Location\n"
        f"File: {location.file_path}\n"
        f"Suspicious line: {location.line} (rank {location.rank}, score {score_str})"
    )


def _build_buggy_code_section(
    location: RankedLocation,
    function_source_text: str,
    function_start_line: int,
) -> str:
    code_lines = function_source_text.splitlines()
    end_line = function_start_line + len(code_lines) - 1

    numbered_lines: list[str] = []
    for line_offset, code_line in enumerate(code_lines):
        current_line_number = function_start_line + line_offset
        if current_line_number == location.line:
            numbered_lines.append(f"{current_line_number}  -->  {code_line}")
        else:
            numbered_lines.append(f"{current_line_number}      {code_line}")

    numbered_code = "\n".join(numbered_lines)
    return (
        f"## Buggy Code (lines {function_start_line}–{end_line})\n"
        f"```python\n{numbered_code}\n```"
    )


def _build_failing_test_section(failing_test_source: str) -> str:
    return (
        "## Failing Test\n"
        "This is the test the fix must make pass. Use it to infer the intended "
        "behaviour.\n"
        f"```python\n{failing_test_source.strip()}\n```"
    )


def _build_error_traceback_section(error_traceback: str) -> str:
    return (
        "## Failure Traceback\n"
        "Running the failing test on the current (buggy) code produces this "
        "traceback:\n"
        f"```\n{error_traceback.strip()}\n```"
    )


def _build_retrieval_instructions_section(retrieval_instructions: str) -> str:
    return retrieval_instructions.strip()


def _build_task_section(location: RankedLocation) -> str:
    return (
        f"## Task\n"
        f"Fix the bug at line {location.line}. Return the corrected function in a Python fenced\n"
        "code block. Keep the fix minimal — change as few lines as necessary."
    )


def _build_task_section_with_retrieval_option(location: RankedLocation) -> str:
    """Build the closing task instruction for retrieval mode.

    This is the last thing the model reads, so it must offer the retrieve-or-patch
    choice. The plain task section closes with an unconditional "return the corrected
    function", which overrides the retrieval offer made earlier in the prompt.
    """
    return (
        f"## Task\n"
        f"Fix the bug at line {location.line}. First check the buggy region for project symbols\n"
        "whose definitions are not shown above — helpers it calls, methods invoked on self, the\n"
        "class it belongs to. If the fix depends on any of them, reply with a single RETRIEVE\n"
        "command and nothing else. Only when every symbol the fix depends on is already visible\n"
        "(or comes from the standard library), return the corrected function in a Python fenced\n"
        "code block. Keep the fix minimal — change as few lines as necessary."
    )
