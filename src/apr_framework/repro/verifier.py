"""Verification: does the test fail on the buggy code, for the reported reason?

Three checks, in order:

1. **Failure classification** of the test's output on the current code. Only an
   ``assertion`` failure or an ``exception`` raised by the code under test (or one
   the issue names) can be a reproduction. Collection, import, syntax, fixture,
   timeout and environment errors, and exceptions a broken test raises itself
   (``NameError``, a wrong call signature, ...), cannot.
2. **Repeats**: the test runs 3 times; every run must fail the same way.
3. **The right-reason judge** (a separate prompt, temperature 0). It sees the issue
   and the failure output, never the fix, and must quote the issue sentence the
   failure matches. A quote that does not occur in the issue downgrades the label
   to ``unclear``. The judge only runs when checks 1 and 2 passed, so it can never
   label an environment error a reproduction.

With a fix (the developer fix on BugsInPy, ``--patch`` otherwise) the test must
also pass once the fix is applied (fail-to-pass).
"""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass

from apr_framework.core.models import TestRunResult
from apr_framework.repair.evaluator_integrity import EvaluatorGuard
from apr_framework.repair.patch_applier import is_passing_trigger_run
from apr_framework.repro.models import (
    FailureClassification,
    FixCheck,
    JudgeVerdict,
    ReproIssue,
    TestVersion,
    Verification,
)
from apr_framework.repro.prompts import load_prompt, render_prompt
from apr_framework.repro.runner import TIMEOUT_RETURN_CODE, TestCommandRunner
from apr_framework.repro.spend import MeteredLLMClient
from apr_framework.repro.validation import (
    NO_REGRESSION_CHECK,
    FixUnderTest,
    validate_file_states_with_repro,
)

JUDGE_LABELS = ("reproduces", "wrong_reason", "unclear")
_OUTPUT_TAIL_LINES = 60
_MIN_QUOTE_CHARACTERS = 10
# Exceptions a broken test raises itself; never a reproduction from the test file.
_TEST_BUG_EXCEPTIONS = frozenset(
    {
        "NameError",
        "UnboundLocalError",
        "ImportError",
        "ModuleNotFoundError",
        "SyntaxError",
        "IndentationError",
        "TabError",
    }
)
_ENVIRONMENT_EXCEPTIONS = frozenset({"ModuleNotFoundError", "ImportError"})
_API_MISUSE_MESSAGE = re.compile(
    r"unexpected keyword argument|positional argument|required (?:keyword-only )?"
    r"argument|module '[^']+' has no attribute|object is not callable"
)
_FRAME_LOCATION = re.compile(
    r"^(?P<path>[^\s:][^:\n]*\.py):(?P<line>\d+): (?P<exception>[A-Za-z_][\w.]*)\s*$",
    re.MULTILINE,
)
_ERROR_LINE = re.compile(r"^E\s+(?P<text>.*)$", re.MULTILINE)
_EXCEPTION_PREFIX = re.compile(
    r"^(?P<type>[A-Za-z_][\w.]*(?:Error|Exception|Exit|Warning|Failed|Interrupt))\b:?"
)
_SECTION_HEADER = re.compile(r"^_{3,} (?P<name>.+?) _{3,}$", re.MULTILINE)
_COLLECTION_ERROR = re.compile(
    r"ERROR collecting|Interrupted: \d+ errors? during collection|"
    r"ImportError while importing test module"
)
_SETUP_ERROR = re.compile(r"ERROR at (?:setup|teardown) of|fixture '[^']+' not found")
_NO_TESTS = re.compile(r"no tests ran|collected 0 items")


@dataclass(frozen=True)
class VerificationOptions:
    repeats: int = 3
    timeout_seconds: int = 300


def verify_repro_test(
    test: TestVersion,
    issue: ReproIssue,
    runner: TestCommandRunner,
    judge_client: MeteredLLMClient,
    test_file_name: str,
    options: VerificationOptions,
    fix: FixUnderTest | None = None,
    build_guard: Callable[[], EvaluatorGuard] | None = None,
) -> Verification:
    """Classify 3 buggy runs, judge the reason, then check F→P if a fix is given."""
    buggy_runs = run_on_buggy_code_repeatedly(
        test, runner, test_file_name, issue, options
    )
    verification = Verification(
        test=test,
        buggy_runs=buggy_runs,
        judge=JudgeVerdict.not_run("the test did not fail the same real way 3 times"),
    )
    if verification.fails_for_a_real_reason:
        verification.judge = judge_failure_reason(issue, buggy_runs[0], judge_client)
    if fix is not None and build_guard is not None:
        verification.fix_check = check_fix_turns_test_green(
            test, fix, runner, build_guard(), options.timeout_seconds
        )
    return verification


