"""Behaviour Diff: where do versions of a fix behave differently?

Steps (docs/behaviour-diff.md has the user-facing description):

1. **Versions.** Buggy, the reference fix (if any) and the candidates. Candidate
   sections that edit tests, fixtures or test configuration are stripped and
   listed; versions that leave identical files are merged.
2. **Focus.** The functions any version changes (``focus.py``).
3. **Discovery.** The buggy version's suite runs under the driver; tests that
   call a focus function are the *reaching* tests.
4. **Amplification.** One-literal variants of the reaching tests are inserted
   into the test files (``amplify.py``) for the rest of the run.
5. **Observation.** Every version is observed once, the buggy version twice
   (first and last) to find unstable tests.
6. **Restore and verify.** The test files get their original bytes back; the
   checkout must fingerprint exactly as before the run, and the evaluator guard
   must find every test, fixture and configuration file unchanged.
7. **Compare** (``compare.py``): divergences from the reference and from the
   buggy code, and behaviour classes.
"""

import ast
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from apr_framework.behaviour.amplify import (
    ReachingTest,
    Variant,
    augment_source,
    plan_variants,
    variant_of_test_id,
)
from apr_framework.behaviour.compare import (
    Divergence,
    behaviour_classes,
    common_focus,
    compared_tests,
    diverge,
    unstable_tests,
)
from apr_framework.behaviour.focus import changed_functions, focus_id
from apr_framework.behaviour.observe import (
    DriverLimits,
    Observation,
    ObservationRunner,
)
from apr_framework.challenge.multi_file_patch import MultiFilePatch, PatchApplyError
from apr_framework.challenge.replay import fingerprint_checkout
from apr_framework.core.exceptions import EvaluationError
from apr_framework.evidence.target import EvidenceTarget
from apr_framework.evidence.versions import (
    BUGGY,
    CANDIDATE,
    REFERENCE,
    FixVersion,
    buggy_version,
    deduplicate,
    strip_evaluator_edits,
    unique_labels,
)
from apr_framework.repair.evaluator_integrity import (
    EvaluatorGuard,
    purge_bytecode_caches,
)

CALIBRATION_SUFFIX = "#2"
MAX_DIVERGENCES_PER_COMPARISON = 200


@dataclass(frozen=True)
class BehaviourDiffOptions:
    """How much to observe.

    Fields:
        max_variants:       Total amplified variants (0 disables amplification).
        variants_per_test:  Variants per reaching test.
        timeout_seconds:    Wall-clock limit for one suite run.
        limits:             What the driver records per test and per value.
    """

    max_variants: int = 40
    variants_per_test: int = 8
    timeout_seconds: float = 900
    limits: DriverLimits = field(default_factory=DriverLimits)


@dataclass
class VersionSummary:
    """One version as the report shows it."""

    label: str
    kind: str
    source: str
    sha256: str
    stripped_paths: list[str]
    status: str
    detail: str
    outcome_counts: dict[str, int]
    variant_outcome_counts: dict[str, int]
    duplicates: list[str] = field(default_factory=list)
    behaviour_class: str | None = None

    def to_json(self) -> dict:
        return dict(self.__dict__)


@dataclass
class Comparison:
    """All divergences between two versions."""

    left: str
    right: str
    divergent_tests: int
    on_trigger_tests: int
    on_other_tests: int
    divergences: list[Divergence]

    def to_json(self) -> dict:
        return {
            "left": self.left,
            "right": self.right,
            "divergent_tests": self.divergent_tests,
            "on_trigger_tests": self.on_trigger_tests,
            "on_other_tests": self.on_other_tests,
            "divergences": [
                divergence.to_json()
                for divergence in self.divergences[:MAX_DIVERGENCES_PER_COMPARISON]
            ],
        }


