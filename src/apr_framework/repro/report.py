"""Strict Repro's terminal report, run artifacts and witness export.

Run directory ``runs/run_NNN/``:

    config.json           command, target, model, temperatures, caps, prices,
                          prompt template hashes
    issue.json            issue URL, SHA-256 and leak-screen flags (never the text)
    prompts/              the prompt templates used, as shipped
    tests/NN_<label>.py   every generated test version, in order
    test_repro_<id>.py    the final test; with repro.json, ``repro check`` input
    repro.json            what ``repro check`` needs (BugsInPy runs)
    verification.json     buggy-code runs (3 repeats), judge verdict, F→P check,
                          for the test and, when hardened, the hardened test
    generation.json       each version's single buggy-code run, retrievals
    probes/               every probe as a diff against the fix
    probes.json           every probe's oracle verdict and the repro verdicts
    llm_calls.json        every LLM call: purpose, tokens, USD, messages hash
    result.json           the summary the terminal table shows
    execution.log
"""

import json
import re
import shutil
from dataclasses import asdict
from pathlib import Path

from apr_framework.evaluation.run_writer import RunWriter
from apr_framework.repro.contamination import NEAR_COPY_THRESHOLD
from apr_framework.repro.models import (
    ProbeResult,
    ReproRunResult,
    StrictnessSummary,
    Verification,
)
from apr_framework.repro.prompts import PROMPT_NAMES, load_prompt, prompt_hashes
from apr_framework.repro.runner import repro_test_file_name
from apr_framework.repro.spend import SpendMeter

WITNESS_EXPORT_STATUS = "candidate"
_LABEL_WIDTH = 27


# --------------------------------------------------------------- terminal


def format_repro_report(result: ReproRunResult) -> str:
    """The terminal table (Feature Summary §3 layout)."""
    lines = [format_heading(result)]
    if result.final_test is not None and result.run_dir is not None:
        test_location = result.test_path or result.run_dir / repro_test_file_name(
            result.test_id
        )
        rounds = (
            f"({result.generation_rounds} generation rounds)"
            if result.command == "repro new"
            else ""
        )
        lines.append(f"Test: {test_location}   {rounds}".rstrip())
    if result.integrity_note:
        lines.append(row("integrity gate", result.integrity_note))
    if result.verification is not None:
        lines.extend(format_verification_rows(result.verification))
    lines.extend(format_probe_rows(result))
    if result.contamination_similarity is not None:
        near_copy = result.contamination_similarity >= NEAR_COPY_THRESHOLD
        lines.append(
            row(
                "similarity to trigger test",
                f"{result.contamination_similarity:.2f}"
                + ("  NEAR-COPY (possible contamination)" if near_copy else ""),
            )
        )
    if result.issue is not None and result.issue.leak_flags:
        lines.append(row("issue leak screen", "; ".join(result.issue.leak_flags)))
    lines.append(row("llm", f"{result.llm_calls} calls, ${result.llm_usd:.4f}"))
    lines.append(row("time", f"{result.seconds:.0f}s"))
    lines.extend(row("note", note) for note in result.notes)
    if result.run_dir is not None:
        lines.append(row("evidence", f"{result.run_dir}/"))
    return "\n".join(lines)


def format_heading(result: ReproRunResult) -> str:
    title = result.issue.title if result.issue else ""
    return f'Strict Repro · {result.target} "{title}"'


