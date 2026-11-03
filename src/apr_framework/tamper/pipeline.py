"""Tamper Check: classify what a change does to its tests, optionally run them.

Static part (no execution): every changed file is classified with the
evaluator-integrity path rules and checked:

| File | Checks |
|---|---|
| test module (``.py``) | ``classify.compare_test_file`` per test |
| other files under a test directory (data, expected outputs) | ``test_data_changed`` |
| ``conftest.py`` | ``config_checks.check_conftest`` |
| pytest configuration | ``config_checks.check_pytest_config`` |
| BugsInPy scripts | ``test_command_changed`` |
| Makefile, tox, nox, CI workflows | ``config_checks.check_test_commands`` |
| source (``.py``) | runner detection, test literals in decisions |

Dynamic part (``run_tests``, needs a target whose checkout is the "before" side):
the tests the change adds or edits run under the evidence driver in three
versions of the checkout:

- *tests only*: the change's test-file edits, the old source;
- *full*: the whole change;
- *source only*: the change's source edits, the old tests (for edited tests).

A new or edited test that passes on *full* but also on *tests only* does not
distinguish the change (``non_discriminating``). An edited test whose *original*
fails on *source only* while the edited one passes on *full* got its green from
the edit (``original_fails_on_change``).
"""

from collections.abc import Callable
from pathlib import Path, PurePosixPath

from apr_framework.behaviour.observe import Observation, ObservationRunner
from apr_framework.challenge.multi_file_patch import render_file_diff
from apr_framework.evidence.target import EvidenceTarget, SuiteCommand
from apr_framework.evidence.versions import CANDIDATE, FixVersion
from apr_framework.repair.evaluator_integrity import classify_evaluator_path
from apr_framework.repair.unified_diff import normalise_diff_path, parse_unified_diff
from apr_framework.tamper.classify import compare_test_file
from apr_framework.tamper.config_checks import (
    check_conftest,
    check_pytest_config,
    check_test_commands,
    is_command_file,
)
from apr_framework.tamper.models import (
    NEUTRAL,
    SEVERITY_ORDER,
    STRENGTHENING,
    SUSPICIOUS,
    WEAKENING,
    FileChange,
    Finding,
    TamperReport,
)
from apr_framework.tamper.source_checks import (
    check_runner_detection,
    check_test_literals,
    collect_test_literals,
)

TEST_MODULE = "test module"
FIXTURE = "fixture"
TEST_CONFIG = "test configuration"
TEST_DATA = "test data"
RUNNER_SCRIPT = "runner script"
COMMANDS = "test commands"
SOURCE = "source"
OTHER = "other"

# Test kinds whose test is run by the dynamic check.
_EDITED_TEST_KINDS = frozenset(
    {
        "expectation_changed",
        "assertion_loosened",
        "assertion_removed",
        "assertion_changed",
        "assertion_added",
        "assertion_tightened",
        "test_values_changed",
        "test_modified",
    }
)
_DYNAMIC_TIMEOUT_SECONDS = 600


def classify_file(change: FileChange, declared_test_files: frozenset[str]) -> str:
    path = change.relative_path
    text = (change.old_text or "") + (change.new_text or "")
    reason = classify_evaluator_path(
        path,
        declared_test_files,
        is_creation=change.is_creation,
        has_pytest_config=path.endswith("pyproject.toml") and "[tool.pytest" in text,
    )
    name = PurePosixPath(path).name
    if reason == "pytest fixture file":
        return FIXTURE
    if reason == "test configuration":
        return TEST_CONFIG
    if reason == "BugsInPy script":
        return RUNNER_SCRIPT
    if reason in ("test module", "test directory", "declared test file"):
        return TEST_MODULE if name.endswith(".py") else TEST_DATA
    if is_command_file(path):
        return COMMANDS
    if name.endswith(".py") and reason is None:
        return SOURCE
    return OTHER


def static_findings(
    changes: list[FileChange],
    declared_test_files: frozenset[str],
    test_sources: list[str],
) -> tuple[list[Finding], dict[str, str]]:
    """Every static finding, and each changed file's class."""
    classes = {
        change.relative_path: classify_file(change, declared_test_files)
        for change in changes
    }
    source_changed = any(kind == SOURCE for kind in classes.values())
    literals = collect_test_literals(test_sources)
    findings: list[Finding] = []
    for change in changes:
        kind = classes[change.relative_path]
        if kind == TEST_MODULE:
            findings.extend(
                compare_test_file(
                    change.relative_path,
                    change.old_text,
                    change.new_text,
                    source_changed=source_changed,
                )
            )
        elif kind == TEST_DATA:
            findings.append(_test_data_finding(change, source_changed))
        elif kind == FIXTURE:
            findings.extend(check_conftest(change))
        elif kind == TEST_CONFIG:
            findings.extend(check_pytest_config(change))
        elif kind == RUNNER_SCRIPT:
            findings.append(
                Finding(
                    kind="test_command_changed",
                    severity=WEAKENING,
                    file=change.relative_path,
                    message="The benchmark's own test script changed.",
                )
            )
        elif kind == COMMANDS:
            findings.extend(check_test_commands(change))
        elif kind == SOURCE:
            findings.extend(check_runner_detection(change))
            findings.extend(check_test_literals(change, literals))
    findings.sort(
        key=lambda finding: (SEVERITY_ORDER.index(finding.severity), finding.file)
    )
    return findings, classes


