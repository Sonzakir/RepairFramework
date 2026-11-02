"""Behaviour Diff: suite parsing, focus, amplification, driver, comparison, end to end.

The end-to-end tests run the real driver with this interpreter on small projects
written to a temporary directory (``LocalTarget``), for both unittest and pytest
suites.
"""

import ast
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from apr_framework.behaviour import driver
from apr_framework.behaviour.amplify import (
    ReachingTest,
    augment_source,
    literal_sites,
    plan_variants,
    variant_of_test_id,
)
from apr_framework.behaviour.compare import (
    behaviour_classes,
    diverge,
    unstable_tests,
)
from apr_framework.behaviour.focus import changed_functions, function_spans
from apr_framework.behaviour.observe import Observation, RecordedTest
from apr_framework.behaviour.pipeline import BehaviourDiffOptions, run_behaviour_diff
from apr_framework.behaviour.report import format_leads, format_summary
from apr_framework.challenge.replay import fingerprint_checkout
from apr_framework.core.exceptions import ConfigurationError
from apr_framework.evidence.target import LocalTarget, SuiteCommand
from apr_framework.evidence.versions import (
    REFERENCE,
    FixVersion,
    deduplicate,
    load_run_candidates,
    strip_evaluator_edits,
)

CACHE_SOURCE = '''\
import collections


class LocalCache(collections.OrderedDict):
    """Dictionary with a finite number of keys; older keys are evicted first."""

    def __init__(self, limit=None):
        super().__init__()
        self.limit = limit

    def __setitem__(self, key, value):
        while len(self) >= self.limit:
            self.popitem(last=False)
        super().__setitem__(key, value)
'''

UNITTEST_TESTS = """\
import unittest

from cache import LocalCache


class LocalCacheTest(unittest.TestCase):
    def test_cache_with_limit(self):
        cache = LocalCache(limit=2)
        cache["a"] = 1
        cache["b"] = 2
        cache["c"] = 3
        self.assertEqual(len(cache), 2)
        self.assertNotIn("a", cache)

    def test_cache_without_limit(self):
        cache = LocalCache()
        for x in range(50):
            cache[str(x)] = x
        self.assertEqual(len(cache), 50)
"""

PYTEST_TESTS = """\
from cache import LocalCache


def test_cache_with_limit():
    cache = LocalCache(limit=2)
    cache["a"] = 1
    cache["b"] = 2
    cache["c"] = 3
    assert len(cache) == 2
    assert "a" not in cache


def test_cache_without_limit():
    cache = LocalCache()
    for x in range(50):
        cache[str(x)] = x
    assert len(cache) == 50
"""

FIXES = {
    "developer": (
        "        while len(self) >= self.limit:\n            self.popitem(last=False)\n",
        (
            "        if self.limit:\n            while len(self) >= self.limit:\n"
            "                self.popitem(last=False)\n"
        ),
    ),
    "is_not_none": (
        "while len(self) >= self.limit:",
        "while self.limit is not None and len(self) >= self.limit:",
    ),
    "key_not_in": (
        "        while len(self) >= self.limit:\n            self.popitem(last=False)\n",
        (
            "        if self.limit is not None:\n"
            "            while len(self) >= self.limit and key not in self:\n"
            "                self.popitem(last=False)\n"
        ),
    ),
    "infinity": (
        "        self.limit = limit\n",
        "        self.limit = limit if limit is not None else float('inf')\n",
    ),
}


def _diff(relative_path: str, old: str, new: str) -> str:
    import difflib

    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{relative_path}",
            tofile=f"b/{relative_path}",
        )
    )


def _fix(name: str) -> str:
    old, new = FIXES[name]
    return _diff("cache.py", CACHE_SOURCE, CACHE_SOURCE.replace(old, new, 1))


def _project(tmp_path: Path, tests_source: str) -> Path:
    project = tmp_path / "proj"
    (project / "tests").mkdir(parents=True)
    (project / "cache.py").write_text(CACHE_SOURCE, encoding="utf-8")
    (project / "tests" / "__init__.py").write_text("", encoding="utf-8")
    (project / "tests" / "test_cache.py").write_text(tests_source, encoding="utf-8")
    return project


def _candidate(label: str, diff_text: str) -> FixVersion:
    return FixVersion(label=label, kind="candidate", diff_text=diff_text, source=label)


