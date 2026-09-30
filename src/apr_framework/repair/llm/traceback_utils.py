"""Shared extraction of Python tracebacks from raw test output.

Both the Task 2 context enricher (which shows the model the failure on the
*unpatched* checkout) and the Task 3 iterative feedback loop (which shows the
model the failure of its *own* last patch) need the same operation: pull the last
Python traceback block out of a potentially large pytest transcript, capped so it
never dominates the prompt. This module is the single home for that logic.
"""

_TRACEBACK_MARKER = "Traceback (most recent call last):"
_TRACEBACK_MAX_LINES = 60


def extract_last_traceback(
    raw_output: str, max_lines: int = _TRACEBACK_MAX_LINES
) -> str | None:
    """Return the last Python traceback block from test output, capped in length.

    Args:
        raw_output: Full stdout/stderr transcript of a test run.
        max_lines:  Maximum number of traceback lines to keep (from the top of the
                    block); longer tracebacks are truncated.

    Returns:
        The trimmed traceback text, or None if no traceback marker is present.
    """
    marker_index = raw_output.rfind(_TRACEBACK_MARKER)
    if marker_index == -1:
        return None

    traceback_lines = raw_output[marker_index:].splitlines()
    if len(traceback_lines) > max_lines:
        traceback_lines = traceback_lines[:max_lines]
    return "\n".join(traceback_lines).strip() or None
