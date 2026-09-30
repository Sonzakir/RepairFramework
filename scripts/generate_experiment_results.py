"""
Generate experiment_results/ from existing FauxPy CSV outputs.

This script processes FauxPy SBFL CSV files that were produced by prior Docker
runs and computes faulty-line ranks against the BugsInPy ground-truth patches.
It produces experiment_results/results.json and experiment_results/README.md
using the same LocalizationComparisonRunner that the CLI evaluate-localization
command calls at runtime.

Run from the project root:
    python scripts/generate_experiment_results.py

Docker is NOT required for this script; it only reads pre-existing CSV output
that was written to .workspace/ during previous Docker runs.
"""

from __future__ import annotations

import csv
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# Make the src/ package importable when run from the project root.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from apr_framework.core.models import BugIdentifier, RankedLocation
from apr_framework.evaluation.ground_truth import (
    GroundTruthLine,
    find_faulty_rank,
    in_top_k,
    parse_bug_patch,
)
from apr_framework.evaluation.localization_runner import (
    LocalizationComparisonRunner,
    LocalizationTechniqueResult,
)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

WORKSPACE = REPO_ROOT / ".workspace" / "bugsinpy"
BUGSINPY_PROJECTS = REPO_ROOT / ".tools" / "bugsinpy" / "projects"
OUTPUT_DIR = REPO_ROOT / "experiment_results"
TOP_KS = (1, 5, 10)

# The bugs we evaluate in this experiment
EVAL_BUGS = [
    BugIdentifier(benchmark="bugsinpy", project="black",     bug_id=1),
    BugIdentifier(benchmark="bugsinpy", project="PySnooper", bug_id=1),
    BugIdentifier(benchmark="bugsinpy", project="black",     bug_id=7),
]

# ---------------------------------------------------------------------------
# FauxPy CSV reader
# ---------------------------------------------------------------------------

def _find_fauxpy_report(bug_dir: Path, family: str, granularity: str) -> Path | None:
    """Find the most recent FauxPy report directory for the given family/granularity."""
    pattern = f"FauxPyReport_*_{family.lower()}_{granularity.lower()}_traditional_*"
    candidates = sorted(bug_dir.glob(pattern), reverse=True)
    return candidates[0] if candidates else None


def _find_best_sbfl_report(bug_dir: Path) -> tuple[Path | None, str]:
    """Find an SBFL report (prefer statement, fall back to function)."""
    for gran in ("statement", "function"):
        r = _find_fauxpy_report(bug_dir, "sbfl", gran)
        if r is not None:
            return r, gran
    return None, "statement"


def _parse_fauxpy_sbfl_csv(
    report_dir: Path,
    metric: str,
    project: str,
    bug_id: int,
) -> list[RankedLocation]:
    """Read a FauxPy SBFL Scores_<metric>.csv and return ranked locations.

    FauxPy writes absolute Docker-internal paths like
    ``/home/workspace/<project>_<id>/<project>/some/file.py``.  We strip
    everything up to and including ``<project>/`` to get the worktree-relative
    path that matches what BugsInPy patch files use.
    """
    csv_name = f"Scores_{metric.capitalize()}.csv"
    csv_path = report_dir / csv_name
    if not csv_path.exists():
        return []

    # Prefix used by FauxPy inside Docker: /home/workspace/<project>_<id>/<project>/
    # We want everything after the second <project>/ component.
    strip_prefix = re.compile(
        rf"^.+/{re.escape(project)}_\d+/{re.escape(project)}/"
    )

    locations: list[RankedLocation] = []
    with csv_path.open(encoding="utf-8") as fh:
        reader = csv.reader(fh)
        next(reader, None)  # skip header
        for rank, row in enumerate(reader, start=1):
            if not row or len(row) < 2:
                continue
            entity, score_str = row[0].strip(), row[1].strip()
            try:
                score = float(score_str)
            except ValueError:
                continue

            # entity: /abs/path.py::line  (statement)
            # or      /abs/path.py::FunctionName::start-end  (function)
            sep_parts = entity.split("::")
            if len(sep_parts) < 2:
                continue

            abs_path = sep_parts[0]
            m = strip_prefix.match(abs_path)
            if m:
                rel_path = abs_path[m.end():]
            else:
                rel_path = Path(abs_path).name

            try:
                line = int(sep_parts[1])
            except ValueError:
                continue

            end_line: int | None = None
            if len(sep_parts) >= 3:
                # function granularity: start-end range
                try:
                    end_line = int(sep_parts[2].split("-")[-1])
                except ValueError:
                    pass

            loc_str = f"{rel_path}:{line}"
            locations.append(
                RankedLocation(
                    rank=rank,
                    file_path=rel_path,
                    location=loc_str,
                    line=line,
                    end_line=end_line,
                    score=score,
                    raw_location=f"{rel_path} | {line} | {score:.4f}",
                    metadata={"score_formula": metric.capitalize()},
                )
            )

    return locations


# ---------------------------------------------------------------------------
# Result builders
# ---------------------------------------------------------------------------

