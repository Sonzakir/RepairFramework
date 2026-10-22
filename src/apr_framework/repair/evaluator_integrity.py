"""Evaluator integrity: keep candidate patches away from the files that judge them.

A patch is judged by the bug's tests, their fixtures, the test-runner configuration
and the BugsInPy scripts that run them. Together these are the **evaluator files**.
A candidate that edits one of them is judged by a different evaluator than the one
the bug was recorded with, so its "plausible" verdict means nothing. In the
project's own history, all four "plausible" black#3 patches (``run_349``) only
rewrote an expected exit code in ``tests/test_black.py``.

This module provides the three guards the validation path applies:

1. **Screen before applying** (``EvaluatorGuard.screen_candidate``). A candidate
   whose diff (or explicit target file) touches an evaluator file is *rejected*,
   never silently dropped: rejections are evidence of test tampering. Source edits
   that detect the test runner (``PYTEST_CURRENT_TEST``, ``"pytest" in
   sys.modules``, ...) are rejected the same way.
2. **Lock and verify** (``EvaluatorGuard.verify_evaluator_files``). The evaluator
   files are snapshotted once per checkout. After every validation their hashes
   are compared with the snapshot; any change is restored from the snapshot and
   reported as ``evaluator_files_mutated``.
3. **Never target them** (``EvaluatorGuard.is_evaluator_file``). Repair engines
   skip evaluator files when choosing where to edit.

What counts as an evaluator file (paths relative to the checkout):

- the test files named by ``bugsinpy_run_test.sh`` and ``test_file`` in
  ``bugsinpy_bug.info``;
- anything under a ``tests/`` or ``test/`` directory, and any ``test_*.py`` or
  ``*_test.py`` module;
- ``conftest.py`` anywhere;
- pytest / tox configuration: ``pytest.ini``, ``tox.ini``, ``setup.cfg``, and
  ``pyproject.toml`` when it carries a ``[tool.pytest...]`` section;
- the BugsInPy scripts and metadata (``bugsinpy_*``) and the pinned
  ``.python-version``;
- the test environment itself (the ``env/`` virtualenv BugsInPy builds);
- newly created modules that would shadow the runner or the standard library
  (``sitecustomize.py``, ``pytest.py``, ``*.pth``, ``asyncio.py``, ...);
- any path outside the checkout.

The guard does not sandbox runtime behaviour: a patch that writes elsewhere at
run time is caught only if it leaves a changed evaluator file behind.
"""

import hashlib
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Iterable

from apr_framework.benchmarks.executor_view import remove_host_file
from apr_framework.core.exceptions import APRFrameworkError
from apr_framework.repair.unified_diff import (
    normalise_diff_path,
    parse_unified_diff,
    touched_relative_paths,
)

logger = logging.getLogger(__name__)

INTEGRITY_OK = "ok"
INTEGRITY_EVALUATOR_FILE_EDIT = "evaluator_file_edit"
INTEGRITY_TEST_ENVIRONMENT_DETECTION = "test_environment_detection"
INTEGRITY_EVALUATOR_FILES_MUTATED = "evaluator_files_mutated"

_TEST_DIRECTORY_NAMES = frozenset({"tests", "test"})
_TEST_CONFIG_FILE_NAMES = frozenset(
    {"pytest.ini", "tox.ini", "setup.cfg", ".python-version"}
)
_ENVIRONMENT_DIRECTORY_NAMES = frozenset({"env", ".venv", "venv"})
# Output files bugsinpy-test rewrites on every run; they are not evaluator inputs.
_BUGSINPY_OUTPUT_FILE_NAMES = frozenset(
    {"bugsinpy_fail.txt", "bugsinpy_alltest.txt", "bugsinpy_singletest.txt"}
)
_SKIPPED_WALK_DIRECTORY_NAMES = frozenset(
    {".git", "__pycache__", ".pytest_cache", ".tox", ".eggs", ".mypy_cache"}
    | _ENVIRONMENT_DIRECTORY_NAMES
)
_RUNNER_MODULE_NAMES = frozenset(
    {"sitecustomize", "usercustomize", "pytest", "_pytest", "py", "pluggy"}
)
_STDLIB_MODULE_NAMES = frozenset(getattr(sys, "stdlib_module_names", ()))
_SYMLINK_MARKER = b"\0symlink\0"
# Added source lines that make behaviour depend on whether a test runner is active.
_TEST_ENVIRONMENT_DETECTION = re.compile(
    r"PYTEST_|\b_?pytest\b|py\.test|bugsinpy|sys\.modules[^\n]*unittest",
    re.IGNORECASE,
)


