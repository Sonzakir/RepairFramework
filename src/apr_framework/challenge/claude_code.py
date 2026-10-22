"""Run Claude Code headless on one workspace, locked down, and read its transcript.

The agent gets a copy of the buggy project and nothing else. The run uses:

- ``--restricted``: the file tools are confined to the workspace, user/project
  settings are not loaded, and Bash exists only because ``--tools`` names it;
- ``--safe-mode``: no CLAUDE.md, skills, plugins from the user, hooks, MCP servers
  or auto memory (the login still works, unlike ``--bare``);
- ``--permission-mode dontAsk`` with ``--permission-prompts none``: only the
  allow-listed calls run; Bash may run nothing but the two test shims;
- ``--settings``: ``blockReadsOutsideWorkingDirectories``, deny rules for the
  repository (``.tools/bugsinpy`` holds ``bug_patch.txt``, plus ``runs/``,
  ``experiment_results/``, ``challenges/``), and the OS sandbox around every shell
  command with reads of the home directory denied and no network;
- ``--max-turns``, ``--max-budget-usd`` and a wall-clock timeout.

After the run, ``scan_transcript_for_leaks`` flags any attempt to reach a
forbidden path, version-control history, the web, Docker or the reference fix.
Such a run is ``leak_suspect`` and never becomes a case.
"""

import json
import os
import re
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from apr_framework.challenge.agent_workspace import SHIM_NAMES
from apr_framework.core.exceptions import ConfigurationError

ALLOWED_TOOLS = ("Read", "Edit", "Write", "Glob", "Grep", "Bash")
_SECRET_ENVIRONMENT_PATTERN = re.compile(
    r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|^BUGSINPY_|^APR_", re.IGNORECASE
)
# Tool inputs that mean the agent looked for the answer or outside its workspace.
_LEAK_PATTERNS = (
    ("reference fix", re.compile(r"bug_patch|bugsinpy|\.tools/|patchfile", re.I)),
    (
        "repository artifacts",
        re.compile(r"experiment_results|challenges/|/runs/|\bruns/run_"),
    ),
    ("version-control history", re.compile(r"\bgit\b|\.git/|\bhg\b|\bsvn\b")),
    ("container or executor", re.compile(r"\bdocker\b|/home/workspace|/home/bugsinpy")),
    ("network", re.compile(r"\b(curl|wget|pip|https?://)", re.I)),
    ("Claude Code state", re.compile(r"\.claude/|claude\.json", re.I)),
    ("home directory", re.compile(r"(^|[\s\"'=])~(/|\s|$)|\$HOME|\$\{HOME\}")),
)
# Test output that only a reachable git history (or a patch file) could produce.
_VERSION_CONTROL_DIFF = re.compile(
    r"^diff --git |^index [0-9a-f]{7,}\.\.", re.MULTILINE
)
# Which input fields name what a call *reaches*. Edit/Write content and Grep search
# patterns are what the agent writes or looks for, not where it goes.
_ACCESS_FIELDS_BY_TOOL = {
    "Bash": ("command",),
    "Read": ("file_path",),
    "Edit": ("file_path",),
    "Write": ("file_path",),
    "NotebookEdit": ("notebook_path",),
    "Glob": ("path", "pattern"),
    "Grep": ("path", "glob"),
}
_NETWORK_TOOLS = frozenset({"WebFetch", "WebSearch"})


@dataclass(frozen=True)
class ClaudeCodeCaps:
    """Budget for one attempt."""

    max_turns: int = 40
    timeout_minutes: int = 20
    max_budget_usd: float = 3.0
    model: str | None = None


@dataclass
class ClaudeCodeSession:
    """What one headless Claude Code run produced."""

    command: list[str]
    settings: dict[str, Any]
    cli_version: str
    exit_code: int | None
    timed_out: bool
    duration_seconds: float
    events: list[dict[str, Any]]
    stderr: str

    @property
    def init_event(self) -> dict[str, Any]:
        return next(
            (
                event
                for event in self.events
                if event.get("type") == "system" and event.get("subtype") == "init"
            ),
            {},
        )

    @property
    def result_event(self) -> dict[str, Any]:
        return next(
            (event for event in reversed(self.events) if event.get("type") == "result"),
            {},
        )

    def summary(self) -> dict[str, Any]:
        """Model, version, turns, duration, cost and stop reason, for the run record."""
        init_event, result_event = self.init_event, self.result_event
        return {
            "cli_version": self.cli_version,
            "session_cli_version": init_event.get("claude_code_version"),
            "model": init_event.get("model"),
            "tools": init_event.get("tools"),
            "plugins": [plugin.get("name") for plugin in init_event.get("plugins", [])],
            "mcp_servers": init_event.get("mcp_servers"),
            "permission_mode": init_event.get("permissionMode"),
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "duration_seconds": round(self.duration_seconds, 1),
            "stop_reason": result_event.get("subtype"),
            "is_error": result_event.get("is_error"),
            "num_turns": result_event.get("num_turns"),
            "total_cost_usd": result_event.get("total_cost_usd"),
            "usage": result_event.get("usage"),
            "permission_denials": result_event.get("permission_denials", []),
            "final_message": result_event.get("result"),
        }


