"""Unit checks for keeping the executor's file view coherent (no Docker)."""

from pathlib import Path

import pytest

from apr_framework.benchmarks import executor_view
from apr_framework.benchmarks.bugsinpy import BugsInPyConfig, BugsInPyDockerExecutor
from apr_framework.core.exceptions import ConfigurationError
from apr_framework.benchmarks.executor_view import (
    build_view_refresh_script,
    record_host_deletion,
    remove_host_file,
    remove_host_tree,
    take_host_deletions,
)


@pytest.fixture(autouse=True)
def empty_deletion_journal():
    take_host_deletions()
    yield
    take_host_deletions()


@pytest.fixture
def executor(tmp_path: Path, monkeypatch) -> tuple[BugsInPyDockerExecutor, list]:
    config = BugsInPyConfig.from_project_root(tmp_path)
    config.workspace_dir.mkdir(parents=True)
    docker_calls: list[list[str]] = []
    executor = BugsInPyDockerExecutor(config, container="sealed")
    monkeypatch.setattr(
        executor,
        "_run_docker",
        lambda args, check, capture_output=False, timeout=None: docker_calls.append(
            args
        ),
    )
    return executor, docker_calls


def test_removals_are_recorded_and_drained_once(tmp_path: Path) -> None:
    (tmp_path / "tree").mkdir()
    (tmp_path / "tree" / "a.py").write_text("x")
    (tmp_path / "b.pyc").write_text("x")
    remove_host_file(tmp_path / "b.pyc")
    remove_host_tree(tmp_path / "tree")

    assert not (tmp_path / "tree").exists() and not (tmp_path / "b.pyc").exists()
    assert set(take_host_deletions()) == {tmp_path / "b.pyc", tmp_path / "tree"}
    assert take_host_deletions() == []


def test_refresh_script_removes_deletions_then_walks_the_tree() -> None:
    script = build_view_refresh_script(
        ["/home/workspace/run/proj/gone.py"], "/home/workspace/run/proj"
    )
    assert script.index("rm -rf -- /home/workspace/run/proj/gone.py") < script.index(
        "find /home/workspace/run/proj -path /home/workspace/run/proj/env -prune"
    )
    assert build_view_refresh_script([], None) is None


def test_refresh_removes_only_deleted_paths_strictly_inside_the_workspace(
    executor, tmp_path: Path
) -> None:
    bugsinpy_executor, docker_calls = executor
    workspace = tmp_path / ".workspace" / "bugsinpy"
    checkout = workspace / "run_1" / "black"
    checkout.mkdir(parents=True)
    (checkout / "recreated.py").write_text("x")
    for path in (
        checkout / "gone.py",
        checkout / "recreated.py",
        workspace,
        tmp_path / "outside.py",
    ):
        record_host_deletion(path)

    bugsinpy_executor._refresh_view_of_host_changes(checkout)

    (script,) = [call[-1] for call in docker_calls]
    assert "rm -rf -- /home/workspace/run_1/black/gone.py " in script
    assert "recreated.py" not in script
    assert "rm -rf -- /home/workspace " not in script
    assert "outside.py" not in script
    assert "find /home/workspace/run_1/black " in script


def test_commands_refresh_the_view_only_when_running_on_the_host(
    executor, tmp_path: Path, monkeypatch
) -> None:
    bugsinpy_executor, docker_calls = executor
    checkout = tmp_path / ".workspace" / "bugsinpy" / "black"
    checkout.mkdir(parents=True)
    for name in ("_ensure_docker_available", "_container_exists", "_container_running"):
        monkeypatch.setattr(bugsinpy_executor, name, lambda: True)

    monkeypatch.setattr(
        BugsInPyDockerExecutor, "runs_on_host", property(lambda _: True)
    )
    bugsinpy_executor.run_command(["true"], cwd=checkout)
    assert [call[:3] for call in docker_calls] == [
        ["exec", "sealed", "sh"],
        ["exec", "-w", "/home/workspace/black"],
    ]

    docker_calls.clear()
    monkeypatch.setattr(
        BugsInPyDockerExecutor, "runs_on_host", property(lambda _: False)
    )
    bugsinpy_executor.run_command(["true"], cwd=checkout)
    assert [call[:3] for call in docker_calls] == [
        ["exec", "-w", "/home/workspace/black"]
    ]


def test_sealed_test_container_is_offline_and_sees_only_its_run(
    executor, tmp_path: Path, monkeypatch
) -> None:
    bugsinpy_executor, docker_calls = executor
    monkeypatch.setattr(bugsinpy_executor, "_ensure_docker_available", lambda: None)
    monkeypatch.delenv("APR_HOST_PROJECT_ROOT", raising=False)
    run_directory = tmp_path / ".workspace" / "bugsinpy" / "agent_run_7"
    run_directory.mkdir(parents=True)

    bugsinpy_executor.create_sealed_test_container(
        "apr-agent-tests-run_7", run_directory
    )

    run_args = next(call for call in docker_calls if call[0] == "run")
    mounts = [run_args[i + 1] for i, arg in enumerate(run_args) if arg == "-v"]
    assert run_args[run_args.index("--network") + 1] == "none"
    assert f"{run_directory.resolve()}:/home/workspace/agent_run_7" in mounts
    assert any(mount.endswith(":/home/bugsinpy/framework:ro") for mount in mounts)
    assert any(mount.endswith(":/root/.pyenv/versions:ro") for mount in mounts)
    assert not any("/home/bugsinpy/projects" in mount for mount in mounts)
    assert not any(mount.endswith(":/home/workspace") for mount in mounts)


def test_journal_is_thread_safe_state_of_one_module() -> None:
    assert executor_view._pending_deletions == set()


def test_workspace_trees_are_deleted_inside_the_executor_and_only_there(
    executor, tmp_path: Path, monkeypatch
) -> None:
    bugsinpy_executor, docker_calls = executor
    for name in ("_ensure_docker_available", "_container_exists", "_container_running"):
        monkeypatch.setattr(bugsinpy_executor, name, lambda: True)
    monkeypatch.setattr(
        BugsInPyDockerExecutor, "runs_on_host", property(lambda _: False)
    )
    destination = tmp_path / ".workspace" / "bugsinpy" / "challenge_case"
    destination.mkdir(parents=True)

    bugsinpy_executor.remove_workspace_tree(destination)

    assert docker_calls[-1][-4:] == [
        "rm",
        "-rf",
        "--",
        "/home/workspace/challenge_case",
    ]
    for outside in (tmp_path / ".workspace" / "bugsinpy", tmp_path / "src"):
        with pytest.raises(ConfigurationError):
            bugsinpy_executor.remove_workspace_tree(outside)
