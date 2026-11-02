"""Versions of a project: the buggy code, a reference fix, candidate fixes.

A version is a diff against the target's checkout (the buggy version's diff is
empty). Candidates come from patch files or from earlier runs: the plausible
patches a ``repair`` run recorded in ``repair_results.json``, or the diff a
``challenge agent-run`` captured in ``patches/agent.diff``.

Two candidates that leave every file with the same bytes are one version, however
their diff headers differ, so duplicates are found by applying-in-memory
(``MultiFilePatch.plan``) rather than by comparing diff text.

The evidence commands compare *source* behaviour, so sections of a candidate that
edit evaluator files (tests, fixtures, test configuration) are stripped and listed;
what a candidate did to the tests is Tamper Check's question.
"""

import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

from apr_framework.challenge.multi_file_patch import (
    MultiFilePatch,
    PatchApplyError,
    render_file_diff,
)
from apr_framework.core.exceptions import ConfigurationError
from apr_framework.repair.evaluator_integrity import (
    classify_evaluator_path,
    purge_bytecode_caches,
)
from apr_framework.repair.unified_diff import normalise_diff_path, parse_unified_diff

BUGGY = "buggy"
REFERENCE = "reference"
CANDIDATE = "candidate"
BUGGY_LABEL = "buggy"
REFERENCE_LABEL = "developer-fix"


@dataclass(frozen=True)
class FixVersion:
    """One version of the project, as a diff against the buggy checkout.

    Fields:
        label:          Short unique name used in reports (``run_173:llm-1-0``).
        kind:           ``buggy``, ``reference`` or ``candidate``.
        diff_text:      The diff to apply (empty for the buggy version).
        source:         Where the diff came from (a path, a run and patch id).
        stripped_paths: Evaluator files whose sections were removed from the diff.
    """

    label: str
    kind: str
    diff_text: str
    source: str
    stripped_paths: tuple[str, ...] = ()

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.diff_text.encode("utf-8")).hexdigest()


def buggy_version() -> FixVersion:
    return FixVersion(
        label=BUGGY_LABEL, kind=BUGGY, diff_text="", source="the checkout"
    )


def reference_version(diff_text: str, source: str) -> FixVersion:
    return FixVersion(
        label=REFERENCE_LABEL, kind=REFERENCE, diff_text=diff_text, source=source
    )


def load_patch_file(path: Path) -> FixVersion:
    """A candidate from a diff file; its label is the file name without suffix."""
    if not path.is_file():
        raise ConfigurationError(f"No such patch file: {path}")
    return FixVersion(
        label=path.stem,
        kind=CANDIDATE,
        diff_text=path.read_text(encoding="utf-8"),
        source=str(path),
    )


def load_run_candidates(run_directory: Path) -> list[FixVersion]:
    """The candidates an earlier run produced.

    - a ``repair`` run: every entry of ``plausible_patches`` in
      ``repair_results.json`` (they all passed the trigger test and the
      regression check when they were recorded);
    - a ``challenge agent-run``: the captured ``patches/agent.diff``.
    """
    if not run_directory.is_dir():
        raise ConfigurationError(f"No such run directory: {run_directory}")
    results_path = run_directory / "repair_results.json"
    agent_diff_path = run_directory / "patches" / "agent.diff"
    candidates: list[FixVersion] = []
    if results_path.is_file():
        results = json.loads(results_path.read_text(encoding="utf-8"))
        for patch in results.get("plausible_patches") or []:
            diff_text = patch.get("diff_text") or ""
            if not diff_text.strip():
                continue
            candidates.append(
                FixVersion(
                    label=f"{run_directory.name}:{patch.get('patch_id', 'patch')}",
                    kind=CANDIDATE,
                    diff_text=diff_text,
                    source=f"{results_path} ({patch.get('patch_id')})",
                )
            )
    if not candidates and agent_diff_path.is_file():
        diff_text = agent_diff_path.read_text(encoding="utf-8")
        if diff_text.strip():
            candidates.append(
                FixVersion(
                    label=f"{run_directory.name}:agent",
                    kind=CANDIDATE,
                    diff_text=diff_text,
                    source=str(agent_diff_path),
                )
            )
    if not candidates:
        raise ConfigurationError(
            f"{run_directory} has no plausible patches (repair_results.json) and no "
            "captured agent diff (patches/agent.diff)."
        )
    return candidates


