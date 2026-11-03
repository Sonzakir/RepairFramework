"""``bugsinpy evaluate-repro``: Strict Repro over an explicit, pre-declared bug list.

There is no default bug list: the command refuses to run without ``--bugs`` and
``--max-usd``, so an evaluation is always a deliberate, capped choice. Every bug
runs the full ``repro new`` pipeline (its own ``runs/run_NNN``) against one
shared spend meter; when the cap is reached the remaining bugs are recorded as
``not run`` rather than silently dropped. ``results.json`` is rewritten after
every bug (an interrupted evaluation keeps what finished) and the generated
``README.md`` scores the run against the Feature Summary §8 thresholds. An
existing ``results.json`` is never overwritten.
"""

import json
import statistics
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from apr_framework.benchmarks.bugsinpy import BugsInPyAdapter
from apr_framework.core.exceptions import ConfigurationError
from apr_framework.core.models import BugIdentifier
from apr_framework.repro.models import ReproRunResult
from apr_framework.repro.pipeline import (
    ReproLLMSettings,
    ReproOptions,
    run_repro_new_on_bugsinpy,
)
from apr_framework.repro.report import summarise_result
from apr_framework.repro.spend import SpendMeter

# Feature Summary §8: (target, drop-if) per metric.
THRESHOLDS = {
    "verified_reproduction_rate": (0.40, 0.25),
    "label_precision": (0.90, 0.80),
    "strict_at_least_baseline_share": (0.60, None),
    "hardening_changes_nothing_share": (None, 0.90),
    "median_usd_per_bug": (0.50, None),
    "median_minutes_per_bug": (6.0, None),
}


@dataclass
class ReproCell:
    """One bug's outcome in the evaluation."""

    bug: str
    status: str  # "ok", "error" or "not_run"
    run_dir: str | None = None
    summary: dict = field(default_factory=dict)
    error: str = ""


def parse_explicit_bug_list(spec: str | None) -> list[BugIdentifier]:
    """``project:bug,project:bug,...``; refuses an empty or malformed list."""
    if not spec or not spec.strip():
        raise ConfigurationError(
            "evaluate-repro needs an explicit --bugs list (project:bug,...); "
            "there is no default that covers all bugs."
        )
    bugs: list[BugIdentifier] = []
    for item in spec.split(","):
        project, separator, bug_id = item.strip().partition(":")
        if not separator or not project or not bug_id.isdigit():
            raise ConfigurationError(f"Malformed bug {item!r}; expected project:bug")
        bug = BugIdentifier("bugsinpy", project, int(bug_id))
        if bug not in bugs:
            bugs.append(bug)
    return bugs


def validate_spend_cap(max_usd: float | None) -> float:
    if max_usd is None or max_usd <= 0:
        raise ConfigurationError("evaluate-repro needs a positive --max-usd cap.")
    return max_usd


class ReproEvaluationRunner:
    """Runs ``repro new`` per bug under one cap and writes the evaluation report."""

    name = "strict-repro"

    def run(
        self,
        bugs: list[BugIdentifier],
        adapter: BugsInPyAdapter,
        project_root: Path,
        settings: ReproLLMSettings,
        options: ReproOptions,
        output_dir: Path,
    ) -> list[ReproCell]:
        refuse_existing_results(output_dir)
        meter = SpendMeter(cap_usd=options.max_usd)
        started_at = datetime.now(timezone.utc)
        cells: list[ReproCell] = []
        for bug in bugs:
            cells.append(
                run_one_bug_cell(bug, adapter, project_root, settings, options, meter)
            )
            write_evaluation_results(
                output_dir, bugs, cells, settings, options, meter, started_at
            )
        return cells


def refuse_existing_results(output_dir: Path) -> None:
    if (output_dir / "results.json").exists():
        raise ConfigurationError(
            f"{output_dir / 'results.json'} exists; committed results are never "
            "rewritten. Choose another --output-dir."
        )


