from __future__ import annotations

import argparse
import getpass
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dotenv import load_dotenv

from apr_framework.benchmarks.bugsinpy import BugsInPyAdapter
from apr_framework.benchmarks.registry import (
    create_bugsinpy_adapter,
    list_benchmark_names,
)
from apr_framework.cli.parser import build_parser
from apr_framework.core.exceptions import (
    APRFrameworkError,
    BenchmarkError,
    ConfigurationError,
)
from apr_framework.core.models import (
    BugIdentifier,
    CheckoutResult,
    EvaluationResult,
    LocalizationResult,
    RepairAttemptResult,
    TestRunResult,
)
from apr_framework.evaluation import DEFAULT_DUMMY_BUGS, DummyEvaluationRunner
from apr_framework.evaluation.localization_runner import LocalizationComparisonRunner
from apr_framework.evaluation.run_writer import RunWriter, serialize_localization_result
from apr_framework.localization import (
    FaultLocalizer,
    FauxPyConfig,
    FauxPyLocalizer,
    FauxPyToolchain,
    HybridFaultLocalizer,
)
from apr_framework.localization.fauxpy import (
    extract_pytest_node_ids,
    load_pytest_targets,
)
from apr_framework.repair import (
    DummyRepairAlgorithm,
    TemplateRepairAlgorithm,
    TemplateRepairConfig,
)
from apr_framework.reporting import ArchiveReportGenerator

if TYPE_CHECKING:
    from apr_framework.evaluation.course_approaches import CourseApproach
    from apr_framework.evaluation.localization_runner import (
        LocalizationTechniqueResult,
    )
    from apr_framework.evaluation.repair_comparison_runner import RepairCellResult
    from apr_framework.repair.base import RepairAlgorithm
    from apr_framework.repair.assessment.base import PatchAssessor
    from apr_framework.repair.ranking.base import PatchRanker


def main() -> int:
    """
    Execute the APR framework command-line application.

    Returns:
        Process exit code: `0` for success and `1` for handled framework errors.
    """
    try:
        return _run()
    except APRFrameworkError as error:
        print(f"Error: {error}")
        return 1


def _run() -> int:

    args, project_root = _parse_args_and_resolve_project_root()

    # List Integrated Benchmarks in the Framework
    if args.command == "list-benchmarks":
        return _get_benchmark_names()

    # Interactively store an LLM API key in the local .env file
    if args.command == "configure":
        return _prompt_and_save_llm_api_key(args, project_root)

    # -- Localization (FauxPy and LLM-based FL) --
    if args.command == "localize":
        family = _resolve_localize_family(args)
        _validate_localize_args(args, family)

        adapter, worktree, bug_dir = _create_adapter_and_resolve_checked_out_bug(
            args, project_root
        )
        checkout = CheckoutResult(
            bug=BugIdentifier(
                benchmark="bugsinpy",
                project=args.project,
                bug_id=args.bug,
            ),
            worktree=worktree,
            success=True,
            prepared=True,
        )

        writer, started_at = _build_run_writer(args, project_root)
        writer.log(f"Started {args.backend} localization for {args.project}#{args.bug}")

        localizer, config_data = _build_localizer_and_config_data(
            args, family, adapter, worktree, bug_dir, started_at
        )
        writer.write_json("config.json", config_data)
        writer.log("Running localization")

        try:
            result = run_localization(localizer, checkout, writer)
        except Exception as exc:
            write_localize_error_results_and_log(exc, writer, started_at)
            raise

        finished_at = datetime.now(timezone.utc)
        write_localize_success_results(result, writer, started_at, finished_at)

        archive_path = write_localize_report_archive(
            result, writer, started_at, finished_at
        )
        print_localize_run_summary(result, writer, archive_path)
        print_localize_raw_output_if_requested(args, result)

        return 0

    # --  BugsInPy --
    if args.command == "bugsinpy":
        adapter = create_bugsinpy_adapter(project_root)

        if args.bugsinpy_command == "setup":
            adapter.toolchain.bootstrap()
            print("BugsInPy setup successfully")
            return 0

        if args.bugsinpy_command == "list-projects":
            for project in adapter.list_projects():
                print(project)
            return 0

        if args.bugsinpy_command == "list-bugs":
            for bug_info in adapter.list_bugs(args.project):
                print(f"{bug_info.identifier.project} {bug_info.identifier.bug_id}")
            return 0

        if args.bugsinpy_command == "checkout":
            bug = BugIdentifier(
                benchmark="bugsinpy", project=args.project, bug_id=args.bug_id
            )
            destination = (
                project_root
                / ".workspace"
                / "bugsinpy"
                / f"{args.project}_{args.bug_id}"
            )

            result = adapter.checkout(bug, destination)
            print(f"Project: {result.bug.project}")
            print(f"Bug ID: {result.bug.bug_id}")
            print(f"Checkout success: {result.success}")
            print(f"Worktree: {result.worktree}")
            if result.message:
                print(f"Message: {result.message}")

            return 0

        if args.bugsinpy_command == "compile":
            canonical_project = adapter.resolve_project(args.project)
            bug = BugIdentifier(
                benchmark="bugsinpy", project=canonical_project, bug_id=args.bug_id
            )
            destination = (
                project_root
                / ".workspace"
                / "bugsinpy"
                / f"{args.project}_{args.bug_id}"
            )
            worktree = destination / canonical_project
            if not worktree.exists():
                raise BenchmarkError(
                    f"No checkout found at {worktree}. "
                    f"Run `python -m apr_framework bugsinpy checkout {args.project} {args.bug_id}` first."
                )

            checkout = CheckoutResult(bug=bug, worktree=worktree, success=True)
            adapter.prepare_environment(checkout)
            print(f"Project: {checkout.bug.project}")
            print(f"Bug ID: {checkout.bug.bug_id}")
            print(f"Prepared: {checkout.prepared}")
            print(f"Worktree: {checkout.worktree}")
            return 0

        if args.bugsinpy_command == "test":
            bug = BugIdentifier(
                benchmark="bugsinpy", project=args.project, bug_id=args.bug_id
            )
            destination = (
                project_root
                / ".workspace"
                / "bugsinpy"
                / f"{args.project}_{args.bug_id}"
            )
            checkout = adapter.checkout(bug, destination)
            adapter.prepare_environment(checkout)
            test_result = adapter.run_tests(checkout)

            print(f"Project: {checkout.bug.project}")
            print(f"Bug ID: {checkout.bug.bug_id}")
            print(f"Checkout success: {checkout.success}")
            print(f"Prepared: {checkout.prepared}")
            print(f"Tests run: {test_result.total_count}")
            print(f"Passing: {test_result.passed_count}")
            print(f"Failing: {test_result.failed_count}")

            if test_result.raw_output:
                print("\nRaw output:")
                print(test_result.raw_output)

            return 0

        if args.bugsinpy_command == "evaluate-dummy":
            repair = DummyRepairAlgorithm(project_root=project_root, seed=args.seed)
            runner = DummyEvaluationRunner(
                project_root=project_root,
                runs_dir=args.runs_dir,
                seed=args.seed,
            )

            results = runner.run(
                bugs=list(DEFAULT_DUMMY_BUGS),
                benchmark=adapter,
                repair=repair,
            )

            if runner.last_run_dir is not None:
                print(f"Run directory: {runner.last_run_dir}")
                report = ArchiveReportGenerator()
                archive_path = report.write_summary(results, runner.last_run_dir)
                print(f"Report archive: {archive_path}")

            for result in results:
                print(f"{result.bug.project} {result.bug.bug_id}: {result.status}")

            return 0

        if args.bugsinpy_command == "evaluate-localization":
            return _run_evaluate_localization(args, project_root, adapter)

        if args.bugsinpy_command == "evaluate-repair":
            return _run_evaluate_repair(args, project_root, adapter)

        if args.bugsinpy_command == "evaluate-llm-repair":
            return _run_evaluate_llm_repair(args, project_root, adapter)

        if args.bugsinpy_command == "evaluate-course-comparison":
            return _run_evaluate_course_comparison(args, project_root, adapter)

    # -- Repair --

    if args.command == "repair":
        # Currently repair components are working on the BugsInPy Projects
        adapter = create_bugsinpy_adapter(project_root)
        return handle_repair(args, adapter, project_root)

    return 1


def handle_repair(
    args: argparse.Namespace, adapter: BugsInPyAdapter, project_root: Path
) -> int:
    """Orchestrate a repair run (template or LLM technique) from CLI arguments.

    Steps:
    1. Verify the project is checked out and compiled.
    2. Run localization (or load a cached result if --skip-localize).
    3. Build the technique-specific repair algorithm (template or LLM).
    4. Delegate the generate-validate-correctness pipeline to
       RepairEvaluationRunner (T-2), which writes repair_results.json (incl.
       the validation metrics) and the plausible-patch artifacts.
    5. Print a human-readable summary.
    """
    project_root = Path(project_root)

    checkout = _resolve_repair_checkout_or_fail(args, adapter, project_root)
    writer, runs_dir, started_at = _create_run_writer_and_log_repair_start(
        args, project_root, checkout.bug
    )
    config_data = _build_repair_config_data(args, checkout.bug, started_at)
    writer.write_json("config.json", config_data)

    localization_result = _localize_for_repair(
        args, adapter, checkout, writer, runs_dir
    )

    repair_result, ranker, assessor = _run_repair_evaluation_and_write_results(
        args,
        adapter,
        project_root,
        runs_dir,
        config_data,
        writer,
        checkout,
        localization_result,
    )
    _print_repair_summary_for_bug(checkout.bug, writer, repair_result, ranker, assessor)

    return 0


def _resolve_repair_checkout_or_fail(
    args: argparse.Namespace, adapter: BugsInPyAdapter, project_root: Path
) -> CheckoutResult:
    """Resolve the canonical bug and its checked-out worktree, raising if absent."""
    adapter.toolchain.ensure_installed()
    canonical_project = adapter.resolve_project(args.project)
    bug = BugIdentifier(
        benchmark="bugsinpy", project=canonical_project, bug_id=args.bug
    )

    destination = (
        project_root / ".workspace" / "bugsinpy" / f"{canonical_project}_{args.bug}"
    )
    worktree = destination / canonical_project
    if not worktree.exists():
        raise BenchmarkError(
            f"No checkout found at {worktree}. "
            f"Run `python -m apr_framework bugsinpy checkout {args.project} {args.bug}` first."
        )

    return CheckoutResult(
        bug=bug,
        worktree=worktree,
        success=True,
        prepared=True,
    )


def _create_run_writer_and_log_repair_start(
    args: argparse.Namespace, project_root: Path, bug: BugIdentifier
) -> tuple[RunWriter, Path, datetime]:
    """Create the run_NNN writer, capture the start timestamp, and log the run start."""
    runs_dir = Path(args.runs_dir)
    if not runs_dir.is_absolute():
        runs_dir = project_root / runs_dir
    writer = RunWriter.create(runs_dir)

    started_at = datetime.now(timezone.utc)
    writer.log(f"Started {args.technique} repair for {bug.project}#{bug.bug_id}")
    return writer, runs_dir, started_at