def format_verification_rows(verification: Verification) -> list[str]:
    runs = verification.buggy_runs
    first = runs[0] if runs else None
    failing = sum(run.kind != "passed" for run in runs)
    if first is None:
        fails_row = "not run"
    elif verification.fails_for_a_real_reason:
        fails_row = (
            f"yes, {failing}/{len(runs)} repeats   "
            f"{first.exception_type or first.kind}: {first.message[:70]}"
        )
    else:
        kinds = ", ".join(sorted({run.kind for run in runs}))
        fails_row = f"no ({kinds}; {failing}/{len(runs)} repeats fail)"
    judge = verification.judge
    judge_row = {
        "reproduces": "yes",
        "wrong_reason": "no",
        "unclear": "unclear",
        "not_run": "not judged",
    }[judge.label]
    judge_detail = judge.justification or judge.note
    rows = [
        row("fails on current code", fails_row),
        row(
            "fails for reported reason",
            f"{judge_row}"
            + (f"  (judge: {judge_detail[:110]})" if judge_detail else ""),
        ),
        row("collection/import errors", describe_error_runs(verification)),
    ]
    fix_check = verification.fix_check
    if fix_check is None:
        rows.append(
            row(
                "strictness",
                "not measured: no fix yet. Give the issue + test to your agent.",
            )
        )
    elif not fix_check.applied:
        rows.append(
            row(
                f"passes on {fix_check.fix_label}",
                f"not applied ({fix_check.detail[:80]})",
            )
        )
    else:
        rows.append(
            row(
                f"passes on {fix_check.fix_label}",
                "yes (fails before, passes after)"
                if verification.is_fail_to_pass
                else ("yes" if fix_check.repro_passes else "no"),
            )
        )
    return rows


def describe_error_runs(verification: Verification) -> str:
    error_kinds = [
        run.kind
        for run in verification.buggy_runs
        if run.kind
        in (
            "collection_error",
            "setup_error",
            "environment_error",
            "test_error",
            "no_tests",
        )
    ]
    return "none" if not error_kinds else ", ".join(sorted(set(error_kinds)))


def format_probe_rows(result: ReproRunResult) -> list[str]:
    before = result.strictness_before
    if before is None:
        return []
    rows = [
        row(
            "wrong-fix probes",
            f"{before.generated} generated, {before.confirmed} confirmed wrong, "
            f"{before.unconfirmed} unconfirmed, {before.invalid} invalid",
        ),
        row("  rejected", format_strictness(before)),
    ]
    if before.accepted_probe_ids:
        rows.append(
            row(
                "  accepted (lax spots)",
                f"{len(before.accepted_probe_ids)}: "
                + " · ".join(
                    describe_probe_ids(result.probe_results, before.accepted_probe_ids)
                ),
            )
        )
    hardening = result.hardening
    if hardening is not None:
        rows.append(row("hardened test (1 round)", describe_hardening(result)))
        if hardening.kept and hardening.after and hardening.after.unconfirmed_rejected:
            rows.append(
                row(
                    "  review (may be over-strict)",
                    "rejects unconfirmed "
                    + ", ".join(hardening.after.unconfirmed_rejected),
                )
            )
    return rows


def format_strictness(summary: StrictnessSummary) -> str:
    if summary.confirmed == 0:
        return "n/a (no confirmed-wrong probe)"
    text = f"{summary.rejected_by_repro}/{summary.confirmed}"
    if summary.trigger_rejects is not None:
        text += f"   (the bug's trigger test rejects {summary.trigger_rejects}/{summary.confirmed})"
    return text


def describe_probe_ids(
    results: list[ProbeResult], probe_ids: tuple[str, ...]
) -> list[str]:
    by_id = {result.probe.probe_id: result.probe for result in results}
    return [
        f"{probe_id} {by_id[probe_id].kind} {by_id[probe_id].path}:{by_id[probe_id].line}"
        for probe_id in probe_ids
        if probe_id in by_id
    ]


def describe_hardening(result: ReproRunResult) -> str:
    hardening = result.hardening
    if not hardening.attempted:
        return f"skipped: {hardening.reason}"
    if not hardening.kept:
        return f"not kept: {hardening.reason}"
    after = hardening.after
    return (
        f"rejects {format_strictness(after)}, still passes on "
        f"{hardening.verification.fix_check.fix_label}"
    )


def row(label: str, value: str) -> str:
    return f"{label:<{_LABEL_WIDTH}}{value}"


# --------------------------------------------------------------- artifacts


