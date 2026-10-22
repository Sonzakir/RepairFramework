"""Keep the BugsInPy executor's view of host-side file changes current.

Docker Desktop (macOS, Windows) shares host directories with containers through a
file-sharing layer that caches file sizes and directory entries inside its VM.
When the framework runs **on the host** (``challenge agent-run`` must, so it can
use the host's coding-agent login) and rewrites a file the executor has already
read, the executor keeps the old size and reads a truncated or padded file. A
file the host deletes stays visible to the executor. Measured on this project:
a 44-byte ``bugsinpy_run_test.sh`` rewritten to 83 bytes was still read as 44
bytes (5 of 5 trials), so tornado#14's developer-identical patch was judged
failing. Writes made from inside a container are coherent, so the documented
container mode never needed this.

Two steps inside the executor restore a correct view, and both are cheap:

1. remove every path the host deleted since the last command (a container-side
   ``rm`` drops the stale entry; the host no longer has the file); and
2. walk the command's checkout with ``find``, which refreshes cached sizes and
   directory entries for in-place rewrites, replaced files and recreated
   directories.

Host-side code that deletes anything under the BugsInPy workspace goes through
:func:`remove_host_file` / :func:`remove_host_tree`, which record the deletion.
``BugsInPyDockerExecutor`` drains the record and runs both steps right before
every command it executes, when the framework runs on the host.

One case needs more: a tree the *executor* will recreate (a checkout
destination). After a host-side delete the executor can keep stale entries for
the deleted directories and fail to create files in them, so such trees are
deleted from inside the executor (``BugsInPyToolchain.remove_workspace_tree``).
"""

import shlex
import shutil
import threading
from pathlib import Path, PurePosixPath

_ENVIRONMENT_DIRECTORY_NAME = "env"

_pending_deletions: set[Path] = set()
_pending_deletions_lock = threading.Lock()


def remove_host_file(path: Path) -> None:
    """Delete a file (or symlink) on the host and record it for the executor."""
    path.unlink(missing_ok=True)
    record_host_deletion(path)


def remove_host_directory(path: Path) -> None:
    """Delete an empty directory on the host and record it for the executor."""
    path.rmdir()
    record_host_deletion(path)


def remove_host_tree(path: Path) -> None:
    """Delete a directory tree on the host and record it for the executor."""
    shutil.rmtree(path, ignore_errors=True)
    record_host_deletion(path)


def record_host_deletion(path: Path) -> None:
    """Remember that ``path`` was deleted on the host (absolute, not resolved).

    The path is stored as given: resolving would follow a symlink that was just
    removed, or fail for a path that no longer exists.
    """
    with _pending_deletions_lock:
        _pending_deletions.add(Path(path).absolute())


def take_host_deletions() -> list[Path]:
    """Return and forget every deletion recorded since the last call."""
    with _pending_deletions_lock:
        deletions = sorted(_pending_deletions)
        _pending_deletions.clear()
    return deletions


def build_view_refresh_script(
    deleted_container_paths: list[str], refreshed_container_tree: str | None
) -> str | None:
    """The shell script that refreshes the executor's view, or None if nothing to do.

    Removing a path the host already deleted only drops the executor's stale
    entry. The walk skips the checkout's ``env/`` virtualenv, which only the
    executor writes. Errors are ignored: a path may already be gone in both views.
    """
    steps: list[str] = []
    if deleted_container_paths:
        quoted_paths = " ".join(shlex.quote(path) for path in deleted_container_paths)
        steps.append(f"rm -rf -- {quoted_paths} 2>/dev/null")
    if refreshed_container_tree is not None:
        tree = shlex.quote(refreshed_container_tree)
        environment = shlex.quote(
            str(PurePosixPath(refreshed_container_tree) / _ENVIRONMENT_DIRECTORY_NAME)
        )
        steps.append(
            f"find {tree} -path {environment} -prune -o -print >/dev/null 2>&1"
        )
    if not steps:
        return None
    return "; ".join(steps) + "; true"