def _resolve_repair_fl_backend_label(args: argparse.Namespace) -> str:
    """Name the FL source this repair run actually used, for the result files."""
    if args.fl_mode == "perfect":
        return "oracle"
    if _is_llm_fault_localization_selected(args):
        return "llm-fl"
    return args.fl_family


def _build_repair_config_data(
    args: argparse.Namespace, bug: BugIdentifier, started_at: datetime
) -> dict:
    """Build the config.json payload describing this repair run, branching on technique."""
    config_data = {
        "project": bug.project,
        "bug_id": bug.bug_id,
        "technique": args.technique,
        "budget": args.budget,
        "top_n_locations": args.top_n,
        "stop_on_first": args.stop_on_first,
        "regression_check": args.regression_check,
        "fl_mode": args.fl_mode,
        "fl_backend": _resolve_repair_fl_backend_label(args),
        "fl_backend_choice": getattr(args, "fl_backend", "fauxpy"),
        "fl_family": args.fl_family,
        "localization_metric": args.localization_metric,
        "mbfl_metric": args.mbfl_metric,
        "granularity": args.granularity,
        "skip_localize": args.skip_localize,
        "started_at": started_at.isoformat(),
    }

    if args.technique == "template":
        enabled_operators = [
            operator.strip()
            for operator in args.operators.split(",")
            if operator.strip()
        ]
        config_data.update(
            {
                "runner": "template-repair",
                "enabled_operators": enabled_operators,
                "timeout_per_test": args.timeout,
            }
        )
    elif args.technique == "llm":
        config_data.update(
            {
                "runner": "llm-repair",
                "model": args.model,
                "temperature": args.temperature,
                "max_candidates": args.max_candidates,
                "llm_provider": args.llm_provider,
                "system_prompt": args.system_prompt,
                "context_enrichment": args.context_enrichment,
                "few_shot_count": args.few_shot_count,
                "iterative": args.iterative,
                "max_iterations": args.max_iterations,
                "retrieval_budget": args.retrieval_budget,
                "timeout_seconds": args.timeout,
            }
        )
    else:
        raise ConfigurationError(f"Unknown repair technique: {args.technique!r}")

    if args.fl_mode == "auto" and _is_llm_fault_localization_selected(args):
        config_data.update(
            {
                "fl_system_prompt": args.fl_system_prompt,
                "fl_max_source_lines": args.max_source_lines,
                "fl_source_window": args.source_window,
            }
        )

    if args.assess:
        config_data.update(
            {
                "assess": True,
                "assess_system_prompt": args.assess_system_prompt,
                "assess_max_patches": args.assess_max_patches,
                "assess_model": args.model,
                "assess_temperature": args.temperature,
                "assess_llm_provider": args.llm_provider,
                "assess_llm_base_url": args.llm_base_url,
                "assess_llm_api_key_env": args.llm_api_key_env,
            }
        )

    if args.similarity_score:
        config_data["similarity_score"] = True

    return config_data


def _localize_for_repair(
    args: argparse.Namespace,
    adapter: BugsInPyAdapter,
    checkout: CheckoutResult,
    writer: RunWriter,
    runs_dir: Path,
) -> LocalizationResult:
    """Produce the fault-localization result feeding repair.

    Precedence: perfect (oracle) FL, then a cached result under --skip-localize,
    then a fresh LLM-FL run under --fl-backend llm, then a fresh FauxPy run.
    """
    localization_result = None

    # Perfect FL: bypass any localizer and use the BugsInPy developer-fix lines as
    # the oracle fault location. Ignores --fl-backend / --fl-family / --skip-localize.
    if args.fl_mode == "perfect":
        if _is_llm_fault_localization_selected(args):
            writer.log(
                "--fl-mode perfect overrides --fl-backend llm: using oracle locations."
            )
        localization_result = _run_perfect_localization_from_developer_fix(
            adapter, checkout, writer
        )

    if localization_result is None and args.skip_localize:
        localization_result = _load_cached_localization_for_bug(
            checkout.bug, runs_dir, writer
        )
        if localization_result is None:
            writer.log("No cached localization found — running fresh localization.")

    if localization_result is None and _is_llm_fault_localization_selected(args):
        localization_result = _run_llm_fault_localization_for_repair(
            args, adapter, checkout, writer
        )

    if localization_result is None:
        localization_result = _run_fresh_fauxpy_localization(
            args, adapter, checkout, writer
        )

    return localization_result


def _is_llm_fault_localization_selected(args: argparse.Namespace) -> bool:
    """Report whether the caller asked for the LLM fault-localization backend."""
    return getattr(args, "fl_backend", "fauxpy") == "llm"


def _run_llm_fault_localization_for_repair(
    args: argparse.Namespace,
    adapter: BugsInPyAdapter,
    checkout: CheckoutResult,
    writer: RunWriter,
) -> LocalizationResult:
    """Localize with the LLM backend and log the evidence it was shown."""
    writer.log(
        f"Running LLM fault localization (model={args.model}, "
        f"system_prompt={args.fl_system_prompt})"
    )
    localizer = _build_llm_fault_localizer(args, adapter)
    localization_result = localizer.localize(checkout.bug, checkout)

    # files_shown is the misfire signal: when only the test file appears, symbol
    # anchoring found no project source and the ranking will target the test.
    files_shown = localization_result.metadata.get("files_shown", [])
    writer.log(
        f"LLM FL done: {len(localization_result.ranked_locations)} ranked location(s) "
        f"from {len(files_shown)} file(s) shown to the model: {files_shown}"
    )
    return localization_result


def _run_perfect_localization_from_developer_fix(
    adapter: BugsInPyAdapter, checkout: CheckoutResult, writer: RunWriter
) -> LocalizationResult:
    """Localize using the BugsInPy developer fix as the oracle fault location."""
    from apr_framework.localization import PerfectFaultLocalizer

    writer.log("Running perfect (oracle) FL from the BugsInPy developer fix")
    localizer = PerfectFaultLocalizer(adapter)
    localization_result = localizer.localize(checkout.bug, checkout)
    writer.log(
        f"Perfect FL done: {len(localization_result.ranked_locations)} "
        "oracle location(s) from bug_patch.txt"
    )
    return localization_result


def _load_cached_localization_for_bug(
    bug: BugIdentifier, runs_dir: Path, writer: RunWriter
) -> LocalizationResult | None:
    """Return the most recent cached localization for this exact bug, or None.

    Scans run_*/results.json newest-first and filters on project and bug_id so a
    cache entry from a different bug is never mistakenly loaded.
    """
    import json

    results_files = sorted(runs_dir.glob("run_*/results.json"), reverse=True)
    for rf in results_files:
        try:
            cached = json.loads(rf.read_text(encoding="utf-8"))
            cached_bug = cached.get("bug", {})
            if (
                "ranked_locations" in cached
                and cached_bug.get("project") == bug.project
                and cached_bug.get("bug_id") == bug.bug_id
            ):
                from apr_framework.core.models import LocalizationResult, RankedLocation

                ranked = [
                    RankedLocation(
                        **{
                            k: v
                            for k, v in loc.items()
                            if k in RankedLocation.__dataclass_fields__
                        }
                    )
                    for loc in cached["ranked_locations"]
                ]
                localization_result = LocalizationResult(
                    bug=bug,
                    backend=cached.get("backend", "cached"),
                    ranked_locations=ranked,
                    metadata=cached.get("metadata", {}),
                )
                writer.log(
                    f"Loaded cached localization from {rf} ({len(ranked)} locations)"
                )
                return localization_result
        except Exception as exc:
            writer.log(f"Could not load cached result from {rf}: {exc}")

    return None


def _run_fresh_fauxpy_localization(
    args: argparse.Namespace,
    adapter: BugsInPyAdapter,
    checkout: CheckoutResult,
    writer: RunWriter,
) -> LocalizationResult:
    """Run a fresh FauxPy SBFL/MBFL/hybrid localization for the bug."""
    from apr_framework.localization import (
        FauxPyConfig,
        FauxPyLocalizer,
        FauxPyToolchain,
    )

    bug = checkout.bug
    canonical_project = bug.project
    bug_id = bug.bug_id
    worktree = checkout.worktree

    projects_dir = adapter.toolchain.repo_dir / "projects"
    bug_dir = projects_dir / canonical_project / "bugs" / str(bug_id)
    if not bug_dir.is_dir():
        raise BenchmarkError(
            f"No such bug {bug_id} for BugsInPy project {canonical_project}"
        )

    src = _infer_fauxpy_src(worktree, canonical_project)
    bug_test_targets = load_pytest_targets(bug_dir / "run_test.sh")
    bug_test_ids = [t for t in bug_test_targets if not t.startswith("-")]

    fauxpy_toolchain = FauxPyToolchain(adapter.toolchain)

    def _make_sbfl() -> FauxPyLocalizer:
        return FauxPyLocalizer(
            FauxPyConfig(
                src=src,
                test_targets=bug_test_targets,
                family="sbfl",
                granularity=args.granularity,
                failing_tests=bug_test_ids,
                metric=args.localization_metric,
            ),
            fauxpy_toolchain,
        )

    def _make_mbfl() -> FauxPyLocalizer:
        return FauxPyLocalizer(
            FauxPyConfig(
                src=src,
                test_targets=bug_test_targets,
                family="mbfl",
                granularity=args.granularity,
                failing_tests=bug_test_ids,
                metric=args.mbfl_metric,
                mutation_strategy="random",
                mutation_budget=args.mutation_budget,
                mutation_seed=args.seed,
            ),
            fauxpy_toolchain,
        )

    if args.fl_family == "sbfl":
        localizer = _make_sbfl()
        writer.log(f"Running SBFL localization (metric={args.localization_metric})")
    elif args.fl_family == "mbfl":
        localizer = _make_mbfl()
        writer.log(f"Running MBFL localization (metric={args.mbfl_metric})")
    else:  # hybrid
        from apr_framework.localization import HybridFaultLocalizer

        localizer = HybridFaultLocalizer(
            _make_sbfl(),
            _make_mbfl(),
            sbfl_weight=args.sbfl_weight,
            mbfl_weight=args.mbfl_weight,
        )
        writer.log(
            f"Running hybrid localization (sbfl={args.localization_metric}@"
            f"{args.sbfl_weight}, mbfl={args.mbfl_metric}@{args.mbfl_weight})"
        )
    try:
        localization_result = localizer.localize(bug, checkout)
        writer.log(
            f"Localization done: {len(localization_result.ranked_locations)} locations ranked"
        )
    except Exception as exc:
        writer.log(f"Localization failed: {exc}")
        raise

    return localization_result