def run_one_bug_cell(
    bug: BugIdentifier,
    adapter: BugsInPyAdapter,
    project_root: Path,
    settings: ReproLLMSettings,
    options: ReproOptions,
    meter: SpendMeter,
) -> ReproCell:
    label = f"{bug.project}:{bug.bug_id}"
    if meter.spent_usd >= meter.cap_usd:
        return ReproCell(bug=label, status="not_run", error="spend cap reached")
    try:
        result = run_repro_new_on_bugsinpy(
            bug, adapter, project_root, settings, options, meter=meter
        )
    except Exception as error:  # noqa: BLE001 — record every failing cell
        return ReproCell(
            bug=label,
            status="error",
            error=f"{type(error).__name__}: {error}"[-800:]
            + "\n"
            + traceback.format_exc()[-1200:],
        )
    print_cell_progress(label, result)
    return ReproCell(
        bug=label,
        status="ok",
        run_dir=str(result.run_dir),
        summary=summarise_result(result),
    )


def print_cell_progress(label: str, result: ReproRunResult) -> None:
    verification = result.verification
    print(
        f"{label:<12} verified={verification.is_verified_reproduction if verification else None} "
        f"judge={verification.judge.label if verification else '-'} "
        f"usd={result.llm_usd:.3f} t={result.seconds:.0f}s run={result.run_dir}",
        flush=True,
    )


def compute_evaluation_metrics(cells: list[ReproCell]) -> dict:
    """The Feature Summary §8 technical metrics over the evaluated bugs."""
    declared = len(cells)
    summaries = [cell.summary for cell in cells if cell.status == "ok"]
    verdicts = [summary["verdict"] for summary in summaries]
    labelled = [v for v in verdicts if v["labelled_reproduces"]]
    measured = [s for s in summaries if (s["strictness_before"] or {}).get("confirmed")]
    return {
        "bugs_declared": declared,
        "bugs_completed": len(summaries),
        "verified_reproductions": sum(
            bool(v["verified_reproduction"]) for v in verdicts
        ),
        "verified_reproduction_rate": ratio(
            sum(bool(v["verified_reproduction"]) for v in verdicts), declared
        ),
        "labelled_reproduces": len(labelled),
        "label_precision": ratio(
            sum(bool(v["fail_to_pass"]) for v in labelled), len(labelled)
        ),
        "bugs_with_confirmed_probes": len(measured),
        "strict_at_least_baseline_share": ratio(
            sum(is_final_test_at_least_baseline(s) for s in measured), len(measured)
        ),
        "hardening_changes_nothing_share": ratio(
            sum(not is_hardening_effective(s) for s in measured), len(measured)
        ),
        "near_copies": sum(bool(s["near_copy"]) for s in summaries),
        "median_usd_per_bug": median([s["llm_usd"] for s in summaries]),
        "median_minutes_per_bug": median([s["seconds"] / 60 for s in summaries]),
        "total_usd": round(sum(s["llm_usd"] for s in summaries), 4),
    }


def final_strictness_record(summary: dict) -> dict:
    hardening = summary.get("hardening") or {}
    if hardening.get("kept") and hardening.get("after"):
        return hardening["after"]
    return summary["strictness_before"]


def is_final_test_at_least_baseline(summary: dict) -> bool:
    record = final_strictness_record(summary)
    return record["rejected_by_repro"] >= (record["trigger_rejects"] or 0)


def is_hardening_effective(summary: dict) -> bool:
    hardening = summary.get("hardening") or {}
    if not hardening.get("kept") or not hardening.get("after"):
        return False
    return (
        hardening["after"]["rejected_by_repro"]
        > summary["strictness_before"]["rejected_by_repro"]
    )