def write_run_artifacts(
    result: ReproRunResult, writer: RunWriter, meter: SpendMeter
) -> None:
    run_dir = writer.run_dir
    write_issue_record(result, writer)
    write_prompt_copies(run_dir)
    write_test_versions(result, run_dir)
    write_final_test_and_state(result, writer)
    writer.write_json("generation.json", describe_generation(result))
    writer.write_json("verification.json", describe_verifications(result))
    write_probe_artifacts(result, writer)
    (run_dir / "llm_calls.json").write_text(meter.to_json(), encoding="utf-8")
    writer.write_json("result.json", summarise_result(result))
    writer.log(
        f"Finished {result.command} for {result.target}: "
        f"{json.dumps(summarise_result(result)['verdict'])}"
    )


def write_issue_record(result: ReproRunResult, writer: RunWriter) -> None:
    issue = result.issue
    if issue is None:
        return
    writer.write_json(
        "issue.json",
        {
            "url": issue.url,
            "sha256": issue.sha256,
            "leak_flags": list(issue.leak_flags),
            "removed_by_leak_screen": list(issue.removed),
            "note": "The issue text is fetched at run time and never stored here.",
        },
    )


def write_prompt_copies(run_dir: Path) -> None:
    prompts_dir = run_dir / "prompts"
    prompts_dir.mkdir(exist_ok=True)
    for name in PROMPT_NAMES:
        (prompts_dir / f"{name}.txt").write_text(load_prompt(name), encoding="utf-8")
    (prompts_dir / "sha256.json").write_text(
        json.dumps(prompt_hashes(), indent=2) + "\n", encoding="utf-8"
    )


def write_test_versions(result: ReproRunResult, run_dir: Path) -> None:
    tests_dir = run_dir / "tests"
    tests_dir.mkdir(exist_ok=True)
    for index, version in enumerate(result.test_versions, start=1):
        safe_label = re.sub(r"\W", "_", version.label)
        (tests_dir / f"{index:02d}_{safe_label}.py").write_text(
            version.source, encoding="utf-8"
        )


def write_final_test_and_state(result: ReproRunResult, writer: RunWriter) -> None:
    """The final test and, for BugsInPy runs, ``repro.json`` (a repro directory)."""
    final_test = result.final_test
    if final_test is None:
        return
    test_file_name = repro_test_file_name(result.test_id)
    (writer.run_dir / test_file_name).write_text(final_test.source, encoding="utf-8")
    if result.mode != "bugsinpy" or result.command != "repro new":
        return
    project, bug_id = result.target.split(":")
    writer.write_json(
        "repro.json",
        {
            "mode": "bugsinpy",
            "target": result.target,
            "project": project,
            "bug_id": int(bug_id),
            "test_id": result.test_id,
            "test_file": test_file_name,
            "test_label": final_test.label,
            "issue": result.issue.url if result.issue else "",
            "issue_sha256": result.issue.sha256 if result.issue else "",
        },
    )


def describe_generation(result: ReproRunResult) -> dict:
    return {
        "rounds": result.generation_rounds,
        "stop_reason": result.generation_stop_reason,
        "retrievals": result.retrievals,
        "versions": [
            {
                "label": version.label,
                "sha256": version.sha256,
                "buggy_run": asdict(classification),
            }
            for version, classification in zip(
                result.test_versions, result.generation_classifications
            )
        ],
    }


def describe_verifications(result: ReproRunResult) -> dict:
    payload = {"test": describe_verification(result.verification)}
    if result.hardening and result.hardening.verification:
        payload["hardened"] = describe_verification(result.hardening.verification)
    return payload


def describe_verification(verification: Verification | None) -> dict | None:
    if verification is None:
        return None
    return {
        "test_label": verification.test.label,
        "test_sha256": verification.test.sha256,
        "buggy_runs": [asdict(run) for run in verification.buggy_runs],
        "repeats_agree": verification.repeats_agree,
        "fails_for_a_real_reason": verification.fails_for_a_real_reason,
        "judge": asdict(verification.judge),
        "labelled_reproduces": verification.labelled_reproduces,
        "fix_check": asdict(verification.fix_check) if verification.fix_check else None,
        "fail_to_pass": verification.is_fail_to_pass,
        "verified_reproduction": verification.is_verified_reproduction,
    }