def _run_repair_evaluation_and_write_results(
    args: argparse.Namespace,
    adapter: BugsInPyAdapter,
    project_root: Path,
    runs_dir: Path,
    config_data: dict,
    writer: RunWriter,
    checkout: CheckoutResult,
    localization_result: LocalizationResult,
) -> tuple[EvaluationResult, PatchRanker | None, PatchAssessor | None]:
    """Build the technique-specific repair algorithm and persist evaluation results.

    Builds either a TemplateRepairAlgorithm or an LLMRepairAlgorithm depending on
    args.technique and the optional ranker, records the chosen ranker into
    config_data, then runs RepairEvaluationRunner (which writes
    repair_results.json + patch artifacts). Returns the bug's EvaluationResult
    and the ranker (or None).
    """
    from apr_framework.evaluation.repair_runner import RepairEvaluationRunner

    if args.technique == "template":
        algorithm = _build_template_algorithm_and_log_start(
            args, localization_result, adapter, writer
        )
    elif args.technique == "llm":
        algorithm = _build_llm_algorithm_and_log_start(
            args, localization_result, adapter, writer
        )
    else:
        raise ConfigurationError(f"Unknown repair technique: {args.technique!r}")

    ranker = _build_ranker(args)
    if ranker is not None:
        writer.log(f"Patch ranker: {ranker.name}")
        config_data["ranker"] = ranker.name
    else:
        config_data["ranker"] = "none"

    assessor = _build_assessor(args, adapter)
    if assessor is not None:
        writer.log(f"Patch assessor: {assessor.name}")
        config_data["assessor"] = assessor.name

    if args.similarity_score:
        writer.log("Similarity scoring: enabled")
    config_data["similarity_score"] = args.similarity_score

    runner = RepairEvaluationRunner(
        project_root=project_root,
        runs_dir=runs_dir,
        budget=args.budget,
        stop_on_first=args.stop_on_first,
        config_data=config_data,
        writer=writer,
        ranker=ranker,
        assessor=assessor,
        localization_result=localization_result,
        score_similarity=args.similarity_score,
    )
    eval_results = runner.run([checkout.bug], adapter, algorithm)
    repair_result = eval_results[0]
    metrics = repair_result.metrics
    assert metrics is not None  # RepairEvaluationRunner always populates metrics

    writer.log(
        f"Repair finished: status={repair_result.status}, "
        f"candidates_validated={metrics.candidates_validated}, "
        f"elapsed={metrics.total_wall_clock_seconds:.1f}s"
    )

    return repair_result, ranker, assessor


def _print_repair_summary_for_bug(
    bug: BugIdentifier,
    writer: RunWriter,
    repair_result: EvaluationResult,
    ranker: PatchRanker | None,
    assessor: PatchAssessor | None,
) -> None:
    """Print the human-readable repair summary for a single bug."""
    metrics = repair_result.metrics
    assert metrics is not None  # RepairEvaluationRunner always populates metrics
    ttfp = metrics.time_to_first_plausible_seconds
    ttfp_str = f"{ttfp:.1f}s" if ttfp is not None else "n/a"
    print(f"\nRun directory: {writer.run_dir}")
    print(f"Project:       {bug.project}")
    print(f"Bug ID:        {bug.bug_id}")
    print(f"Status:        {repair_result.status}")
    print(f"Generated:     {metrics.total_candidates_generated} candidate(s)")
    print(f"Validated:     {metrics.candidates_validated} candidate(s)")
    print(f"Plausible:     {metrics.plausible_count} patch(es)")
    print(f"Correct:       {metrics.correct_count} patch(es)")
    print(f"1st plausible: {ttfp_str}")
    print(f"Total time:    {metrics.total_wall_clock_seconds:.1f}s")
    if ranker is not None:
        rank_str = (
            str(metrics.rank_of_first_correct)
            if metrics.rank_of_first_correct is not None
            else "n/a"
        )
        print(f"Rank of 1st correct (ranked): {rank_str}")
    if assessor is not None:
        query_count_str = (
            str(metrics.assessment_query_count)
            if metrics.assessment_query_count is not None
            else "n/a"
        )
        print(f"Assessment LLM calls:          {query_count_str}")
        _print_plausible_patch_assessments(repair_result.assessed_plausible_results)
    if repair_result.similarity_scored_results is not None:
        _print_similarity_scores(repair_result.similarity_scored_results)


def _print_plausible_patch_assessments(
    assessed_plausible_results: list[RepairAttemptResult] | None,
) -> None:
    """Print each plausible patch's LLM quality score and rationale, best first."""
    if not assessed_plausible_results:
        print("Assessed patches:              none")
        return
    print("Assessed plausible patches (by descending quality):")
    for rank_position, attempt_result in enumerate(assessed_plausible_results, start=1):
        patch = attempt_result.patch
        patch_id_str = patch.patch_id if patch is not None else "n/a"
        metadata = patch.metadata if patch is not None else {}
        quality_score = metadata.get("quality_score")
        score_str = (
            f"{quality_score:.2f}" if quality_score is not None else "not assessed"
        )
        rationale_str = metadata.get("assessment_rationale") or "—"
        print(f"  {rank_position}. {patch_id_str}  score={score_str}")
        print(f"     {rationale_str}")


def _print_similarity_scores(
    similarity_scored_results: list[RepairAttemptResult],
) -> None:
    """Print every generated candidate's graded closeness to the developer fix.

    Covers non-plausible candidates too: a patch that failed the test suite can still
    be a near-miss on the developer's edit, and hiding that behind the pass/fail
    verdict is exactly what the graded metric exists to avoid. Shown in generation
    order (same order as ``all_results`` in ``repair_results.json``) with a
    human-readable band next to each score, since a bare float like ``0.92`` is hard
    to eyeball on its own.
    """
    print("#" * 90)
    print(
        "# Similarity scores for every generated candidate "
        "(closeness to the developer fix, 0.0-1.0):"
    )
    if not similarity_scored_results:
        print("  none")
        return
    print("#  1.00        identical to the developer fix")
    print("#  0.85-0.99   very similar (nearly the same edit)")
    print("#  0.60-0.84   similar (recognizable overlap)")
    print("#  0.30-0.59   loosely similar")
    print("#  0.00-0.29   different (little in common)")
    print("#" * 90)
    for patch_position, attempt_result in enumerate(similarity_scored_results, start=1):
        patch = attempt_result.patch
        patch_id_str = patch.patch_id if patch is not None else "n/a"
        metadata = patch.metadata if patch is not None else {}
        similarity_score = metadata.get("context_similarity_score")
        band_str = metadata.get("similarity_band", "n/a")
        score_str = f"{similarity_score:.2f}" if similarity_score is not None else "n/a"
        print(f"  Patch {patch_position} ({patch_id_str}) -> {score_str}  ({band_str})")


def _build_ranker(args: argparse.Namespace) -> PatchRanker | None:
    """Construct a PatchRanker from CLI args, or return None if ranking is disabled."""
    if args.ranker == "none":
        return None
    from apr_framework.repair.ranking import create_ranker

    ranker_kwargs: dict = {}
    if args.ranker_weights:
        weight_parts = [part.strip() for part in args.ranker_weights.split(",")]
        if len(weight_parts) != 3:
            raise ConfigurationError(
                "--ranker-weights must be exactly three comma-separated numbers: "
                "suspiciousness_weight,simplicity_weight,operator_priority_weight"
            )
        try:
            ranker_kwargs = {
                "suspiciousness_weight": float(weight_parts[0]),
                "simplicity_weight": float(weight_parts[1]),
                "operator_priority_weight": float(weight_parts[2]),
            }
        except ValueError:
            raise ConfigurationError(
                "--ranker-weights values must be numeric (e.g. 0.6,0.25,0.15)"
            )
    return create_ranker(args.ranker, **ranker_kwargs)


def _build_assessor(
    args: argparse.Namespace, adapter: BugsInPyAdapter
) -> PatchAssessor | None:
    """Construct a PatchAssessor from CLI args, or return None if disabled."""
    if not args.assess:
        return None
    if args.llm_provider != "openai-compatible":
        raise ConfigurationError(
            "--assess currently supports only --llm-provider openai-compatible"
        )

    from apr_framework.repair.assessment import LLMAssessmentConfig, LLMPatchAssessor
    from apr_framework.repair.llm import OpenAICompatibleClient

    assessment_config = LLMAssessmentConfig(
        model_name=args.model,
        temperature=args.temperature,
        base_url=args.llm_base_url,
        api_key_env_var=args.llm_api_key_env,
        system_prompt_name=args.assess_system_prompt,
        max_patches_assessed=args.assess_max_patches,
        timeout_seconds=args.timeout,
    )
    assessment_client = OpenAICompatibleClient(assessment_config)
    return LLMPatchAssessor(assessment_config, assessment_client, adapter)


def _build_run_writer(
    args: argparse.Namespace, project_root: Path
) -> tuple[RunWriter, datetime]:

    runs_dir = Path(args.runs_dir)
    if not runs_dir.is_absolute():
        runs_dir = project_root / runs_dir
    writer = RunWriter.create(runs_dir)
    started_at = datetime.now(timezone.utc)

    return writer, started_at


def _build_localizer_and_config_data(
    args: argparse.Namespace,
    family: str,
    adapter: BugsInPyAdapter,
    worktree: Path,
    bug_dir: Path,
    started_at: datetime,
) -> tuple[FaultLocalizer, dict]:
    """Build the localizer and its config.json payload for the chosen backend."""
    if args.backend == "llm":
        return _build_llm_localizer_and_config_data(args, adapter, started_at)
    return _build_fauxpy_localizer_and_config_data(
        args, family, adapter, worktree, bug_dir, started_at
    )


def _build_llm_fault_localizer(
    args: argparse.Namespace, adapter: BugsInPyAdapter
) -> FaultLocalizer:
    """Build an LLMFaultLocalizer with its own OpenAI-compatible client.

    Shared by the ``localize --backend llm`` command and the ``repair``/evaluation
    commands' ``--fl-backend llm`` path, which read the same CLI dest names.
    """
    from apr_framework.localization import LLMFaultLocalizer, LLMLocalizationConfig
    from apr_framework.repair.llm import OpenAICompatibleClient

    localization_config = LLMLocalizationConfig(
        model_name=args.model,
        temperature=args.temperature,
        base_url=args.llm_base_url,
        api_key_env_var=args.llm_api_key_env,
        llm_provider=args.llm_provider,
        system_prompt_name=args.fl_system_prompt,
        top_n=args.top_n,
        max_source_lines=args.max_source_lines,
        source_window_lines=args.source_window,
    )
    llm_client = OpenAICompatibleClient(localization_config)
    return LLMFaultLocalizer(localization_config, llm_client, adapter)


