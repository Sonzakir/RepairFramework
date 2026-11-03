"""Variants of a fix: one hunk reverted, or one changed line mutated.

- **Hunk reverts.** For every change block (``repro.probes.change_blocks``) of
  every source file the fix changes: the fix with that block put back to the buggy
  code. A single-block fix's revert is the buggy code itself, which tells whether
  the tests notice the fix at all.
- **Clause drops.** One operand of an ``and``/``or`` on a changed line removed.
- **Line probes.** Strict Repro's deterministic probes on the lines the fix adds
  or replaces: one template-engine AST operator, or the statement replaced by
  ``pass`` (``repro.probes.build_operator_and_drop_probes``), sampled with the same
  seeded round-robin (``select_probes``).

A block whose revert leaves the file's syntax tree unchanged once annotations and
docstrings are ignored is *non-behavioural* (black#1's ``executor: Executor`` →
``Optional[Executor]``): reverting it cannot change what any test observes, so it
is not run.
"""

import ast
import difflib
from dataclasses import dataclass

from apr_framework.repro.probes import (
    build_operator_and_drop_probes,
    change_blocks,
    lines_changed_by_fix,
    number_probes,
    revert_change_block,
    select_probes,
)


@dataclass(frozen=True)
class Hunk:
    """One change block of the fix.

    Fields:
        hunk_id:        ``h1``, ``h2``, ... in file and line order.
        path:           The file.
        fixed_lines:    1-based lines the block occupies in the fixed file (empty
                        for a pure deletion).
        original_lines: 1-based lines it replaced in the buggy file.
        behavioural:    False when only annotations or docstrings differ.
        before/after:   The block's buggy and fixed text (shortened).
        reverted_text:  The fixed file with this block reverted.
    """

    hunk_id: str
    path: str
    fixed_lines: tuple[int, ...]
    original_lines: tuple[int, ...]
    behavioural: bool
    before: str
    after: str
    reverted_text: str

    @property
    def location(self) -> str:
        lines = self.fixed_lines or self.original_lines
        if not lines:
            return self.path
        span = f"{lines[0]}" if len(lines) == 1 else f"{lines[0]}-{lines[-1]}"
        return f"{self.path}:{span}"


@dataclass(frozen=True)
class Variant:
    """A version of the fix with one change: a hunk revert or a line probe.

    Fields:
        variant_id: The hunk id (``h1``) or the probe id (``p01``).
        family:     ``revert``, ``operator`` or ``drop``.
        kind:       ``revert``, the AST operator key, or ``drop``.
        path/line:  Where it changes the fix.
        before/after: The fixed line and the variant's line.
        file_text:  The variant's text of ``path`` (other files stay as fixed).
        hunk_id:    The hunk the change lies in.
    """

    variant_id: str
    family: str
    kind: str
    path: str
    line: int
    before: str
    after: str
    file_text: str
    hunk_id: str | None


def hunks_of(
    path: str, original_text: str, fixed_text: str, start_index: int
) -> list[Hunk]:
    hunks: list[Hunk] = []
    original_lines = original_text.splitlines()
    fixed_lines = fixed_text.splitlines()
    for offset, block in enumerate(change_blocks(original_text, fixed_text)):
        _, original_start, original_end, fixed_start, fixed_end = block
        reverted = revert_change_block(original_text, fixed_text, block)
        hunks.append(
            Hunk(
                hunk_id=f"h{start_index + offset}",
                path=path,
                fixed_lines=tuple(range(fixed_start + 1, fixed_end + 1)),
                original_lines=tuple(range(original_start + 1, original_end + 1)),
                behavioural=not _same_behaviour(fixed_text, reverted),
                before="\n".join(original_lines[original_start:original_end])[:400],
                after="\n".join(fixed_lines[fixed_start:fixed_end])[:400],
                reverted_text=reverted,
            )
        )
    return hunks


def hunk_variants(hunks: list[Hunk]) -> list[Variant]:
    return [
        Variant(
            variant_id=hunk.hunk_id,
            family="revert",
            kind="revert",
            path=hunk.path,
            line=(hunk.fixed_lines or hunk.original_lines or (0,))[0],
            before=hunk.after.splitlines()[0] if hunk.after else "",
            after=hunk.before.splitlines()[0] if hunk.before else "",
            file_text=hunk.reverted_text,
            hunk_id=hunk.hunk_id,
        )
        for hunk in hunks
        if hunk.behavioural
    ]


def line_probe_variants(
    python_files: dict[str, tuple[str, str]],
    hunks: list[Hunk],
    *,
    max_probes: int,
    seed: str,
) -> list[Variant]:
    probes = []
    for path, (original_text, fixed_text) in python_files.items():
        probes.extend(build_operator_and_drop_probes(path, original_text, fixed_text))
    chosen = number_probes(select_probes(probes, max_probes, seed))
    return [
        Variant(
            variant_id=probe.probe_id,
            family=probe.family,
            kind=probe.kind,
            path=probe.path,
            line=probe.line,
            before=probe.before,
            after=probe.after,
            file_text=probe.probe_text,
            hunk_id=_hunk_at(hunks, probe.path, probe.line),
        )
        for probe in chosen
    ]


