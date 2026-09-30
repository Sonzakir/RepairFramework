"""Two correctness metrics comparing a plausible patch to the developer fix.

A patch is *plausible* if the patched program passes the test suite. Given a
plausible patch, this module offers **two** independent ways to judge how close it
is to the developer's ground-truth fix (``bug_patch.txt``). They are complementary,
not alternatives — the runner records both.

--------------------------------------------------------------------------------
Metric 1 — exact diff match (:func:`is_correct_patch`, unchanged)
--------------------------------------------------------------------------------
A boolean: does the candidate reproduce the developer fix *exactly* (same changed
lines), ignoring only cosmetic reformatting. The steps:

  1. Build a *minimal* candidate diff by unparsing the original source the same way
     the patched source was produced (``ast.unparse(ast.parse(original))`` vs
     ``patched_source``). Cosmetic reformatting then appears on *both* sides and
     cancels out, leaving only the operator's real change.
  2. Reduce each diff (candidate and reference) to its set of *changed content
     lines* — the added (``+``) and removed (``-``) lines, whitespace-normalized,
     ignoring file headers, hunk headers and context lines.
  3. Declare the patch correct when, for the file it touches, the candidate's
     (added, removed) normalized line sets equal the reference's.

This is a deliberately strict, purely syntactic check. Known limitations: it does
not recognise semantically-equivalent-but-textually-different fixes, and developer
fixes that span multiple files or hunks (or add whole statements out of operator
reach) will not match a single-location template patch. Because the match is
byte-exact, a *high exact-match rate is itself a useful signal*: when an LLM keeps
reproducing the developer fix 1:1, that points to overfitting to — or memorisation
(data contamination) of — the benchmark's public fixes rather than independent
reasoning.

--------------------------------------------------------------------------------
Metric 2 — context similarity score (:func:`context_similarity_score`, new)
--------------------------------------------------------------------------------
A float in ``[0.0, 1.0]``: instead of pass/fail, *how close* is the candidate's
edit to the developer's, **including the surrounding context lines** the exact
metric throws away? Intuition - we line the candidate's changed region up against
the developer's changed region (each as a *hunk*: its added/removed lines plus the
unchanged lines around them) and measure their textual overlap with
``difflib.SequenceMatcher``:

  * ``1.0`` -> the two hunks are identical (same edit, same neighbourhood). This is
    guaranteed, not just typical: whenever :func:`is_correct_patch` already agrees
    the candidate is an exact match, the score short-circuits to ``1.0`` rather than
    trusting ``SequenceMatcher`` — which can otherwise land a point or two short of
    ``1.0`` on a true match, since ``ast.unparse`` reformatting elsewhere in the file
    can shift *unchanged* context lines the hunk-level comparison still looks at.
  * high-but-below-1.0 —> the fix lands in the same place and is nearly the same,
    differing only in small ways (e.g. a renamed local variable). The exact metric
    would call this ``False``; the score rewards the near-miss.
  * low —> a plausible patch that fixes the bug a *different* way than the developer;
    it shares only the surrounding context, so the score stays small.

Where the exact metric collapses "same fix, renamed variable" and "completely
different valid fix" into the same ``False`` bucket, this score separates them, and
a *spread* of high-but-sub-1.0 scores (rather than a cluster of exact 1.0s) is what
independent reasoning-to-the-same-region looks like versus verbatim recitation.

Both metrics reuse the same reformatting-neutral :func:`minimal_candidate_diff`, so
neither is fooled by ``ast.unparse`` cosmetics.
"""

import ast
import difflib
import re
from dataclasses import dataclass, field
from pathlib import Path

from apr_framework.core.models import PatchCandidate

_WHITESPACE_RUN = re.compile(r"\s+")


def _normalize_line(line: str) -> str:
    """Strip a leading +/- marker and collapse whitespace for robust comparison."""
    if line and line[0] in "+-":
        line = line[1:]
    return _WHITESPACE_RUN.sub(" ", line).strip()


@dataclass
class ChangedLines:
    """Normalized added/removed content lines for one file in a unified diff."""

    added: set[str] = field(default_factory=set)
    removed: set[str] = field(default_factory=set)

    def is_empty(self) -> bool:
        return not self.added and not self.removed


