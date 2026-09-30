import re
import shlex
import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, overload

from apr_framework.core.exceptions import ConfigurationError
from apr_framework.core.models import (
    BugIdentifier,
    CheckoutResult,
    LocalizationResult,
    RankedLocation,
    TestRunResult,
)
from apr_framework.localization.base import FaultLocalizer

FAUXPY_INSTALL_REQUIREMENT = "fauxpy==0.7.0"

_SCRIPTS_DIR = Path(__file__).parent / "scripts"
_FAUXPY_SBFL_METRICS_PATCH_SCRIPT = (
    _SCRIPTS_DIR / "fauxpy_sbfl_metrics_patch.py"
).read_text(encoding="utf-8")
_FAUXPY_MBFL_SELECTION_PATCH_SCRIPT = (
    _SCRIPTS_DIR / "fauxpy_mbfl_selection_patch.py"
).read_text(encoding="utf-8")


def _completed_output(completed: subprocess.CompletedProcess[str]) -> str:
    return "\n".join(
        part
        for part in [
            (completed.stdout or "").strip(),
            (completed.stderr or "").strip(),
        ]
        if part
    )


def _validate_string_list(name: str, values: list[str]) -> None:
    if not isinstance(values, list):
        raise ConfigurationError(f"FauxPy {name} must be a list of strings")
    if any(not isinstance(value, str) for value in values):
        raise ConfigurationError(f"FauxPy {name} must contain only strings.")


def _parse_int_from_output(pattern: str, raw_output: str) -> int | None:
    match = re.search(pattern, raw_output)
    if match is None:
        return None
    return int(match.group(1))


def _parse_float_from_output(pattern: str, raw_output: str) -> float | None:
    match = re.search(pattern, raw_output)
    if match is None:
        return None
    return float(match.group(1))


def _list_fauxpy_report_dirs(worktree: Path) -> dict[Path, Path]:
    parent = worktree.parent
    if not parent.exists():
        return {}
    return {
        path.resolve(): path for path in parent.glob("FauxPyReport_*") if path.is_dir()
    }


def _find_new_fauxpy_report_dir(
    before: dict[Path, Path], after: dict[Path, Path]
) -> Path | None:
    new_paths = [path for key, path in after.items() if key not in before]
    candidates = new_paths or list(after.values())
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def _query_mbfl_validation_metadata(report_dir: Path | None) -> dict[str, int | str]:
    if report_dir is None:
        return {}

    db_path = report_dir / "fauxpy.db"
    if not db_path.exists():
        return {"fauxpy_report_dir": str(report_dir)}

    metadata: dict[str, int | str] = {"fauxpy_report_dir": str(report_dir)}
    try:
        with sqlite3.connect(str(db_path)) as connection:
            cursor = connection.cursor()
            cursor.execute("SELECT COUNT(*) FROM MutantInfo")
            metadata["mutants_validated"] = int(cursor.fetchone()[0])
            cursor.execute("SELECT COUNT(*) FROM MutantInfo WHERE Timeout = 1")
            metadata["mutants_timed_out"] = int(cursor.fetchone()[0])
            cursor.execute("SELECT COUNT(*) FROM MutantInfo WHERE HasMissingTests = 1")
            metadata["mutants_with_missing_tests"] = int(cursor.fetchone()[0])
    except sqlite3.Error:
        return metadata

    return metadata


def extract_mbfl_tracking_metadata(
    raw_output: str, report_dir: Path | None = None
) -> dict[str, int | float | str]:
    """Extract MBFL cost-control metadata from FauxPy output and report DB."""
    metadata: dict[str, int | float | str] = {}
    output_patterns = {
        "candidate_mutation_locations": r"Candidate mutation locations:\s+(\d+)",
        "selected_mutation_locations": r"Selected mutation locations:\s+(\d+)",
        "mutants_generated": r"Total generated mutants:\s+(\d+)",
        "mutants_validated": r"Mutants selected for validation:\s+(\d+)",
    }

    for key, pattern in output_patterns.items():
        value = _parse_int_from_output(pattern, raw_output)
        if value is not None:
            metadata[key] = value

    if "mutants_validated" not in metadata:
        running_count = _parse_int_from_output(r"Running\s+(\d+)\s+Mutants", raw_output)
        if running_count is not None:
            metadata["mutants_validated"] = running_count

    generated_counts = [
        int(match)
        for match in re.findall(r"Number of generated mutants:\s+(\d+)", raw_output)
    ]
    if generated_counts and "mutants_generated" not in metadata:
        metadata["mutants_generated"] = sum(generated_counts)

    time_patterns = {
        "mutation_generation_time_seconds": r"Mutation generation time:\s+([0-9]+(?:\.[0-9]+)?)",
        "mutant_validation_time_seconds": r"Mutant validation time:\s+([0-9]+(?:\.[0-9]+)?)",
    }
    for key, pattern in time_patterns.items():
        value = _parse_float_from_output(pattern, raw_output)
        if value is not None:
            metadata[key] = value

    for key, value in _query_mbfl_validation_metadata(report_dir).items():
        metadata.setdefault(key, value)
    return metadata


