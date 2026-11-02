"""Amplification: variants of the project's own tests with one literal changed.

The tests that reach a focus function already set up realistic objects; changing
one input literal (``LocalCache(limit=2)`` → ``limit=0``, ``cache['c']`` →
``cache['a']``) explores behaviour next to what the tests check, without writing
any test from scratch and without a model.

Rules:

- Only literals in the test body are changed: never the docstring, never inside
  an ``assert`` statement or an ``assert*``/``fail*`` call (changing an expected
  value only makes every version fail the same way), never inside an f-string.
- Mutations: int → 0, 1, -1, n+1, n-1; float → 0.0, -x, x+1.0; str → up to two
  other string literals of the same test, then ""; bool → its negation;
  bytes → b"".
- Selection is deterministic: round-robin over tests, then over literals, then
  over mutations, up to a per-test and a total cap.
- A variant is the original function's exact source with one literal spliced at
  its recorded position and the function renamed ``<name>__bdiff_<id>``. It is
  inserted right after the original, at the same indentation, so decorators,
  class scope, zero-argument ``super()`` and name mangling all keep working.
"""

import ast
from dataclasses import dataclass

VARIANT_MARKER = "__bdiff_"
_ASSERTION_CALL_PREFIXES = ("assert", "fail")


@dataclass(frozen=True)
class ReachingTest:
    """A test that called a focus function in the discovery run."""

    test_id: str
    file: str
    qualname: str


@dataclass(frozen=True)
class Variant:
    """One amplified test.

    Fields:
        variant_id:  ``v01``, ``v02``, ... in selection order.
        test_id:     The original test's id.
        file:        The test file (relative to the checkout).
        qualname:    The original test's qualname (``Class.test_x`` or ``test_x``).
        line:        The changed literal's line in the original file.
        original:    The literal's source text.
        replacement: The new literal's source text.
        new_name:    The variant function's name.
    """

    variant_id: str
    test_id: str
    file: str
    qualname: str
    line: int
    original: str
    replacement: str
    new_name: str

    def describe(self) -> str:
        return f"{self.file}:{self.line} {self.original} → {self.replacement}"

    def test_id_for(self, runner: str) -> str:
        """The id the runner gives the variant (pytest adds ``[params]`` itself)."""
        old_name = self.qualname.rsplit(".", 1)[-1]
        if runner == "unittest":
            return self.test_id[: len(self.test_id) - len(old_name)] + self.new_name
        node_prefix, _, last = self.test_id.rpartition("::")
        return f"{node_prefix}::{self.new_name}" if last else self.new_name


@dataclass(frozen=True)
class _LiteralSite:
    line: int
    column: int
    end_line: int
    end_column: int
    source: str
    replacements: tuple[str, ...]


