"""Unit checks for the agent workspace, diff capture and leak scan (no agent, no Docker)."""

from pathlib import Path

import pytest

from apr_framework.challenge.agent_run import read_trigger_test_ids
from apr_framework.challenge.agent_workspace import (
    SHIM_NAMES,
    TEST_CHANNEL_DIRECTORY,
    capture_workspace_diff,
    create_agent_workspace,
    find_modified_shims,
    sync_workspace_into_checkout,
)
from apr_framework.challenge.claude_code import (
    ClaudeCodeSession,
    build_claude_code_settings,
    scan_transcript_for_leaks,
)
from apr_framework.challenge.multi_file_patch import MultiFilePatch
from apr_framework.challenge.replay import fingerprint_checkout


@pytest.fixture
def compiled_checkout(tmp_path: Path) -> Path:
    checkout = tmp_path / "executor" / "black"
    (checkout / "tests").mkdir(parents=True)
    (checkout / ".git" / "refs").mkdir(parents=True)
    (checkout / "env" / "bin").mkdir(parents=True)
    (checkout / "black.py").write_text("def main():\n    return 1\n")
    (checkout / "old_module.py").write_text("x = 1\n")
    (checkout / "tests" / "test_black.py").write_text("assert main() == 2\n")
    (checkout / ".git" / "HEAD").write_text("fix-commit\n")
    (checkout / "env" / "bin" / "python").write_text("#!/bin/sh\n")
    (checkout / "bugsinpy_patchfile.info").write_text("black.py\n")
    (checkout / "bugsinpy_bug.info").write_text('fixed_commit_id="abc"\n')
    (checkout / ".python-version").write_text("3.8.3\n")
    return checkout


@pytest.fixture
def agent_workspace(compiled_checkout: Path, tmp_path: Path):
    return create_agent_workspace(compiled_checkout, tmp_path / "agent", 60)


def test_workspace_has_no_answer_bearing_files(agent_workspace) -> None:
    names = {path.name for path in agent_workspace.workspace.rglob("*")}
    assert {"black.py", "test_black.py", *SHIM_NAMES} <= names
    assert not names & {
        ".git",
        "HEAD",
        "env",
        "bugsinpy_patchfile.info",
        "bugsinpy_bug.info",
        ".python-version",
    }


def test_captured_diff_reproduces_every_kind_of_change(
    agent_workspace, compiled_checkout: Path, tmp_path: Path
) -> None:
    workspace = agent_workspace.workspace
    (workspace / "black.py").write_text("def main():\n    return 2")  # no final newline
    (workspace / "pkg").mkdir()
    (workspace / "pkg" / "helper.py").write_text("def helper():\n    return 2\n")
    (workspace / "old_module.py").unlink()
    (workspace / TEST_CHANNEL_DIRECTORY / "1.output").write_text("harness file\n")

    diff_text = capture_workspace_diff(agent_workspace)
    assert TEST_CHANNEL_DIRECTORY not in diff_text
    assert "run_failing_test" not in diff_text

    patch = MultiFilePatch.plan(diff_text, agent_workspace.pristine)
    patch.apply()
    expected = {
        path.relative_to(workspace).as_posix(): path.read_bytes()
        for path in workspace.rglob("*")
        if path.is_file()
        and path.relative_to(workspace).parts[0]
        not in (TEST_CHANNEL_DIRECTORY, *SHIM_NAMES)
    }
    actual = {
        path.relative_to(agent_workspace.pristine).as_posix(): path.read_bytes()
        for path in agent_workspace.pristine.rglob("*")
        if path.is_file()
    }
    assert actual == expected


def test_sync_mirrors_project_files_but_never_environment_or_metadata(
    agent_workspace, compiled_checkout: Path
) -> None:
    workspace = agent_workspace.workspace
    (workspace / "black.py").write_text("def main():\n    return 2\n")
    (workspace / "old_module.py").unlink()
    (workspace / "env").mkdir()
    (workspace / "env" / "sitecustomize.py").write_text("import os\n")
    (workspace / "bugsinpy_run_test.sh").write_text("true\n")

    skipped_paths = sync_workspace_into_checkout(agent_workspace, compiled_checkout)

    assert "return 2" in (compiled_checkout / "black.py").read_text()
    assert not (compiled_checkout / "old_module.py").exists()
    assert not (compiled_checkout / "env" / "sitecustomize.py").exists()
    assert not (compiled_checkout / "bugsinpy_run_test.sh").exists()
    assert (compiled_checkout / ".git" / "HEAD").exists()
    assert sorted(skipped_paths) == ["bugsinpy_run_test.sh", "env/sitecustomize.py"]


def test_unchanged_workspace_syncs_to_an_unchanged_checkout(
    agent_workspace, compiled_checkout: Path
) -> None:
    before = fingerprint_checkout(compiled_checkout)
    sync_workspace_into_checkout(agent_workspace, compiled_checkout)
    assert fingerprint_checkout(compiled_checkout) == before
    assert capture_workspace_diff(agent_workspace) == ""


def test_tampered_shims_are_detected(agent_workspace) -> None:
    (agent_workspace.workspace / "run_failing_test").write_text("echo PASSED\n")
    assert find_modified_shims(agent_workspace) == ["run_failing_test"]


