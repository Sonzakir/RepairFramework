"""Wrong-fix probes: plausible-wrong variants of a fix, and their verdicts.

A probe is the fix with one file replaced. Two sources:

- **Deterministic** (ported from the Strict Repro audit,
  ``99 Attachments/strict-repro-audit-2026-10-22``), one change on a line the fix
  added or replaced:
  - ``operator``: one of the template engine's six AST operators;
  - ``drop``: one touched statement replaced by ``pass`` (an incomplete fix);
  - ``revert``: one change block of a multi-block fix reverted.
  At most ``max_probes``, round-robin across families with a seeded shuffle.
- **Test-aware LLM probes**, up to 12 in four kinds (symptom suppression,
  input-specific shortcut, incomplete fix, behaviour substitution). The model sees
  the issue, the fix and the repro test, and rewrites one function the fix touched
  into a variant it believes passes the test but is wrong.

**Confirmation rule.** A probe counts as *confirmed wrong* only with evidence from
an oracle that is independent of the repro test and passes on the fix itself:

- BugsInPy: the probe fails the bug's trigger test (``trigger``), or passes it but
  adds a failure to the bug's test file (``test_file``).
- Local mode: the probe fails ``--regression-cmd`` (``regression_cmd``).

Everything else is ``unconfirmed`` (possibly equivalent) and never enters the
strictness denominator; a probe that does not apply, is rejected by the
integrity gate, or does not import is ``invalid``. Oracle timeouts are
``unconfirmed``: a slow executor is not evidence.
"""

import ast
import difflib
import random
from collections.abc import Callable
from dataclasses import dataclass, replace

from apr_framework.repair.evaluator_integrity import EvaluatorGuard
from apr_framework.repair.patch_applier import ValidationOutcome, is_passing_trigger_run
from apr_framework.repair.regression import RegressionContext
from apr_framework.repair.template.operators import get_operator_class
from apr_framework.repro.models import (
    FileStates,
    Probe,
    ProbeResult,
    ReproIssue,
    TestVersion,
)
from apr_framework.repro.prompts import load_prompt, render_prompt
from apr_framework.repro.runner import TIMEOUT_RETURN_CODE, TestCommandRunner
from apr_framework.repro.spend import MeteredLLMClient
from apr_framework.repro.validation import (
    NO_REGRESSION_CHECK,
    FixUnderTest,
    decode,
    render_file_states_diff,
    validate_file_states_with_repro,
)
from apr_framework.repro.verifier import parse_json_object

OPERATOR_KEYS = ("arith", "comp", "obo", "bool", "negate", "return")
DETERMINISTIC_FAMILIES = ("operator", "drop", "revert")
LLM_PROBE_KINDS: dict[str, str] = {
    "symptom_suppression": (
        "Symptom suppression: hide the reported symptom (catch and swallow the "
        "error, special-case the visible output, clamp a value) instead of fixing "
        "its cause."
    ),
    "input_specific_shortcut": (
        "Input-specific shortcut: make the fix work only for inputs like the ones "
        "in the test (a hard-coded value, a check for one type or one length), so "
        "other inputs from the report's scenario still fail."
    ),
    "incomplete_fix": (
        "Incomplete fix: fix only part of the problem (one code path, one branch, "
        "the first call but not later ones)."
    ),
    "behaviour_substitution": (
        "Behaviour substitution: replace the correct behaviour with a different "
        "behaviour that happens to satisfy the test's assertions but is wrong for "
        "the report's scenario or breaks behaviour the report relies on."
    ),
}
_SYNTAX_SIGNATURES = ("SyntaxError", "IndentationError", "TabError")
_UNSPLICEABLE_STATEMENTS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
_FUNCTION_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)


@dataclass(frozen=True)
class ProbeOptions:
    max_probes: int = 8
    max_llm_probes: int = 12
    timeout_seconds: int = 300


# --------------------------------------------------------------- deterministic


