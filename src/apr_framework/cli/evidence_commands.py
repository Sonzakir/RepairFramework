"""CLI for the evidence commands: ``behaviour-diff`` (and the shared target options).

Every evidence command works on either target:

- BugsInPy: ``--project P --bug N`` (fresh checkout + compile in
  ``.workspace/bugsinpy/evidence_<project>_<bug>``; ``--reuse-checkout`` keeps a
  compiled one). The developer fix is the reference.
- Local: ``--repo PATH --python PY --tests "CMD" --allow-local-exec``. Runs the
  project's tests and the candidates' code on this machine without isolation.
"""

import argparse
import hashlib
import signal
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from apr_framework.core.exceptions import APRFrameworkError, ConfigurationError
from apr_framework.core.models import BugIdentifier
from apr_framework.evaluation.run_writer import RunWriter
from apr_framework.evidence.target import (
    BugsInPyTarget,
    EvidenceTarget,
    LocalTarget,
    SuiteCommand,
)
from apr_framework.evidence.versions import (
    REFERENCE,
    FixVersion,
    load_patch_file,
    load_run_candidates,
    reference_version,
)


def add_evidence_parsers(subparsers) -> None:
    """Register the evidence commands on the top-level subparsers."""
    _add_behaviour_diff_parser(subparsers)
    _add_tamper_check_parser(subparsers)
    _add_grip_parser(subparsers)


