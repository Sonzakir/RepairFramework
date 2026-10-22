"""Run the agent's test shims where its code cannot reach the answer.

Whenever the agent calls ``./run_failing_test`` or ``./run_tests``, the project's
tests import the code the agent wrote, so that code runs inside an executor. The
shared BugsInPy executor would let it read the developer fix
(``/home/bugsinpy/projects/<project>/bugs/<id>/bug_patch.txt``), the checkout's
git history (which contains the fix commit), every other checkout, and the
internet, and print any of it into the test output the agent reads. The agent's
own sandbox does not cover the executor.

So, before the agent starts:

1. ``strip_answer_material`` removes the shim checkout's ``.git`` and
   ``bugsinpy_patchfile.info`` and blanks the fix commit id in
   ``bugsinpy_bug.info`` (inside the executor, so its view stays coherent);
2. ``sealed_test_adapter`` starts a throwaway executor for the attempt with no
   network, only the shim checkout's run directory mounted, read-only scripts and
   interpreters, and no BugsInPy projects. The shim server runs the agent's tests
   there.

``read_answer_markers`` keeps the fix commit id (read before stripping) so the
transcript scan can flag any test output that still shows it.
"""

import re
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

from apr_framework.benchmarks.bugsinpy import BugsInPyAdapter
from apr_framework.core.models import CheckoutResult

SEALED_CONTAINER_PREFIX = "apr-agent-tests-"
ANSWER_BEARING_PATHS = (".git", "bugsinpy_patchfile.info")
_FIXED_COMMIT_PATTERN = re.compile(r'^fixed_commit_id="([0-9a-fA-F]+)"', re.MULTILINE)
_SHORT_COMMIT_LENGTH = 7


@dataclass(frozen=True)
class SealedTestSetup:
    """What isolated the agent's test runs, recorded in the run's config.json."""

    container: str
    network: str
    mounted: tuple[str, ...]
    stripped_from_checkout: tuple[str, ...]


def read_answer_markers(checkout: CheckoutResult) -> tuple[str, ...]:
    """The fix commit id (full and abbreviated), for scanning the agent's test output."""
    bug_info_path = checkout.worktree / "bugsinpy_bug.info"
    if not bug_info_path.is_file():
        return ()
    match = _FIXED_COMMIT_PATTERN.search(bug_info_path.read_text(encoding="utf-8"))
    if match is None:
        return ()
    fixed_commit_id = match.group(1)
    return (fixed_commit_id, fixed_commit_id[:_SHORT_COMMIT_LENGTH])


def strip_answer_material(checkout: CheckoutResult, adapter: BugsInPyAdapter) -> None:
    """Remove the git history and fix metadata from the shim checkout.

    Runs inside the shared executor so its file view stays coherent with the
    host's. The test scripts still work: ``bugsinpy-test`` needs neither.
    """
    adapter.toolchain.run_command(
        [
            "sh",
            "-c",
            "rm -rf -- "
            + " ".join(ANSWER_BEARING_PATHS)
            + " && sed -i 's/^fixed_commit_id=.*/fixed_commit_id=\"\"/' "
            "bugsinpy_bug.info",
        ],
        cwd=checkout.worktree,
        check=True,
        capture_output=True,
    )


@contextmanager
def sealed_test_adapter(
    adapter: BugsInPyAdapter, checkout: CheckoutResult, run_name: str
) -> Iterator[tuple[BugsInPyAdapter, SealedTestSetup]]:
    """Start a sealed executor for one attempt; yield an adapter that runs in it."""
    container_name = f"{SEALED_CONTAINER_PREFIX}{run_name}"
    run_directory = checkout.worktree.parent
    toolchain = adapter.toolchain
    toolchain.create_sealed_test_container(container_name, run_directory)
    try:
        yield (
            BugsInPyAdapter(toolchain.for_container(container_name)),
            SealedTestSetup(
                container=container_name,
                network="none",
                mounted=(
                    f"{toolchain.executor_path(run_directory)} (read-write)",
                    "/home/bugsinpy/framework (read-only)",
                    "/root/.pyenv/versions (read-only)",
                ),
                stripped_from_checkout=(
                    *ANSWER_BEARING_PATHS,
                    "fixed_commit_id in bugsinpy_bug.info",
                ),
            ),
        )
    finally:
        toolchain.remove_sealed_test_container(container_name)