def write_probe_artifacts(result: ReproRunResult, writer: RunWriter) -> None:
    if not result.probe_results:
        return
    probes_dir = writer.run_dir / "probes"
    probes_dir.mkdir(exist_ok=True)
    after_by_id = (
        {r.probe.probe_id: r for r in result.hardening.probe_results_after}
        if result.hardening
        else {}
    )
    entries = []
    for probe_result in result.probe_results:
        probe = probe_result.probe
        diff_name = f"{probe.probe_id}_{probe.family}_{probe.kind}.diff"
        (probes_dir / diff_name).write_text(probe_result.diff_text, encoding="utf-8")
        after = after_by_id.get(probe.probe_id)
        entries.append(
            {
                "probe_id": probe.probe_id,
                "family": probe.family,
                "kind": probe.kind,
                "path": probe.path,
                "line": probe.line,
                "before": probe.before,
                "after": probe.after,
                "rationale": probe.rationale,
                "diff": f"probes/{diff_name}",
                "oracle_verdict": probe_result.oracle_verdict,
                "confirmed_by": probe_result.confirmed_by,
                "oracle_detail": probe_result.oracle_detail,
                "repro_verdict": probe_result.repro_verdict,
                "hardened_repro_verdict": after.repro_verdict if after else None,
            }
        )
    writer.write_json("probes.json", {"probes": entries})


def summarise_result(result: ReproRunResult) -> dict:
    verification = result.verification
    hardening = result.hardening
    return {
        "command": result.command,
        "mode": result.mode,
        "target": result.target,
        "test_id": result.test_id,
        "issue_url": result.issue.url if result.issue else None,
        "issue_sha256": result.issue.sha256 if result.issue else None,
        "issue_leak_flags": list(result.issue.leak_flags) if result.issue else [],
        "generation_rounds": result.generation_rounds,
        "verdict": {
            "fails_for_a_real_reason": verification.fails_for_a_real_reason
            if verification
            else None,
            "judge_label": verification.judge.label if verification else None,
            "labelled_reproduces": verification.labelled_reproduces
            if verification
            else None,
            "fail_to_pass": verification.is_fail_to_pass if verification else None,
            "verified_reproduction": verification.is_verified_reproduction
            if verification
            else None,
        },
        "integrity": result.integrity_note,
        "strictness_before": summarise_strictness_record(result.strictness_before),
        "hardening": {
            "attempted": hardening.attempted,
            "kept": hardening.kept,
            "reason": hardening.reason,
            "after": summarise_strictness_record(hardening.after),
        }
        if hardening
        else None,
        "contamination_similarity": result.contamination_similarity,
        "near_copy": (
            result.contamination_similarity is not None
            and result.contamination_similarity >= NEAR_COPY_THRESHOLD
        ),
        "checkout_clean_after": result.checkout_clean_after,
        "llm_calls": result.llm_calls,
        "llm_usd": round(result.llm_usd, 6),
        "seconds": result.seconds,
        "notes": result.notes,
    }


def summarise_strictness_record(summary: StrictnessSummary | None) -> dict | None:
    if summary is None:
        return None
    return asdict(summary) | {"strictness": summary.strictness}


# --------------------------------------------------------------- witness export


def export_witness_bundle(result: ReproRunResult, destination: Path) -> Path | None:
    """Write a ``candidate`` Break This Fix bundle draft; never into challenges/.

    The witness is the final repro test, wrapped so ``unittest discover`` (how
    ``challenge run`` runs witnesses) executes its fixture-free test functions.
    The candidate is the ``--patch`` of ``repro check``, or (BugsInPy ``new``) the
    first probe that the bug's trigger test accepts but the repro test rejects.
    Creating the case under ``challenges/cases/`` is left to the maintainer.
    """
    final_test = result.final_test
    if final_test is None or result.run_dir is None:
        return None
    case_id = f"{result.test_id.replace('_', '-')}-strict-repro"
    bundle_dir = destination / case_id
    if bundle_dir.exists():
        shutil.rmtree(bundle_dir)
    (bundle_dir / "witness").mkdir(parents=True)
    witness_name = "test_witness_" + re.sub(r"\W", "_", case_id) + ".py"
    (bundle_dir / "witness" / witness_name).write_text(
        wrap_as_unittest_witness(final_test.source), encoding="utf-8"
    )
    candidate_note = write_candidate_diff(result, bundle_dir)
    (bundle_dir / "case.json").write_text(
        json.dumps(
            build_case_draft(result, case_id, witness_name, candidate_note), indent=2
        )
        + "\n",
        encoding="utf-8",
    )
    (bundle_dir / "NOTES.md").write_text(
        "# Why the expected behaviour is right\n\nTo be written by the curator: quote "
        "the issue sentence the witness enforces (see verification.json in "
        f"{result.run_dir}).\n",
        encoding="utf-8",
    )
    return bundle_dir


