"""Terminal summary, ``leads.md`` and run artifacts for Behaviour Diff.

Wording rules (docs/behaviour-diff.md): a divergence is reported as a divergence,
never as "wrong"; "no divergence" is always qualified by how many tests were
compared; a lead is not a Break This Fix case until a witness's expectation is
justified from the issue, the docs or a docstring.
"""

import json

from apr_framework.behaviour.amplify import Variant
from apr_framework.behaviour.compare import Divergence
from apr_framework.behaviour.pipeline import BehaviourDiffResult, Comparison
from apr_framework.evaluation.run_writer import RunWriter

DIVERGENCES_SHOWN = 3
SHOWN_VALUE_LENGTH = 220


def format_summary(result: BehaviourDiffResult) -> str:
    """The terminal report."""
    lines = [
        f"Behaviour Diff · {result.target.get('bug') or result.target.get('repo')}"
    ]
    lines.append(
        "Focus: "
        + ", ".join(identifier.split("::", 1)[1] for identifier in result.common_focus)
    )
    lines.append(
        f"Compared: {result.tests_compared} tests "
        f"({result.tests_compared - result.variant_tests_compared} original, "
        f"{result.variant_tests_compared} from {len(result.variants)} one-literal variants "
        f"of {len(result.reaching_tests)} test(s) that reach the focus); "
        f"{len(result.unstable_tests)} unstable test(s) left out"
    )
    if not result.reaching_tests:
        lines.append(
            "No test calls a changed function: the suite cannot tell these versions apart."
        )
    for change in result.module_level_changes:
        lines.append(
            f"Not traced: {change['label']} changes {change['file']} outside any "
            f"function (lines {change['lines']})"
        )
    lines.append("")
    lines.append(_versions_table(result))
    lines.append("")
    lines.append(_classes_section(result))
    if result.has_reference and result.reference_comparisons:
        lines.append("")
        lines.append("Divergences from the reference fix:")
        lines.extend(
            _comparison_lines(result.reference_comparisons, result, vs="reference")
        )
    else:
        lines.append("")
        lines.append("Divergences from the buggy version (what each version changes):")
        lines.extend(_comparison_lines(result.buggy_comparisons, result, vs="buggy"))
    lines.append("")
    lines.append(
        "A divergence is a concrete input on which two versions behave differently; "
        "it is not a verdict on which one is right."
    )
    return "\n".join(lines)


def _versions_table(result: BehaviourDiffResult) -> str:
    rows = [("VERSION", "KIND", "ORIGINAL TESTS", "VARIANTS", "CLASS", "NOTES")]
    for version in result.versions:
        notes = []
        if version.duplicates:
            shown = ", ".join(version.duplicates[:2])
            more = len(version.duplicates) - 2
            notes.append(
                "same files as " + shown + (f" and {more} more" if more > 0 else "")
            )
        if version.stripped_paths:
            notes.append("test edits ignored: " + ", ".join(version.stripped_paths))
        if version.status != "observed":
            notes.append(version.detail)
        observed = version.status == "observed"
        rows.append(
            (
                version.label,
                version.kind,
                _tally(version.outcome_counts) if observed else version.status,
                _tally(version.variant_outcome_counts) if observed else "-",
                version.behaviour_class or "-",
                "; ".join(notes),
            )
        )
    widths = [max(len(row[index]) for row in rows) for index in range(5)]
    table = "\n".join(
        (
            "  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row[:5]))
            + ("  " + row[5] if row[5] else "")
        ).rstrip()
        for row in rows
    )
    return table + "\n(counts are passed/failed/error/skipped)"


def _tally(counts: dict[str, int]) -> str:
    return "/".join(
        str(counts.get(key, 0)) for key in ("passed", "failed", "error", "skipped")
    )


def _classes_section(result: BehaviourDiffResult) -> str:
    lines = [
        f"Behaviour classes (identical on all {result.tests_compared} compared tests):"
    ]
    for behaviour_class in result.classes:
        members = behaviour_class["members"]
        lines.append(f"  {behaviour_class['name']}  " + ", ".join(members))
    return "\n".join(lines)