def build_deterministic_probes(fix: FixUnderTest) -> list[Probe]:
    """Every distinct one-change variant of the fix, from all three families."""
    python_files = python_file_texts(fix.files)
    candidates: dict[tuple[str, str], Probe] = {}
    for relative_path, (original_text, fixed_text) in python_files.items():
        for probe in build_operator_and_drop_probes(
            relative_path, original_text, fixed_text
        ):
            candidates.setdefault((probe.path, probe.probe_text), probe)
    for probe in build_revert_probes(python_files):
        candidates.setdefault((probe.path, probe.probe_text), probe)
    return sorted(candidates.values(), key=probe_sort_key)


def python_file_texts(files: FileStates) -> dict[str, tuple[str, str]]:
    return {
        relative_path: (decode(original), decode(fixed))
        for relative_path, (original, fixed) in files.items()
        if relative_path.endswith(".py") and fixed is not None
    }


def build_operator_and_drop_probes(
    relative_path: str, original_text: str, fixed_text: str
) -> list[Probe]:
    try:
        fixed_tree = ast.parse(fixed_text)
    except SyntaxError:
        return []
    fixed_lines = fixed_text.splitlines(keepends=True)
    probes = []
    for line in lines_changed_by_fix(original_text, fixed_text):
        statement = innermost_statement(fixed_tree, line)
        if statement is None or not is_spliceable(statement, fixed_lines):
            continue
        renderings = operator_renderings(fixed_tree, statement, line) + [
            ("drop", "pass")
        ]
        for operator_key, rendering in renderings:
            probe_text = splice_statement(fixed_lines, statement, rendering)
            if probe_text == fixed_text or not parses(probe_text):
                continue
            probes.append(
                Probe(
                    probe_id="",
                    family="drop" if operator_key == "drop" else "operator",
                    kind=operator_key,
                    path=relative_path,
                    line=line,
                    before=fixed_lines[line - 1].strip()[:160],
                    after=first_changed_line(fixed_text, probe_text)[:160],
                    probe_text=probe_text,
                )
            )
    return probes


def operator_renderings(
    fixed_tree: ast.AST, statement: ast.stmt, line: int
) -> list[tuple[str, str]]:
    """``(operator, statement source)`` for every operator variant at ``line``."""
    original_rendering = ast.unparse(statement)
    renderings = []
    for operator_key in OPERATOR_KEYS:
        operator = get_operator_class(operator_key)(line)
        for variant_tree in operator.generate_variants(fixed_tree):
            mutated = matching_statement(variant_tree, statement)
            if mutated is None:
                continue
            mutated_rendering = ast.unparse(mutated)
            if mutated_rendering != original_rendering:
                renderings.append((operator_key, mutated_rendering))
    return renderings


def build_revert_probes(python_files: dict[str, tuple[str, str]]) -> list[Probe]:
    """The fix with one change block reverted, when the fix has two or more blocks."""
    blocks = [
        (relative_path, block)
        for relative_path, (original_text, fixed_text) in python_files.items()
        for block in change_blocks(original_text, fixed_text)
    ]
    if len(blocks) < 2:
        return []
    probes = []
    for relative_path, block in blocks:
        original_text, fixed_text = python_files[relative_path]
        probe_text = revert_change_block(original_text, fixed_text, block)
        if probe_text == fixed_text or not parses(probe_text):
            continue
        _, _, _, fixed_start, _ = block
        probes.append(
            Probe(
                probe_id="",
                family="revert",
                kind="revert",
                path=relative_path,
                line=fixed_start + 1,
                before=first_changed_line(probe_text, fixed_text)[:160],
                after=first_changed_line(fixed_text, probe_text)[:160],
                probe_text=probe_text,
            )
        )
    return probes


def change_blocks(
    original_text: str, fixed_text: str
) -> list[tuple[str, int, int, int, int]]:
    """The non-equal opcodes between the buggy and the fixed file (line based)."""
    matcher = difflib.SequenceMatcher(
        None, original_text.splitlines(), fixed_text.splitlines(), autojunk=False
    )
    return [opcode for opcode in matcher.get_opcodes() if opcode[0] != "equal"]


