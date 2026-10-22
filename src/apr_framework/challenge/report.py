"""Plain-text rendering of case lists and replay results for the terminal."""

from apr_framework.challenge.bundle import CaseBundle
from apr_framework.challenge.replay import ReplayResult, VariantObservation

VARIANT_LABELS = {
    "buggy": "buggy",
    "candidate": "candidate",
    "candidate_as_submitted": "candidate as submitted (its edited tests)",
    "developer_fixed": "developer-fixed",
}
# In a Type T case the candidate row replays only the non-test changes.
TYPE_T_CANDIDATE_LABEL = "candidate, non-test changes only"
_LIST_COLUMN_WIDTH = 60


def format_case_list(bundles: list[CaseBundle]) -> str:
    """One line per case: id, type, status, bug, agent and the promise."""
    if not bundles:
        return "No cases under challenges/cases/."
    rows = [("CASE", "TYPE", "STATUS", "BUG", "AGENT", "PROMISE")]
    for bundle in bundles:
        provenance = bundle.data.get("candidate", {}).get("provenance", {})
        rows.append(
            (
                bundle.case_id,
                bundle.case_type,
                bundle.status,
                f"{bundle.bug.project}#{bundle.bug.bug_id}",
                _shorten(str(provenance.get("agent", "?"))),
                _shorten(str(bundle.data.get("promise", ""))),
            )
        )
    return _render_rows(rows)


def format_replay_result(result: ReplayResult) -> str:
    """A compact table: one row per variant, repeats folded when they agree."""
    lines = [
        f"Case {result.case_id} (Type {result.case_type}, status {result.case_status})"
        f" on {result.bug}, {result.repeats} repeat(s)",
        f"Candidate: {result.candidate_source}",
        "",
    ]
    rows = [("VARIANT", "TRIGGER", "REGRESSION", "WITNESS", "INTEGRITY", "REPEATS")]
    for variant, observations in _group_by_variant(result.observations).items():
        rows.extend(_variant_rows(_variant_label(variant, result), observations))
    lines.append(_render_rows(rows))
    lines.append("")
    if result.witness_classification:
        lines.append(f"Witness: {result.witness_classification}")
    for variant in result.unchecked_variants:
        lines.append(
            f"{VARIANT_LABELS.get(variant, variant)}: not compared with the case's "
            "expectation (the replayed patch is not the case's candidate)"
        )
    if result.matched:
        lines.append("Result: observed outcomes match the case's recorded expectation.")
    else:
        lines.append("Result: MISMATCH with the case's recorded expectation:")
        lines.extend(f"  - {mismatch}" for mismatch in result.mismatches)
    return "\n".join(lines)


def _group_by_variant(
    observations: list[VariantObservation],
) -> dict[str, list[VariantObservation]]:
    grouped: dict[str, list[VariantObservation]] = {}
    for observation in observations:
        grouped.setdefault(observation.variant, []).append(observation)
    return grouped


def _variant_label(variant: str, result: ReplayResult) -> str:
    if variant == "candidate" and result.case_type == "T":
        return TYPE_T_CANDIDATE_LABEL
    return VARIANT_LABELS.get(variant, variant)


def _variant_rows(
    label: str, observations: list[VariantObservation]
) -> list[tuple[str, ...]]:
    def cells(observation: VariantObservation) -> tuple[str, ...]:
        return (
            observation.trigger,
            observation.regression,
            observation.witness or "-",
            observation.integrity,
        )

    distinct_cells = {cells(observation) for observation in observations}
    if len(distinct_cells) == 1:
        return [(label, *cells(observations[0]), f"x{len(observations)} identical")]
    return [
        (f"{label} #{observation.repeat}", *cells(observation), "DIFFERS")
        for observation in observations
    ]


def _shorten(text: str) -> str:
    if len(text) <= _LIST_COLUMN_WIDTH:
        return text
    return text[: _LIST_COLUMN_WIDTH - 3].rstrip() + "..."


def _render_rows(rows: list[tuple[str, ...]]) -> str:
    widths = [max(len(row[column]) for row in rows) for column in range(len(rows[0]))]
    return "\n".join(
        "  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip()
        for row in rows
    )
