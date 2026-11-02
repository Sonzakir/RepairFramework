"""Strictness arithmetic and the hardening round.

**Strictness** of a test over a probe pool = confirmed-wrong probes the test
rejects ÷ confirmed-wrong probes. Unconfirmed probes (possibly equivalent to the
fix) are never in the denominator; the unconfirmed ones the test rejects are
listed separately, because a strict test that rejects a valid alternative fix is
over-strict, and a person has to look at those.

**Baseline** (BugsInPy): how many of the same confirmed-wrong probes the bug's
own trigger test rejects. By the confirmation rule, a confirmed probe the
trigger accepts was caught only by the bug's test file.

**Hardening** (``--harden``): one LLM round that adds assertions only about
behaviour the issue describes, shown the confirmed-wrong probes the test accepts.
It is skipped when the test already rejects every confirmed-wrong probe. The
hardened test is kept only if it passes verification again (fails on the buggy
code for the reported reason, passes on the fix).
"""

from apr_framework.repro.generator import extract_python_module
from apr_framework.repro.models import (
    ProbeResult,
    ReproIssue,
    StrictnessSummary,
    TestVersion,
)
from apr_framework.repro.prompts import load_prompt, render_prompt
from apr_framework.repro.spend import MeteredLLMClient

_MAX_PROBES_SHOWN_FOR_HARDENING = 8


def summarise_strictness(
    test_label: str, results: list[ProbeResult], has_trigger_baseline: bool
) -> StrictnessSummary:
    """Count confirmed / unconfirmed / invalid probes and the repro test's verdicts."""
    confirmed = [result for result in results if result.is_confirmed_wrong]
    unconfirmed = [
        result for result in results if result.oracle_verdict == "unconfirmed"
    ]
    return StrictnessSummary(
        test_label=test_label,
        generated=len(results),
        confirmed=len(confirmed),
        unconfirmed=len(unconfirmed),
        invalid=sum(result.oracle_verdict == "invalid" for result in results),
        rejected_by_repro=sum(
            result.repro_verdict == "rejects" for result in confirmed
        ),
        accepted_probe_ids=tuple(
            result.probe.probe_id
            for result in confirmed
            if result.repro_verdict != "rejects"
        ),
        trigger_rejects=(
            sum(result.confirmed_by == "trigger" for result in confirmed)
            if has_trigger_baseline
            else None
        ),
        unconfirmed_rejected=tuple(
            result.probe.probe_id
            for result in unconfirmed
            if result.repro_verdict == "rejects"
        ),
    )


def select_probes_for_hardening(results: list[ProbeResult]) -> list[ProbeResult]:
    """Confirmed-wrong probes the test accepts: the lax spots to harden against."""
    return [
        result
        for result in results
        if result.is_confirmed_wrong and result.repro_verdict == "accepts"
    ][:_MAX_PROBES_SHOWN_FOR_HARDENING]


def write_hardened_test(
    issue: ReproIssue,
    test: TestVersion,
    lax_spots: list[ProbeResult],
    client: MeteredLLMClient,
) -> TestVersion:
    """One hardening call; returns the hardened module ("" if the model gave none)."""
    messages = [
        {"role": "system", "content": load_prompt("harden_system")},
        {
            "role": "user",
            "content": render_prompt(
                "harden_user",
                issue_title=issue.title,
                issue_body=issue.prompt_body,
                test_source=test.source,
                accepted_probes="\n\n".join(
                    f"# wrong fix {result.probe.probe_id} ({result.probe.kind})\n"
                    f"{result.diff_text}"
                    for result in lax_spots
                ),
            ),
        },
    ]
    with client.purpose("hardening"):
        response_text = client.complete(messages)
    return TestVersion(label="hardened", source=extract_python_module(response_text))