class EvaluatorIntegrityError(APRFrameworkError):
    """Evaluator files changed and could not be restored to their snapshot."""


@dataclass(frozen=True)
class IntegrityVerdict:
    """Outcome of one integrity check.

    Fields:
        integrity: ``ok`` or one of the ``INTEGRITY_*`` rejection labels.
        paths:     The checkout-relative paths that caused the rejection.
        detail:    Human-readable reason, used in validation summaries.
    """

    integrity: str = INTEGRITY_OK
    paths: tuple[str, ...] = ()
    detail: str = ""

    @property
    def is_rejected(self) -> bool:
        return self.integrity != INTEGRITY_OK

    def as_metadata(self) -> dict[str, object]:
        """Return the structured flag stored on the candidate and in reports."""
        return {"integrity": self.integrity, "integrity_paths": list(self.paths)}


@dataclass
class EvaluatorFileSnapshot:
    """Evaluator file bytes at snapshot time, plus every checkout path that existed.

    Fields:
        contents:  Bytes of each locked evaluator file, keyed by relative path.
        all_paths: Every checkout file present at snapshot time; a file absent
                   from it was created later and is removed on restore.
    """

    contents: dict[str, bytes] = field(default_factory=dict)
    all_paths: frozenset[str] = frozenset()
    digests: dict[str, str] = field(init=False)

    def __post_init__(self) -> None:
        self.digests = {path: _sha256(data) for path, data in self.contents.items()}


