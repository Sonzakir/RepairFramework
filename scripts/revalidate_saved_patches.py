"""Re-validate a committed run's saved plausible patches against the current gate.

Committed ``repair_results.json`` files record the verdicts of the validation path
*at the time they ran*. This script replays each saved plausible LLM patch through
today's ``LLMRepairAlgorithm.validate_patch`` (evaluator-integrity screening,
trigger test, regression check, evaluator verification) and prints the recorded
and current verdict side by side. It writes nothing and makes no LLM calls.

    python scripts/revalidate_saved_patches.py <run_dir> [<run_dir> ...]

Unlike the other scripts here it needs the executor: run it inside the framework
container, with each run's bug checked out and compiled
(``python -m apr_framework bugsinpy test <project> <bug>``).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from apr_framework.benchmarks.registry import create_bugsinpy_adapter  # noqa: E402
from apr_framework.core.exceptions import BenchmarkError  # noqa: E402
from apr_framework.core.models import (  # noqa: E402
    BugIdentifier,
    CheckoutResult,
    LocalizationResult,
    PatchCandidate,
    RepairAttemptResult,
)
from apr_framework.repair.llm import LLMRepairAlgorithm, LLMRepairConfig  # noqa: E402
from apr_framework.repair.llm.client import LLMClient  # noqa: E402


class _NoLLMClient(LLMClient):
    """Validation never queries a model; fail loudly if anything tries to."""

    def complete(self, messages: list[dict[str, str]]) -> str:
        raise RuntimeError("revalidate_saved_patches makes no LLM calls")


def main(run_dirs: list[str]) -> int:
    if not run_dirs:
        print(__doc__)
        return 2
    for run_dir in run_dirs:
        revalidate_run(Path(run_dir))
    return 0


def revalidate_run(run_dir: Path) -> None:
    """Print recorded vs current verdicts for every saved plausible patch of a run."""
    results = json.loads((run_dir / "repair_results.json").read_text("utf-8"))
    bug = BugIdentifier("bugsinpy", results["project"], int(results["bug_id"]))
    checkout = resolve_compiled_checkout(bug)
    algorithm = build_validation_only_algorithm(bug, results.get("config", {}))

    print(f"\n== {run_dir} ({bug.project}#{bug.bug_id}) ==")
    for saved_patch in results.get("plausible_patches", []):
        candidate = PatchCandidate(
            bug=checkout.bug,
            patch_id=saved_patch["patch_id"],
            summary=saved_patch.get("summary") or "",
            diff_text=saved_patch["diff_text"],
        )
        current_result = algorithm.validate_patch(checkout.bug, checkout, candidate)
        print_verdict_row(saved_patch, current_result)


def resolve_compiled_checkout(bug: BugIdentifier) -> CheckoutResult:
    project_root = Path.cwd()
    worktree = (
        project_root
        / ".workspace"
        / "bugsinpy"
        / f"{bug.project}_{bug.bug_id}"
        / bug.project
    )
    if not (worktree / "bugsinpy_compile_flag").is_file():
        raise BenchmarkError(
            f"{bug.project}#{bug.bug_id} is not checked out and compiled at {worktree}. "
            f"Run `python -m apr_framework bugsinpy test {bug.project} {bug.bug_id}`."
        )
    return CheckoutResult(bug=bug, worktree=worktree, success=True, prepared=True)


def build_validation_only_algorithm(
    bug: BugIdentifier, recorded_config: dict
) -> LLMRepairAlgorithm:
    repair_config = LLMRepairConfig(
        model_name=str(recorded_config.get("model", "replay")),
        timeout_seconds=int(recorded_config.get("timeout_per_test", 120)),
        regression_check=bool(recorded_config.get("regression_check", True)),
    )
    return LLMRepairAlgorithm(
        localization_result=LocalizationResult(
            bug=bug, backend="none", ranked_locations=[]
        ),
        adapter=create_bugsinpy_adapter(Path.cwd()),
        repair_config=repair_config,
        llm_client=_NoLLMClient(),
    )


def print_verdict_row(saved_patch: dict, current_result: RepairAttemptResult) -> None:
    metadata = current_result.patch.metadata if current_result.patch else {}
    print(
        f"{saved_patch['patch_id']:<14} recorded={saved_patch['status']:<9} "
        f"current={current_result.status.value:<9} "
        f"integrity={metadata.get('integrity', '-')} "
        f"paths={metadata.get('integrity_paths', [])} "
        f"regression_checked={metadata.get('regression_checked', '-')}"
    )
    print(f"{'':<14} {current_result.validation_summary}")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
