"""Unit checks for the challenge package (no Docker, no BugsInPy)."""

import json
from pathlib import Path

import pytest

from apr_framework.challenge.bundle import (
    CaseBundle,
    find_case_problems,
    list_cases,
    load_case,
)
from apr_framework.challenge.multi_file_patch import (
    MultiFilePatch,
    PatchApplyError,
    without_evaluator_files,
)
from apr_framework.challenge.replay import (
    VariantObservation,
    classify_witness_outcome,
    find_inconsistent_repeats,
    fingerprint_checkout,
)
from apr_framework.core.exceptions import ConfigurationError
from apr_framework.repair.evaluator_integrity import classify_evaluator_path

MULTI_FILE_DIFF = """\
diff --git a/pkg/core.py b/pkg/core.py
--- a/pkg/core.py
+++ b/pkg/core.py
@@ -1,3 +1,3 @@
 def limit(value):
-    return value
+    return min(value, 10)
 # end
diff --git a/pkg/sub/new_module.py b/pkg/sub/new_module.py
new file mode 100644
--- /dev/null
+++ b/pkg/sub/new_module.py
@@ -0,0 +1,2 @@
+def helper():
+    return 1
diff --git a/pkg/old.py b/pkg/old.py
deleted file mode 100644
--- a/pkg/old.py
+++ /dev/null
@@ -1,2 +0,0 @@
-x = 1
-y = 2
diff --git a/tests/test_core.py b/tests/test_core.py
--- a/tests/test_core.py
+++ b/tests/test_core.py
@@ -1 +1 @@
-assert limit(20) == 10
+assert limit(20) == 20
"""


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / "pkg").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "pkg" / "core.py").write_text(
        "def limit(value):\n    return value\n# end\n"
    )
    (root / "pkg" / "old.py").write_text("x = 1\ny = 2\n")
    (root / "tests" / "test_core.py").write_text("assert limit(20) == 10\n")
    (root / "crlf.py").write_bytes(b"a = 1\r\nb = 2\r\n")
    return root


def test_multi_file_patch_applies_and_restores_exactly(worktree: Path) -> None:
    before = fingerprint_checkout(worktree)
    patch = MultiFilePatch.plan(MULTI_FILE_DIFF, worktree)
    patch.apply()

    assert "min(value, 10)" in (worktree / "pkg" / "core.py").read_text()
    assert (worktree / "pkg" / "sub" / "new_module.py").is_file()
    assert not (worktree / "pkg" / "old.py").exists()

    patch.restore()
    assert fingerprint_checkout(worktree) == before
    assert not (worktree / "pkg" / "sub").exists()


def test_restore_after_a_failed_apply_leaves_no_residue(
    worktree: Path, monkeypatch
) -> None:
    before = fingerprint_checkout(worktree)
    patch = MultiFilePatch.plan(MULTI_FILE_DIFF, worktree)
    original_write_bytes = Path.write_bytes
    write_count = {"n": 0}

    def fail_on_third_write(self, data):
        write_count["n"] += 1
        if write_count["n"] == 3:
            raise OSError("disk full")
        return original_write_bytes(self, data)

    monkeypatch.setattr(Path, "write_bytes", fail_on_third_write)
    with pytest.raises(OSError):
        try:
            patch.apply()
        finally:
            monkeypatch.setattr(Path, "write_bytes", original_write_bytes)
            patch.restore()
    assert fingerprint_checkout(worktree) == before


def test_crlf_files_keep_their_line_endings(worktree: Path) -> None:
    diff = "--- a/crlf.py\n+++ b/crlf.py\n@@ -1,2 +1,2 @@\n a = 1\n-b = 2\n+b = 3\n"
    patch = MultiFilePatch.plan(diff, worktree)
    patch.apply()
    assert (worktree / "crlf.py").read_bytes() == b"a = 1\r\nb = 3\r\n"
    patch.restore()
    assert (worktree / "crlf.py").read_bytes() == b"a = 1\r\nb = 2\r\n"


@pytest.mark.parametrize(
    "diff_text",
    [
        "--- /dev/null\n+++ b/../escape.py\n@@ -0,0 +1 @@\n+x = 1\n",
        "--- /dev/null\n+++ /etc/escape.py\n@@ -0,0 +1 @@\n+x = 1\n",
        "--- a/pkg/core.py\n+++ b/pkg/core.py\n@@ -1 +1 @@\n-not in file\n+x\n",
        "--- /dev/null\n+++ b/pkg/core.py\n@@ -0,0 +1 @@\n+exists already\n",
        "--- a/pkg/old.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x = 1\n",
    ],
)
def test_bad_diffs_are_refused_before_anything_is_written(
    worktree: Path, diff_text: str
) -> None:
    before = fingerprint_checkout(worktree)
    with pytest.raises(PatchApplyError):
        MultiFilePatch.plan(diff_text, worktree)
    assert fingerprint_checkout(worktree) == before


