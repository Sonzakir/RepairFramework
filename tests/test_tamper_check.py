"""Tamper Check: test-edit classification, config and source checks, dynamic runs."""

import difflib
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from apr_framework.evidence.target import LocalTarget, SuiteCommand
from apr_framework.tamper.changes import changes_from_diff, changes_from_git
from apr_framework.tamper.classify import compare_test_file
from apr_framework.tamper.config_checks import (
    check_conftest,
    check_pytest_config,
    check_test_commands,
)
from apr_framework.tamper.models import FileChange
from apr_framework.tamper.pipeline import (
    build_report,
    read_test_sources,
    run_dynamic_checks,
    static_findings,
)
from apr_framework.tamper.source_checks import (
    check_runner_detection,
    check_test_literals,
    collect_test_literals,
)

BASE_TEST = textwrap.dedent(
    """\
    import unittest

    from calc import add, ratio


    class CalcTest(unittest.TestCase):
        def test_add(self):
            self.assertEqual(add(1, 2), 3)
            self.assertEqual(add(-1, 1), 0)

        def test_ratio(self):
            self.assertAlmostEqual(ratio(1, 3), 0.3333333, places=6)

        def test_errors(self):
            with self.assertRaises(ZeroDivisionError):
                ratio(1, 0)
    """
)


def _kinds(findings):
    return sorted((finding.kind, finding.severity) for finding in findings)


def _edit(old: str, new: str, *, source_changed: bool = True):
    return compare_test_file(
        "tests/test_calc.py", old, new, source_changed=source_changed
    )


# -- test functions -------------------------------------------------------------------
def test_rewritten_expectation_without_source_change_is_weakening() -> None:
    new = BASE_TEST.replace("add(1, 2), 3", "add(1, 2), 4")
    assert _kinds(_edit(BASE_TEST, new, source_changed=False)) == [
        ("expectation_changed", "weakening")
    ]
    assert _kinds(_edit(BASE_TEST, new, source_changed=True)) == [
        ("expectation_changed", "suspicious")
    ]


def test_black3_style_keyword_expectation_is_caught() -> None:
    old = (
        "class T:\n    def test_x(self):\n        self.invokeBlack(args, exit_code=2)\n"
    )
    new = old.replace("exit_code=2", "exit_code=1")
    findings = _edit(old, new, source_changed=False)
    # invokeBlack asserts internally; the edit is caught as a literal-only change.
    assert _kinds(findings) == [("test_values_changed", "weakening")]
    assert (findings[0].before, findings[0].after) == (
        "self.invokeBlack(args, exit_code=2)",
        "self.invokeBlack(args, exit_code=1)",
    )
    assert _kinds(_edit(old, new, source_changed=True)) == [
        ("test_values_changed", "suspicious")
    ]
    restructured = old.replace("self.invokeBlack(args, exit_code=2)", "run(args)")
    assert _kinds(_edit(old, restructured)) == [("test_modified", "neutral")]


def test_deletion_rename_and_addition() -> None:
    renamed = BASE_TEST.replace("def test_errors", "def test_division_errors")
    assert _kinds(_edit(BASE_TEST, renamed)) == [("test_renamed", "neutral")]
    deleted = BASE_TEST.split("    def test_errors")[0]
    assert _kinds(_edit(BASE_TEST, deleted)) == [("test_deleted", "weakening")]
    added = (
        BASE_TEST
        + "\n    def test_more(self):\n        self.assertEqual(add(2, 2), 4)\n"
    )
    assert _kinds(_edit(BASE_TEST, added)) == [("test_added", "strengthening")]


@pytest.mark.parametrize(
    ("replacement", "expected"),
    [
        (
            (
                "    def test_add(self):",
                "    @unittest.skip('flaky')\n    def test_add(self):",
            ),
            [("test_skipped", "weakening")],
        ),
        (
            (
                "    def test_add(self):",
                "    @unittest.skipIf(sys.platform == 'win32', 'x')\n    def test_add(self):",
            ),
            [("test_skipped", "suspicious")],
        ),
        (
            (
                "    def test_add(self):\n",
                "    def test_add(self):\n        self.skipTest('later')\n",
            ),
            [("test_skipped", "weakening")],
        ),
        (
            ("    def test_add(self):\n", "    def test_add(self):\n        return\n"),
            [("test_skipped", "weakening")],
        ),
        (
            (
                "    def test_add(self):",
                "    @unittest.expectedFailure\n    def test_add(self):",
            ),
            [("test_xfailed", "weakening")],
        ),
    ],
)
def test_skips_and_expected_failures(replacement, expected) -> None:
    assert _kinds(_edit(BASE_TEST, BASE_TEST.replace(*replacement, 1))) == expected


