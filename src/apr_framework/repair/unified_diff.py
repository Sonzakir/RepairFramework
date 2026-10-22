"""Parse unified diffs into per-file sections.

Repair backends and external agents produce diffs in several header styles:

  - absolute paths (``--- /workspace/.workspace/bugsinpy/black_3/black/x.py``), as
    written by the LLM engine's ``patch_extractor``;
  - git style (``diff --git a/x.py b/x.py`` with ``--- a/x.py`` / ``+++ b/x.py``),
    as written by ``git diff`` or ``diff -ruN`` with labels;
  - plain relative paths (``--- x.py``).

``parse_unified_diff`` turns any of them into ``FileDiff`` sections that say which
file a section touches, whether it creates or deletes that file, and which lines
it adds and removes. The evaluator-integrity gate and the multi-file patch
applier both read the same sections, so they always agree on what a diff touches.
"""

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

DEV_NULL = "/dev/null"

_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_GIT_HEADER = re.compile(r"^diff --git (\S+) (\S+)")
# BugsInPy workspace prefix: ".workspace/bugsinpy/<project>_<id>/<project>/<rest>".
_WORKSPACE_PREFIX = re.compile(
    r"(?:^|/)\.workspace/bugsinpy/[^/]+_\d+/[^/]+/(?P<rest>.+)$"
)


@dataclass
class FileDiff:
    """One file's section of a unified diff.

    Fields:
        old_path:      Path on the ``---`` line (``/dev/null`` for a created file).
        new_path:      Path on the ``+++`` line (``/dev/null`` for a deleted file).
        added_lines:   Text of every ``+`` line in the section's hunks.
        removed_lines: Text of every ``-`` line in the section's hunks.
        hunk_lines:    The raw hunk text (``@@`` headers included), used to re-emit
                       a single-file diff for ``patch``.
    """

    old_path: str
    new_path: str
    added_lines: list[str] = field(default_factory=list)
    removed_lines: list[str] = field(default_factory=list)
    hunk_lines: list[str] = field(default_factory=list)

    @property
    def is_creation(self) -> bool:
        return self.old_path == DEV_NULL

    @property
    def is_deletion(self) -> bool:
        return self.new_path == DEV_NULL

    @property
    def target_path(self) -> str:
        """The path this section writes to (the old path for a deletion)."""
        return self.old_path if self.is_deletion else self.new_path


def parse_unified_diff(diff_text: str) -> list[FileDiff]:
    """Split a unified diff into one ``FileDiff`` per touched file.

    Header timestamps (``\\t2024-01-01 ...``) are stripped. ``git diff`` sections
    without hunks (pure mode changes, binary files) still yield a section from
    their ``diff --git`` line so the touched path is never lost.
    """
    file_diffs: list[FileDiff] = []
    current: FileDiff | None = None
    pending_git_paths: tuple[str, str] | None = None
    lines = diff_text.splitlines()
    index = 0

    while index < len(lines):
        line = lines[index]

        git_match = _GIT_HEADER.match(line)
        if git_match:
            _append_git_only_section(file_diffs, pending_git_paths)
            pending_git_paths = (git_match.group(1), git_match.group(2))
            current = None
            index += 1
            continue

        if (
            line.startswith("--- ")
            and index + 1 < len(lines)
            and lines[index + 1].startswith("+++ ")
        ):
            current = FileDiff(
                old_path=_strip_header_path(line[4:]),
                new_path=_strip_header_path(lines[index + 1][4:]),
            )
            file_diffs.append(current)
            pending_git_paths = None
            index += 2
            continue

        if current is not None and _HUNK_HEADER.match(line):
            index = _consume_hunk(lines, index, current)
            continue

        index += 1

    _append_git_only_section(file_diffs, pending_git_paths)
    return file_diffs


def normalise_diff_path(raw_path: str, worktree_name: str | None = None) -> str:
    """Return a header path relative to the project root, in POSIX form.

    Strips git ``a/`` / ``b/`` prefixes and BugsInPy workspace prefixes (absolute
    headers written inside or outside the framework container). ``worktree_name``
    is the checkout directory name (the project); when given, an absolute path
    containing ``/<worktree_name>/`` is cut after the last such component.
    """
    path = raw_path.strip()
    if path == DEV_NULL:
        return path

    workspace_match = _WORKSPACE_PREFIX.search(path)
    if workspace_match:
        return workspace_match.group("rest")

    if worktree_name and path.startswith("/"):
        marker = f"/{worktree_name}/"
        if marker in path:
            return path.rsplit(marker, 1)[1]

    if path.startswith(("a/", "b/", "./")):
        path = path[2:]
    return str(PurePosixPath(path))


def touched_relative_paths(
    diff_text: str, worktree_name: str | None = None
) -> list[str]:
    """Return every project-relative path a diff creates, modifies or deletes."""
    paths: list[str] = []
    for file_diff in parse_unified_diff(diff_text):
        for raw_path in (file_diff.old_path, file_diff.new_path):
            if raw_path == DEV_NULL:
                continue
            relative_path = normalise_diff_path(raw_path, worktree_name)
            if relative_path not in paths:
                paths.append(relative_path)
    return paths


def _strip_header_path(header_value: str) -> str:
    return header_value.split("\t")[0].strip()


def _consume_hunk(lines: list[str], index: int, current: FileDiff) -> int:
    """Read one hunk starting at ``lines[index]``; return the index after it."""
    header_match = _HUNK_HEADER.match(lines[index])
    assert header_match is not None
    old_remaining = int(header_match.group(2) or "1")
    new_remaining = int(header_match.group(4) or "1")
    current.hunk_lines.append(lines[index])
    index += 1

    while index < len(lines) and (old_remaining > 0 or new_remaining > 0):
        line = lines[index]
        if line.startswith("\\"):
            current.hunk_lines.append(line)
            index += 1
            continue
        marker = line[:1]
        if marker == "+":
            current.added_lines.append(line[1:])
            new_remaining -= 1
        elif marker == "-":
            current.removed_lines.append(line[1:])
            old_remaining -= 1
        elif marker == " " or line == "":
            old_remaining -= 1
            new_remaining -= 1
        else:
            break
        current.hunk_lines.append(line)
        index += 1

    # A trailing "\ No newline at end of file" belongs to the hunk just read.
    if index < len(lines) and lines[index].startswith("\\"):
        current.hunk_lines.append(lines[index])
        index += 1
    return index


def _append_git_only_section(
    file_diffs: list[FileDiff], pending_git_paths: tuple[str, str] | None
) -> None:
    """Record a ``diff --git`` section that had no ``---``/``+++`` pair."""
    if pending_git_paths is None:
        return
    old_path, new_path = pending_git_paths
    file_diffs.append(FileDiff(old_path=old_path, new_path=new_path))