def _translate_unittest_target(target: str) -> str:
    parts = target.split(".")
    selector_start = None

    for index, part in enumerate(parts):
        if part[:1].isupper():
            selector_start = index
            break

    if selector_start is None and len(parts) > 2 and parts[-1].startswith("test"):
        selector_start = len(parts) - 1

    module_parts = parts if selector_start is None else parts[:selector_start]
    selector_parts = [] if selector_start is None else parts[selector_start:]

    module = "/".join(module_parts) + ".py"
    if not selector_parts:
        return module

    return module + "::" + "::".join(selector_parts)


def _unittest_targets(parts: list[str]) -> list[str]:
    """
    Helper method to convert arguments from unit tests into test targets 
    """
    targets: list[str] = []
    skip_next = False
    options_with_values = {"-k", "--pattern"}

    for part in parts:
        if skip_next:
            skip_next = False
            continue
        if part in options_with_values:
            skip_next = True
            continue
        if part.startswith("-"):
            continue
        if part == "discover":
            raise ConfigurationError(
                "FauxPy localization does not support unittest discover commands."
            )

        targets.append(_translate_unittest_target(part))

    return targets


def load_pytest_targets(run_test_script: Path) -> list[str]:
    """Return pytest targets declared by a BugsInPy ``run_test.sh`` script.

    The parser accepts direct ``pytest`` commands, ``python -m pytest`` commands,
    and targeted ``python -m unittest`` commands that pytest can execute after
    translation to node IDs. It skips comments and blank lines, and raises
    ``ConfigurationError`` when the script is missing, contains unsupported
    commands, or has no runnable pytest targets.
    """
    if not run_test_script.exists():
        raise ConfigurationError(f"No BugsInPy test script found at {run_test_script}")

    targets: list[str] = []
    # In script some bugs has multiple tests (node IDs)
    for raw_line in run_test_script.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        # split each command like shell
        parts = shlex.split(line)

        if not parts:
            continue
        # direct pytest calls (pytest tests/test.py tests/test_test.py)
        if parts[0] == "pytest":
            targets.extend(parts[1:])
            continue
        # python -m pytest style calls
        if len(parts) >= 3 and parts[1:3] == ["-m", "pytest"]:
            targets.extend(parts[3:])
            continue
        # python -m unittest style calls
        if len(parts) >= 3 and parts[1:3] == ["-m", "unittest"]:
            targets.extend(_unittest_targets(parts[3:]))
            continue

        raise ConfigurationError(
            "FauxPy localization currently supports BugsInPy run_test.sh lines "
            "that invoke pytest directly or targeted unittest commands. "
            f"Unsupported line: {line}"
        )

    if not targets:
        raise ConfigurationError(f"No pytest targets found in {run_test_script}")

    return targets


def extract_pytest_node_ids(test_targets: list[str]) -> list[str]:
    """Filter out pytest CLI flags (e.g. -q, -s), keeping only test node IDs."""
    return [
        test_target for test_target in test_targets if not test_target.startswith("-")
    ]