# -- suite commands ---------------------------------------------------------------
@pytest.mark.parametrize(
    ("command", "runner", "arguments"),
    [
        ("pytest -rfE -q tests/test_x.py", "pytest", ("-rfE", "-q", "tests/test_x.py")),
        ("python -m pytest tests", "pytest", ("tests",)),
        ("/venv/bin/python3.8 -m unittest -q a.b", "unittest", ("-q", "a.b")),
    ],
)
def test_suite_command_parses_runner_and_arguments(command, runner, arguments) -> None:
    suite = SuiteCommand.parse(command)
    assert (suite.runner, suite.arguments) == (runner, arguments)


def test_suite_command_refuses_other_runners() -> None:
    with pytest.raises(ConfigurationError, match="test runner"):
        SuiteCommand.parse("tox -e py38")


def test_suite_command_selects_tests_by_runner_rules() -> None:
    unittest_suite = SuiteCommand.parse(
        "python -m unittest -q tests.test_x.Case.test_a"
    )
    assert unittest_suite.selects("tests.test_x.Case.test_a")
    assert not unittest_suite.selects("tests.test_x.Case.test_ab")
    pytest_suite = SuiteCommand.parse("pytest tests/test_x.py::test_a")
    assert pytest_suite.selects("tests/test_x.py::test_a[1-2]")
    assert not pytest_suite.selects("tests/test_x.py::test_b")
    assert SuiteCommand.parse("pytest tests").selects("tests/test_x.py::test_b")


# -- focus --------------------------------------------------------------------------
def test_function_spans_cover_methods_nested_functions_and_decorators() -> None:
    source = textwrap.dedent(
        """\
        class A:
            @staticmethod
            def f():
                def inner():
                    return 1
                return inner()
        """
    )
    spans = function_spans(source)
    assert spans["A.f"].first_line == 2 and spans["A.f"].def_line == 3
    assert spans["A.f.<locals>.inner"].def_line == 4


def test_changed_functions_attributes_changes_and_module_level_lines() -> None:
    old = "X = 1\n\n\ndef f():\n    return X\n\n\ndef g():\n    return 2\n"
    new = "X = 2\n\n\ndef f():\n    return X + 1\n\n\ndef g():\n    return 2\n"
    change = changed_functions("m.py", old, new)
    assert change.changed_qualnames == ("f",)
    assert change.module_level_lines == (1,)


# -- amplification --------------------------------------------------------------------
def test_literal_sites_skip_docstrings_assertions_and_fstrings() -> None:
    source = textwrap.dedent(
        '''\
        def test_x(self):
            """Doc."""
            value = make(2, "k")
            label = f"n={value}"
            assert value == 3
            self.assertEqual(value, 4)
        '''
    )
    function = ast.parse(source).body[0]
    sites = literal_sites(function, source.splitlines(keepends=True))
    assert [site.source for site in sites] == ["2", '"k"']


def test_plan_variants_round_robins_and_respects_caps() -> None:
    reaching = [
        ReachingTest(
            "tests.test_cache.LocalCacheTest.test_cache_with_limit",
            "t.py",
            "LocalCacheTest.test_cache_with_limit",
        ),
        ReachingTest(
            "tests.test_cache.LocalCacheTest.test_cache_without_limit",
            "t.py",
            "LocalCacheTest.test_cache_without_limit",
        ),
    ]
    variants = plan_variants(reaching, {"t.py": UNITTEST_TESTS}, per_test=3, total=5)
    assert [variant.variant_id for variant in variants] == [
        "v01",
        "v02",
        "v03",
        "v04",
        "v05",
    ]
    assert [variant.qualname.split(".")[1] for variant in variants[:2]] == [
        "test_cache_with_limit",
        "test_cache_without_limit",
    ]
    assert (variants[0].original, variants[0].replacement) == ("2", "0")
    # String literals swap with another literal of the same test before "".
    first_string = next(variant for variant in variants if variant.original == '"a"')
    assert first_string.replacement == "'b'"


def test_augment_source_inserts_renamed_variant_after_the_original() -> None:
    reaching = [ReachingTest("x", "t.py", "LocalCacheTest.test_cache_with_limit")]
    variant = plan_variants(reaching, {"t.py": UNITTEST_TESTS}, per_test=1, total=1)[0]
    augmented = augment_source(UNITTEST_TESTS, [variant])
    tree = ast.parse(augmented)
    names = [node.name for node in tree.body[2].body]
    assert names == [
        "test_cache_with_limit",
        "test_cache_with_limit__bdiff_v01",
        "test_cache_without_limit",
    ]
    assert "LocalCache(limit=0)" in augmented
    assert augmented.count("LocalCache(limit=2)") == 1


