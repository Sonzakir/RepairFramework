"""Fix Grip: which parts of a fix do the project's tests pin down?

1. The fix runs twice under the evidence driver (outcomes per test, plus which of
   the fix's changed lines ran). Tests that pass in both runs are the *stable
   passing* tests; the buggy version runs once for context.
2. Every variant (``variants.py``) runs once. A variant is *detected* when a
   stable passing test fails or errors with it (an import or collection failure
   counts), and *not detected* when every stable passing test still passes.
3. Hunks: ``pinned`` (its revert is detected), ``not pinned`` (its lines run but
   no test notices the revert), ``not executed`` (no test runs its lines),
   ``non-behavioural`` (annotations or docstrings only; not run), ``unknown``
   (the revert could not be judged: timeout, did not apply).

"Not detected" says that no test notices the change, never that the variant is
wrong: it may be equivalent to the fix (the same rule as Strict Repro's
unconfirmed probes).
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from apr_framework.behaviour.focus import focus_id, function_spans, innermost_function
from apr_framework.behaviour.observe import Observation, ObservationRunner
from apr_framework.challenge.multi_file_patch import MultiFilePatch, render_file_diff
from apr_framework.challenge.replay import fingerprint_checkout
from apr_framework.core.exceptions import ConfigurationError, EvaluationError
from apr_framework.evidence.target import EvidenceTarget, SuiteCommand
from apr_framework.evidence.versions import (
    CANDIDATE,
    FixVersion,
    buggy_version,
    strip_evaluator_edits,
)
from apr_framework.grip.variants import (
    Hunk,
    Variant,
    clause_variants,
    hunk_variants,
    hunks_of,
    line_probe_variants,
    variant_diff,
)
from apr_framework.repair.evaluator_integrity import EvaluatorGuard
from apr_framework.repair.unified_diff import normalise_diff_path, parse_unified_diff
from apr_framework.repro.probes import lines_changed_by_fix

DETECTED = "detected"
VARIANT_TIMEOUT_FACTOR = 5
NOT_DETECTED = "not_detected"
FAILING_OUTCOMES = ("failed", "error")


@dataclass(frozen=True)
class GripOptions:
    """Fields:
    max_probes:          Line variants to run (hunk reverts always run).
    seed:                Seed for choosing the line variants.
    timeout_seconds:     Wall-clock limit for one run of the suite.
    min_variant_timeout: Floor of a variant's limit, which is otherwise five
                         times the fix's own run time (a mutated loop may
                         never end).
    """

    max_probes: int = 24
    seed: str = "grip"
    timeout_seconds: float = 900
    min_variant_timeout: float = 60


@dataclass
class VariantResult:
    variant: Variant
    status: str
    detected_by: list[str] = field(default_factory=list)
    detail: str = ""

    def to_json(self) -> dict:
        payload = {
            key: value
            for key, value in self.variant.__dict__.items()
            if key != "file_text"
        }
        return {
            **payload,
            "status": self.status,
            "detected_by": self.detected_by,
            "detail": self.detail,
        }


@dataclass
class HunkResult:
    hunk: Hunk
    verdict: str
    executed: bool | None
    detected_by: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        payload = {
            key: value
            for key, value in self.hunk.__dict__.items()
            if key != "reverted_text"
        }
        return {
            **payload,
            "location": self.hunk.location,
            "verdict": self.verdict,
            "executed": self.executed,
            "detected_by": self.detected_by,
        }


@dataclass
class GripResult:
    target: dict
    fix_label: str
    stripped_paths: list[str]
    started_at: str
    finished_at: str
    suite: str
    fix_counts: dict[str, int]
    buggy_counts: dict[str, int]
    fixed_by_the_fix: list[str]
    stable_passing: int
    unstable_tests: list[str]
    executed_lines: list[str]
    hunks: list[HunkResult]
    variants: list[VariantResult]
    trigger: str | None = None
    trigger_with_fix: dict[str, str] = field(default_factory=dict)
    trigger_fails_in_suite: list[str] = field(default_factory=list)
    fixed_texts: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> dict:
        return {
            "target": self.target,
            "fix": self.fix_label,
            "stripped_paths": self.stripped_paths,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "suite": self.suite,
            "fix_counts": self.fix_counts,
            "buggy_counts": self.buggy_counts,
            "fixed_by_the_fix": self.fixed_by_the_fix,
            "stable_passing_tests": self.stable_passing,
            "unstable_tests": self.unstable_tests,
            "executed_lines": self.executed_lines,
            "trigger": self.trigger,
            "trigger_with_fix": self.trigger_with_fix,
            "trigger_fails_in_suite": self.trigger_fails_in_suite,
            "hunks": [hunk.to_json() for hunk in self.hunks],
            "variants": [variant.to_json() for variant in self.variants],
        }


def run_grip(
    target: EvidenceTarget,
    fix: FixVersion,
    options: GripOptions,
    *,
    log: Callable[[str], None] = lambda message: None,
) -> GripResult:
    started_at = datetime.now(timezone.utc).isoformat()
    worktree = target.worktree
    fix = strip_evaluator_edits(fix, worktree, target.declared_test_files)
    python_files = _python_files(fix, worktree)
    if not python_files:
        raise ConfigurationError(
            "The fix changes no Python source file outside the tests."
        )
    hunks: list[Hunk] = []
    for path, (original_text, fixed_text) in sorted(python_files.items()):
        hunks.extend(
            hunks_of(path, original_text, fixed_text, start_index=len(hunks) + 1)
        )
    variants = (
        hunk_variants(hunks)
        + clause_variants(python_files, hunks)
        + line_probe_variants(
            python_files, hunks, max_probes=options.max_probes, seed=options.seed
        )
    )
    focus_ids, watch, inside_functions = _watch_plan(python_files)
    trigger = combined_trigger(target)
    log(f"Hunks: {[hunk.location for hunk in hunks]}; variants: {len(variants)}")

    guard = EvaluatorGuard.for_worktree(worktree)
    clean_fingerprint = fingerprint_checkout(worktree)
    with target.scratch_directory("grip") as scratch:
        runner = ObservationRunner(
            target, scratch, timeout_seconds=options.timeout_seconds
        )
        baseline = runner.observe(
            fix, focus_ids, record_calls=False, watch=watch, label="fix"
        )
        if not baseline.observed:
            raise EvaluationError(
                f"The fix could not be observed ({baseline.status}: {baseline.detail}).\n"
                f"{baseline.output_tail}"
            )
        rerun = runner.observe(fix, [], record_calls=False, label="fix#2")
        buggy = runner.observe(buggy_version(), [], record_calls=False, label="buggy")
        stable_passing, unstable = _stable_passing(baseline, rerun)
        # The bug's failing test also runs alone, as plausibility checks it: a test
        # can pass alone and fail inside its whole file (black#1 does).
        trigger_runs = TriggerRuns()
        if trigger is not None:
            trigger_runs.fix = runner.observe(
                fix, [], record_calls=False, suite=trigger, label="fix (trigger)"
            )
            rerun_trigger = runner.observe(
                fix, [], record_calls=False, suite=trigger, label="fix#2 (trigger)"
            )
            trigger_runs.buggy = runner.observe(
                buggy_version(),
                [],
                record_calls=False,
                suite=trigger,
                label="buggy (trigger)",
            )
            trigger_runs.stable_passing, _ = _stable_passing(
                trigger_runs.fix, rerun_trigger
            )
        log(
            f"fix {baseline.outcome_counts()}, buggy {buggy.outcome_counts()}, "
            f"trigger alone with the fix: {trigger_runs.fix_outcomes()}"
        )
        # A mutated loop often never ends: a variant gets a few times the fix's own
        # run time, not the whole suite budget.
        variant_timeout = min(
            options.timeout_seconds,
            max(
                options.min_variant_timeout, VARIANT_TIMEOUT_FACTOR * rerun.wall_seconds
            ),
        )
        log(f"Variant timeout: {variant_timeout:.0f}s")
        results: list[VariantResult] = []
        for variant in variants:
            version = FixVersion(
                label=variant.variant_id,
                kind=CANDIDATE,
                diff_text=variant_diff(
                    variant,
                    python_files,
                    _other_sections(fix.diff_text, worktree.name, variant.path),
                ),
                source=f"{variant.family} {variant.path}:{variant.line}",
            )
            observation = runner.observe(
                version,
                [],
                record_calls=False,
                label=variant.variant_id,
                timeout_seconds=variant_timeout,
            )
            trigger_observation = (
                runner.observe(
                    version,
                    [],
                    record_calls=False,
                    suite=trigger,
                    label=f"{variant.variant_id} (trigger)",
                    timeout_seconds=variant_timeout,
                )
                if trigger is not None and trigger_runs.stable_passing
                else None
            )
            results.append(
                _judge(
                    variant,
                    observation,
                    stable_passing,
                    trigger_observation,
                    trigger_runs.stable_passing,
                )
            )
            log(
                f"{variant.variant_id}: {results[-1].status} {results[-1].detected_by[:3]}"
            )
            if fingerprint_checkout(worktree) != clean_fingerprint:
                raise EvaluationError(
                    f"Variant {variant.variant_id} left the checkout changed."
                )

    if fingerprint_checkout(worktree) != clean_fingerprint:
        raise EvaluationError(
            f"{worktree} is not byte-identical to its state before the run."
        )
    verdict = guard.verify_evaluator_files()
    if verdict.is_rejected:
        raise EvaluationError(
            f"Evaluator files changed during the run: {verdict.detail}"
        )

    executed_lines = sorted(
        {line for record in baseline.tests.values() for line in record.lines}
    )
    hunk_results = [
        _hunk_result(hunk, results, executed_lines, inside_functions) for hunk in hunks
    ]
    if trigger_runs.fix is not None:
        fixed_by_the_fix = sorted(
            f"{test_id} (alone)"
            for test_id in trigger_runs.stable_passing
            if _failed(trigger_runs.buggy, test_id)
        )
    else:
        fixed_by_the_fix = sorted(
            test_id for test_id in stable_passing if _failed(buggy, test_id)
        )
    trigger_fails_in_suite = sorted(
        test_id for test_id in trigger_runs.stable_passing if _failed(baseline, test_id)
    )
    return GripResult(
        target=target.describe(),
        fix_label=fix.label,
        stripped_paths=list(fix.stripped_paths),
        started_at=started_at,
        finished_at=datetime.now(timezone.utc).isoformat(),
        suite=target.suite.describe(),
        fix_counts=baseline.outcome_counts(),
        buggy_counts=buggy.outcome_counts() if buggy.observed else {},
        fixed_by_the_fix=fixed_by_the_fix,
        stable_passing=len(stable_passing),
        unstable_tests=unstable,
        executed_lines=executed_lines,
        hunks=hunk_results,
        variants=results,
        trigger=trigger.describe() if trigger is not None else None,
        trigger_with_fix=trigger_runs.fix_outcomes(),
        trigger_fails_in_suite=trigger_fails_in_suite,
        fixed_texts={path: texts[1] for path, texts in python_files.items()},
    )


@dataclass
class TriggerRuns:
    """The bug's failing test, run alone: with the fix (twice) and on the buggy code."""

    fix: Observation | None = None
    buggy: Observation | None = None
    stable_passing: set[str] = field(default_factory=set)

    def fix_outcomes(self) -> dict[str, str]:
        if self.fix is None or not self.fix.observed:
            return {}
        return {test_id: record.outcome for test_id, record in self.fix.tests.items()}


