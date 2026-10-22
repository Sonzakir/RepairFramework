"""One fresh repair attempt by a coding agent, captured and evaluated.

``run_agent_attempt`` is the whole pipeline behind ``challenge agent-run``:

1. check the bug out and compile it in an executor-visible directory (the
   checkout the agent's test shims run in);
2. copy the project's own files into a workspace outside the repository (no
   ``.git``, no BugsInPy metadata, no environment) and install the shims;
3. run Claude Code headless in that workspace, locked down
   (``challenge/claude_code.py``), while ``ShimServer`` answers its test runs;
4. capture the complete diff against the pristine copy and scan the transcript
   for leaks;
5. evaluate the diff in a *separate* clean checkout through the shared
   validation path (evaluator-integrity gate, trigger test, regression suite);
6. write a normal ``runs/run_NNN/`` directory plus the transcript.

The outcome is one of ``plausible``, ``rejected_evaluator_edit``, ``failed``,
``no_patch``, ``environment_error`` or ``leak_suspect``. A leak overrides any
other outcome: such a run never becomes a case.
"""

import hashlib
import shutil
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from apr_framework.benchmarks.bugsinpy import BugsInPyAdapter
from apr_framework.challenge.agent_workspace import (
    AgentWorkspace,
    capture_workspace_diff,
    create_agent_workspace,
    find_modified_shims,
)
from apr_framework.challenge.claude_code import (
    ClaudeCodeCaps,
    ClaudeCodeSession,
    LeakReport,
    locate_claude_cli,
    run_claude_code,
    scan_transcript_for_leaks,
)
from apr_framework.challenge.multi_file_patch import MultiFilePatch, PatchApplyError
from apr_framework.challenge.replay import check_out_and_compile_into
from apr_framework.challenge.sealed_tests import (
    SealedTestSetup,
    read_answer_markers,
    sealed_test_adapter,
    strip_answer_material,
)
from apr_framework.challenge.shim_server import ShimServer, sanitise_test_output
from apr_framework.core.models import BugIdentifier, CheckoutResult, RepairStatus
from apr_framework.evaluation.run_writer import RunWriter
from apr_framework.repair.evaluator_integrity import EvaluatorGuard
from apr_framework.repair.patch_applier import (
    ValidationOutcome,
    apply_patch_and_validate,
)
from apr_framework.repair.regression import (
    build_regression_context,
    derive_regression_command,
)
from apr_framework.repair.unified_diff import touched_relative_paths

PROMPT_TEMPLATE_PATH = Path(__file__).parent / "prompts" / "claude_code_repair.txt"
AGENT_WORKSPACES_DIRECTORY_NAME = "apr-agent-workspaces"
_FAILING_OUTPUT_LINE_LIMIT = 80
_STATUS_BY_OUTCOME = {
    "plausible": RepairStatus.PLAUSIBLE,
    "rejected_evaluator_edit": RepairStatus.REJECTED,
    "no_patch": RepairStatus.NO_PATCH,
}


@dataclass(frozen=True)
class AgentRunOptions:
    """How to run one attempt."""

    caps: ClaudeCodeCaps = field(default_factory=ClaudeCodeCaps)
    runs_dir: Path = Path("runs")
    test_timeout_seconds: int = 300
    keep_workspace: bool = False


@dataclass(frozen=True)
class FailingTestDescription:
    """What the prompt tells the agent about the failing test."""

    trigger_tests: list[str]
    trigger_command: str
    regression_command: str
    regression_scope: str
    failing_output: str


@dataclass
class CandidateEvaluation:
    """The captured diff, judged in a separate clean checkout."""

    outcome: ValidationOutcome | None
    apply_error: str = ""
    regression_disabled_reason: str = ""


@dataclass
class AgentRunResult:
    run_dir: Path
    outcome: str
    status: RepairStatus
    summary: str
    candidate_diff: str
    session_summary: dict[str, Any]
    leak_findings: list[str]