def run_on_buggy_code_repeatedly(
    test: TestVersion,
    runner: TestCommandRunner,
    test_file_name: str,
    issue: ReproIssue,
    options: VerificationOptions,
) -> list[FailureClassification]:
    return [
        classify_test_run(
            runner.run_repro_test(test, options.timeout_seconds),
            test_file_name,
            issue.text,
        )
        for _ in range(options.repeats)
    ]


def classify_test_run(
    run: TestRunResult, test_file_name: str, issue_text: str = ""
) -> FailureClassification:
    """Classify one pytest run of the repro test (see the module docstring)."""
    output = run.raw_output or ""
    tail = tail_lines(output)
    if run.return_code == TIMEOUT_RETURN_CODE:
        return FailureClassification(kind="timeout", output_tail=tail)
    if is_passing_trigger_run(run):
        return FailureClassification(kind="passed", output_tail=tail)
    if _COLLECTION_ERROR.search(output):
        return classify_error_section(output, "collection_error", tail)
    if _SETUP_ERROR.search(output):
        return classify_error_section(output, "setup_error", tail)
    if run.failed_count == 0:
        kind = "no_tests" if _NO_TESTS.search(output) else "environment_error"
        return classify_error_section(output, kind, tail)
    return classify_first_failure(output, test_file_name, issue_text, tail)


def classify_first_failure(
    output: str, test_file_name: str, issue_text: str, tail: str
) -> FailureClassification:
    """Assertion, exception from the code under test, or a broken test."""
    section = first_failure_section(output)
    exception_type, location = innermost_frame(section)
    message = first_error_message(section)
    if not exception_type:
        exception_type = exception_type_from_message(message)
    kind = decide_failure_kind(
        exception_type, location, message, test_file_name, issue_text
    )
    return FailureClassification(
        kind=kind,
        exception_type=exception_type,
        location=location,
        message=message[:300],
        output_tail=tail_lines(section) + "\n...\n" + tail_lines(tail, 3),
    )


def decide_failure_kind(
    exception_type: str,
    location: str,
    message: str,
    test_file_name: str,
    issue_text: str,
) -> str:
    short_type = exception_type.rsplit(".", 1)[-1]
    if short_type in ("AssertionError", "Failed") or message.startswith("assert "):
        return "assertion"
    if is_broken_test_exception(short_type, location, message, test_file_name):
        return "test_error"
    if short_type and short_type in issue_text:
        return "exception"  # the exception the issue reports
    if short_type in _ENVIRONMENT_EXCEPTIONS:
        return "environment_error"
    return "exception"


def is_broken_test_exception(
    short_type: str, location: str, message: str, test_file_name: str
) -> bool:
    """An exception the test raised itself: a typo, bad import or wrong call."""
    if not location.split(":", 1)[0].endswith(test_file_name):
        return False
    return short_type in _TEST_BUG_EXCEPTIONS or (
        short_type in ("TypeError", "AttributeError")
        and bool(_API_MISUSE_MESSAGE.search(message))
    )


def classify_error_section(output: str, kind: str, tail: str) -> FailureClassification:
    message = first_error_message(output)
    return FailureClassification(
        kind=kind,
        exception_type=exception_type_from_message(message),
        message=message[:300],
        output_tail=tail,
    )


def first_failure_section(output: str) -> str:
    """The text of the first ``____ test_x ____`` section of the FAILURES block."""
    failures_start = output.find("= FAILURES =")
    block = output[failures_start:] if failures_start >= 0 else output
    headers = list(_SECTION_HEADER.finditer(block))
    if not headers:
        return block
    end = headers[1].start() if len(headers) > 1 else len(block)
    summary_start = block.find("short test summary", headers[0].end())
    if 0 <= summary_start < end:
        end = summary_start
    return block[headers[0].end() : end]


def innermost_frame(section: str) -> tuple[str, str]:
    """(exception type, ``path:line``) from the last frame line of a section."""
    frames = list(_FRAME_LOCATION.finditer(section))
    if not frames:
        return "", ""
    last = frames[-1]
    return last.group("exception"), f"{last.group('path')}:{last.group('line')}"