def test_variant_ids_follow_each_runner() -> None:
    reaching = [ReachingTest("pkg.t.C.test_a", "t.py", "C.test_a")]
    source = "class C:\n    def test_a(self):\n        f(1)\n"
    variant = plan_variants(reaching, {"t.py": source}, per_test=1, total=1)[0]
    assert variant.test_id_for("unittest") == "pkg.t.C.test_a__bdiff_v01"
    pytest_variant = plan_variants(
        [ReachingTest("t.py::C::test_a[1]", "t.py", "C.test_a")],
        {"t.py": source},
        per_test=1,
        total=1,
    )[0]
    assert pytest_variant.test_id_for("pytest") == "t.py::C::test_a__bdiff_v01"
    assert (
        variant_of_test_id("t.py::C::test_a__bdiff_v01[1]", {"v01": variant}) is variant
    )


# -- driver rendering ---------------------------------------------------------------
def test_renderer_is_run_independent() -> None:
    renderer = driver.Renderer(max_length=60)

    class Plain:
        def __init__(self):
            self.b = {3, 1, 2}
            self.a = [1, "x"]

    assert renderer.render(Plain()) == "<Plain> {'a': [1, 'x'], 'b': set{1, 2, 3}}"
    assert (
        renderer.render(
            object.__new__(
                type("Q", (), {"__repr__": lambda s: "<Q at 0x7f3a2b1c9d10>"})
            )
        )
        == "<Q at 0x?>"
    )
    long_text = renderer.render("y" * 500)
    assert long_text.startswith("'yyyy") and "crc" in long_text and len(long_text) < 120


# -- comparison ---------------------------------------------------------------------
def _observation(label: str, tests: dict[str, tuple]) -> Observation:
    records = {
        test_id: RecordedTest(
            test_id=test_id, outcome=outcome, detail=detail, calls=calls
        )
        for test_id, (outcome, detail, calls) in tests.items()
    }
    return Observation(
        label=label, status="observed", tests=records, focus_ids=("m.py::f",)
    )


def test_compare_finds_unstable_tests_divergences_and_classes() -> None:
    call = {"fn": "m.py::f", "args": [["x", "1"]], "result": "1", "raised": None}
    other = {**call, "result": "2"}
    buggy = _observation(
        "buggy", {"t1": ("passed", "", [call]), "t2": ("passed", "", [call])}
    )
    rerun = _observation(
        "buggy#2", {"t1": ("passed", "", [call]), "t2": ("passed", "", [other])}
    )
    fix_a = _observation(
        "a",
        {
            "t1": ("failed", "AssertionError: 1 != 2", [call]),
            "t2": ("passed", "", [call]),
        },
    )
    fix_b = _observation(
        "b", {"t1": ("passed", "", [other]), "t2": ("passed", "", [call])}
    )
    fix_c = _observation(
        "c", {"t1": ("passed", "", [other]), "t2": ("passed", "", [other])}
    )
    focus = frozenset({"m.py::f"})
    unstable = unstable_tests(buggy, rerun, focus)
    assert unstable == ["t2"]
    divergences = diverge(buggy, fix_a, ["t1"], focus, {})
    assert [(d.kind, d.left, d.right) for d in divergences] == [
        ("outcome", "passed", "failed: AssertionError: 1 != 2")
    ]
    call_divergence = diverge(buggy, fix_b, ["t1"], focus, {})[0]
    assert call_divergence.kind == "call"
    assert call_divergence.call == "f(x=1)"
    assert (call_divergence.left, call_divergence.right) == ("returned 1", "returned 2")
    classes = behaviour_classes([buggy, fix_a, fix_b, fix_c], ["t1"], focus)
    assert classes == [["buggy"], ["a"], ["b", "c"]]


# -- versions -----------------------------------------------------------------------
def test_strip_evaluator_edits_and_deduplicate_by_content(tmp_path: Path) -> None:
    project = _project(tmp_path, UNITTEST_TESTS)
    test_edit = _diff(
        "tests/test_cache.py", UNITTEST_TESTS, UNITTEST_TESTS.replace("2)", "3)")
    )
    tampered = _candidate("tampered", _fix("developer") + test_edit)
    stripped = strip_evaluator_edits(tampered, project, frozenset())
    assert stripped.stripped_paths == ("tests/test_cache.py",)
    assert "tests/test_cache.py" not in stripped.diff_text
    absolute_headers = _fix("developer").replace(
        "a/cache.py", "/somewhere/proj/cache.py"
    )
    kept, duplicates = deduplicate(
        [
            _candidate("first", _fix("developer")),
            stripped,
            _candidate("abs", absolute_headers),
        ],
        project,
    )
    assert [version.label for version in kept] == ["first"]
    assert duplicates == {"tampered": "first", "abs": "first"}