class EvaluatorGuard:
    """Screens candidates against, and locks, one checkout's evaluator files.

    Build one per checkout with :meth:`for_worktree` and reuse it for every
    candidate validated there; the snapshot is taken once, on construction.
    """

    def __init__(self, worktree: Path, declared_test_files: frozenset[str]) -> None:
        self._worktree = worktree.resolve()
        self._declared_test_files = declared_test_files
        self._snapshot = self._take_snapshot()

    @classmethod
    def for_worktree(cls, worktree: Path) -> "EvaluatorGuard":
        return cls(worktree, read_declared_test_files(worktree))

    @property
    def worktree(self) -> Path:
        return self._worktree

    @property
    def declared_test_files(self) -> frozenset[str]:
        return self._declared_test_files

    @property
    def snapshot(self) -> EvaluatorFileSnapshot:
        return self._snapshot

    # ------------------------------------------------------------------
    # Guard 1: screen a candidate before it is applied
    # ------------------------------------------------------------------

    def screen_candidate(
        self, diff_text: str, extra_touched_paths: tuple[Path | str, ...] = ()
    ) -> IntegrityVerdict:
        """Reject a candidate that edits evaluator files or detects the runner."""
        evaluator_edit_verdict = self._find_evaluator_edits(
            diff_text, extra_touched_paths
        )
        if evaluator_edit_verdict.is_rejected:
            return evaluator_edit_verdict
        return self._find_test_environment_detection(diff_text)

    def is_evaluator_file(self, path: Path | str) -> bool:
        """Report whether an absolute or checkout-relative path is an evaluator file."""
        relative_path = self.relative_path(path)
        return self._classify_path(relative_path, is_creation=False) is not None

    def relative_path(self, path: Path | str) -> str:
        """Return ``path`` relative to the checkout, or ``..``-prefixed if outside it."""
        candidate = Path(path)
        if not candidate.is_absolute():
            return normalise_diff_path(str(path), self._worktree.name)
        try:
            return candidate.resolve().relative_to(self._worktree).as_posix()
        except ValueError:
            normalised = normalise_diff_path(str(path), self._worktree.name)
            return normalised if not normalised.startswith("/") else f"../{normalised}"

    # ------------------------------------------------------------------
    # Guard 2: verify the locked evaluator files after a validation
    # ------------------------------------------------------------------

    def verify_evaluator_files(self) -> IntegrityVerdict:
        """Compare evaluator files with the snapshot; restore and report any change.

        Raises:
            EvaluatorIntegrityError: if the restored files still differ from the
                snapshot, so no further validation on this checkout can be trusted.
        """
        mutated_paths = self._find_mutated_paths()
        if not mutated_paths:
            return IntegrityVerdict()

        logger.error(
            "Evaluator files changed during validation: %s — restoring snapshot",
            mutated_paths,
        )
        self._restore_paths(mutated_paths)
        still_mutated_paths = self._find_mutated_paths()
        if still_mutated_paths:
            raise EvaluatorIntegrityError(
                "Evaluator files could not be restored to their snapshot: "
                f"{still_mutated_paths}. Re-checkout the bug before validating again."
            )
        return IntegrityVerdict(
            integrity=INTEGRITY_EVALUATOR_FILES_MUTATED,
            paths=tuple(mutated_paths),
            detail=(
                "evaluator files changed while the candidate was validated "
                f"(restored from snapshot): {', '.join(mutated_paths)}"
            ),
        )

    def assert_evaluator_files_unchanged(self) -> None:
        """Raise if evaluator files differ from the snapshot before a validation."""
        mutated_paths = self._find_mutated_paths()
        if mutated_paths:
            raise EvaluatorIntegrityError(
                "Evaluator files changed outside validation: "
                f"{mutated_paths}. Re-checkout the bug before validating."
            )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _find_evaluator_edits(
        self, diff_text: str, extra_touched_paths: tuple[Path | str, ...]
    ) -> IntegrityVerdict:
        created_paths = self._created_relative_paths(diff_text)
        pytest_mentioning_paths = self._pytest_mentioning_relative_paths(diff_text)
        touched_paths = touched_relative_paths(diff_text, self._worktree.name)
        for extra_path in extra_touched_paths:
            relative_extra_path = self.relative_path(extra_path)
            if relative_extra_path not in touched_paths:
                touched_paths.append(relative_extra_path)

        reasons = {
            path: reason
            for path in touched_paths
            if (
                reason := self._classify_path(
                    path,
                    is_creation=path in created_paths,
                    mentions_pytest=path in pytest_mentioning_paths,
                )
            )
        }
        if not reasons:
            return IntegrityVerdict()
        return IntegrityVerdict(
            integrity=INTEGRITY_EVALUATOR_FILE_EDIT,
            paths=tuple(reasons),
            detail="candidate edits evaluator files: "
            + ", ".join(f"{path} ({reason})" for path, reason in reasons.items()),
        )

    def _find_test_environment_detection(self, diff_text: str) -> IntegrityVerdict:
        # Count mentions the diff *introduces*: whole-file template diffs re-add
        # every reformatted line, so pre-existing mentions cancel out.
        flagged_paths: list[str] = []
        for file_diff in parse_unified_diff(diff_text):
            if _count_runner_mentions(file_diff.added_lines) > _count_runner_mentions(
                file_diff.removed_lines
            ):
                flagged_paths.append(
                    normalise_diff_path(file_diff.target_path, self._worktree.name)
                )
        if not flagged_paths:
            return IntegrityVerdict()
        return IntegrityVerdict(
            integrity=INTEGRITY_TEST_ENVIRONMENT_DETECTION,
            paths=tuple(flagged_paths),
            detail=(
                "candidate source refers to the test runner (pytest / unittest / "
                f"BugsInPy) in added lines: {', '.join(flagged_paths)}"
            ),
        )

    def _created_relative_paths(self, diff_text: str) -> set[str]:
        return {
            normalise_diff_path(file_diff.new_path, self._worktree.name)
            for file_diff in parse_unified_diff(diff_text)
            if file_diff.is_creation
        }

    def _pytest_mentioning_relative_paths(self, diff_text: str) -> set[str]:
        return {
            normalise_diff_path(file_diff.target_path, self._worktree.name)
            for file_diff in parse_unified_diff(diff_text)
            if any(
                "pytest" in line
                for line in file_diff.added_lines + file_diff.removed_lines
            )
        }

    def _classify_path(
        self, relative_path: str, is_creation: bool, mentions_pytest: bool = False
    ) -> str | None:
        """Return why ``relative_path`` is an evaluator file, or None if it is not."""
        has_pytest_config = PurePosixPath(relative_path).name == "pyproject.toml" and (
            mentions_pytest or self._pyproject_has_pytest_config(relative_path)
        )
        return classify_evaluator_path(
            relative_path,
            self._declared_test_files,
            is_creation=is_creation,
            has_pytest_config=has_pytest_config,
        )

    def _pyproject_has_pytest_config(self, relative_path: str) -> bool:
        try:
            return b"[tool.pytest" in (self._worktree / relative_path).read_bytes()
        except OSError:
            return False

    def _take_snapshot(self) -> EvaluatorFileSnapshot:
        all_paths = self._walk_checkout_files()
        contents = {
            relative_path: read_checkout_entry(self._worktree / relative_path) or b""
            for relative_path in all_paths
            if self._is_lockable_evaluator_file(relative_path)
        }
        logger.info(
            "Locked %d evaluator file(s) in %s", len(contents), self._worktree.name
        )
        return EvaluatorFileSnapshot(contents=contents, all_paths=frozenset(all_paths))

    def _walk_checkout_files(self) -> list[str]:
        return list_checkout_files(self._worktree)

    def _is_lockable_evaluator_file(self, relative_path: str) -> bool:
        # Every pyproject.toml is locked: a run-time edit could add a pytest
        # section, and only a locked file can be restored byte for byte.
        if PurePosixPath(relative_path).name == "pyproject.toml":
            return True
        return self._classify_path(relative_path, is_creation=False) is not None

    def _find_mutated_paths(self) -> list[str]:
        """Return evaluator paths changed, deleted or newly created since the snapshot."""
        snapshot_digests = self._snapshot.digests
        mutated_paths = [
            path
            for path, digest in snapshot_digests.items()
            if (current_entry := read_checkout_entry(self._worktree / path)) is None
            or _sha256(current_entry) != digest
        ]
        mutated_paths.extend(
            path
            for path in self._walk_checkout_files()
            if path not in self._snapshot.all_paths
            and _is_code_or_config(path)
            and self._classify_path(path, is_creation=True) is not None
        )
        return sorted(mutated_paths)

    def _restore_paths(self, relative_paths: list[str]) -> None:
        for relative_path in relative_paths:
            absolute_path = self._worktree / relative_path
            original_bytes = self._snapshot.contents.get(relative_path)
            if original_bytes is None:
                if relative_path not in self._snapshot.all_paths:
                    remove_host_file(absolute_path)
                continue
            write_checkout_entry(absolute_path, original_bytes)
        purge_bytecode_caches(
            self._worktree / relative_path for relative_path in relative_paths
        )


