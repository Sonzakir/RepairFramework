"""Build the change set: every file before and after the change.

- ``changes_from_diff``: a unified diff applied in memory to a tree (the tree is
  the "before" side; nothing is written).
- ``changes_from_git``: the working tree of a git repository against a base
  revision (``git diff <base>``: committed, staged and unstaged changes to tracked
  files; untracked files are not included).

Binary files are skipped.
"""

import subprocess
from pathlib import Path

from apr_framework.challenge.multi_file_patch import MultiFilePatch, PatchApplyError
from apr_framework.core.exceptions import ConfigurationError
from apr_framework.tamper.models import FileChange


def changes_from_diff(diff_text: str, base: Path) -> list[FileChange]:
    try:
        patch = MultiFilePatch.plan(diff_text, base)
    except PatchApplyError as error:
        raise ConfigurationError(
            f"The diff does not apply to {base}: {error}"
        ) from error
    changes: list[FileChange] = []
    for change in patch.changes:
        old_path = base / change.relative_path
        old_text = _text(old_path.read_bytes()) if old_path.is_file() else None
        new_text = None if change.new_bytes is None else _text(change.new_bytes)
        if (old_path.is_file() and old_text is None) or (
            change.new_bytes is not None and new_text is None
        ):
            continue
        changes.append(FileChange(change.relative_path, old_text, new_text))
    return changes


def changes_from_git(repo: Path, base_revision: str) -> list[FileChange]:
    names = _git(
        repo, "diff", "--no-renames", "--name-only", base_revision, "--"
    ).splitlines()
    changes: list[FileChange] = []
    for relative_path in names:
        if not relative_path.strip():
            continue
        old_bytes = _git_show(repo, base_revision, relative_path)
        new_path = repo / relative_path
        new_bytes = new_path.read_bytes() if new_path.is_file() else None
        old_text = None if old_bytes is None else _text(old_bytes)
        new_text = None if new_bytes is None else _text(new_bytes)
        if (old_bytes is not None and old_text is None) or (
            new_bytes is not None and new_text is None
        ):
            continue
        changes.append(FileChange(relative_path, old_text, new_text))
    return changes


def _git(repo: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise ConfigurationError(
            f"git {' '.join(arguments)} failed: {completed.stderr.strip()}"
        )
    return completed.stdout


def _git_show(repo: Path, revision: str, relative_path: str) -> bytes | None:
    completed = subprocess.run(
        ["git", "-C", str(repo), "show", f"{revision}:{relative_path}"],
        capture_output=True,
        check=False,
    )
    return completed.stdout if completed.returncode == 0 else None


def _text(data: bytes) -> str | None:
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None