def _comparison_lines(
    comparisons: list[Comparison], result: BehaviourDiffResult, *, vs: str
) -> list[str]:
    variants = {variant.variant_id: variant for variant in result.variants}
    lines: list[str] = []
    for comparison in comparisons:
        if not comparison.divergent_tests:
            lines.append(
                f"  {comparison.right}: no divergence on {result.tests_compared} tests"
            )
            continue
        split = (
            f" ({comparison.on_trigger_tests} on the failing test, "
            f"{comparison.on_other_tests} elsewhere)"
            if vs == "buggy"
            else ""
        )
        lines.append(
            f"  {comparison.right}: differs on {comparison.divergent_tests} test(s){split}"
        )
        for divergence in _most_telling(comparison.divergences)[:DIVERGENCES_SHOWN]:
            lines.extend(
                "    " + line
                for line in describe_divergence(
                    divergence, comparison.left, comparison.right, variants
                )
            )
    return lines


def _most_telling(divergences: list[Divergence]) -> list[Divergence]:
    """Outcome differences first, then call differences; originals before variants."""
    order = {"outcome": 0, "missing": 1, "call": 2, "call_count": 3}
    return sorted(
        divergences,
        key=lambda divergence: (
            order.get(divergence.kind, 9),
            divergence.variant_id is not None,
            divergence.variant_id or "",
        ),
    )


def describe_divergence(
    divergence: Divergence, left: str, right: str, variants: dict[str, Variant]
) -> list[str]:
    variant = variants.get(divergence.variant_id or "")
    heading = divergence.test_id
    if variant is not None:
        heading += f"  [variant {variant.variant_id}: {variant.describe()}]"
    lines = [heading]
    width = max(len(left), len(right))
    if divergence.kind == "call":
        lines.append(f"  call {_short(divergence.call)}")
        if divergence.right_call:
            lines.append(f"  ({right} called {_short(divergence.right_call)})")
    lines.append(f"  {left.ljust(width)} → {_short(divergence.left)}")
    lines.append(f"  {right.ljust(width)} → {_short(divergence.right)}")
    return lines


def _short(text: str, limit: int = SHOWN_VALUE_LENGTH) -> str:
    """Long values are cut for reading; behaviour_diff.json keeps them whole."""
    return text if len(text) <= limit else text[:limit] + " …"


def format_leads(result: BehaviourDiffResult) -> str:
    """``leads.md``: every divergence from the reference, ready for a human to judge."""
    variants = {variant.variant_id: variant for variant in result.variants}
    lines = [
        f"# Behaviour Diff leads · {result.target.get('bug') or result.target.get('repo')}",
        "",
        "Each lead is a concrete test on which a candidate and the reference behave",
        "differently. It is **not** a Break This Fix case: a case needs a witness whose",
        "expectation is justified from the issue, the docs or a docstring, never from",
        '"the developer fix does X" (docs/challenges.md). A divergence can also mean the',
        "candidate is the better fix.",
        "",
    ]
    comparisons = result.reference_comparisons or result.buggy_comparisons
    if not any(comparison.divergent_tests for comparison in comparisons):
        lines.append(f"No divergence on {result.tests_compared} compared tests.")
        return "\n".join(lines) + "\n"
    for comparison in comparisons:
        if not comparison.divergent_tests:
            continue
        lines.append(f"## {comparison.right} vs {comparison.left}")
        lines.append("")
        for divergence in _most_telling(comparison.divergences)[:10]:
            lines.append("```text")
            lines.extend(
                describe_divergence(
                    divergence, comparison.left, comparison.right, variants
                )
            )
            lines.append("```")
            variant = variants.get(divergence.variant_id or "")
            if variant is not None:
                lines.append(
                    f"Variant of `{variant.qualname}` in `{variant.file}`: line "
                    f"{variant.line} changes `{variant.original}` to "
                    f"`{variant.replacement}`."
                )
            lines.append("")
    return "\n".join(lines) + "\n"


def write_artifacts(writer: RunWriter, result: BehaviourDiffResult) -> None:
    """``behaviour_diff.json``, ``observations/*.json`` and ``leads.md``."""
    writer.write_json("behaviour_diff.json", result.to_json())
    observations_directory = writer.run_dir / "observations"
    observations_directory.mkdir(exist_ok=True)
    for index, observation in enumerate(result.observations, start=1):
        safe_label = "".join(
            character if character.isalnum() or character in "-_." else "_"
            for character in observation.label
        )
        (observations_directory / f"{index:02d}-{safe_label}.json").write_text(
            json.dumps(observation.to_json(), separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
    (writer.run_dir / "leads.md").write_text(format_leads(result), encoding="utf-8")