def wrap_as_unittest_witness(test_source: str) -> str:
    return (
        test_source.rstrip()
        + "\n\n\n# --- added by Strict Repro's witness export: run the test functions\n"
        "# --- under unittest discovery, as `challenge run` does.\n"
        "import inspect as _inspect\n"
        "import unittest as _unittest\n\n\n"
        "class StrictReproWitness(_unittest.TestCase):\n"
        "    pass\n\n\n"
        "def _as_method(function):\n"
        "    def method(self):\n"
        "        function()\n\n"
        "    return method\n\n\n"
        "for _name, _function in list(globals().items()):\n"
        "    if _name.startswith('test') and _inspect.isfunction(_function):\n"
        "        if _inspect.signature(_function).parameters:\n"
        "            raise TypeError(f'{_name} needs pytest fixtures; not exportable')\n"
        "        setattr(StrictReproWitness, _name, _as_method(_function))\n"
    )


def write_candidate_diff(result: ReproRunResult, bundle_dir: Path) -> str:
    """The checked patch if the repro test rejects it, else a probe lead."""
    fix_check = result.verification.fix_check if result.verification else None
    if result.command == "repro check" and fix_check and not fix_check.repro_passes:
        (bundle_dir / "candidate.diff").write_text(
            result.fix_diff_text, encoding="utf-8"
        )
        return "the patch passed to `repro check`, which the repro test rejects"
    lead = next(
        (
            probe_result
            for probe_result in result.probe_results
            if probe_result.confirmed_by == "test_file"
            and probe_result.repro_verdict == "rejects"
        ),
        None,
    )
    if lead is None:
        (bundle_dir / "candidate.diff").write_text("", encoding="utf-8")
        return "no candidate: no probe passes the trigger test and fails the repro test"
    (bundle_dir / "candidate.diff").write_text(lead.full_diff_text, encoding="utf-8")
    return (
        f"probe {lead.probe.probe_id} ({lead.probe.kind}): passes the bug's trigger "
        "test, fails its test file and the repro test"
    )


def build_case_draft(
    result: ReproRunResult, case_id: str, witness_name: str, candidate_note: str
) -> dict:
    project, _, bug_id = result.target.partition(":")
    return {
        "id": case_id,
        "version": 1,
        "status": WITNESS_EXPORT_STATUS,
        "type": "C",
        "title": "TODO",
        "project": project,
        "bug_id": int(bug_id) if bug_id.isdigit() else None,
        "subject_license": "TODO",
        "promise": "TODO: the issue-described behaviour the witness enforces",
        "contract_basis": [
            {
                "source": "issue",
                "url": result.issue.url if result.issue else "",
                "quote": "TODO",
            }
        ],
        "recorded_test_scope": {},
        "candidate": {"diff": "candidate.diff", "summary": candidate_note},
        "witness": {"path": f"witness/{witness_name}"},
        "expected": {
            "buggy": {"witness": "fail"},
            "candidate": {"trigger": "pass", "witness": "fail"},
            "developer_fixed": {"trigger": "pass", "witness": "pass"},
        },
        "replay_log": [],
        "reviewers": [],
        "dispute_history": [],
        "provenance": {
            "tool": "Strict Repro witness export",
            "run_dir": str(result.run_dir),
        },
    }