def write_evaluation_results(
    output_dir: Path,
    bugs: list[BugIdentifier],
    cells: list[ReproCell],
    settings: ReproLLMSettings,
    options: ReproOptions,
    meter: SpendMeter,
    started_at: datetime,
) -> None:
    """Flush ``results.json`` and the generated ``README.md`` (after every bug)."""
    output_dir.mkdir(parents=True, exist_ok=True)
    pending = [f"{bug.project}:{bug.bug_id}" for bug in bugs[len(cells) :]]
    metrics = compute_evaluation_metrics(cells)
    payload = {
        "harness": "strict-repro",
        "started_at": started_at.isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "declared_bugs": [f"{bug.project}:{bug.bug_id}" for bug in bugs],
        "pending_bugs": pending,
        "model": settings.model_name,
        "temperatures": {
            "generation": settings.generation_temperature,
            "judge": settings.judge_temperature,
        },
        "max_usd": options.max_usd,
        "spent_usd": round(meter.spent_usd, 6),
        "metrics": metrics,
        "cells": [cell.__dict__ for cell in cells],
    }
    (output_dir / "results.json").write_text(
        json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8"
    )
    (output_dir / "README.md").write_text(
        render_evaluation_readme(payload), encoding="utf-8"
    )


def render_evaluation_readme(payload: dict) -> str:
    metrics = payload["metrics"]
    lines = [
        "# Strict Repro evaluation",
        "",
        "_Generated by `bugsinpy evaluate-repro` from `results.json`; do not edit._",
        "",
        f"- Bugs (pre-declared): {', '.join(payload['declared_bugs'])}",
        (
            f"- Model: {payload['model']} (generation T={payload['temperatures']['generation']}, "
            f"judge T={payload['temperatures']['judge']})"
        ),
        f"- Spend: ${payload['spent_usd']:.4f} of ${payload['max_usd']:.2f}",
        f"- Pending (not run yet): {', '.join(payload['pending_bugs']) or 'none'}",
        "",
        "With this few bugs the rates are indicative, not statistically significant.",
        "",
        "## Metrics against Feature Summary §8",
        "",
        "| Metric | Value | Target | Drop if |",
        "|---|---|---|---|",
    ]
    for name, (target, drop_if) in THRESHOLDS.items():
        lines.append(
            f"| {name} | {format_metric(metrics.get(name))} | "
            f"{format_metric(target)} | {format_metric(drop_if)} |"
        )
    lines += [
        "",
        (
            f"Verified reproductions: {metrics['verified_reproductions']} of "
            f'{metrics["bugs_declared"]}; labelled "reproduces": '
            f"{metrics['labelled_reproduces']}; bugs with confirmed-wrong probes: "
            f"{metrics['bugs_with_confirmed_probes']}; near-copies of the trigger test: "
            f"{metrics['near_copies']}."
        ),
        "",
        "## Per bug",
        "",
        (
            "| Bug | Status | Real failure | Judge | F→P | Verified | Confirmed probes | "
            "Rejected (before) | Trigger rejects | Hardened | Rejected (after) | "
            "Similarity | USD | Seconds | Run |"
        ),
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for cell in payload["cells"]:
        lines.append(render_cell_row(cell))
    lines.append("")
    return "\n".join(lines)


def render_cell_row(cell: dict) -> str:
    if cell["status"] != "ok":
        return (
            f"| {cell['bug']} | {cell['status']}: {cell['error'].splitlines()[0][:80]} |"
            + " |" * 13
        )
    summary = cell["summary"]
    verdict = summary["verdict"]
    before = summary["strictness_before"] or {}
    hardening = summary["hardening"] or {}
    after = hardening.get("after") or {}
    similarity = summary["contamination_similarity"]
    return (
        f"| {cell['bug']} | ok | {verdict['fails_for_a_real_reason']} | "
        f"{verdict['judge_label']} | {verdict['fail_to_pass']} | "
        f"{verdict['verified_reproduction']} | {before.get('confirmed', '-')} | "
        f"{before.get('rejected_by_repro', '-')} | {before.get('trigger_rejects', '-')} | "
        f"{'kept' if hardening.get('kept') else (hardening.get('reason') or '-')[:40]} | "
        f"{after.get('rejected_by_repro', '-')} | "
        f"{'-' if similarity is None else f'{similarity:.2f}'} | "
        f"{summary['llm_usd']:.3f} | {summary['seconds']:.0f} | {cell['run_dir']} |"
    )


def ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 3) if denominator else None


def median(values: list[float]) -> float | None:
    return round(statistics.median(values), 3) if values else None


def format_metric(value) -> str:
    return "-" if value is None else str(value)