def combined_trigger(target: EvidenceTarget) -> SuiteCommand | None:
    """One command that runs every trigger test of the target, or None."""
    commands = [command for command in target.trigger_tests if command.selectors]
    if not commands or len({command.runner for command in commands}) != 1:
        return None
    selectors: list[str] = []
    for command in commands:
        selectors.extend(
            selector for selector in command.selectors if selector not in selectors
        )
    return SuiteCommand(runner=commands[0].runner, arguments=tuple(selectors))


def _failed(observation: Observation | None, test_id: str) -> bool:
    if observation is None or not observation.observed:
        return False
    record = observation.tests.get(test_id)
    return record is not None and record.outcome in FAILING_OUTCOMES


def _python_files(fix: FixVersion, worktree) -> dict[str, tuple[str, str]]:
    patch = MultiFilePatch.plan(fix.diff_text, worktree)
    files: dict[str, tuple[str, str]] = {}
    for change in patch.changes:
        if not change.relative_path.endswith(".py") or change.new_bytes is None:
            continue
        original_path = worktree / change.relative_path
        original = (
            original_path.read_text(encoding="utf-8") if original_path.is_file() else ""
        )
        files[change.relative_path] = (original, change.new_bytes.decode("utf-8"))
    return files


def _watch_plan(
    python_files: dict[str, tuple[str, str]],
) -> tuple[list[str], dict[str, list[int]], set[str]]:
    """Focus functions holding changed lines, the lines to watch, lines in functions."""
    focus_ids: list[str] = []
    watch: dict[str, list[int]] = {}
    inside: set[str] = set()
    for path, (original_text, fixed_text) in python_files.items():
        spans = function_spans(fixed_text)
        lines = lines_changed_by_fix(original_text, fixed_text)
        watch[path] = lines
        for line in lines:
            span = innermost_function(spans, line)
            if span is None:
                continue
            inside.add(f"{path}:{line}")
            identifier = focus_id(path, span.qualname)
            if identifier not in focus_ids:
                focus_ids.append(identifier)
    return focus_ids, watch, inside