def _session_with_tool_calls(*tool_calls: tuple[str, dict]) -> ClaudeCodeSession:
    events = [{"type": "system", "subtype": "init", "model": "m"}]
    for name, tool_input in tool_calls:
        events.append(
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "tool_use", "name": name, "input": tool_input}]
                },
            }
        )
    events.append({"type": "result", "subtype": "success", "permission_denials": []})
    return ClaudeCodeSession(
        command=[],
        settings={},
        cli_version="test",
        exit_code=0,
        timed_out=False,
        duration_seconds=1.0,
        events=events,
        stderr="",
    )


@pytest.mark.parametrize(
    "tool_call",
    [
        (
            "Bash",
            {
                "command": "cat /repo/.tools/bugsinpy/projects/black/bugs/3/bug_patch.txt"
            },
        ),
        ("Bash", {"command": "git log -p"}),
        ("Read", {"file_path": "/repo/experiment_results/x.json"}),
        ("Bash", {"command": "curl https://github.com/psf/black/commit/7a14a37"}),
        ("WebSearch", {"query": "black exit code 2 config"}),
        ("Bash", {"command": "ls ~/"}),
        ("Glob", {"pattern": "**/*.py", "path": "/repo"}),
        ("Bash", {"command": "docker exec x cat /home/bugsinpy/projects"}),
    ],
)
def test_leak_scan_flags_reaching_outside_the_task(tool_call) -> None:
    report = scan_transcript_for_leaks(
        _session_with_tool_calls(tool_call), [Path("/repo")], []
    )
    assert report.is_suspect


def test_leak_scan_ignores_normal_work_and_code_contents() -> None:
    session = _session_with_tool_calls(
        ("Read", {"file_path": "/tmp/ws/black/black.py"}),
        ("Bash", {"command": "./run_failing_test"}),
        (
            "Edit",
            {
                "file_path": "/tmp/ws/black/black.py",
                "new_string": "# see https://github.com/psf/black and git docs",
            },
        ),
        ("Grep", {"pattern": "bugsinpy", "path": "/tmp/ws/black"}),
    )
    assert not scan_transcript_for_leaks(session, [Path("/repo")], []).is_suspect


def test_settings_deny_the_repository_and_sandbox_the_shell(tmp_path: Path) -> None:
    settings = build_claude_code_settings(tmp_path / "ws", [tmp_path / "repo"])
    assert settings["permissions"]["blockReadsOutsideWorkingDirectories"] is True
    assert (
        f"Read(/{(tmp_path / 'repo').resolve()}/**)" in settings["permissions"]["deny"]
    )
    assert settings["sandbox"]["allowUnsandboxedCommands"] is False
    assert settings["sandbox"]["network"]["allowedDomains"] == []


def test_trigger_test_ids_are_read_from_pytest_and_unittest_commands() -> None:
    assert read_trigger_test_ids(
        "python -m unittest -q tests.test_black.BlackTestCase.test_x"
    ) == ["tests.test_black.BlackTestCase.test_x"]
    assert read_trigger_test_ids(
        "pytest tests/test_a.py::test_b\npytest -q tests/test_c.py"
    ) == [
        "tests/test_a.py::test_b",
        "tests/test_c.py",
    ]


class _StubToolchain:
    def executor_path(self, path: Path) -> str:
        return "/home/workspace/agent_x/black"


class _StubAdapter:
    """Answers run_tests like bugsinpy-test would, with executor paths and noise."""

    toolchain = _StubToolchain()

    def __init__(self) -> None:
        self.commands: list[str | None] = []

    def run_tests(self, checkout, timeout=None, command=None):
        from apr_framework.core.models import TestRunResult

        self.commands.append(command)
        return TestRunResult(
            bug=checkout.bug,
            results=[],
            raw_output=(
                "RUN EVERY COMMAND\n0\n"
                'File "/home/workspace/agent_x/black/black.py", line 2\n'
                "1 passed in 0.01s"
            ),
            total_count=1,
            passed_count=1,
        )


def test_shim_round_trip_through_the_harness(
    agent_workspace, compiled_checkout
) -> None:
    import subprocess

    from apr_framework.challenge.shim_server import ShimServer
    from apr_framework.core.models import BugIdentifier, CheckoutResult

    adapter = _StubAdapter()
    checkout = CheckoutResult(
        bug=BugIdentifier("bugsinpy", "black", 3),
        worktree=compiled_checkout,
        success=True,
    )
    (agent_workspace.workspace / "black.py").write_text("def main():\n    return 2\n")
    with ShimServer(
        agent_workspace, checkout, adapter, "pytest -q tests", 30, lambda _: None
    ) as server:
        completed = subprocess.run(
            ["./run_tests"],
            cwd=agent_workspace.workspace,
            capture_output=True,
            text=True,
            timeout=60,
        )

    assert completed.returncode == 0
    assert "RUN EVERY COMMAND" not in completed.stdout
    assert str(agent_workspace.workspace / "black.py") in completed.stdout
    assert adapter.commands == ["pytest -q tests"]
    assert "return 2" in (compiled_checkout / "black.py").read_text()
    assert [record.kind for record in server.requests] == ["regression"]
