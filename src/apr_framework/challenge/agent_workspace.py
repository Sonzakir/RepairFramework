"""The agent's workspace: an answer-free copy of a buggy checkout, and its diff.

An agent must not be able to see the answer. A BugsInPy checkout carries several
copies of it: the git history (the fix commit is in it), ``bugsinpy_*`` metadata
(``bugsinpy_patchfile.info`` names the files the fix touches, ``bugsinpy_bug.info``
the fix commit) and the compiled ``env/``. ``create_agent_workspace`` therefore
copies only the project's own files into a directory *outside the repository*,
keeps a pristine copy next to it, and installs two test shims. After the run,
``capture_workspace_diff`` turns every change (edits, new files, deletions) into
one unified diff against the pristine copy.

The workspace and the executor-visible evaluation checkout are kept apart: the
shims only drop request files in ``.test_channel/`` and the harness (``ShimServer``)
syncs the workspace into the evaluation checkout and runs the tests there.
"""

import difflib
import os
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from apr_framework.benchmarks.executor_view import remove_host_file
from apr_framework.repair.evaluator_integrity import (
    list_checkout_files,
    purge_bytecode_caches,
)

SHIM_NAMES = ("run_failing_test", "run_tests")
TEST_CHANNEL_DIRECTORY = ".test_channel"
_HARNESS_NAMES = frozenset(SHIM_NAMES) | {TEST_CHANNEL_DIRECTORY}
_CACHE_DIRECTORY_NAMES = frozenset({"__pycache__", ".pytest_cache", ".mypy_cache"})
# Never copied to the agent: answer-bearing metadata and build products.
_EXCLUDED_DIRECTORY_NAMES = frozenset(
    {".git", "env", ".venv", "venv", "__pycache__", ".pytest_cache", ".tox", ".eggs"}
)
_EXCLUDED_FILE_NAMES = frozenset({".python-version"})

_SHIM_TEMPLATE = """#!/bin/sh
# {description}
# The project's own Python environment lives behind this script; it takes no
# arguments and prints the test output.
dir=$(cd "$(dirname "$0")" && pwd)
channel="$dir/{channel}"
id="$(date +%s)-$$"
mkdir -p "$channel"
printf '%s\\n' "{kind}" > "$channel/$id.request.tmp"
mv "$channel/$id.request.tmp" "$channel/$id.request"
waited=0
while [ ! -f "$channel/$id.done" ]; do
  sleep 1
  waited=$((waited + 1))
  if [ "$waited" -gt {timeout_seconds} ]; then
    echo "The test run did not finish within {timeout_seconds} seconds."
    exit 2
  fi
done
cat "$channel/$id.output"
exit "$(cat "$channel/$id.exitcode")"
"""
_SHIM_DESCRIPTIONS = {
    "run_failing_test": ("trigger", "Runs the failing test."),
    "run_tests": ("regression", "Runs the test file(s) around the failing test."),
}


@dataclass(frozen=True)
class AgentWorkspace:
    """Paths of one agent attempt's workspace.

    Fields:
        root:          Directory holding both copies (outside the repository).
        workspace:     The agent's working directory (its cwd).
        pristine:      Untouched copy, the base of the captured diff.
        shim_contents: Bytes of each installed shim, to detect tampering.
    """

    root: Path
    workspace: Path
    pristine: Path
    shim_contents: dict[str, bytes]


def create_agent_workspace(
    source_checkout: Path, root: Path, shim_timeout_seconds: int
) -> AgentWorkspace:
    """Copy the checkout's project files to ``root`` and install the test shims."""
    if root.exists():
        shutil.rmtree(root)
    workspace = root / source_checkout.name
    pristine = root / f"{source_checkout.name}.pristine"
    for relative_path in list_project_files(source_checkout):
        for destination_root in (workspace, pristine):
            _copy_entry(
                source_checkout / relative_path, destination_root / relative_path
            )
    shim_contents = install_test_shims(workspace, shim_timeout_seconds)
    return AgentWorkspace(
        root=root, workspace=workspace, pristine=pristine, shim_contents=shim_contents
    )


def list_project_files(checkout: Path) -> list[str]:
    """The checkout's own files: no VCS, environment, cache or BugsInPy metadata."""
    return [
        relative_path
        for relative_path in list_checkout_files(checkout)
        if is_project_path(relative_path)
    ]


def is_project_path(relative_path: str) -> bool:
    """Whether a path belongs to the project (and may be shown to or synced from the agent)."""
    posix_path = PurePosixPath(relative_path)
    if set(posix_path.parts[:-1]) & _EXCLUDED_DIRECTORY_NAMES:
        return False
    if any(part.endswith(".egg-info") for part in posix_path.parts):
        return False
    if posix_path.name in _EXCLUDED_FILE_NAMES or posix_path.name.endswith(".pyc"):
        return False
    return not posix_path.name.startswith("bugsinpy_")


def install_test_shims(workspace: Path, timeout_seconds: int) -> dict[str, bytes]:
    shim_contents: dict[str, bytes] = {}
    for shim_name, (kind, description) in _SHIM_DESCRIPTIONS.items():
        shim_text = _SHIM_TEMPLATE.format(
            description=description,
            channel=TEST_CHANNEL_DIRECTORY,
            kind=kind,
            timeout_seconds=timeout_seconds,
        )
        shim_path = workspace / shim_name
        shim_path.write_text(shim_text, encoding="utf-8")
        shim_path.chmod(0o755)
        shim_contents[shim_name] = shim_text.encode("utf-8")
    (workspace / TEST_CHANNEL_DIRECTORY).mkdir(exist_ok=True)
    return shim_contents


