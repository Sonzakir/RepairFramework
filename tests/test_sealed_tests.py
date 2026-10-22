"""Unit checks for sealing the agent's test runs away from the answer (no Docker)."""

from pathlib import Path

import pytest

from apr_framework.challenge.claude_code import (
    ClaudeCodeSession,
    scan_transcript_for_leaks,
)
from apr_framework.challenge.sealed_tests import (
    read_answer_markers,
    sealed_test_adapter,
    strip_answer_material,
)
from apr_framework.core.models import BugIdentifier, CheckoutResult

FIX_COMMIT = "74c4d1c1dbe6bfdb05d6e4fc767ffe062398f0a3"


class _FakeToolchain:
    def __init__(self, container: str = "shared") -> None:
        self.container = container
        self.calls: list[tuple] = []

    def run_command(self, args, *, cwd, check, capture_output):
        self.calls.append(("run_command", tuple(args), cwd))

    def create_sealed_test_container(self, name, host_directory):
        self.calls.append(("create", name, host_directory))

    def remove_sealed_test_container(self, name):
        self.calls.append(("remove", name))

    def for_container(self, container):
        return _FakeToolchain(container)

    def executor_path(self, path: Path) -> str:
        return f"/home/workspace/{path.name}"


class _FakeAdapter:
    def __init__(self, toolchain: _FakeToolchain) -> None:
        self.toolchain = toolchain


@pytest.fixture
def checkout(tmp_path: Path) -> CheckoutResult:
    worktree = tmp_path / "agent_run_7" / "fastapi"
    worktree.mkdir(parents=True)
    (worktree / "bugsinpy_bug.info").write_text(
        f'buggy_commit_id="7ccd81f"\nfixed_commit_id="{FIX_COMMIT}"\n'
        'test_file="tests/test_x.py"\n'
    )
    return CheckoutResult(
        bug=BugIdentifier(benchmark="bugsinpy", project="fastapi", bug_id=4),
        worktree=worktree,
        success=True,
    )


def test_answer_markers_are_the_fix_commit_id(checkout: CheckoutResult) -> None:
    assert read_answer_markers(checkout) == (FIX_COMMIT, FIX_COMMIT[:7])


def test_stripping_removes_history_and_fix_metadata_inside_the_executor(
    checkout: CheckoutResult,
) -> None:
    toolchain = _FakeToolchain()
    strip_answer_material(checkout, _FakeAdapter(toolchain))

    ((kind, args, cwd),) = toolchain.calls
    script = args[-1]
    assert kind == "run_command" and cwd == checkout.worktree
    assert "rm -rf -- .git bugsinpy_patchfile.info" in script
    assert "fixed_commit_id=" in script and "sed -i" in script


def test_sealed_container_is_always_removed(checkout: CheckoutResult) -> None:
    toolchain = _FakeToolchain()
    with pytest.raises(RuntimeError):
        with sealed_test_adapter(_FakeAdapter(toolchain), checkout, "run_7") as (
            sealed_adapter,
            setup,
        ):
            assert sealed_adapter.toolchain.container == "apr-agent-tests-run_7"
            assert setup.network == "none"
            raise RuntimeError("agent crashed")

    assert toolchain.calls == [
        ("create", "apr-agent-tests-run_7", checkout.worktree.parent),
        ("remove", "apr-agent-tests-run_7"),
    ]


def _session_with_tool_result(result_text: str) -> ClaudeCodeSession:
    return ClaudeCodeSession(
        command=[],
        settings={},
        cli_version="test",
        exit_code=0,
        timed_out=False,
        duration_seconds=1.0,
        events=[
            {"type": "system", "subtype": "init", "model": "m"},
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "content": [{"type": "text", "text": result_text}],
                        }
                    ]
                },
            },
            {"type": "result", "subtype": "success", "permission_denials": []},
        ],
        stderr="",
    )


@pytest.mark.parametrize(
    "test_output",
    [
        f"commit {FIX_COMMIT}\nAuthor: someone",
        f"see {FIX_COMMIT[:7]} for details",
        "diff --git a/fastapi/openapi/utils.py b/fastapi/openapi/utils.py\n",
    ],
)
def test_leak_scan_flags_answers_in_test_output(test_output: str) -> None:
    report = scan_transcript_for_leaks(
        _session_with_tool_result(test_output),
        [Path("/repo")],
        [],
        (FIX_COMMIT, FIX_COMMIT[:7]),
    )
    assert report.is_suspect


def test_leak_scan_accepts_ordinary_test_output() -> None:
    report = scan_transcript_for_leaks(
        _session_with_tool_result(
            "FAILED tests/test_x.py::test_a - AssertionError: 1 != 2\n1 failed"
        ),
        [Path("/repo")],
        [],
        (FIX_COMMIT, FIX_COMMIT[:7]),
    )
    assert not report.is_suspect
