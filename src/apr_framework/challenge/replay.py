"""Replay a case three ways: buggy, candidate, developer-fixed.

``run_case`` checks the bug out fresh from BugsInPy, compiles it in the executor,
then, for every repeat and every variant of the case:

1. applies the variant's patch through the shared validation path
   (``apply_patch_and_validate``) with the evaluator files locked and verified;
2. runs the original checks (the bug's trigger test and regression suite);
3. while the patch is still applied, runs the witness (Type C) from an isolated
   copy outside the checkout, through ``run_tests(command=...)``;
4. restores the checkout and confirms it is byte-identical to the fresh one.

Type T cases add one more variant, ``candidate_as_submitted``: the agent's patch
*including* its test edits, applied without the integrity screen, so the report can
show the modified test passing next to the original test failing on the
candidate's non-test changes. That variant is labelled, never called plausible.

The observed outcomes are compared with the expectations in ``case.json``. The
replay "matches" only when every expected field agrees on every repeat.
"""

import hashlib
import json
import logging
import re
import shutil
import subprocess
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from apr_framework.benchmarks.bugsinpy import BugsInPyAdapter
from apr_framework.benchmarks.executor_view import remove_host_tree
from apr_framework.challenge.bundle import CaseBundle
from apr_framework.challenge.multi_file_patch import (
    MultiFilePatch,
    PatchApplyError,
    without_evaluator_files,
)
from apr_framework.core.exceptions import APRFrameworkError, BenchmarkError
from apr_framework.core.models import BugIdentifier, CheckoutResult, TestRunResult
from apr_framework.repair.evaluator_integrity import (
    EvaluatorGuard,
    classify_evaluator_path,
    list_checkout_files,
    purge_bytecode_caches,
    read_checkout_entry,
)
from apr_framework.repair.patch_applier import (
    ValidationOutcome,
    apply_patch_and_validate,
    is_passing_trigger_run,
)
from apr_framework.repair.regression import RegressionContext, build_regression_context

logger = logging.getLogger(__name__)

CHALLENGE_WORKSPACE_PREFIX = "challenge_"
WITNESS_WORKSPACE_NAME = "_witness"
DEFAULT_RESULTS_DIR = Path(".workspace") / "challenge_results"


class ReplayError(APRFrameworkError):
    """The replay itself could not be trusted (e.g. a variant left residue)."""


@dataclass(frozen=True)
class ReplayOptions:
    """How to replay a case.

    Fields:
        patch_path:      Replay this diff as the candidate instead of the case's
                         ``candidate.diff``.
        repeats:         Clean repeats of every variant.
        timeout_seconds: Wall-clock limit per test command.
        reuse_checkout:  Reuse an existing compiled challenge checkout instead of
                         checking out and compiling afresh (faster, recorded).
    """

    patch_path: Path | None = None
    repeats: int = 3
    timeout_seconds: int = 300
    reuse_checkout: bool = False


@dataclass(frozen=True)
class CandidateSource:
    """The candidate diff being replayed and where it came from."""

    diff_text: str
    label: str
    sha256: str
    is_case_candidate: bool


@dataclass
class VariantObservation:
    """What one variant did in one repeat."""

    variant: str
    repeat: int
    trigger: str
    regression: str
    witness: str | None
    integrity: str
    detail: str = ""
    regression_new_failures: list[str] = field(default_factory=list)
    witness_output_tail: str = ""


@dataclass
class ReplayResult:
    """Everything a replay established, serialised to JSON."""

    case_id: str
    case_type: str
    case_status: str
    bug: str
    candidate_source: str
    candidate_sha256: str
    candidate_is_case_candidate: bool
    repeats: int
    started_at: str
    finished_at: str
    environment: dict[str, str]
    observations: list[VariantObservation]
    mismatches: list[str]
    unchecked_variants: list[str]
    witness_classification: str | None

    @property
    def matched(self) -> bool:
        return not self.mismatches

    def to_json(self) -> str:
        payload = asdict(self)
        payload["matched"] = self.matched
        return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


@dataclass
class _ReplayContext:
    """Shared state for all variants of one replay."""

    adapter: BugsInPyAdapter
    checkout: CheckoutResult
    evaluator_guard: EvaluatorGuard
    regression_context: RegressionContext
    witness_command: str | None
    timeout_seconds: int
    clean_fingerprint: str


