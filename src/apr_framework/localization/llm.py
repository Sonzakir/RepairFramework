"""LLM-based fault localization backend (Assignment 5, Task 1).

An alternative to the FauxPy SBFL/MBFL localizers: instead of instrumenting and
running the test suite, this backend asks an LLM to read the failing test, its
error output, and the relevant source, and to return a ranked list of suspicious
``(file, line)`` pairs. It implements the same ``FaultLocalizer`` interface and
emits the same ``RankedLocation`` format, so it is a drop-in replacement anywhere
the pipeline consumes localization results.

The failure evidence (failing-test source + traceback on the unpatched checkout)
is gathered by reusing ``build_failing_test_context`` from the repair side, and the
LLM transport is the shared ``OpenAICompatibleClient``. The only LLM-FL-specific
logic here is source selection (which files/lines to show the model) and parsing
the model's JSON answer back into ``RankedLocation`` objects.
"""

import ast
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from apr_framework.core.exceptions import ConfigurationError
from apr_framework.core.models import (
    BugIdentifier,
    CheckoutResult,
    LocalizationResult,
    RankedLocation,
    TestRunResult,
)
from apr_framework.localization.base import FaultLocalizer
from apr_framework.repair.llm.client import LLMClient

if TYPE_CHECKING:
    from apr_framework.benchmarks.bugsinpy import BugsInPyAdapter

logger = logging.getLogger(__name__)

_PROMPTS_DIRECTORY_PATH = Path(__file__).parent / "prompts"
_VALID_LLM_PROVIDERS: frozenset[str] = frozenset({"openai-compatible"})

# Matches a CPython traceback frame line: '  File "<path>", line <n>, in <fn>'.
_TRACEBACK_FRAME_PATTERN = re.compile(r'File "([^"]+)", line (\d+)')

# Matches a fenced code block so a JSON answer wrapped in ``` fences can be recovered.
_FENCED_BLOCK_PATTERN = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


@dataclass
class LLMLocalizationConfig:
    """Configuration for the LLM fault-localization backend.

    The first four fields are exactly the ``LLMConnectionConfig`` Protocol the shared
    LLM client depends on, so an instance can be passed straight to
    ``OpenAICompatibleClient``.

    Fields:
        model_name:          Model identifier sent to the API.
        temperature:         Sampling temperature in [0.0, 2.0]. Low values give more
                             stable rankings; some models (e.g. gpt-5.*) require 1.0.
        base_url:            API endpoint URL. None → GPT@RUB default.
        api_key_env_var:     Environment variable holding the API key.
        llm_provider:        Client implementation to use (currently openai-compatible).
        system_prompt_name:  File stem under localization/prompts/ for the system message.
        top_n:               Keep only the top-N ranked locations (None → keep all).
        max_source_lines:    Upper bound on numbered source lines shown to the model.
        source_window_lines: Lines of context shown above/below each traceback line.
        timeout_seconds:     Wall-clock limit for the one trigger-test run used to
                             capture the traceback.
    """

    model_name: str
    temperature: float = 0.0
    base_url: str | None = None
    api_key_env_var: str = "GPT_AT_RUB_API_KEY"
    llm_provider: str = "openai-compatible"
    system_prompt_name: str = "fl_prompt1"
    top_n: int | None = None
    max_source_lines: int = 400
    source_window_lines: int = 40
    timeout_seconds: int = 120

    def __post_init__(self) -> None:
        if not (0.0 <= self.temperature <= 2.0):
            raise ConfigurationError(
                f"temperature must be in [0.0, 2.0], got {self.temperature}"
            )
        if self.llm_provider not in _VALID_LLM_PROVIDERS:
            raise ConfigurationError(
                f"Unknown llm_provider {self.llm_provider!r}. "
                f"Valid options: {sorted(_VALID_LLM_PROVIDERS)}"
            )
        if self.top_n is not None and self.top_n < 1:
            raise ConfigurationError(f"top_n must be >= 1 or None, got {self.top_n}")
        if self.max_source_lines < 1:
            raise ConfigurationError(
                f"max_source_lines must be >= 1, got {self.max_source_lines}"
            )
        if self.source_window_lines < 0:
            raise ConfigurationError(
                f"source_window_lines must be >= 0, got {self.source_window_lines}"
            )
        # Fail fast if the named prompt file is missing, rather than at first LLM call.
        load_fl_system_prompt(self.system_prompt_name)