@dataclass(frozen=True)
class FauxPyConfig:
    """Configuration for one FauxPy localization run.

    The config describes the fault-localization family, source root, granularity,
    test selection, and metric to expose as the primary ranking. Validation keeps
    unsupported combinations out of the toolchain before any subprocess runs.
    """

    family: str = "sbfl"  # sbfl | mbfl
    granularity: str = "statement"  # statement | function
    src: str = "."
    exclude: list[str] = field(default_factory=list)
    test_targets: list[str] = field(default_factory=list)
    failing_tests: list[str] = field(default_factory=list)
    top_n: int | None = None
    # MBFL-only knobs
    mutation_strategy: str | None = None
    mutation_budget: int | None = None
    mutation_seed: int = 0
    # Metric selection
    metric: str | None = None
    # WSBI alpha — weight applied to passing-test coverage (0 < alpha <= 1)
    wsbi_alpha: float = 0.5

    def __post_init__(self) -> None:
        """Validate FauxPy options and reject unsupported combinations early."""
        if self.family not in {"sbfl", "mbfl"}:
            raise ConfigurationError(
                "Currently the FauxPy integration supports only SBFL and MBFL."
            )
        if self.granularity not in {"statement", "function"}:
            raise ConfigurationError(
                "Currently the FauxPy integration only supports statement level and function level granularity"
            )
        if (
            self.mutation_strategy is not None or self.mutation_budget is not None
        ) and self.family != "mbfl":
            raise ConfigurationError("Mutation selection options are MBFL only.")
        if self.mutation_strategy is not None:
            if self.mutation_strategy != "random":
                raise ConfigurationError(
                    "Currently the FauxPy integration supports only the random mutation selection strategy."
                )
            if self.mutation_budget is None:
                raise ConfigurationError(
                    "FauxPy mutation selection requires a positive mutation budget."
                )
        if self.mutation_budget is not None:
            if not isinstance(self.mutation_budget, int) or self.mutation_budget <= 0:
                raise ConfigurationError(
                    "FauxPy mutation budget must be a positive integer."
                )
            if self.mutation_strategy is None:
                raise ConfigurationError(
                    "FauxPy mutation budget requires a mutation strategy."
                )
        if not isinstance(self.mutation_seed, int):
            raise ConfigurationError("FauxPy mutation seed must be an integer.")
        if self.metric is None:
            object.__setattr__(
                self,
                "metric",
                "metallaxis" if self.family == "mbfl" else "ochiai",
            )
        elif not isinstance(self.metric, str) or not self.metric.strip():
            raise ConfigurationError("FauxPy metric must be a non-empty string.")
        if not isinstance(self.wsbi_alpha, (int, float)) or self.wsbi_alpha <= 0:
            raise ConfigurationError("wsbi_alpha must be a positive number.")
        if not isinstance(self.src, str) or not self.src.strip():
            raise ConfigurationError("FauxPy src must be a non-empty string.")
        if self.top_n is not None:
            if not isinstance(self.top_n, int) or self.top_n <= 0:
                raise ConfigurationError(
                    "FauxPy top_n must be a positive integer or None."
                )

        _validate_string_list("test_targets", self.test_targets)
        _validate_string_list("failing_tests", self.failing_tests)


