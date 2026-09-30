"""Few-shot example gathering for LLM repair prompts.

A ``fix examples`` context-enrichment strategy: prepend a small number of real
``(buggy code -> fixed code)`` pairs taken from *other* bugs of the **same**
BugsInPy project, so the model sees how bugs in this codebase are typically fixed
(style, size, output format) before it is asked to fix the current one.

The pairs are reconstructed offline from each example bug's
``bugs/<id>/bug_patch.txt`` developer diff — no checkout, compile, or test run is
required, so this enrichment is cheap and side-effect free. Selection is
deterministic (lowest other bug ids first) so a run is reproducible. Every step is
best-effort: a missing/oversized/garbled patch is skipped, and if no usable example
can be built the whole strategy degrades to ``None`` (the prompt is then unchanged).
"""

import logging

from apr_framework.benchmarks.bugsinpy import BugsInPyAdapter
from apr_framework.core.models import BugIdentifier

logger = logging.getLogger(__name__)

# Skip an example whose developer diff changes more than this many lines: large
# multi-hunk fixes make poor, prompt-bloating few-shot examples.
_MAX_EXAMPLE_CHANGED_LINES = 40
# Trim each reconstructed before/after snippet to at most this many lines.
_MAX_EXAMPLE_SNIPPET_LINES = 60


def build_few_shot_examples(
    adapter: BugsInPyAdapter,
    bug: BugIdentifier,
    few_shot_count: int,
) -> str | None:
    """Build up to ``few_shot_count`` (buggy -> fixed) examples from sibling bugs.

    Args:
        adapter:        BugsInPyAdapter used to list bugs and read their patches.
        bug:            The bug currently being repaired (excluded from examples).
        few_shot_count: How many examples to include; <= 0 disables the strategy.

    Returns:
        Formatted example text ready to drop into the prompt, or None when the
        strategy is disabled or no usable example could be built.
    """
    if few_shot_count < 1:
        return None

    example_blocks: list[str] = []
    for candidate_bug_id in _other_bug_ids_ascending(adapter, bug):
        if len(example_blocks) >= few_shot_count:
            break
        example_block = _build_one_example(
            adapter, bug, candidate_bug_id, len(example_blocks) + 1
        )
        if example_block is not None:
            example_blocks.append(example_block)

    if not example_blocks:
        logger.warning(
            "Few-shot requested (%d) but no usable example found for %s#%d",
            few_shot_count,
            bug.project,
            bug.bug_id,
        )
        return None

    return "\n\n".join(example_blocks)


# ---------------------------------------------------------------------------
# Example selection
# ---------------------------------------------------------------------------


def _other_bug_ids_ascending(
    adapter: BugsInPyAdapter, bug: BugIdentifier
) -> list[int]:
    """Return the project's bug ids (ascending) excluding the current bug."""
    try:
        project_bug_infos = adapter.list_bugs(bug.project)
    except Exception as listing_error:
        logger.warning(
            "Cannot list bugs for few-shot selection on %s: %s",
            bug.project,
            listing_error,
        )
        return []

    return sorted(
        bug_info.identifier.bug_id
        for bug_info in project_bug_infos
        if bug_info.identifier.bug_id != bug.bug_id
    )


# ---------------------------------------------------------------------------
# Single-example construction
# ---------------------------------------------------------------------------


def _build_one_example(
    adapter: BugsInPyAdapter,
    bug: BugIdentifier,
    example_bug_id: int,
    example_index: int,
) -> str | None:
    """Read one sibling bug's fix and format it as a (buggy -> fixed) block, or None."""
    example_bug = BugIdentifier(
        benchmark=bug.benchmark, project=bug.project, bug_id=example_bug_id
    )
    patch_diff_text = adapter.get_reference_patch(example_bug)
    if not patch_diff_text:
        return None

    if _changed_line_count(patch_diff_text) > _MAX_EXAMPLE_CHANGED_LINES:
        return None

    reconstructed = _reconstruct_before_after(patch_diff_text)
    if reconstructed is None:
        return None

    before_text, after_text = reconstructed
    return _format_example(example_index, bug.project, example_bug_id, before_text, after_text)


def _changed_line_count(patch_diff_text: str) -> int:
    """Count added/removed content lines in a unified diff (ignores headers)."""
    changed_line_count = 0
    for diff_line in patch_diff_text.splitlines():
        if diff_line.startswith(("+++", "---")):
            continue
        if diff_line.startswith(("+", "-")):
            changed_line_count += 1
    return changed_line_count


def _reconstruct_before_after(patch_diff_text: str) -> tuple[str, str] | None:
    """Rebuild the pre-fix and post-fix source snippets from a unified diff.

    Walks every hunk body: context lines go to both sides, ``-`` lines to the
    "before" side only, ``+`` lines to the "after" side only. Header lines and the
    "\\ No newline at end of file" marker are ignored. Returns None when no hunk
    content was found.
    """
    before_lines: list[str] = []
    after_lines: list[str] = []
    inside_hunk = False

    for diff_line in patch_diff_text.splitlines():
        if diff_line.startswith("@@"):
            inside_hunk = True
            continue
        if diff_line.startswith(("diff ", "index ", "--- ", "+++ ")):
            inside_hunk = False
            continue
        if not inside_hunk or diff_line.startswith("\\"):
            continue

        if diff_line.startswith("+"):
            after_lines.append(diff_line[1:])
        elif diff_line.startswith("-"):
            before_lines.append(diff_line[1:])
        else:
            context_line = diff_line[1:] if diff_line.startswith(" ") else diff_line
            before_lines.append(context_line)
            after_lines.append(context_line)

    if not before_lines and not after_lines:
        return None

    return _join_trimmed(before_lines), _join_trimmed(after_lines)


def _join_trimmed(snippet_lines: list[str]) -> str:
    """Join snippet lines, capping length so a single example cannot bloat the prompt."""
    if len(snippet_lines) > _MAX_EXAMPLE_SNIPPET_LINES:
        snippet_lines = snippet_lines[:_MAX_EXAMPLE_SNIPPET_LINES] + ["# ... (truncated)"]
    return "\n".join(snippet_lines)


def _format_example(
    example_index: int,
    project: str,
    example_bug_id: int,
    before_text: str,
    after_text: str,
) -> str:
    """Render one (buggy -> fixed) pair as a labelled two-block example."""
    return (
        f"### Example {example_index}: {project}#{example_bug_id}\n"
        "Buggy code:\n"
        f"```python\n{before_text}\n```\n"
        "Fixed code:\n"
        f"```python\n{after_text}\n```"
    )
