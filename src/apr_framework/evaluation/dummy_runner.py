import subprocess
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from apr_framework.benchmarks.base import BenchmarkAdapter
from apr_framework.core.models import (
    BugIdentifier,
    EvaluationResult,
    PatchCandidate,
    TestRunResult,
)
from apr_framework.evaluation.base import EvaluationRunner
from apr_framework.evaluation.run_writer import RunWriter
from apr_framework.localization.base import FaultLocalizer
from apr_framework.repair.base import RepairAlgorithm


DEFAULT_DUMMY_BUGS = [
    BugIdentifier("bugsinpy", "black", 1),
    BugIdentifier("bugsinpy", "black", 3),
    BugIdentifier("bugsinpy", "black", 23),
]


class DummyEvaluationRunner(EvaluationRunner):
    """
    End-to-end dummy evaluation runner for Task 3.
    """

    def __init__(
        self,
        project_root: Path,
        runs_dir: Path | str = "runs",
        seed: int | None = None,
    ) -> None:
        self._project_root = project_root
        self._runs_dir = self._resolve_runs_dir(runs_dir)
        self._seed = seed
        self._last_run_dir: Path | None = None

    @property
    def name(self) -> str:
        """
        Stable name written to dummy evaluation logs and config files.
        """
        return "dummy-evaluation-runner"

    @property
    def last_run_dir(self) -> Path | None:
        """
        Directory created for the most recent run, or `None` before `run` is called.
        """
        return self._last_run_dir

    def run(
        self,
        bugs: list[BugIdentifier],
        benchmark: BenchmarkAdapter,
        repair: RepairAlgorithm,
        localizer: FaultLocalizer | None = None,
    ) -> list[EvaluationResult]:
        """
        Run the dummy APR pipeline and write run artifacts to disk.

        For each bug, the runner checks out the project, prepares it, runs
        baseline tests, asks the repair algorithm for patches, optionally applies
        the first patch, and records final test results.

        Args:
            bugs: Bugs to evaluate.
            benchmark: Benchmark adapter used for checkout, compile, and tests.
            repair: Repair algorithm that supplies candidate patches.
            localizer: Currently accepted for interface compatibility but unused.

        Returns:
            Evaluation results summarizing the status and timestamps for each bug.
        """
        writer = RunWriter.create(self._runs_dir)
        self._last_run_dir = writer.run_dir
        started_at = self._now()

        writer.write_json(
            "config.json",
            {
                "runner": self.name,
                "repair": repair.name,
                "benchmark": benchmark.name,
                "seed": self._seed,
                "started_at": self._iso(started_at),
                "bugs": [self._bug_to_dict(bug) for bug in bugs],
            },
        )
        writer.log(f"Started {self.name}")

        evaluation_results: list[EvaluationResult] = []
        bug_results: list[dict[str, Any]] = []

        for bug in bugs:
            bug_started_at = self._now()
            writer.log(f"Starting {self._bug_label(bug)}")

            try:
                result_entry = self._run_one_bug(writer, bug, benchmark, repair)
                status = str(result_entry["status"])
            except Exception as exc:
                status = "error"
                result_entry = {
                    "bug": self._bug_to_dict(bug),
                    "status": status,
                    "started_at": self._iso(bug_started_at),
                    "finished_at": self._iso(self._now()),
                    "error": f"{type(exc).__name__}: {exc}",
                }
                writer.log(f"ERROR {self._bug_label(bug)}: {exc}")

            bug_finished_at = self._now()
            result_entry.setdefault("started_at", self._iso(bug_started_at))
            result_entry.setdefault("finished_at", self._iso(bug_finished_at))
            bug_results.append(result_entry)
            evaluation_results.append(
                EvaluationResult(
                    bug=bug,
                    status=status,
                    started_at=bug_started_at,
                    finished_at=bug_finished_at,
                )
            )
            writer.log(f"Finished {self._bug_label(bug)} with status {status}")

        finished_at = self._now()
        run_status = "completed"
        if any(entry["status"] == "error" for entry in bug_results):
            run_status = "error"

        writer.write_json(
            "results.json",
            {
                "run_id": writer.run_dir.name,
                "status": run_status,
                "started_at": self._iso(started_at),
                "finished_at": self._iso(finished_at),
                "results": bug_results,
            },
        )
        writer.log(f"Finished run with status {run_status}")

        return evaluation_results

    def _run_one_bug(
        self,
        writer: RunWriter,
        bug: BugIdentifier,
        benchmark: BenchmarkAdapter,
        repair: RepairAlgorithm,
    ) -> dict[str, Any]:
        checkout_destination = (
            self._project_root
            / ".workspace"
            / "bugsinpy"
            / "evaluation"
            / writer.run_dir.name
            / f"{bug.project}_{bug.bug_id}"
        )

        writer.log(f"Checking out {self._bug_label(bug)}")
        checkout = benchmark.checkout(bug, checkout_destination)

        writer.log(f"Preparing baseline environment for {self._bug_label(bug)}")
        benchmark.prepare_environment(checkout)

        writer.log(f"Running baseline tests for {self._bug_label(bug)}")
        baseline_tests = benchmark.run_tests(checkout)

        patches = repair.generate_patches(checkout.bug, checkout)
        if not patches:
            return {
                "bug": self._bug_to_dict(checkout.bug),
                "status": "no_patch",
                "checkout": str(checkout.worktree),
                "baseline_tests": self._test_result_to_dict(baseline_tests),
                "final_tests": None,
                "patch": None,
            }

        patch = patches[0]
        writer.log(
            f"Selected patch {patch.patch_id} for {self._bug_label(checkout.bug)}"
        )

        patch_apply = None
        if patch.diff_text:
            patch_apply = self._apply_patch(checkout.worktree, patch)
            writer.log(
                f"Applied patch for {self._bug_label(checkout.bug)} "
                f"with return code {patch_apply['returncode']}",
            )
            if patch_apply["returncode"] != 0:
                return {
                    "bug": self._bug_to_dict(checkout.bug),
                    "status": "error",
                    "checkout": str(checkout.worktree),
                    "baseline_tests": self._test_result_to_dict(baseline_tests),
                    "final_tests": None,
                    "patch": self._patch_to_dict(patch),
                    "patch_apply": patch_apply,
                }

            writer.log(
                f"Preparing patched environment for {self._bug_label(checkout.bug)}"
            )
            benchmark.prepare_environment(checkout)
        else:
            writer.log(f"No-op patch selected for {self._bug_label(checkout.bug)}")

        writer.log(f"Running final tests for {self._bug_label(checkout.bug)}")
        final_tests = benchmark.run_tests(checkout)

        status = self._status_for_patch(patch, final_tests)
        return {
            "bug": self._bug_to_dict(checkout.bug),
            "status": status,
            "checkout": str(checkout.worktree),
            "baseline_tests": self._test_result_to_dict(baseline_tests),
            "final_tests": self._test_result_to_dict(final_tests),
            "patch": self._patch_to_dict(patch),
            "patch_apply": patch_apply,
        }

    def _apply_patch(self, worktree: Path, patch: PatchCandidate) -> dict[str, Any]:
        completed = subprocess.run(
            ["git", "apply", "--whitespace=nowarn", "-"],
            input=patch.diff_text,
            text=True,
            cwd=worktree,
            capture_output=True,
            check=False,
        )
        return {
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }

    def _status_for_patch(
        self, patch: PatchCandidate, final_tests: TestRunResult
    ) -> str:
        if patch.metadata.get("is_noop"):
            return "no_patch"
        if final_tests.failed_count == 0 and final_tests.error_count == 0:
            return "correct"
        return "failed"

    def _resolve_runs_dir(self, runs_dir: Path | str) -> Path:
        path = Path(runs_dir)
        if path.is_absolute():
            return path
        return self._project_root / path

    def _test_result_to_dict(self, result: TestRunResult) -> dict[str, Any]:
        return {
            "total_count": result.total_count,
            "passed_count": result.passed_count,
            "failed_count": result.failed_count,
            "error_count": result.error_count,
            "raw_output": result.raw_output,
        }

    def _patch_to_dict(self, patch: PatchCandidate) -> dict[str, Any]:
        return {
            "patch_id": patch.patch_id,
            "summary": patch.summary,
            "metadata": patch.metadata,
            "diff_text": patch.diff_text,
        }

    def _bug_to_dict(self, bug: BugIdentifier) -> dict[str, Any]:
        return asdict(bug)

    def _bug_label(self, bug: BugIdentifier) -> str:
        return f"{bug.benchmark}:{bug.project}:{bug.bug_id}"

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    def _iso(self, value: datetime) -> str:
        return value.isoformat()
