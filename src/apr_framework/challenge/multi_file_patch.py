"""Apply and restore unified diffs that touch several files, create or delete them.

External agents produce patches the framework's own engines never do: edits to
several files, new modules, deleted files. ``MultiFilePatch`` applies such a diff
to a checkout in pure Python (no ``patch`` binary, no fuzz), remembers everything
it changed, and ``restore()`` puts the checkout back exactly: modified and deleted
files get their original bytes, created files and the directories made for them
are removed. Call ``restore()`` in a ``finally`` block; ``apply_patch_and_validate``
does.

Every target path is resolved relative to the checkout and must stay inside it;
a path that escapes (``..``, absolute paths outside the workspace) is refused
before anything is written.
"""

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from apr_framework.benchmarks.executor_view import (
    remove_host_directory,
    remove_host_file,
)
from apr_framework.core.exceptions import APRFrameworkError
from apr_framework.repair.unified_diff import (
    FileDiff,
    normalise_diff_path,
    parse_unified_diff,
)

# How far (in lines) a hunk may have drifted from its recorded position.
_MAX_HUNK_OFFSET = 200


class PatchApplyError(APRFrameworkError):
    """A diff cannot be applied cleanly to the checkout."""


@dataclass(frozen=True)
class PlannedFileChange:
    """The new state of one file, computed before anything is written.

    Fields:
        relative_path: Path relative to the checkout (POSIX form).
        new_bytes:     Content after the patch; None when the file is deleted.
    """

    relative_path: str
    new_bytes: bytes | None


@dataclass
class MultiFilePatch:
    """A diff planned against one checkout, with exact restore."""

    worktree: Path
    changes: list[PlannedFileChange]
    _original_bytes: dict[str, bytes | None] = field(default_factory=dict)
    _created_directories: list[Path] = field(default_factory=list)

    @classmethod
    def plan(cls, diff_text: str, worktree: Path) -> "MultiFilePatch":
        """Compute every file's new content; raise PatchApplyError if it won't apply."""
        worktree = worktree.resolve()
        file_diffs = parse_unified_diff(diff_text)
        if diff_text.strip() and not file_diffs:
            raise PatchApplyError("The diff contains no file sections.")
        changes = [
            _plan_file_change(file_diff, worktree)
            for file_diff in file_diffs
            if file_diff.hunk_lines or file_diff.is_deletion
        ]
        planned_paths = [change.relative_path for change in changes]
        if len(set(planned_paths)) != len(planned_paths):
            raise PatchApplyError("The diff patches the same file in two sections.")
        return cls(worktree=worktree, changes=changes)

    @property
    def touched_paths(self) -> tuple[str, ...]:
        return tuple(change.relative_path for change in self.changes)

    def apply(self) -> None:
        """Write the planned changes, remembering the original state of each file."""
        for change in self.changes:
            target_path = self.worktree / change.relative_path
            self._original_bytes[change.relative_path] = (
                target_path.read_bytes() if target_path.is_file() else None
            )
            if change.new_bytes is None:
                remove_host_file(target_path)
                continue
            self._create_missing_parent_directories(target_path)
            target_path.write_bytes(change.new_bytes)

    def restore(self) -> None:
        """Undo ``apply``: original bytes back, created files and directories gone."""
        for relative_path, original_bytes in self._original_bytes.items():
            target_path = self.worktree / relative_path
            if original_bytes is None:
                remove_host_file(target_path)
            else:
                target_path.parent.mkdir(parents=True, exist_ok=True)
                target_path.write_bytes(original_bytes)
        for directory in reversed(self._created_directories):
            if directory.is_dir() and not any(directory.iterdir()):
                remove_host_directory(directory)
        self._original_bytes.clear()
        self._created_directories.clear()

    def _create_missing_parent_directories(self, target_path: Path) -> None:
        missing_directories = [
            parent
            for parent in reversed(target_path.parents)
            if parent != self.worktree
            and self.worktree in parent.parents
            and not parent.exists()
        ]
        for directory in missing_directories:
            directory.mkdir()
            self._created_directories.append(directory)


def without_evaluator_files(
    diff_text: str, worktree_name: str, is_evaluator_path
) -> str:
    """Return the diff with every section that touches an evaluator file removed.

    Used to replay a Type T candidate's *non-test* changes against the original
    tests. ``is_evaluator_path`` takes a checkout-relative path.
    """
    kept_sections: list[str] = []
    for file_diff in parse_unified_diff(diff_text):
        relative_path = normalise_diff_path(file_diff.target_path, worktree_name)
        if is_evaluator_path(relative_path):
            continue
        kept_sections.append(render_file_diff(file_diff, worktree_name))
    return "".join(kept_sections)


def render_file_diff(file_diff: FileDiff, worktree_name: str) -> str:
    """Re-emit one section with ``a/`` / ``b/`` relative headers."""
    old_header = (
        file_diff.old_path
        if file_diff.is_creation
        else f"a/{normalise_diff_path(file_diff.old_path, worktree_name)}"
    )
    new_header = (
        file_diff.new_path
        if file_diff.is_deletion
        else f"b/{normalise_diff_path(file_diff.new_path, worktree_name)}"
    )
    lines = [f"--- {old_header}", f"+++ {new_header}", *file_diff.hunk_lines]
    return "\n".join(lines) + "\n"


