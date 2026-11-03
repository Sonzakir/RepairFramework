"""Strict Repro unit tests: no Docker, no BugsInPy, no LLM, no network.

The local runner tests run real pytest subprocesses with this interpreter on toy
projects under ``tmp_path``; LLM calls are scripted fakes.
"""

import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

import pytest

from apr_framework.core.exceptions import ConfigurationError
from apr_framework.core.models import BugIdentifier, CheckoutResult
from apr_framework.core.models import TestRunResult as RunResult
from apr_framework.evaluation.repro_runner import (
    ReproCell,
    compute_evaluation_metrics,
    parse_explicit_bug_list,
    refuse_existing_results,
    validate_spend_cap,
)
from apr_framework.repair.evaluator_integrity import IntegrityVerdict
from apr_framework.repair.llm.client import LLMClient, TokenUsage
from apr_framework.repair.patch_applier import ValidationOutcome
from apr_framework.repro import issues
from apr_framework.repro.contamination import max_function_similarity
from apr_framework.repro.generator import GenerationOptions, generate_repro_test
from apr_framework.repro.models import (
    Probe,
    ProbeResult,
    ReproIssue,
    TestVersion,
)
from apr_framework.repro.pipeline import (
    ReproClients,
    ReproOptions,
    check_patch_with_repro_test,
    prepare_local_target,
)
from apr_framework.repro.probes import (
    build_deterministic_probes,
    classify_probe_oracle_outcome,
    functions_touched_by_fix,
    parse_llm_probes,
    select_probes,
)
from apr_framework.repro.report import (
    format_repro_report,
    wrap_as_unittest_witness,
)
from apr_framework.repro.runner import (
    BugsInPyCommandRunner,
    LocalPytestRunner,
    repro_test_file_name,
)
from apr_framework.repro.source_view import build_bugsinpy_source_view
from apr_framework.repro.spend import (
    MeteredLLMClient,
    ModelPrice,
    SpendCapReached,
    SpendMeter,
    resolve_model_price,
)
from apr_framework.repro.strictness import summarise_strictness
from apr_framework.repro.validation import (
    FixUnderTest,
    plan_fix_without_evaluator_files,
)
from apr_framework.repro.verifier import (
    VerificationOptions,
    classify_test_run,
    parse_judge_response,
)

TEST_FILE = "test_repro_toy_1.py"
ISSUE = ReproIssue(
    url="issue.md",
    title="add() returns the wrong sum",
    body="Calling add(2, 2) returns 5. The sum of two and two should be 4.",
)


# --------------------------------------------------------------------- fakes