def test_without_evaluator_files_keeps_only_source_sections(worktree: Path) -> None:
    source_only = without_evaluator_files(
        MULTI_FILE_DIFF,
        "project",
        lambda path: classify_evaluator_path(path) is not None,
    )
    patch = MultiFilePatch.plan(source_only, worktree)
    assert sorted(patch.touched_paths) == [
        "pkg/core.py",
        "pkg/old.py",
        "pkg/sub/new_module.py",
    ]


def test_absolute_workspace_headers_map_to_the_checkout(worktree: Path) -> None:
    diff = (
        "--- /workspace/.workspace/bugsinpy/project_1/project/pkg/core.py\n"
        "+++ /workspace/.workspace/bugsinpy/project_1/project/pkg/core.py\n"
        "@@ -2 +2 @@\n-    return value\n+    return 0\n"
    )
    assert MultiFilePatch.plan(diff, worktree).touched_paths == ("pkg/core.py",)


def _write_case(root: Path, case_id: str, **overrides) -> Path:
    case_directory = root / "challenges" / "cases" / case_id
    case_directory.mkdir(parents=True)
    (case_directory / "candidate.diff").write_text(SIMPLE_DIFF)
    (case_directory / "NOTES.md").write_text("why\n")
    data = {
        "id": case_id,
        "version": 1,
        "status": "candidate",
        "type": "T",
        "project": "black",
        "bug_id": 3,
        "subject_license": "MIT",
        "promise": "p",
        "contract_basis": [],
        "recorded_test_scope": {},
        "candidate": {"diff": "candidate.diff", "provenance": {"agent": "a"}},
        "expected": {
            "buggy": {"trigger": "fail"},
            "candidate": {"trigger": "fail"},
            "candidate_as_submitted": {"trigger": "pass"},
            "developer_fixed": {"trigger": "pass"},
        },
        "replay_log": [],
        "reviewers": [],
        "dispute_history": [],
    }
    data.update(overrides)
    (case_directory / "case.json").write_text(json.dumps(data))
    return case_directory


SIMPLE_DIFF = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n"


def test_well_formed_case_loads_and_lists(tmp_path: Path) -> None:
    _write_case(tmp_path, "demo-case")
    bundle = load_case("demo-case", tmp_path)
    assert bundle.variants[2] == "candidate_as_submitted"
    assert [case.case_id for case in list_cases(tmp_path)] == ["demo-case"]


@pytest.mark.parametrize(
    "overrides, problem",
    [
        ({"status": "verified"}, "status"),
        ({"type": "C"}, "witness"),
        ({"expected": {"buggy": {}}}, "expected results missing"),
        ({"id": "other"}, "differs from directory"),
    ],
)
def test_malformed_cases_are_refused(
    tmp_path: Path, overrides: dict, problem: str
) -> None:
    case_directory = _write_case(tmp_path, "demo-case", **overrides)
    bundle = CaseBundle(
        directory=case_directory,
        data=json.loads((case_directory / "case.json").read_text()),
    )
    assert any(problem in message for message in find_case_problems(bundle))
    with pytest.raises(ConfigurationError):
        load_case("demo-case", tmp_path)


def _observation(variant: str, witness: str | None, repeat: int = 1):
    return VariantObservation(
        variant=variant,
        repeat=repeat,
        trigger="pass",
        regression="ok",
        witness=witness,
        integrity="ok",
    )


@pytest.mark.parametrize(
    "buggy, candidate, developer_fixed, expected_start",
    [
        ("fail", "fail", "pass", "residual bug"),
        ("pass", "fail", "pass", "new regression"),
        ("fail", "fail", "fail", "invalid witness"),
        ("fail", "pass", "pass", "the candidate passes"),
    ],
)
def test_witness_classification_separates_residual_bugs_from_regressions(
    buggy: str, candidate: str, developer_fixed: str, expected_start: str
) -> None:
    observations = [
        _observation("buggy", buggy),
        _observation("candidate", candidate),
        _observation("developer_fixed", developer_fixed),
    ]
    assert classify_witness_outcome(observations).startswith(expected_start)


def test_disagreeing_repeats_are_reported() -> None:
    observations = [
        _observation("candidate", "fail", repeat=1),
        _observation("candidate", "pass", repeat=2),
    ]
    assert find_inconsistent_repeats(observations)