def load_fl_system_prompt(prompt_name: str) -> str:
    """Load a fault-localization system prompt from localization/prompts/ by stem.

    Args:
        prompt_name: File stem (without ``.txt``), e.g. ``"fl_prompt1"``.

    Returns:
        The prompt text with surrounding whitespace stripped.

    Raises:
        ConfigurationError: If no matching file exists.
    """
    prompt_file_path = _PROMPTS_DIRECTORY_PATH / f"{prompt_name}.txt"
    if not prompt_file_path.is_file():
        available_prompt_names = sorted(
            candidate_path.stem
            for candidate_path in _PROMPTS_DIRECTORY_PATH.glob("*.txt")
        )
        raise ConfigurationError(
            f"FL system prompt {prompt_name!r} not found at {prompt_file_path}. "
            f"Available prompts: {available_prompt_names}"
        )
    return prompt_file_path.read_text(encoding="utf-8").strip()


class LLMFaultLocalizer(FaultLocalizer):
    """Fault localizer that ranks suspicious lines with an LLM 

    Constructed with the LLM config, a completed ``LLMClient`` transport, and the
    benchmark adapter (needed only to run the trigger test once for its traceback,
    mirroring ``PerfectFaultLocalizer(adapter)``).
    """

    def __init__(
        self,
        config: LLMLocalizationConfig,
        client: LLMClient,
        adapter: "BugsInPyAdapter",
    ) -> None:
        self._config = config
        self._client = client
        self._adapter = adapter

    @property
    def name(self) -> str:
        return "llm-fl"

    def localize(
        self,
        bug: BugIdentifier,
        checkout: CheckoutResult,
        test_result: TestRunResult | None = None,
    ) -> LocalizationResult:
        """Rank suspicious locations for a bug by prompting the LLM.

        Runs the trigger test once to capture its traceback, selects the project
        source referenced by that traceback, asks the model to rank suspicious lines,
        and parses the JSON answer into ``RankedLocation`` objects.

        Args:
            bug:         Bug identifier.
            checkout:    Checked-out worktree (source is read from here).
            test_result: Accepted for ABC compatibility; ignored — the localizer runs
                         its own trigger test to obtain a fresh traceback.

        Returns:
            A ``LocalizationResult`` with ``backend="llm-fl"`` and the ranked lines.
        """
        failing_test_source, error_traceback = self._gather_failure_evidence(checkout)
        source_sections = self._select_source_sections(checkout, error_traceback)
        messages = self._build_localization_messages(
            source_sections, failing_test_source, error_traceback
        )
        response_text = self._client.complete(messages)
        known_relative_paths = [relative_path for relative_path, _ in source_sections]
        ranked_locations = parse_llm_fl_response(
            response_text, known_relative_paths, self._config.top_n
        )
        return self._build_localization_result(
            bug, ranked_locations, source_sections, error_traceback, response_text
        )

    # ------------------------------------------------------------------ evidence

    def _gather_failure_evidence(
        self, checkout: CheckoutResult
    ) -> tuple[str | None, str | None]:
        """Return the failing-test source and its traceback, reusing the repair helper."""
        # Imported lazily so importing the localization package does not pull in the
        # benchmark/Docker stack unless LLM-FL is actually used.
        from apr_framework.repair.llm.context_enricher import build_failing_test_context

        failure_context = build_failing_test_context(
            self._adapter,
            checkout,
            enabled=True,
            timeout=self._config.timeout_seconds,
        )
        if failure_context.error_traceback is None:
            logger.warning(
                "LLM-FL: no traceback captured for %s#%d; the model will localize "
                "from the test body alone.",
                checkout.bug.project,
                checkout.bug.bug_id,
            )
        return failure_context.failing_test_source, failure_context.error_traceback

    # -------------------------------------------------------------- source select

    def _select_source_sections(
        self, checkout: CheckoutResult, error_traceback: str | None
    ) -> list[tuple[str, str]]:
        """Choose and render the project source the model should read.

        Two complementary signals decide which source to show:

          1. **Traceback frames** — project files/lines in the failure traceback. This
             is enough for bugs whose failure is an exception thrown from the source.
          2. **Test-referenced symbols** — for assertion-style failures (e.g. a mocked
             dependency forced to raise), the traceback holds only the test frame, so
             the buggy source never appears. We recover it by anchoring windows on the
             project symbols the failing test imports, patches, or calls.

        Non-test source is rendered before test source, and each file is shown as
        line-numbered windows around its lines of interest, capped by ``max_source_lines``.
        """
        lines_of_interest_by_file = self._collect_project_frames(
            checkout.worktree, error_traceback
        )
        self._augment_with_test_referenced_source(
            lines_of_interest_by_file, checkout.worktree
        )
        if not lines_of_interest_by_file:
            return []

        remaining_line_budget = self._config.max_source_lines
        source_sections: list[tuple[str, str]] = []
        for source_file_path in _non_test_source_first(lines_of_interest_by_file):
            if remaining_line_budget <= 0:
                break
            numbered_source = _render_numbered_windows(
                source_file_path,
                lines_of_interest_by_file[source_file_path],
                self._config.source_window_lines,
                remaining_line_budget,
            )
            if numbered_source is None:
                continue
            relative_path = _to_worktree_relative(source_file_path, checkout.worktree)
            source_sections.append((relative_path, numbered_source))
            remaining_line_budget -= numbered_source.count("\n") + 1
        return source_sections

    def _augment_with_test_referenced_source(
        self, lines_of_interest_by_file: dict[Path, set[int]], worktree: Path
    ) -> None:
        """Add source windows anchored on project symbols the failing test references.

        Reads the *whole* failing-test file (imports and decorators included, which the
        function body alone omits), extracts the project modules it imports and the
        symbols it accesses/patches on them, then anchors windows on where those
        symbols occur in the corresponding project source files. Mutates
        ``lines_of_interest_by_file`` in place. Best-effort: any failure is a no-op.
        """
        failing_test_target = _find_failing_test_target(worktree)
        if failing_test_target is None:
            return
        test_file_path, test_method_name = failing_test_target
        try:
            test_source = test_file_path.read_text(encoding="utf-8")
        except OSError:
            return

        module_files, referenced_symbols = _extract_project_modules_and_symbols(
            test_source, worktree, test_method_name
        )
        for module_file_path in module_files:
            anchor_lines = _grep_symbol_lines(module_file_path, referenced_symbols)
            if anchor_lines:
                lines_of_interest_by_file.setdefault(module_file_path, set()).update(
                    anchor_lines
                )

    def _collect_project_frames(
        self, worktree: Path, error_traceback: str | None
    ) -> dict[Path, set[int]]:
        """Map each in-project source file in the traceback to its referenced lines.

        Frames pointing outside the worktree (stdlib, site-packages) are dropped so
        only the project's own source is offered to the model. Insertion order follows
        first appearance in the traceback.
        """
        lines_of_interest_by_file: dict[Path, set[int]] = {}
        if not error_traceback:
            return lines_of_interest_by_file

        for frame_path_str, frame_line_str in _TRACEBACK_FRAME_PATTERN.findall(
            error_traceback
        ):
            resolved_file_path = _resolve_under_worktree(worktree, frame_path_str)
            if resolved_file_path is None:
                continue
            lines_of_interest_by_file.setdefault(resolved_file_path, set()).add(
                int(frame_line_str)
            )
        return lines_of_interest_by_file

    # --------------------------------------------------------------- prompt build

    def _build_localization_messages(
        self,
        source_sections: list[tuple[str, str]],
        failing_test_source: str | None,
        error_traceback: str | None,
    ) -> list[dict[str, str]]:
        """Assemble the [system, user] messages for the localization request."""
        system_prompt = load_fl_system_prompt(self._config.system_prompt_name)
        user_message = _render_user_message(
            source_sections, failing_test_source, error_traceback
        )
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]

    # --------------------------------------------------------------- result build

    def _build_localization_result(
        self,
        bug: BugIdentifier,
        ranked_locations: list[RankedLocation],
        source_sections: list[tuple[str, str]],
        error_traceback: str | None,
        response_text: str,
    ) -> LocalizationResult:
        """Wrap the ranked locations and provenance metadata into a LocalizationResult."""
        return LocalizationResult(
            bug=bug,
            backend="llm-fl",
            ranked_locations=ranked_locations,
            metadata={
                "fl_backend": "llm",
                "model": self._config.model_name,
                "temperature": self._config.temperature,
                "system_prompt": self._config.system_prompt_name,
                "score_formula": "llm-rank: descending synthetic score by model order",
                "files_shown": [relative_path for relative_path, _ in source_sections],
                "had_traceback": error_traceback is not None,
                "location_count": len(ranked_locations),
                "raw_llm_response": response_text,
            },
        )