def purge_bytecode_caches(source_paths: Iterable[Path]) -> None:
    """Delete the cached bytecode of each source file.

    Python reuses ``__pycache__/<name>.*.pyc`` when the source's mtime (in whole
    seconds) and size match what the cache recorded. A patch that keeps a file's
    size (``exit_code=2`` → ``1``) and is restored within the same second leaves a
    cache that still matches, so the *next* run silently executes the patched
    code. Deleting the caches after every apply and restore rules that out.
    """
    for source_path in source_paths:
        if source_path.suffix != ".py":
            continue
        cache_directory = source_path.parent / "__pycache__"
        stale_caches = [source_path.with_suffix(".pyc")]
        if cache_directory.is_dir():
            stale_caches.extend(cache_directory.glob(f"{source_path.stem}.*.pyc"))
        for stale_cache in stale_caches:
            if stale_cache.exists():
                remove_host_file(stale_cache)


def read_checkout_entry(path: Path) -> bytes | None:
    """Return a file's bytes, a symlink's target (never followed), or None if absent.

    Checkouts contain symlinks, some dangling (black's ``docs/authors.md``); a
    symlink is recorded as its target so it is compared and restored as a link.
    """
    if path.is_symlink():
        return _SYMLINK_MARKER + os.readlink(path).encode("utf-8", "surrogateescape")
    if path.is_file():
        return path.read_bytes()
    return None