def _build_llm_localizer_and_config_data(
    args: argparse.Namespace,
    adapter: BugsInPyAdapter,
    started_at: datetime,
) -> tuple[FaultLocalizer, dict]:
    """Build the LLM fault localizer and its config.json payload."""
    localizer = _build_llm_fault_localizer(args, adapter)
    config_data = {
        "runner": "llm-localizer",
        "backend": "llm",
        "project": args.project,
        "bug_id": args.bug,
        "model": args.model,
        "temperature": args.temperature,
        "top_n": args.top_n,
        "system_prompt": args.fl_system_prompt,
        "llm_base_url": args.llm_base_url,
        "max_source_lines": args.max_source_lines,
        "source_window": args.source_window,
        "started_at": started_at.isoformat(),
    }
    return localizer, config_data


def _build_fauxpy_localizer_and_config_data(
    args: argparse.Namespace,
    family: str,
    adapter: BugsInPyAdapter,
    worktree: Path,
    bug_dir: Path,
    started_at: datetime,
) -> tuple[FaultLocalizer, dict]:
    """Build the FauxPy (SBFL/MBFL/hybrid) localizer and its config.json payload."""
    source_package = args.src or _infer_fauxpy_src(worktree, args.project)
    bug_test_targets = load_pytest_targets(bug_dir / "run_test.sh")
    bug_test_ids = extract_pytest_node_ids(bug_test_targets)
    test_targets = (
        _parse_fauxpy_test_targets_command(args.test_target) or bug_test_targets
    )
    failing_tests = (
        _parse_fauxpy_failing_tests_command(args.failing_tests) or bug_test_ids
    )
    fauxpy_toolchain = FauxPyToolchain(adapter.toolchain)

    effective_metric = resolve_effective_metric(args, family)
    config_data = build_localize_config_data(args, family, effective_metric, started_at)

    if family == "hybrid":
        localizer = _build_hybrid_localizer(
            source_package, test_targets, failing_tests, args, fauxpy_toolchain
        )
    else:
        fauxpy_config = FauxPyConfig(
            src=source_package,
            test_targets=test_targets,
            family=family,
            granularity=args.granularity,
            failing_tests=failing_tests,
            top_n=args.top_n,
            mutation_strategy=args.mutation_strategy,
            mutation_budget=args.mutation_budget,
            mutation_seed=args.seed,
            metric=effective_metric,
            wsbi_alpha=args.wsbi_alpha,
        )
        localizer = FauxPyLocalizer(fauxpy_config, fauxpy_toolchain)
    return localizer, config_data


def _build_hybrid_localizer(
    source_package: str,
    test_targets: list[str],
    failing_tests: list[str],
    args: argparse.Namespace,
    fauxpy_toolchain: FauxPyToolchain,
) -> HybridFaultLocalizer:
    sbfl_config = FauxPyConfig(
        src=source_package,
        test_targets=test_targets,
        family="sbfl",
        granularity=args.granularity,
        failing_tests=failing_tests,
        top_n=None,
        metric=args.sbfl_metric,
    )
    mbfl_config = FauxPyConfig(
        src=source_package,
        test_targets=test_targets,
        family="mbfl",
        granularity=args.granularity,
        failing_tests=failing_tests,
        top_n=None,
        mutation_strategy=args.mutation_strategy,
        mutation_budget=args.mutation_budget,
        mutation_seed=args.seed,
        metric=args.mbfl_metric,
    )
    return HybridFaultLocalizer(
        FauxPyLocalizer(sbfl_config, fauxpy_toolchain),
        FauxPyLocalizer(mbfl_config, fauxpy_toolchain),
        sbfl_weight=args.sbfl_weight,
        mbfl_weight=args.mbfl_weight,
        top_n=args.top_n,
    )


def run_localization(
    localizer: FaultLocalizer, checkout: CheckoutResult, writer: RunWriter
) -> LocalizationResult:
    """Run the localizer and log the ranked-location count; exceptions propagate to the caller."""
    localization_result = localizer.localize(checkout.bug, checkout, None)
    writer.log(
        f"Localization completed: {len(localization_result.ranked_locations)} ranked locations"
    )
    return localization_result


def write_localize_error_results_and_log(
    exc: Exception, writer: RunWriter, started_at: datetime
) -> None:
    """Persist an error results.json, log the failure, and print the run directory."""
    writer.log(f"ERROR: {exc}")
    writer.write_json(
        "results.json",
        {
            "run_id": writer.run_dir.name,
            "status": "error",
            "started_at": started_at.isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "error": f"{type(exc).__name__}: {exc}",
        },
    )
    writer.log("Finished run with status error")
    print(f"Run directory: {writer.run_dir}")


def write_localize_success_results(
    localization_result: LocalizationResult,
    writer: RunWriter,
    started_at: datetime,
    finished_at: datetime,
) -> None:
    """Persist results.json for a completed localization run and log completion."""
    writer.write_json(
        "results.json",
        {
            "run_id": writer.run_dir.name,
            "status": "completed",
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            **serialize_localization_result(localization_result),
        },
    )
    writer.log("Finished run with status completed")


def write_localize_report_archive(
    localization_result: LocalizationResult,
    writer: RunWriter,
    started_at: datetime,
    finished_at: datetime,
) -> Path:
    """Write the archived run summary report and return its path."""
    return ArchiveReportGenerator().write_summary(
        [
            EvaluationResult(
                bug=localization_result.bug,
                status="completed",
                started_at=started_at,
                finished_at=finished_at,
            )
        ],
        writer.run_dir,
    )


def print_localize_run_summary(
    localization_result: LocalizationResult, writer: RunWriter, archive_path: Path
) -> None:
    """Print the run directory, report archive path, bug identity, and ranked locations."""
    print(f"Run directory: {writer.run_dir}")
    print(f"Report archive: {archive_path}")
    print(f"Project: {localization_result.bug.project}")
    print(f"Bug ID: {localization_result.bug.bug_id}")
    print(f"Backend: {localization_result.backend}")
    if localization_result.metadata.get("score_formula"):
        print(f"Score formula: {localization_result.metadata['score_formula']}")
    print("Ranked locations:")
    for ranked_location in localization_result.ranked_locations:
        score_str = (
            "" if ranked_location.score is None else f"{ranked_location.score:.4f}"
        )
        print(
            f"{ranked_location.rank}. {ranked_location.file_path}:{ranked_location.line} {score_str}".rstrip()
        )


def print_localize_raw_output_if_requested(
    args: argparse.Namespace, localization_result: LocalizationResult
) -> None:
    """Print FauxPy raw output when --show-raw-output was passed, handling hybrid's two backends."""
    if args.show_raw_output and localization_result.metadata.get("raw_output"):
        print("\nRaw FauxPy output:")
        print(localization_result.metadata["raw_output"])
    elif args.show_raw_output and localization_result.backend == "hybrid-fauxpy":
        sbfl_raw_output = localization_result.metadata.get("sbfl_metadata", {}).get(
            "raw_output"
        )
        mbfl_raw_output = localization_result.metadata.get("mbfl_metadata", {}).get(
            "raw_output"
        )
        if sbfl_raw_output:
            print("\nRaw FauxPy SBFL output:")
            print(sbfl_raw_output)
        if mbfl_raw_output:
            print("\nRaw FauxPy MBFL output:")
            print(mbfl_raw_output)


def _build_template_algorithm_and_log_start(
    args: argparse.Namespace,
    localization_result: LocalizationResult,
    adapter: BugsInPyAdapter,
    writer: RunWriter,
) -> RepairAlgorithm:
    """Construct a TemplateRepairAlgorithm from CLI arguments and log the repair start line."""
    enabled_operators = [
        operator.strip() for operator in args.operators.split(",") if operator.strip()
    ]
    template_repair_config = TemplateRepairConfig(
        budget=args.budget,
        top_n_locations=args.top_n,
        enabled_operators=enabled_operators,
        timeout_per_test=args.timeout,
        stop_on_first=args.stop_on_first,
        regression_check=args.regression_check,
    )
    algorithm = TemplateRepairAlgorithm(
        localization_result=localization_result,
        adapter=adapter,
        config=template_repair_config,
    )
    writer.log(
        f"Repair started: budget={args.budget}, top_n={args.top_n}, "
        f"operators={template_repair_config.enabled_operators}"
    )
    return algorithm


def _build_llm_algorithm_and_log_start(
    args: argparse.Namespace,
    localization_result: LocalizationResult,
    adapter: BugsInPyAdapter,
    writer: RunWriter,
) -> RepairAlgorithm:
    """Construct an LLMRepairAlgorithm from CLI arguments and log the repair start line."""
    from apr_framework.repair.llm import (
        LLMRepairAlgorithm,
        LLMRepairConfig,
        OpenAICompatibleClient,
    )

    repair_config = LLMRepairConfig(
        model_name=args.model,
        temperature=args.temperature,
        max_patch_count=args.max_candidates,
        top_n_locations=args.top_n,
        llm_provider=args.llm_provider,
        base_url=args.llm_base_url,
        api_key_env_var=args.llm_api_key_env,
        system_prompt_name=args.system_prompt,
        timeout_seconds=args.timeout,
        budget=args.budget,
        stop_on_first=args.stop_on_first,
        regression_check=args.regression_check,
        context_enrichment=args.context_enrichment,
        few_shot_count=args.few_shot_count,
        iterative=args.iterative,
        max_iterations=args.max_iterations,
        retrieval_budget=args.retrieval_budget,
    )
    llm_client = OpenAICompatibleClient(repair_config)
    algorithm = LLMRepairAlgorithm(
        localization_result=localization_result,
        adapter=adapter,
        repair_config=repair_config,
        llm_client=llm_client,
    )
    writer.log(
        f"Repair started: budget={args.budget}, top_n={args.top_n}, "
        f"model={args.model}, temperature={args.temperature}, "
        f"max_candidates={args.max_candidates}, "
        f"iterative={args.iterative}, max_iterations={args.max_iterations}, "
        f"retrieval_budget={args.retrieval_budget}"
    )
    return algorithm


def _write_env_var(env_file_path: Path, key_name: str, value: str) -> None:
    """Set `key_name=value` in the given .env file, replacing an existing entry or appending a new one."""
    existing_lines = (
        env_file_path.read_text().splitlines() if env_file_path.exists() else []
    )
    updated_lines = []
    key_written = False
    for line in existing_lines:
        if line.startswith(f"{key_name}="):
            updated_lines.append(f"{key_name}={value}")
            key_written = True
        else:
            updated_lines.append(line)
    if not key_written:
        updated_lines.append(f"{key_name}={value}")
    env_file_path.write_text("\n".join(updated_lines) + "\n")