def add_target_arguments(parser: argparse.ArgumentParser) -> None:
    """``--project/--bug`` or ``--repo/--python/--tests/--allow-local-exec``."""
    bugsinpy = parser.add_argument_group("BugsInPy target")
    bugsinpy.add_argument("--project", default=None, help="BugsInPy project")
    bugsinpy.add_argument("--bug", type=int, default=None, help="BugsInPy bug id")
    bugsinpy.add_argument(
        "--reuse-checkout",
        dest="reuse_checkout",
        action="store_true",
        help="Reuse the compiled evidence checkout instead of checking out afresh",
    )
    local = parser.add_argument_group("local target")
    local.add_argument("--repo", default=None, help="Local project directory")
    local.add_argument(
        "--python",
        default=None,
        help="The local project's interpreter (e.g. PATH/.venv/bin/python)",
    )
    local.add_argument(
        "--tests",
        default=None,
        help=(
            "The suite to observe, as a pytest or unittest command, e.g. "
            "'python -m pytest tests/test_cache.py' (files or modules, not single tests)"
        ),
    )
    local.add_argument(
        "--trigger",
        action="append",
        default=[],
        help="Local mode: the command of the failing test (repeatable; optional)",
    )
    local.add_argument(
        "--allow-local-exec",
        dest="allow_local_exec",
        action="store_true",
        help=(
            "Local mode: allow running the project's tests and candidate code on "
            "this machine without container isolation. Required for --repo."
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=900,
        help="Seconds allowed for one run of the suite (default: 900)",
    )
    parser.add_argument(
        "--runs-dir", dest="runs_dir", default="runs", help="Where runs/run_NNN go"
    )


def build_target(args: argparse.Namespace, project_root: Path) -> EvidenceTarget:
    """The BugsInPy or local target the arguments name."""
    bugsinpy_given = args.project is not None or args.bug is not None
    local_given = args.repo is not None
    if bugsinpy_given == local_given:
        raise ConfigurationError(
            "Name exactly one target: --project/--bug (BugsInPy) or --repo (local)."
        )
    if bugsinpy_given:
        if args.project is None or args.bug is None:
            raise ConfigurationError(
                "A BugsInPy target needs both --project and --bug."
            )
        from apr_framework.benchmarks.registry import create_bugsinpy_adapter

        adapter = create_bugsinpy_adapter(project_root)
        adapter.toolchain.ensure_installed()
        bug = BugIdentifier(
            benchmark="bugsinpy",
            project=adapter.resolve_project(args.project),
            bug_id=args.bug,
        )
        return BugsInPyTarget.open(
            adapter, project_root, bug, reuse_checkout=args.reuse_checkout
        )
    if args.python is None or args.tests is None:
        raise ConfigurationError("A local target needs --python and --tests.")
    reference_path = getattr(args, "reference", None)
    return LocalTarget(
        Path(args.repo),
        Path(args.python),
        SuiteCommand.parse(args.tests),
        allow_local_exec=args.allow_local_exec,
        reference_diff_text=(
            Path(reference_path).read_text(encoding="utf-8") if reference_path else None
        ),
        trigger_tests=tuple(SuiteCommand.parse(command) for command in args.trigger),
    )


def load_candidates(args: argparse.Namespace) -> list[FixVersion]:
    """Candidates from ``--patch`` files and ``--from-run`` run directories."""
    candidates = [load_patch_file(Path(path)) for path in args.patch or []]
    for run_directory in args.from_run or []:
        candidates.extend(load_run_candidates(Path(run_directory)))
    return candidates


def describe_versions(versions: list[FixVersion]) -> list[dict]:
    return [
        {
            "label": version.label,
            "kind": version.kind,
            "source": version.source,
            "sha256": version.sha256,
        }
        for version in versions
    ]


@contextmanager
def restore_on_termination() -> Iterator[None]:
    """Turn SIGTERM/SIGHUP into SystemExit while an evidence command runs.

    The commands apply versions to a checkout (for --repo: the user's own working
    tree) and restore it in ``finally`` blocks. Python's default SIGTERM handling
    ends the process without running them; raising SystemExit instead does.
    """

    def stop(signum, frame) -> None:
        raise SystemExit(128 + signum)

    previous = {}
    for name in ("SIGTERM", "SIGHUP"):
        number = getattr(signal, name, None)
        if number is not None:
            previous[number] = signal.signal(number, stop)
    try:
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def command_line() -> str:
    return "python -m apr_framework " + " ".join(sys.argv[1:])


# -- behaviour-diff -----------------------------------------------------------
def _add_behaviour_diff_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "behaviour-diff",
        help=(
            "Behaviour Diff: run the project's tests and one-literal variants of them "
            "against the buggy code, the reference fix and candidate fixes, tracing "
            "the changed functions, and show where the versions behave differently."
        ),
    )
    add_target_arguments(parser)
    versions = parser.add_argument_group("versions")
    versions.add_argument(
        "--patch", action="append", default=[], help="A candidate fix (repeatable)"
    )
    versions.add_argument(
        "--from-run",
        dest="from_run",
        action="append",
        default=[],
        help=(
            "Use an earlier run's candidates: the plausible patches of a `repair` run, "
            "or the diff of a `challenge agent-run` (repeatable)"
        ),
    )
    versions.add_argument(
        "--reference",
        default=None,
        help="Local mode: the reference fix as a diff (BugsInPy uses the developer fix)",
    )
    versions.add_argument(
        "--no-reference",
        dest="no_reference",
        action="store_true",
        help="Compare without the reference fix (divergences from the buggy code)",
    )
    parser.add_argument(
        "--max-variants",
        dest="max_variants",
        type=int,
        default=40,
        help="One-literal test variants in total (default: 40; 0 = original tests only)",
    )
    parser.add_argument(
        "--variants-per-test",
        dest="variants_per_test",
        type=int,
        default=8,
        help="Variants per test that reaches a changed function (default: 8)",
    )


def handle_behaviour_diff(args: argparse.Namespace, project_root: Path) -> int:
    from apr_framework.behaviour import driver as driver_module
    from apr_framework.behaviour.pipeline import (
        BehaviourDiffOptions,
        run_behaviour_diff,
    )
    from apr_framework.behaviour.report import format_summary, write_artifacts

    if args.max_variants < 0 or args.variants_per_test < 1:
        raise ConfigurationError(
            "--max-variants must be >= 0 and --variants-per-test >= 1."
        )
    candidates = load_candidates(args)
    target = build_target(args, project_root)
    reference = _reference(target, args)
    if not candidates and reference is None:
        raise ConfigurationError(
            "Nothing to compare: give candidates (--patch, --from-run) or a reference."
        )
    options = BehaviourDiffOptions(
        max_variants=args.max_variants,
        variants_per_test=args.variants_per_test,
        timeout_seconds=args.timeout,
    )
    writer = RunWriter.create(project_root / args.runs_dir)
    driver_source = Path(driver_module.__file__).read_bytes()
    writer.write_json(
        "config.json",
        {
            "runner": "behaviour-diff",
            "command": command_line(),
            "started_at": datetime.now(timezone.utc).isoformat(),
            "target": target.describe(),
            "reference": describe_versions([reference]) if reference else None,
            "candidates": describe_versions(candidates),
            "options": {
                "max_variants": options.max_variants,
                "variants_per_test": options.variants_per_test,
                "timeout_seconds": options.timeout_seconds,
                "limits": options.limits.__dict__,
            },
            "driver_sha256": hashlib.sha256(driver_source).hexdigest(),
        },
    )
    writer.log(f"behaviour-diff on {target.label}")
    with restore_on_termination():
        result = run_behaviour_diff(
            target, candidates, reference, options, log=writer.log
        )
    write_artifacts(writer, result)
    writer.log("finished")
    print(format_summary(result))
    print(f"\nRun directory: {writer.run_dir}")
    return 0