def first_error_message(text: str) -> str:
    """The first ``E   ...`` line that names an exception, else the first one."""
    error_lines = [match.group("text").strip() for match in _ERROR_LINE.finditer(text)]
    for line in error_lines:
        if _EXCEPTION_PREFIX.match(line):
            return line
    return error_lines[0] if error_lines else ""


def exception_type_from_message(message: str) -> str:
    match = _EXCEPTION_PREFIX.match(message)
    return match.group("type") if match else ""


def judge_failure_reason(
    issue: ReproIssue,
    classification: FailureClassification,
    judge_client: MeteredLLMClient,
) -> JudgeVerdict:
    """Ask the right-reason judge; downgrade answers that do not quote the issue."""
    messages = [
        {"role": "system", "content": load_prompt("judge_system")},
        {
            "role": "user",
            "content": render_prompt(
                "judge_user",
                issue_title=issue.title,
                issue_body=issue.prompt_body,
                classification=describe_classification(classification),
                test_output=classification.output_tail,
            ),
        },
    ]
    with judge_client.purpose("judge"):
        response_text = judge_client.complete(messages)
    return parse_judge_response(response_text, issue.text)


def parse_judge_response(response_text: str, issue_text: str) -> JudgeVerdict:
    payload = parse_json_object(response_text)
    if payload is None:
        return JudgeVerdict(label="unclear", note="the judge did not answer in JSON")
    label = str(payload.get("label", "")).strip().lower()
    justification = str(payload.get("justification", "")).strip()
    quote = str(payload.get("issue_quote", "")).strip()
    quote_found = is_quote_in_issue(quote, issue_text)
    if label not in JUDGE_LABELS:
        return JudgeVerdict(
            label="unclear",
            justification=justification,
            issue_quote=quote,
            quote_found=quote_found,
            note=f"unknown label {label!r}",
        )
    if label == "reproduces" and not quote_found:
        return JudgeVerdict(
            label="unclear",
            justification=justification,
            issue_quote=quote,
            quote_found=False,
            note="the judge's quote does not occur in the issue",
        )
    return JudgeVerdict(
        label=label,
        justification=justification,
        issue_quote=quote,
        quote_found=quote_found,
    )


def parse_json_object(text: str) -> dict | None:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def is_quote_in_issue(quote: str, issue_text: str) -> bool:
    normalised_quote = normalise_for_quote(quote)
    if len(normalised_quote) < _MIN_QUOTE_CHARACTERS:
        return False
    return normalised_quote in normalise_for_quote(issue_text)


def normalise_for_quote(text: str) -> str:
    text = text.strip().strip("\"'`“”‘’").rstrip(".…").removesuffix("...")
    return re.sub(r"[\s`*_>]+", " ", text).strip().lower()


def check_fix_turns_test_green(
    test: TestVersion,
    fix: FixUnderTest,
    runner: TestCommandRunner,
    guard: EvaluatorGuard,
    timeout_seconds: int,
) -> FixCheck:
    """Apply the fix through the shared path and run the repro test (F→P)."""
    outcome, repro_run = validate_file_states_with_repro(
        fix.files, runner, guard, NO_REGRESSION_CHECK, test, timeout_seconds
    )
    if not outcome.applied or outcome.integrity.is_rejected:
        return FixCheck(
            fix_label=fix.label,
            applied=False,
            integrity=outcome.integrity.integrity,
            detail=outcome.integrity.detail or outcome.apply_error,
        )
    return FixCheck(
        fix_label=fix.label,
        applied=True,
        integrity=outcome.integrity.integrity,
        repro_passes=repro_run is not None and is_passing_trigger_run(repro_run),
        oracle_passes=outcome.trigger_passed if runner.has_oracle else None,
        detail=tail_lines(repro_run.raw_output if repro_run else "", 15),
    )


def describe_classification(classification: FailureClassification) -> str:
    parts = [classification.kind]
    if classification.exception_type:
        parts.append(classification.exception_type)
    if classification.location:
        parts.append(f"at {classification.location}")
    if classification.message:
        parts.append(f"({classification.message})")
    return " ".join(parts)


def tail_lines(text: str, line_count: int = _OUTPUT_TAIL_LINES) -> str:
    return "\n".join(text.splitlines()[-line_count:])
