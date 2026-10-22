"""Audit committed repair results for "plausible" patches that edit evaluator files.

Until the evaluator-integrity gate existed (``repair/evaluator_integrity.py``), the
validation path accepted a candidate that rewrote the very test judging it. This
script scans every committed result file for plausible or correct patches whose
diff touches an evaluator file (tests, fixtures, test configuration, BugsInPy
scripts) and writes a Markdown report. Committed results are never modified:
the report is the correction.

    python scripts/audit_test_edits.py [output_path]

Scans ``runs/*/repair_results.json`` and every ``repair_results.json`` under
``experiment_results/``. No Docker, no API key. When the local BugsInPy clone
(``.tools/bugsinpy``) is present, each bug's declared test files are used too.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from apr_framework.repair.evaluator_integrity import (  # noqa: E402
    find_recorded_patch_evaluator_edits,
)

DEFAULT_OUTPUT_PATH = Path("experiment_results/INTEGRITY_AUDIT.md")
ACCEPTED_STATUSES = ("plausible", "correct")


@dataclass
class EvaluatorEditFinding:
    """One accepted patch that touches evaluator files."""

    run_path: str
    project: str
    bug_id: int
    approach: str
    patch_id: str
    status: str
    touched_evaluator_files: dict[str, str]
    diff_sha256: str


@dataclass
class AuditSummary:
    """Everything the report needs."""

    result_files: int = 0
    accepted_patches: int = 0
    findings: list[EvaluatorEditFinding] = field(default_factory=list)
    accepted_by_run: dict[str, int] = field(default_factory=dict)


def main(argv: list[str]) -> int:
    output_path = Path(argv[0]) if argv else DEFAULT_OUTPUT_PATH
    project_root = Path.cwd()
    summary = audit_result_files(collect_result_files(project_root), project_root)
    output_path.write_text(render_report(summary), encoding="utf-8")
    print(
        f"Scanned {summary.result_files} result file(s), "
        f"{summary.accepted_patches} plausible/correct patch(es): "
        f"{len(summary.findings)} edit evaluator files. Report: {output_path}"
    )
    return 0


def collect_result_files(project_root: Path) -> list[Path]:
    run_files = sorted(project_root.glob("runs/*/repair_results.json"))
    matrix_files = sorted(
        project_root.glob("experiment_results/**/repair_results.json")
    )
    return run_files + matrix_files


def audit_result_files(result_files: list[Path], project_root: Path) -> AuditSummary:
    summary = AuditSummary(result_files=len(result_files))
    for result_file in result_files:
        results = json.loads(result_file.read_text(encoding="utf-8"))
        for bug_payload in results.get("bugs", [results]):
            audit_bug_payload(
                bug_payload,
                result_file.parent.relative_to(project_root).as_posix(),
                project_root,
                summary,
            )
    return summary


def audit_bug_payload(
    bug_payload: dict, run_path: str, project_root: Path, summary: AuditSummary
) -> None:
    project = str(bug_payload.get("project"))
    bug_id = int(bug_payload.get("bug_id", 0))
    declared_test_files = read_declared_test_files_from_bugsinpy(
        project_root, project, bug_id
    )
    accepted_patches = unique_accepted_patches(bug_payload)
    summary.accepted_patches += len(accepted_patches)
    summary.accepted_by_run[run_path] = len(accepted_patches)

    for patch in accepted_patches:
        touched_evaluator_files = find_recorded_patch_evaluator_edits(
            patch, project, declared_test_files
        )
        if touched_evaluator_files:
            summary.findings.append(
                EvaluatorEditFinding(
                    run_path=run_path,
                    project=project,
                    bug_id=bug_id,
                    approach=describe_approach(bug_payload.get("config", {})),
                    patch_id=str(patch.get("patch_id")),
                    status=str(patch.get("status")),
                    touched_evaluator_files=touched_evaluator_files,
                    diff_sha256=hashlib.sha256(
                        (patch.get("diff_text") or "").encode("utf-8")
                    ).hexdigest(),
                )
            )


def unique_accepted_patches(bug_payload: dict) -> list[dict]:
    """Plausible/correct entries from all_results and plausible_patches, once each."""
    patches_by_id: dict[str, dict] = {}
    for patch in bug_payload.get("all_results", []) + bug_payload.get(
        "plausible_patches", []
    ):
        if patch.get("status") in ACCEPTED_STATUSES:
            patches_by_id.setdefault(str(patch.get("patch_id")), patch)
    return list(patches_by_id.values())


def read_declared_test_files_from_bugsinpy(
    project_root: Path, project: str, bug_id: int
) -> frozenset[str]:
    bug_dir = project_root / ".tools" / "bugsinpy" / "projects" / project / "bugs"
    bug_info_path = bug_dir / str(bug_id) / "bug.info"
    if not bug_info_path.is_file():
        return frozenset()
    match = re.search(r'test_file="([^"]+)"', bug_info_path.read_text("utf-8"))
    if not match:
        return frozenset()
    return frozenset(path.strip() for path in match.group(1).split(";") if path)


def describe_approach(config: dict) -> str:
    approach = config.get("approach") or config.get("variant")
    technique = config.get("technique") or config.get("runner") or "?"
    model = config.get("model")
    label = f"{approach} ({technique})" if approach else str(technique)
    return f"{label}, {model}" if model else label


def render_report(summary: AuditSummary) -> str:
    findings_by_run: dict[str, list[EvaluatorEditFinding]] = defaultdict(list)
    for finding in summary.findings:
        findings_by_run[finding.run_path].append(finding)
    distinct_diffs = {finding.diff_sha256 for finding in summary.findings}
    affected_bugs = sorted(
        {f"{finding.project}#{finding.bug_id}" for finding in summary.findings}
    )

    lines = [
        "# Evaluator-integrity audit of committed repair results",
        "",
        "Generated by `scripts/audit_test_edits.py` on "
        f"{datetime.now(timezone.utc).date().isoformat()} (no Docker, no API calls).",
        "",
        "Before the evaluator-integrity gate existed, the validation path accepted "
        "a candidate that edited the tests judging it, so a rewritten expectation "
        "could be recorded as a *plausible* repair. This report lists every "
        "committed plausible or correct patch whose diff touches an **evaluator "
        "file**: a declared test file, anything under `tests/` or `test/`, "
        "`test_*.py` / `*_test.py`, `conftest.py`, pytest/tox configuration, or a "
        "`bugsinpy_*` script. The committed result files are left unchanged; this "
        "report is the correction. See `docs/validation-and-scoring.md` for the "
        "gate that now rejects such candidates.",
        "",
        "## Summary",
        "",
        f"- Result files scanned: **{summary.result_files}**",
        f"- Plausible or correct patches scanned: **{summary.accepted_patches}**",
        f"- Of those, patches that edit evaluator files: **{len(summary.findings)}** "
        f"in **{len(findings_by_run)}** run(s), **{len(distinct_diffs)}** distinct "
        "diff(s)",
        f"- Affected bugs: {', '.join(affected_bugs) if affected_bugs else 'none'}",
        "",
    ]
    if not summary.findings:
        lines.append("No committed plausible or correct patch edits evaluator files.")
        return "\n".join(lines) + "\n"

    lines += [
        "## Affected runs",
        "",
        "| Run | Bug | Approach | Recorded plausible/correct | Evaluator edits "
        "| Patch IDs | Touched evaluator files |",
        "|---|---|---|---:|---:|---|---|",
    ]
    for run_path, run_findings in sorted(findings_by_run.items()):
        first = run_findings[0]
        touched = sorted(
            {
                f"`{path}` ({reason})"
                for finding in run_findings
                for path, reason in finding.touched_evaluator_files.items()
            }
        )
        lines.append(
            f"| `{run_path}` | {first.project}#{first.bug_id} | {first.approach} "
            f"| {summary.accepted_by_run.get(run_path, 0)} | {len(run_findings)} "
            f"| {', '.join(finding.patch_id for finding in run_findings)} "
            f"| {'; '.join(touched)} |"
        )
    lines += [
        "",
        "## Distinct diffs",
        "",
        "Identical diffs count once as evidence, however often they were recorded.",
        "",
        "| Diff SHA-256 (first 12) | Bug | Occurrences | Runs |",
        "|---|---|---:|---|",
    ]
    findings_by_diff: dict[str, list[EvaluatorEditFinding]] = defaultdict(list)
    for finding in summary.findings:
        findings_by_diff[finding.diff_sha256].append(finding)
    for diff_sha256, diff_findings in sorted(findings_by_diff.items()):
        runs = sorted({finding.run_path for finding in diff_findings})
        lines.append(
            f"| `{diff_sha256[:12]}` | {diff_findings[0].project}#"
            f"{diff_findings[0].bug_id} | {len(diff_findings)} "
            f"| {', '.join(f'`{run}`' for run in runs)} |"
        )
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