@dataclass
class BehaviourDiffResult:
    """Everything a Behaviour Diff run established."""

    target: dict
    started_at: str
    finished_at: str
    focus: list[dict]
    common_focus: list[str]
    module_level_changes: list[dict]
    versions: list[VersionSummary]
    reaching_tests: list[str]
    variants: list[Variant]
    variants_not_inserted: list[str]
    tests_compared: int
    variant_tests_compared: int
    unstable_tests: list[str]
    classes: list[dict]
    reference_comparisons: list[Comparison]
    buggy_comparisons: list[Comparison]
    observations: list[Observation]

    @property
    def has_reference(self) -> bool:
        return any(version.kind == REFERENCE for version in self.versions)

    def to_json(self) -> dict:
        return {
            "target": self.target,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "focus": self.focus,
            "common_focus": self.common_focus,
            "module_level_changes": self.module_level_changes,
            "versions": [version.to_json() for version in self.versions],
            "reaching_tests": self.reaching_tests,
            "variants": [
                {**variant.__dict__, "description": variant.describe()}
                for variant in self.variants
            ],
            "variants_not_inserted": self.variants_not_inserted,
            "tests_compared": self.tests_compared,
            "variant_tests_compared": self.variant_tests_compared,
            "unstable_tests": self.unstable_tests,
            "classes": self.classes,
            "reference_comparisons": [
                comparison.to_json() for comparison in self.reference_comparisons
            ],
            "buggy_comparisons": [
                comparison.to_json() for comparison in self.buggy_comparisons
            ],
        }


def run_behaviour_diff(
    target: EvidenceTarget,
    candidates: list[FixVersion],
    reference: FixVersion | None,
    options: BehaviourDiffOptions,
    *,
    log: Callable[[str], None] = lambda message: None,
) -> BehaviourDiffResult:
    """Observe every version on the target and compare them."""
    started_at = datetime.now(timezone.utc).isoformat()
    worktree = target.worktree
    guard = EvaluatorGuard.for_worktree(worktree)
    clean_fingerprint = fingerprint_checkout(worktree)

    versions, duplicates = _prepare_versions(target, candidates, reference)
    log(f"Versions: {[version.label for version in versions]}; merged: {duplicates}")
    focus, module_level = _find_focus(versions, worktree)
    focus_ids = [entry["id"] for entry in focus]
    if not focus_ids:
        raise EvaluationError(
            "No version changes a function, so there is no behaviour to trace. "
            "(Changes at module or class level are not traced.)"
        )
    log(f"Focus functions: {focus_ids}")

    with target.scratch_directory("behaviour") as scratch:
        runner = ObservationRunner(
            target,
            scratch,
            timeout_seconds=options.timeout_seconds,
            limits=options.limits,
        )
        buggy = versions[0]
        discovery = runner.observe(buggy, focus_ids, label="buggy (discovery)")
        if not discovery.observed:
            raise EvaluationError(
                f"The buggy version could not be observed ({discovery.status}: "
                f"{discovery.detail}).\n{discovery.output_tail}"
            )
        reaching = _reaching_tests(discovery)
        log(f"Reaching tests: {[test.test_id for test in reaching]}")
        variants = (
            plan_variants(
                reaching,
                _read_test_sources(worktree, reaching),
                per_test=options.variants_per_test,
                total=options.max_variants,
            )
            if options.max_variants > 0
            else []
        )
        observations, not_inserted = _observe_with_variants(
            runner, versions, focus_ids, variants, worktree, log
        )

    if fingerprint_checkout(worktree) != clean_fingerprint:
        raise EvaluationError(
            f"{worktree} is not byte-identical to its state before the run; "
            "the results cannot be trusted."
        )
    verdict = guard.verify_evaluator_files()
    if verdict.is_rejected:
        raise EvaluationError(
            f"Evaluator files changed during the run and were restored: {verdict.detail}"
        )

    inserted = [
        variant for variant in variants if variant.variant_id not in not_inserted
    ]
    return _compare(
        target,
        started_at,
        versions,
        duplicates,
        focus,
        module_level,
        reaching,
        inserted,
        not_inserted,
        observations,
    )


def _prepare_versions(
    target: EvidenceTarget,
    candidates: list[FixVersion],
    reference: FixVersion | None,
) -> tuple[list[FixVersion], dict[str, str]]:
    worktree = target.worktree
    stripped = [
        strip_evaluator_edits(candidate, worktree, target.declared_test_files)
        for candidate in candidates
    ]
    ordered = [buggy_version(), *([reference] if reference else []), *stripped]
    kept, duplicates = deduplicate(unique_labels(ordered), worktree)
    return kept, duplicates


