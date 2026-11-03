"""Terminal report and run artifacts for Fix Grip."""

import difflib

from apr_framework.evaluation.run_writer import RunWriter
from apr_framework.grip.pipeline import DETECTED, NOT_DETECTED, GripResult

_VERDICT_TEXT = {
    "pinned": "pinned",
    "not_pinned": "NOT PINNED",
    "not_executed": "NOT EXECUTED",
    "non_behavioural": "non-behavioural",
    "unknown": "unknown",
}


def format_grip(result: GripResult) -> str:
    target = result.target.get("bug") or result.target.get("repo")
    lines = [f"Fix Grip · {target} · {result.fix_label}"]
    lines.append(
        f"Suite: {result.suite}; with the fix {_counts(result.fix_counts)}, "
        f"buggy {_counts(result.buggy_counts)}; {result.stable_passing} stable passing "
        f"test(s), {len(result.unstable_tests)} unstable left out"
    )
    if result.trigger:
        outcomes = result.trigger_with_fix
        passing = bool(outcomes) and all(
            outcome == "passed" for outcome in outcomes.values()
        )
        lines.append(
            f"Failing test, run alone: {result.trigger} → with the fix "
            + (", ".join(sorted(set(outcomes.values()))) if outcomes else "not run")
        )
        if not passing:
            lines.append(
                "WARNING: the fix does not make the failing test pass when it runs alone; "
                "the verdicts below rest on the rest of the suite."
            )
    for test_id in result.trigger_fails_in_suite:
        lines.append(
            f"Note: {test_id} passes alone but fails inside its whole test file "
            "(test-order dependence); Fix Grip judges it alone."
        )
    if result.fixed_by_the_fix:
        lines.append(
            "Tests the fix turns green: " + ", ".join(result.fixed_by_the_fix[:5])
        )
    if result.stripped_paths:
        lines.append(
            "Test edits in the fix ignored: " + ", ".join(result.stripped_paths)
        )
    lines.append("")
    rows = [("HUNK", "WHERE", "VERDICT", "EXECUTED", "NOTICED BY")]
    for hunk_result in result.hunks:
        executed = {True: "yes", False: "no", None: "-"}[hunk_result.executed]
        rows.append(
            (
                hunk_result.hunk.hunk_id,
                hunk_result.hunk.location,
                _VERDICT_TEXT[hunk_result.verdict],
                executed,
                _tests(hunk_result.detected_by),
            )
        )
    widths = [max(len(row[index]) for row in rows) for index in range(4)]
    for row in rows:
        lines.append(
            (
                "  ".join(
                    cell.ljust(widths[index]) for index, cell in enumerate(row[:4])
                )
                + "  "
                + row[4]
            ).rstrip()
        )
    probes = [
        variant for variant in result.variants if variant.variant.family != "revert"
    ]
    detected = [variant for variant in probes if variant.status == DETECTED]
    missed = [variant for variant in probes if variant.status == NOT_DETECTED]
    other = [
        variant for variant in probes if variant.status not in (DETECTED, NOT_DETECTED)
    ]
    lines.append("")
    lines.append(
        f"Line and clause variants: {len(probes)} run, {len(detected)} noticed by a test, "
        f"{len(missed)} not noticed"
        + (f", {len(other)} could not be judged" if other else "")
    )
    for variant_result in missed:
        variant = variant_result.variant
        lines.append(
            f"  {variant.variant_id} {variant.family}/{variant.kind} "
            f"{variant.path}:{variant.line} ({variant.hunk_id or 'outside the hunks'})"
        )
        lines.append(f"    - {variant.before}")
        lines.append(f"    + {variant.after}")
    for variant_result in other:
        variant = variant_result.variant
        lines.append(
            f"  {variant.variant_id} {variant.family}/{variant.kind} {variant.path}:{variant.line}"
            f" could not be judged ({variant_result.status}: {variant_result.detail})"
        )
    lines.append("")
    lines.append(
        "A variant no test notices is a change to the fix the tests would accept; it may "
        "be equivalent. A NOT PINNED hunk is code the fix adds that no test depends on."
    )
    return "\n".join(lines)


def write_artifacts(writer: RunWriter, result: GripResult) -> None:
    """``grip.json`` and one diff per variant no test notices."""
    writer.write_json("grip.json", result.to_json())
    missed = [variant for variant in result.variants if variant.status == NOT_DETECTED]
    if not missed:
        return
    directory = writer.run_dir / "not_noticed"
    directory.mkdir(exist_ok=True)
    for variant_result in missed:
        variant = variant_result.variant
        fixed_text = result.fixed_texts[variant.path]
        diff = "".join(
            difflib.unified_diff(
                fixed_text.splitlines(keepends=True),
                variant.file_text.splitlines(keepends=True),
                fromfile=f"fix/{variant.path}",
                tofile=f"{variant.variant_id}/{variant.path}",
            )
        )
        (directory / f"{variant.variant_id}.diff").write_text(diff, encoding="utf-8")


def _counts(counts: dict[str, int]) -> str:
    return (
        "/".join(str(counts.get(key, 0)) for key in ("passed", "failed", "error"))
        + " (pass/fail/error)"
    )


def _tests(test_ids: list[str]) -> str:
    if not test_ids:
        return ""
    names = [test_id.rsplit(".", 1)[-1].rsplit("::", 1)[-1] for test_id in test_ids]
    return ", ".join(names[:2]) + (f" +{len(names) - 2}" if len(names) > 2 else "")
