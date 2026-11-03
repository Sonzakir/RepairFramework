"""Compare a test module before and after a change; one finding per effect.

Per test (matched by qualname; a test whose body reappears under a new name is a
rename, not a deletion):

- deleted → ``test_deleted``; skipped, xfailed or turned into an early return →
  ``test_skipped`` / ``test_xfailed``; a new ``try`` that swallows its assertions
  → ``exception_swallowed``; fewer ``parametrize`` cases → ``cases_removed``;
- assertions are compared as multisets. A removed assertion is paired with an
  added one on the same *actual* expression:
  - same kind, different expected value → ``expectation_changed``;
  - an exact kind replaced by a weaker one (``==`` → ``in``/truthiness,
    ``assertEqual`` → ``assertTrue``), a broader exception in ``raises``, or a
    looser tolerance → ``assertion_loosened``;
  - unpaired removals → ``assertion_removed``; unpaired additions →
    ``assertion_added``;
- added tests → ``test_added``; anything else that changed → ``test_modified``.

``expectation_changed`` is *weakening* when the change touches no source file
outside the tests (the test was rewritten to match the old behaviour, the black#3
pattern) and *suspicious* otherwise: a fix that changes behaviour may legitimately
update an expected value, so a person has to check which.
"""

import ast
import difflib
from collections import Counter

from apr_framework.tamper.models import (
    NEUTRAL,
    STRENGTHENING,
    SUSPICIOUS,
    WEAKENING,
    Finding,
)
from apr_framework.tamper.test_ast import (
    EXACT_KINDS,
    RAISES_KINDS,
    WEAK_KINDS,
    AssertionAtom,
    TestFunction,
    TestModule,
    parse_quietly,
    parse_test_module,
)

_BROAD_EXCEPTION_NAMES = ("Exception", "BaseException")
SIMILAR_SUBJECT_RATIO = 0.8
RENAMED_TEST_RATIO = 0.6
_LARGER_IS_LOOSER = ("delta", "rel", "abs", "rtol", "atol")
_SMALLER_IS_LOOSER = ("places", "decimal")


def compare_test_file(
    relative_path: str,
    old_text: str | None,
    new_text: str | None,
    *,
    source_changed: bool,
) -> list[Finding]:
    """Every finding for one test module."""
    old = parse_test_module(old_text) if old_text is not None else TestModule()
    new = parse_test_module(new_text) if new_text is not None else TestModule()
    if not new.parsed:
        if old_text is None or not old.parsed:
            # Not a test module: a data file (black keeps deliberately invalid
            # Python under tests/data/). It never parsed, so nothing stopped running.
            return []
        return [
            Finding(
                kind="test_file_unparseable",
                severity=WEAKENING,
                file=relative_path,
                message="The changed test file does not parse, so none of its tests run.",
            )
        ]
    findings: list[Finding] = []
    findings.extend(_module_skips(relative_path, old, new))
    renamed_to = _renames(old, new)
    rewritten_to = _rewritten_renames(old, new, renamed_to)
    for qualname, old_test in old.functions.items():
        if qualname in new.functions:
            findings.extend(
                _compare_test(
                    relative_path, old_test, new.functions[qualname], source_changed
                )
            )
        elif qualname in rewritten_to:
            new_test = new.functions[rewritten_to[qualname]]
            findings.append(
                Finding(
                    kind="test_renamed",
                    severity=NEUTRAL,
                    file=relative_path,
                    test=new_test.qualname,
                    line=new_test.line,
                    message=f"Renamed from {qualname} and edited; compared as one test.",
                )
            )
            findings.extend(
                _compare_test(relative_path, old_test, new_test, source_changed)
            )
        elif qualname in renamed_to:
            findings.append(
                Finding(
                    kind="test_renamed",
                    severity=NEUTRAL,
                    file=relative_path,
                    test=renamed_to[qualname],
                    line=new.functions[renamed_to[qualname]].line,
                    message=f"Renamed from {qualname}; the body is unchanged.",
                )
            )
        else:
            findings.append(
                Finding(
                    kind="test_deleted",
                    severity=WEAKENING,
                    file=relative_path,
                    test=qualname,
                    line=old_test.line,
                    before=_first_lines(old_test.source),
                    message=(
                        "The test file was deleted, and this test with it."
                        if new_text is None
                        else "Test deleted."
                    ),
                )
            )
    renamed_targets = set(renamed_to.values()) | set(rewritten_to.values())
    for qualname, new_test in new.functions.items():
        if qualname in old.functions or qualname in renamed_targets:
            continue
        findings.append(
            Finding(
                kind="test_added",
                severity=STRENGTHENING,
                file=relative_path,
                test=qualname,
                line=new_test.line,
                after=_first_lines(new_test.source),
                message=f"New test with {len(new_test.atoms)} assertion(s).",
            )
        )
    return findings