class ScriptedLLMClient(LLMClient):
    """Returns canned responses in order and records every prompt."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.prompts: list[list[dict[str, str]]] = []

    @property
    def last_usage(self) -> TokenUsage | None:
        return TokenUsage(prompt_tokens=1_000, completion_tokens=200)

    def complete(self, messages: list[dict[str, str]]) -> str:
        self.prompts.append([dict(message) for message in messages])
        return self._responses.pop(0)


def metered(responses: list[str], meter: SpendMeter | None = None) -> MeteredLLMClient:
    return MeteredLLMClient(
        ScriptedLLMClient(responses),
        meter or SpendMeter(cap_usd=1.0),
        ModelPrice("fake", 2.5, 15.0),
        temperature=0.0,
    )


def run_result(raw_output: str, return_code: int = 1) -> RunResult:
    from apr_framework.benchmarks.bugsinpy import parse_test_run_counts

    total, passed, failed, errors = parse_test_run_counts(raw_output)
    return RunResult(
        bug=BugIdentifier("local", "toy", 0),
        results=[],
        raw_output=raw_output,
        total_count=total,
        passed_count=passed,
        failed_count=failed,
        error_count=errors,
        return_code=return_code,
    )


BUGGY_SOURCE = (
    "def add(a, b):\n    return a + b + 1\n\n\ndef sub(a, b):\n    return a - b\n"
)
FIXED_SOURCE = (
    "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n"
)
REPRO_TEST = (
    "from calc import add\n\n\ndef test_add_two_and_two():\n    assert add(2, 2) == 4\n"
)
FIX_DIFF = (
    "--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n"
    "-    return a + b + 1\n+    return a + b\n"
)


@pytest.fixture
def toy_project(tmp_path: Path) -> Path:
    """A pytest project with a seeded bug in add() and a regression suite."""
    project = tmp_path / "toy"
    (project / "tests").mkdir(parents=True)
    (project / "calc.py").write_text(BUGGY_SOURCE, encoding="utf-8")
    (project / "tests" / "test_calc.py").write_text(
        "from calc import add, sub\n\n\ndef test_add_zero():\n    assert add(0, 3) == 3\n\n\n"
        "def test_add_negative():\n    assert add(-1, -1) == -2\n\n\n"
        "def test_sub():\n    assert sub(3, 1) == 2\n",
        encoding="utf-8",
    )
    return project


def local_runner(project: Path, oracle: str | None = None) -> LocalPytestRunner:
    return LocalPytestRunner(
        project,
        Path(sys.executable),
        "toy_1",
        oracle,
        allow_local_exec=True,
    )


# --------------------------------------------------------------------- intake


def test_leak_screen_strips_fix_links_diffs_and_commit_ids() -> None:
    body = (
        "Crash when total is unknown.\n"
        "Fixed in https://github.com/o/r/pull/12 and "
        "https://github.com/o/r/commit/0123456789abcdef0123456789abcdef01234567\n"
        "See https://github.com/o/r/compare/a...b and https://example.com/fix.diff\n"
        "```diff\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n```\n"
        "Docs: https://github.com/o/r/issues/3 stay.\n"
        "bare id 89abcdef0123456789abcdef0123456789abcdef\n"
    )
    issue = issues.screen_issue_for_leaks(
        "u", "Title", body, fix_commit_id="0123456789abcdef"
    )
    assert "/pull/12" not in issue.body
    assert "/commit/" not in issue.body
    assert "/compare/" not in issue.body
    assert "fix.diff" not in issue.body
    assert "+++ b/x.py" not in issue.body
    assert "89abcdef0123456789abcdef0123456789abcdef" not in issue.body
    assert "https://github.com/o/r/issues/3" in issue.body
    assert "Crash when total is unknown." in issue.body
    assert "the issue body contained a diff" in issue.leak_flags
    assert "the issue mentions the fix commit id" in issue.leak_flags


def test_clean_issue_is_not_flagged_and_html_comments_go() -> None:
    issue = issues.screen_issue_for_leaks(
        "u", "Title", "<!-- template -->Steps:\n1. run it", fix_commit_id="abc1234def"
    )
    assert issue.leak_flags == ()
    assert issue.body == "Steps:\n1. run it"


def test_issue_links_are_pointers_only_and_cover_the_development_bugs() -> None:
    links = issues.load_bugsinpy_issue_links()
    assert links["tqdm:5"] == "https://github.com/tqdm/tqdm/issues/574"
    assert all(url.startswith("https://github.com/") for url in links.values())
    with pytest.raises(ConfigurationError):
        issues.find_bugsinpy_issue_url(BugIdentifier("bugsinpy", "black", 1))


def test_github_issue_is_fetched_once_and_cached(tmp_path: Path, monkeypatch) -> None:
    calls = []

    def fake_request(owner, repo, number):
        calls.append((owner, repo, number))
        return {"title": "T", "body": "B", "comments": 9}

    monkeypatch.setattr(issues, "request_github_issue", fake_request)
    url = "https://github.com/tqdm/tqdm/issues/574"
    assert issues.fetch_github_issue_cached(url, tmp_path) == ("T", "B")
    assert issues.fetch_github_issue_cached(url, tmp_path) == ("T", "B")
    assert calls == [("tqdm", "tqdm", "574")]
    assert (tmp_path / issues.ISSUE_CACHE_DIR).is_dir()


def test_pull_request_links_are_refused(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        issues,
        "request_github_issue",
        lambda *_: {"title": "T", "body": "", "pull_request": {}},
    )
    with pytest.raises(ConfigurationError):
        issues.fetch_github_issue_cached("https://github.com/o/r/issues/1", tmp_path)


# --------------------------------------------------------------------- runners


def test_local_runner_refuses_without_the_flag(toy_project: Path) -> None:
    with pytest.raises(ConfigurationError, match="--allow-local-exec"):
        LocalPytestRunner(
            toy_project, Path(sys.executable), "toy_1", None, allow_local_exec=False
        )


def test_local_runner_runs_the_test_outside_the_project(toy_project: Path) -> None:
    runner = local_runner(toy_project)
    before = sorted(path.name for path in toy_project.rglob("*"))
    failing = runner.run_repro_test(TestVersion("v", REPRO_TEST), timeout=60)
    assert failing.failed_count == 1 and failing.return_code != 0
    (toy_project / "calc.py").write_text(FIXED_SOURCE, encoding="utf-8")
    passing = runner.run_repro_test(TestVersion("v", REPRO_TEST), timeout=60)
    assert passing.passed_count == 1 and passing.return_code == 0
    after = sorted(path.name for path in toy_project.rglob("*"))
    assert [name for name in after if name not in before] == []


def test_local_runner_reports_timeouts_and_a_missing_oracle(toy_project: Path) -> None:
    runner = local_runner(toy_project)
    slow = TestVersion("v", "import time\n\n\ndef test_slow():\n    time.sleep(30)\n")
    assert runner.run_repro_test(slow, timeout=2).return_code == 124
    assert not runner.has_oracle
    assert runner.run_tests(runner.checkout).return_code == -1


class _FakeToolchain:
    def executor_path(self, path: Path) -> str:
        return f"/exec{path}"


class _RecordingAdapter:
    def __init__(self) -> None:
        self.toolchain = _FakeToolchain()
        self.commands: list[str] = []
        self.copies_seen: list[bool] = []

    def run_tests(self, checkout, timeout=None, command=None):
        self.commands.append(command)
        directory = Path(
            command.split(" -c ")[1].split("/pytest.ini")[0].removeprefix("/exec")
        )
        self.copies_seen.append((directory / "pytest.ini").is_file())
        return run_result("1 passed in 0.01s", return_code=0)


def test_bugsinpy_runner_runs_an_isolated_copy_and_removes_it(tmp_path: Path) -> None:
    checkout = CheckoutResult(
        bug=BugIdentifier("bugsinpy", "tqdm", 5),
        worktree=tmp_path / ".workspace" / "bugsinpy" / "strictrepro_tqdm_5" / "tqdm",
        success=True,
    )
    checkout.worktree.mkdir(parents=True)
    adapter = _RecordingAdapter()
    runner = BugsInPyCommandRunner(adapter, checkout, tmp_path, "tqdm_5")
    result = runner.run_repro_test(TestVersion("v", REPRO_TEST), timeout=10)
    assert result.passed_count == 1
    command = adapter.commands[0]
    assert command.startswith("python -m pytest ")
    assert "/.workspace/bugsinpy/_repro/tqdm_5-" in command
    assert command.endswith(repro_test_file_name("tqdm_5"))
    assert adapter.copies_seen == [True]
    assert not any((tmp_path / ".workspace" / "bugsinpy" / "_repro").iterdir())
    assert list(checkout.worktree.iterdir()) == []


# --------------------------------------------------------------------- classification

ASSERTION_OUTPUT = """
=================================== FAILURES ===================================
____________________________ test_add_two_and_two _____________________________

    def test_add_two_and_two():
