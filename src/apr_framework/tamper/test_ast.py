"""What a test module contains, in the terms Tamper Check compares.

For every test function (``test*`` functions and ``test*`` methods of any class),
``TestFunction`` records:

- its assertion *atoms*: each ``assert`` statement and each assertion-style call
  (``self.assertEqual``, ``assert_called_once_with``, ``pytest.raises``,
  ``np.testing.assert_allclose``, a helper named ``check_*``/``verify_*``/``expect_*``)
  with its *actual* expression, its *expected* expression and its tolerance
  keywords;
- whether it is skipped or marked as an expected failure, by decorator or by a
  ``pytest.skip`` / ``self.skipTest`` / ``raise SkipTest`` / leading ``return``;
- ``try`` blocks that wrap assertions and swallow the error;
- the number of ``parametrize`` cases;
- a fingerprint of its body, to recognise a renamed test.
"""

import ast
import hashlib
import warnings
from dataclasses import dataclass, field

_ASSERTION_PREFIXES = ("assert", "check_", "verify_", "expect_")
_SKIP_DECORATORS = ("skip", "skipif", "skipIf", "skipUnless")
_XFAIL_DECORATORS = ("xfail", "expectedFailure")
_SKIP_CALLS = ("skip", "skipTest", "xfail", "importorskip")
_BROAD_EXCEPTIONS = frozenset({"Exception", "BaseException", "AssertionError"})
EXACT_KINDS = frozenset(
    {
        "==",
        "is",
        "assertEqual",
        "assertEquals",
        "assertIs",
        "assertDictEqual",
        "assertListEqual",
        "assertTupleEqual",
        "assertSetEqual",
        "assertSequenceEqual",
        "assertMultiLineEqual",
        "assertCountEqual",
        "assert_called_once_with",
        "assert_called_with",
        "assert_any_call",
        "assert_has_calls",
        "assert_array_equal",
        "assert_frame_equal",
        "assert_series_equal",
    }
)
WEAK_KINDS = frozenset(
    {
        "truthy",
        "assertTrue",
        "assertIsNotNone",
        "assertIsInstance",
        "assert_called",
        "assert_called_once",
        "assertIn",
        "in",
        "assertGreater",
        "assertGreaterEqual",
        "assertLess",
        "assertLessEqual",
        ">",
        ">=",
        "<",
        "<=",
        "!=",
        "assertNotEqual",
    }
)
RAISES_KINDS = frozenset(
    {"raises", "assertRaises", "assertRaisesRegex", "assertRaisesRegexp"}
)
TOLERANCE_KEYWORDS = ("places", "delta", "rel", "abs", "rtol", "atol", "decimal")


@dataclass(frozen=True)
class AssertionAtom:
    """One assertion: what kind, on what, expecting what.

    Fields:
        kind:      ``==``, ``in``, ``truthy`` ... for ``assert`` statements; the
                   called name (``assertEqual``, ``raises``) for calls.
        actual:    The checked expression (the first operand or argument, or the
                   receiver of a mock assertion).
        expected:  The expected expression(s), or "" when there is none.
        tolerance: Tolerance keywords and their source (``places=7``).
        text:      The whole assertion's source, for the report.
        line:      Its line.
    """

    kind: str
    actual: str
    expected: str
    tolerance: tuple[tuple[str, str], ...]
    text: str
    line: int


@dataclass
class TestFunction:
    """One test function or method."""

    qualname: str
    line: int
    atoms: list[AssertionAtom] = field(default_factory=list)
    skipped_by: str = ""
    skip_conditional: bool = False
    xfailed_by: str = ""
    swallowing_try: list[int] = field(default_factory=list)
    parametrize_cases: int | None = None
    body_fingerprint: str = ""
    source: str = ""


@dataclass
class TestModule:
    """The tests of one file, plus module-level skips."""

    functions: dict[str, TestFunction] = field(default_factory=dict)
    module_skips: list[str] = field(default_factory=list)
    parsed: bool = True


def parse_test_module(source: str) -> TestModule:
    """Read the tests of ``source`` (``parsed`` is False when it does not parse)."""
    try:
        tree = parse_quietly(source)
    except SyntaxError:
        return TestModule(parsed=False)
    module = TestModule()
    for statement in tree.body:
        skip = _module_level_skip(statement)
        if skip:
            module.module_skips.append(skip)
    _collect_tests(tree.body, "", module)
    return module


