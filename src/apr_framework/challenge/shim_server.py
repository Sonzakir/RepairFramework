"""Serve the agent's test shims from outside its sandbox.

The shims in the agent's workspace (``./run_failing_test``, ``./run_tests``) only
drop a request file into ``.test_channel/`` and wait. ``ShimServer`` runs in
the harness process, outside the agent's sandbox and process tree: for each
request it mirrors the workspace into the executor-visible evaluation checkout,
runs the bug's trigger test or regression command there through the BugsInPy
adapter, and writes the output back. The agent therefore never needs Docker,
the repository or the network, and editing a shim can only break its own
feedback.

Executor paths in the output are rewritten to the workspace path and the
``bugsinpy-test`` wrapper's own lines are dropped, so the output reads like a
local test run.
"""

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from apr_framework.benchmarks.bugsinpy import BugsInPyAdapter
from apr_framework.challenge.agent_workspace import (
    TEST_CHANNEL_DIRECTORY,
    AgentWorkspace,
    sync_workspace_into_checkout,
)
from apr_framework.core.models import CheckoutResult, TestRunResult
from apr_framework.repair.patch_applier import is_passing_trigger_run

logger = logging.getLogger(__name__)

_POLL_SECONDS = 0.5
_WRAPPER_NOISE = re.compile(r"^(RUN EVERY COMMAND|\d+)$")


@dataclass
class ShimRequestRecord:
    """One test run the agent asked for."""

    kind: str
    started_at: float
    duration_seconds: float
    passed: int
    failed: int
    errors: int
    skipped_paths: list[str] = field(default_factory=list)


class ShimServer:
    """Answer shim requests while the agent runs (use as a context manager)."""

    def __init__(
        self,
        agent_workspace: AgentWorkspace,
        evaluation_checkout: CheckoutResult,
        adapter: BugsInPyAdapter,
        regression_command: str,
        timeout_seconds: int,
        log: Callable[[str], None],
    ) -> None:
        self._agent_workspace = agent_workspace
        self._evaluation_checkout = evaluation_checkout
        self._adapter = adapter
        self._regression_command = regression_command
        self._timeout_seconds = timeout_seconds
        self._log = log
        self._channel = agent_workspace.workspace / TEST_CHANNEL_DIRECTORY
        self._stop_requested = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self.requests: list[ShimRequestRecord] = []

    def __enter__(self) -> "ShimServer":
        self._thread.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self._stop_requested.set()
        self._thread.join(timeout=self._timeout_seconds + 30)

    def _serve(self) -> None:
        while not self._stop_requested.is_set():
            for request_path in sorted(self._channel.glob("*.request")):
                self._answer_request(request_path)
            time.sleep(_POLL_SECONDS)

    def _answer_request(self, request_path: Path) -> None:
        request_id, kind = self._take_request(request_path)
        started_at = time.time()
        skipped_paths, test_run, output_text, exit_code = (
            self._sync_and_run_requested_tests(request_id, kind)
        )
        self._write_response(request_id, output_text, exit_code)
        self._record_and_log_request(kind, started_at, test_run, skipped_paths)

    @staticmethod
    def _take_request(request_path: Path) -> tuple[str, str]:
        """Read a request file's id and kind, then remove it so it runs once."""
        request_id = request_path.name.removesuffix(".request")
        kind = request_path.read_text(encoding="utf-8").strip()
        request_path.unlink(missing_ok=True)
        return request_id, kind

    def _sync_and_run_requested_tests(
        self, request_id: str, kind: str
    ) -> tuple[list[str], TestRunResult | None, str, int]:
        """Mirror the workspace, run the tests, and render the agent's answer.

        Returns (paths not synced, the test run, sanitised output, exit code). The
        agent always gets an answer, even when the run cannot start.
        """
        try:
            skipped_paths = sync_workspace_into_checkout(
                self._agent_workspace, self._evaluation_checkout.worktree
            )
            test_run = self._run_requested_tests(kind)
        except Exception as error:  # noqa: BLE001 — the agent must always get an answer
            logger.exception("Shim request %s failed", request_id)
            return [], None, f"The test run could not start: {error}\n", 2
        output_text = sanitise_test_output(
            test_run.raw_output,
            self._adapter.toolchain.executor_path(self._evaluation_checkout.worktree),
            self._agent_workspace.workspace,
        )
        return (
            skipped_paths,
            test_run,
            output_text,
            (0 if is_passing_trigger_run(test_run) else 1),
        )

    def _record_and_log_request(
        self,
        kind: str,
        started_at: float,
        test_run: TestRunResult | None,
        skipped_paths: list[str],
    ) -> None:
        record = ShimRequestRecord(
            kind=kind,
            started_at=started_at,
            duration_seconds=round(time.time() - started_at, 1),
            passed=test_run.passed_count if test_run else 0,
            failed=test_run.failed_count if test_run else 0,
            errors=test_run.error_count if test_run else 0,
            skipped_paths=skipped_paths,
        )
        self.requests.append(record)
        self._log(
            f"Shim {kind}: passed={record.passed} failed={record.failed} "
            f"errors={record.errors} ({record.duration_seconds}s)"
            + (
                f"; not synced (outside the project): {skipped_paths}"
                if skipped_paths
                else ""
            )
        )

    def _run_requested_tests(self, kind: str) -> TestRunResult:
        if kind == "regression":
            return self._adapter.run_tests(
                self._evaluation_checkout,
                timeout=self._timeout_seconds,
                command=self._regression_command,
            )
        return self._adapter.run_tests(
            self._evaluation_checkout, timeout=self._timeout_seconds
        )

    def _write_response(
        self, request_id: str, output_text: str, exit_code: int
    ) -> None:
        (self._channel / f"{request_id}.output").write_text(
            output_text, encoding="utf-8"
        )
        (self._channel / f"{request_id}.exitcode").write_text(
            f"{exit_code}\n", encoding="utf-8"
        )
        (self._channel / f"{request_id}.done").write_text("", encoding="utf-8")


def sanitise_test_output(
    raw_output: str, executor_checkout_path: str, workspace: Path
) -> str:
    """Point executor paths at the agent's workspace; drop the wrapper's own lines."""
    rewritten = raw_output.replace(executor_checkout_path, str(workspace))
    kept_lines = [
        line for line in rewritten.splitlines() if not _WRAPPER_NOISE.match(line)
    ]
    return "\n".join(kept_lines).strip() + "\n"