def _module_skips(
    relative_path: str, old: TestModule, new: TestModule
) -> list[Finding]:
    added = [skip for skip in new.module_skips if skip not in old.module_skips]
    return [
        Finding(
            kind="module_skipped",
            severity=WEAKENING,
            file=relative_path,
            after=skip,
            message="Added a module-level skip: every test in the file may stop running.",
        )
        for skip in added
    ]


def _renames(old: TestModule, new: TestModule) -> dict[str, str]:
    """Deleted test → added test with the same body fingerprint."""
    added = {
        test.body_fingerprint: qualname
        for qualname, test in new.functions.items()
        if qualname not in old.functions
    }
    return {
        qualname: added[test.body_fingerprint]
        for qualname, test in old.functions.items()
        if qualname not in new.functions and test.body_fingerprint in added
    }


def _rewritten_renames(
    old: TestModule, new: TestModule, exact: dict[str, str]
) -> dict[str, str]:
    """Deleted test → added test in the same class whose source is still similar.

    scrapy#10's developer fix renamed ``test_location_with_wrong_encoding`` to
    ``test_utf8_location`` and changed its input and expected URL: one edited
    test, not a deletion plus an addition.
    """
    deleted = [
        qualname
        for qualname in old.functions
        if qualname not in new.functions and qualname not in exact
    ]
    added = [
        qualname
        for qualname in new.functions
        if qualname not in old.functions and qualname not in exact.values()
    ]
    pairs: dict[str, str] = {}
    for old_name in deleted:
        scope = old_name.rpartition(".")[0]
        best, best_ratio = None, RENAMED_TEST_RATIO
        for new_name in added:
            if new_name in pairs.values() or new_name.rpartition(".")[0] != scope:
                continue
            ratio = difflib.SequenceMatcher(
                a=_body_source(old.functions[old_name]),
                b=_body_source(new.functions[new_name]),
                autojunk=False,
            ).ratio()
            if ratio >= best_ratio:
                best, best_ratio = new_name, ratio
        if best is not None:
            pairs[old_name] = best
    return pairs


def _body_source(test: TestFunction) -> str:
    """The test's source without its ``def`` line (the name changed)."""
    return "\n".join(test.source.splitlines()[1:])


def _compare_test(
    relative_path: str, old: TestFunction, new: TestFunction, source_changed: bool
) -> list[Finding]:
    findings: list[Finding] = []

    def add(
        kind: str,
        severity: str,
        message: str,
        before: str = "",
        after: str = "",
        line=None,
    ):
        findings.append(
            Finding(
                kind=kind,
                severity=severity,
                file=relative_path,
                test=new.qualname,
                line=line or new.line,
                before=before,
                after=after,
                message=message,
            )
        )

    if new.skipped_by and not old.skipped_by:
        conditional = new.skip_conditional
        add(
            "test_skipped",
            SUSPICIOUS if conditional else WEAKENING,
            "Now skipped under a condition." if conditional else "Now skipped.",
            after=new.skipped_by,
        )
    if new.xfailed_by and not old.xfailed_by:
        add(
            "test_xfailed",
            WEAKENING,
            "Now marked as an expected failure.",
            after=new.xfailed_by,
        )
    for line in new.swallowing_try:
        if not old.swallowing_try:
            add(
                "exception_swallowed",
                WEAKENING,
                "Assertions now sit in a try block whose handler does not re-raise.",
                line=line,
            )
    if (
        old.parametrize_cases is not None
        and new.parametrize_cases is not None
        and new.parametrize_cases < old.parametrize_cases
    ):
        add(
            "cases_removed",
            WEAKENING,
            f"Parametrized cases went from {old.parametrize_cases} to {new.parametrize_cases}.",
        )
    findings.extend(_compare_atoms(relative_path, old, new, source_changed))
    if not findings and old.source != new.source:
        changed_values = _literal_only_change(old.source, new.source)
        if changed_values:
            before, after = changed_values
            if source_changed:
                add(
                    "test_values_changed",
                    SUSPICIOUS,
                    "Only literal values in the test changed, together with the source; "
                    "check that the new values are the specified behaviour.",
                    before=before,
                    after=after,
                )
            else:
                add(
                    "test_values_changed",
                    WEAKENING,
                    "Only literal values in the test changed and no source file changed: "
                    "the test was rewritten to accept the current behaviour.",
                    before=before,
                    after=after,
                )
        else:
            add(
                "test_modified",
                NEUTRAL,
                "The test body changed; its assertions did not.",
            )
    return findings