def revert_change_block(original_text: str, fixed_text: str, block) -> str:
    _, original_start, original_end, fixed_start, fixed_end = block
    original_lines = original_text.splitlines(keepends=True)
    fixed_lines = fixed_text.splitlines(keepends=True)
    return "".join(
        fixed_lines[:fixed_start]
        + original_lines[original_start:original_end]
        + fixed_lines[fixed_end:]
    )


def lines_changed_by_fix(original_text: str, fixed_text: str) -> list[int]:
    """1-based line numbers, on the fixed side, that the fix added or replaced."""
    changed: set[int] = set()
    for tag, _, _, fixed_start, fixed_end in change_blocks(original_text, fixed_text):
        if tag in ("replace", "insert"):
            changed.update(range(fixed_start + 1, fixed_end + 1))
    return sorted(changed)


def innermost_statement(tree: ast.AST, line: int) -> ast.stmt | None:
    candidates = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.stmt)
        and node.lineno <= line <= (node.end_lineno or node.lineno)
    ]
    if not candidates:
        return None
    return min(
        candidates, key=lambda node: (node.end_lineno or node.lineno) - node.lineno
    )


def is_spliceable(statement: ast.stmt, fixed_lines: list[str]) -> bool:
    """Whole lines only: the statement must own its first and last line."""
    if isinstance(statement, _UNSPLICEABLE_STATEMENTS):
        return False
    first_line = fixed_lines[statement.lineno - 1]
    last_line = fixed_lines[(statement.end_lineno or statement.lineno) - 1]
    starts_line = first_line[: statement.col_offset].strip() == ""
    rest_of_last_line = last_line.encode("utf-8")[
        statement.end_col_offset or 0 :
    ].decode("utf-8", errors="replace")
    ends_line = rest_of_last_line.strip() == "" or rest_of_last_line.strip().startswith(
        "#"
    )
    return starts_line and ends_line


def matching_statement(variant_tree: ast.AST, statement: ast.stmt) -> ast.stmt | None:
    for node in ast.walk(variant_tree):
        if (
            type(node) is type(statement)
            and getattr(node, "lineno", None) == statement.lineno
            and getattr(node, "col_offset", None) == statement.col_offset
            and getattr(node, "end_lineno", None) == statement.end_lineno
        ):
            return node
    return None


def splice_statement(
    fixed_lines: list[str], statement: ast.stmt, rendering: str
) -> str:
    first = statement.lineno - 1
    last = statement.end_lineno or statement.lineno
    indent = leading_whitespace(fixed_lines[first])
    replacement = "".join(f"{indent}{text}\n" for text in rendering.splitlines())
    return "".join(fixed_lines[:first]) + replacement + "".join(fixed_lines[last:])


def select_probes(probes: list[Probe], limit: int, seed: str) -> list[Probe]:
    """Up to ``limit`` probes, taken round-robin across families, shuffled per family."""
    if len(probes) <= limit:
        return probes
    generator = random.Random(seed)
    by_family: dict[str, list[Probe]] = {}
    for probe in probes:
        by_family.setdefault(probe.family, []).append(probe)
    for family_probes in by_family.values():
        generator.shuffle(family_probes)
    chosen: list[Probe] = []
    while len(chosen) < limit:
        for family in sorted(by_family):
            if by_family[family] and len(chosen) < limit:
                chosen.append(by_family[family].pop())
    return sorted(chosen, key=probe_sort_key)


# --------------------------------------------------------------- LLM probes


def generate_llm_probes(
    fix: FixUnderTest,
    test: TestVersion,
    issue: ReproIssue,
    client: MeteredLLMClient,
    max_probes: int,
) -> list[Probe]:
    """Ask for up to ``max_probes`` test-aware wrong fixes, spread over the 4 kinds."""
    if max_probes <= 0:
        return []
    fixed_functions = functions_touched_by_fix(fix)
    if not fixed_functions:
        return []
    per_kind = distribute_evenly(max_probes, len(LLM_PROBE_KINDS))
    probes: list[Probe] = []
    with client.purpose("llm_probes"):
        for (kind, description), count in zip(LLM_PROBE_KINDS.items(), per_kind):
            if count == 0:
                continue
            response_text = client.complete(
                build_llm_probe_messages(
                    fix, fixed_functions, test, issue, kind, description, count
                )
            )
            probes.extend(
                parse_llm_probes(response_text, kind, fix, fixed_functions)[:count]
            )
    return deduplicate_probes(probes)