def test_load_run_candidates_reads_plausible_patches_and_agent_diffs(
    tmp_path: Path,
) -> None:
    repair_run = tmp_path / "run_001"
    repair_run.mkdir()
    (repair_run / "repair_results.json").write_text(
        json.dumps(
            {
                "plausible_patches": [
                    {"patch_id": "llm-1-0", "diff_text": _fix("developer")}
                ]
            }
        ),
        encoding="utf-8",
    )
    agent_run = tmp_path / "run_002"
    (agent_run / "patches").mkdir(parents=True)
    (agent_run / "patches" / "agent.diff").write_text(
        _fix("infinity"), encoding="utf-8"
    )
    assert [version.label for version in load_run_candidates(repair_run)] == [
        "run_001:llm-1-0"
    ]
    assert [version.label for version in load_run_candidates(agent_run)] == [
        "run_002:agent"
    ]
    with pytest.raises(ConfigurationError):
        load_run_candidates(tmp_path / "missing")


def test_local_target_requires_explicit_permission(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="allow-local-exec"):
        LocalTarget(
            tmp_path,
            Path(sys.executable),
            SuiteCommand.parse("python -m pytest"),
            allow_local_exec=False,
        )


# -- end to end ---------------------------------------------------------------------
@pytest.mark.parametrize(
    ("tests_source", "suite"),
    [
        (UNITTEST_TESTS, "python -m unittest tests.test_cache"),
        (PYTEST_TESTS, "python -m pytest tests/test_cache.py"),
    ],
)
def test_behaviour_diff_separates_fixes_that_all_pass_the_tests(
    tmp_path: Path, tests_source: str, suite: str
) -> None:
    project = _project(tmp_path, tests_source)
    clean = fingerprint_checkout(project)
    target = LocalTarget(
        project, Path(sys.executable), SuiteCommand.parse(suite), allow_local_exec=True
    )
    reference = FixVersion(
        label="reference", kind=REFERENCE, diff_text=_fix("developer"), source="dev"
    )
    candidates = [
        _candidate(name, _fix(name))
        for name in ("is_not_none", "key_not_in", "infinity")
    ] + [_candidate("same_as_reference", _fix("developer"))]

    result = run_behaviour_diff(
        target, candidates, reference, BehaviourDiffOptions(timeout_seconds=120)
    )

    assert fingerprint_checkout(project) == clean
    assert result.unstable_tests == []
    assert result.common_focus == [
        "cache.py::LocalCache.__init__",
        "cache.py::LocalCache.__setitem__",
    ]
    summaries = {version.label: version for version in result.versions}
    assert summaries["reference"].duplicates == ["same_as_reference"]
    # Every version passes the original tests except the buggy one ...
    for label in ("reference", "is_not_none", "key_not_in", "infinity"):
        originals = [
            record
            for record in next(
                o for o in result.observations if o.label == label
            ).tests.values()
            if "__bdiff_" not in record.test_id
        ]
        assert {record.outcome for record in originals} == {"passed"}
    # ... yet they fall into four different behaviour classes.
    classes = [sorted(behaviour_class["members"]) for behaviour_class in result.classes]
    assert sorted(classes) == [
        ["buggy"],
        ["infinity"],
        ["is_not_none"],
        ["key_not_in"],
        ["reference"],
    ]

    by_candidate = {
        comparison.right: comparison for comparison in result.reference_comparisons
    }
    limit_zero = [
        divergence
        for divergence in by_candidate["is_not_none"].divergences
        if divergence.kind == "outcome"
    ]
    assert limit_zero and "KeyError" in limit_zero[0].right
    assert "KeyError" not in limit_zero[0].left
    collision = [
        divergence
        for divergence in by_candidate["key_not_in"].divergences
        if divergence.variant_id and "'a'" in divergence.right
    ]
    assert collision, "re-setting an existing key at capacity should tell them apart"
    infinity_calls = [
        divergence
        for divergence in by_candidate["infinity"].divergences
        if divergence.kind == "call"
    ]
    assert any("'limit': inf" in divergence.right for divergence in infinity_calls)

    summary = format_summary(result)
    assert "Behaviour classes" in summary and "it is not a verdict" in summary
    assert "not** a Break This Fix case" in format_leads(result)