def parse_unified_diff(diff_text: str) -> dict[str, ChangedLines]:
    """Parse a unified diff into per-file normalized changed-line sets.

    Files are keyed by basename (e.g. ``black.py``) so that the candidate's
    ``a/black.py`` and the reference's ``a/black.py`` line up regardless of path
    prefixes. Header lines (``---``/``+++``/``diff``/``index``), hunk headers
    (``@@``) and context lines are ignored; only true ``+``/``-`` content lines
    are collected.
    """
    by_file: dict[str, ChangedLines] = {}
    current: ChangedLines | None = None

    for raw in diff_text.splitlines():
        if raw.startswith("+++ "):
            path = raw[4:].strip().split("\t", 1)[0]
            name = Path(path.split(" ", 1)[0]).name
            current = by_file.setdefault(name, ChangedLines())
            continue
        if (
            raw.startswith("--- ")
            or raw.startswith("diff ")
            or raw.startswith("index ")
        ):
            continue
        if raw.startswith("@@"):
            continue
        if current is None:
            continue
        if raw.startswith("+"):
            content = _normalize_line(raw)
            if content:
                current.added.add(content)
        elif raw.startswith("-"):
            content = _normalize_line(raw)
            if content:
                current.removed.add(content)

    return by_file


def _unparse_normalize(source: str) -> str:
    """Round-trip source through the AST so formatting is canonical, if parseable."""
    try:
        return ast.unparse(ast.parse(source))
    except SyntaxError:
        return source


def minimal_candidate_diff(original_source: str, patched_source: str) -> str:
    """Compute a reformatting-neutral unified diff for a candidate patch.

    Both sides go through ``ast.unparse`` so cosmetic differences (whitespace,
    redundant parens, quote style) cancel out, isolating the operator's semantic
    change. In the real pipeline ``patched_source`` is already an unparse result, so
    re-normalizing it is idempotent; doing it explicitly keeps the comparison robust
    to patched sources produced any other way.
    """
    normalized_original = _unparse_normalize(original_source)
    normalized_patched = _unparse_normalize(patched_source)

    if not normalized_original.endswith("\n"):
        normalized_original += "\n"
    if not normalized_patched.endswith("\n"):
        normalized_patched += "\n"

    diff = difflib.unified_diff(
        normalized_original.splitlines(keepends=True),
        normalized_patched.splitlines(keepends=True),
        fromfile="a/source",
        tofile="b/source",
    )
    return "".join(diff)


def is_correct_patch(candidate: PatchCandidate, reference_diff_text: str) -> bool:
    """Return True if the candidate matches the developer fix at the diff level.

    Args:
        candidate: A plausible patch. Uses ``metadata['patched_source']`` and
            ``metadata['source_path']`` to recompute a reformatting-neutral diff.
        reference_diff_text: The developer fix as a unified diff (BugsInPy
            ``bug_patch.txt`` content).

    Returns:
        True iff, for the source file the candidate changes, its normalized added
        and removed line sets equal the reference's for that file.
    """
    patched_source = candidate.metadata.get("patched_source")
    source_path_str = candidate.metadata.get("source_path")
    if not patched_source or not source_path_str or not reference_diff_text:
        return False

    source_path = Path(source_path_str)
    try:
        original_source = source_path.read_text(encoding="utf-8")
    except OSError:
        return False

    candidate_diff = minimal_candidate_diff(original_source, patched_source)
    candidate_changes = parse_unified_diff(candidate_diff)
    reference_changes = parse_unified_diff(reference_diff_text)

    # The candidate touches exactly one synthetic file ("source"); compare it
    # against the reference hunk for the same underlying file (matched by basename).
    candidate_set = next(
        (c for c in candidate_changes.values() if not c.is_empty()), None
    )
    if candidate_set is None:
        return False

    reference_set = reference_changes.get(source_path.name)
    if reference_set is None or reference_set.is_empty():
        return False

    return (
        candidate_set.added == reference_set.added
        and candidate_set.removed == reference_set.removed
    )


def extract_hunks(diff_text: str) -> list[str]:
    """Split a unified diff into hunk *bodies*, keeping their context lines.

    Unlike :func:`parse_unified_diff` — which discards context lines and pools the
    ``+``/``-`` lines into per-file sets — this preserves each hunk as a contiguous
    block of added, removed, and surrounding unchanged lines, so callers can measure
    how close two edits are *in their neighbourhood*, not just whether the changed
    lines match. The ``@@ ... @@`` header itself is dropped (its line numbers differ
    between the synthetic candidate diff and the real reference diff and would only
    add noise); file-header lines (``---``/``+++``/``diff``/``index``) are dropped
    too. Everything from one ``@@`` up to the next (or end of text) is one hunk body.
    """
    hunk_bodies: list[str] = []
    current_body_lines: list[str] | None = None

    for raw_line in diff_text.splitlines():
        if raw_line.startswith("@@"):
            if current_body_lines is not None:
                hunk_bodies.append("\n".join(current_body_lines))
            current_body_lines = []  # start a new hunk body; header excluded
            continue
        if current_body_lines is None:
            continue
        if (
            raw_line.startswith("--- ")
            or raw_line.startswith("+++ ")
            or raw_line.startswith("diff ")
            or raw_line.startswith("index ")
        ):
            continue
        current_body_lines.append(raw_line)

    if current_body_lines is not None:
        hunk_bodies.append("\n".join(current_body_lines))

    return hunk_bodies


