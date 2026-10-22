"""Preflight BugsInPy bugs for an agent pilot: do they check out, compile and fail?

For each ``project:bug`` the script checks the bug out into its own directory
(``.workspace/bugsinpy/preflight_<project>_<bug>``), compiles it with
``bugsinpy-safe-compile``, runs the trigger test (it must fail on the buggy
revision), and counts the tests pytest collects from the bug's regression test
file (a proxy for how many tests surround the faulty code). It prints one line per
bug and writes a JSON report; every exclusion carries its reason.

    python scripts/preflight_bugs.py fastapi:2 luigi:1 ... [--out preflight.json]

Needs the executor: run it inside the framework container or on a host whose
Docker CLI reaches the executor container. Makes no LLM calls.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from apr_framework.benchmarks.registry import create_bugsinpy_adapter  # noqa: E402
from apr_framework.challenge.replay import check_out_and_compile_into  # noqa: E402
from apr_framework.core.models import BugIdentifier  # noqa: E402

_COLLECTED = re.compile(r"(\d+) tests? collected|collected (\d+) items?")


def main(argv: list[str]) -> int:
    arguments = parse_arguments(argv)
    adapter = create_bugsinpy_adapter(Path.cwd())
    adapter.toolchain.ensure_installed()
    reports = [
        preflight_bug(adapter, spec, arguments.timeout) for spec in arguments.bugs
    ]
    Path(arguments.out).write_text(json.dumps(reports, indent=2) + "\n", "utf-8")
    print(f"Wrote {arguments.out}")
    return 0


def parse_arguments(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("bugs", nargs="+", help="project:bug, e.g. fastapi:2")
    parser.add_argument("--out", default="preflight.json")
    parser.add_argument("--timeout", type=int, default=600)
    return parser.parse_args(argv)


def preflight_bug(adapter, spec: str, timeout: int) -> dict:
    project, bug_id = spec.split(":")
    bug = BugIdentifier("bugsinpy", adapter.resolve_project(project), int(bug_id))
    report: dict = {"bug": spec, "passed": False}
    destination = (
        Path.cwd() / ".workspace" / "bugsinpy" / f"preflight_{bug.project}_{bug.bug_id}"
    )
    try:
        checkout = check_out_and_compile_into(bug, adapter, destination)
    except Exception as error:  # noqa: BLE001 — record every exclusion with its reason
        return finish(report, f"checkout/compile failed: {str(error)[-300:]}")

    trigger_run = adapter.run_tests(checkout, timeout=timeout)
    report["trigger"] = {
        "passed": trigger_run.passed_count,
        "failed": trigger_run.failed_count,
        "errors": trigger_run.error_count,
    }
    if trigger_run.total_count == 0:
        return finish(report, "trigger test produced no result on the buggy revision")
    if trigger_run.failed_count + trigger_run.error_count == 0:
        return finish(report, "trigger test does not fail on the buggy revision")

    test_file = read_test_file(checkout.worktree)
    collect_run = adapter.run_tests(
        checkout, timeout=timeout, command=f"pytest --collect-only -q {test_file}"
    )
    report["test_file"] = test_file
    report["collected_tests"] = count_collected_tests(collect_run.raw_output)
    report["passed"] = True
    return finish(report, "ok")


def read_test_file(worktree: Path) -> str:
    bug_info = (worktree / "bugsinpy_bug.info").read_text(encoding="utf-8")
    match = re.search(r'test_file="([^"]+)"', bug_info)
    return match.group(1).split(";")[0] if match else ""


def count_collected_tests(raw_output: str) -> int | None:
    match = _COLLECTED.search(raw_output)
    if match:
        return int(match.group(1) or match.group(2))
    node_ids = [line for line in raw_output.splitlines() if "::" in line]
    return len(node_ids) or None


def finish(report: dict, reason: str) -> dict:
    report["reason"] = reason
    print(
        f"{report['bug']:<12} {'PASS' if report['passed'] else 'EXCLUDED':<9} "
        f"tests={report.get('collected_tests')} {reason}",
        flush=True,
    )
    return report


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