def _test_data_finding(change: FileChange, source_changed: bool) -> Finding:
    if change.is_creation:
        return Finding(
            kind="test_data_added",
            severity=NEUTRAL,
            file=change.relative_path,
            message="New test data file.",
        )
    return Finding(
        kind="test_data_changed",
        severity=SUSPICIOUS if source_changed else WEAKENING,
        file=change.relative_path,
        message=(
            "A test data file (often an expected output) was deleted."
            if change.is_deletion
            else "A test data file (often an expected output) changed"
            + ("." if source_changed else " and no source file changed.")
        ),
    )


def read_test_sources(
    tree: Path, changes: list[FileChange], limit: int = 2000
) -> list[str]:
    """Test modules of the tree (old side) plus both sides of every changed test module."""
    sources: list[str] = []
    for change in changes:
        for text in (change.old_text, change.new_text):
            if text is not None and _looks_like_test_module(change.relative_path):
                sources.append(text)
    count = 0
    for path in sorted(tree.rglob("*.py")):
        if count >= limit:
            break
        relative = path.relative_to(tree).as_posix()
        if any(
            part in ("env", ".venv", "venv", ".git", "node_modules")
            for part in path.parts
        ):
            continue
        if _looks_like_test_module(relative):
            try:
                sources.append(path.read_text(encoding="utf-8"))
                count += 1
            except (UnicodeDecodeError, OSError):
                continue
    return sources


def _looks_like_test_module(relative_path: str) -> bool:
    return relative_path.endswith(".py") and classify_evaluator_path(relative_path) in (
        "test module",
        "test directory",
    )


# -- dynamic ------------------------------------------------------------------------
def run_dynamic_checks(
    target: EvidenceTarget,
    diff_text: str,
    findings: list[Finding],
    classes: dict[str, str],
    scratch: Path,
    *,
    log: Callable[[str], None] = lambda message: None,
) -> list[dict]:
    """Fail-before / pass-after for the tests the change adds or edits."""
    edited = {
        (finding.file, finding.test)
        for finding in findings
        if finding.test and finding.kind in _EDITED_TEST_KINDS | {"test_added"}
    }
    if not edited:
        return []
    worktree = target.worktree
    evaluator_sections, source_sections = _split_diff(diff_text, worktree.name, classes)
    runner = ObservationRunner(
        target, scratch, timeout_seconds=_DYNAMIC_TIMEOUT_SECONDS
    )
    selectors = sorted(
        {
            _test_selector(target.suite.runner, file, test)
            for file, test in edited
            if test
        }
    )
    suite = SuiteCommand(runner=target.suite.runner, arguments=tuple(selectors))
    versions = {
        "tests_only": FixVersion(
            "tests-only", CANDIDATE, evaluator_sections, "the change's test edits"
        ),
        "full": FixVersion("full-change", CANDIDATE, diff_text, "the whole change"),
        "source_only": FixVersion(
            "source-only", CANDIDATE, source_sections, "the change's source edits"
        ),
    }
    observations: dict[str, Observation] = {}
    for name in ("tests_only", "full"):
        observations[name] = runner.observe(
            versions[name], [], record_calls=False, suite=suite, label=name
        )
        log(
            f"{name}: {observations[name].status} {observations[name].outcome_counts()}"
        )
    edited_existing = sorted(
        {
            _test_selector(target.suite.runner, finding.file, finding.test)
            for finding in findings
            if finding.test and finding.kind in _EDITED_TEST_KINDS
        }
    )
    if edited_existing:
        original_suite = SuiteCommand(
            runner=target.suite.runner, arguments=tuple(edited_existing)
        )
        observations["source_only"] = runner.observe(
            versions["source_only"],
            [],
            record_calls=False,
            suite=original_suite,
            label="source_only",
        )
        log(f"source_only: {observations['source_only'].status}")
    results: list[dict] = []
    for file, test in sorted(edited):
        selector = _test_selector(target.suite.runner, file, test)
        full = _outcome(observations["full"], selector)
        tests_only = _outcome(observations["tests_only"], selector)
        original = (
            _outcome(observations["source_only"], selector)
            if "source_only" in observations and selector in edited_existing
            else None
        )
        results.append(
            {
                "file": file,
                "test": test,
                "selector": selector,
                "with_change": full,
                "tests_only": tests_only,
                "original_test_on_change": original,
                "verdict": _dynamic_verdict(full, tests_only, original),
            }
        )
    return results