def run_agent_attempt(
    bug: BugIdentifier,
    adapter: BugsInPyAdapter,
    project_root: Path,
    options: AgentRunOptions,
) -> AgentRunResult:
    """Run one Claude Code repair attempt end to end and write runs/run_NNN/."""
    claude_path = locate_claude_cli()
    writer = RunWriter.create(_resolve_runs_dir(project_root, options.runs_dir))
    started_at = datetime.now(timezone.utc)
    writer.log(
        f"Started challenge agent-run (claude-code) for {bug.project}#{bug.bug_id}"
    )

    shim_checkout = check_out_and_compile_into(
        bug, adapter, _executor_directory(project_root, f"agent_{writer.run_dir.name}")
    )
    agent_workspace = create_agent_workspace(
        shim_checkout.worktree,
        _workspace_root(writer.run_dir.name, bug),
        shim_timeout_seconds=options.test_timeout_seconds + 60,
    )
    failing_test = describe_failing_test(
        shim_checkout, adapter, agent_workspace, options.test_timeout_seconds
    )
    prompt = render_repair_prompt(failing_test)
    forbidden_roots = forbidden_roots_for(project_root)
    answer_markers = read_answer_markers(shim_checkout)
    strip_answer_material(shim_checkout, adapter)

    with (
        sealed_test_adapter(adapter, shim_checkout, writer.run_dir.name) as (
            sealed_adapter,
            sealed_setup,
        ),
        ShimServer(
            agent_workspace,
            shim_checkout,
            sealed_adapter,
            failing_test.regression_command,
            options.test_timeout_seconds,
            writer.log,
        ) as shim_server,
    ):
        write_agent_config(
            writer,
            bug,
            options,
            prompt,
            failing_test,
            agent_workspace,
            sealed_setup,
            started_at,
        )
        session = run_claude_code(
            claude_path,
            agent_workspace.workspace,
            prompt,
            options.caps,
            forbidden_roots,
            writer.run_dir / "agent_transcript.jsonl",
        )
    writer.log(f"Claude Code finished: {session.summary()['stop_reason']}")

    candidate_diff = capture_workspace_diff(agent_workspace)
    leak_report = scan_transcript_for_leaks(
        session,
        forbidden_roots,
        find_modified_shims(agent_workspace),
        answer_markers,
    )
    evaluation = evaluate_candidate_in_clean_checkout(
        bug, adapter, project_root, candidate_diff, writer, options.test_timeout_seconds
    )
    outcome = classify_agent_outcome(session, leak_report, candidate_diff, evaluation)

    result = write_agent_run_results(
        writer,
        bug,
        outcome,
        candidate_diff,
        session,
        leak_report,
        evaluation,
        shim_server.requests,
        started_at,
    )
    if not options.keep_workspace:
        shutil.rmtree(agent_workspace.root, ignore_errors=True)
    return result


def describe_failing_test(
    checkout: CheckoutResult,
    adapter: BugsInPyAdapter,
    agent_workspace: AgentWorkspace,
    timeout_seconds: int,
) -> FailingTestDescription:
    """Run the trigger test on the buggy code and describe it for the prompt."""
    trigger_command = (
        (checkout.worktree / "bugsinpy_run_test.sh").read_text("utf-8").strip()
    )
    test_file = _read_bug_info_value(checkout.worktree, "test_file")
    regression_command = derive_regression_command(trigger_command, test_file) or (
        trigger_command
    )
    buggy_run = adapter.run_tests(checkout, timeout=timeout_seconds)
    failing_output = sanitise_test_output(
        buggy_run.raw_output,
        adapter.toolchain.executor_path(checkout.worktree),
        agent_workspace.workspace,
    )
    return FailingTestDescription(
        trigger_tests=read_trigger_test_ids(trigger_command),
        trigger_command=trigger_command,
        regression_command=regression_command,
        regression_scope=(test_file or "the bug's test file").replace(";", ", "),
        failing_output="\n".join(
            failing_output.splitlines()[-_FAILING_OUTPUT_LINE_LIMIT:]
        ),
    )


def read_trigger_test_ids(trigger_command: str) -> list[str]:
    """Test ids named by the trigger command (pytest node ids or unittest names)."""
    runner_words = {"python", "python3", "-m", "pytest", "py.test", "unittest"}
    return [
        token
        for line in trigger_command.splitlines()
        for token in line.split()
        if token not in runner_words and not token.startswith("-")
    ]


def render_repair_prompt(failing_test: FailingTestDescription) -> str:
    return PROMPT_TEMPLATE_PATH.read_text(encoding="utf-8").format(
        failing_tests=", ".join(failing_test.trigger_tests),
        failing_output=failing_test.failing_output,
        regression_scope=failing_test.regression_scope,
    )