# ---------------------------------------------------------------------------
# Module-level helpers (source selection: test-referenced symbols)
# ---------------------------------------------------------------------------

_MAX_LINES_PER_SYMBOL = 5


def _is_test_source_file(source_file_path: Path) -> bool:
    """True if the path looks like a test file (test_*.py / *_test.py / under tests/)."""
    file_name = source_file_path.name
    if file_name.startswith("test_") or file_name.endswith("_test.py"):
        return True
    return "tests" in source_file_path.parts


def _non_test_source_first(
    lines_of_interest_by_file: dict[Path, set[int]],
) -> list[Path]:
    """Order the candidate files so non-test source is rendered before test files."""
    return sorted(
        lines_of_interest_by_file, key=lambda file_path: _is_test_source_file(file_path)
    )


def _find_failing_test_target(worktree: Path) -> tuple[Path, str] | None:
    """Resolve the first pytest target's ``(file_path, method_name)`` from run_test.sh.

    Returns None when the script is missing/unparseable or the file does not resolve.
    The method name has any pytest parametrization bracket stripped.
    """
    from apr_framework.localization.fauxpy import load_pytest_targets

    run_test_script_path = worktree / "bugsinpy_run_test.sh"
    if not run_test_script_path.is_file():
        return None
    try:
        pytest_targets = load_pytest_targets(run_test_script_path)
    except Exception:
        return None
    if not pytest_targets:
        return None
    first_target = pytest_targets[0]
    test_file_relpath = first_target.split("::")[0]
    test_method_name = first_target.split("::")[-1].split("[")[0]
    test_file_path = worktree / test_file_relpath
    if not test_file_path.is_file():
        return None
    return test_file_path.resolve(), test_method_name