def run_case(
    bundle: CaseBundle,
    adapter: BugsInPyAdapter,
    project_root: Path,
    options: ReplayOptions,
) -> ReplayResult:
    """Replay a case three ways for ``options.repeats`` clean repeats."""
    started_at = datetime.now(timezone.utc)
    candidate_source = resolve_candidate_source(bundle, options.patch_path)
    checkout = check_out_and_compile_bug(
        bundle, adapter, project_root, options.reuse_checkout
    )
    evaluator_guard = EvaluatorGuard.for_worktree(checkout.worktree)
    regression_context = build_regression_context(
        adapter, checkout, enabled=True, timeout=options.timeout_seconds
    )
    variant_diffs = build_variant_diffs(
        bundle, adapter, evaluator_guard, candidate_source.diff_text
    )

    with isolated_witness_copy(bundle, adapter, project_root) as witness_command:
        context = _ReplayContext(
            adapter=adapter,
            checkout=checkout,
            evaluator_guard=evaluator_guard,
            regression_context=regression_context,
            witness_command=witness_command,
            timeout_seconds=options.timeout_seconds,
            clean_fingerprint=fingerprint_checkout(checkout.worktree),
        )
        observations = [
            observe_variant(variant, variant_diffs[variant], context, repeat)
            for repeat in range(1, options.repeats + 1)
            for variant in bundle.variants
        ]

    return compare_observations_with_expectations(
        bundle,
        observations,
        candidate_source,
        describe_environment(checkout, project_root, options.reuse_checkout),
        started_at,
        options.repeats,
    )


def resolve_candidate_source(
    bundle: CaseBundle, patch_path: Path | None
) -> CandidateSource:
    case_diff_text = bundle.read_candidate_diff()
    if patch_path is None:
        return CandidateSource(
            diff_text=case_diff_text,
            label=f"{bundle.candidate_diff_path.name} (the case's candidate)",
            sha256=_sha256_text(case_diff_text),
            is_case_candidate=True,
        )
    diff_text = patch_path.read_text(encoding="utf-8")
    return CandidateSource(
        diff_text=diff_text,
        label=str(patch_path),
        sha256=_sha256_text(diff_text),
        is_case_candidate=_sha256_text(diff_text) == _sha256_text(case_diff_text),
    )


def check_out_and_compile_bug(
    bundle: CaseBundle,
    adapter: BugsInPyAdapter,
    project_root: Path,
    reuse_checkout: bool,
) -> CheckoutResult:
    """Check the bug out from BugsInPy into a challenge-only directory and compile it."""
    destination = (
        project_root
        / ".workspace"
        / "bugsinpy"
        / f"{CHALLENGE_WORKSPACE_PREFIX}{bundle.case_id}"
    )
    return check_out_and_compile_into(bundle.bug, adapter, destination, reuse_checkout)


def check_out_and_compile_into(
    bug: BugIdentifier,
    adapter: BugsInPyAdapter,
    destination: Path,
    reuse_checkout: bool = False,
) -> CheckoutResult:
    """Fresh BugsInPy checkout + ``bugsinpy-safe-compile`` under ``destination``.

    ``destination`` must lie under the BugsInPy workspace so the executor sees it.
    With ``reuse_checkout``, an already compiled checkout there is kept.
    """
    worktree = destination / adapter.resolve_project(bug.project)
    if reuse_checkout and (worktree / "bugsinpy_compile_flag").is_file():
        logger.info("Reusing compiled checkout %s", worktree)
        return CheckoutResult(bug=bug, worktree=worktree, success=True, prepared=True)
    if destination.exists():
        adapter.toolchain.remove_workspace_tree(destination)
    checkout = adapter.checkout(bug, destination)
    adapter.prepare_environment(checkout)
    return checkout