>       assert add(2, 2) == 4
E       assert 5 == 4
E        +  where 5 = add(2, 2)

/tmp/x/test_repro_toy_1.py:5: AssertionError
=========================== short test summary info ============================
FAILED ../tmp/x/test_repro_toy_1.py::test_add_two_and_two - assert 5 == 4
1 failed in 0.02s
"""

PROJECT_EXCEPTION_OUTPUT = """
=================================== FAILURES ===================================
_________________________________ test_total __________________________________

    def test_total():
>       tqdm(gen()).total
/tmp/x/test_repro_toy_1.py:4:
_ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _ _

    def __len__(self):
>       return len(self.iterable)
E       TypeError: object of type 'generator' has no len()

tqdm/_tqdm.py:900: TypeError
1 failed in 0.05s
"""

TEST_NAME_ERROR_OUTPUT = """
=================================== FAILURES ===================================
_________________________________ test_total __________________________________

    def test_total():
>       assert tqdmm(range(3)).total == 3
E       NameError: name 'tqdmm' is not defined

/tmp/x/test_repro_toy_1.py:4: NameError
1 failed in 0.05s
"""

WRONG_CALL_OUTPUT = """
=================================== FAILURES ===================================
_________________________________ test_total __________________________________

    def test_total():
>       add(1, 2, 3)
E       TypeError: add() takes 2 positional arguments but 3 were given

/tmp/x/test_repro_toy_1.py:4: TypeError
1 failed in 0.05s
"""

COLLECTION_OUTPUT = """
==================================== ERRORS ====================================
_____________________ ERROR collecting test_repro_toy_1.py _____________________
ImportError while importing test module '/tmp/x/test_repro_toy_1.py'.
E   ModuleNotFoundError: No module named 'calcx'
!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
1 error in 0.10s
"""

SETUP_OUTPUT = """
==================================== ERRORS ====================================
____________________ ERROR at setup of test_with_fixture _______________________
file /tmp/x/test_repro_toy_1.py, line 3
  def test_with_fixture(project_cfg):