@pytest.mark.parametrize(
    ("old", "new", "kind"),
    [
        (
            "self.assertEqual(add(1, 2), 3)",
            "self.assertTrue(add(1, 2))",
            "assertion_loosened",
        ),
        ("places=6", "places=2", "assertion_loosened"),
        (
            "self.assertRaises(ZeroDivisionError)",
            "self.assertRaises(Exception)",
            "assertion_loosened",
        ),
        ("        self.assertEqual(add(-1, 1), 0)\n", "", "assertion_removed"),
    ],
)
def test_loosened_and_removed_assertions(old, new, kind) -> None:
    assert _kinds(_edit(BASE_TEST, BASE_TEST.replace(old, new, 1))) == [
        (kind, "weakening")
    ]


def test_pytest_style_loosening_and_tightening() -> None:
    old = "def test_x():\n    assert f() == [1, 2]\n    assert g() == pytest.approx(1.0, rel=1e-6)\n"
    loosened = old.replace("f() == [1, 2]", "1 in f()").replace("rel=1e-6", "rel=1e-2")
    findings = compare_test_file("t/test_x.py", old, loosened, source_changed=True)
    assert sorted((finding.kind, finding.message) for finding in findings) == [
        ("assertion_loosened", "An exact check (==) became a weaker one (in)."),
        ("assertion_loosened", "Tolerance rel loosened (1e-06 → 0.01)."),
    ]
    tightened = "def test_x():\n    assert f()\n"
    stricter = "def test_x():\n    assert f() == 3\n"
    assert _kinds(
        compare_test_file("t/test_x.py", tightened, stricter, source_changed=True)
    ) == [("assertion_tightened", "strengthening")]


def test_swallowed_exceptions_parametrize_and_module_skips() -> None:
    old = textwrap.dedent(
        """\
        import pytest

        @pytest.mark.parametrize("n", [1, 2, 3])
        def test_n(n):
            assert check(n)
        """
    )
    swallowed = old.replace(
        "    assert check(n)\n",
        "    try:\n        assert check(n)\n    except Exception:\n        pass\n",
    )
    assert ("exception_swallowed", "weakening") in _kinds(
        compare_test_file("t/test_n.py", old, swallowed, source_changed=True)
    )
    fewer = old.replace("[1, 2, 3]", "[1]")
    assert _kinds(
        compare_test_file("t/test_n.py", old, fewer, source_changed=True)
    ) == [("cases_removed", "weakening")]
    skipped = old.replace(
        "import pytest\n", "import pytest\npytestmark = pytest.mark.skip\n"
    )
    assert ("module_skipped", "weakening") in _kinds(
        compare_test_file("t/test_n.py", old, skipped, source_changed=True)
    )


def test_unparseable_test_file_is_weakening() -> None:
    assert _kinds(_edit(BASE_TEST, BASE_TEST + "\n    def broken(:\n")) == [
        ("test_file_unparseable", "weakening")
    ]


# -- configuration and commands ------------------------------------------------------
def test_conftest_deselection_and_config_options() -> None:
    conftest = FileChange(
        "conftest.py",
        "import pytest\n",
        "import pytest\n\ndef pytest_collection_modifyitems(config, items):\n    items[:] = [i for i in items if 'slow' not in i.name]\n",
    )
    assert _kinds(check_conftest(conftest)) == [
        ("hook_or_fixture_changed", "weakening")
    ]
    ini = FileChange(
        "pytest.ini",
        "[pytest]\naddopts = -q\n",
        "[pytest]\naddopts = -q --deselect tests/test_a.py::test_b\n",
    )
    assert _kinds(check_pytest_config(ini)) == [("test_selection_changed", "weakening")]
    makefile = FileChange(
        "Makefile",
        "test:\n\tpytest tests\n",
        "test:\n\tpytest tests -k 'not network'\n",
    )
    assert _kinds(check_test_commands(makefile)) == [
        ("test_selection_changed", "weakening")
    ]
    removed = FileChange(
        "Makefile", "test:\n\tpytest tests\n", "test:\n\techo skipped\n"
    )
    assert _kinds(check_test_commands(removed)) == [
        ("test_command_removed", "weakening")
    ]


# -- source -----------------------------------------------------------------------
def test_runner_detection_and_test_literal_special_casing() -> None:
    detecting = FileChange(
        "calc.py",
        "def add(a, b):\n    return a + b\n",
        "import os\n\ndef add(a, b):\n    if 'PYTEST_CURRENT_TEST' in os.environ:\n        return 3\n    return a + b\n",
    )
    assert _kinds(check_runner_detection(detecting)) == [
        ("runner_detection", "weakening")
    ]
    literals = collect_test_literals(
        ["def test_user():\n    assert lookup('alice@example.com') == 42\n"]
    )
    special_cased = FileChange(
        "users.py",
        "def lookup(name):\n    return DB[name]\n",
        "def lookup(name):\n    if name == 'alice@example.com':\n        return 42\n    return DB[name]\n",
    )
    findings = check_test_literals(special_cased, literals)
    assert [finding.kind for finding in findings] == ["test_literal_in_source"] * 2
    honest = FileChange(
        "users.py",
        "def lookup(name):\n    return DB[name]\n",
        "def lookup(name):\n    return DB.get(name)\n",
    )
    assert check_test_literals(honest, literals) == []