def find_test_function(
    tree: ast.Module, qualname: str
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """The function or method named by ``Class.method`` / ``function``."""
    scope: list[ast.stmt] = tree.body
    parts = qualname.split(".")
    for index, part in enumerate(parts):
        last = index == len(parts) - 1
        match = None
        for node in scope:
            if last and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == part:
                    match = node
            elif not last and isinstance(node, ast.ClassDef) and node.name == part:
                match = node
        if match is None:
            return None
        if last:
            return match
        scope = match.body
    return None


def literal_sites(
    function: ast.FunctionDef | ast.AsyncFunctionDef, source_lines: list[str]
) -> list[_LiteralSite]:
    """The mutable literals of a test function's body, in source order."""
    body = list(function.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    constants: list[ast.Constant] = []
    for statement in body:
        _collect_constants(statement, constants, excluded=False)
    string_values = sorted(
        {
            constant.value
            for constant in constants
            if isinstance(constant.value, str) and len(constant.value) <= 80
        }
    )
    sites: list[_LiteralSite] = []
    seen: set[tuple[int, int]] = set()
    for constant in constants:
        if constant.end_lineno is None or constant.end_col_offset is None:
            continue
        position = (constant.lineno, constant.col_offset)
        if position in seen:
            continue
        seen.add(position)
        original = _segment(
            source_lines,
            constant.lineno,
            constant.col_offset,
            constant.end_lineno,
            constant.end_col_offset,
        )
        replacements = _replacements(constant.value, string_values)
        if not original or not replacements:
            continue
        sites.append(
            _LiteralSite(
                line=constant.lineno,
                column=constant.col_offset,
                end_line=constant.end_lineno,
                end_column=constant.end_col_offset,
                source=original,
                replacements=replacements,
            )
        )
    sites.sort(key=lambda site: (site.line, site.column))
    return sites


def _collect_constants(
    node: ast.AST, found: list[ast.Constant], excluded: bool
) -> None:
    if isinstance(node, (ast.Assert, ast.JoinedStr)):
        return
    if isinstance(node, ast.Call) and _is_assertion_call(node):
        return
    if isinstance(node, ast.Constant) and not excluded:
        found.append(node)
        return
    for child in ast.iter_child_nodes(node):
        _collect_constants(child, found, excluded)


def _is_assertion_call(call: ast.Call) -> bool:
    function = call.func
    name = (
        function.attr
        if isinstance(function, ast.Attribute)
        else function.id
        if isinstance(function, ast.Name)
        else ""
    )
    return name.lower().startswith(_ASSERTION_CALL_PREFIXES)


def _replacements(value: object, string_values: list[str]) -> tuple[str, ...]:
    if isinstance(value, bool):
        return (repr(not value),)
    if isinstance(value, int):
        candidates = [0, 1, -1, value + 1, value - 1]
        return tuple(dict.fromkeys(repr(item) for item in candidates if item != value))
    if isinstance(value, float):
        candidates = [0.0, -value, value + 1.0]
        return tuple(dict.fromkeys(repr(item) for item in candidates if item != value))
    if isinstance(value, str):
        # Another literal of the same test first: reusing a key or name the test
        # already uses (a collision) tells versions apart more often than "".
        others = [other for other in string_values if other != value][:2]
        candidates = others + ([""] if value else [])
        return tuple(dict.fromkeys(repr(item) for item in candidates))
    if isinstance(value, bytes):
        return (repr(b""),) if value else ()
    return ()


def plan_variants(
    reaching_tests: list[ReachingTest],
    sources: dict[str, str],
    *,
    per_test: int,
    total: int,
) -> list[Variant]:
    """Choose the variants: round-robin over tests, literals and mutations."""
    queues: list[tuple[ReachingTest, list[tuple[_LiteralSite, str]]]] = []
    for test in reaching_tests:
        source = sources.get(test.file)
        if source is None:
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        function = find_test_function(tree, test.qualname)
        if function is None:
            continue
        sites = literal_sites(function, source.splitlines(keepends=True))
        ordered: list[tuple[_LiteralSite, str]] = []
        depth = max((len(site.replacements) for site in sites), default=0)
        for round_index in range(depth):
            for site in sites:
                if round_index < len(site.replacements):
                    ordered.append((site, site.replacements[round_index]))
        if ordered:
            queues.append((test, ordered[:per_test]))

    variants: list[Variant] = []
    round_index = 0
    while len(variants) < total and any(
        round_index < len(queue) for _, queue in queues
    ):
        for test, queue in queues:
            if round_index >= len(queue) or len(variants) >= total:
                continue
            site, replacement = queue[round_index]
            variant_id = f"v{len(variants) + 1:02d}"
            old_name = test.qualname.rsplit(".", 1)[-1]
            variants.append(
                Variant(
                    variant_id=variant_id,
                    test_id=test.test_id,
                    file=test.file,
                    qualname=test.qualname,
                    line=site.line,
                    original=site.source,
                    replacement=replacement,
                    new_name=f"{old_name}{VARIANT_MARKER}{variant_id}",
                )
            )
        round_index += 1
    return variants


def augment_source(source: str, variants: list[Variant]) -> str:
    """Insert every variant of ``source``'s tests right after its original."""
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    insertions: dict[int, list[str]] = {}
    for variant in variants:
        function = find_test_function(tree, variant.qualname)
        if function is None:
            continue
        block = _variant_block(function, lines, variant)
        if block is None:
            continue
        end_line = function.end_lineno or function.lineno
        insertions.setdefault(end_line, []).extend(block)
    for end_line in sorted(insertions, reverse=True):
        lines[end_line:end_line] = insertions[end_line]
    return "".join(lines)


def _variant_block(
    function: ast.FunctionDef | ast.AsyncFunctionDef, lines: list[str], variant: Variant
) -> list[str] | None:
    first_line = min(
        [function.lineno, *(decorator.lineno for decorator in function.decorator_list)]
    )
    end_line = function.end_lineno or function.lineno
    block = lines[first_line - 1 : end_line]
    site = _site_at(function, lines, variant)
    if site is None:
        return None
    block = _splice(block, first_line, site, variant.replacement)
    def_index = function.lineno - first_line
    def_line = block[def_index]
    old_name = function.name
    for keyword in ("async def ", "def "):
        needle = f"{keyword}{old_name}("
        if needle in def_line:
            block[def_index] = def_line.replace(
                needle, f"{keyword}{variant.new_name}(", 1
            )
            break
    else:
        return None
    indentation = block[0][: len(block[0]) - len(block[0].lstrip())]
    return [f"{indentation}\n" if indentation else "\n", *block]


def _site_at(
    function: ast.FunctionDef | ast.AsyncFunctionDef, lines: list[str], variant: Variant
) -> _LiteralSite | None:
    for site in literal_sites(function, lines):
        if site.line == variant.line and site.source == variant.original:
            return site
    return None


def _splice(
    block: list[str], first_line: int, site: _LiteralSite, replacement: str
) -> list[str]:
    start = site.line - first_line
    end = site.end_line - first_line
    head = block[start].encode("utf-8")[: site.column].decode("utf-8")
    tail = block[end].encode("utf-8")[site.end_column :].decode("utf-8")
    return [*block[:start], head + replacement + tail, *block[end + 1 :]]


def _segment(
    lines: list[str], line: int, column: int, end_line: int, end_column: int
) -> str:
    if line == end_line:
        return lines[line - 1].encode("utf-8")[column:end_column].decode("utf-8")
    first = lines[line - 1].encode("utf-8")[column:].decode("utf-8")
    middle = lines[line : end_line - 1]
    last = lines[end_line - 1].encode("utf-8")[:end_column].decode("utf-8")
    return first + "".join(middle) + last


def variant_of_test_id(
    test_id: str, variants_by_name: dict[str, Variant]
) -> Variant | None:
    """The variant a result id belongs to (pytest may add ``[params]``)."""
    if VARIANT_MARKER not in test_id:
        return None
    tail = test_id.split(VARIANT_MARKER, 1)[1]
    variant_id = tail.split("[", 1)[0].split(".", 1)[0]
    return variants_by_name.get(variant_id)
