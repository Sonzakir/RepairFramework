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

from apr_framework.core.exceptions import ConfigurationError
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