def build_variant_diffs(
    bundle: CaseBundle,
    adapter: BugsInPyAdapter,
    evaluator_guard: EvaluatorGuard,
    candidate_diff_text: str,
) -> dict[str, str]:
    """Return the diff each variant applies (the buggy variant applies nothing)."""
    reference_diff_text = adapter.get_reference_patch(bundle.bug)
    if reference_diff_text is None:
        raise BenchmarkError(
            f"No developer fix (bug_patch.txt) for {bundle.bug.project}#"
            f"{bundle.bug.bug_id}; the developer-fixed control cannot run."
        )
    declared_test_files = evaluator_guard.declared_test_files
    variant_diffs = {
        "buggy": "",
        "developer_fixed": reference_diff_text,
        "candidate": candidate_diff_text,
    }
    if bundle.case_type == "T":
        variant_diffs["candidate_as_submitted"] = candidate_diff_text
        variant_diffs["candidate"] = without_evaluator_files(
            candidate_diff_text,
            evaluator_guard.worktree.name,
            lambda relative_path: (
                classify_evaluator_path(relative_path, declared_test_files) is not None
            ),
        )
    return variant_diffs


@contextmanager
def isolated_witness_copy(
    bundle: CaseBundle, adapter: BugsInPyAdapter, project_root: Path
) -> Iterator[str | None]:
    """Copy the witness outside the checkout and yield the command that runs it.

    The witness lives in its own directory under the BugsInPy workspace (so the
    executor can see it) but outside the checkout, so the project's conftest.py
    and test configuration never apply to it. It runs with unittest discovery
    from the checkout as the working directory, so it imports the project's
    (patched) source. Hyphens in the case id become underscores in the copied
    file name, because unittest only discovers importable module names.
    """
    witness_path = bundle.witness_path
    if witness_path is None:
        yield None
        return
    witness_directory = (
        project_root
        / ".workspace"
        / "bugsinpy"
        / WITNESS_WORKSPACE_NAME
        / f"{bundle.case_id}-{uuid.uuid4().hex[:8]}"
    )
    witness_file_name = "test_witness_" + re.sub(r"\W", "_", bundle.case_id) + ".py"
    witness_directory.mkdir(parents=True)
    try:
        shutil.copyfile(witness_path, witness_directory / witness_file_name)
        executor_directory = adapter.toolchain.executor_path(witness_directory)
        yield (
            f"python -m unittest discover -v -s {executor_directory} "
            f"-t {executor_directory} -p {witness_file_name}"
        )
    finally:
        remove_host_tree(witness_directory)


def observe_variant(
    variant: str, diff_text: str, context: _ReplayContext, repeat: int
) -> VariantObservation:
    """Run one variant once and confirm the checkout is clean afterwards."""
    if variant == "candidate_as_submitted":
        observation = observe_candidate_as_submitted(diff_text, context, repeat)
    else:
        observation = observe_through_integrity_gate(
            variant, diff_text, context, repeat
        )
    if fingerprint_checkout(context.checkout.worktree) != context.clean_fingerprint:
        raise ReplayError(
            f"Variant {variant!r} (repeat {repeat}) left the checkout changed; "
            "the replay cannot claim clean repeats."
        )
    logger.info(
        "%s repeat %d: trigger=%s regression=%s witness=%s integrity=%s",
        variant,
        repeat,
        observation.trigger,
        observation.regression,
        observation.witness,
        observation.integrity,
    )
    return observation


def observe_through_integrity_gate(
    variant: str, diff_text: str, context: _ReplayContext, repeat: int
) -> VariantObservation:
    """Apply a variant through the shared validation path, then run the witness."""
    try:
        patch = MultiFilePatch.plan(diff_text, context.checkout.worktree)
    except PatchApplyError as apply_error:
        return VariantObservation(
            variant=variant,
            repeat=repeat,
            trigger="not_applied",
            regression="not_run",
            witness="not_run" if context.witness_command else None,
            integrity="ok",
            detail=str(apply_error),
        )

    witness_runs: list[TestRunResult] = []
    outcome = apply_patch_and_validate(
        patch.apply,
        patch.restore,
        context.adapter,
        context.checkout,
        context.regression_context,
        context.timeout_seconds,
        evaluator_guard=context.evaluator_guard,
        diff_text=diff_text,
        touched_paths=patch.touched_paths,
        run_while_applied=(
            (lambda: witness_runs.append(run_witness(context)))
            if context.witness_command
            else None
        ),
    )
    return VariantObservation(
        variant=variant,
        repeat=repeat,
        trigger=describe_trigger(outcome),
        regression=describe_regression(outcome, context.regression_context),
        witness=describe_witness(witness_runs, context.witness_command),
        integrity=outcome.integrity.integrity,
        detail=outcome.integrity.detail or outcome.apply_error,
        regression_new_failures=list(outcome.regression_new_failures),
        witness_output_tail=_tail(witness_runs[0].raw_output) if witness_runs else "",
    )