def test_test_data_edits_without_source_change_are_weakening() -> None:
    changes = [FileChange("tests/data/expected.txt", "a = 1\n", "a=1\n")]
    findings, classes = static_findings(changes, frozenset(), [])
    assert classes == {"tests/data/expected.txt": "test data"}
    assert _kinds(findings) == [("test_data_changed", "weakening")]


# -- change sets ------------------------------------------------------------------
def _diff(path: str, old: str, new: str) -> str:
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )


def test_changes_from_diff_and_git(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_calc.py").write_text(BASE_TEST, encoding="utf-8")
    edited = BASE_TEST.replace("add(1, 2), 3", "add(1, 2), 4")
    changes = changes_from_diff(
        _diff("tests/test_calc.py", BASE_TEST, edited), tmp_path
    )
    assert [
        (c.relative_path, c.old_text == BASE_TEST, c.new_text == edited)
        for c in changes
    ] == [("tests/test_calc.py", True, True)]
    git = ["git", "-C", str(tmp_path), "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", "base"], check=True)
    (tmp_path / "tests" / "test_calc.py").write_text(edited, encoding="utf-8")
    git_changes = changes_from_git(tmp_path, "HEAD")
    assert [change.relative_path for change in git_changes] == ["tests/test_calc.py"]
    findings, _ = static_findings(
        git_changes, frozenset(), read_test_sources(tmp_path, git_changes)
    )
    assert _kinds(findings) == [("expectation_changed", "weakening")]