@dataclass(frozen=True)
class FixedFunction:
    """A function of the fixed code that the fix changed."""

    path: str
    name: str
    first_line: int  # 1-based, decorators included
    last_line: int
    source: str


def functions_touched_by_fix(fix: FixUnderTest) -> list[FixedFunction]:
    """The innermost function around every changed line, on the fixed side."""
    functions: dict[tuple[str, int], FixedFunction] = {}
    for relative_path, (original_text, fixed_text) in python_file_texts(
        fix.files
    ).items():
        try:
            tree = ast.parse(fixed_text)
        except SyntaxError:
            continue
        fixed_lines = fixed_text.splitlines(keepends=True)
        for line in lines_changed_by_fix(original_text, fixed_text):
            node = innermost_function(tree, line)
            if node is None:
                continue
            first_line = min([node.lineno] + [d.lineno for d in node.decorator_list])
            last_line = node.end_lineno or node.lineno
            functions.setdefault(
                (relative_path, first_line),
                FixedFunction(
                    path=relative_path,
                    name=node.name,
                    first_line=first_line,
                    last_line=last_line,
                    source="".join(fixed_lines[first_line - 1 : last_line]),
                ),
            )
    return list(functions.values())


def innermost_function(tree: ast.AST, line: int):
    candidates = [
        node
        for node in ast.walk(tree)
        if isinstance(node, _FUNCTION_NODES)
        and node.lineno <= line <= (node.end_lineno or node.lineno)
    ]
    if not candidates:
        return None
    return min(
        candidates, key=lambda node: (node.end_lineno or node.lineno) - node.lineno
    )


def build_llm_probe_messages(
    fix: FixUnderTest,
    fixed_functions: list[FixedFunction],
    test: TestVersion,
    issue: ReproIssue,
    kind: str,
    description: str,
    count: int,
) -> list[dict[str, str]]:
    rendered_functions = "\n\n".join(
        f"# file: {function.path}  function: {function.name}\n{function.source}"
        for function in fixed_functions
    )
    return [
        {"role": "system", "content": load_prompt("probe_system")},
        {
            "role": "user",
            "content": render_prompt(
                "probe_user",
                issue_title=issue.title,
                issue_body=issue.prompt_body,
                fix_diff=fix.diff_text,
                fixed_functions=rendered_functions,
                test_source=test.source,
                count=count,
                kind_name=kind,
                kind_description=description,
            ),
        },
    ]


def parse_llm_probes(
    response_text: str,
    kind: str,
    fix: FixUnderTest,
    fixed_functions: list[FixedFunction],
) -> list[Probe]:
    """Turn the model's JSON variants into probes (malformed variants are dropped)."""
    payload = parse_json_object(response_text) or {}
    variants = payload.get("variants") if isinstance(payload, dict) else None
    probes = []
    for variant in variants if isinstance(variants, list) else []:
        if not isinstance(variant, dict):
            continue
        function = find_fixed_function(
            fixed_functions,
            str(variant.get("file", "")),
            str(variant.get("function", "")),
        )
        code = str(variant.get("code", ""))
        if function is None or not code.strip():
            continue
        _, fixed_bytes = fix.files[function.path]
        fixed_text = decode(fixed_bytes)
        probe_text = splice_function(fixed_text, function, code)
        if probe_text is None or probe_text == fixed_text or not parses(probe_text):
            continue
        probes.append(
            Probe(
                probe_id="",
                family="llm",
                kind=kind,
                path=function.path,
                line=function.first_line,
                before=f"def {function.name}(...) as fixed",
                after=first_changed_line(fixed_text, probe_text)[:160],
                probe_text=probe_text,
                rationale=str(variant.get("rationale", ""))[:300],
            )
        )
    return probes