E       fixture 'project_cfg' not found
1 error in 0.01s
"""


@pytest.mark.parametrize(
    ("output", "return_code", "kind", "exception_type"),
    [
        (ASSERTION_OUTPUT, 1, "assertion", "AssertionError"),
        (PROJECT_EXCEPTION_OUTPUT, 1, "exception", "TypeError"),
        (TEST_NAME_ERROR_OUTPUT, 1, "test_error", "NameError"),
        (WRONG_CALL_OUTPUT, 1, "test_error", "TypeError"),
        (COLLECTION_OUTPUT, 2, "collection_error", "ModuleNotFoundError"),
        (SETUP_OUTPUT, 1, "setup_error", ""),
        ("1 passed in 0.01s", 0, "passed", ""),
        ("Traceback ...\nNo module named pytest\n", 1, "environment_error", ""),
        ("[timed out after 5s]", 124, "timeout", ""),
        ("no tests ran in 0.01s", 5, "no_tests", ""),
    ],
)
def test_failure_classification(output, return_code, kind, exception_type) -> None:
    classification = classify_test_run(run_result(output, return_code), TEST_FILE)
    assert classification.kind == kind
    assert classification.exception_type == exception_type
    assert classification.is_reproduction_candidate == (
        kind in ("assertion", "exception")
    )


def test_an_exception_the_issue_names_counts_even_from_the_test_file() -> None:
    output = TEST_NAME_ERROR_OUTPUT.replace("NameError", "KeyError")
    classification = classify_test_run(
        run_result(output), TEST_FILE, issue_text="It raises KeyError: 'total'"
    )
    assert classification.kind == "exception"


def test_a_broken_test_never_counts_even_if_the_issue_names_the_error() -> None:
    classification = classify_test_run(
        run_result(TEST_NAME_ERROR_OUTPUT), TEST_FILE, issue_text="a NameError occurs"
    )
    assert classification.kind == "test_error"


# --------------------------------------------------------------------- judge


def test_judge_label_needs_a_real_quote_from_the_issue() -> None:
    good = parse_judge_response(
        '{"label": "reproduces", "justification": "sum is 5", '
        '"issue_quote": "The sum of two and two should be 4."}',
        ISSUE.text,
    )
    assert good.reproduces and good.quote_found
    invented = parse_judge_response(
        '{"label": "reproduces", "justification": "x", "issue_quote": "add is broken badly"}',
        ISSUE.text,
    )
    assert invented.label == "unclear" and not invented.quote_found
    assert parse_judge_response("I think it reproduces", ISSUE.text).label == "unclear"
    assert (
        parse_judge_response('{"label": "maybe", "issue_quote": ""}', ISSUE.text).label
        == "unclear"
    )


# --------------------------------------------------------------------- generation


class _ScriptedRunner:
    """Returns canned test runs in order (the generator runs each version once)."""

    def __init__(self, outputs: list[tuple[str, int]]) -> None:
        self._outputs = list(outputs)
        self.checkout = CheckoutResult(
            bug=BugIdentifier("local", "toy", 0), worktree=Path("."), success=True
        )

    def run_repro_test(self, test, timeout):
        output, return_code = self._outputs.pop(0)
        return run_result(output, return_code)


def _view(tmp_path: Path):
    from apr_framework.repro.source_view import build_local_source_view

    (tmp_path / "proj").mkdir(exist_ok=True)
    (tmp_path / "proj" / "calc.py").write_text(BUGGY_SOURCE, encoding="utf-8")
    checkout = CheckoutResult(
        bug=BugIdentifier("local", "proj", 0), worktree=tmp_path / "proj", success=True
    )
    return build_local_source_view(checkout, tmp_path / "view")


def test_generation_feeds_back_until_a_real_failure(tmp_path: Path) -> None:
    client = metered(
        [
            'RETRIEVE: get_function_definition("add")',
            "```python\nfrom calc import addd\n\ndef test_x():\n    assert addd(2, 2) == 4\n```",
            f"```python\n{REPRO_TEST}```",
        ]
    )
    runner = _ScriptedRunner([(COLLECTION_OUTPUT, 2), (ASSERTION_OUTPUT, 1)])
    result = generate_repro_test(
        ISSUE,
        _view(tmp_path),
        runner,
        client,
        TEST_FILE,
        GenerationOptions(max_rounds=3),
    )
    assert result.rounds == 2
    assert result.final.source == REPRO_TEST
    assert result.retrievals == ["get_function_definition(add)"]
    retrieval_answer = client._inner.prompts[1][-1]["content"]
    assert "def add(a, b)" in retrieval_answer
    feedback = client._inner.prompts[2][-1]["content"]
    assert "collection_error" in feedback
    assert {record.purpose for record in client.meter.records} == {"generation"}


def test_generation_stops_after_max_rounds(tmp_path: Path) -> None:
    client = metered([f"```python\n{REPRO_TEST}```"] * 2)
    runner = _ScriptedRunner([("1 passed in 0.01s", 0)] * 2)
    result = generate_repro_test(
        ISSUE,
        _view(tmp_path),
        runner,
        client,
        TEST_FILE,
        GenerationOptions(max_rounds=2),
    )
    assert result.rounds == 2
    assert "no version failed" in result.stop_reason


def test_assertflip_passes_first_then_inverts(tmp_path: Path) -> None:
    passing = "from calc import add\n\n\ndef test_add():\n    assert add(2, 2) == 5\n"
    client = metered([f"```python\n{passing}```", f"```python\n{REPRO_TEST}```"])
    runner = _ScriptedRunner([("1 passed in 0.01s", 0), (ASSERTION_OUTPUT, 1)])
    result = generate_repro_test(
        ISSUE,
        _view(tmp_path),
        runner,
        client,
        TEST_FILE,
        GenerationOptions(max_rounds=3, strategy="assertflip"),
    )
    assert [version.label for version in result.versions] == [
        "assertflip-pass-1",
        "assertflip-inverted",
    ]
    assert result.final.source == REPRO_TEST


# --------------------------------------------------------------------- source view


def test_bugsinpy_source_view_hides_evaluator_files(tmp_path: Path) -> None:
    worktree = tmp_path / "tqdm"
    for relative_path, text in {
        "tqdm/_tqdm.py": "def tqdm():\n    pass\n",
        "tqdm/tests/tests_tqdm.py": "def test_trigger():\n    secret = 1\n",
        "tqdm/tests/tests_contrib.py": "import tqdm\n\ndef test_other():\n    pass\n",
        "tqdm/tests/tests_touched.py": "def test_touched():\n    pass\n",
        "bugsinpy_bug.info": 'test_file="tqdm/tests/tests_tqdm.py"\n',
        "conftest.py": "",
    }.items():
        (worktree / relative_path).parent.mkdir(parents=True, exist_ok=True)
        (worktree / relative_path).write_text(text, encoding="utf-8")
    checkout = CheckoutResult(
        bug=BugIdentifier("bugsinpy", "tqdm", 5), worktree=worktree, success=True
    )
    view = build_bugsinpy_source_view(
        checkout,
        frozenset({"tqdm/tests/tests_tqdm.py"}),
        frozenset({"tqdm/_tqdm.py", "tqdm/tests/tests_touched.py"}),
        tmp_path / "view",
    )
    copied = sorted(
        path.relative_to(tmp_path / "view").as_posix()
        for path in (tmp_path / "view").rglob("*.py")
    )
    assert copied == ["tqdm/_tqdm.py"]
    assert view.test_file_names == ("tqdm/tests/tests_contrib.py",)
    assert "secret" not in view.style_excerpt
    assert "tests_contrib.py" in view.style_excerpt


# --------------------------------------------------------------------- probes


def _toy_fix(project: Path) -> FixUnderTest:
    return plan_fix_without_evaluator_files("fix.diff", FIX_DIFF, project, frozenset())


def test_deterministic_probes_cover_operator_and_drop(toy_project: Path) -> None:
    probes = build_deterministic_probes(_toy_fix(toy_project))
    families = {probe.family for probe in probes}
    assert families == {"operator", "drop"}
    assert all(probe.path == "calc.py" and probe.line == 2 for probe in probes)
    assert any(probe.after == "return a - b" for probe in probes)
    assert any(probe.after == "pass" for probe in probes)


def test_revert_probes_need_two_change_blocks(tmp_path: Path) -> None:
    source = (
        "def f(x):\n    a = x + 1\n    b = 2\n    c = 3\n    d = 4\n    return a * x\n"
    )
    fixed = source.replace("x + 1", "abs(x) + 1").replace("a * x", "a * x * 2")
    fix = FixUnderTest("fix", {"m.py": (source.encode(), fixed.encode())}, (), "")
    probes = build_deterministic_probes(fix)
    reverts = [probe for probe in probes if probe.family == "revert"]
    assert len(reverts) == 2


def test_probe_selection_is_seeded_and_round_robin() -> None:
    def probe(family: str, index: int) -> Probe:
        return Probe("", family, family, "m.py", index, "", "", f"{family}{index}")

    pool = [probe("operator", i) for i in range(10)] + [
        probe("drop", i) for i in range(3)
    ]
    first = select_probes(pool, 4, seed="tqdm_5")
    assert first == select_probes(pool, 4, seed="tqdm_5")
    assert {p.family for p in first} == {"operator", "drop"}
    assert len(first) == 4


def test_llm_probes_are_spliced_into_the_fixed_function(toy_project: Path) -> None:
    fix = _toy_fix(toy_project)
    functions = functions_touched_by_fix(fix)
    assert [function.name for function in functions] == ["add"]
    response = (
        '{"variants": [{"file": "calc.py", "function": "add", '
        '"code": "def add(a, b):\\n    if (a, b) == (2, 2):\\n        return 4\\n'
        '    return a + b + 1", "rationale": "only the tested input"},'
        '{"file": "calc.py", "function": "nope", "code": "def nope(): pass"},'
        '{"file": "calc.py", "function": "add", "code": "def add(a, b) return"}]}'
    )
    probes = parse_llm_probes(response, "input_specific_shortcut", fix, functions)
    assert len(probes) == 1
    assert "if (a, b) == (2, 2):" in probes[0].probe_text
    assert "def sub(a, b):" in probes[0].probe_text
    assert probes[0].rationale == "only the tested input"


def _outcome(
    applied=True,
    trigger_passed=False,
    total=1,
    return_code=1,
    new_failures=(),
    untrusted=False,
    integrity=None,
    output="1 failed in 0.1s",
) -> ValidationOutcome:
    trigger = run_result(output, return_code)
    trigger.total_count = total
    return ValidationOutcome(
        is_plausible=False,
        trigger_test_run_result=trigger,
        trigger_passed=trigger_passed,
        regression_new_failures=list(new_failures),
        regression_untrusted=untrusted,
        integrity=integrity if integrity is not None else IntegrityVerdict(),
        applied=applied,
    )


@pytest.mark.parametrize(
    ("outcome", "has_oracle", "usable", "verdict", "confirmed_by"),
    [
        (_outcome(), True, True, "confirmed_wrong", "trigger"),
        (
            _outcome(trigger_passed=True, return_code=0, new_failures=["t::x"]),
            True,
            True,
            "confirmed_wrong",
            "test_file",
        ),
        (_outcome(trigger_passed=True, return_code=0), True, True, "unconfirmed", ""),
        (
            _outcome(trigger_passed=True, return_code=0, untrusted=True),
            True,
            True,
            "unconfirmed",
            "",
        ),
        (_outcome(return_code=124), True, True, "unconfirmed", ""),
        (_outcome(total=0), True, True, "unconfirmed", ""),
        (_outcome(), False, False, "unconfirmed", ""),
        (_outcome(), True, False, "unconfirmed", ""),
        (_outcome(applied=False), True, True, "invalid", ""),
        (
            _outcome(
                integrity=IntegrityVerdict("evaluator_file_edit", ("tests/x.py",))
            ),
            True,
            True,
            "invalid",
            "",
        ),
        (
            _outcome(output="SyntaxError: bad\n1 error in 0.1s"),
            True,
            True,
            "invalid",
            "",
        ),
    ],
)
def test_probe_confirmation_rule(
    outcome, has_oracle, usable, verdict, confirmed_by
) -> None:
    observed, observed_by, _ = classify_probe_oracle_outcome(
        outcome, has_oracle, "trigger", usable
    )
    assert (observed, observed_by) == (verdict, confirmed_by)


# --------------------------------------------------------------------- strictness


def _probe_result(probe_id: str, verdict: str, by: str, repro: str) -> ProbeResult:
    probe = Probe(probe_id, "operator", "comp", "m.py", 1, "", "", "")
    return ProbeResult(probe, verdict, confirmed_by=by, repro_verdict=repro)


def test_strictness_counts_only_confirmed_probes() -> None:
    results = [
        _probe_result("p01", "confirmed_wrong", "trigger", "rejects"),
        _probe_result("p02", "confirmed_wrong", "trigger", "accepts"),
        _probe_result("p03", "confirmed_wrong", "test_file", "rejects"),
        _probe_result("p04", "unconfirmed", "", "rejects"),
        _probe_result("p05", "unconfirmed", "", "accepts"),
        _probe_result("p06", "invalid", "", "not_run"),
    ]
    summary = summarise_strictness("round-1", results, has_trigger_baseline=True)
    assert (
        summary.generated,
        summary.confirmed,
        summary.unconfirmed,
        summary.invalid,
    ) == (
        6,
        3,
        2,
        1,
    )
    assert summary.rejected_by_repro == 2
    assert summary.strictness == pytest.approx(2 / 3)
    assert summary.accepted_probe_ids == ("p02",)
    assert summary.trigger_rejects == 2
    assert summary.unconfirmed_rejected == ("p04",)
    local = summarise_strictness("v", results[3:], has_trigger_baseline=False)
    assert local.strictness is None and local.trigger_rejects is None


# --------------------------------------------------------------------- spend


def test_spend_meter_records_cost_and_enforces_the_cap() -> None:
    meter = SpendMeter(cap_usd=0.07)
    client = metered(["a", "b", "c"], meter)
    with client.purpose("judge"):
        client.complete([{"role": "user", "content": "x" * 400}])
    record = meter.records[0]
    assert record.purpose == "judge"
    assert record.usd == pytest.approx((1_000 * 2.5 + 200 * 15.0) / 1_000_000)
    assert not record.usage_estimated
    with pytest.raises(SpendCapReached):
        for _ in range(20):
            client.complete([{"role": "user", "content": "x"}])
    assert meter.spent_usd <= meter.cap_usd


def test_unknown_models_need_an_explicit_price() -> None:
    assert resolve_model_price("gpt-5.4", None, None).usd_per_mtok_out == 15.0
    with pytest.raises(ConfigurationError):
        resolve_model_price("mystery-model", None, None)
    assert resolve_model_price("mystery-model", 1.0, 2.0).usd_per_mtok_in == 1.0


# --------------------------------------------------------------------- local check


def _clients(responses: list[str]) -> ReproClients:
    meter = SpendMeter(cap_usd=1.0)
    client = metered(responses, meter)
    return ReproClients(generator=client, judge=client, meter=meter)


JUDGE_YES = (
    '{"label": "reproduces", "justification": "add(2, 2) returns 5", '
    '"issue_quote": "Calling add(2, 2) returns 5."}'
)


def test_repro_check_end_to_end_on_a_toy_project(
    toy_project: Path, tmp_path: Path
) -> None:
    """Integrity gate, F→P through the shared validation path, confirmed probes."""
    patch = tmp_path / "fix.diff"
    patch.write_text(FIX_DIFF, encoding="utf-8")
    runner = local_runner(toy_project, oracle=f"{sys.executable} -m pytest -q tests")
    fix = plan_fix_without_evaluator_files(
        "fix.diff", FIX_DIFF, runner.repo, frozenset()
    )
    target = prepare_local_target(runner, "toy_1", tmp_path, fix, with_view=False)
    before = snapshot_bytes(toy_project)
    options = ReproOptions(
        verification=VerificationOptions(repeats=2, timeout_seconds=60),
        harden=False,
    )
    from apr_framework.repro.probes import ProbeOptions

    options = ReproOptions(
        verification=options.verification,
        probes=ProbeOptions(max_probes=4, max_llm_probes=0),
        harden=False,
    )
    result = check_patch_with_repro_test(
        TestVersion("repro", REPRO_TEST), ISSUE, target, _clients([JUDGE_YES]), options
    )
    verification = result.verification
    assert [run.kind for run in verification.buggy_runs] == ["assertion", "assertion"]
    assert verification.judge.reproduces
    assert verification.fix_check.repro_passes and verification.is_fail_to_pass
    assert verification.is_verified_reproduction
    assert result.strictness_before is not None
    assert result.strictness_before.confirmed >= 1
    assert all(r.oracle_verdict != "invalid" for r in result.probe_results)
    assert snapshot_bytes(toy_project) == before
    report = format_repro_report(result)
    assert "fails on current code" in report and "wrong-fix probes" in report


def test_a_patch_that_edits_tests_is_judged_without_them(toy_project: Path) -> None:
    diff = FIX_DIFF + (
        "--- a/tests/test_calc.py\n+++ b/tests/test_calc.py\n@@ -1,1 +1,1 @@\n"
        "-from calc import add, sub\n+from calc import add, sub  # edited\n"
    )
    fix = plan_fix_without_evaluator_files("p.diff", diff, toy_project, frozenset())
    assert list(fix.files) == ["calc.py"]
    assert fix.stripped_paths == ("tests/test_calc.py",)


def test_test_sections_that_do_not_apply_are_still_stripped(toy_project: Path) -> None:
    """BugsInPy checkouts already carry the fixed tests, so their hunks won't apply."""
    diff = FIX_DIFF + (
        "--- a/tests/test_calc.py\n+++ b/tests/test_calc.py\n@@ -1,1 +1,1 @@\n"
        "-this line is not in the file\n+replacement\n"
    )
    fix = plan_fix_without_evaluator_files("p.diff", diff, toy_project, frozenset())
    assert list(fix.files) == ["calc.py"]
    assert fix.stripped_paths == ("tests/test_calc.py",)