def _literal_only_change(old_source: str, new_source: str) -> tuple[str, str] | None:
    """The changed lines when two functions differ only in literal values, else None.

    Catches expectations passed to helpers that assert internally, such as
    black#3's ``self.invokeBlack(args, exit_code=2)`` → ``exit_code=1``.
    """
    old_tree, new_tree = parse_quietly(old_source), parse_quietly(new_source)
    if _masked_dump(old_tree) != _masked_dump(new_tree):
        return None
    old_lines, new_lines = old_source.splitlines(), new_source.splitlines()
    before: list[str] = []
    after: list[str] = []
    for old_node, new_node in zip(ast.walk(old_tree), ast.walk(new_tree), strict=True):
        if (
            isinstance(old_node, ast.Constant)
            and isinstance(new_node, ast.Constant)
            and (type(old_node.value), old_node.value)
            != (type(new_node.value), new_node.value)
        ):
            old_line = old_lines[old_node.lineno - 1].strip()
            new_line = new_lines[new_node.lineno - 1].strip()
            if old_line not in before:
                before.append(old_line)
            if new_line not in after:
                after.append(new_line)
    if not before:
        return None
    return "\n".join(before), "\n".join(after)


def _masked_dump(tree: ast.AST) -> str:
    masked = parse_quietly(ast.unparse(tree))
    for node in ast.walk(masked):
        if isinstance(node, ast.Constant):
            node.value = None
    return ast.dump(masked, include_attributes=False)


def _compare_atoms(
    relative_path: str, old: TestFunction, new: TestFunction, source_changed: bool
) -> list[Finding]:
    old_counts = Counter(atom.text for atom in old.atoms)
    new_counts = Counter(atom.text for atom in new.atoms)
    removed = _take(old.atoms, old_counts - new_counts)
    added = _take(new.atoms, new_counts - old_counts)
    findings: list[Finding] = []
    unpaired_added = list(added)
    unpaired_removed: list[AssertionAtom] = []
    for old_atom in removed:
        partner = next(
            (atom for atom in unpaired_added if _same_subject(old_atom, atom)), None
        ) or next(
            (atom for atom in unpaired_added if _similar_subject(old_atom, atom)), None
        )
        if partner is None:
            unpaired_removed.append(old_atom)
            continue
        unpaired_added.remove(partner)
        findings.append(
            _paired_finding(relative_path, new, old_atom, partner, source_changed)
        )
    if unpaired_removed and _checks(new) >= _checks(old):
        # Assertions moved into a helper, a loop or a parametrized form: as many
        # checks as before, written differently. A person should look, but no
        # check disappeared.
        findings.append(
            Finding(
                kind="assertions_rewritten",
                severity=SUSPICIOUS,
                file=relative_path,
                test=new.qualname,
                line=new.line,
                before="\n".join(atom.text for atom in unpaired_removed),
                after="\n".join(atom.text for atom in unpaired_added),
                message=(
                    f"{len(unpaired_removed)} assertion(s) replaced by "
                    f"{len(unpaired_added)} different one(s); the test checks "
                    f"{_checks(old)} → {_checks(new)} cases."
                ),
            )
        )
        return findings
    for old_atom in unpaired_removed:
        findings.append(
            _finding(
                relative_path,
                new,
                "assertion_removed",
                WEAKENING,
                "Assertion removed.",
                old_atom,
                None,
            )
        )
    for atom in unpaired_added:
        findings.append(
            _finding(
                relative_path,
                new,
                "assertion_added",
                STRENGTHENING,
                "Assertion added.",
                None,
                atom,
            )
        )
    return findings


def _checks(test: TestFunction) -> int:
    """Assertions times parametrized cases: how many checks the test makes."""
    return len(test.atoms) * max(1, test.parametrize_cases or 1)


def _similar_subject(old: AssertionAtom, new: AssertionAtom) -> bool:
    """Same kind of assertion on a nearly identical expression (an input changed)."""
    if old.kind != new.kind or not old.actual or not new.actual:
        return False
    ratio = difflib.SequenceMatcher(a=old.actual, b=new.actual, autojunk=False).ratio()
    return ratio >= SIMILAR_SUBJECT_RATIO


def _same_subject(old: AssertionAtom, new: AssertionAtom) -> bool:
    """Whether two assertions check the same thing (so one replaced the other)."""
    if old.actual:
        return old.actual == new.actual
    # ``with self.assertRaises(X):`` / ``with pytest.raises(X):`` have no subject.
    return not new.actual and old.kind in RAISES_KINDS and new.kind in RAISES_KINDS