def find_modified_shims(agent_workspace: AgentWorkspace) -> list[str]:
    """Shims the agent changed or removed (tampering with its own test harness)."""
    return [
        shim_name
        for shim_name, original_bytes in agent_workspace.shim_contents.items()
        if not (agent_workspace.workspace / shim_name).is_file()
        or (agent_workspace.workspace / shim_name).read_bytes() != original_bytes
    ]


def list_agent_files(workspace: Path) -> list[str]:
    """Every workspace file except caches and the harness, ``env/`` and ``.git`` included.

    Unlike a checkout listing, nothing the agent could create is hidden here, so a
    file it writes into ``env/`` or ``.git/`` still shows up in the captured diff
    (where the evaluator-integrity gate rejects it) instead of vanishing.
    """
    agent_files: list[str] = []
    for directory, sub_directories, file_names in os.walk(workspace):
        sub_directories[:] = [
            name for name in sub_directories if name not in _CACHE_DIRECTORY_NAMES
        ]
        for file_name in file_names:
            relative_path = (Path(directory) / file_name).relative_to(workspace)
            if relative_path.parts[0] in _HARNESS_NAMES or file_name.endswith(".pyc"):
                continue
            agent_files.append(relative_path.as_posix())
    return sorted(agent_files)


def sync_workspace_into_checkout(
    agent_workspace: AgentWorkspace, evaluation_checkout: Path
) -> list[str]:
    """Mirror the agent's project files into the evaluation checkout.

    Copies changed and new files, deletes files the agent deleted, and purges
    their bytecode caches. Paths outside the project (``env/``, ``bugsinpy_*``,
    ``.git``) are never written; they are returned so the run can report them.
    """
    workspace_paths = set(list_agent_files(agent_workspace.workspace))
    skipped_paths: list[str] = []
    changed_files: list[Path] = []

    for relative_path in sorted(workspace_paths):
        if not is_project_path(relative_path):
            skipped_paths.append(relative_path)
            continue
        source_path = agent_workspace.workspace / relative_path
        target_path = evaluation_checkout / relative_path
        if _entries_differ(source_path, target_path):
            _copy_entry(source_path, target_path)
            changed_files.append(target_path)

    for relative_path in list_project_files(evaluation_checkout):
        if relative_path not in workspace_paths:
            target_path = evaluation_checkout / relative_path
            remove_host_file(target_path)
            changed_files.append(target_path)

    purge_bytecode_caches(changed_files)
    return skipped_paths


def capture_workspace_diff(agent_workspace: AgentWorkspace) -> str:
    """Return one unified diff of every change the agent made, with a/ b/ headers."""
    pristine_paths = set(list_agent_files(agent_workspace.pristine))
    workspace_paths = set(list_agent_files(agent_workspace.workspace))
    sections = [
        render_file_diff(
            relative_path,
            _read_text_or_none(agent_workspace.pristine / relative_path),
            _read_text_or_none(agent_workspace.workspace / relative_path),
        )
        for relative_path in sorted(pristine_paths | workspace_paths)
        if _entries_differ(
            agent_workspace.pristine / relative_path,
            agent_workspace.workspace / relative_path,
        )
    ]
    return "".join(sections)


def render_file_diff(
    relative_path: str, old_text: str | None, new_text: str | None
) -> str:
    """One file's unified diff, ``/dev/null`` for creations and deletions."""
    old_header = f"a/{relative_path}" if old_text is not None else "/dev/null"
    new_header = f"b/{relative_path}" if new_text is not None else "/dev/null"
    old_lines = _split_keeping_final_newline_state(old_text or "")
    new_lines = _split_keeping_final_newline_state(new_text or "")
    diff_lines = list(
        difflib.unified_diff(
            old_lines, new_lines, fromfile=old_header, tofile=new_header, lineterm=""
        )
    )
    if not diff_lines:
        return f"--- {old_header}\n+++ {new_header}\n"
    rendered_lines: list[str] = []
    for line in diff_lines:
        if line.endswith(_NO_NEWLINE_SENTINEL):
            rendered_lines.append(line[: -len(_NO_NEWLINE_SENTINEL)])
            rendered_lines.append("\\ No newline at end of file")
        else:
            rendered_lines.append(line)
    return "\n".join(rendered_lines) + "\n"


_NO_NEWLINE_SENTINEL = "\0NO-NEWLINE"


def _split_keeping_final_newline_state(text: str) -> list[str]:
    lines = text.split("\n")
    if lines and lines[-1] == "":
        return lines[:-1]
    if lines:
        lines[-1] += _NO_NEWLINE_SENTINEL
    return lines


def _entries_differ(first_path: Path, second_path: Path) -> bool:
    first_exists = first_path.is_file() or first_path.is_symlink()
    second_exists = second_path.is_file() or second_path.is_symlink()
    if first_exists != second_exists:
        return True
    if not first_exists:
        return False
    if first_path.is_symlink() or second_path.is_symlink():
        return not (
            first_path.is_symlink()
            and second_path.is_symlink()
            and first_path.readlink() == second_path.readlink()
        )
    return first_path.read_bytes() != second_path.read_bytes()


def _copy_entry(source_path: Path, target_path: Path) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    if target_path.is_symlink():
        target_path.unlink()
    if source_path.is_symlink():
        target_path.unlink(missing_ok=True)
        target_path.symlink_to(source_path.readlink())
        return
    shutil.copyfile(source_path, target_path)


def _read_text_or_none(path: Path) -> str | None:
    if not (path.is_file() or path.is_symlink()):
        return None
    if path.is_symlink():
        return f"symlink -> {path.readlink()}\n"
    return path.read_bytes().decode("utf-8", "surrogateescape")