def _find_focus(
    versions: list[FixVersion], worktree: Path
) -> tuple[list[dict], list[dict]]:
    """The focus functions (with the versions that change each) and module-level changes."""
    changed_by: dict[str, list[str]] = {}
    module_level: list[dict] = []
    for version in versions:
        if version.kind == BUGGY:
            continue
        try:
            patch = MultiFilePatch.plan(version.diff_text, worktree)
        except PatchApplyError:
            continue
        for change in patch.changes:
            if not change.relative_path.endswith(".py") or change.new_bytes is None:
                continue
            original_path = worktree / change.relative_path
            old_source = (
                original_path.read_text(encoding="utf-8", errors="replace")
                if original_path.is_file()
                else ""
            )
            new_source = change.new_bytes.decode("utf-8", errors="replace")
            file_change = changed_functions(
                change.relative_path, old_source, new_source
            )
            for qualname in file_change.changed_qualnames:
                identifier = focus_id(change.relative_path, qualname)
                changed_by.setdefault(identifier, []).append(version.label)
            if file_change.module_level_lines:
                module_level.append(
                    {
                        "label": version.label,
                        "file": change.relative_path,
                        "lines": list(file_change.module_level_lines),
                    }
                )
    focus = [
        {"id": identifier, "changed_by": labels}
        for identifier, labels in changed_by.items()
    ]
    return focus, module_level


def _reaching_tests(discovery: Observation) -> list[ReachingTest]:
    reaching: list[ReachingTest] = []
    seen: set[tuple[str, str]] = set()
    for record in discovery.tests.values():
        if not (record.calls or record.dropped_calls):
            continue
        if not record.file or not record.qualname:
            continue
        key = (record.file, record.qualname)
        if key in seen:
            continue
        seen.add(key)
        reaching.append(
            ReachingTest(
                test_id=record.test_id, file=record.file, qualname=record.qualname
            )
        )
    return reaching


def _read_test_sources(worktree: Path, reaching: list[ReachingTest]) -> dict[str, str]:
    sources: dict[str, str] = {}
    for test in reaching:
        path = worktree / test.file
        if test.file in sources or not path.is_file():
            continue
        try:
            sources[test.file] = path.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            continue
    return sources


def _observe_with_variants(
    runner: ObservationRunner,
    versions: list[FixVersion],
    focus_ids: list[str],
    variants: list[Variant],
    worktree: Path,
    log: Callable[[str], None],
) -> tuple[list[Observation], list[str]]:
    """Insert the variants, observe every version, then restore the test files."""
    original_bytes: dict[str, bytes] = {}
    not_inserted: list[str] = []
    by_file: dict[str, list[Variant]] = {}
    for variant in variants:
        by_file.setdefault(variant.file, []).append(variant)
    try:
        for relative_path, file_variants in by_file.items():
            path = worktree / relative_path
            original = path.read_bytes()
            augmented = augment_source(original.decode("utf-8"), file_variants)
            try:
                ast.parse(augmented)
            except SyntaxError:
                not_inserted.extend(variant.variant_id for variant in file_variants)
                continue
            original_bytes[relative_path] = original
            path.write_bytes(augmented.encode("utf-8"))
        purge_bytecode_caches(
            worktree / relative_path for relative_path in original_bytes
        )
        expected_fingerprint = fingerprint_checkout(worktree)
        log(f"Inserted {len(variants) - len(not_inserted)} variant(s)")

        # The buggy version and the reference run twice (first and last): a test
        # whose record differs between two runs of the same code is unstable.
        schedule = [(versions[0], versions[0].label)]
        schedule += [(version, version.label) for version in versions[1:]]
        schedule += [
            (version, version.label + CALIBRATION_SUFFIX)
            for version in versions
            if version.kind in (BUGGY, REFERENCE)
        ]
        observations: list[Observation] = []
        for version, label in schedule:
            observation = runner.observe(version, focus_ids, label=label)
            log(
                f"Observed {label}: {observation.status}, "
                f"{observation.outcome_counts()} in {observation.seconds}s"
            )
            observations.append(observation)
            if fingerprint_checkout(worktree) != expected_fingerprint:
                raise EvaluationError(
                    f"Observing {label!r} left the checkout changed; stopping."
                )
        return observations, not_inserted
    finally:
        for relative_path, original in original_bytes.items():
            (worktree / relative_path).write_bytes(original)
        purge_bytecode_caches(
            worktree / relative_path for relative_path in original_bytes
        )