def clause_variants(
    python_files: dict[str, tuple[str, str]], hunks: list[Hunk]
) -> list[Variant]:
    """The fix with one operand of an ``and``/``or`` on a changed line removed.

    The template operators change a comparison or negate a condition, but never
    drop a clause, so they cannot show that a clause the fix added (scrapy#2's
    ``... and key not in self``) is one no test depends on. The splice is textual,
    on the expression's exact span, so the rest of the file is untouched.
    """
    variants: list[Variant] = []
    for path, (original_text, fixed_text) in sorted(python_files.items()):
        try:
            tree = ast.parse(fixed_text)
        except SyntaxError:
            continue
        lines = fixed_text.splitlines(keepends=True)
        changed = set(lines_changed_by_fix(original_text, fixed_text))
        for node in ast.walk(tree):
            if not isinstance(node, ast.BoolOp) or len(node.values) < 2:
                continue
            span = range(node.lineno, (node.end_lineno or node.lineno) + 1)
            if not changed.intersection(span):
                continue
            joiner = " and " if isinstance(node.op, ast.And) else " or "
            operands = [_operand_source(lines, value) for value in node.values]
            for dropped in range(len(operands)):
                kept = operands[:dropped] + operands[dropped + 1 :]
                variant_text = _splice_expression(lines, node, joiner.join(kept))
                if variant_text == fixed_text or not _parses(variant_text):
                    continue
                variants.append(
                    Variant(
                        variant_id=f"c{len(variants) + 1:02d}",
                        family="clause",
                        kind="drop_clause",
                        path=path,
                        line=node.lineno,
                        before=lines[node.lineno - 1].strip()[:160],
                        after=_first_difference(fixed_text, variant_text)[:160],
                        file_text=variant_text,
                        hunk_id=_hunk_at(hunks, path, node.lineno),
                    )
                )
    return variants


def _operand_source(lines: list[str], node: ast.expr) -> str:
    text = _segment(lines, node)
    if isinstance(node, (ast.IfExp, ast.Lambda, ast.BoolOp, ast.NamedExpr)):
        return f"({text})"
    return text


def _segment(lines: list[str], node: ast.AST) -> str:
    first, last = node.lineno - 1, (node.end_lineno or node.lineno) - 1
    start, end = node.col_offset, node.end_col_offset or 0
    if first == last:
        return lines[first].encode("utf-8")[start:end].decode("utf-8")
    head = lines[first].encode("utf-8")[start:].decode("utf-8")
    tail = lines[last].encode("utf-8")[:end].decode("utf-8")
    return head + "".join(lines[first + 1 : last]) + tail


def _splice_expression(lines: list[str], node: ast.AST, replacement: str) -> str:
    first, last = node.lineno - 1, (node.end_lineno or node.lineno) - 1
    head = lines[first].encode("utf-8")[: node.col_offset].decode("utf-8")
    tail = lines[last].encode("utf-8")[node.end_col_offset or 0 :].decode("utf-8")
    return (
        "".join(lines[:first]) + head + replacement + tail + "".join(lines[last + 1 :])
    )


def _parses(source: str) -> bool:
    try:
        ast.parse(source)
    except SyntaxError:
        return False
    return True


def _first_difference(old_text: str, new_text: str) -> str:
    for old_line, new_line in zip(
        old_text.splitlines(), new_text.splitlines(), strict=False
    ):
        if old_line != new_line:
            return new_line.strip()
    return ""


def variant_diff(
    variant: Variant, python_files: dict[str, tuple[str, str]], fix_diff_others: str
) -> str:
    """The diff (against the buggy checkout) that applies the fix with ``variant``."""
    original_text = python_files[variant.path][0]
    section = "".join(
        difflib.unified_diff(
            original_text.splitlines(keepends=True),
            variant.file_text.splitlines(keepends=True),
            fromfile=f"a/{variant.path}",
            tofile=f"b/{variant.path}",
        )
    )
    return fix_diff_others + section


def _hunk_at(hunks: list[Hunk], path: str, line: int) -> str | None:
    for hunk in hunks:
        if hunk.path == path and line in hunk.fixed_lines:
            return hunk.hunk_id
    return None


def _same_behaviour(left: str, right: str) -> bool:
    try:
        return _normalised_dump(left) == _normalised_dump(right)
    except SyntaxError:
        return False


def _normalised_dump(source: str) -> str:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            node.returns = None
            _drop_docstring(node)
        elif isinstance(node, (ast.ClassDef, ast.Module)):
            _drop_docstring(node)
        elif isinstance(node, ast.arg):
            node.annotation = None
        elif isinstance(node, ast.AnnAssign):
            node.annotation = ast.Constant(value=None)
    return ast.dump(tree, include_attributes=False)


def _drop_docstring(node: ast.AST) -> None:
    body = getattr(node, "body", None)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        node.body = body[1:] or [ast.Pass()]
