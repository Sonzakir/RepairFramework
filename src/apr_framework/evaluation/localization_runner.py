"""
Run multiple fault-localization techniques over a set of BugsInPy bugs and
compare their rankings against the ground-truth faulty lines.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from apr_framework.core.models import BugIdentifier, CheckoutResult, RankedLocation
from apr_framework.evaluation.ground_truth import (
    GroundTruthLine,
    find_faulty_rank,
    in_top_k,
    parse_bug_patch,
)
from apr_framework.localization.base import FaultLocalizer


@dataclass
class LocalizationTechniqueResult:
    bug: BugIdentifier
    technique: str
    ranked_locations: list[RankedLocation]
    ground_truth: list[GroundTruthLine]
    faulty_rank: int | None
    top_k_hits: dict[int, bool] = field(default_factory=dict)
    error: str | None = None


class LocalizationComparisonRunner:
    """Run several localizers on a fixed set of bugs and produce a comparison report."""

    TOP_KS = (1, 5, 10, 20)

    def __init__(self, top_ks: tuple[int, ...] = TOP_KS) -> None:
        self._top_ks = top_ks

    def run(
        self,
        bugs: list[BugIdentifier],
        techniques: list[tuple[str, FaultLocalizer]],
        checkouts: dict[BugIdentifier, CheckoutResult],
        patch_dir_fn: Any,
    ) -> list[LocalizationTechniqueResult]:
        """Run all techniques on all bugs.

        Args:
            bugs: Bugs to evaluate.
            techniques: ``(name, localizer)`` pairs — each localizer is called
                for every bug.
            checkouts: Pre-built ``CheckoutResult`` keyed by ``BugIdentifier``.
            patch_dir_fn: Callable ``(bug) -> Path`` returning the directory
                that contains ``bug_patch.txt`` for that bug.
        """
        results: list[LocalizationTechniqueResult] = []
        for bug in bugs:
            checkout = checkouts.get(bug)
            if checkout is None:
                for name, _ in techniques:
                    results.append(
                        LocalizationTechniqueResult(
                            bug=bug,
                            technique=name,
                            ranked_locations=[],
                            ground_truth=[],
                            faulty_rank=None,
                            error="No checkout available",
                        )
                    )
                continue

            patch_path = patch_dir_fn(bug) / "bug_patch.txt"
            ground_truth = parse_bug_patch(patch_path)

            for name, localizer in techniques:
                print(f"  [{bug.project} #{bug.bug_id}] {name} ...", flush=True)
                try:
                    loc_result = localizer.localize(bug, checkout)
                    ranked = loc_result.ranked_locations
                    rank = find_faulty_rank(ranked, ground_truth)
                    hits = {k: in_top_k(rank, k) for k in self._top_ks}
                    results.append(
                        LocalizationTechniqueResult(
                            bug=bug,
                            technique=name,
                            ranked_locations=ranked,
                            ground_truth=ground_truth,
                            faulty_rank=rank,
                            top_k_hits=hits,
                        )
                    )
                except Exception as exc:
                    results.append(
                        LocalizationTechniqueResult(
                            bug=bug,
                            technique=name,
                            ranked_locations=[],
                            ground_truth=ground_truth,
                            faulty_rank=None,
                            error=f"{type(exc).__name__}: {exc}",
                        )
                    )
                    print(f"    ERROR: {exc}", flush=True)

        return results

    def write_results(
        self,
        results: list[LocalizationTechniqueResult],
        output_dir: Path,
    ) -> Path:
        """Persist raw JSON and a human-readable README to *output_dir*.

        Returns the path of the written README.
        """
        output_dir.mkdir(parents=True, exist_ok=True)
        self._write_json(results, output_dir / "results.json")
        readme_path = output_dir / "README.md"
        readme_path.write_text(self._build_readme(results), encoding="utf-8")
        return readme_path

    def _write_json(
        self, results: list[LocalizationTechniqueResult], path: Path
    ) -> None:
        rows: list[dict[str, Any]] = []
        for r in results:
            rows.append(
                {
                    "bug": {
                        "benchmark": r.bug.benchmark,
                        "project": r.bug.project,
                        "bug_id": r.bug.bug_id,
                    },
                    "technique": r.technique,
                    "faulty_rank": r.faulty_rank,
                    "top_k_hits": {str(k): v for k, v in r.top_k_hits.items()},
                    "ground_truth_lines": [
                        {"file": gt.file_path, "line": gt.line} for gt in r.ground_truth
                    ],
                    "total_ranked": len(r.ranked_locations),
                    "error": r.error,
                }
            )
        path.write_text(
            json.dumps(
                {
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "results": rows,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    def _build_readme(self, results: list[LocalizationTechniqueResult]) -> str:
        # Builds a markdown report string from a flat list of (bug, technique) results.
        #
        # Structure of the output:
        #   1. Header + description + timestamp
        #   2. One section per bug — ground-truth location, then a table where each
        #      row is a technique and columns are: rank of the faulty line, Top-k hit
        #      checkmarks (one per value in self._top_ks), total ranked locations, errors.
        #   3. Aggregate Top-k table — for each technique, how many bugs had the faulty
        #      line in the top-k (shown as hits/valid, excluding errored runs).
        #   4. Discussion section generated by _build_discussion().
        #
        # The (bug, technique) → result index is built upfront so table cells can be
        # filled in O(1) rather than scanning the list for each cell.
        bugs = _unique_ordered([r.bug for r in results])
        techniques = _unique_ordered([r.technique for r in results])
        top_ks = sorted(self._top_ks)

        index: dict[tuple, LocalizationTechniqueResult] = {}
        for r in results:
            index[(r.bug, r.technique)] = r

        lines: list[str] = []
        lines.append("# Fault Localization Evaluation Results\n")
        lines.append(
            "Comparison of SBFL (baseline), MBFL (baseline), and extension "
            "techniques (custom SBFL metrics: Jaccard, WSBI; Hybrid SBFL+MBFL) "
            "on BugsInPy bugs.\n"
        )
        lines.append(
            f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"
        )

        # --- Per-bug tables ---
        for bug in bugs:
            lines.append(f"\n## {bug.project} bug #{bug.bug_id}\n")

            # ground truth summary
            sample = index.get((bug, techniques[0]))
            if sample and sample.ground_truth:
                gt_summary = ", ".join(
                    f"`{gt.file_path}:{gt.line}`" for gt in sample.ground_truth[:5]
                )
                if len(sample.ground_truth) > 5:
                    gt_summary += f" … ({len(sample.ground_truth)} lines total)"
                lines.append(f"**Ground-truth faulty lines:** {gt_summary}\n")
            else:
                lines.append("**Ground-truth faulty lines:** *(not available)*\n")

            header = (
                "| Technique | Rank | "
                + " | ".join(f"Top-{k}" for k in top_ks)
                + " | Total ranked | Notes |"
            )
            sep = "|---|---|" + "|".join("---" for _ in top_ks) + "|---|---|"
            lines.append(header)
            lines.append(sep)

            for tech in techniques:
                r = index.get((bug, tech))
                if r is None:
                    continue
                rank_str = str(r.faulty_rank) if r.faulty_rank is not None else "—"
                top_k_cols = " | ".join(
                    ("✓" if r.top_k_hits.get(k) else "✗") for k in top_ks
                )
                total = str(len(r.ranked_locations))
                note = r.error or ""
                lines.append(
                    f"| {tech} | {rank_str} | {top_k_cols} | {total} | {note} |"
                )

        # --- Aggregate Top-k table ---
        lines.append("\n## Aggregate Top-k Accuracy\n")
        lines.append(
            "Fraction of bugs where the faulty line appeared within the top-k ranked locations.\n"
        )
        n_bugs = len(bugs)
        header = (
            "| Technique | "
            + " | ".join(f"Top-{k} ({n_bugs} bugs)" for k in top_ks)
            + " |"
        )
        sep = "|---|" + "|".join("---" for _ in top_ks) + "|"
        lines.append(header)
        lines.append(sep)

        for tech in techniques:
            tech_results = [index.get((bug, tech)) for bug in bugs]
            tech_results = [r for r in tech_results if r is not None]
            cols = []
            for k in top_ks:
                hits = sum(1 for r in tech_results if r.top_k_hits.get(k))
                valid = len([r for r in tech_results if r.error is None])
                cols.append(f"{hits}/{valid}" if valid else "—")
            lines.append("| " + tech + " | " + " | ".join(cols) + " |")

        # --- Discussion ---
        lines.append("\n## Discussion\n")
        lines.append(_build_discussion(results, bugs, techniques, top_ks, index))

        return "\n".join(lines) + "\n"


def _build_discussion(
    results: list[LocalizationTechniqueResult],
    bugs: list[BugIdentifier],
    techniques: list[str],
    top_ks: list[int],
    index: dict[tuple, LocalizationTechniqueResult],
) -> str:
    paras: list[str] = []

    # Which technique found the most bugs in top-1?
    top1_counts: dict[str, int] = {}
    for tech in techniques:
        top1_counts[tech] = sum(
            1 for bug in bugs if (r := index.get((bug, tech))) and r.top_k_hits.get(1)
        )
    best_count = max(top1_counts.values())
    worst_count = min(top1_counts.values())

    if best_count == worst_count:
        paras.append(
            f"**Extension vs baseline.**  "
            f"All techniques achieved the same Top-1 accuracy "
            f"({best_count}/{len(bugs)} bugs).  "
            f"The extensions (Jaccard, WSBI, Hybrid) and the baselines (Ochiai, Tarantula, D*) "
            f"are indistinguishable by this metric on the evaluated bug set."
        )
    else:
        best_tech  = max(top1_counts, key=lambda t: top1_counts[t])
        worst_tech = min(top1_counts, key=lambda t: top1_counts[t])
        paras.append(
            f"**Extension vs baseline.**  "
            f"Among the techniques evaluated, `{best_tech}` achieved the highest Top-1 accuracy "
            f"({top1_counts[best_tech]}/{len(bugs)} bugs), while `{worst_tech}` ranked lowest "
            f"({top1_counts[worst_tech]}/{len(bugs)})."
        )

    # Rank trends
    rank_avgs: dict[str, float] = {}
    for tech in techniques:
        ranks = [
            r.faulty_rank
            for bug in bugs
            if (r := index.get((bug, tech))) and r.faulty_rank is not None
        ]
        rank_avgs[tech] = sum(ranks) / len(ranks) if ranks else float("inf")

    ranked_by_avg = sorted(
        [(t, v) for t, v in rank_avgs.items() if v < float("inf")],
        key=lambda x: x[1],
    )
    if ranked_by_avg:
        best_avg_tech, best_avg = ranked_by_avg[0]
        paras.append(
            f"**Average rank.**  "
            f"`{best_avg_tech}` achieved the lowest average rank of the faulty line "
            f"({best_avg:.1f}) across bugs where any technique produced a ranking."
        )

    paras.append(
        "**Custom SBFL metrics (Jaccard, WSBI).**  "
        "These metrics are not present in the stock FauxPy 0.7.0 release and were added "
        "via the framework's in-place source patch.  Jaccard uses set-based overlap scoring; "
        "WSBI (Weighted SBI) is a novel metric ef / (ef + alpha * ep) with configurable alpha, "
        "where alpha=0.5 (default) down-weights passing executions to surface the faulty line "
        "more reliably.  Both apply different scoring formulae to the same coverage spectrum, "
        "which can shift the rank of the faulty line up or down depending on failure density."
    )

    paras.append(
        "**Hybrid SBFL+MBFL.**  "
        "The hybrid technique normalises and combines the scores of an SBFL run and an MBFL "
        "run via a weighted sum.  Locations found by both backends receive a tiebreak bonus. "
        "For bugs where SBFL and MBFL individually miss the faulty line but overlap near it, "
        "the combined score can surface the faulty location at a higher rank than either "
        "technique alone.  Conversely, when MBFL is noisy (e.g. few failing tests), the "
        "hybrid can also inherit that noise."
    )

    paras.append(
        "**Runtime trade-offs.**  "
        "SBFL runs complete in seconds because they only require a single test execution per "
        "bug.  MBFL requires generating and running mutants, which takes minutes even with "
        "the framework's random-budget mutation-selection extension.  The hybrid therefore "
        "inherits MBFL's cost but aims to compensate with improved accuracy."
    )

    return "\n\n".join(paras)


def _unique_ordered(items: list) -> list:
    seen: set = set()
    out: list = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out