def forbidden_roots_for(project_root: Path) -> list[Path]:
    """Paths the agent must never read: the answer, the history and the cases."""
    return [
        project_root,
        project_root / ".tools" / "bugsinpy",
        project_root / "runs",
        project_root / "experiment_results",
        project_root / "challenges",
        Path.home() / ".claude",
    ]


def evaluate_candidate_in_clean_checkout(
    bug: BugIdentifier,
    adapter: BugsInPyAdapter,
    project_root: Path,
    candidate_diff: str,
    writer: RunWriter,
    timeout_seconds: int,
) -> CandidateEvaluation | None:
    """Judge the captured diff in a fresh checkout with the evaluator files locked."""
    if not candidate_diff.strip():
        return None
    checkout = check_out_and_compile_into(
        bug,
        adapter,
        _executor_directory(project_root, f"agent_eval_{writer.run_dir.name}"),
    )
    evaluator_guard = EvaluatorGuard.for_worktree(checkout.worktree)
    regression_context = build_regression_context(
        adapter, checkout, enabled=True, timeout=timeout_seconds
    )
    try:
        patch = MultiFilePatch.plan(candidate_diff, checkout.worktree)
    except PatchApplyError as apply_error:
        writer.log(f"Captured diff does not apply to a clean checkout: {apply_error}")
        return CandidateEvaluation(outcome=None, apply_error=str(apply_error))
    outcome = apply_patch_and_validate(
        patch.apply,
        patch.restore,
        adapter,
        checkout,
        regression_context,
        timeout_seconds,
        evaluator_guard=evaluator_guard,
        diff_text=candidate_diff,
        touched_paths=patch.touched_paths,
    )
    writer.log(
        f"Clean-checkout evaluation: plausible={outcome.is_plausible}, "
        f"integrity={outcome.integrity.integrity}, "
        f"regression_checked={outcome.regression_checked}"
    )
    return CandidateEvaluation(
        outcome=outcome,
        regression_disabled_reason=regression_context.disabled_reason,
    )


def classify_agent_outcome(
    session: ClaudeCodeSession,
    leak_report: LeakReport,
    candidate_diff: str,
    evaluation: CandidateEvaluation | None,
) -> str:
    if leak_report.is_suspect:
        return "leak_suspect"
    if not session.init_event:
        return "environment_error"
    if not candidate_diff.strip():
        return "no_patch"
    if evaluation is None or (
        evaluation.outcome is None and not evaluation.apply_error
    ):
        return "environment_error"
    if evaluation.outcome is None:
        return "failed"
    if evaluation.outcome.integrity.is_rejected:
        return "rejected_evaluator_edit"
    if evaluation.outcome.is_plausible:
        return "plausible"
    return "failed"