def _sbfl_results(
    bug: BugIdentifier,
    report_dir: Path | None,
    ground_truth: list[GroundTruthLine],
    granularity: str,
) -> list[LocalizationTechniqueResult]:
    """Build one result per SBFL metric for the given bug."""
    techniques = [
        ("SBFL-Ochiai (baseline)",    "ochiai"),
        ("SBFL-Tarantula (baseline)", "tarantula"),
        ("SBFL-DStar (baseline)",     "dstar"),
        ("SBFL-Jaccard (extension)",  "jaccard"),
        ("SBFL-SBI (extension)",      "sbi"),
    ]
    results = []
    for name, metric in techniques:
        if report_dir is None:
            results.append(
                LocalizationTechniqueResult(
                    bug=bug,
                    technique=name,
                    ranked_locations=[],
                    ground_truth=ground_truth,
                    faulty_rank=None,
                    top_k_hits={k: False for k in TOP_KS},
                    error=f"No FauxPy SBFL {granularity} report found — Docker required",
                )
            )
            continue

        ranked = _parse_fauxpy_sbfl_csv(report_dir, metric, bug.project, bug.bug_id)
        faulty_rank = find_faulty_rank(ranked, ground_truth)
        hits = {k: in_top_k(faulty_rank, k) for k in TOP_KS}
        results.append(
            LocalizationTechniqueResult(
                bug=bug,
                technique=name,
                ranked_locations=ranked,
                ground_truth=ground_truth,
                faulty_rank=faulty_rank,
                top_k_hits=hits,
            )
        )
    return results


def _mbfl_error(
    bug: BugIdentifier,
    ground_truth: list[GroundTruthLine],
    label: str,
    reason: str,
) -> LocalizationTechniqueResult:
    return LocalizationTechniqueResult(
        bug=bug,
        technique=label,
        ranked_locations=[],
        ground_truth=ground_truth,
        faulty_rank=None,
        top_k_hits={k: False for k in TOP_KS},
        error=reason,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print(f"Repository root : {REPO_ROOT}")
    print(f"Workspace       : {WORKSPACE}")
    print(f"Output dir      : {OUTPUT_DIR}")
    print()

    all_results: list[LocalizationTechniqueResult] = []

    for bug in EVAL_BUGS:
        bug_label = f"{bug.project} #{bug.bug_id}"
        print(f"=== {bug_label} ===")

        # Ground truth from the BugsInPy patch
        patch_path = BUGSINPY_PROJECTS / bug.project / "bugs" / str(bug.bug_id) / "bug_patch.txt"
        ground_truth = parse_bug_patch(patch_path)
        if ground_truth:
            print(f"  Ground truth: {len(ground_truth)} deleted line(s)")
            for gt in ground_truth[:3]:
                print(f"    {gt.file_path}:{gt.line}")
        else:
            print("  Ground truth: (not found)")

        # Bug workspace
        bug_workspace = WORKSPACE / f"{bug.project}_{bug.bug_id}"

        # --- SBFL (prefer statement, fall back to function) ---------------
        sbfl_report, sbfl_gran = _find_best_sbfl_report(bug_workspace)
        if sbfl_report:
            print(f"  SBFL {sbfl_gran} report : {sbfl_report.name}")
        else:
            print(f"  SBFL report: not found (Docker required)")

        sbfl_results = _sbfl_results(bug, sbfl_report, ground_truth, sbfl_gran)
        for r in sbfl_results:
            rank_str = str(r.faulty_rank) if r.faulty_rank is not None else "—"
            top1 = "✓" if r.top_k_hits.get(1) else "✗"
            top5 = "✓" if r.top_k_hits.get(5) else "✗"
            top10 = "✓" if r.top_k_hits.get(10) else "✗"
            note = f"  [{r.error}]" if r.error else ""
            print(f"    {r.technique:<35} rank={rank_str:<5} T1={top1} T5={top5} T10={top10}{note}")
        all_results.extend(sbfl_results)

        # --- MBFL / Hybrid (Docker required) --------------------------------
        mbfl_note = "Docker required for mutant execution"
        for label in [
            "MBFL-Metallaxis (baseline)",
            "MBFL-Metallaxis-Random (extension)",
            "Hybrid SBFL+MBFL (extension)",
        ]:
            r = _mbfl_error(bug, ground_truth, label, mbfl_note)
            print(f"    {r.technique:<35} [{r.error}]")
            all_results.append(r)

        print()

    # Write results — JSON only; preserve a hand-crafted README if one exists.
    runner = LocalizationComparisonRunner(top_ks=TOP_KS)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    runner._write_json(all_results, OUTPUT_DIR / "results.json")
    print(f"results.json written to : {OUTPUT_DIR / 'results.json'}")

    readme_path = OUTPUT_DIR / "README.md"
    if not readme_path.exists():
        readme_path.write_text(runner._build_readme(all_results), encoding="utf-8")
        print(f"README generated        : {readme_path}")
    else:
        print(f"README preserved        : {readme_path} (already exists)")


if __name__ == "__main__":
    main()