def _run_evaluate_localization(
    args: argparse.Namespace, project_root: Path, adapter: BugsInPyAdapter
) -> int:
    """Run the localization comparison evaluation and write results."""
    from apr_framework.core.models import CheckoutResult
    from apr_framework.localization import (
        FauxPyConfig,
        FauxPyLocalizer,
        FauxPyToolchain,
        HybridFaultLocalizer,
    )

    bugs = _parse_bug_list(args.bugs or "black:1,black:3,black:7")
    top_ks = tuple(int(k.strip()) for k in args.top_ks.split(",") if k.strip())
    output_dir = Path(args.output_dir)
    if not output_dir.is_absolute():
        output_dir = project_root / output_dir
    granularity = args.granularity

    fauxpy_toolchain = FauxPyToolchain(adapter.toolchain)
    projects_dir = adapter.toolchain.repo_dir / "projects"

    # Validate bugs and build checkouts
    checkouts: dict = {}
    for bug in bugs:
        worktree = (
            project_root
            / ".workspace"
            / "bugsinpy"
            / f"{bug.project}_{bug.bug_id}"
            / bug.project
        )
        if not worktree.exists():
            raise BenchmarkError(
                f"No checkout found for {bug.project} #{bug.bug_id} at {worktree}. "
                f"Run `python -m apr_framework bugsinpy checkout {bug.project} {bug.bug_id}` "
                "and then `compile` before evaluating."
            )
        checkouts[bug] = CheckoutResult(
            bug=bug,
            worktree=worktree,
            success=True,
            prepared=True,
        )

    def patch_dir(bug: BugIdentifier) -> Path:
        return projects_dir / bug.project / "bugs" / str(bug.bug_id)

    def _src(bug: BugIdentifier) -> str:
        worktree = checkouts[bug].worktree
        return _infer_fauxpy_src(worktree, bug.project)

    def _make_sbfl(bug: BugIdentifier, metric: str) -> FauxPyLocalizer:
        bug_dir = patch_dir(bug)
        test_targets = load_pytest_targets(bug_dir / "run_test.sh")
        test_ids = extract_pytest_node_ids(test_targets)
        return FauxPyLocalizer(
            FauxPyConfig(
                src=_src(bug),
                test_targets=test_targets,
                family="sbfl",
                granularity=granularity,
                failing_tests=test_ids,
                metric=metric,
            ),
            fauxpy_toolchain,
        )

    def _make_mbfl_traditional(bug: BugIdentifier) -> FauxPyLocalizer:
        """True stock FauxPy MBFL baseline —> exhaustive mutation, no budget cap.

        No mutation_strategy or mutation_budget means FauxPy runs all generated
        mutants, which is the stock behaviour of FauxPy 0.7.0
        """
        bug_dir = patch_dir(bug)
        test_targets = load_pytest_targets(bug_dir / "run_test.sh")
        test_ids = [t for t in test_targets if not t.startswith("-")]
        return FauxPyLocalizer(
            FauxPyConfig(
                src=_src(bug),
                test_targets=test_targets,
                family="mbfl",
                granularity=granularity,
                failing_tests=test_ids,
                metric="metallaxis",
            ),
            fauxpy_toolchain,
        )

    def _make_mbfl(bug: BugIdentifier) -> FauxPyLocalizer:
        """MBFL with random mutation-budget extension"""
        bug_dir = patch_dir(bug)
        test_targets = load_pytest_targets(bug_dir / "run_test.sh")
        test_ids = extract_pytest_node_ids(test_targets)
        return FauxPyLocalizer(
            FauxPyConfig(
                src=_src(bug),
                test_targets=test_targets,
                family="mbfl",
                granularity=granularity,
                failing_tests=test_ids,
                metric="metallaxis",
                mutation_strategy="random",
                mutation_budget=args.budget,
                mutation_seed=args.seed,
            ),
            fauxpy_toolchain,
        )

    def _make_hybrid(bug: BugIdentifier) -> HybridFaultLocalizer:
        bug_dir = patch_dir(bug)
        test_targets = load_pytest_targets(bug_dir / "run_test.sh")
        test_ids = extract_pytest_node_ids(test_targets)
        sbfl_cfg = FauxPyConfig(
            src=_src(bug),
            test_targets=test_targets,
            family="sbfl",
            granularity=granularity,
            failing_tests=test_ids,
            metric="ochiai",
        )
        mbfl_cfg = FauxPyConfig(
            src=_src(bug),
            test_targets=test_targets,
            family="mbfl",
            granularity=granularity,
            failing_tests=test_ids,
            metric="metallaxis",
            mutation_strategy="random",
            mutation_budget=args.budget,
            mutation_seed=args.seed,
        )
        return HybridFaultLocalizer(
            FauxPyLocalizer(sbfl_cfg, fauxpy_toolchain),
            FauxPyLocalizer(mbfl_cfg, fauxpy_toolchain),
        )

    techniques = _build_techniques(
        _make_sbfl, _make_mbfl_traditional, _make_mbfl, _make_hybrid
    )

    runner = LocalizationComparisonRunner(top_ks=top_ks)

    print(f"Evaluating {len(bugs)} bug(s) with {len(techniques)} technique(s)...")
    eval_results = runner.run(
        bugs=bugs,
        techniques=techniques,
        checkouts=checkouts,
        patch_dir_fn=patch_dir,
    )

    readme_path = runner.write_results(eval_results, output_dir)
    print(f"\nResults written to: {output_dir}")
    print(f"README: {readme_path}")

    # Print a compact summary table to stdout
    _print_summary(eval_results, top_ks)
    return 0


def _run_evaluate_repair(
    args: argparse.Namespace, project_root: Path, adapter: BugsInPyAdapter
) -> int:
    """Run the T-5 repair comparison: each bug under both FL modes + ranker."""
    from apr_framework.core.models import CheckoutResult
    from apr_framework.evaluation.repair_comparison_runner import RepairComparisonRunner
    from apr_framework.localization import (
        FauxPyConfig,
        FauxPyLocalizer,
        FauxPyToolchain,
        HybridFaultLocalizer,
        PerfectFaultLocalizer,
    )
    from apr_framework.repair import TemplateRepairAlgorithm, TemplateRepairConfig

    adapter.toolchain.ensure_installed()
    bugs = _parse_bug_list(args.bugs or "tornado:14,scrapy:2,black:1")
    fl_modes = [mode.strip() for mode in args.fl_modes.split(",") if mode.strip()]
    for fl_mode in fl_modes:
        if fl_mode not in ("auto", "perfect"):
            raise ConfigurationError(
                f"Invalid FL mode {fl_mode!r} — expected 'auto' or 'perfect'."
            )

    output_dir = Path(args.output_dir)
    if not output_dir.is_absolute():
        output_dir = project_root / output_dir
    runs_dir = Path(args.runs_dir)
    if not runs_dir.is_absolute():
        runs_dir = project_root / runs_dir

    fauxpy_toolchain = FauxPyToolchain(adapter.toolchain)
    projects_dir = adapter.toolchain.repo_dir / "projects"

    # Resolve and validate every bug's checkout up front, keyed by canonical bug.
    canonical_bug_list: list[BugIdentifier] = []
    checkouts: dict[BugIdentifier, CheckoutResult] = {}
    for bug in bugs:
        canonical_project = adapter.resolve_project(bug.project)
        canonical_bug = BugIdentifier(
            benchmark="bugsinpy", project=canonical_project, bug_id=bug.bug_id
        )
        worktree = (
            project_root
            / ".workspace"
            / "bugsinpy"
            / f"{canonical_project}_{bug.bug_id}"
            / canonical_project
        )
        if not worktree.exists():
            raise BenchmarkError(
                f"No checkout found for {bug.project} #{bug.bug_id} at {worktree}. "
                f"Run `python -m apr_framework bugsinpy checkout {bug.project} {bug.bug_id}` "
                "and then `compile` before evaluating."
            )
        canonical_bug_list.append(canonical_bug)
        checkouts[canonical_bug] = CheckoutResult(
            bug=canonical_bug, worktree=worktree, success=True, prepared=True
        )

    def _auto_localizer(canonical_bug: BugIdentifier) -> FaultLocalizer:
        worktree = checkouts[canonical_bug].worktree
        source_package = _infer_fauxpy_src(worktree, canonical_bug.project)
        bug_dir = (
            projects_dir / canonical_bug.project / "bugs" / str(canonical_bug.bug_id)
        )
        bug_test_targets = load_pytest_targets(bug_dir / "run_test.sh")
        bug_test_ids = extract_pytest_node_ids(bug_test_targets)

        def _make_sbfl() -> FauxPyLocalizer:
            return FauxPyLocalizer(
                FauxPyConfig(
                    src=source_package,
                    test_targets=bug_test_targets,
                    family="sbfl",
                    granularity=args.granularity,
                    failing_tests=bug_test_ids,
                    metric=args.localization_metric,
                ),
                fauxpy_toolchain,
            )

        def _make_mbfl() -> FauxPyLocalizer:
            return FauxPyLocalizer(
                FauxPyConfig(
                    src=source_package,
                    test_targets=bug_test_targets,
                    family="mbfl",
                    granularity=args.granularity,
                    failing_tests=bug_test_ids,
                    metric=args.mbfl_metric,
                    mutation_strategy="random",
                    mutation_budget=args.mutation_budget,
                    mutation_seed=args.seed,
                ),
                fauxpy_toolchain,
            )

        if args.fl_family == "sbfl":
            return _make_sbfl()
        if args.fl_family == "mbfl":
            return _make_mbfl()
        return HybridFaultLocalizer(_make_sbfl(), _make_mbfl())

    perfect_localizer = PerfectFaultLocalizer(adapter)

    def localization_provider(
        canonical_bug: BugIdentifier, fl_mode: str
    ) -> LocalizationResult:
        checkout = checkouts[canonical_bug]
        if fl_mode == "perfect":
            return perfect_localizer.localize(canonical_bug, checkout)
        return _auto_localizer(canonical_bug).localize(canonical_bug, checkout)

    enabled_operators = [
        operator_key.strip()
        for operator_key in args.operators.split(",")
        if operator_key.strip()
    ]
    repair_config = TemplateRepairConfig(
        budget=args.budget,
        top_n_locations=args.top_n,
        enabled_operators=enabled_operators,
        timeout_per_test=args.timeout,
        stop_on_first=args.stop_on_first,
        regression_check=args.regression_check,
    )

    def repair_algorithm_factory(
        localization_result: LocalizationResult,
    ) -> RepairAlgorithm:
        return TemplateRepairAlgorithm(
            localization_result=localization_result,
            adapter=adapter,
            config=repair_config,
        )

    ranker = _build_ranker(args)
    repair_config_data = {
        "technique": "template",
        "budget": repair_config.budget,
        "top_n_locations": repair_config.top_n_locations,
        "enabled_operators": repair_config.enabled_operators,
        "timeout_per_test": repair_config.timeout_per_test,
        "stop_on_first": repair_config.stop_on_first,
        "regression_check": repair_config.regression_check,
        "fl_family": args.fl_family,
        "localization_metric": args.localization_metric,
        "granularity": args.granularity,
    }

    runner = RepairComparisonRunner(
        project_root=project_root,
        runs_dir=runs_dir,
        ranker=ranker,
        repair_config_data=repair_config_data,
        budget=repair_config.budget,
        stop_on_first=repair_config.stop_on_first,
    )

    print(
        f"Evaluating {len(bugs)} bug(s) x {len(fl_modes)} FL mode(s) "
        f"({', '.join(fl_modes)}) with ranker={ranker.name if ranker else 'none'}..."
    )
    cells = runner.run(
        bugs=canonical_bug_list,
        fl_modes=fl_modes,
        benchmark=adapter,
        localization_provider=localization_provider,
        repair_algorithm_factory=repair_algorithm_factory,
    )

    readme_path = runner.write_results(cells, output_dir)
    print(f"\nResults written to: {output_dir}")
    print(f"README: {readme_path}")
    _print_repair_summary(cells)
    return 0