def strip_evaluator_edits(
    version: FixVersion, worktree: Path, declared_test_files: frozenset[str]
) -> FixVersion:
    """Remove the sections that edit tests, fixtures or test configuration."""
    kept_sections: list[str] = []
    stripped: list[str] = []
    for file_diff in parse_unified_diff(version.diff_text):
        relative_path = normalise_diff_path(file_diff.target_path, worktree.name)
        has_pytest_config = relative_path.endswith(
            "pyproject.toml"
        ) and _has_pytest_section(worktree / relative_path)
        if (
            classify_evaluator_path(
                relative_path,
                declared_test_files,
                is_creation=file_diff.is_creation,
                has_pytest_config=has_pytest_config,
            )
            is not None
        ):
            stripped.append(relative_path)
            continue
        kept_sections.append(render_file_diff(file_diff, worktree.name))
    if not stripped:
        return version
    return replace(
        version, diff_text="".join(kept_sections), stripped_paths=tuple(stripped)
    )


def content_key(version: FixVersion, worktree: Path) -> str | None:
    """A hash of every file's content after applying the version (None: won't apply)."""
    try:
        patch = MultiFilePatch.plan(version.diff_text, worktree)
    except PatchApplyError:
        return None
    digest = hashlib.sha256()
    for change in sorted(patch.changes, key=lambda change: change.relative_path):
        digest.update(change.relative_path.encode("utf-8") + b"\0")
        digest.update(b"<deleted>" if change.new_bytes is None else change.new_bytes)
        digest.update(b"\0")
    return digest.hexdigest()


def deduplicate(
    versions: list[FixVersion], worktree: Path
) -> tuple[list[FixVersion], dict[str, str]]:
    """Keep the first version of each distinct content; map duplicates to it.

    Versions that do not apply are kept (they are reported as not applied).
    """
    kept: list[FixVersion] = []
    duplicates: dict[str, str] = {}
    first_label_by_key: dict[str, str] = {}
    for version in versions:
        key = content_key(version, worktree)
        if key is not None and key in first_label_by_key:
            duplicates[version.label] = first_label_by_key[key]
            continue
        if key is not None:
            first_label_by_key[key] = version.label
        kept.append(version)
    return kept, duplicates


def unique_labels(versions: list[FixVersion]) -> list[FixVersion]:
    """Suffix repeated labels (two ``fix.diff`` files from different folders)."""
    seen: dict[str, int] = {}
    unique: list[FixVersion] = []
    for version in versions:
        count = seen.get(version.label, 0)
        seen[version.label] = count + 1
        unique.append(
            version
            if count == 0
            else replace(version, label=f"{version.label}~{count + 1}")
        )
    return unique


@contextmanager
def applied(version: FixVersion, worktree: Path) -> Iterator[MultiFilePatch]:
    """Apply a version, purge stale bytecode, and always restore it exactly.

    Raises:
        PatchApplyError: The diff does not apply (nothing was written).
    """
    patch = MultiFilePatch.plan(version.diff_text, worktree)
    touched_files = [worktree / relative_path for relative_path in patch.touched_paths]
    try:
        patch.apply()
        purge_bytecode_caches(touched_files)
        yield patch
    finally:
        patch.restore()
        purge_bytecode_caches(touched_files)


def _has_pytest_section(path: Path) -> bool:
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8", errors="replace")
    return "[tool.pytest" in text