def _extract_project_modules_and_symbols(
    test_source: str, worktree: Path, test_method_name: str
) -> tuple[list[Path], set[str]]:
    """From a test file, find imported project source files and the symbols it targets.

    Only *high-signal* symbols are returned: the dependencies the test explicitly
    manipulates or imports by name. Collecting every ``module.attr`` access floods a
    large source file with anchors and buries the true fault beyond the line budget,
    so those are deliberately excluded.

    Parses the test with ``ast`` and returns:
      - the project source files it imports (modules resolving to a file under
        ``worktree``; stdlib/third-party imports resolve to nothing and are dropped);
      - the symbol names it targets: leaves of dotted string literals passed to
        ``patch``-like calls (e.g. ``patch("black.ProcessPoolExecutor")``).

    Only patched/mocked symbols are treated as anchors. These are the dependencies
    the test deliberately manipulates to trigger the bug, so their usage sites in the
    source are the fault region. Broader signals (every imported name, every attribute
    access) were tried but flood a large file with early anchors that push the true
    fault past the line budget.
    """
    try:
        parsed_test = ast.parse(test_source)
    except SyntaxError:
        return [], set()

    local_name_to_module_file: dict[str, Path] = {}
    targeted_symbols: set[str] = set()

    for node in ast.walk(parsed_test):
        if isinstance(node, ast.Import):
            for alias in node.names:
                module_file_path = _resolve_module_source_file(worktree, alias.name)
                if module_file_path is not None:
                    local_name_to_module_file[
                        (alias.asname or alias.name).split(".")[0]
                    ] = module_file_path
        elif isinstance(node, ast.ImportFrom) and node.module:
            module_file_path = _resolve_module_source_file(worktree, node.module)
            if module_file_path is not None:
                local_name_to_module_file[node.module.split(".")[0]] = module_file_path

    # Scope patch-target extraction to the *failing* test method only. A test file
    # holds many methods, each patching different dependencies; collecting patch
    # targets from all of them would flood the source with irrelevant anchors.
    failing_method_node = _find_function_node(parsed_test, test_method_name)
    patch_search_root = failing_method_node or parsed_test
    for node in ast.walk(patch_search_root):
        if isinstance(node, ast.Call) and _is_patch_like_call(node):
            targeted_symbols.update(
                _patch_target_symbols(node, local_name_to_module_file)
            )

    # De-duplicate module files while preserving discovery order.
    unique_module_files: list[Path] = []
    for module_file_path in local_name_to_module_file.values():
        if module_file_path not in unique_module_files:
            unique_module_files.append(module_file_path)
    return unique_module_files, targeted_symbols