# Each LLM variant differs from the bare baseline by exactly one factor, so the
# evaluation isolates the effect of enrichment and of the iterative loop.
_LLM_VARIANT_TOGGLES: dict[str, dict[str, Any]] = {
    "single-shot": {
        "context_enrichment": False,
        "iterative": False,
        "few_shot_count": 0,
    },
    "context-enriched": {
        "context_enrichment": True,
        "iterative": False,
        "few_shot_count": 0,
    },
    "iterative": {
        "context_enrichment": False,
        "iterative": True,
        "few_shot_count": 0,
    },
}


def _build_llm_algorithm_for_variant(
    args: argparse.Namespace,
    localization_result: LocalizationResult,
    variant_label: str,
    adapter: BugsInPyAdapter,
) -> RepairAlgorithm:
    """Build a fresh LLM algorithm + client for one matrix cell's variant.

    A fresh ``OpenAICompatibleClient`` per call keeps that cell's LLM-query
    counter isolated from every other cell.
    """
    from apr_framework.repair.llm import (
        LLMRepairAlgorithm,
        LLMRepairConfig,
        OpenAICompatibleClient,
    )

    variant_toggles = _LLM_VARIANT_TOGGLES[variant_label]
    repair_config = LLMRepairConfig(
        model_name=args.model,
        temperature=args.temperature,
        max_patch_count=args.max_candidates,
        top_n_locations=args.top_n,
        llm_provider=args.llm_provider,
        base_url=args.llm_base_url,
        api_key_env_var=args.llm_api_key_env,
        system_prompt_name=args.system_prompt,
        timeout_seconds=args.timeout,
        budget=args.budget,
        stop_on_first=args.stop_on_first,
        regression_check=args.regression_check,
        context_enrichment=variant_toggles["context_enrichment"],
        few_shot_count=variant_toggles["few_shot_count"],
        iterative=variant_toggles["iterative"],
        max_iterations=args.max_iterations,
    )
    llm_client = OpenAICompatibleClient(repair_config)
    return LLMRepairAlgorithm(
        localization_result=localization_result,
        adapter=adapter,
        repair_config=repair_config,
        llm_client=llm_client,
    )


def _run_evaluate_llm_repair(
    args: argparse.Namespace, project_root: Path, adapter: BugsInPyAdapter
) -> int:
    """Run the LLM repair comparison: each bug x variant x FL mode, aggregated."""
    from apr_framework.core.models import CheckoutResult
    from apr_framework.evaluation.llm_repair_comparison_runner import (
        LLMRepairComparisonRunner,
    )
    from apr_framework.localization import (
        FauxPyConfig,
        FauxPyLocalizer,
        FauxPyToolchain,
        HybridFaultLocalizer,
        PerfectFaultLocalizer,
    )

    bugs = _parse_bug_list(args.bugs)
    variants = [
        variant.strip() for variant in args.variants.split(",") if variant.strip()
    ]
    for variant_label in variants:
        if variant_label not in _LLM_VARIANT_TOGGLES:
            raise ConfigurationError(
                f"Invalid variant {variant_label!r} — expected one of "
                f"{', '.join(_LLM_VARIANT_TOGGLES)}."
            )
    fl_modes = [mode.strip() for mode in args.fl_modes.split(",") if mode.strip()]
    for fl_mode in fl_modes:
        if fl_mode not in ("auto", "perfect"):
            raise ConfigurationError(
                f"Invalid FL mode {fl_mode!r} — expected 'auto' or 'perfect'."
            )

    # Automated FL needs FauxPy; skip the install when only perfect FL is requested.
    if "auto" in fl_modes:
        adapter.toolchain.ensure_installed()

    output_dir = Path(args.output_dir)
    if not output_dir.is_absolute():
        output_dir = project_root / output_dir
    runs_dir = Path(args.runs_dir)
    if not runs_dir.is_absolute():
        runs_dir = project_root / runs_dir

    fauxpy_toolchain = FauxPyToolchain(adapter.toolchain)
    projects_dir = adapter.toolchain.repo_dir / "projects"

    # Resolve and validate every bug's checkout up front, keyed by canonical bug.
    canonical_bug_list: list[BugIdentifier] = []
    checkouts: dict[BugIdentifier, CheckoutResult] = {}
    for bug in bugs:
        canonical_project = adapter.resolve_project(bug.project)
        canonical_bug = BugIdentifier(
            benchmark="bugsinpy", project=canonical_project, bug_id=bug.bug_id
        )
        worktree = (
            project_root
            / ".workspace"
            / "bugsinpy"
            / f"{canonical_project}_{bug.bug_id}"
            / canonical_project
        )
        if not worktree.exists():
            raise BenchmarkError(
                f"No checkout found for {bug.project} #{bug.bug_id} at {worktree}. "
                f"Run `python -m apr_framework bugsinpy checkout {bug.project} {bug.bug_id}` "
                "and then `compile` before evaluating."
            )
        canonical_bug_list.append(canonical_bug)
        checkouts[canonical_bug] = CheckoutResult(
            bug=canonical_bug, worktree=worktree, success=True, prepared=True
        )

    def _auto_localizer(canonical_bug: BugIdentifier) -> FaultLocalizer:
        worktree = checkouts[canonical_bug].worktree
        source_package = _infer_fauxpy_src(worktree, canonical_bug.project)
        bug_dir = (
            projects_dir / canonical_bug.project / "bugs" / str(canonical_bug.bug_id)
        )
        bug_test_targets = load_pytest_targets(bug_dir / "run_test.sh")
        bug_test_ids = extract_pytest_node_ids(bug_test_targets)

        def _make_sbfl() -> FauxPyLocalizer:
            return FauxPyLocalizer(
                FauxPyConfig(
                    src=source_package,
                    test_targets=bug_test_targets,
                    family="sbfl",
                    granularity=args.granularity,
                    failing_tests=bug_test_ids,
                    metric=args.localization_metric,
                ),
                fauxpy_toolchain,
            )

        def _make_mbfl() -> FauxPyLocalizer:
            return FauxPyLocalizer(
                FauxPyConfig(
                    src=source_package,
                    test_targets=bug_test_targets,
                    family="mbfl",
                    granularity=args.granularity,
                    failing_tests=bug_test_ids,
                    metric=args.mbfl_metric,
                    mutation_strategy="random",
                    mutation_budget=args.mutation_budget,
                    mutation_seed=args.seed,
                ),
                fauxpy_toolchain,
            )

        if args.fl_family == "sbfl":
            return _make_sbfl()
        if args.fl_family == "mbfl":
            return _make_mbfl()
        return HybridFaultLocalizer(_make_sbfl(), _make_mbfl())

    perfect_localizer = PerfectFaultLocalizer(adapter)

    def localization_provider(
        canonical_bug: BugIdentifier, fl_mode: str
    ) -> LocalizationResult:
        checkout = checkouts[canonical_bug]
        if fl_mode == "perfect":
            return perfect_localizer.localize(canonical_bug, checkout)
        return _auto_localizer(canonical_bug).localize(canonical_bug, checkout)

    def repair_algorithm_factory(
        localization_result: LocalizationResult, variant_label: str
    ) -> RepairAlgorithm:
        return _build_llm_algorithm_for_variant(
            args, localization_result, variant_label, adapter
        )

    ranker = _build_ranker(args)
    repair_config_data = {
        "technique": "llm",
        "model": args.model,
        "temperature": args.temperature,
        "max_candidates": args.max_candidates,
        "top_n_locations": args.top_n,
        "max_iterations": args.max_iterations,
        "budget": args.budget,
        "timeout_per_test": args.timeout,
        "stop_on_first": args.stop_on_first,
        "regression_check": args.regression_check,
        "llm_provider": args.llm_provider,
        "llm_base_url": args.llm_base_url,
        "fl_family": args.fl_family,
        "localization_metric": args.localization_metric,
        "granularity": args.granularity,
    }

    runner = LLMRepairComparisonRunner(
        project_root=project_root,
        runs_dir=runs_dir,
        ranker=ranker,
        repair_config_data=repair_config_data,
        budget=args.budget,
        stop_on_first=args.stop_on_first,
    )

    print(
        f"Evaluating {len(bugs)} bug(s) x {len(variants)} variant(s) "
        f"({', '.join(variants)}) x {len(fl_modes)} FL mode(s) "
        f"({', '.join(fl_modes)}) with model={args.model}..."
    )
    cells = runner.run(
        bugs=canonical_bug_list,
        variants=variants,
        fl_modes=fl_modes,
        benchmark=adapter,
        localization_provider=localization_provider,
        repair_algorithm_factory=repair_algorithm_factory,
        output_dir=output_dir,
    )

    readme_path = runner.write_results(cells, output_dir)
    runner.copy_run_artifacts(cells, output_dir)
    print(f"\nResults written to: {output_dir}")
    print(f"README: {readme_path}")
    _print_llm_repair_summary(cells)
    return 0


def _build_algorithm_for_course_approach(
    args: argparse.Namespace,
    approach: "CourseApproach",
    localization_result: LocalizationResult,
    adapter: BugsInPyAdapter,
) -> RepairAlgorithm:
    """Build a fresh repair algorithm for one course-comparison cell.

    Dispatches on the approach's technique. A fresh ``OpenAICompatibleClient`` per
    LLM cell keeps that cell's query counter isolated from every other cell.
    """
    if approach.technique == "template":
        enabled_operators = [
            operator_key.strip()
            for operator_key in args.operators.split(",")
            if operator_key.strip()
        ]
        return TemplateRepairAlgorithm(
            localization_result=localization_result,
            adapter=adapter,
            config=TemplateRepairConfig(
                budget=args.budget,
                top_n_locations=args.top_n,
                enabled_operators=enabled_operators,
                timeout_per_test=args.timeout,
                stop_on_first=args.stop_on_first,
                regression_check=args.regression_check,
            ),
        )

    from apr_framework.repair.llm import (
        LLMRepairAlgorithm,
        LLMRepairConfig,
        OpenAICompatibleClient,
    )

    repair_config = LLMRepairConfig(
        model_name=args.model,
        temperature=args.temperature,
        max_patch_count=args.max_candidates,
        top_n_locations=args.top_n,
        llm_provider=args.llm_provider,
        base_url=args.llm_base_url,
        api_key_env_var=args.llm_api_key_env,
        system_prompt_name=args.system_prompt,
        timeout_seconds=args.timeout,
        budget=args.budget,
        stop_on_first=args.stop_on_first,
        regression_check=args.regression_check,
        context_enrichment=approach.context_enrichment,
        few_shot_count=0,
        iterative=approach.iterative,
        max_iterations=args.max_iterations,
        retrieval_budget=approach.retrieval_budget,
    )
    return LLMRepairAlgorithm(
        localization_result=localization_result,
        adapter=adapter,
        repair_config=repair_config,
        llm_client=OpenAICompatibleClient(repair_config),
    )


