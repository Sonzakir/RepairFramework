"""
Parse BugsInPy ground-truth patches and score localization rankings against them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from apr_framework.core.models import RankedLocation


@dataclass(frozen=True)
class GroundTruthLine:
    file_path: str
    line: int


def parse_bug_patch(patch_path: Path) -> list[GroundTruthLine]:
    """Return the set of (file, old-line-number) pairs deleted by the patch.

    Only lines that existed in the buggy version (the ``-`` side of the diff)
    are returned, because those are the lines fault localization tools are
    expected to rank highly.
    """
    if not patch_path.exists():
        return []

    text = patch_path.read_text(encoding="utf-8", errors="replace")
    results: list[GroundTruthLine] = []

    current_file: str | None = None
    old_line: int = 0

    hunk_header = re.compile(r"^@@ -(\d+)(?:,\d+)? \+\d+(?:,\d+)? @@")

    for raw in text.splitlines():
        # file header: --- a/some/path.py
        if raw.startswith("--- "):
            path = raw[4:].strip()
            current_file = _normalize_patch_path(path)
            old_line = 0
            continue

        if raw.startswith("+++ "):
            continue

        if current_file is None:
            continue

        m = hunk_header.match(raw)
        if m:
            old_line = int(m.group(1))
            continue

        if raw.startswith("-") and not raw.startswith("---"):
            results.append(GroundTruthLine(file_path=current_file, line=old_line))
            old_line += 1
            continue

        if raw.startswith("+") and not raw.startswith("+++"):
            continue

        # context line
        old_line += 1

    return results


def _normalize_patch_path(raw: str) -> str:
    """Strip ``a/`` / ``b/`` prefixes written by git-diff."""
    for prefix in ("a/", "b/"):
        if raw.startswith(prefix):
            return raw[len(prefix) :]
    if raw == "/dev/null":
        return ""
    return raw


def find_faulty_rank(
    ranked: list[RankedLocation],
    truth: list[GroundTruthLine],
) -> int | None:
    """Return the lowest rank at which any ground-truth line appears.

    For statement-level rows the match is ``(normalized_file, line)``.
    For function-level rows (where ``end_line`` is set) any truth line
    that falls inside ``[line, end_line]`` counts as a hit.

    Returns ``None`` when no ground-truth line appears anywhere in the ranking.
    """
    if not truth or not ranked:
        return None

    best: int | None = None
    for location in ranked:
        for gt in truth:
            if not _files_match(location.file_path, gt.file_path):
                continue
            if _line_matches(location, gt.line):
                if best is None or location.rank < best:
                    best = location.rank
                break

    return best


def in_top_k(rank: int | None, k: int) -> bool:
    """Return True when ``rank`` is not None and is at most ``k``."""
    return rank is not None and rank <= k


def _files_match(loc_path: str, truth_path: str) -> bool:
    """Compare FauxPy-reported paths against patch paths flexibly.

    FauxPy reports paths relative to the worktree (e.g. ``black.py`` or
    ``pysnooper/tracer.py``).  The patch may use the same relative path after
    stripping ``a/``.  We try an exact match first, then fall back to checking
    whether the location path ends with the truth path or vice-versa.
    """
    if not loc_path or not truth_path:
        return False
    loc_norm = loc_path.replace("\\", "/").strip("/")
    truth_norm = truth_path.replace("\\", "/").strip("/")
    if loc_norm == truth_norm:
        return True
    if loc_norm.endswith("/" + truth_norm) or truth_norm.endswith("/" + loc_norm):
        return True
    # last-resort: compare filenames only (catches different relative roots)
    return Path(loc_norm).name == Path(truth_norm).name


def _line_matches(location: RankedLocation, truth_line: int) -> bool:
    if location.line is None:
        return False
    if location.end_line is not None:
        return location.line <= truth_line <= location.end_line
    return location.line == truth_line
