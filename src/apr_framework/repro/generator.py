"""Generation: issue → pytest module that fails on the buggy code.

The model sees the screened issue, the list of Python files in the answer-free
source view, the names and opening lines of visible test files, and whatever it
asks for through the retrieval tools (run on the source view, never on the
checkout). It never sees the developer fix, the trigger test or a test file the
fix touched.

Two strategies:

- ``direct``: write the failing test; after each round the test runs once on the
  buggy code and, unless it fails for a real reason (an assertion or an exception
  from the code under test), the classified output is fed back. At most
  ``max_rounds`` test-writing rounds.
- ``assertflip`` (AssertFlip): first a test that *passes* on the buggy code by
  asserting the buggy behaviour (with feedback until it passes), then one
  inversion turn that flips its assertions; failed inversions get the remaining
  feedback rounds.
"""

import ast
import re
from dataclasses import dataclass, field
from typing import Callable

from apr_framework.repair.llm.retrieval_protocol import (
    build_retrieval_result_message,
    parse_retrieve_command,
)
from apr_framework.repair.llm.retrieval_tools import execute_retrieval_command
from apr_framework.repro.models import FailureClassification, ReproIssue, TestVersion
from apr_framework.repro.prompts import load_prompt, render_prompt
from apr_framework.repro.runner import TestCommandRunner
from apr_framework.repro.source_view import SourceView
from apr_framework.repro.spend import MeteredLLMClient
from apr_framework.repro.verifier import classify_test_run, tail_lines

GENERATION_STRATEGIES = ("direct", "assertflip")
_PYTHON_BLOCK = re.compile(r"```(?:python|py)?\s*\n(?P<code>.*?)```", re.DOTALL)
_FEEDBACK_OUTPUT_LINES = 50


@dataclass(frozen=True)
class GenerationOptions:
    max_rounds: int = 3
    strategy: str = "direct"
    retrieval_budget: int = 4
    timeout_seconds: int = 300


@dataclass
class GenerationResult:
    """Every test version written, and the one handed to verification.

    Fields:
        versions:        Every version, in order (including AssertFlip step 1).
        final:           The version to verify (None if the model wrote none).
        rounds:          Test-writing rounds used.
        classifications: The single buggy-code run of each version, in order.
        stop_reason:     Why generation stopped.
        retrievals:      ``tool(argument)`` of every retrieval the model made.
    """

    versions: list[TestVersion] = field(default_factory=list)
    final: TestVersion | None = None
    rounds: int = 0
    classifications: list[FailureClassification] = field(default_factory=list)
    stop_reason: str = ""
    retrievals: list[str] = field(default_factory=list)


@dataclass
class _Conversation:
    """One generation dialogue and what it produced so far."""

    issue: ReproIssue
    view: SourceView
    runner: TestCommandRunner
    client: MeteredLLMClient
    test_file_name: str
    options: GenerationOptions
    on_version: Callable[[TestVersion], None]
    messages: list[dict[str, str]]
    result: GenerationResult = field(default_factory=GenerationResult)


def generate_repro_test(
    issue: ReproIssue,
    view: SourceView,
    runner: TestCommandRunner,
    client: MeteredLLMClient,
    test_file_name: str,
    options: GenerationOptions,
    on_version: Callable[[TestVersion], None] = lambda version: None,
) -> GenerationResult:
    """Run the chosen strategy and return every version it wrote."""
    system_prompt, user_prompt = (
        ("assertflip_system", "assertflip_pass_user")
        if options.strategy == "assertflip"
        else ("generate_system", "generate_user")
    )
    conversation = _Conversation(
        issue=issue,
        view=view,
        runner=runner,
        client=client,
        test_file_name=test_file_name,
        options=options,
        on_version=on_version,
        messages=build_opening_messages(issue, view, system_prompt, user_prompt),
    )
    with client.purpose("generation"):
        if options.strategy == "assertflip":
            generate_with_assertflip(conversation)
        else:
            generate_with_feedback_rounds(conversation, first_round=1, label="round")
    return conversation.result


def build_opening_messages(
    issue: ReproIssue, view: SourceView, system_prompt: str, user_prompt: str
) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": load_prompt(system_prompt)},
        {
            "role": "user",
            "content": render_prompt(
                user_prompt,
                issue_title=issue.title,
                issue_body=issue.prompt_body,
                project_layout=view.describe_layout() or "(none)",
                test_file_names=", ".join(view.test_file_names) or "(none visible)",
                test_style=view.style_excerpt or "(no test file visible)",
            ),
        },
    ]


def generate_with_feedback_rounds(
    conversation: _Conversation, first_round: int, label: str
) -> None:
    """Write → run once on buggy code → feed back, until it fails for a real reason."""
    for round_number in range(first_round, conversation.options.max_rounds + 1):
        version, classification = write_and_run_test_version(
            conversation, f"{label}-{round_number}"
        )
        conversation.result.final = version
        if classification.is_reproduction_candidate:
            conversation.result.stop_reason = (
                "fails on the buggy code for a real reason"
            )
            return
        append_feedback_turn(
            conversation, classification, describe_failing_instruction(classification)
        )
    conversation.result.stop_reason = f"no version failed for a real reason within {conversation.options.max_rounds} rounds"