def _run_evaluate_course_comparison(
    args: argparse.Namespace, project_root: Path, adapter: BugsInPyAdapter
) -> int:
    """Compare every course approach on each bug, assessing and scoring every cell."""
    from apr_framework.core.models import CheckoutResult
    from apr_framework.evaluation.course_approaches import resolve_course_approaches
    from apr_framework.evaluation.course_comparison_runner import CourseComparisonRunner
    from apr_framework.localization import (
        FauxPyConfig,
        FauxPyLocalizer,
        FauxPyToolchain,
        HybridFaultLocalizer,
        PerfectFaultLocalizer,
    )

    bugs = _parse_bug_list(args.bugs)
    approach_labels = [
        label.strip() for label in args.approaches.split(",") if label.strip()
    ]
    approaches = resolve_course_approaches(approach_labels, args.retrieval_budget)
    fl_modes = [mode.strip() for mode in args.fl_modes.split(",") if mode.strip()]
    for fl_mode in fl_modes:
        if fl_mode not in ("auto", "perfect"):
            raise ConfigurationError(
                f"Invalid FL mode {fl_mode!r} — expected 'auto' or 'perfect'."
            )

    # FauxPy is needed only when an approach actually runs automated FL; the LLM-FL
    # approach and the perfect-FL cells do not touch it.
    needs_fauxpy = "auto" in fl_modes and any(
        not approach.uses_llm_fault_localization for approach in approaches
    )
    if needs_fauxpy:
        adapter.toolchain.ensure_installed()

    output_dir = Path(args.output_dir)
    if not output_dir.is_absolute():
        output_dir = project_root / output_dir
    runs_dir = Path(args.runs_dir)
    if not runs_dir.is_absolute():
        runs_dir = project_root / runs_dir

    fauxpy_toolchain = FauxPyToolchain(adapter.toolchain)
    projects_dir = adapter.toolchain.repo_dir / "projects"

    # Resolve and validate every bug's checkout up front, keyed by canonical bug.
    canonical_bug_list: list[BugIdentifier] = []
    checkouts: dict[BugIdentifier, CheckoutResult] = {}
    for bug in bugs:
        canonical_project = adapter.resolve_project(bug.project)
        canonical_bug = BugIdentifier(
            benchmark="bugsinpy", project=canonical_project, bug_id=bug.bug_id
        )
        worktree = (
            project_root
            / ".workspace"
            / "bugsinpy"
            / f"{canonical_project}_{bug.bug_id}"
            / canonical_project
        )
        if not worktree.exists():
            raise BenchmarkError(
                f"No checkout found for {bug.project} #{bug.bug_id} at {worktree}. "
                f"Run `python -m apr_framework bugsinpy checkout {bug.project} {bug.bug_id}` "
                "and then `compile` before evaluating."
            )
        canonical_bug_list.append(canonical_bug)
        checkouts[canonical_bug] = CheckoutResult(
            bug=canonical_bug, worktree=worktree, success=True, prepared=True
        )

    def _build_auto_localizer(canonical_bug: BugIdentifier) -> FaultLocalizer:
        worktree = checkouts[canonical_bug].worktree
        source_package = _infer_fauxpy_src(worktree, canonical_bug.project)
        bug_dir = (
            projects_dir / canonical_bug.project / "bugs" / str(canonical_bug.bug_id)
        )
        bug_test_targets = load_pytest_targets(bug_dir / "run_test.sh")
        bug_test_ids = extract_pytest_node_ids(bug_test_targets)

        def _make_sbfl() -> FauxPyLocalizer:
            return FauxPyLocalizer(
                FauxPyConfig(
                    src=source_package,
                    test_targets=bug_test_targets,
                    family="sbfl",
                    granularity=args.granularity,
                    failing_tests=bug_test_ids,
                    metric=args.localization_metric,
                ),
                fauxpy_toolchain,
            )

        def _make_mbfl() -> FauxPyLocalizer:
            return FauxPyLocalizer(
                FauxPyConfig(
                    src=source_package,
                    test_targets=bug_test_targets,
                    family="mbfl",
                    granularity=args.granularity,
                    failing_tests=bug_test_ids,
                    metric=args.mbfl_metric,
                    mutation_strategy="random",
                    mutation_budget=args.mutation_budget,
                    mutation_seed=args.seed,
                ),
                fauxpy_toolchain,
            )

        if args.fl_family == "sbfl":
            return _make_sbfl()
        if args.fl_family == "mbfl":
            return _make_mbfl()
        return HybridFaultLocalizer(_make_sbfl(), _make_mbfl())

    perfect_localizer = PerfectFaultLocalizer(adapter)

    def localization_provider(
        canonical_bug: BugIdentifier, approach: "CourseApproach", fl_mode: str
    ) -> LocalizationResult:
        checkout = checkouts[canonical_bug]
        if approach.uses_llm_fault_localization:
            return _build_llm_fault_localizer(args, adapter).localize(
                canonical_bug, checkout
            )
        if fl_mode == "perfect":
            return perfect_localizer.localize(canonical_bug, checkout)
        return _build_auto_localizer(canonical_bug).localize(canonical_bug, checkout)

    def repair_algorithm_factory(
        approach: "CourseApproach", localization_result: LocalizationResult
    ) -> RepairAlgorithm:
        return _build_algorithm_for_course_approach(
            args, approach, localization_result, adapter
        )

    def assessor_factory() -> "PatchAssessor | None":
        # Assessment is always on here: it is what makes the course-wide table's
        # quality column comparable across approaches.
        return _build_assessor(
            argparse.Namespace(**{**vars(args), "assess": True}), adapter
        )

    ranker = _build_ranker(args)
    repair_config_data = {
        "model": args.model,
        "temperature": args.temperature,
        "max_candidates": args.max_candidates,
        "top_n_locations": args.top_n,
        "max_iterations": args.max_iterations,
        "budget": args.budget,
        "timeout_per_test": args.timeout,
        "stop_on_first": args.stop_on_first,
        "regression_check": args.regression_check,
        "llm_provider": args.llm_provider,
        "llm_base_url": args.llm_base_url,
        "system_prompt": args.system_prompt,
        "fl_system_prompt": args.fl_system_prompt,
        "assess_system_prompt": args.assess_system_prompt,
        "fl_family": args.fl_family,
        "localization_metric": args.localization_metric,
        "granularity": args.granularity,
    }

    runner = CourseComparisonRunner(
        project_root=project_root,
        runs_dir=runs_dir,
        ranker=ranker,
        repair_config_data=repair_config_data,
        budget=args.budget,
        stop_on_first=args.stop_on_first,
    )

    print(
        f"Comparing {len(approaches)} approach(es) "
        f"({', '.join(approach.label for approach in approaches)}) on {len(bugs)} bug(s) "
        f"with model={args.model}; FL modes for the non-LLM-FL approaches: "
        f"{', '.join(fl_modes)}..."
    )
    cells = runner.run(
        bugs=canonical_bug_list,
        approaches=approaches,
        fl_modes=fl_modes,
        benchmark=adapter,
        localization_provider=localization_provider,
        repair_algorithm_factory=repair_algorithm_factory,
        assessor_factory=assessor_factory,
        output_dir=output_dir,
    )

    readme_path = runner.write_results(cells, output_dir)
    runner.copy_run_artifacts(cells, output_dir)
    print(f"\nResults written to: {output_dir}")
    print(f"README: {readme_path}")
    _print_course_comparison_summary(cells)
    return 0


def _print_course_comparison_summary(cells: list) -> None:
    """Print a compact per-cell summary table of the course-comparison matrix."""
    print("\n=== Course-wide comparison summary ===")
    header = (
        f"{'Bug':<14} {'Approach':<16} {'FL':<8} {'Q':>4} {'Gen':>4} "
        f"{'Plaus':>6} {'Corr':>5} {'Qual':>6} {'Sim':>6}"
    )
    print(header)
    print("-" * len(header))
    for cell in cells:
        bug_label = f"{cell.bug.project}#{cell.bug.bug_id}"
        if cell.error:
            short_error = " ".join(
                part.strip() for part in cell.error.splitlines() if part.strip()
            )
            if len(short_error) > 80:
                short_error = short_error[:80].rstrip() + " …"
            print(
                f"{bug_label:<14} {cell.approach_label:<16} {cell.fl_mode:<8} "
                f"ERROR: {short_error}"
            )
            continue
        query_count = (
            str(cell.llm_query_count) if cell.llm_query_count is not None else "-"
        )
        best_quality = (
            f"{cell.best_quality_score:.2f}"
            if cell.best_quality_score is not None
            else "-"
        )
        best_similarity = (
            f"{cell.best_context_similarity_score:.2f}"
            if cell.best_context_similarity_score is not None
            else "-"
        )
        print(
            f"{bug_label:<14} {cell.approach_label:<16} {cell.fl_mode:<8} "
            f"{query_count:>4} {cell.total_candidates_generated:>4} "
            f"{cell.plausible_count:>6} {cell.correct_count:>5} "
            f"{best_quality:>6} {best_similarity:>6}"
        )


def _print_llm_repair_summary(cells: list) -> None:
    """Print a compact per-cell summary table of the LLM repair matrix."""
    print("\n=== LLM repair comparison summary ===")
    header = (
        f"{'Bug':<14} {'Variant':<17} {'FL':<8} {'Q':>4} {'Gen':>4} "
        f"{'Plaus':>6} {'Corr':>5}"
    )
    print(header)
    print("-" * len(header))
    for cell in cells:
        bug_label = f"{cell.bug.project}#{cell.bug.bug_id}"
        if cell.error:
            short_error = " ".join(
                part.strip() for part in cell.error.splitlines() if part.strip()
            )
            if len(short_error) > 90:
                short_error = short_error[:90].rstrip() + " …"
            print(
                f"{bug_label:<14} {cell.variant_label:<17} {cell.fl_mode:<8} "
                f"ERROR: {short_error}"
            )
            continue
        query_count = (
            str(cell.llm_query_count) if cell.llm_query_count is not None else "-"
        )
        print(
            f"{bug_label:<14} {cell.variant_label:<17} {cell.fl_mode:<8} "
            f"{query_count:>4} {cell.total_candidates_generated:>4} "
            f"{cell.plausible_count:>6} {cell.correct_count:>5}"
        )


