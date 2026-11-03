"""Fix Grip: hunks, line variants, verdicts, timeouts and restore on termination.

The end-to-end tests run the real driver with this interpreter on small projects
in a temporary directory (``LocalTarget``).
"""

import difflib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from apr_framework.challenge.replay import fingerprint_checkout
from apr_framework.evidence.target import LocalTarget, SuiteCommand
from apr_framework.evidence.versions import FixVersion
from apr_framework.grip.pipeline import GripOptions, run_grip
from apr_framework.grip.report import format_grip
from apr_framework.grip.variants import hunk_variants, hunks_of

BUGGY = """\
LOG = []


def add(a, b):
    note = "add"
    return a - b


def unused(x):
    return x * 2


def total(values):
    return sum(values)
"""

FIXED = """\
LOG = []


def add(a, b):
    LOG.append("add")
    note = "add"
    return a + b


def unused(x):
    return x * 3


def total(values: list) -> int:
    return sum(values)
"""

TESTS = """\
import unittest

from calc import add, total


class CalcTest(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(2, 3), 5)

    def test_total(self):
        self.assertEqual(total([1, 2]), 3)
"""


def _diff(path: str, old: str, new: str) -> str:
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )


def _project(tmp_path: Path, tests: str = TESTS, source: str = BUGGY) -> Path:
    project = tmp_path / "proj"
    (project / "tests").mkdir(parents=True)
    (project / "calc.py").write_text(source, encoding="utf-8")
    (project / "tests" / "__init__.py").write_text("", encoding="utf-8")
    (project / "tests" / "test_calc.py").write_text(tests, encoding="utf-8")
    return project


def test_hunks_mark_annotation_only_blocks_as_non_behavioural() -> None:
    hunks = hunks_of("calc.py", BUGGY, FIXED, start_index=1)
    assert [(hunk.hunk_id, hunk.behavioural) for hunk in hunks] == [
        ("h1", True),
        ("h2", True),
        ("h3", True),
        ("h4", False),
    ]
    assert [variant.variant_id for variant in hunk_variants(hunks)] == [
        "h1",
        "h2",
        "h3",
    ]


def test_grip_verdicts_for_pinned_unchecked_unexecuted_and_non_behavioural(
    tmp_path: Path,
) -> None:
    project = _project(tmp_path)
    clean = fingerprint_checkout(project)
    target = LocalTarget(
        project,
        Path(sys.executable),
        SuiteCommand.parse("python -m unittest tests.test_calc"),
        allow_local_exec=True,
    )
    fix = FixVersion("fix", "candidate", _diff("calc.py", BUGGY, FIXED), "fix.diff")

    result = run_grip(target, fix, GripOptions(max_probes=12, min_variant_timeout=20))

    assert fingerprint_checkout(project) == clean
    verdicts = {hunk.hunk.location: hunk.verdict for hunk in result.hunks}
    assert verdicts == {
        "calc.py:5": "not_pinned",  # LOG.append("add") runs, no test reads LOG
        "calc.py:7": "pinned",  # a + b
        "calc.py:11": "not_executed",  # unused() is never called by a test
        "calc.py:14": "non_behavioural",  # annotations only
    }
    assert result.fixed_by_the_fix == ["tests.test_calc.CalcTest.test_add"]
    pinned = next(hunk for hunk in result.hunks if hunk.verdict == "pinned")
    assert pinned.detected_by == ["tests.test_calc.CalcTest.test_add"]
    missed = [
        variant for variant in result.variants if variant.status == "not_detected"
    ]
    assert missed, "the LOG line and unused() changes should survive some variants"
    assert all(variant.variant.path == "calc.py" for variant in missed)
    report = format_grip(result)
    assert (
        "NOT PINNED" in report
        and "NOT EXECUTED" in report
        and "non-behavioural" in report
    )


def test_grip_cuts_off_variants_that_never_finish(tmp_path: Path) -> None:
    buggy = "def drain(items):\n    out = []\n    while len(items) > 0:\n        out.append(items.pop())\n    return out\n"
    fixed = "def drain(items):\n    out = []\n    while len(items) > 0:\n        out.append(items.pop(0))\n    return out\n"
    tests = (
        "import unittest\n\nfrom calc import drain\n\n\nclass T(unittest.TestCase):\n"
        "    def test_order(self):\n        self.assertEqual(drain([1, 2]), [1, 2])\n"
    )
    project = _project(tmp_path, tests=tests, source=buggy)
    target = LocalTarget(
        project,
        Path(sys.executable),
        SuiteCommand.parse("python -m unittest tests.test_calc"),
        allow_local_exec=True,
    )
    fix = FixVersion("fix", "candidate", _diff("calc.py", buggy, fixed), "fix.diff")
    started = time.monotonic()
    result = run_grip(target, fix, GripOptions(max_probes=6, min_variant_timeout=3))
    statuses = {variant.status for variant in result.variants}
    assert "timeout" in statuses  # dropping `out.append(items.pop(0))` never ends
    assert time.monotonic() - started < 120
    assert (project / "calc.py").read_text(encoding="utf-8") == buggy


