"""What the generator may see of the project: an answer-free source view.

The retrieval tools (``repair/llm/retrieval_tools.py``) walk every ``.py`` file of
the worktree they are given. A BugsInPy checkout of the buggy revision already
contains the developer's new test, so pointing the tools at the checkout would
leak the trigger test. Instead the generator gets a **copy** holding only the
Python files it may see:

- BugsInPy mode: every source file except evaluator files (tests, fixtures,
  ``bugsinpy_*``, test configuration). The developer fix itself is never in the
  checkout (it is the buggy revision).
- Local mode: every Python file except the ``.apr/`` directory and environments.

Test *style* is shown separately: the names of the test files the generator may
know about and the opening lines of one of them, never a declared test file or a
test file the developer fix touched.
"""

import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from apr_framework.core.models import CheckoutResult
from apr_framework.repair.evaluator_integrity import (
    classify_evaluator_path,
    list_checkout_files,
)

LOCAL_REPRO_DIRECTORY = ".apr"
_STYLE_EXCERPT_LINES = 40
_MAX_LISTED_FILES = 120


@dataclass(frozen=True)
class SourceView:
    """The copy the retrieval tools see, plus the layout and style hints.

    Fields:
        checkout:        A ``CheckoutResult`` whose worktree is the copy.
        source_files:    Relative paths of the copied Python files.
        test_file_names: Test files the generator may know by name.
        style_excerpt:   Opening lines of one visible test file ("" if none).
        hidden_files:    Relative paths kept from the generator.
    """

    checkout: CheckoutResult
    source_files: tuple[str, ...]
    test_file_names: tuple[str, ...]
    style_excerpt: str
    hidden_files: tuple[str, ...]

    def describe_layout(self) -> str:
        listed = list(self.source_files[:_MAX_LISTED_FILES])
        if len(self.source_files) > _MAX_LISTED_FILES:
            listed.append(f"... ({len(self.source_files) - _MAX_LISTED_FILES} more)")
        return "\n".join(listed)


def build_bugsinpy_source_view(
    checkout: CheckoutResult,
    declared_test_files: frozenset[str],
    fix_touched_paths: frozenset[str],
    destination: Path,
) -> SourceView:
    """Answer-free view of a BugsInPy checkout (no evaluator file, no fixed test)."""
    hidden_test_files = declared_test_files | {
        path
        for path in fix_touched_paths
        if classify_evaluator_path(path, declared_test_files) is not None
    }

    def is_visible_source(relative_path: str) -> bool:
        return classify_evaluator_path(relative_path, declared_test_files) is None

    def is_nameable_test(relative_path: str) -> bool:
        return (
            relative_path not in hidden_test_files
            and is_test_module(relative_path)
            and classify_evaluator_path(relative_path, declared_test_files) is not None
        )

    return build_source_view(
        checkout, destination, is_visible_source, is_nameable_test, hidden_test_files
    )


def build_local_source_view(checkout: CheckoutResult, destination: Path) -> SourceView:
    """View of a local project: everything but ``.apr/`` (its own repro tests)."""

    def is_visible_source(relative_path: str) -> bool:
        return not relative_path.startswith(LOCAL_REPRO_DIRECTORY + "/")

    def is_nameable_test(relative_path: str) -> bool:
        return is_visible_source(relative_path) and is_test_module(relative_path)

    return build_source_view(
        checkout, destination, is_visible_source, is_nameable_test, frozenset()
    )


def build_source_view(
    checkout: CheckoutResult,
    destination: Path,
    is_visible_source: Callable[[str], bool],
    is_nameable_test: Callable[[str], bool],
    hidden_files: frozenset[str],
) -> SourceView:
    """Copy the visible Python files to ``destination`` and collect the hints."""
    python_files = [
        relative_path
        for relative_path in list_checkout_files(checkout.worktree)
        if relative_path.endswith(".py")
    ]
    visible_sources = [path for path in python_files if is_visible_source(path)]
    copy_files(checkout.worktree, destination, visible_sources)
    test_file_names = tuple(path for path in python_files if is_nameable_test(path))
    return SourceView(
        checkout=CheckoutResult(
            bug=checkout.bug, worktree=destination, success=True, prepared=True
        ),
        source_files=tuple(visible_sources),
        test_file_names=test_file_names,
        style_excerpt=read_style_excerpt(checkout.worktree, test_file_names),
        hidden_files=tuple(sorted(hidden_files)),
    )


def copy_files(source_root: Path, destination: Path, relative_paths: list[str]) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    for relative_path in relative_paths:
        target = destination / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_root / relative_path, target)


def read_style_excerpt(worktree: Path, test_file_names: tuple[str, ...]) -> str:
    """The opening lines of the largest nameable test file (imports, first tests)."""
    if not test_file_names:
        return ""
    sample = max(test_file_names, key=lambda path: (worktree / path).stat().st_size)
    lines = (worktree / sample).read_text("utf-8", errors="replace").splitlines()
    return f"# {sample}\n" + "\n".join(lines[:_STYLE_EXCERPT_LINES])


def is_test_module(relative_path: str) -> bool:
    name = PurePosixPath(relative_path).name
    return name.endswith(".py") and (
        name.startswith("test") or name.endswith("_test.py")
    )
