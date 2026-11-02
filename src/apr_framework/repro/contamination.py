"""Contamination flag: how close is the generated test to the developer's trigger test?

BugsInPy is old and public, so a model may recall the developer's test. The
framework (never the model) reads the trigger test functions named in the
checkout's ``bugsinpy_run_test.sh`` and compares them with the generated test:
the score is the highest token-sequence similarity (0.0-1.0) between any
generated test function and any trigger test function, both normalised by
``ast.unparse`` (comments and formatting removed). A score at or above
``NEAR_COPY_THRESHOLD`` marks the test a near-copy; reports give results with and
without near-copies.
"""

import ast
import difflib
import re
from pathlib import Path

NEAR_COPY_THRESHOLD = 0.8
_TOKEN = re.compile(r"\w+|[^\w\s]")


def read_developer_trigger_functions(worktree: Path) -> list[str]:
    """Source of every test function the trigger command names (pytest node ids)."""
    script = worktree / "bugsinpy_run_test.sh"
    if not script.is_file():
        return []
    functions: list[str] = []
    for token in script.read_text(encoding="utf-8").split():
        if "::" not in token:
            continue
        file_part, *name_parts = token.split("::")
        test_file = worktree / file_part
        if test_file.is_file() and name_parts:
            functions.extend(
                find_function_sources(
                    test_file.read_text("utf-8", "replace"), name_parts
                )
            )
    return functions


def find_function_sources(source: str, name_parts: list[str]) -> list[str]:
    """``[Class, method]`` or ``[function]`` (a ``[param]`` suffix is ignored)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    wanted = name_parts[-1].split("[", 1)[0]
    return [
        ast.unparse(node)
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == wanted
    ]


def generated_test_functions(source: str) -> list[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    return [
        ast.unparse(node)
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test")
    ]


def max_function_similarity(
    generated_source: str, reference_functions: list[str]
) -> float | None:
    """Highest similarity between a generated and a reference test function."""
    generated_functions = generated_test_functions(generated_source)
    if not generated_functions or not reference_functions:
        return None
    return max(
        token_similarity(generated, reference)
        for generated in generated_functions
        for reference in reference_functions
    )


def token_similarity(left: str, right: str) -> float:
    left_tokens = _TOKEN.findall(strip_function_name(left))
    right_tokens = _TOKEN.findall(strip_function_name(right))
    return difflib.SequenceMatcher(
        None, left_tokens, right_tokens, autojunk=False
    ).ratio()


def strip_function_name(function_source: str) -> str:
    """Names differ by construction; compare bodies and signatures only."""
    return re.sub(
        r"^(async\s+)?def\s+\w+",
        r"\1def _",
        function_source,
        count=1,
        flags=re.MULTILINE,
    )