def generate_with_assertflip(conversation: _Conversation) -> None:
    """Step 1 until a version passes on buggy code; step 2 inverts it."""
    for round_number in range(1, conversation.options.max_rounds + 1):
        version, classification = write_and_run_test_version(
            conversation, f"assertflip-pass-{round_number}"
        )
        if classification.kind == "passed":
            invert_passing_test(conversation, rounds_used=round_number)
            return
        append_feedback_turn(
            conversation, classification, describe_passing_instruction(classification)
        )
    conversation.result.stop_reason = (
        "AssertFlip step 1: no version passed on the buggy code within "
        f"{conversation.options.max_rounds} rounds"
    )


def invert_passing_test(conversation: _Conversation, rounds_used: int) -> None:
    conversation.messages.append(
        {"role": "user", "content": load_prompt("assertflip_invert_user")}
    )
    version, classification = write_and_run_test_version(
        conversation, "assertflip-inverted"
    )
    conversation.result.final = version
    if classification.is_reproduction_candidate:
        conversation.result.stop_reason = "the inverted test fails for a real reason"
        return
    append_feedback_turn(
        conversation, classification, describe_failing_instruction(classification)
    )
    generate_with_feedback_rounds(
        conversation, first_round=rounds_used + 1, label="assertflip-fix"
    )


def write_and_run_test_version(
    conversation: _Conversation, label: str
) -> tuple[TestVersion, FailureClassification]:
    """One test-writing round: ask (with retrieval), record, show, run once."""
    response_text, source = ask_for_test_module(conversation)
    conversation.messages.append({"role": "assistant", "content": response_text})
    version = TestVersion(label=label, source=source)
    conversation.result.versions.append(version)
    conversation.result.rounds += 1
    conversation.on_version(version)
    classification = classify_version_on_buggy_code(conversation, version)
    conversation.result.classifications.append(classification)
    return version, classification


def ask_for_test_module(conversation: _Conversation) -> tuple[str, str]:
    """Answer retrieval requests until the model returns a module (or the budget ends)."""
    retrievals_left = conversation.options.retrieval_budget
    while True:
        response_text = conversation.client.complete(conversation.messages)
        retrieve_command = parse_retrieve_command(response_text)
        if retrieve_command is None or retrievals_left <= 0:
            return response_text, extract_python_module(response_text)
        retrievals_left -= 1
        conversation.result.retrievals.append(
            f"{retrieve_command.tool_name}({retrieve_command.argument})"
        )
        conversation.messages.append({"role": "assistant", "content": response_text})
        conversation.messages.append(
            build_retrieval_result_message(
                retrieve_command,
                execute_retrieval_command(retrieve_command, conversation.view.checkout),
            )
        )


def extract_python_module(response_text: str) -> str:
    """The last fenced Python block of the answer ("" when there is none)."""
    blocks = [match.group("code") for match in _PYTHON_BLOCK.finditer(response_text)]
    return blocks[-1].strip() + "\n" if blocks else ""


def classify_version_on_buggy_code(
    conversation: _Conversation, version: TestVersion
) -> FailureClassification:
    problem = find_module_problem(version.source)
    if problem:
        return FailureClassification(kind="invalid_module", message=problem)
    run = conversation.runner.run_repro_test(
        version, conversation.options.timeout_seconds
    )
    return classify_test_run(run, conversation.test_file_name, conversation.issue.text)


def find_module_problem(source: str) -> str:
    """Why a generated module cannot be run ("" when it can)."""
    if not source.strip():
        return "the answer contained no ```python block"
    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        return f"SyntaxError: {error.msg} (line {error.lineno})"
    has_test_function = any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test")
        for node in ast.walk(tree)
    )
    return "" if has_test_function else "the module defines no test_* function"


def append_feedback_turn(
    conversation: _Conversation,
    classification: FailureClassification,
    instruction: str,
) -> None:
    conversation.messages.append(
        {
            "role": "user",
            "content": render_prompt(
                "feedback_user",
                classification=describe_kind(classification),
                test_output=tail_lines(
                    classification.output_tail or classification.message,
                    _FEEDBACK_OUTPUT_LINES,
                ),
                instruction=instruction,
            ),
        }
    )


def describe_kind(classification: FailureClassification) -> str:
    detail = " ".join(
        part for part in (classification.exception_type, classification.message) if part
    )
    return f"{classification.kind}" + (f" ({detail[:200]})" if detail else "")


def describe_failing_instruction(classification: FailureClassification) -> str:
    """What to change when a test should fail on the buggy code but does not."""
    if classification.kind == "passed":
        return (
            "The test PASSES on the buggy code, so it does not reproduce the bug. "
            "Exercise the scenario the report describes and assert the behaviour "
            "the report says is correct."
        )
    if classification.kind == "timeout":
        return "The test timed out. Make it fast and make sure it terminates."
    return (
        "The test fails for a reason unrelated to the bug (a broken test, import, "
        "fixture or environment problem). Fix the test itself so that it reaches "
        "the reported behaviour."
    )


def describe_passing_instruction(classification: FailureClassification) -> str:
    """What to change when the AssertFlip step-1 test should pass but does not."""
    return (
        "AssertFlip step 1 needs a test that PASSES on the current code by asserting "
        f"what the code does today. This one did not pass ({classification.kind}). "
        "Assert the current (buggy) behaviour instead."
    )