def _parse_args_and_resolve_project_root() -> tuple[argparse.Namespace, Path]:
    "Parse CLI arguments and resolve the project root, loading .env before any command runs"

    parser = build_parser()
    args = parser.parse_args()
    project_root = Path.cwd()
    load_dotenv(project_root / ".env")
    return args, project_root


def _get_benchmark_names() -> int:
    for name in list_benchmark_names():
        print(name)
    return 0


def _prompt_and_save_llm_api_key(args: argparse.Namespace, project_root: Path) -> int:
    api_key = getpass.getpass(f"Enter value for {args.llm_api_key_env}: ")
    _write_env_var(project_root / ".env", args.llm_api_key_env, api_key)
    print(f"Saved {args.llm_api_key_env} to {project_root / '.env'}")
    return 0


def _create_adapter_and_resolve_checked_out_bug(
    args: argparse.Namespace, project_root: Path
) -> tuple[BugsInPyAdapter, Path, Path]:

    adapter = create_bugsinpy_adapter(project_root)
    adapter.toolchain.ensure_installed()

    projects_dir = adapter.toolchain.repo_dir / "projects"
    project_dir = projects_dir / args.project
    if not project_dir.is_dir():
        raise BenchmarkError(f"No such BugsInPy project: {args.project}")

    bug_dir = project_dir / "bugs" / str(args.bug)
    if not bug_dir.is_dir():
        raise BenchmarkError(
            f"No such bug {args.bug} for BugsInPy project {args.project}"
        )

    destination = (
        project_root / ".workspace" / "bugsinpy" / f"{args.project}_{args.bug}"
    )
    worktree = destination / args.project
    if not worktree.exists():
        raise BenchmarkError(
            f"No checkout found at {worktree}. "
            f"Run `python -m apr_framework bugsinpy checkout {args.project} {args.bug}` first."
        )

    return adapter, worktree, bug_dir


def _print_repair_summary(cells: list[RepairCellResult]) -> None:
    print("\n=== Repair comparison summary ===")
    header = (
        f"{'Bug':<16} {'FL':<8} {'Gen':>4} {'Plaus':>6} {'Corr':>5} "
        f"{'GenRank':>8} {'RankRank':>9}"
    )
    print(header)
    print("-" * len(header))
    for cell in cells:
        bug_label = f"{cell.bug.project}#{cell.bug.bug_id}"
        if cell.error:
            short_error = " ".join(
                part.strip() for part in cell.error.splitlines() if part.strip()
            )
            if len(short_error) > 120:
                short_error = short_error[:120].rstrip() + " …"
            print(f"{bug_label:<16} {cell.fl_mode:<8} ERROR: {short_error}")
            continue
        generation_rank = (
            str(cell.generation_rank_of_first_correct)
            if cell.generation_rank_of_first_correct is not None
            else "-"
        )
        ranked_rank = (
            str(cell.ranked_rank_of_first_correct)
            if cell.ranked_rank_of_first_correct is not None
            else "-"
        )
        print(
            f"{bug_label:<16} {cell.fl_mode:<8} {cell.total_candidates_generated:>4} "
            f"{cell.plausible_count:>6} {cell.correct_count:>5} "
            f"{generation_rank:>8} {ranked_rank:>9}"
        )


def _build_techniques(
    make_sbfl: Callable[[BugIdentifier, str], FauxPyLocalizer],
    make_mbfl_traditional: Callable[[BugIdentifier], FauxPyLocalizer],
    make_mbfl: Callable[[BugIdentifier], FauxPyLocalizer],
    make_hybrid: Callable[[BugIdentifier], HybridFaultLocalizer],
) -> list[tuple[str, FaultLocalizer]]:
    """Return (name, per-bug-localizer) pairs for all techniques.

    Baselines are stock FauxPy 0.7.0 behaviour:
      - SBFL: Ochiai, Tarantula, D*
      - MBFL: Metallaxis with traditional (exhaustive) mutation strategy

    Extensions added by this framework:
      - SBFL: Jaccard (literature), WSBI (custom weighted metric via in-place FauxPy patch)
      - MBFL: Metallaxis with random mutation-budget selection (Task 3)
      - Hybrid: normalised weighted merge of SBFL + MBFL scores (Task 4)
    """

    class _BugLocalizer(FaultLocalizer):
        def __init__(self, factory: Callable[[BugIdentifier], FaultLocalizer]) -> None:
            self._factory = factory

        @property
        def name(self) -> str:
            return "per-bug-localizer"

        def localize(
            self,
            bug: BugIdentifier,
            checkout: CheckoutResult,
            test_result: TestRunResult | None = None,
        ) -> LocalizationResult:
            return self._factory(bug).localize(bug, checkout, test_result)

    return [
        ("SBFL-Ochiai (baseline)", _BugLocalizer(lambda bug: make_sbfl(bug, "ochiai"))),
        (
            "SBFL-Tarantula (baseline)",
            _BugLocalizer(lambda bug: make_sbfl(bug, "tarantula")),
        ),
        ("SBFL-DStar (baseline)", _BugLocalizer(lambda bug: make_sbfl(bug, "dstar"))),
        (
            "SBFL-Jaccard (extension)",
            _BugLocalizer(lambda bug: make_sbfl(bug, "jaccard")),
        ),
        ("SBFL-WSBI (extension)", _BugLocalizer(lambda bug: make_sbfl(bug, "wsbi"))),
        ("MBFL-Metallaxis (baseline)", _BugLocalizer(make_mbfl_traditional)),
        ("MBFL-Metallaxis-Random (extension)", _BugLocalizer(make_mbfl)),
        ("Hybrid SBFL+MBFL (extension)", _BugLocalizer(make_hybrid)),
    ]


def _parse_bug_list(spec: str) -> list[BugIdentifier]:
    bugs: list[BugIdentifier] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" not in part:
            raise ConfigurationError(
                f"Invalid bug specification {part!r} — expected project:bug_id"
            )
        project, bug_id_str = part.rsplit(":", 1)
        try:
            bug_id = int(bug_id_str)
        except ValueError:
            raise ConfigurationError(
                f"Invalid bug ID {bug_id_str!r} in {part!r} — must be an integer"
            )
        bugs.append(BugIdentifier(benchmark="bugsinpy", project=project, bug_id=bug_id))
    if not bugs:
        raise ConfigurationError("No bugs specified for evaluate-localization.")
    return bugs


def _print_summary(
    results: list[LocalizationTechniqueResult],
    top_ks: tuple[int, ...],
) -> None:
    print("\n=== Summary ===")
    header = f"{'Bug':<20} {'Technique':<30} {'Rank':>6}  " + "  ".join(
        f"Top-{k}" for k in sorted(top_ks)
    )
    print(header)
    print("-" * len(header))
    for r in results:
        bug_label = f"{r.bug.project}#{r.bug.bug_id}"
        rank_str = str(r.faulty_rank) if r.faulty_rank is not None else "—"
        top_k_str = "  ".join(
            ("✓" if r.top_k_hits.get(k) else "✗") for k in sorted(top_ks)
        )
        note = f"  ERROR: {r.error}" if r.error else ""
        print(f"{bug_label:<20} {r.technique:<30} {rank_str:>6}  {top_k_str}{note}")


def _resolve_localize_family(args: argparse.Namespace) -> str:
    return "mbfl" if args.mbfl else args.family


def resolve_effective_metric(args: argparse.Namespace, family: str) -> str:
    """Return the metric to use, falling back to the family's default when --metric is unset."""
    return args.metric or ("metallaxis" if family == "mbfl" else "ochiai")


def build_localize_config_data(
    args: argparse.Namespace,
    family: str,
    effective_metric: str,
    started_at: datetime,
) -> dict:
    """Build the config.json payload for a `localize` run."""
    config_data: dict = {
        "runner": "fauxpy-localizer",
        "backend": args.backend,
        "project": args.project,
        "bug_id": args.bug,
        "family": family,
        "granularity": args.granularity,
        "top_n": args.top_n,
        "mutation_strategy": args.mutation_strategy,
        "mutation_budget": args.mutation_budget,
        "mutation_seed": args.seed,
        "started_at": started_at.isoformat(),
    }
    if family == "hybrid":
        config_data["sbfl_metric"] = args.sbfl_metric
        config_data["mbfl_metric"] = args.mbfl_metric
        config_data["sbfl_weight"] = args.sbfl_weight
        config_data["mbfl_weight"] = args.mbfl_weight
    else:
        config_data["metric"] = effective_metric
    return config_data


def _validate_localize_args(args: argparse.Namespace, family: str) -> None:
    if family == "hybrid" and args.metric is not None:
        raise ConfigurationError(
            "Use --sbfl-metric and --mbfl-metric for hybrid localization; "
            "--metric is only for single-family SBFL or MBFL runs."
        )
    if family == "hybrid":
        if args.sbfl_weight < 0 or args.mbfl_weight < 0:
            raise ConfigurationError(
                "Hybrid localization weights must be non-negative."
            )
        if args.sbfl_weight == 0 and args.mbfl_weight == 0:
            raise ConfigurationError("Hybrid localization weights cannot both be zero.")


def _infer_fauxpy_src(worktree: Path, project: str) -> str:
    """
    Helper function to find --src (source code) of the project to give the FauxPy
    """
    # Convert project names to Python package style
    package_name = project.replace("-", "_")

    # Enumerate real names to handle case-sensitive filesystems inside Docker
    try:
        entries = {e.name: e for e in worktree.iterdir()}
    except OSError:
        return "."

    for candidate in [package_name, project, package_name.lower(), project.lower()]:
        entry = entries.get(candidate)
        if entry is not None and entry.is_dir():
            return candidate

    for candidate in [
        f"{package_name}.py",
        f"{project}.py",
        f"{package_name.lower()}.py",
    ]:
        entry = entries.get(candidate)
        if entry is not None and entry.is_file():
            return candidate

    return "."


def _parse_fauxpy_failing_tests_command(value: str | None) -> list[str]:
    if value is None:
        return []

    stripped = value.strip()
    if not stripped:
        return []
    if stripped.startswith("[") and stripped.endswith("]"):
        stripped = stripped[1:-1]

    return [item.strip() for item in stripped.split(",") if item.strip()]


def _parse_fauxpy_test_targets_command(values: list[str] | None) -> list[str]:
    if not values:
        return []

    targets: list[str] = []
    for value in values:
        targets.extend(item.strip() for item in value.split(",") if item.strip())
    return targets
