"""Test-failure feedback turns and stop heuristics for iterative LLM repair.

Task 3 turns a single LLM query into a multi-turn conversation: after a patch
fails validation, the model is shown *why* it failed and asked to try again. This
module holds the pure string logic for that hand-off — building the feedback turn,
building the format-retry turn, and deciding when a location's conversation is no
longer making progress. Everything here is dependency-light (plain strings and
counts plus ``traceback_utils``) so it is fully unit-testable in isolation from the
algorithm and the LLM client.
"""

from apr_framework.repair.llm.traceback_utils import extract_last_traceback

# Case-insensitive substrings that signal the model has given up. Checked against
# the raw response text as a cheap "cannot improve further" heuristic (no extra LLM
# call to ask the model to self-report). Documented in the README as a heuristic,
# not a semantic judgment.
_REFUSAL_PHRASES = (
    "cannot fix",
    "unable to fix",
    "can't fix",
    "no further changes",
    "i am unable",
    "cannot determine a further fix",
)

_FORMAT_RETRY_MESSAGE = (
    "Your last reply did not contain a Python fenced code block (```python ... ```). "
    "Please respond again with ONLY the corrected function inside a single Python "
    "fenced code block."
)


def build_test_failure_feedback_message(
    raw_test_output: str,
    *,
    iteration_index: int,
    max_iterations: int,
    passed_count: int,
    failed_count: int,
    error_count: int,
    failure_kind: str = "trigger",
) -> str:
    """Build the '## Previous Attempt Failed' user turn from a failed validation.

    Mirrors the section style of ``prompt_builder.py``'s other sections. The body
    depends on *why* the patch failed:

      - ``"trigger"`` — the bug's own test still fails. The trigger run's output is
        meaningful, so include its traceback (via ``extract_last_traceback``), or
        fall back to the pass/fail/error counts when none is present.
      - ``"regression"`` — the patch made the target test pass but broke another
        test. The trigger run then shows all-green, so its counts/traceback would
        be misleading; instead state plainly that a regression was introduced and
        ask for a fix that changes no other behavior.

    Args:
        raw_test_output: Full transcript of the failed test run.
        iteration_index: 1-based turn number just completed (rendered as "turn N").
        max_iterations:  Conversation-turn budget for this location.
        passed_count:    Tests that passed under the failed patch.
        failed_count:    Tests that failed under the failed patch.
        error_count:     Tests that errored under the failed patch.
        failure_kind:    "trigger" or "regression" (see above).

    Returns:
        A ready-to-send user-turn string.
    """
    turn_header = f"## Previous Attempt Failed (turn {iteration_index} of {max_iterations})"

    if failure_kind == "regression":
        summary_line = (
            "Your fix made the target (bug-triggering) test pass, but it broke a "
            "test that previously passed — a regression."
        )
        failure_detail = (
            "No failure traceback is available: the target test itself passed, so "
            "the counts above do not reflect the regression. Revise the function so "
            "it fixes the bug WITHOUT changing any other behavior."
        )
    else:
        summary_line = (
            f"Your last fix did not pass validation — "
            f"passed={passed_count}, failed={failed_count}, errors={error_count}."
        )
        error_traceback = extract_last_traceback(raw_test_output)
        if error_traceback:
            failure_detail = f"Traceback:\n```\n{error_traceback}\n```"
        else:
            failure_detail = (
                "No Python traceback was captured; rely on the pass/fail counts above."
            )

    instruction = (
        "Please analyze this failure and provide a revised fix. Return the corrected "
        "function in a Python fenced code block, as before."
    )

    return f"{turn_header}\n{summary_line}\n\n{failure_detail}\n\n{instruction}"


def build_format_retry_message() -> str:
    """Return the fixed retry message sent when the LLM's reply had no fenced code block."""
    return _FORMAT_RETRY_MESSAGE


def is_no_improvement_signal(
    new_diff_text: str,
    previous_diff_text: str | None,
    llm_response_text: str,
) -> bool:
    """Detect when a location's conversation should stop for lack of progress.

    Two cheap, explainable heuristics:
      1. Identical diff twice in a row — the model is repeating itself.
      2. A refusal / "no further changes" phrase in the raw response text.

    Args:
        new_diff_text:      Diff just extracted from the latest response.
        previous_diff_text: Diff extracted from the immediately preceding turn, or
                            None on the first turn.
        llm_response_text:  Raw text of the latest LLM response.

    Returns:
        True if the loop should stop retrying this location.
    """
    if previous_diff_text is not None and new_diff_text == previous_diff_text:
        return True

    lowered_response = llm_response_text.lower()
    return any(phrase in lowered_response for phrase in _REFUSAL_PHRASES)