def find_fixed_function(
    fixed_functions: list[FixedFunction], path: str, name: str
) -> FixedFunction | None:
    by_name = [function for function in fixed_functions if function.name == name]
    for function in by_name:
        if function.path == path or path.endswith(function.path):
            return function
    return by_name[0] if len(by_name) == 1 else None


def splice_function(fixed_text: str, function: FixedFunction, code: str) -> str | None:
    """Replace the function's lines with ``code``, re-indented to its position."""
    fixed_lines = fixed_text.splitlines(keepends=True)
    indent = leading_whitespace(fixed_lines[function.first_line - 1])
    code_lines = strip_common_indentation(code).splitlines()
    if not code_lines:
        return None
    replacement = "".join(
        (f"{indent}{line}" if line.strip() else "") + "\n" for line in code_lines
    )
    return (
        "".join(fixed_lines[: function.first_line - 1])
        + replacement
        + "".join(fixed_lines[function.last_line :])
    )


def strip_common_indentation(code: str) -> str:
    lines = code.replace("\t", "    ").splitlines()
    indents = [len(line) - len(line.lstrip()) for line in lines if line.strip()]
    margin = min(indents) if indents else 0
    return "\n".join(line[margin:] for line in lines)


def deduplicate_probes(probes: list[Probe]) -> list[Probe]:
    seen: set[tuple[str, str]] = set()
    unique = []
    for probe in probes:
        key = (probe.path, probe.probe_text)
        if key in seen:
            continue
        seen.add(key)
        unique.append(probe)
    return unique


def distribute_evenly(total: int, buckets: int) -> list[int]:
    return [
        total // buckets + (1 if index < total % buckets else 0)
        for index in range(buckets)
    ]


# --------------------------------------------------------------- running probes


def number_probes(probes: list[Probe]) -> list[Probe]:
    """Give every probe a stable id ``p01``, ``p02``, ... in order."""
    return [
        replace(probe, probe_id=f"p{index:02d}")
        for index, probe in enumerate(probes, start=1)
    ]


def run_probes(
    probes: list[Probe],
    fix: FixUnderTest,
    test: TestVersion,
    runner: TestCommandRunner,
    build_guard: Callable[[], EvaluatorGuard],
    regression_context: RegressionContext,
    oracle_usable: bool,
    timeout_seconds: int,
) -> list[ProbeResult]:
    """Validate every probe once: oracle verdict and repro verdict in one pass."""
    guard = build_guard()
    return [
        run_one_probe(
            probe,
            fix,
            test,
            runner,
            guard,
            regression_context,
            oracle_usable,
            timeout_seconds,
        )
        for probe in probes
    ]


def run_one_probe(
    probe: Probe,
    fix: FixUnderTest,
    test: TestVersion,
    runner: TestCommandRunner,
    guard: EvaluatorGuard,
    regression_context: RegressionContext,
    oracle_usable: bool,
    timeout_seconds: int,
) -> ProbeResult:
    files = probe_file_states(probe, fix.files)
    outcome, repro_run = validate_file_states_with_repro(
        files, runner, guard, regression_context, test, timeout_seconds
    )
    oracle_verdict, confirmed_by, detail = classify_probe_oracle_outcome(
        outcome, runner.has_oracle, runner.oracle_label, oracle_usable
    )
    return ProbeResult(
        probe=probe,
        oracle_verdict=oracle_verdict,
        confirmed_by=confirmed_by,
        oracle_detail=detail,
        repro_verdict=describe_repro_verdict(outcome, repro_run),
        diff_text=render_probe_diff(probe, fix),
        full_diff_text=render_file_states_diff(files),
    )