def _find_function_node(
    parsed_module: ast.Module, function_name: str
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """Return the first function/method node with the given name, or None."""
    for node in ast.walk(parsed_module):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == function_name
        ):
            return node
    return None


def _is_patch_like_call(node: ast.AST) -> bool:
    """True if the node is a call to ``patch`` / ``mock.patch`` / ``patch.object``."""
    if not isinstance(node, ast.Call):
        return False
    call_target = node.func
    if isinstance(call_target, ast.Name):
        return call_target.id == "patch"
    if isinstance(call_target, ast.Attribute):
        return call_target.attr in {"patch", "object"}
    return False


def _patch_target_symbols(
    call_node: ast.Call, local_name_to_module_file: dict[str, Path]
) -> set[str]:
    """Extract project-symbol leaves from the string/name arguments of a patch call.

    Handles both ``patch("black.ProcessPoolExecutor")`` (dotted string whose head is a
    project module) and ``patch.object(black, "ProcessPoolExecutor")`` (module name
    positional followed by an attribute string).
    """
    targeted_symbols: set[str] = set()
    positional_arguments = call_node.args
    for argument_index, argument_node in enumerate(positional_arguments):
        if not (
            isinstance(argument_node, ast.Constant)
            and isinstance(argument_node.value, str)
        ):
            continue
        literal_value = argument_node.value
        if "." in literal_value:
            head_segment = literal_value.split(".")[0]
            leaf_segment = literal_value.split(".")[-1]
            if (
                head_segment in local_name_to_module_file
                and leaf_segment.isidentifier()
            ):
                targeted_symbols.add(leaf_segment)
        elif argument_index > 0 and literal_value.isidentifier():
            # patch.object(<module>, "<attr>") — a bare attribute name argument.
            preceding_argument = positional_arguments[argument_index - 1]
            if (
                isinstance(preceding_argument, ast.Name)
                and preceding_argument.id in local_name_to_module_file
            ):
                targeted_symbols.add(literal_value)
    return targeted_symbols


def _resolve_module_source_file(worktree: Path, module_name: str) -> Path | None:
    """Resolve a dotted module name to a project source file under ``worktree``, or None."""
    module_path_parts = module_name.split(".")
    candidate_paths = [
        worktree.joinpath(*module_path_parts).with_suffix(".py"),
        worktree.joinpath(*module_path_parts, "__init__.py"),
    ]
    for candidate_path in candidate_paths:
        if candidate_path.is_file():
            return candidate_path.resolve()
    return None


def _grep_symbol_lines(source_file_path: Path, symbols: set[str]) -> set[int]:
    """Return line numbers in a file where any of the given symbols occurs as a word.

    Each symbol contributes at most ``_MAX_LINES_PER_SYMBOL`` lines so a common name
    cannot flood the anchor set.
    """
    if not symbols:
        return set()
    try:
        source_lines = source_file_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return set()

    symbol_pattern = re.compile(
        r"\b(?:" + "|".join(re.escape(symbol) for symbol in symbols) + r")\b"
    )
    anchor_lines: set[int] = set()
    per_symbol_counts: dict[str, int] = {}
    for line_index, source_line in enumerate(source_lines, start=1):
        for matched_symbol in symbol_pattern.findall(source_line):
            if per_symbol_counts.get(matched_symbol, 0) >= _MAX_LINES_PER_SYMBOL:
                continue
            anchor_lines.add(line_index)
            per_symbol_counts[matched_symbol] = (
                per_symbol_counts.get(matched_symbol, 0) + 1
            )
    return anchor_lines