@dataclass
class LeakReport:
    """Transcript evidence that the agent reached for something it must not see."""

    findings: list[str] = field(default_factory=list)

    @property
    def is_suspect(self) -> bool:
        return bool(self.findings)


def locate_claude_cli() -> str:
    """Return the ``claude`` executable, refusing to run inside the framework container."""
    if Path("/.dockerenv").exists():
        raise ConfigurationError(
            "challenge agent-run must run on the host, where Claude Code is installed "
            "and logged in; the framework container has no Claude Code and must not "
            "hold its credentials. See docs/challenges.md."
        )
    claude_path = shutil.which("claude")
    if claude_path is None:
        raise ConfigurationError(
            "Claude Code (`claude`) is not on PATH. Install it and log in first."
        )
    return claude_path


def read_claude_code_version(claude_path: str) -> str:
    completed = subprocess.run(
        [claude_path, "--version"], capture_output=True, text=True, check=False
    )
    return completed.stdout.strip() or completed.stderr.strip()


def build_claude_code_settings(
    workspace: Path, forbidden_roots: list[Path]
) -> dict[str, Any]:
    """Permission and sandbox settings that keep the agent inside its workspace."""
    deny_rules = ["WebFetch", "WebSearch", "Bash(git *)", "Bash(docker *)"]
    for forbidden_root in forbidden_roots:
        for tool_name in ("Read", "Edit"):
            deny_rules.append(f"{tool_name}(/{forbidden_root.resolve()}/**)")
    return {
        "permissions": {
            "blockReadsOutsideWorkingDirectories": True,
            "allow": _allow_rules(),
            "deny": deny_rules,
        },
        "sandbox": {
            "enabled": True,
            "allowUnsandboxedCommands": False,
            "failIfUnavailable": True,
            "filesystem": {
                "denyRead": [str(Path.home())]
                + [str(root.resolve()) for root in forbidden_roots],
                "allowRead": [str(workspace.resolve())],
            },
            "network": {"allowedDomains": []},
        },
    }


def build_claude_code_command(
    claude_path: str, prompt: str, caps: ClaudeCodeCaps, settings: dict[str, Any]
) -> list[str]:
    command = [
        claude_path,
        "-p",
        prompt,
        "--restricted",
        "--safe-mode",
        "--tools",
        ",".join(ALLOWED_TOOLS),
        "--allowedTools",
        *_allow_rules(),
        "--disallowedTools",
        "WebFetch",
        "WebSearch",
        "mcp__*",
        "--permission-mode",
        "dontAsk",
        "--permission-prompts",
        "none",
        "--max-turns",
        str(caps.max_turns),
        "--max-budget-usd",
        f"{caps.max_budget_usd:.2f}",
        "--output-format",
        "stream-json",
        "--verbose",
        "--no-session-persistence",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--settings",
        json.dumps(settings),
    ]
    if caps.model:
        command.extend(["--model", caps.model])
    return command


def run_claude_code(
    claude_path: str,
    workspace: Path,
    prompt: str,
    caps: ClaudeCodeCaps,
    forbidden_roots: list[Path],
    transcript_path: Path,
) -> ClaudeCodeSession:
    """Run one attempt in ``workspace``; stream the transcript to ``transcript_path``."""
    settings = build_claude_code_settings(workspace, forbidden_roots)
    command = build_claude_code_command(claude_path, prompt, caps, settings)
    started_at = time.monotonic()
    timed_out = False
    with transcript_path.open("w", encoding="utf-8") as transcript_file:
        process = subprocess.Popen(
            command,
            cwd=workspace,
            stdin=subprocess.DEVNULL,
            stdout=transcript_file,
            stderr=subprocess.PIPE,
            text=True,
            env=_agent_environment(),
        )
        try:
            _, stderr_text = process.communicate(timeout=caps.timeout_minutes * 60)
        except subprocess.TimeoutExpired:
            timed_out = True
            stderr_text = _stop_process(process)
    return ClaudeCodeSession(
        command=_redact_prompt(command),
        settings=settings,
        cli_version=read_claude_code_version(claude_path),
        exit_code=process.returncode,
        timed_out=timed_out,
        duration_seconds=time.monotonic() - started_at,
        events=read_stream_json(transcript_path),
        stderr=stderr_text or "",
    )


