"""Focus functions: the functions and methods the versions change.

Behaviour Diff traces calls of the functions any version changes. A function is
named by ``<file>::<qualname>`` (``scrapy/utils/datatypes.py::LocalCache.__setitem__``;
nested functions get ``outer.<locals>.inner`` like Python's own ``__qualname__``),
and that name is the same in every version. Its *span* (first decorator line,
``def`` line) differs between versions because patches shift lines, so the spec
each version's observation run receives carries that version's own spans.

A change outside any function (module level, class body) cannot be traced as a
call; it is listed so the report can say so.
"""

import ast
import difflib
from dataclasses import dataclass


@dataclass(frozen=True)
class FunctionSpan:
    """Where one function is defined in one version of a file.

    Fields:
        qualname:   ``Class.method``, ``function`` or ``outer.<locals>.inner``.
        name:       The ``def`` name (the code object's ``co_name``).
        first_line: First decorator line, or the ``def`` line.
        def_line:   The ``def`` line.
        last_line:  The last line of the body.
    """

    qualname: str
    name: str
    first_line: int
    def_line: int
    last_line: int

    def contains(self, line: int) -> bool:
        return self.first_line <= line <= self.last_line

    @property
    def length(self) -> int:
        return self.last_line - self.first_line


@dataclass(frozen=True)
class FileChange:
    """The functions one version changes in one file, and what lies outside them."""

    relative_path: str
    changed_qualnames: tuple[str, ...]
    module_level_lines: tuple[int, ...]


def function_spans(source: str) -> dict[str, FunctionSpan]:
    """Every function and method in ``source`` by qualname (empty if it won't parse)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}
    spans: dict[str, FunctionSpan] = {}
    _collect_spans(tree, "", spans)
    return spans


def _collect_spans(node: ast.AST, prefix: str, spans: dict[str, FunctionSpan]) -> None:
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            qualname = prefix + child.name
            first_line = min(
                [
                    child.lineno,
                    *(decorator.lineno for decorator in child.decorator_list),
                ]
            )
            spans[qualname] = FunctionSpan(
                qualname=qualname,
                name=child.name,
                first_line=first_line,
                def_line=child.lineno,
                last_line=child.end_lineno or child.lineno,
            )
            _collect_spans(child, qualname + ".<locals>.", spans)
        elif isinstance(child, ast.ClassDef):
            _collect_spans(child, prefix + child.name + ".", spans)
        else:
            _collect_spans(child, prefix, spans)


def innermost_function(
    spans: dict[str, FunctionSpan], line: int
) -> FunctionSpan | None:
    """The smallest function span containing ``line``."""
    containing = [span for span in spans.values() if span.contains(line)]
    return min(containing, key=lambda span: span.length) if containing else None


def changed_functions(
    relative_path: str, old_source: str, new_source: str
) -> FileChange:
    """Which functions a change from ``old_source`` to ``new_source`` touches.

    Removed or replaced lines are located in the old version, inserted or
    replacing lines in the new one; each is attributed to its innermost function.
    """
    old_spans = function_spans(old_source)
    new_spans = function_spans(new_source)
    changed: list[str] = []
    module_level: list[int] = []
    matcher = difflib.SequenceMatcher(
        a=old_source.splitlines(), b=new_source.splitlines(), autojunk=False
    )
    for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        if tag == "equal":
            continue
        located = False
        for line in range(old_start + 1, old_end + 1):
            span = innermost_function(old_spans, line)
            located |= _note(span, changed)
        for line in range(new_start + 1, new_end + 1):
            span = innermost_function(new_spans, line)
            located |= _note(span, changed)
        if not located:
            module_level.append(new_start + 1 if new_end > new_start else old_start + 1)
    return FileChange(
        relative_path=relative_path,
        changed_qualnames=tuple(changed),
        module_level_lines=tuple(module_level),
    )


def _note(span: FunctionSpan | None, changed: list[str]) -> bool:
    if span is None:
        return False
    if span.qualname not in changed:
        changed.append(span.qualname)
    return True


def focus_id(relative_path: str, qualname: str) -> str:
    return f"{relative_path}::{qualname}"


def driver_focus_entries(
    focus_ids: list[str], sources: dict[str, str]
) -> list[dict[str, object]]:
    """The driver spec's ``focus`` list for one version.

    ``sources`` maps each relative path to that version's file text. Focus
    functions that this version does not define are left out.
    """
    spans_by_file: dict[str, dict[str, FunctionSpan]] = {}
    entries: list[dict[str, object]] = []
    for identifier in focus_ids:
        relative_path, qualname = identifier.split("::", 1)
        if relative_path not in spans_by_file:
            spans_by_file[relative_path] = function_spans(
                sources.get(relative_path, "")
            )
        span = spans_by_file[relative_path].get(qualname)
        if span is None:
            continue
        entries.append(
            {
                "id": identifier,
                "file": relative_path,
                "name": span.name,
                "first": span.first_line,
                "def": span.def_line,
            }
        )
    return entries