# ---------------------------------------------------------------------------
# Module-level helpers (source rendering + response parsing)
# ---------------------------------------------------------------------------


def _resolve_under_worktree(worktree: Path, frame_path_str: str) -> Path | None:
    """Resolve a traceback file path to a real file under ``worktree``, or None.

    Traceback paths may be absolute container paths, so a direct join is unreliable.
    We match by progressively shorter path suffixes against the worktree, which also
    naturally excludes stdlib/site-packages frames (no suffix resolves under it).
    """
    normalized_parts = Path(frame_path_str).parts
    for start_index in range(len(normalized_parts)):
        suffix_candidate = Path(*normalized_parts[start_index:])
        resolved = worktree / suffix_candidate
        if resolved.is_file():
            return resolved.resolve()
    return None


def _to_worktree_relative(source_file_path: Path, worktree: Path) -> str:
    """Return ``source_file_path`` relative to ``worktree`` (falls back to name)."""
    try:
        return str(source_file_path.resolve().relative_to(worktree.resolve()))
    except ValueError:
        return source_file_path.name


def _render_numbered_windows(
    source_file_path: Path,
    lines_of_interest: set[int],
    window_lines: int,
    max_lines: int,
) -> str | None:
    """Render line-numbered windows around the lines of interest, capped at max_lines.

    Overlapping windows are merged; non-adjacent windows are separated by an elision
    marker so the model can tell the regions apart. Returns None if the file cannot
    be read.
    """
    try:
        source_lines = source_file_path.read_text(encoding="utf-8").splitlines()
    except OSError as read_error:
        logger.warning("LLM-FL: cannot read %s: %s", source_file_path, read_error)
        return None

    merged_intervals = _merge_line_windows(
        lines_of_interest, window_lines, len(source_lines)
    )
    rendered_blocks: list[str] = []
    emitted_line_count = 0
    for interval_start, interval_end in merged_intervals:
        if emitted_line_count >= max_lines:
            break
        for line_number in range(interval_start, interval_end + 1):
            if emitted_line_count >= max_lines:
                break
            rendered_blocks.append(f"{line_number:5d}| {source_lines[line_number - 1]}")
            emitted_line_count += 1
        rendered_blocks.append("     | ...")
    # Drop the trailing elision marker for a clean tail.
    if rendered_blocks and rendered_blocks[-1] == "     | ...":
        rendered_blocks.pop()
    return "\n".join(rendered_blocks)


def _merge_line_windows(
    lines_of_interest: set[int], window_lines: int, total_lines: int
) -> list[tuple[int, int]]:
    """Turn a set of interesting lines into merged, clamped [start, end] intervals."""
    if not lines_of_interest:
        return []
    raw_intervals = sorted(
        (
            max(1, line - window_lines),
            min(total_lines, line + window_lines),
        )
        for line in sorted(lines_of_interest)
    )
    merged_intervals: list[tuple[int, int]] = [raw_intervals[0]]
    for interval_start, interval_end in raw_intervals[1:]:
        last_start, last_end = merged_intervals[-1]
        if interval_start <= last_end + 1:
            merged_intervals[-1] = (last_start, max(last_end, interval_end))
        else:
            merged_intervals.append((interval_start, interval_end))
    return merged_intervals


def _render_user_message(
    source_sections: list[tuple[str, str]],
    failing_test_source: str | None,
    error_traceback: str | None,
) -> str:
    """Compose the user message: source sections, failing test, and error output."""
    message_blocks: list[str] = []

    if source_sections:
        rendered_sources = "\n\n".join(
            f"### FILE: {relative_path}\n{numbered_source}"
            for relative_path, numbered_source in source_sections
        )
        message_blocks.append("## Source files\n" + rendered_sources)
    else:
        message_blocks.append(
            "## Source files\n(No in-project source could be resolved from the "
            "traceback; localize from the failing test and error output below.)"
        )

    if failing_test_source:
        message_blocks.append(
            "## Failing test\n```python\n" + failing_test_source + "\n```"
        )

    if error_traceback:
        message_blocks.append("## Error output\n```\n" + error_traceback + "\n```")

    message_blocks.append(
        "Return the ranked suspicious lines as the JSON array described above."
    )
    return "\n\n".join(message_blocks)