class DockerCommandRunner(Protocol):
    """Protocol for benchmark toolchains that execute commands in a checkout."""

    def run_command(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
        check: bool = True,
        capture_output: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        """Run ``args`` in the benchmark environment and return the completed process."""
        ...


class FauxPyToolchain:
    """Run FauxPy inside a prepared benchmark checkout.

    The toolchain installs FauxPy into the checkout virtual environment when
    needed, executes pytest with FauxPy options, parses all emitted metric
    tables, and returns a structured ``LocalizationResult`` for the configured
    primary metric.
    """

    def __init__(self, runner: DockerCommandRunner) -> None:
        """Create a toolchain using runner for all subprocess execution"""
        self._runner = runner

    def localize(
        self, config: FauxPyConfig, checkout: CheckoutResult
    ) -> LocalizationResult:
        """Run FauxPy for ``checkout`` and return ranked locations for ``config.metric``.

        The result metadata includes the raw command output, the selected score
        formula, the FauxPy run options, and all_metrics containing every
        parsed metric table for later consumers.
        """
        # Use a relative interpreter path because BugsInPy creates the venv in
        # its executor container; absolute venv symlinks can look broken here.
        python = Path("env") / "bin" / "python"
        host_python = checkout.worktree / python
        if not host_python.exists() and not host_python.is_symlink():
            raise ConfigurationError(
                f"No prepared virtual environment found at {host_python}. "
                "Run `python -m apr_framework bugsinpy compile "
                f"{checkout.bug.project} {checkout.bug.bug_id}` first."
            )

        self._ensure_patched_fauxpy_installed(python, checkout.worktree, config.wsbi_alpha)

        fauxpy_command = self._build_fauxpy_command(python, config)
        before_report_dirs = _list_fauxpy_report_dirs(checkout.worktree)

        completed = self._runner.run_command(
            fauxpy_command,
            cwd=checkout.worktree,
            check=False,
            capture_output=True,
        )
        raw_output = _completed_output(completed)
        report_dir = _find_new_fauxpy_report_dir(
            before_report_dirs,
            _list_fauxpy_report_dirs(checkout.worktree),
        )

        if completed.returncode not in {0, 1}:
            raise ConfigurationError(
                f"FauxPy localization command failed. {raw_output}".strip()
            )

        all_metrics = parse_fauxpy_output(raw_output, metric_filter=None)
        formula = _resolve_metric_name(all_metrics, config.metric)
        if formula is None:
            available_metrics = ", ".join(sorted(all_metrics)) or "none"
            raise ConfigurationError(
                f"Metric '{config.metric}' was not emitted by FauxPy. "
                f"Available metrics: {available_metrics}. "
                "If you requested Jaccard or WSBI, make sure the prepared checkout uses "
                "the framework's patched FauxPy installation."
            )

        ranked_locations = parse_fauxpy_output(
            raw_output,
            metric_filter=config.metric,
            top_n=config.top_n,
        )

        metadata = {
            "family": config.family,
            "src": config.src,
            "test_targets": list(config.test_targets),
            "score_formula": formula,
            "all_metrics": all_metrics,
            "raw_output": raw_output,
            "returncode": completed.returncode,
        }
        if config.family == "mbfl":
            metadata.update(
                {
                    "mutation_strategy": config.mutation_strategy,
                    "mutation_budget": config.mutation_budget,
                    "mutation_seed": config.mutation_seed,
                }
            )
            metadata.update(extract_mbfl_tracking_metadata(raw_output, report_dir))

        return LocalizationResult(
            bug=checkout.bug,
            backend="fauxpy",
            ranked_locations=ranked_locations,
            metadata=metadata,
        )

    def _ensure_patched_fauxpy_installed(self, python: Path, cwd: Path, wsbi_alpha: float) -> None:
        self._ensure_fauxpy_installed(python, cwd)
        self._apply_fauxpy_sbfl_metric_patch(python, cwd, wsbi_alpha)
        self._apply_fauxpy_mbfl_selection_patch(python, cwd)

    def _ensure_fauxpy_installed(self, python: Path, cwd: Path) -> None:
        show = self._runner.run_command(
            [str(python), "-m", "pip", "show", "fauxpy"],
            cwd=cwd,
            check=False,
            capture_output=True,
        )
        if show.returncode == 0:
            return

        self._upgrade_fauxpy_build_tools(python, cwd)

        install = self._runner.run_command(
            [str(python), "-m", "pip", "install", FAUXPY_INSTALL_REQUIREMENT],
            cwd=cwd,
            check=False,
            capture_output=True,
        )
        if install.returncode != 0:
            raise ConfigurationError(
                "FauxPy is not installed in the project environment and installation "
                f"failed. {_completed_output(install)}".strip()
            )

    def _apply_fauxpy_sbfl_metric_patch(self, python: Path, cwd: Path, wsbi_alpha: float) -> None:
        # Prepend the alpha value so the patch script can embed it in metric_wsbi.py.
        script = f"_WSBI_ALPHA = {wsbi_alpha!r}\n" + _FAUXPY_SBFL_METRICS_PATCH_SCRIPT
        patch = self._runner.run_command(
            [str(python), "-c", script],
            cwd=cwd,
            check=False,
            capture_output=True,
        )
        if patch.returncode != 0:
            raise ConfigurationError(
                "FauxPy is installed, but the framework could not apply the "
                f"custom SBFL metric patch. {_completed_output(patch)}".strip()
            )

    def _apply_fauxpy_mbfl_selection_patch(self, python: Path, cwd: Path) -> None:
        patch = self._runner.run_command(
            [str(python), "-c", _FAUXPY_MBFL_SELECTION_PATCH_SCRIPT],
            cwd=cwd,
            check=False,
            capture_output=True,
        )
        if patch.returncode != 0:
            raise ConfigurationError(
                "FauxPy is installed, but the framework could not apply the "
                f"MBFL selection patch. {_completed_output(patch)}".strip()
            )

    def _upgrade_fauxpy_build_tools(self, python: Path, cwd: Path) -> None:
        upgrade = self._runner.run_command(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--upgrade",
                "pip<25.1",
                "setuptools",
                "wheel",
                "packaging",
            ],
            cwd=cwd,
            check=False,
            capture_output=True,
        )
        if upgrade.returncode != 0:
            raise ConfigurationError(
                "FauxPy is not installed in the project environment and the "
                "framework could not upgrade the environment build tooling. "
                f"{_completed_output(upgrade)}".strip()
            )

    def _build_fauxpy_command(self, python: Path, config: FauxPyConfig):
        """
        Helper function to build FauxPy commands from FauxPy-Config object
        """
        cmd = [
            str(python),
            "-m",
            "pytest",
            *config.test_targets,
            "--src",
            config.src,
            "--family",
            config.family,
            "--granularity",
            config.granularity,
        ]
        if config.top_n is not None:
            cmd += ["--top-n", str(config.top_n)]
        if config.failing_tests:
            cmd += ["--failing-list", "[" + ",".join(config.failing_tests) + "]"]
        if config.family == "mbfl" and config.mutation_strategy is not None:
            cmd += [
                "--mutation-selection",
                config.mutation_strategy,
                "--mutation-budget",
                str(config.mutation_budget),
                "--mutation-seed",
                str(config.mutation_seed),
            ]
        for ex in config.exclude:
            cmd += ["--exclude", ex]
        return cmd