def test_behaviour_diff_reports_versions_that_break_collection(tmp_path: Path) -> None:
    project = _project(tmp_path, PYTEST_TESTS)
    target = LocalTarget(
        project,
        Path(sys.executable),
        SuiteCommand.parse("python -m pytest tests/test_cache.py"),
        allow_local_exec=True,
    )
    broken = _diff(
        "cache.py",
        CACHE_SOURCE,
        CACHE_SOURCE.replace(
            "import collections\n", "import collections\nimport missing_module_xyz\n"
        ).replace("self.limit = limit", "self.limit = limit or 0"),
    )
    result = run_behaviour_diff(
        target,
        [_candidate("broken", broken)],
        None,
        BehaviourDiffOptions(timeout_seconds=120, max_variants=0),
    )
    comparison = result.buggy_comparisons[0]
    assert any(
        divergence.test_id.startswith("<collection>")
        and "missing_module_xyz" in divergence.right
        for divergence in comparison.divergences
    )


def test_driver_runs_standalone_and_pins_the_hash_seed(tmp_path: Path) -> None:
    project = _project(tmp_path, PYTEST_TESTS)
    spec = {
        "runner": "pytest",
        "arguments": ["tests/test_cache.py"],
        "focus": [
            {
                "id": "cache.py::h",
                "file": "cache.py",
                "name": "nothing",
                "first": 1,
                "def": 1,
            }
        ],
        "output": str(tmp_path / "out.json"),
    }
    (tmp_path / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
    probe = project / "tests" / "test_seed.py"
    probe.write_text(
        "import os\n\ndef test_seed():\n    assert os.environ['PYTHONHASHSEED'] == '0'\n",
        encoding="utf-8",
    )
    completed = subprocess.run(
        [sys.executable, str(Path(driver.__file__)), str(tmp_path / "spec.json")],
        cwd=project,
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": "/usr/bin:/bin"},
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    outcomes = {
        test["id"]: (test["outcome"], test["detail"]) for test in payload["tests"]
    }
    assert outcomes["tests/test_cache.py::test_cache_with_limit"] == ("passed", "")
    without_limit = outcomes["tests/test_cache.py::test_cache_without_limit"]
    assert without_limit[0] == "failed" and without_limit[1].startswith("TypeError")
    spec["arguments"] = ["tests/test_seed.py"]
    (tmp_path / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
    subprocess.run(
        [sys.executable, str(Path(driver.__file__)), str(tmp_path / "spec.json")],
        cwd=project,
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": "/usr/bin:/bin"},
    )
    payload = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    assert [test["outcome"] for test in payload["tests"]] == ["passed"]


def test_driver_records_generators_and_coroutines_once(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    (project / "tests").mkdir(parents=True)
    (project / "gen.py").write_text(
        "import asyncio\n\n\ndef numbers(n):\n    for i in range(n):\n        yield i\n\n\n"
        "async def double(x):\n    await asyncio.sleep(0)\n    await asyncio.sleep(0)\n    return 2 * x\n",
        encoding="utf-8",
    )
    (project / "tests" / "test_gen.py").write_text(
        "import asyncio\n\nfrom gen import double, numbers\n\n\n"
        "def test_numbers():\n    assert list(numbers(3)) == [0, 1, 2]\n\n\n"
        "def test_double():\n    assert asyncio.get_event_loop().run_until_complete(double(2)) == 4\n",
        encoding="utf-8",
    )
    spec = {
        "runner": "pytest",
        "arguments": ["tests/test_gen.py"],
        "focus": [
            {
                "id": "gen.py::numbers",
                "file": "gen.py",
                "name": "numbers",
                "first": 4,
                "def": 4,
            },
            {
                "id": "gen.py::double",
                "file": "gen.py",
                "name": "double",
                "first": 9,
                "def": 9,
            },
        ],
        "output": str(tmp_path / "out.json"),
    }
    (tmp_path / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
    subprocess.run(
        [sys.executable, str(Path(driver.__file__)), str(tmp_path / "spec.json")],
        cwd=project,
        capture_output=True,
        check=True,
    )
    tests = {
        test["id"].rsplit("::", 1)[1]: test
        for test in json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))[
            "tests"
        ]
    }
    for name, arguments in (
        ("test_numbers", [["n", "3"]]),
        ("test_double", [["x", "2"]]),
    ):
        assert [call["args"] for call in tests[name]["calls"]] == [arguments]
        assert tests[name]["calls"][0]["result"] == "<generator or coroutine>"