def _other_sections(diff_text: str, worktree_name: str, path: str) -> str:
    return "".join(
        render_file_diff(file_diff, worktree_name)
        for file_diff in parse_unified_diff(diff_text)
        if normalise_diff_path(file_diff.target_path, worktree_name) != path
    )


def _stable_passing(
    first: Observation, second: Observation
) -> tuple[set[str], list[str]]:
    stable: set[str] = set()
    unstable: list[str] = []
    for test_id, record in first.tests.items():
        other = second.tests.get(test_id) if second.observed else record
        if other is None or (other.outcome, other.detail) != (
            record.outcome,
            record.detail,
        ):
            unstable.append(test_id)
        elif record.outcome == "passed":
            stable.add(test_id)
    return stable, sorted(unstable)


def _judge(
    variant: Variant,
    observation: Observation,
    stable_passing: set[str],
    trigger_observation: Observation | None = None,
    trigger_passing: set[str] | frozenset[str] = frozenset(),
) -> VariantResult:
    detected_by = _noticed_by(observation, stable_passing)
    if trigger_observation is not None and trigger_observation.observed:
        detected_by += [
            f"{test_id} (alone)"
            for test_id in _noticed_by(trigger_observation, trigger_passing)
        ]
    if detected_by:
        return VariantResult(variant, DETECTED, detected_by)
    for run in (observation, trigger_observation):
        if run is not None and not run.observed:
            return VariantResult(variant, run.status, detail=run.detail)
    return VariantResult(variant, NOT_DETECTED)