def read_stream_json(transcript_path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in transcript_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            events.append({"type": "unparsed", "line": line})
    return events


def scan_transcript_for_leaks(
    session: ClaudeCodeSession,
    forbidden_roots: list[Path],
    modified_shims: list[str],
    answer_markers: tuple[str, ...] = (),
) -> LeakReport:
    """Flag every tool call that reached for something outside the agent's task.

    Tool *inputs* show where a call reached. Tool *results* are scanned too: code
    the agent wrote runs when its tests run, so a test output that shows the fix
    commit id or a version-control diff means the answer reached the agent.
    """
    findings = _find_reaching_tool_calls(session, forbidden_roots)
    findings.extend(_find_answer_bearing_tool_results(session, answer_markers))
    for denial in session.summary()["permission_denials"]:
        findings.append(
            f"permission denied for {denial.get('tool_name')}: "
            f"{json.dumps(denial.get('tool_input'), ensure_ascii=False)[:200]}"
        )
    findings.extend(f"test shim modified: {shim_name}" for shim_name in modified_shims)
    return LeakReport(findings=_unique(findings))


def _find_reaching_tool_calls(
    session: ClaudeCodeSession, forbidden_roots: list[Path]
) -> list[str]:
    findings: list[str] = []
    root_markers = [str(root.resolve()) for root in forbidden_roots] + [
        str(Path.home())
    ]
    for tool_name, tool_input in _iter_tool_calls(session.events):
        input_text = _access_text(tool_name, tool_input)
        if tool_name in _NETWORK_TOOLS:
            findings.append(f"{tool_name} call: {input_text[:200]}")
        for marker in root_markers:
            if marker and marker in input_text:
                findings.append(f"{tool_name} names {marker}: {input_text[:200]}")
        for label, pattern in _LEAK_PATTERNS:
            if pattern.search(input_text):
                findings.append(f"{tool_name} touches {label}: {input_text[:200]}")
    return findings


def _find_answer_bearing_tool_results(
    session: ClaudeCodeSession, answer_markers: tuple[str, ...]
) -> list[str]:
    findings: list[str] = []
    for result_text in _iter_tool_results(session.events):
        for marker in answer_markers:
            if marker and marker in result_text:
                findings.append(f"a tool result shows the fix commit id {marker}")
        if _VERSION_CONTROL_DIFF.search(result_text):
            findings.append("a tool result shows a version-control diff")
    return findings


def _allow_rules() -> list[str]:
    shim_rules = [
        rule
        for shim_name in SHIM_NAMES
        for rule in (f"Bash(./{shim_name})", f"Bash(./{shim_name} *)")
    ]
    return ["Read", "Edit", "Write", "Glob", "Grep", *shim_rules]


def _agent_environment() -> dict[str, str]:
    """The parent environment without secrets or framework variables.

    The secrets are removed here rather than with ``CLAUDE_CODE_SUBPROCESS_ENV_SCRUB``,
    because that variable makes Claude Code fall back from ``dontAsk`` to the
    default permission mode.
    """
    environment = {
        name: value
        for name, value in os.environ.items()
        if not _SECRET_ENVIRONMENT_PATTERN.search(name)
        and name != "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"
    }
    # Keep the agent version fixed for the run; it is recorded from the session.
    environment["DISABLE_AUTOUPDATER"] = "1"
    return environment


def _stop_process(process: subprocess.Popen) -> str:
    """End a run that hit the wall-clock cap: SIGINT first, then SIGTERM, then kill."""
    for stop_signal, grace_seconds in ((signal.SIGINT, 30), (signal.SIGTERM, 15)):
        process.send_signal(stop_signal)
        try:
            _, stderr_text = process.communicate(timeout=grace_seconds)
            return stderr_text or ""
        except subprocess.TimeoutExpired:
            continue
    process.kill()
    _, stderr_text = process.communicate()
    return stderr_text or ""


def _redact_prompt(command: list[str]) -> list[str]:
    """The command for the record; the prompt itself is saved as prompt.txt."""
    redacted = list(command)
    redacted[2] = "<prompt.txt>"
    return redacted


def _access_text(tool_name: str, tool_input: dict[str, Any]) -> str:
    """The parts of a tool call that say where it reaches (all of it if unknown)."""
    access_fields = _ACCESS_FIELDS_BY_TOOL.get(tool_name)
    if access_fields is None:
        return json.dumps(tool_input, ensure_ascii=False)
    return " ".join(str(tool_input.get(name, "")) for name in access_fields)


def _iter_tool_calls(events: list[dict[str, Any]]):
    for event in events:
        if event.get("type") != "assistant":
            continue
        for block in event.get("message", {}).get("content", []):
            if isinstance(block, dict) and block.get("type") == "tool_use":
                yield block.get("name", "?"), block.get("input", {})


def _iter_tool_results(events: list[dict[str, Any]]):
    """The text of every tool result the agent received."""
    for event in events:
        if event.get("type") != "user":
            continue
        content = event.get("message", {}).get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                yield _tool_result_text(block.get("content"))


def _tool_result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text", "")) for part in content if isinstance(part, dict)
        )
    return ""


def _unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))