def write_checkout_entry(path: Path, entry: bytes) -> None:
    """Restore what ``read_checkout_entry`` returned: a regular file or a symlink.

    An existing symlink is removed first, so restoring never writes *through* a
    link into a file outside the checkout.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        path.unlink()
    if entry.startswith(_SYMLINK_MARKER):
        path.unlink(missing_ok=True)
        os.symlink(
            entry[len(_SYMLINK_MARKER) :].decode("utf-8", "surrogateescape"), path
        )
        return
    path.write_bytes(entry)


def list_checkout_files(worktree: Path) -> list[str]:
    """Return every checkout file except VCS, cache and environment trees.

    Also skips the output files ``bugsinpy-test`` rewrites on every run and
    compiled ``.pyc`` files, so two listings of an untouched checkout are equal.
    """
    worktree = worktree.resolve()
    checkout_files: list[str] = []
    for directory, sub_directories, file_names in os.walk(worktree):
        sub_directories[:] = [
            name
            for name in sub_directories
            if name not in _SKIPPED_WALK_DIRECTORY_NAMES
            and not name.endswith(".egg-info")
        ]
        for file_name in file_names:
            if file_name in _BUGSINPY_OUTPUT_FILE_NAMES or file_name.endswith(".pyc"):
                continue
            checkout_files.append(
                (Path(directory) / file_name).relative_to(worktree).as_posix()
            )
    return sorted(checkout_files)


def classify_evaluator_path(
    relative_path: str,
    declared_test_files: frozenset[str] = frozenset(),
    *,
    is_creation: bool = False,
    has_pytest_config: bool = False,
) -> str | None:
    """Return why a checkout-relative path is an evaluator file, or None if not.

    Pure path rules, usable without a checkout (the history audit uses them).
    ``has_pytest_config`` says whether a ``pyproject.toml`` carries (or the diff
    edits) a pytest section; ``is_creation`` enables the module-shadowing rule.
    """
    posix_path = PurePosixPath(relative_path)
    file_name = posix_path.name
    directory_names = set(posix_path.parts[:-1])

    if relative_path.startswith(("..", "/")):
        return "outside the checkout"
    if ".git" in posix_path.parts:
        return "version-control metadata"
    if relative_path in declared_test_files:
        return "declared test file"
    if directory_names & _ENVIRONMENT_DIRECTORY_NAMES:
        return "test environment"
    if directory_names & _TEST_DIRECTORY_NAMES:
        return "test directory"
    if file_name == "conftest.py":
        return "pytest fixture file"
    if file_name.endswith(".py") and (
        file_name.startswith("test_") or file_name.endswith("_test.py")
    ):
        return "test module"
    if file_name in _TEST_CONFIG_FILE_NAMES:
        return "test configuration"
    if file_name == "pyproject.toml" and has_pytest_config:
        return "test configuration"
    if file_name.startswith("bugsinpy_"):
        return "BugsInPy script"
    if is_creation and _is_shadowing_module(file_name):
        return "shadows a runner or standard-library module"
    return None


def find_recorded_patch_evaluator_edits(
    patch_payload: dict,
    project: str,
    declared_test_files: frozenset[str] = frozenset(),
) -> dict[str, str]:
    """Map each evaluator file a *recorded* patch touches to the rule that matched.

    Works on a patch entry of a committed ``repair_results.json`` (no checkout
    needed): reads the diff headers and, for template patches whose headers carry
    only the file's base name, the recorded ``metadata.source_path``.
    """
    touched_paths: dict[str, bool] = {}
    pytest_config_paths: set[str] = set()
    for file_diff in parse_unified_diff(patch_payload.get("diff_text") or ""):
        relative_path = normalise_diff_path(file_diff.target_path, project)
        touched_paths[relative_path] = file_diff.is_creation
        if any(
            "pytest" in line for line in file_diff.added_lines + file_diff.removed_lines
        ):
            pytest_config_paths.add(relative_path)
    source_path = (patch_payload.get("metadata") or {}).get("source_path")
    if source_path:
        touched_paths.setdefault(normalise_diff_path(source_path, project), False)

    return {
        path: reason
        for path, is_creation in touched_paths.items()
        if (
            reason := classify_evaluator_path(
                path,
                declared_test_files,
                is_creation=is_creation,
                has_pytest_config=path in pytest_config_paths,
            )
        )
    }


def read_declared_test_files(worktree: Path) -> frozenset[str]:
    """Return the test files a BugsInPy checkout names as its evaluator.

    Reads pytest node ids and unittest dotted names from ``bugsinpy_run_test.sh``
    and the ``test_file`` entry of ``bugsinpy_bug.info``. Unittest dotted names
    are resolved to the longest prefix that exists as a ``.py`` file.
    """
    declared: set[str] = set()
    declared.update(_test_files_from_bug_info(worktree))
    declared.update(_test_files_from_run_test_script(worktree))
    return frozenset(declared)


def _test_files_from_bug_info(worktree: Path) -> list[str]:
    bug_info_path = worktree / "bugsinpy_bug.info"
    if not bug_info_path.is_file():
        return []
    match = re.search(r'test_file="([^"]+)"', bug_info_path.read_text(encoding="utf-8"))
    if not match:
        return []
    return [path.strip() for path in match.group(1).split(";") if path.strip()]


def _test_files_from_run_test_script(worktree: Path) -> list[str]:
    script_path = worktree / "bugsinpy_run_test.sh"
    if not script_path.is_file():
        return []
    test_files: list[str] = []
    for token in script_path.read_text(encoding="utf-8").split():
        if token.startswith("-"):
            continue
        file_part = token.split("::", 1)[0]
        if file_part.endswith(".py"):
            test_files.append(file_part)
            continue
        dotted_file = _resolve_dotted_test_name(worktree, token)
        if dotted_file is not None:
            test_files.append(dotted_file)
    return test_files


def _resolve_dotted_test_name(worktree: Path, dotted_name: str) -> str | None:
    """Map ``tests.test_black.Case.test_x`` to ``tests/test_black.py`` if it exists."""
    if not re.fullmatch(r"[A-Za-z_][\w.]*", dotted_name):
        return None
    parts = dotted_name.split(".")
    for length in range(len(parts), 0, -1):
        candidate = "/".join(parts[:length]) + ".py"
        if (worktree / candidate).is_file():
            return candidate
    return None


def _is_shadowing_module(file_name: str) -> bool:
    if file_name.endswith(".pth"):
        return True
    if not file_name.endswith(".py"):
        return False
    module_name = file_name.removesuffix(".py")
    return module_name in _RUNNER_MODULE_NAMES or module_name in _STDLIB_MODULE_NAMES


def _count_runner_mentions(lines: list[str]) -> int:
    return sum(len(_TEST_ENVIRONMENT_DETECTION.findall(line)) for line in lines)


def _is_code_or_config(relative_path: str) -> bool:
    """New files that can change test behaviour (tests writing data files can't)."""
    return relative_path.endswith((".py", ".ini", ".cfg", ".toml", ".pth")) or (
        PurePosixPath(relative_path).name.startswith("bugsinpy_")
    )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