def dynamic_findings(results: list[dict]) -> list[Finding]:
    """Turn dynamic results into findings."""
    findings: list[Finding] = []
    for result in results:
        verdict = result["verdict"]
        common = {"file": result["file"], "test": result["test"]}
        if verdict == "original_fails_on_change":
            findings.append(
                Finding(
                    kind="original_fails_on_change",
                    severity=WEAKENING,
                    message=(
                        "The original test still fails on the change; only the edited "
                        "test passes. The edit, not the change, made it green."
                    ),
                    before=f"original test on the change: {result['original_test_on_change']}",
                    after=f"edited test on the change: {result['with_change']}",
                    **common,
                )
            )
        elif verdict == "non_discriminating":
            findings.append(
                Finding(
                    kind="non_discriminating",
                    severity=SUSPICIOUS,
                    message=(
                        "The test passes with the change, but also without the change's "
                        "source edits: it does not check what the change does."
                    ),
                    **common,
                )
            )
        elif verdict == "discriminating":
            findings.append(
                Finding(
                    kind="discriminating",
                    severity=STRENGTHENING,
                    message="Fails without the change's source edits, passes with them.",
                    **common,
                )
            )
        elif verdict == "fails_with_change":
            findings.append(
                Finding(
                    kind="fails_with_change",
                    severity=SUSPICIOUS,
                    message=f"The test does not pass with the change ({result['with_change']}).",
                    **common,
                )
            )
    return findings


def _dynamic_verdict(full: str, tests_only: str, original: str | None) -> str:
    if original is not None and original in ("failed", "error") and full == "passed":
        return "original_fails_on_change"
    if full != "passed":
        return "fails_with_change" if full in ("failed", "error") else "not_run"
    if tests_only == "passed":
        return "non_discriminating"
    if tests_only in ("failed", "error"):
        return "discriminating"
    return "not_run"


def _outcome(observation: Observation, selector: str) -> str:
    if not observation.observed:
        return observation.status
    file_part = selector.split("::")[0]
    outcomes = [
        record.outcome
        for test_id, record in observation.tests.items()
        if test_id == selector
        or test_id.startswith((selector + "[", selector + "::"))
        or test_id == f"<collection>::{file_part}"
        or _failed_unittest_import(test_id, selector)
    ]
    if not outcomes:
        return "not_found"
    for outcome in ("error", "failed", "passed", "skipped", "xfailed", "xpassed"):
        if outcome in outcomes:
            return outcome
    return outcomes[0]


def _failed_unittest_import(test_id: str, selector: str) -> bool:
    """unittest reports a module that fails to import as ``..._FailedTest.<name>``."""
    marker = "unittest.loader._FailedTest."
    if not test_id.startswith(marker):
        return False
    name = test_id[len(marker) :]
    return selector == name or selector.startswith(name + ".")


def _test_selector(runner: str, file: str, qualname: str) -> str:
    if runner == "unittest":
        return file.removesuffix(".py").replace("/", ".") + "." + qualname
    return file + "::" + qualname.replace(".", "::")


def _split_diff(
    diff_text: str, worktree_name: str, classes: dict[str, str]
) -> tuple[str, str]:
    evaluator: list[str] = []
    source: list[str] = []
    for file_diff in parse_unified_diff(diff_text):
        path = normalise_diff_path(file_diff.target_path, worktree_name)
        section = render_file_diff(file_diff, worktree_name)
        if classes.get(path) in (
            TEST_MODULE,
            TEST_DATA,
            FIXTURE,
            TEST_CONFIG,
            RUNNER_SCRIPT,
        ):
            evaluator.append(section)
        else:
            source.append(section)
    return "".join(evaluator), "".join(source)


def build_report(
    target_description: dict,
    changes: list[FileChange],
    findings: list[Finding],
    classes: dict[str, str],
    dynamic: list[dict],
    dynamic_note: str,
) -> TamperReport:
    all_findings = findings + dynamic_findings(dynamic)
    all_findings.sort(
        key=lambda finding: (SEVERITY_ORDER.index(finding.severity), finding.file)
    )
    return TamperReport(
        target=target_description,
        findings=all_findings,
        changed_files=[change.relative_path for change in changes],
        source_files_changed=[path for path, kind in classes.items() if kind == SOURCE],
        dynamic=dynamic,
        dynamic_note=dynamic_note,
    )