def _normalize_hunk_for_similarity(hunk_body: str) -> str:
    """Collapse per-line whitespace while preserving each line's +/-/context marker.

    Formatting noise (indentation, spacing) is normalised away , as the exact metric
    does , so the similarity score reflects the edit and its neighbourhood rather
    than reformatting. The leading ``+``/``-``/space marker is kept so an added line
    is not confused with a removed or context line.
    """
    normalized_lines: list[str] = []
    for line in hunk_body.splitlines():
        marker = ""
        line_body = line
        if line and line[0] in "+- ":
            marker = line[0]
            line_body = line[1:]
        collapsed_body = _WHITESPACE_RUN.sub(" ", line_body).strip()
        normalized_lines.append(f"{marker}{collapsed_body}")
    return "\n".join(normalized_lines)


def context_similarity_score(
    candidate: PatchCandidate, reference_diff_text: str
) -> float:
    """Return how close the candidate's edit is to the developer fix, in ``[0, 1]``.

    Hunk-scoped context similarity (see the module docstring). Each candidate hunk
    (its changed lines *plus* the surrounding context) is compared against every
    developer hunk with ``difflib.SequenceMatcher``; the best pairing wins, and the
    overall score is the best-matching candidate hunk's ratio. ``1.0`` means an
    identical edit in an identical neighbourhood; lower means progressively more
    divergent.

    Args:
        candidate: A plausible patch. Uses ``metadata['patched_source']`` and
            ``metadata['source_path']`` to recompute a reformatting-neutral diff —
            the same inputs :func:`is_correct_patch` uses.
        reference_diff_text: The developer fix as a unified diff (BugsInPy
            ``bug_patch.txt`` content).

    Returns:
        A float in ``[0.0, 1.0]``. Degrades to ``0.0`` (never raises) when patch
        metadata is missing, the source file cannot be read, or either side yields
        no hunk — mirroring :func:`is_correct_patch`'s defensive contract. Always
        exactly ``1.0`` when :func:`is_correct_patch` already agrees the candidate
        is an exact match — hunk-level ``SequenceMatcher`` similarity can otherwise
        fall short of ``1.0`` on a true match, since it also compares unchanged
        context lines that ``ast.unparse`` can reformat elsewhere in the file.
    """
    if is_correct_patch(candidate, reference_diff_text):
        return 1.0

    patched_source = candidate.metadata.get("patched_source")
    source_path_str = candidate.metadata.get("source_path")
    if not patched_source or not source_path_str or not reference_diff_text:
        return 0.0

    source_path = Path(source_path_str)
    try:
        original_source = source_path.read_text(encoding="utf-8")
    except OSError:
        return 0.0

    candidate_diff = minimal_candidate_diff(original_source, patched_source)
    candidate_hunks = extract_hunks(candidate_diff)
    reference_hunks = extract_hunks(reference_diff_text)
    if not candidate_hunks or not reference_hunks:
        return 0.0

    normalized_reference_hunks = [
        _normalize_hunk_for_similarity(reference_hunk)
        for reference_hunk in reference_hunks
    ]

    best_ratio = 0.0
    for candidate_hunk in candidate_hunks:
        normalized_candidate_hunk = _normalize_hunk_for_similarity(candidate_hunk)
        for normalized_reference_hunk in normalized_reference_hunks:
            pairing_ratio = difflib.SequenceMatcher(
                None, normalized_candidate_hunk, normalized_reference_hunk
            ).ratio()
            if pairing_ratio > best_ratio:
                best_ratio = pairing_ratio

    return best_ratio


_SIMILARITY_BANDS: tuple[tuple[float, str], ...] = (
    (1.0, "identical to the developer fix"),
    (0.85, "very similar (nearly the same edit)"),
    (0.6, "similar (recognizable overlap)"),
    (0.3, "loosely similar"),
    (0.0, "different (little in common)"),
)


def describe_similarity_score(score: float) -> str:
    """Translate a :func:`context_similarity_score` value into a human-readable band.

    The raw float is hard to eyeball at a glance — this maps it onto the same bands
    used in the module docstring's worked example, so callers (the CLI, reports) can
    show e.g. ``0.92 (very similar)`` instead of a bare number. Bounds are inclusive
    on the low end of each band; ``1.0`` only ever occurs via
    :func:`context_similarity_score`'s :func:`is_correct_patch` short-circuit.
    """
    for lower_bound, label in _SIMILARITY_BANDS:
        if score >= lower_bound:
            return label
    return _SIMILARITY_BANDS[-1][1]