def observe_candidate_as_submitted(
    diff_text: str, context: _ReplayContext, repeat: int
) -> VariantObservation:
    """Apply a Type T candidate *with* its test edits and run its (modified) trigger.

    This deliberately bypasses the integrity screen: it reproduces the verdict the
    agent's own, edited evaluator gave. The evaluator files are restored and
    verified afterwards like any other variant.
    """
    screening_verdict = context.evaluator_guard.screen_candidate(diff_text)
    integrity_label = (
        f"bypassed: {screening_verdict.integrity} {list(screening_verdict.paths)}"
        if screening_verdict.is_rejected
        else "ok"
    )
    try:
        patch = MultiFilePatch.plan(diff_text, context.checkout.worktree)
    except PatchApplyError as apply_error:
        return VariantObservation(
            variant="candidate_as_submitted",
            repeat=repeat,
            trigger="not_applied",
            regression="not_run",
            witness=None,
            integrity=integrity_label,
            detail=str(apply_error),
        )

    touched_files = [
        context.checkout.worktree / relative_path
        for relative_path in patch.touched_paths
    ]
    context.evaluator_guard.assert_evaluator_files_unchanged()
    patch.apply()
    try:
        purge_bytecode_caches(touched_files)
        trigger_run = context.adapter.run_tests(
            context.checkout, timeout=context.timeout_seconds
        )
    finally:
        patch.restore()
        purge_bytecode_caches(touched_files)
    mutation_verdict = context.evaluator_guard.verify_evaluator_files()
    return VariantObservation(
        variant="candidate_as_submitted",
        repeat=repeat,
        trigger=_describe_trigger_run(trigger_run),
        regression="not_run",
        witness=None,
        integrity=integrity_label,
        detail=(
            "modified tests: the trigger ran against the candidate's edited test files"
            + (f"; {mutation_verdict.detail}" if mutation_verdict.is_rejected else "")
        ),
    )


def run_witness(context: _ReplayContext) -> TestRunResult:
    assert context.witness_command is not None
    return context.adapter.run_tests(
        context.checkout,
        timeout=context.timeout_seconds,
        command=context.witness_command,
    )


def describe_trigger(outcome: ValidationOutcome) -> str:
    if outcome.integrity.is_rejected and not outcome.applied:
        return "not_run"
    if not outcome.applied:
        return "not_applied"
    return _describe_trigger_run(outcome.trigger_test_run_result)


def describe_regression(
    outcome: ValidationOutcome, regression_context: RegressionContext
) -> str:
    if not outcome.applied or not outcome.trigger_passed:
        return "not_run"
    if not regression_context.enabled:
        return "not_checked"
    if outcome.regression_untrusted:
        return "untrusted"
    if outcome.regression_new_failures:
        return "new_failures"
    return "ok"


def describe_witness(
    witness_runs: list[TestRunResult], witness_command: str | None
) -> str | None:
    if witness_command is None:
        return None
    if not witness_runs:
        return "not_run"
    witness_run = witness_runs[0]
    if witness_run.total_count == 0:
        return "no_result"
    return "pass" if is_passing_trigger_run(witness_run) else "fail"


def compare_observations_with_expectations(
    bundle: CaseBundle,
    observations: list[VariantObservation],
    candidate_source: CandidateSource,
    environment: dict[str, str],
    started_at: datetime,
    repeats: int,
) -> ReplayResult:
    """Check every observed field against ``case.json``; collect the mismatches."""
    unchecked_variants = (
        []
        if candidate_source.is_case_candidate
        else [variant for variant in bundle.variants if variant.startswith("candidate")]
    )
    mismatches: list[str] = []
    for observation in observations:
        if observation.variant in unchecked_variants:
            continue
        expected = bundle.expected_outcome(observation.variant)
        for field_name, expected_value in expected.items():
            observed_value = getattr(observation, field_name)
            if expected_value is not None and observed_value != expected_value:
                mismatches.append(
                    f"{observation.variant} repeat {observation.repeat}: "
                    f"{field_name} expected {expected_value}, observed {observed_value}"
                )
    mismatches.extend(find_inconsistent_repeats(observations))
    return ReplayResult(
        case_id=bundle.case_id,
        case_type=bundle.case_type,
        case_status=bundle.status,
        bug=f"{bundle.bug.project}#{bundle.bug.bug_id}",
        candidate_source=candidate_source.label,
        candidate_sha256=candidate_source.sha256,
        candidate_is_case_candidate=candidate_source.is_case_candidate,
        repeats=repeats,
        started_at=started_at.isoformat(),
        finished_at=datetime.now(timezone.utc).isoformat(),
        environment=environment,
        observations=observations,
        mismatches=mismatches,
        unchecked_variants=unchecked_variants,
        witness_classification=classify_witness_outcome(observations),
    )