def parse_quietly(source: str) -> ast.Module:
    """``ast.parse`` without the project's own SyntaxWarnings (invalid escapes)."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return ast.parse(source)


def _collect_tests(body: list[ast.stmt], prefix: str, module: TestModule) -> None:
    for node in body:
        if isinstance(node, ast.ClassDef):
            _collect_tests(node.body, prefix + node.name + ".", module)
        elif isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef)
        ) and node.name.startswith("test"):
            qualname = prefix + node.name
            module.functions[qualname] = _read_test(node, qualname)


def _read_test(
    node: ast.FunctionDef | ast.AsyncFunctionDef, qualname: str
) -> TestFunction:
    test = TestFunction(qualname=qualname, line=node.lineno, source=ast.unparse(node))
    body = _without_docstring(node.body)
    test.body_fingerprint = hashlib.sha256(
        "\n".join(ast.dump(statement) for statement in body).encode("utf-8")
    ).hexdigest()
    for decorator in node.decorator_list:
        name = _called_name(decorator)
        text = ast.unparse(decorator)
        if name in _SKIP_DECORATORS and not test.skipped_by:
            test.skipped_by = f"@{text}"
            test.skip_conditional = name != "skip"
        if name in _XFAIL_DECORATORS:
            test.xfailed_by = test.xfailed_by or f"@{text}"
        if (
            name == "parametrize"
            and isinstance(decorator, ast.Call)
            and len(decorator.args) >= 2
        ):
            cases = decorator.args[1]
            if isinstance(cases, (ast.List, ast.Tuple)):
                test.parametrize_cases = len(cases.elts)
    if body and isinstance(body[0], ast.Return):
        test.skipped_by = test.skipped_by or "a `return` as the first statement"
    for child in ast.walk(ast.Module(body=body, type_ignores=[])):
        _read_node(child, test)
    return test


def _read_node(node: ast.AST, test: TestFunction) -> None:
    if isinstance(node, ast.Assert):
        test.atoms.append(_assert_statement_atom(node))
    elif isinstance(node, ast.Call):
        name = _called_name(node)
        if name in _SKIP_CALLS and _receiver_is_runner(node):
            if name == "xfail":
                test.xfailed_by = test.xfailed_by or ast.unparse(node)
            else:
                test.skipped_by = test.skipped_by or ast.unparse(node)
        elif _is_assertion_name(name):
            test.atoms.append(_call_atom(node, name))
    elif isinstance(node, ast.Raise) and node.exc is not None:
        if _called_name(node.exc) in ("SkipTest", "Skipped"):
            test.skipped_by = test.skipped_by or ast.unparse(node)
    elif isinstance(node, ast.Try) and _swallows_assertions(node):
        test.swallowing_try.append(node.lineno)


def _assert_statement_atom(node: ast.Assert) -> AssertionAtom:
    test = node.test
    if isinstance(test, ast.Compare) and len(test.ops) == 1:
        kind = _COMPARE_SYMBOLS.get(type(test.ops[0]), type(test.ops[0]).__name__)
        actual, expected = ast.unparse(test.left), ast.unparse(test.comparators[0])
        if isinstance(test.ops[0], (ast.In, ast.NotIn)):
            # `x in container` checks the container.
            actual, expected = expected, actual
        elif _is_literal(test.left) and not _is_literal(test.comparators[0]):
            # `assert 3 == f()`: the subject is the computed side.
            actual, expected = expected, actual
        tolerance = _approx_tolerance(test.comparators[0])
    elif isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        kind, actual, expected, tolerance = "falsy", ast.unparse(test.operand), "", ()
    else:
        kind, actual, expected, tolerance = "truthy", ast.unparse(test), "", ()
    return AssertionAtom(
        kind=kind,
        actual=actual,
        expected=expected,
        tolerance=tolerance,
        text=ast.unparse(node),
        line=node.lineno,
    )


_COMPARE_SYMBOLS = {
    ast.Eq: "==",
    ast.NotEq: "!=",
    ast.Is: "is",
    ast.IsNot: "is not",
    ast.In: "in",
    ast.NotIn: "not in",
    ast.Lt: "<",
    ast.LtE: "<=",
    ast.Gt: ">",
    ast.GtE: ">=",
}


def _call_atom(node: ast.Call, name: str) -> AssertionAtom:
    function = node.func
    is_mock_assertion = name.startswith("assert_called") or name in (
        "assert_any_call",
        "assert_has_calls",
        "assert_not_called",
    )
    if name in RAISES_KINDS:
        kind = "raises" if name == "raises" else name
        actual = ast.unparse(node.args[1]) if len(node.args) > 1 else ""
        expected = ast.unparse(node.args[0]) if node.args else ""
    elif is_mock_assertion and isinstance(function, ast.Attribute):
        kind, actual = name, ast.unparse(function.value)
        expected = ", ".join(
            [ast.unparse(argument) for argument in node.args]
            + [ast.unparse(keyword) for keyword in node.keywords]
        )
    else:
        kind = name
        first, second = (node.args + [None, None])[:2]
        if (
            first is not None
            and second is not None
            and _is_literal(first)
            and not _is_literal(second)
        ):
            # `assertEqual(expected, actual)`, the order some projects use.
            first, second = second, first
        actual = ast.unparse(first) if first is not None else ""
        expected = ast.unparse(second) if second is not None else ""
    tolerance = tuple(
        (keyword.arg, ast.unparse(keyword.value))
        for keyword in node.keywords
        if keyword.arg in TOLERANCE_KEYWORDS
    )
    return AssertionAtom(
        kind=kind,
        actual=actual,
        expected=expected,
        tolerance=tolerance,
        text=ast.unparse(node),
        line=node.lineno,
    )


def _approx_tolerance(node: ast.AST) -> tuple[tuple[str, str], ...]:
    if isinstance(node, ast.Call) and _called_name(node) == "approx":
        return tuple(
            (keyword.arg, ast.unparse(keyword.value))
            for keyword in node.keywords
            if keyword.arg in TOLERANCE_KEYWORDS
        )
    return ()


def _swallows_assertions(node: ast.Try) -> bool:
    has_assertion = any(
        isinstance(child, ast.Assert)
        or (isinstance(child, ast.Call) and _is_assertion_name(_called_name(child)))
        for statement in node.body
        for child in ast.walk(statement)
    )
    if not has_assertion:
        return False
    for handler in node.handlers:
        caught = _caught_names(handler)
        if caught and not caught & _BROAD_EXCEPTIONS:
            continue
        reraises = any(isinstance(child, ast.Raise) for child in ast.walk(handler))
        if not reraises:
            return True
    return False


def _caught_names(handler: ast.ExceptHandler) -> set[str]:
    if handler.type is None:
        return set()
    nodes = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return {ast.unparse(node).rsplit(".", 1)[-1] for node in nodes}


def _module_level_skip(statement: ast.stmt) -> str:
    if isinstance(statement, ast.Assign) and any(
        isinstance(target, ast.Name) and target.id == "pytestmark"
        for target in statement.targets
    ):
        text = ast.unparse(statement)
        if "skip" in text or "xfail" in text:
            return text
    calls = [node for node in ast.walk(statement) if isinstance(node, ast.Call)]
    for call in calls:
        name = _called_name(call)
        if name in ("skip", "importorskip") and _receiver_is_runner(call):
            if isinstance(
                statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                return ""
            return ast.unparse(statement)
    return ""


def _is_literal(node: ast.AST) -> bool:
    """Constants and containers or arithmetic made only of constants."""
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return all(_is_literal(item) for item in node.elts)
    if isinstance(node, ast.Dict):
        return all(
            key is not None and _is_literal(key) and _is_literal(value)
            for key, value in zip(node.keys, node.values, strict=True)
        )
    if isinstance(node, ast.UnaryOp):
        return _is_literal(node.operand)
    if isinstance(node, ast.BinOp):
        return _is_literal(node.left) and _is_literal(node.right)
    return False


def _called_name(node: ast.AST) -> str:
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _receiver_is_runner(call: ast.Call) -> bool:
    function = call.func
    if isinstance(function, ast.Name):
        return True
    if isinstance(function, ast.Attribute):
        receiver = ast.unparse(function.value)
        return receiver in ("pytest", "self", "unittest", "cls") or receiver.endswith(
            ("pytest", "unittest")
        )
    return False


def _is_assertion_name(name: str) -> bool:
    return name.startswith(_ASSERTION_PREFIXES) or name in ("raises", "fail")


def _without_docstring(body: list[ast.stmt]) -> list[ast.stmt]:
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        return body[1:]
    return body