def parse_llm_fl_response(
    response_text: str,
    known_relative_paths: list[str],
    top_n: int | None,
) -> list[RankedLocation]:
    """Parse the model's JSON answer into ranked locations 

    Extracts a JSON array of ``{"file", "line", "reason"}`` objects, normalizes each
    file path against the paths actually shown to the model, and builds
    ``RankedLocation`` objects with descending synthetic scores. Malformed entries are
    skipped; a completely unparseable response yields an empty list (logged).

    Args:
        response_text:        Raw model reply.
        known_relative_paths: Worktree-relative paths that were shown to the model,
                              used to normalize returned paths.
        top_n:                Keep only the first N locations (None → keep all).

    Returns:
        Ranked locations in the model's order, re-numbered from rank 1.
    """
    parsed_entries = _extract_json_array(response_text)
    if parsed_entries is None:
        logger.warning("LLM-FL: could not parse a JSON array from the model response.")
        return []

    ranked_locations: list[RankedLocation] = []
    for entry in parsed_entries:
        location = _entry_to_ranked_location(
            entry, len(ranked_locations), known_relative_paths
        )
        if location is not None:
            ranked_locations.append(location)

    if top_n is not None:
        ranked_locations = ranked_locations[:top_n]
    return ranked_locations


def _extract_json_array(response_text: str) -> list | None:
    """Best-effort recovery of a JSON array from a model reply, or None."""
    for candidate_text in _json_array_candidates(response_text):
        try:
            parsed = json.loads(candidate_text)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, list):
            return parsed
    return None


def _json_array_candidates(response_text: str) -> list[str]:
    """Yield substrings of the reply that might be the JSON array, best first."""
    candidates: list[str] = [response_text.strip()]
    for fenced_match in _FENCED_BLOCK_PATTERN.findall(response_text):
        candidates.append(fenced_match.strip())
    first_bracket = response_text.find("[")
    last_bracket = response_text.rfind("]")
    if first_bracket != -1 and last_bracket > first_bracket:
        candidates.append(response_text[first_bracket : last_bracket + 1])
    return candidates


def _entry_to_ranked_location(
    entry: object,
    zero_based_rank: int,
    known_relative_paths: list[str],
) -> RankedLocation | None:
    """Convert one parsed JSON object into a RankedLocation, or None if malformed."""
    if not isinstance(entry, dict):
        return None
    raw_file = entry.get("file")
    raw_line = entry.get("line")
    if not isinstance(raw_file, str) or not isinstance(raw_line, (int, str)):
        return None
    try:
        line_number = int(raw_line)
    except (TypeError, ValueError):
        return None

    normalized_file_path = _normalize_returned_file(raw_file, known_relative_paths)
    reason_text = entry.get("reason")
    return RankedLocation(
        rank=zero_based_rank + 1,
        file_path=normalized_file_path,
        location=f"{normalized_file_path}:{line_number}",
        score=max(0.0, 1.0 - zero_based_rank * 0.01),
        line=line_number,
        metadata={"reason": reason_text} if isinstance(reason_text, str) else {},
    )


def _normalize_returned_file(raw_file: str, known_relative_paths: list[str]) -> str:
    """Map a model-returned path onto one of the shown paths when possible.

    Prefers an exact match, then the shown path that is the longest suffix match of
    the returned path (or vice-versa). Falls back to the returned path with any
    ``a/``/``b/`` diff prefix stripped.
    """
    cleaned = raw_file.strip()
    if cleaned in known_relative_paths:
        return cleaned
    if cleaned.startswith(("a/", "b/")):
        cleaned = cleaned[2:]
    if cleaned in known_relative_paths:
        return cleaned
    for known_path in known_relative_paths:
        if cleaned.endswith(known_path) or known_path.endswith(cleaned):
            return known_path
    return cleaned