def write_agent_config(
    writer: RunWriter,
    bug: BugIdentifier,
    options: AgentRunOptions,
    prompt: str,
    failing_test: FailingTestDescription,
    agent_workspace: AgentWorkspace,
    sealed_setup: SealedTestSetup,
    started_at: datetime,
) -> None:
    (writer.run_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
    writer.write_json(
        "config.json",
        {
            "runner": "challenge-agent-run",
            "agent": "claude-code",
            "project": bug.project,
            "bug_id": bug.bug_id,
            "fl_mode": "none (the agent localizes the fault itself)",
            "caps": asdict(options.caps),
            "test_timeout_seconds": options.test_timeout_seconds,
            "prompt_template": "src/apr_framework/challenge/prompts/claude_code_repair.txt",
            "prompt_template_sha256": _sha256_text(
                PROMPT_TEMPLATE_PATH.read_text(encoding="utf-8")
            ),
            "prompt_sha256": _sha256_text(prompt),
            "trigger_command": failing_test.trigger_command,
            "regression_command": failing_test.regression_command,
            "agent_workspace": str(agent_workspace.workspace),
            "agent_test_runs": asdict(sealed_setup),
            "started_at": started_at.isoformat(),
        },
    )


def write_agent_run_results(
    writer: RunWriter,
    bug: BugIdentifier,
    outcome: str,
    candidate_diff: str,
    session: ClaudeCodeSession,
    leak_report: LeakReport,
    evaluation: CandidateEvaluation | None,
    shim_requests: list,
    started_at: datetime,
) -> AgentRunResult:
    """Write repair_results.json, the diff, the stderr and the claude settings."""
    if candidate_diff.strip():
        patches_directory = writer.run_dir / "patches"
        patches_directory.mkdir(exist_ok=True)
        (patches_directory / "agent.diff").write_text(candidate_diff, encoding="utf-8")
    (writer.run_dir / "agent_stderr.txt").write_text(session.stderr, encoding="utf-8")
    writer.write_json(
        "claude_code_invocation.json",
        {"command": session.command, "settings": session.settings},
    )

    summary = describe_agent_outcome(outcome, evaluation, leak_report)
    status = _STATUS_BY_OUTCOME.get(outcome, RepairStatus.FAILED)
    session_summary = session.summary()
    writer.write_json(
        "repair_results.json",
        {
            "run_id": writer.run_dir.name,
            "project": bug.project,
            "bug_id": bug.bug_id,
            "agent": "claude-code",
            "fl_mode": "none",
            "status": status.value,
            "agent_run_outcome": outcome,
            "validation_summary": summary,
            "started_at": started_at.isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "candidate": {
                "sha256": _sha256_text(candidate_diff) if candidate_diff else None,
                "touched_files": touched_relative_paths(candidate_diff),
                "diff_text": candidate_diff or None,
            },
            "evaluation": serialise_evaluation(evaluation),
            "agent_session": session_summary,
            "leak_report": {
                "suspect": leak_report.is_suspect,
                "findings": leak_report.findings,
            },
            "shim_requests": [asdict(record) for record in shim_requests],
        },
    )
    writer.log(f"Outcome: {outcome}. {summary}")
    return AgentRunResult(
        run_dir=writer.run_dir,
        outcome=outcome,
        status=status,
        summary=summary,
        candidate_diff=candidate_diff,
        session_summary=session_summary,
        leak_findings=leak_report.findings,
    )


def describe_agent_outcome(
    outcome: str, evaluation: CandidateEvaluation | None, leak_report: LeakReport
) -> str:
    if outcome == "leak_suspect":
        return (
            "Excluded from cases: the transcript shows "
            f"{len(leak_report.findings)} attempt(s) to reach forbidden paths, "
            "history, the network or the reference fix."
        )
    if outcome == "no_patch":
        return "The agent changed no file."
    if outcome == "environment_error":
        return "The agent or the evaluation did not run to completion."
    if evaluation is not None and evaluation.outcome is None:
        return f"The captured diff does not apply to a clean checkout: {evaluation.apply_error}"
    assert evaluation is not None and evaluation.outcome is not None
    if evaluation.outcome.is_plausible:
        return evaluation.outcome.describe_success("agent.diff")
    return evaluation.outcome.describe_failure("agent.diff")


def serialise_evaluation(
    evaluation: CandidateEvaluation | None,
) -> dict[str, Any] | None:
    if evaluation is None:
        return None
    if evaluation.outcome is None:
        return {"applied": False, "apply_error": evaluation.apply_error}
    trigger_run = evaluation.outcome.trigger_test_run_result
    return {
        "applied": evaluation.outcome.applied,
        "is_plausible": evaluation.outcome.is_plausible,
        "trigger": {
            "passed": trigger_run.passed_count,
            "failed": trigger_run.failed_count,
            "errors": trigger_run.error_count,
        },
        "regression_disabled_reason": evaluation.regression_disabled_reason,
        **evaluation.outcome.as_metadata(),
    }


def _executor_directory(project_root: Path, name: str) -> Path:
    return project_root / ".workspace" / "bugsinpy" / name


def _workspace_root(run_name: str, bug: BugIdentifier) -> Path:
    return (
        Path(tempfile.gettempdir()).resolve()
        / AGENT_WORKSPACES_DIRECTORY_NAME
        / f"{run_name}-{bug.project}-{bug.bug_id}"
    )


def _resolve_runs_dir(project_root: Path, runs_dir: Path) -> Path:
    return runs_dir if runs_dir.is_absolute() else project_root / runs_dir


def _read_bug_info_value(worktree: Path, key: str) -> str | None:
    for line in (worktree / "bugsinpy_bug.info").read_text("utf-8").splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip().strip('"')
    return None


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
