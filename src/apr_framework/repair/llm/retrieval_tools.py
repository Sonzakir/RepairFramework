"""Static code retrieval tools for LLM repair prompts."""

from __future__ import annotations

import ast
from pathlib import Path

from apr_framework.core.models import CheckoutResult
from apr_framework.repair.llm.retrieval_protocol import RetrieveCommand

_MAX_TOOL_RESULT_CHARS = 12_000
_MAX_USAGE_COUNT = 50
_SKIPPED_DIRECTORY_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "site-packages",
        "venv",
    }
)


def get_function_definition(function_name: str, checkout: CheckoutResult) -> str:
    """Return source for matching functions or methods in the checked-out project."""
    try:
        matches: list[str] = []
        for python_file_path in _iter_python_file_paths(checkout.worktree):
            source_text = _read_source_text(python_file_path)
            if source_text is None:
                continue
            try:
                syntax_tree = ast.parse(source_text, filename=str(python_file_path))
            except SyntaxError:
                continue
            source_lines = source_text.splitlines(keepends=True)
            for syntax_node in ast.walk(syntax_tree):
                if not isinstance(syntax_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if syntax_node.name != function_name:
                    continue
                matches.append(
                    _format_source_match(
                        checkout.worktree,
                        python_file_path,
                        syntax_node.lineno,
                        syntax_node.end_lineno,
                        source_lines,
                    )
                )

        if not matches:
            return f'No function or method named "{function_name}" found.'
        return _truncate_for_prompt("\n\n".join(matches))
    except Exception as error:  # noqa: BLE001 - retrieval must not abort repair
        return f'Could not retrieve function "{function_name}": {error}'


def get_class_definition(class_name: str, checkout: CheckoutResult) -> str:
    """Return source for matching class definitions in the checked-out project."""
    try:
        matches: list[str] = []
        for python_file_path in _iter_python_file_paths(checkout.worktree):
            source_text = _read_source_text(python_file_path)
            if source_text is None:
                continue
            try:
                syntax_tree = ast.parse(source_text, filename=str(python_file_path))
            except SyntaxError:
                continue
            source_lines = source_text.splitlines(keepends=True)
            for syntax_node in ast.walk(syntax_tree):
                if not isinstance(syntax_node, ast.ClassDef):
                    continue
                if syntax_node.name != class_name:
                    continue
                matches.append(
                    _format_source_match(
                        checkout.worktree,
                        python_file_path,
                        syntax_node.lineno,
                        syntax_node.end_lineno,
                        source_lines,
                    )
                )

        if not matches:
            return f'No class named "{class_name}" found.'
        return _truncate_for_prompt("\n\n".join(matches))
    except Exception as error:  # noqa: BLE001 - retrieval must not abort repair
        return f'Could not retrieve class "{class_name}": {error}'


def find_usages(symbol_name: str, checkout: CheckoutResult) -> str:
    """Return reference sites for a symbol in the checked-out project."""
    try:
        usage_lines: list[str] = []
        seen_locations: set[tuple[str, int]] = set()

        for python_file_path in _iter_python_file_paths(checkout.worktree):
            if len(usage_lines) >= _MAX_USAGE_COUNT:
                break
            source_text = _read_source_text(python_file_path)
            if source_text is None:
                continue
            source_lines = source_text.splitlines()
            try:
                syntax_tree = ast.parse(source_text, filename=str(python_file_path))
            except SyntaxError:
                _collect_text_usages(
                    symbol_name,
                    checkout.worktree,
                    python_file_path,
                    source_lines,
                    usage_lines,
                    seen_locations,
                )
                continue

            for syntax_node in ast.walk(syntax_tree):
                if len(usage_lines) >= _MAX_USAGE_COUNT:
                    break
                if not _is_symbol_reference(syntax_node, symbol_name):
                    continue
                line_number = getattr(syntax_node, "lineno", None)
                if line_number is None:
                    continue
                _append_usage_line(
                    checkout.worktree,
                    python_file_path,
                    line_number,
                    source_lines,
                    usage_lines,
                    seen_locations,
                )

        if not usage_lines:
            return f'No usages of "{symbol_name}" found.'

        header = f'Usages of "{symbol_name}" (up to {_MAX_USAGE_COUNT}):'
        return _truncate_for_prompt("\n".join([header, *usage_lines]))
    except Exception as error:  # noqa: BLE001 - retrieval must not abort repair
        return f'Could not find usages of "{symbol_name}": {error}'


def execute_retrieval_command(
    retrieve_command: RetrieveCommand, checkout: CheckoutResult
) -> str:
    """Dispatch a parsed retrieval command to the matching static-analysis tool."""
    if retrieve_command.tool_name == "get_function_definition":
        return get_function_definition(retrieve_command.argument, checkout)
    if retrieve_command.tool_name == "get_class_definition":
        return get_class_definition(retrieve_command.argument, checkout)
    if retrieve_command.tool_name == "find_usages":
        return find_usages(retrieve_command.argument, checkout)
    return f"Unknown retrieval tool: {retrieve_command.tool_name}"


def _truncate_for_prompt(
    result_text: str, max_character_count: int = _MAX_TOOL_RESULT_CHARS
) -> str:
    """Cap tool output so one retrieval cannot dominate the repair prompt."""
    if len(result_text) <= max_character_count:
        return result_text
    return (
        result_text[:max_character_count].rstrip()
        + f"\n\n[truncated after {max_character_count} characters]"
    )


def _iter_python_file_paths(worktree_path: Path) -> list[Path]:
    python_file_paths: list[Path] = []
    for candidate_path in sorted(worktree_path.rglob("*.py")):
        if any(part in _SKIPPED_DIRECTORY_NAMES for part in candidate_path.parts):
            continue
        if candidate_path.is_file():
            python_file_paths.append(candidate_path)
    return python_file_paths


def _read_source_text(python_file_path: Path) -> str | None:
    try:
        return python_file_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        try:
            return python_file_path.read_text(encoding="latin-1")
        except OSError:
            return None
    except OSError:
        return None


def _format_source_match(
    worktree_path: Path,
    python_file_path: Path,
    start_line_number: int,
    end_line_number: int | None,
    source_lines: list[str],
) -> str:
    if end_line_number is None:
        end_line_number = start_line_number
    source_slice = "".join(source_lines[start_line_number - 1 : end_line_number])
    return (
        f"# {_relative_path_str(worktree_path, python_file_path)}:{start_line_number}\n"
        f"{source_slice.rstrip()}"
    )


def _is_symbol_reference(syntax_node: ast.AST, symbol_name: str) -> bool:
    if isinstance(syntax_node, ast.Name):
        return syntax_node.id == symbol_name
    if isinstance(syntax_node, ast.Attribute):
        return syntax_node.attr == symbol_name
    if isinstance(syntax_node, ast.Call):
        function_node = syntax_node.func
        if isinstance(function_node, ast.Name):
            return function_node.id == symbol_name
        if isinstance(function_node, ast.Attribute):
            return function_node.attr == symbol_name
    return False


def _collect_text_usages(
    symbol_name: str,
    worktree_path: Path,
    python_file_path: Path,
    source_lines: list[str],
    usage_lines: list[str],
    seen_locations: set[tuple[str, int]],
) -> None:
    for line_offset, source_line in enumerate(source_lines):
        if len(usage_lines) >= _MAX_USAGE_COUNT:
            return
        if symbol_name not in source_line:
            continue
        _append_usage_line(
            worktree_path,
            python_file_path,
            line_offset + 1,
            source_lines,
            usage_lines,
            seen_locations,
        )


def _append_usage_line(
    worktree_path: Path,
    python_file_path: Path,
    line_number: int,
    source_lines: list[str],
    usage_lines: list[str],
    seen_locations: set[tuple[str, int]],
) -> None:
    relative_file_path_str = _relative_path_str(worktree_path, python_file_path)
    location_key = (relative_file_path_str, line_number)
    if location_key in seen_locations:
        return
    seen_locations.add(location_key)
    snippet = (
        source_lines[line_number - 1].strip()
        if line_number <= len(source_lines)
        else ""
    )
    usage_lines.append(f"{relative_file_path_str}:{line_number}: {snippet}")


def _relative_path_str(worktree_path: Path, python_file_path: Path) -> str:
    try:
        return python_file_path.relative_to(worktree_path).as_posix()
    except ValueError:
        return python_file_path.as_posix()