def _reference(target: EvidenceTarget, args: argparse.Namespace) -> FixVersion | None:
    if args.no_reference:
        return None
    diff_text = target.reference_diff()
    if not diff_text:
        return None
    if isinstance(target, BugsInPyTarget):
        return reference_version(diff_text, "BugsInPy developer fix (bug_patch.txt)")
    return FixVersion(
        label="reference",
        kind=REFERENCE,
        diff_text=diff_text,
        source=str(args.reference),
    )


# -- tamper-check -------------------------------------------------------------------
def _add_tamper_check_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "tamper-check",
        help=(
            "Tamper Check: classify what a change does to its tests (deleted, skipped, "
            "loosened, rewritten expectations vs. added tests) and, with --run-tests, "
            "check that new and edited tests fail before the change and pass after it."
        ),
    )
    add_target_arguments(parser)
    change = parser.add_argument_group("the change")
    change.add_argument(
        "--diff",
        default=None,
        help="The change as a unified diff, applied to --repo or the BugsInPy checkout",
    )
    change.add_argument(
        "--git-base",
        dest="git_base",
        default=None,
        help="With --repo: compare the git working tree against this revision (static only)",
    )
    parser.add_argument(
        "--run-tests",
        dest="run_tests",
        action="store_true",
        help="Also run the added and edited tests before/after the change (needs a target)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit 1 on suspicious findings too, not only on weakening ones",
    )
    parser.add_argument(
        "--json", dest="json_out", default=None, help="Write the report as JSON"
    )


def handle_tamper_check(args: argparse.Namespace, project_root: Path) -> int:
    """Exit 0 (clean), 1 (weakening; with --strict also suspicious), 2 (could not run).

    A CI gate must tell "this change tampers with the tests" from "the check did
    not run", so framework errors exit 2 here rather than the CLI's usual 1.
    """
    try:
        return _tamper_check(args, project_root)
    except APRFrameworkError as error:
        print(f"Error: {error}")
        return 2