def find_inconsistent_repeats(observations: list[VariantObservation]) -> list[str]:
    """Every repeat of a variant must observe the same trigger/regression/witness."""
    outcomes_by_variant: dict[str, set[tuple[str, str, str | None]]] = {}
    for observation in observations:
        outcomes_by_variant.setdefault(observation.variant, set()).add(
            (observation.trigger, observation.regression, observation.witness)
        )
    return [
        f"{variant}: repeats disagree {sorted(outcomes, key=str)}"
        for variant, outcomes in outcomes_by_variant.items()
        if len(outcomes) > 1
    ]


def classify_witness_outcome(observations: list[VariantObservation]) -> str | None:
    """Separate a residual bug from a new regression (case contract item 3)."""
    first_repeat = {
        observation.variant: observation.witness
        for observation in observations
        if observation.repeat == 1
    }
    buggy, candidate, developer_fixed = (
        first_repeat.get("buggy"),
        first_repeat.get("candidate"),
        first_repeat.get("developer_fixed"),
    )
    if candidate is None:
        return None
    if developer_fixed != "pass":
        return "invalid witness: the developer fix does not pass it"
    if candidate == "pass":
        return "the candidate passes the witness"
    if buggy == "fail":
        return (
            "residual bug: the buggy revision and the candidate both fail the "
            "witness; the developer fix passes it"
        )
    if buggy == "pass":
        return (
            "new regression: the buggy revision and the developer fix both pass the "
            "witness; the candidate fails it"
        )
    return f"unclassified (buggy={buggy}, candidate={candidate})"


def fingerprint_checkout(worktree: Path) -> str:
    """Hash every checkout file (paths and bytes) to prove a clean restore."""
    digest = hashlib.sha256()
    for relative_path in list_checkout_files(worktree):
        digest.update(relative_path.encode("utf-8") + b"\0")
        digest.update((read_checkout_entry(worktree / relative_path) or b"") + b"\0")
    return digest.hexdigest()


def describe_environment(
    checkout: CheckoutResult, project_root: Path, reused_checkout: bool
) -> dict[str, str]:
    """Record what a second person needs to reproduce this replay."""
    bug_info = (checkout.worktree / "bugsinpy_bug.info").read_text(encoding="utf-8")
    python_version_match = re.search(r'python_version="([^"]+)"', bug_info)
    return {
        "python_version": python_version_match.group(1)
        if python_version_match
        else "?",
        "framework_commit": _git_describe(project_root),
        "bugsinpy_commit": _git_describe(project_root / ".tools" / "bugsinpy"),
        "checkout": "reused" if reused_checkout else "fresh checkout and compile",
    }


def default_result_path(project_root: Path, result: ReplayResult) -> Path:
    stamp = result.started_at.replace(":", "").replace("-", "")[:15]
    return project_root / DEFAULT_RESULTS_DIR / result.case_id / f"{stamp}.json"


def _describe_trigger_run(trigger_run: TestRunResult) -> str:
    if trigger_run.total_count == 0:
        return "no_result"
    return "pass" if is_passing_trigger_run(trigger_run) else "fail"


def _git_describe(directory: Path) -> str:
    try:
        commit = subprocess.run(
            ["git", "-C", str(directory), "rev-parse", "--short=12", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        dirty = subprocess.run(
            [
                "git",
                "-C",
                str(directory),
                "status",
                "--porcelain",
                "--untracked-files=no",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return f"{commit}{' (uncommitted changes)' if dirty else ''}"


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _tail(text: str, line_count: int = 12) -> str:
    return "\n".join(text.splitlines()[-line_count:])