def test_termination_mid_run_restores_the_project(tmp_path: Path) -> None:
    marker = tmp_path / "started"
    tests = (
        "import time\nimport unittest\nfrom pathlib import Path\n\nfrom calc import add\n\n\n"
        "class T(unittest.TestCase):\n    def test_slow(self):\n"
        f"        Path({str(marker)!r}).write_text('x')\n"
        "        time.sleep(60)\n        self.assertEqual(add(1, 1), 2)\n"
    )
    project = _project(tmp_path, tests=tests)
    (tmp_path / "fix.diff").write_text(_diff("calc.py", BUGGY, FIXED), encoding="utf-8")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "apr_framework",
            "grip",
            "--repo",
            str(project),
            "--python",
            sys.executable,
            "--allow-local-exec",
            "--tests",
            "python -m unittest tests.test_calc",
            "--patch",
            str(tmp_path / "fix.diff"),
            "--runs-dir",
            str(tmp_path / "runs"),
        ],
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
    )
    try:
        deadline = time.monotonic() + 60
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        assert marker.exists(), "the fix's test run never started"
        # The fix is applied while its tests run: terminate now.
        assert "a + b" in (project / "calc.py").read_text(encoding="utf-8")
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=60)
    finally:
        if process.poll() is None:
            process.kill()
    assert process.returncode == 128 + signal.SIGTERM
    assert (project / "calc.py").read_text(encoding="utf-8") == BUGGY


def test_clause_drops_show_an_unpinned_condition(tmp_path: Path) -> None:
    """A fix adds `and key not in self`; no test sets an existing key at capacity."""
    buggy = (
        "class Cache(dict):\n    def __init__(self, limit=None):\n        super().__init__()\n"
        "        self.limit = limit\n\n    def put(self, key, value):\n"
        "        while len(self) >= self.limit:\n            self.pop(next(iter(self)))\n"
        "        self[key] = value\n"
    )
    fixed = buggy.replace(
        "while len(self) >= self.limit:",
        "while self.limit is not None and len(self) >= self.limit and key not in self:",
    )
    tests = (
        "import unittest\n\nfrom calc import Cache\n\n\nclass T(unittest.TestCase):\n"
        "    def test_unlimited(self):\n        cache = Cache()\n        cache.put('a', 1)\n"
        "        self.assertEqual(cache, {'a': 1})\n\n"
        "    def test_limit(self):\n        cache = Cache(limit=1)\n        cache.put('a', 1)\n"
        "        cache.put('b', 2)\n        self.assertEqual(cache, {'b': 2})\n"
    )
    project = _project(tmp_path, tests=tests, source=buggy)
    target = LocalTarget(
        project,
        Path(sys.executable),
        SuiteCommand.parse("python -m unittest tests.test_calc"),
        allow_local_exec=True,
    )
    fix = FixVersion("fix", "candidate", _diff("calc.py", buggy, fixed), "fix.diff")
    result = run_grip(target, fix, GripOptions(max_probes=0, min_variant_timeout=10))
    clauses = {
        variant.variant.after: variant.status
        for variant in result.variants
        if variant.variant.family == "clause"
    }
    assert clauses == {
        "while len(self) >= self.limit and key not in self:": "detected",
        "while self.limit is not None and key not in self:": "detected",
        "while self.limit is not None and len(self) >= self.limit:": "not_detected",
    }


def test_trigger_runs_alone_when_the_suite_order_breaks_it(tmp_path: Path) -> None:
    """black#1: the failing test passes alone but fails inside its whole file."""
    buggy = "STATE = {'broken': False}\n\n\ndef add(a, b):\n    if STATE['broken']:\n        return None\n    return a - b\n"
    fixed = buggy.replace("return a - b", "return a + b")
    tests = (
        "import unittest\n\nimport calc\n\n\nclass T(unittest.TestCase):\n"
        "    def test_a_breaks_state(self):\n        calc.STATE['broken'] = True\n"
        "        self.assertIsNone(calc.add(1, 1))\n\n"
        "    def test_b_add(self):\n        self.assertEqual(calc.add(2, 3), 5)\n"
    )
    project = _project(tmp_path, tests=tests, source=buggy)
    target = LocalTarget(
        project,
        Path(sys.executable),
        SuiteCommand.parse("python -m unittest tests.test_calc"),
        allow_local_exec=True,
        trigger_tests=(
            SuiteCommand.parse("python -m unittest tests.test_calc.T.test_b_add"),
        ),
    )
    fix = FixVersion("fix", "candidate", _diff("calc.py", buggy, fixed), "fix.diff")
    result = run_grip(target, fix, GripOptions(max_probes=0, min_variant_timeout=10))
    assert result.trigger_with_fix == {"tests.test_calc.T.test_b_add": "passed"}
    assert result.trigger_fails_in_suite == ["tests.test_calc.T.test_b_add"]
    assert result.fixed_by_the_fix == ["tests.test_calc.T.test_b_add (alone)"]
    [hunk] = result.hunks
    assert hunk.verdict == "pinned"
    assert hunk.detected_by == ["tests.test_calc.T.test_b_add (alone)"]
    assert "passes alone but fails inside its whole test file" in format_grip(result)