def _tamper_check(args: argparse.Namespace, project_root: Path) -> int:
    import json

    from apr_framework.tamper.changes import changes_from_diff, changes_from_git
    from apr_framework.tamper.models import CLEAN, TAMPERED
    from apr_framework.tamper.pipeline import (
        build_report,
        read_test_sources,
        run_dynamic_checks,
        static_findings,
    )
    from apr_framework.tamper.report import format_report

    if (args.diff is None) == (args.git_base is None):
        raise ConfigurationError("Give the change as --diff PATH or --git-base REV.")
    if args.git_base is not None and (args.repo is None or args.run_tests):
        raise ConfigurationError(
            "--git-base needs --repo and works without --run-tests."
        )
    if args.diff is not None and args.repo is None and args.project is None:
        raise ConfigurationError(
            "--diff needs a tree to apply to: --repo or --project/--bug."
        )

    target: EvidenceTarget | None = None
    if args.project is not None or args.bug is not None or args.run_tests:
        target = build_target(args, project_root)
    base = target.worktree if target is not None else Path(args.repo).resolve()
    declared = target.declared_test_files if target is not None else frozenset()
    if args.git_base is not None:
        changes = changes_from_git(base, args.git_base)
        diff_text = ""
        title = f"{base.name}: working tree vs {args.git_base}"
    else:
        diff_text = Path(args.diff).read_text(encoding="utf-8")
        changes = changes_from_diff(diff_text, base)
        title = f"{args.diff} on {target.label if target else base.name}"

    findings, classes = static_findings(
        changes, declared, read_test_sources(base, changes)
    )
    dynamic: list[dict] = []
    note = ""
    writer = None
    if target is not None:
        writer = RunWriter.create(project_root / args.runs_dir)
        writer.write_json(
            "config.json",
            {
                "runner": "tamper-check",
                "command": command_line(),
                "started_at": datetime.now(timezone.utc).isoformat(),
                "target": target.describe(),
                "diff": args.diff,
                "diff_sha256": hashlib.sha256(diff_text.encode("utf-8")).hexdigest(),
                "run_tests": args.run_tests,
            },
        )
        if diff_text:
            (writer.run_dir / "change.diff").write_text(diff_text, encoding="utf-8")
    if args.run_tests and target is not None:
        with restore_on_termination(), target.scratch_directory("tamper") as scratch:
            dynamic = run_dynamic_checks(
                target,
                diff_text,
                findings,
                classes,
                scratch,
                log=writer.log if writer else (lambda message: None),
            )
        if not dynamic:
            note = "No test was added or edited, so there was nothing to run."
    elif not args.run_tests:
        note = "Static check only; --run-tests also runs the added and edited tests."
    report = build_report(
        target.describe() if target else {"kind": "tree", "path": str(base)},
        changes,
        findings,
        classes,
        dynamic,
        note,
    )
    payload = report.to_json()
    if writer is not None:
        writer.write_json("tamper_check.json", payload)
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8"
        )
    print(format_report(report, title=title))
    if writer is not None:
        print(f"\nRun directory: {writer.run_dir}")
    if report.verdict == TAMPERED or (args.strict and report.verdict != CLEAN):
        return 1
    return 0


# -- grip ---------------------------------------------------------------------------
def _add_grip_parser(subparsers) -> None:
    parser = subparsers.add_parser(
        "grip",
        help=(
            "Fix Grip: revert each hunk of a fix and mutate its changed lines, run the "
            "project's tests on every variant, and show which parts of the fix the "
            "tests pin down."
        ),
    )
    add_target_arguments(parser)
    fix = parser.add_mutually_exclusive_group(required=True)
    fix.add_argument(
        "--patch", default=None, help="The fix to measure, as a unified diff"
    )
    fix.add_argument(
        "--developer-fix",
        dest="developer_fix",
        action="store_true",
        help="BugsInPy: measure the developer fix",
    )
    parser.add_argument(
        "--max-probes",
        dest="max_probes",
        type=int,
        default=24,
        help="Line variants to run (default: 24; hunk reverts always run)",
    )
    parser.add_argument(
        "--seed", default="grip", help="Seed for choosing line variants"
    )


def handle_grip(args: argparse.Namespace, project_root: Path) -> int:
    from apr_framework.grip.pipeline import GripOptions, run_grip
    from apr_framework.grip.report import format_grip, write_artifacts

    if args.max_probes < 0:
        raise ConfigurationError("--max-probes must be >= 0.")
    target = build_target(args, project_root)
    if args.developer_fix:
        diff_text = target.reference_diff()
        if not diff_text:
            raise ConfigurationError("--developer-fix needs a BugsInPy target.")
        fix = reference_version(diff_text, "BugsInPy developer fix (bug_patch.txt)")
    else:
        fix = load_patch_file(Path(args.patch))
    options = GripOptions(
        max_probes=args.max_probes, seed=args.seed, timeout_seconds=args.timeout
    )
    writer = RunWriter.create(project_root / args.runs_dir)
    writer.write_json(
        "config.json",
        {
            "runner": "grip",
            "command": command_line(),
            "started_at": datetime.now(timezone.utc).isoformat(),
            "target": target.describe(),
            "fix": describe_versions([fix]),
            "options": options.__dict__,
        },
    )
    with restore_on_termination():
        result = run_grip(target, fix, options, log=writer.log)
    write_artifacts(writer, result)
    print(format_grip(result))
    print(f"\nRun directory: {writer.run_dir}")
    return 0