def _noticed_by(
    observation: Observation, passing: set[str] | frozenset[str]
) -> list[str]:
    """Tests that passed with the fix and fail, error or vanish with the variant."""
    if not observation.observed:
        return []
    noticed = sorted(
        test_id
        for test_id in passing
        if observation.tests.get(test_id) is None
        or observation.tests[test_id].outcome in FAILING_OUTCOMES
    )
    noticed += sorted(
        test_id
        for test_id, record in observation.tests.items()
        if test_id.startswith(("<collection>", "unittest.loader._FailedTest"))
        and record.outcome == "error"
    )
    return noticed


def _hunk_result(
    hunk: Hunk,
    results: list[VariantResult],
    executed_lines: list[str],
    inside_functions: set[str],
) -> HunkResult:
    if not hunk.behavioural:
        return HunkResult(hunk, "non_behavioural", None)
    executed: bool | None = None
    watched = [f"{hunk.path}:{line}" for line in hunk.fixed_lines]
    if watched and all(line in inside_functions for line in watched):
        executed = any(line in executed_lines for line in watched)
    revert = next(
        (result for result in results if result.variant.variant_id == hunk.hunk_id),
        None,
    )
    if revert is None or revert.status not in (DETECTED, NOT_DETECTED):
        return HunkResult(hunk, "unknown", executed)
    if revert.status == DETECTED:
        return HunkResult(hunk, "pinned", executed, revert.detected_by)
    return HunkResult(
        hunk, "not_executed" if executed is False else "not_pinned", executed
    )