def _compare(
    target: EvidenceTarget,
    started_at: str,
    versions: list[FixVersion],
    duplicates: dict[str, str],
    focus: list[dict],
    module_level: list[dict],
    reaching: list[ReachingTest],
    variants: list[Variant],
    not_inserted: list[str],
    observations: list[Observation],
) -> BehaviourDiffResult:
    by_label = {observation.label: observation for observation in observations}
    buggy = by_label[versions[0].label]
    version_observations = [by_label[version.label] for version in versions]
    shared_focus = common_focus(version_observations)
    unstable: set[str] = set()
    for version in versions:
        rerun = by_label.get(version.label + CALIBRATION_SUFFIX)
        first = by_label[version.label]
        if rerun is not None and rerun.observed and first.observed:
            unstable.update(unstable_tests(first, rerun, shared_focus))
    test_ids = compared_tests(version_observations, unstable)
    variants_by_id = {variant.variant_id: variant for variant in variants}

    classes = behaviour_classes(version_observations, test_ids, shared_focus)
    class_names = {}
    class_payload = []
    for index, members in enumerate(classes):
        name = _class_name(index)
        class_payload.append({"name": name, "members": members})
        for label in members:
            class_names[label] = name

    def compare_pair(left: Observation, right: Observation) -> Comparison:
        divergences = diverge(left, right, test_ids, shared_focus, variants_by_id)
        on_trigger = sum(
            1
            for divergence in divergences
            if target.is_trigger_test(
                _original_test_id(divergence.test_id, variants_by_id)
            )
        )
        return Comparison(
            left=left.label,
            right=right.label,
            divergent_tests=len(divergences),
            on_trigger_tests=on_trigger,
            on_other_tests=len(divergences) - on_trigger,
            divergences=divergences,
        )

    reference = next(
        (version for version in versions if version.kind == REFERENCE), None
    )
    reference_comparisons: list[Comparison] = []
    buggy_comparisons: list[Comparison] = []
    for version in versions:
        observation = by_label[version.label]
        if version.kind == BUGGY or not observation.observed:
            continue
        if buggy.observed:
            buggy_comparisons.append(compare_pair(buggy, observation))
        if version.kind == CANDIDATE and reference is not None:
            reference_observation = by_label[reference.label]
            if reference_observation.observed:
                reference_comparisons.append(
                    compare_pair(reference_observation, observation)
                )

    duplicates_of: dict[str, list[str]] = {}
    for duplicate, kept in duplicates.items():
        duplicates_of.setdefault(kept, []).append(duplicate)
    summaries = [
        VersionSummary(
            label=version.label,
            kind=version.kind,
            source=version.source,
            sha256=version.sha256,
            stripped_paths=list(version.stripped_paths),
            status=by_label[version.label].status,
            detail=by_label[version.label].detail,
            outcome_counts=by_label[version.label].outcome_counts(variants=False),
            variant_outcome_counts=by_label[version.label].outcome_counts(
                variants=True
            ),
            duplicates=duplicates_of.get(version.label, []),
            behaviour_class=class_names.get(version.label),
        )
        for version in versions
    ]
    variant_test_count = sum(
        1 for test_id in test_ids if variant_of_test_id(test_id, variants_by_id)
    )
    return BehaviourDiffResult(
        target=target.describe(),
        started_at=started_at,
        finished_at=datetime.now(timezone.utc).isoformat(),
        focus=focus,
        common_focus=sorted(shared_focus),
        module_level_changes=module_level,
        versions=summaries,
        reaching_tests=[test.test_id for test in reaching],
        variants=variants,
        variants_not_inserted=not_inserted,
        tests_compared=len(test_ids),
        variant_tests_compared=variant_test_count,
        unstable_tests=sorted(unstable),
        classes=class_payload,
        reference_comparisons=reference_comparisons,
        buggy_comparisons=buggy_comparisons,
        observations=observations,
    )


def _original_test_id(test_id: str, variants: dict[str, Variant]) -> str:
    variant = variant_of_test_id(test_id, variants)
    return variant.test_id if variant else test_id


def _class_name(index: int) -> str:
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    return letters[index] if index < len(letters) else f"C{index + 1}"