def test_cli_exit_codes(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_calc.py").write_text(BASE_TEST, encoding="utf-8")
    tampered = tmp_path / "tampered.diff"
    tampered.write_text(
        _diff(
            "tests/test_calc.py",
            BASE_TEST,
            BASE_TEST.replace("add(1, 2), 3", "add(1, 2), 4"),
        ),
        encoding="utf-8",
    )
    added = tmp_path / "added.diff"
    added.write_text(
        _diff(
            "tests/test_calc.py",
            BASE_TEST,
            BASE_TEST
            + "\n    def test_more(self):\n        self.assertEqual(add(2, 2), 4)\n",
        ),
        encoding="utf-8",
    )

    def run(diff: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "apr_framework",
                "tamper-check",
                "--diff",
                str(diff),
                "--repo",
                str(tmp_path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    tampered_run = run(tampered)
    assert tampered_run.returncode == 1 and "Verdict: TAMPERED" in tampered_run.stdout
    clean_run = run(added)
    assert clean_run.returncode == 0 and "Verdict: CLEAN" in clean_run.stdout


# -- dynamic ------------------------------------------------------------------------
CALC = "def add(a, b):\n    return a - b if a > 5 else a + b\n\n\ndef ratio(a, b):\n    return a / b\n"


def _calc_project(tmp_path: Path) -> Path:
    project = tmp_path / "proj"
    (project / "tests").mkdir(parents=True)
    (project / "calc.py").write_text(CALC, encoding="utf-8")
    (project / "tests" / "__init__.py").write_text("", encoding="utf-8")
    (project / "tests" / "test_calc.py").write_text(BASE_TEST, encoding="utf-8")
    return project


def _dynamic(project: Path, diff_text: str):
    target = LocalTarget(
        project,
        Path(sys.executable),
        SuiteCommand.parse("python -m unittest tests.test_calc"),
        allow_local_exec=True,
    )
    changes = changes_from_diff(diff_text, project)
    findings, classes = static_findings(
        changes, frozenset(), read_test_sources(project, changes)
    )
    with target.scratch_directory("tamper") as scratch:
        results = run_dynamic_checks(target, diff_text, findings, classes, scratch)
    return build_report(target.describe(), changes, findings, classes, results, "")


def test_dynamic_checks_classify_new_and_edited_tests(tmp_path: Path) -> None:
    project = _calc_project(tmp_path)
    fix = _diff("calc.py", CALC, CALC.replace("a - b if a > 5 else a + b", "a + b"))
    discriminating = _diff(
        "tests/test_calc.py",
        BASE_TEST,
        BASE_TEST
        + "\n    def test_large(self):\n        self.assertEqual(add(7, 1), 8)\n",
    )
    report = _dynamic(project, fix + discriminating)
    assert {(r["test"], r["verdict"]) for r in report.dynamic} == {
        ("CalcTest.test_large", "discriminating")
    }
    assert report.verdict == "clean"

    not_discriminating = _diff(
        "tests/test_calc.py",
        BASE_TEST,
        BASE_TEST
        + "\n    def test_small(self):\n        self.assertEqual(add(1, 1), 2)\n",
    )
    report = _dynamic(project, fix + not_discriminating)
    assert {(r["test"], r["verdict"]) for r in report.dynamic} == {
        ("CalcTest.test_small", "non_discriminating")
    }
    assert report.verdict == "suspicious"


def test_dynamic_checks_confirm_a_rewritten_expectation(tmp_path: Path) -> None:
    project = _calc_project(tmp_path)
    (project / "tests" / "test_calc.py").write_text(
        BASE_TEST
        + "\n    def test_large(self):\n        self.assertEqual(add(7, 1), 8)\n",
        encoding="utf-8",
    )
    original = (project / "tests" / "test_calc.py").read_text(encoding="utf-8")
    rewritten = _diff(
        "tests/test_calc.py", original, original.replace("add(7, 1), 8", "add(7, 1), 6")
    )
    report = _dynamic(project, rewritten)
    assert report.verdict == "tampered"
    kinds = {(finding.kind, finding.severity) for finding in report.findings}
    assert ("expectation_changed", "weakening") in kinds
    assert ("original_fails_on_change", "weakening") in kinds
    assert (project / "tests" / "test_calc.py").read_text(encoding="utf-8") == original


# -- shapes seen in BugsInPy developer fixes (experiment_results/tamper_check_developer_fixes) --
def test_parametrizing_a_test_is_not_a_removal() -> None:
    """httpie#1: three asserts became one parametrized assert with more cases."""
    old = textwrap.dedent(
        """\
        def test_unique():
            assert 'a' == unique('a', 0)
            assert 'a-1' == unique('a', 1)
            assert 'a-10' == unique('a', 10)
        """
    )
    new = textwrap.dedent(
        """\
        @pytest.mark.parametrize('name, n, expected', [('a', 0, 'a'), ('a', 1, 'a-1'), ('a', 10, 'a-10'), ('b', 0, 'b')])
        def test_unique(name, n, expected):
            assert expected == unique(name, n)
        """
    )
    findings = compare_test_file("tests/test_d.py", old, new, source_changed=True)
    assert _kinds(findings) == [("assertions_rewritten", "suspicious")]
    assert "3 → 4 cases" in findings[0].message


def test_assertion_with_a_changed_input_is_paired() -> None:
    """tqdm#8: an assertion's argument changed and a second one was added."""
    old = "def test_meter():\n    assert meter(20, ncols=30) == ' 20%|x'\n"
    new = (
        "def test_meter():\n    assert meter(20, ncols=13) == ' 20%|x'\n"
        "    assert meter(20, ncols=14) == ' 20%|y'\n"
    )
    assert _kinds(compare_test_file("t/test_m.py", old, new, source_changed=True)) == [
        ("assertion_added", "strengthening"),
        ("assertion_changed", "suspicious"),
    ]


def test_data_files_that_never_parsed_are_not_findings() -> None:
    """black#6: tests/data holds deliberately invalid Python."""
    invalid = "async = 1\ndef f(:\n"
    assert (
        compare_test_file("tests/data/x.py", None, invalid, source_changed=True) == []
    )
    assert (
        compare_test_file(
            "tests/data/x.py", invalid, invalid + "\n", source_changed=True
        )
        == []
    )


def test_renamed_and_edited_test_is_compared_not_deleted() -> None:
    """scrapy#10: a test renamed and given a new input and expectation."""
    old = textwrap.dedent(
        """\
        class T:
            def test_location_with_wrong_encoding(self):
                location = u'/a'
                result = redirect(location)
                self.assertEqual('http://x/%C3%83', result.url)
        """
    )
    new = textwrap.dedent(
        """\
        class T:
            def test_utf8_location(self):
                location = u'/a'.encode('utf-8')
                result = redirect(location)
                self.assertEqual('http://x/%C3%A7', result.url)
        """
    )
    assert _kinds(
        compare_test_file("tests/test_r.py", old, new, source_changed=True)
    ) == [
        ("expectation_changed", "suspicious"),
        ("test_renamed", "neutral"),
    ]
    unrelated = new.replace(
        "location = u'/a'.encode('utf-8')\n        result = redirect(location)\n        "
        "self.assertEqual('http://x/%C3%A7', result.url)",
        "self.assertTrue(parse_headers({}))",
    )
    assert _kinds(
        compare_test_file("tests/test_r.py", old, unrelated, source_changed=True)
    ) == [
        ("test_added", "strengthening"),
        ("test_deleted", "weakening"),
    ]