def _plan_file_change(file_diff: FileDiff, worktree: Path) -> PlannedFileChange:
    relative_path = _checked_relative_path(file_diff.target_path, worktree)
    target_path = worktree / relative_path

    if file_diff.is_creation:
        if target_path.exists():
            raise PatchApplyError(f"Cannot create {relative_path}: it already exists.")
        original_text = ""
    else:
        if not target_path.is_file():
            raise PatchApplyError(f"Cannot patch {relative_path}: no such file.")
        # surrogateescape round-trips any bytes, so non-UTF-8 files survive intact.
        original_text = target_path.read_bytes().decode("utf-8", "surrogateescape")
    newline = "\r\n" if "\r\n" in original_text else "\n"
    original_lines = original_text.splitlines()
    ends_with_newline = original_text.endswith("\n") or original_text == ""

    patched_lines, patched_ends_with_newline = _apply_hunks(
        original_lines, ends_with_newline, file_diff.hunk_lines, relative_path
    )
    if file_diff.is_deletion:
        if patched_lines:
            raise PatchApplyError(
                f"Deletion of {relative_path} does not remove all of its lines."
            )
        return PlannedFileChange(relative_path=relative_path, new_bytes=None)

    patched_text = newline.join(patched_lines)
    if patched_lines and patched_ends_with_newline:
        patched_text += newline
    return PlannedFileChange(
        relative_path=relative_path,
        new_bytes=patched_text.encode("utf-8", "surrogateescape"),
    )


def _checked_relative_path(raw_path: str, worktree: Path) -> str:
    relative_path = normalise_diff_path(raw_path, worktree.name)
    posix_path = PurePosixPath(relative_path)
    if (
        posix_path.is_absolute()
        or ".." in posix_path.parts
        or not relative_path
        or relative_path == "."
    ):
        raise PatchApplyError(
            f"Refusing to patch a path outside the checkout: {raw_path}"
        )
    resolved_path = (worktree / relative_path).resolve()
    if worktree not in resolved_path.parents:
        raise PatchApplyError(
            f"Refusing to patch a path outside the checkout: {raw_path}"
        )
    return relative_path


def _apply_hunks(
    original_lines: list[str],
    ends_with_newline: bool,
    hunk_lines: list[str],
    relative_path: str,
) -> tuple[list[str], bool]:
    """Apply every hunk in order; return the new lines and trailing-newline state."""
    result_lines = list(original_lines)
    result_ends_with_newline = ends_with_newline
    line_offset = 0
    for hunk in _split_hunks(hunk_lines):
        old_lines, new_lines, old_start, new_has_no_newline = _hunk_sides(hunk)
        expected_index = max(old_start - 1, 0) + line_offset
        match_index = _find_hunk(result_lines, old_lines, expected_index)
        if match_index is None:
            raise PatchApplyError(
                f"Hunk at line {old_start} does not apply to {relative_path}."
            )
        result_lines[match_index : match_index + len(old_lines)] = new_lines
        line_offset += len(new_lines) - len(old_lines)
        if match_index + len(new_lines) == len(result_lines):
            result_ends_with_newline = not new_has_no_newline
    return result_lines, result_ends_with_newline


def _split_hunks(hunk_lines: list[str]) -> list[list[str]]:
    hunks: list[list[str]] = []
    for line in hunk_lines:
        if line.startswith("@@"):
            hunks.append([line])
        elif hunks:
            hunks[-1].append(line)
    return hunks


def _hunk_sides(hunk: list[str]) -> tuple[list[str], list[str], int, bool]:
    """Return (old lines, new lines, old start, new side lacks final newline)."""
    old_start = int(hunk[0].split()[1].lstrip("-").split(",")[0])
    old_lines: list[str] = []
    new_lines: list[str] = []
    new_has_no_newline = False
    previous_marker = ""
    for line in hunk[1:]:
        if line.startswith("\\"):
            if previous_marker in ("+", " "):
                new_has_no_newline = True
            continue
        marker, text = (line[:1], line[1:].rstrip("\r")) if line else (" ", "")
        if marker in (" ", "-"):
            old_lines.append(text)
        if marker in (" ", "+"):
            new_lines.append(text)
        previous_marker = marker
    return old_lines, new_lines, old_start, new_has_no_newline


def _find_hunk(
    lines: list[str], old_lines: list[str], expected_index: int
) -> int | None:
    """Find where ``old_lines`` occur, preferring the closest match to the expectation."""
    if not old_lines:
        return min(max(expected_index, 0), len(lines))
    for distance in range(_MAX_HUNK_OFFSET + 1):
        for candidate_index in {expected_index - distance, expected_index + distance}:
            if (
                0 <= candidate_index <= len(lines) - len(old_lines)
                and lines[candidate_index : candidate_index + len(old_lines)]
                == old_lines
            ):
                return candidate_index
    return None