def rerun_repro_on_probes(
    previous_results: list[ProbeResult],
    fix: FixUnderTest,
    test: TestVersion,
    runner: TestCommandRunner,
    build_guard: Callable[[], EvaluatorGuard],
    timeout_seconds: int,
) -> list[ProbeResult]:
    """Re-judge the same probe pool with a new test; oracle verdicts are kept.

    The oracle's verdict on a probe does not depend on the repro test, so the
    regression comparison is skipped here; the trigger still runs (the shared
    validation path always runs it).
    """
    guard = build_guard()
    rerun_results = []
    for previous in previous_results:
        if previous.oracle_verdict == "invalid":
            rerun_results.append(previous)
            continue
        outcome, repro_run = validate_file_states_with_repro(
            probe_file_states(previous.probe, fix.files),
            runner,
            guard,
            NO_REGRESSION_CHECK,
            test,
            timeout_seconds,
        )
        rerun_results.append(
            ProbeResult(
                probe=previous.probe,
                oracle_verdict=previous.oracle_verdict,
                confirmed_by=previous.confirmed_by,
                oracle_detail=previous.oracle_detail,
                repro_verdict=describe_repro_verdict(outcome, repro_run),
                diff_text=previous.diff_text,
                full_diff_text=previous.full_diff_text,
            )
        )
    return rerun_results


def classify_probe_oracle_outcome(
    outcome: ValidationOutcome,
    has_oracle: bool,
    oracle_label: str,
    oracle_usable: bool,
) -> tuple[str, str, str]:
    """(verdict, confirmed_by, detail) under the confirmation rule (module docstring)."""
    trigger_run = outcome.trigger_test_run_result
    trigger_output = trigger_run.raw_output or ""
    if not outcome.applied or outcome.integrity.is_rejected:
        return (
            "invalid",
            "",
            outcome.integrity.detail or outcome.apply_error or "not applied",
        )
    if any(signature in trigger_output for signature in _SYNTAX_SIGNATURES) and (
        not outcome.trigger_passed
    ):
        return "invalid", "", "the probed code does not import"
    if not has_oracle:
        return "unconfirmed", "", "no oracle (pass --regression-cmd in local mode)"
    if not oracle_usable:
        return "unconfirmed", "", "the oracle does not pass on the fix itself"
    if not outcome.trigger_passed:
        if trigger_run.return_code == TIMEOUT_RETURN_CODE:
            return "unconfirmed", "", "the oracle timed out (not evidence)"
        if trigger_run.total_count == 0:
            return "unconfirmed", "", "the oracle produced no result"
        return "confirmed_wrong", oracle_label, f"fails the {oracle_label}"
    if outcome.regression_new_failures:
        return (
            "confirmed_wrong",
            "test_file",
            "passes the trigger, breaks "
            + ", ".join(outcome.regression_new_failures[:3]),
        )
    if outcome.regression_untrusted:
        return "unconfirmed", "", "passes the oracle; the test-file run was unusable"
    return "unconfirmed", "", "passes every oracle check (possibly equivalent)"


def describe_repro_verdict(outcome: ValidationOutcome, repro_run) -> str:
    if not outcome.applied or repro_run is None:
        return "not_run"
    return "accepts" if is_passing_trigger_run(repro_run) else "rejects"


def probe_file_states(probe: Probe, fix_files: FileStates) -> FileStates:
    """The fix's file states with the probe's file replaced."""
    states = dict(fix_files)
    original, _ = states[probe.path]
    states[probe.path] = (
        original,
        probe.probe_text.encode("utf-8", errors="surrogateescape"),
    )
    return states


def render_probe_diff(probe: Probe, fix: FixUnderTest) -> str:
    """The probe as a diff against the fixed file (what a reviewer reads)."""
    _, fixed_bytes = fix.files[probe.path]
    return render_file_states_diff(
        {
            probe.path: (
                fixed_bytes,
                probe.probe_text.encode("utf-8", errors="surrogateescape"),
            )
        }
    )


# --------------------------------------------------------------- helpers


def probe_sort_key(probe: Probe) -> tuple[str, int, str, str]:
    return (probe.path, probe.line, probe.family, probe.kind)


def parses(source: str) -> bool:
    try:
        ast.parse(source)
    except SyntaxError:
        return False
    return True


def first_changed_line(old_text: str, new_text: str) -> str:
    for old_line, new_line in zip(old_text.splitlines(), new_text.splitlines()):
        if old_line != new_line:
            return new_line.strip()
    return ""


def leading_whitespace(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]