def _paired_finding(
    relative_path: str,
    test: TestFunction,
    old_atom: AssertionAtom,
    new_atom: AssertionAtom,
    source_changed: bool,
) -> Finding:
    loosened = _loosening(old_atom, new_atom)
    if loosened:
        return _finding(
            relative_path,
            test,
            "assertion_loosened",
            WEAKENING,
            loosened,
            old_atom,
            new_atom,
        )
    if old_atom.kind == new_atom.kind and old_atom.expected != new_atom.expected:
        if source_changed:
            return _finding(
                relative_path,
                test,
                "expectation_changed",
                SUSPICIOUS,
                "Expected value changed together with the source; check that the new "
                "expectation is the specified behaviour.",
                old_atom,
                new_atom,
            )
        return _finding(
            relative_path,
            test,
            "expectation_changed",
            WEAKENING,
            "Expected value changed and no source file changed: the test was "
            "rewritten to accept the current behaviour.",
            old_atom,
            new_atom,
        )
    if _stricter(old_atom, new_atom):
        return _finding(
            relative_path,
            test,
            "assertion_tightened",
            STRENGTHENING,
            "Assertion made stricter.",
            old_atom,
            new_atom,
        )
    return _finding(
        relative_path,
        test,
        "assertion_changed",
        SUSPICIOUS,
        "Assertion on the same value rewritten.",
        old_atom,
        new_atom,
    )


def _loosening(old: AssertionAtom, new: AssertionAtom) -> str:
    if old.kind in EXACT_KINDS and new.kind in WEAK_KINDS | {"falsy"}:
        return f"An exact check ({old.kind}) became a weaker one ({new.kind})."
    if old.kind in RAISES_KINDS and new.kind in RAISES_KINDS:
        old_name = old.expected.rsplit(".", 1)[-1]
        new_name = new.expected.rsplit(".", 1)[-1]
        if (
            new_name in _BROAD_EXCEPTION_NAMES
            and old_name not in _BROAD_EXCEPTION_NAMES
        ):
            return f"Expected exception broadened from {old_name} to {new_name}."
    old_tolerance, new_tolerance = dict(old.tolerance), dict(new.tolerance)
    for keyword in set(old_tolerance) | set(new_tolerance):
        before, after = (
            _number(old_tolerance.get(keyword)),
            _number(new_tolerance.get(keyword)),
        )
        if keyword in _LARGER_IS_LOOSER:
            if after is not None and (before is None or after > before):
                return f"Tolerance {keyword} loosened ({before} → {after})."
        elif (
            keyword in _SMALLER_IS_LOOSER
            and before is not None
            and after is not None
            and after < before
        ):
            return f"Tolerance {keyword} loosened ({before} → {after})."
    return ""


def _stricter(old: AssertionAtom, new: AssertionAtom) -> bool:
    return old.kind in WEAK_KINDS | {"falsy"} and new.kind in EXACT_KINDS


def _number(text: str | None) -> float | None:
    """The value of a numeric literal expression (``1e-6``, ``10 ** -6``), else None.

    The text comes from the change under review, so it is never executed: only
    numbers and arithmetic operators are evaluated, on the parsed tree.
    """
    if text is None:
        return None
    try:
        return _evaluate_number(ast.parse(text, mode="eval").body)
    except (SyntaxError, ValueError, ZeroDivisionError, OverflowError):
        return None


def _evaluate_number(node: ast.AST) -> float:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        value = _evaluate_number(node.operand)
        return -value if isinstance(node.op, ast.USub) else value
    if isinstance(node, ast.BinOp) and isinstance(
        node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow)
    ):
        left, right = _evaluate_number(node.left), _evaluate_number(node.right)
        if isinstance(node.op, ast.Pow):
            if abs(right) > 64:
                raise ValueError("exponent too large")
            return left**right
        operations = {
            ast.Add: left + right,
            ast.Sub: left - right,
            ast.Mult: left * right,
        }
        if isinstance(node.op, ast.Div):
            return left / right
        return operations[type(node.op)]
    raise ValueError("not a numeric literal")


def _take(atoms: list[AssertionAtom], counts: Counter) -> list[AssertionAtom]:
    remaining = Counter(counts)
    taken: list[AssertionAtom] = []
    for atom in atoms:
        if remaining[atom.text] > 0:
            remaining[atom.text] -= 1
            taken.append(atom)
    return taken


def _finding(
    relative_path: str,
    test: TestFunction,
    kind: str,
    severity: str,
    message: str,
    old_atom: AssertionAtom | None,
    new_atom: AssertionAtom | None,
) -> Finding:
    return Finding(
        kind=kind,
        severity=severity,
        file=relative_path,
        test=test.qualname,
        line=(new_atom or old_atom).line if (new_atom or old_atom) else test.line,
        before=old_atom.text if old_atom else "",
        after=new_atom.text if new_atom else "",
        message=message,
    )


def _first_lines(source: str, count: int = 3) -> str:
    lines = source.splitlines()
    return "\n".join(lines[:count]) + ("\n..." if len(lines) > count else "")