class FauxPyLocalizer(FaultLocalizer):
    """Fault-localizer adapter that delegates FauxPy execution to a toolchain."""

    def __init__(self, config: FauxPyConfig, toolchain: FauxPyToolchain) -> None:
        """Create a localizer with a fixed FauxPy config and execution toolchain."""
        self._config = config
        self._toolchain = toolchain

    @property
    def name(self) -> str:
        """Return the stable backend name used in localization results."""
        return "fauxpy"

    def localize(
        self,
        bug: BugIdentifier,
        checkout: CheckoutResult,
        test_result: TestRunResult | None = None,
    ) -> LocalizationResult:
        """Localize ``bug`` in ``checkout`` using the configured FauxPy metric.

        ``test_result`` is accepted to match the framework interface; FauxPy
        derives its test selection from ``FauxPyConfig`` and the prepared
        checkout instead.
        """
        return self._toolchain.localize(self._config, checkout)

# TODO: 
@overload
def parse_fauxpy_output(
    raw_output: str,
    *,
    metric_filter: None = None,
    top_n: int | None = None,
) -> dict[str, list[RankedLocation]]: ...


@overload
def parse_fauxpy_output(
    raw_output: str,
    *,
    metric_filter: str,
    top_n: int | None = None,
) -> list[RankedLocation]: ...


def parse_fauxpy_output(
    raw_output: str,
    *,
    metric_filter: str | None = None,
    top_n: int | None = None,
) -> dict[str, list[RankedLocation]] | list[RankedLocation]:
    """Parse FauxPy metric tables from pytest output.

    FauxPy emits one ASCII table per scoring metric. When metric_filter is
    None, this function returns every table as {metric_name: rows}.
    When metric_filter is set, it returns only that metric's ranked rows.
    Statement rows use File | Line | Score and function rows use
    File | Function | Line | Score.
    """
    metric_tables: dict[str, list[RankedLocation]] = {}
    current_metric: str | None = None
    current_rows: list[RankedLocation] | None = None
    formula_pattern = re.compile(r"Scores for (?P<formula>.+?)\s+\|")
    row_pattern = re.compile(
        r"^(?P<file>.+?)\s+\|"
        r"(?:\s+(?P<function>.+?)\s+\|)?"
        r"\s+(?P<line>\d+)(?:-(?P<end_line>\d+))?\s+\|"
        r"\s+(?P<score>[+-]?\d+(?:\.\d+)?)\s*$"
    )

    for raw_line in raw_output.splitlines():
        line = raw_line.rstrip()

        formula_match = formula_pattern.search(line)
        if formula_match:
            current_metric = formula_match.group("formula").strip()
            current_rows = metric_tables.setdefault(current_metric, [])
            continue

        if current_metric is None or current_rows is None:
            continue

        if (
            not line.strip()
            or line.lstrip().startswith("-")
            or line.strip().startswith("|")
        ):
            continue

        if line.strip().startswith("File"):
            continue

        row_match = row_pattern.match(line)
        if row_match is None:
            continue

        file_path = row_match.group("file").strip()
        function = row_match.group("function")
        function = function.strip() if function is not None else None
        line_number = int(row_match.group("line"))
        end_line = row_match.group("end_line")
        score = float(row_match.group("score"))
        current_rows.append(
            RankedLocation(
                rank=len(current_rows) + 1,
                file_path=file_path,
                location=(
                    f"{file_path}:{function}:{line_number}"
                    if function
                    else f"{file_path}:{line_number}"
                ),
                score=score,
                line=line_number,
                end_line=int(end_line) if end_line is not None else None,
                function=function,
                raw_location=line.strip(),
                metadata={"score_formula": current_metric},
            )
        )

    if metric_filter is None:
        if top_n is None:
            return metric_tables
        return {metric: rows[:top_n] for metric, rows in metric_tables.items()}

    selected_metric = _resolve_metric_name(metric_tables, metric_filter)
    if selected_metric is None:
        return []
    rows = metric_tables[selected_metric]
    if top_n is not None:
        return rows[:top_n]
    return rows


def _resolve_metric_name(
    metric_tables: dict[str, list[RankedLocation]], metric_filter: str
) -> str | None:
    normalized_filter = metric_filter.strip().casefold()
    for metric in metric_tables:
        if metric.casefold() == normalized_filter:
            return metric
    return None