def snapshot_bytes(project: Path) -> dict[str, bytes]:
    return {
        path.relative_to(project).as_posix(): path.read_bytes()
        for path in project.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and ".pytest_cache" not in path.parts
    }


# --------------------------------------------------------------------- evaluation


def test_evaluate_repro_refuses_without_an_explicit_list_and_cap(
    tmp_path: Path,
) -> None:
    for bad in (None, "", "  ", "tqdm", "tqdm:x"):
        with pytest.raises(ConfigurationError):
            parse_explicit_bug_list(bad)
    assert [b.bug_id for b in parse_explicit_bug_list("tqdm:1, tqdm:1,httpie:4")] == [
        1,
        4,
    ]
    for bad_cap in (None, 0, -1):
        with pytest.raises(ConfigurationError):
            validate_spend_cap(bad_cap)
    (tmp_path / "results.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ConfigurationError):
        refuse_existing_results(tmp_path)


def test_evaluation_metrics_follow_the_feature_summary_definitions() -> None:
    def summary(
        verified, labelled, f2p, before, trigger, after=None, usd=0.2, seconds=120
    ):
        return {
            "verdict": {
                "verified_reproduction": verified,
                "labelled_reproduces": labelled,
                "fail_to_pass": f2p,
            },
            "strictness_before": {
                "confirmed": 4,
                "rejected_by_repro": before,
                "trigger_rejects": trigger,
            },
            "hardening": {
                "kept": after is not None,
                "after": {"rejected_by_repro": after, "trigger_rejects": trigger}
                if after is not None
                else None,
            },
            "near_copy": False,
            "llm_usd": usd,
            "seconds": seconds,
        }

    cells = [
        ReproCell("a:1", "ok", summary=summary(True, True, True, 2, 3, after=3)),
        ReproCell("a:2", "ok", summary=summary(False, True, False, 1, 1)),
        ReproCell("a:3", "error", error="boom"),
        ReproCell("a:4", "not_run", error="spend cap reached"),
    ]
    metrics = compute_evaluation_metrics(cells)
    assert metrics["verified_reproduction_rate"] == 0.25
    assert metrics["label_precision"] == 0.5
    assert metrics["strict_at_least_baseline_share"] == 1.0
    assert metrics["hardening_changes_nothing_share"] == 0.5
    assert metrics["median_usd_per_bug"] == 0.2


# --------------------------------------------------------------------- report bits


def test_witness_wrapper_runs_fixture_free_tests_under_unittest(tmp_path: Path) -> None:
    witness = tmp_path / "test_witness_x.py"
    witness.write_text(
        wrap_as_unittest_witness("def test_ok():\n    assert 1 + 1 == 2\n"),
        encoding="utf-8",
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "unittest",
            "discover",
            "-s",
            str(tmp_path),
            "-p",
            witness.name,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "Ran 1 test" in completed.stderr


def test_witness_wrapper_refuses_fixture_tests() -> None:
    namespace: dict = {}
    source = wrap_as_unittest_witness("def test_fx(tmp_path):\n    pass\n")
    with pytest.raises(TypeError, match="not exportable"):
        exec(compile(source, "w.py", "exec"), namespace)  # noqa: S102 - on purpose
    assert unittest  # the wrapper relies only on the standard library


def test_contamination_similarity_flags_near_copies() -> None:
    reference = [
        "def test_iterable_total():\n    assert tqdm(gen(), total=3).total == 3"
    ]
    copy = "def test_x():\n    assert tqdm(gen(), total=3).total == 3\n"
    different = "def test_y():\n    with open('f') as f:\n        f.read()\n"
    assert max_function_similarity(copy, reference) == pytest.approx(1.0)
    assert max_function_similarity(different, reference) < 0.5
    assert max_function_similarity(copy, []) is None


def test_test_file_names_are_importable() -> None:
    assert repro_test_file_name("tqdm-5.x") == "test_repro_tqdm_5_x.py"
    assert textwrap.dedent("x") == "x"
