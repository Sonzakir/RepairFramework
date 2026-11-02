"""Dataclasses Strict Repro's stages exchange.

The stages are issue intake → generation → verification → probes → strictness →
report. Each stage takes and returns these objects, never raw strings, so the
report and the run artifacts can be written from one ``ReproRunResult``.
"""

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

# A file's state before and after a change: (original bytes, new bytes). None
# means the file does not exist on that side.
FileStates = dict[str, tuple[bytes | None, bytes | None]]

REPRODUCTION_FAILURE_KINDS = ("assertion", "exception")
# Longer issue bodies are cut in prompts (the quote check uses the full text).
MAX_PROMPT_ISSUE_CHARACTERS = 8_000


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogateescape")).hexdigest()


@dataclass(frozen=True)
class ReproIssue:
    """A bug report after the leak screen.

    Fields:
        url:        Where the issue came from (a GitHub URL or a local file path).
        title:      Issue title.
        body:       Issue body with fix-revealing links and diffs removed.
        leak_flags: Why the issue is suspicious (a diff, the fix commit id, ...).
        removed:    Short descriptions of what the screen stripped.
    """

    url: str
    title: str
    body: str
    leak_flags: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()

    @property
    def text(self) -> str:
        return f"{self.title}\n\n{self.body}".strip()

    @property
    def sha256(self) -> str:
        return sha256_text(self.text)

    @property
    def prompt_body(self) -> str:
        if len(self.body) <= MAX_PROMPT_ISSUE_CHARACTERS:
            return self.body
        return self.body[:MAX_PROMPT_ISSUE_CHARACTERS] + "\n[issue body truncated]"


@dataclass(frozen=True)
class TestVersion:
    """One version of the generated test file.

    Fields:
        label:  Where it came from (``round-1``, ``assertflip-pass-1``,
                ``hardened``, ...).
        source: The test module's source.
    """

    __test__ = False  # not a pytest test class, despite the name

    label: str
    source: str

    @property
    def sha256(self) -> str:
        return sha256_text(self.source)


@dataclass(frozen=True)
class FailureClassification:
    """What one run of the repro test showed.

    Fields:
        kind:           ``passed``, ``assertion``, ``exception``, ``test_error``,
                        ``collection_error``, ``setup_error``, ``no_tests``,
                        ``timeout`` or ``environment_error``.
        exception_type: The exception that ended the first failing test, if any.
        location:       ``file:line`` of the innermost frame, if known.
        message:        The exception message (first ``E`` line), if any.
        output_tail:    The last lines of the raw output.
    """

    kind: str
    exception_type: str = ""
    location: str = ""
    message: str = ""
    output_tail: str = ""

    @property
    def is_reproduction_candidate(self) -> bool:
        """A real assertion failure or an exception raised by the code under test."""
        return self.kind in REPRODUCTION_FAILURE_KINDS

    @property
    def signature(self) -> tuple[str, str]:
        """What repeats must agree on."""
        return (self.kind, self.exception_type)


@dataclass(frozen=True)
class JudgeVerdict:
    """The right-reason judge's answer.

    Fields:
        label:         ``reproduces``, ``wrong_reason``, ``unclear`` or ``not_run``.
        justification: One sentence from the judge.
        issue_quote:   The issue sentence the judge cites.
        quote_found:   Whether that quote really occurs in the issue text.
        note:          Why the label was downgraded or not run.
    """

    label: str
    justification: str = ""
    issue_quote: str = ""
    quote_found: bool = False
    note: str = ""

    @classmethod
    def not_run(cls, reason: str) -> "JudgeVerdict":
        return cls(label="not_run", note=reason)

    @property
    def reproduces(self) -> bool:
        return self.label == "reproduces"


@dataclass(frozen=True)
class FixCheck:
    """Whether the repro test passes once a fix is applied (F→P).

    Fields:
        fix_label:      ``developer fix`` or the patch file name.
        applied:        Whether the fix applied (and passed the integrity screen).
        integrity:      The integrity verdict label.
        repro_passes:   Whether the repro test passed with the fix applied.
        oracle_passes:  Whether the oracle (trigger test / --regression-cmd) passed
                        with the fix; None when there is no oracle.
        detail:         Why it did not apply, or the repro output tail.
    """

    fix_label: str
    applied: bool
    integrity: str = "ok"
    repro_passes: bool = False
    oracle_passes: bool | None = None
    detail: str = ""


@dataclass
class Verification:
    """Everything verification established about one test version."""

    test: TestVersion
    buggy_runs: list[FailureClassification]
    judge: JudgeVerdict
    fix_check: FixCheck | None = None

    @property
    def repeats_agree(self) -> bool:
        return len({run.signature for run in self.buggy_runs}) == 1

    @property
    def fails_for_a_real_reason(self) -> bool:
        """Every repeat failed the same way, with an assertion or expected exception."""
        return (
            bool(self.buggy_runs)
            and self.repeats_agree
            and self.buggy_runs[0].is_reproduction_candidate
        )

    @property
    def labelled_reproduces(self) -> bool:
        """The label given without the fix: real failure + judge says right reason."""
        return self.fails_for_a_real_reason and self.judge.reproduces

    @property
    def is_fail_to_pass(self) -> bool | None:
        if self.fix_check is None:
            return None
        return self.fails_for_a_real_reason and self.fix_check.repro_passes

    @property
    def is_verified_reproduction(self) -> bool | None:
        """Fails for the reported reason on the buggy code and passes on the fix."""
        if self.fix_check is None:
            return None
        return self.labelled_reproduces and self.fix_check.repro_passes


@dataclass(frozen=True)
class Probe:
    """A plausible-wrong variant of a fix: one file of the fix replaced.

    Fields:
        probe_id:   Stable id within a run (``p01``...).
        family:     ``operator``, ``drop``, ``revert`` or ``llm``.
        kind:       The AST operator, ``drop``/``revert``, or the LLM probe kind
                    (``symptom_suppression``, ``input_specific_shortcut``,
                    ``incomplete_fix``, ``behaviour_substitution``).
        path:       Checkout-relative file the probe changes.
        line:       First line the probe changes (fixed side).
        before:     The fixed line (shortened).
        after:      The probe's line (shortened).
        probe_text: The whole file as the probe has it.
        rationale:  Why the LLM believes it is wrong (LLM probes only).
    """

    probe_id: str
    family: str
    kind: str
    path: str
    line: int
    before: str
    after: str
    probe_text: str
    rationale: str = ""


@dataclass
class ProbeResult:
    """One probe's oracle verdict and the repro test's verdict on it.

    Fields:
        oracle_verdict: ``confirmed_wrong``, ``unconfirmed`` or ``invalid``.
        confirmed_by:   ``trigger`` or ``test_file`` (BugsInPy), ``regression_cmd``
                        (local); empty unless confirmed.
        oracle_detail:  Human-readable reason for the verdict.
        repro_verdict:  ``rejects``, ``accepts`` or ``not_run``.
        diff_text:      The probe as a diff against the fix (what a reviewer reads).
        full_diff_text: The probe as a diff against the buggy code (replayable).
    """

    probe: Probe
    oracle_verdict: str
    confirmed_by: str = ""
    oracle_detail: str = ""
    repro_verdict: str = "not_run"
    diff_text: str = ""
    full_diff_text: str = ""

    @property
    def is_confirmed_wrong(self) -> bool:
        return self.oracle_verdict == "confirmed_wrong"


@dataclass(frozen=True)
class StrictnessSummary:
    """Strictness of one test version over one probe pool.

    Fields:
        test_label:            Which test version was measured.
        generated:             Probes run.
        confirmed:             Confirmed-wrong probes (the denominator).
        unconfirmed:           Probes no oracle confirmed (never counted).
        invalid:               Probes that did not apply or were rejected.
        rejected_by_repro:     Confirmed-wrong probes the repro test rejects.
        accepted_probe_ids:    Confirmed-wrong probes the repro test accepts.
        trigger_rejects:       Confirmed-wrong probes the bug's own trigger test
                               rejects (BugsInPy baseline); None in local mode.
        unconfirmed_rejected:  Unconfirmed probes the repro test rejects (review
                               these: they may be valid alternative fixes).
    """

    test_label: str
    generated: int
    confirmed: int
    unconfirmed: int
    invalid: int
    rejected_by_repro: int
    accepted_probe_ids: tuple[str, ...]
    trigger_rejects: int | None
    unconfirmed_rejected: tuple[str, ...] = ()

    @property
    def strictness(self) -> float | None:
        if self.confirmed == 0:
            return None
        return self.rejected_by_repro / self.confirmed


@dataclass
class HardeningOutcome:
    """What the hardening round did.

    Fields:
        attempted:    Whether an LLM hardening call was made.
        kept:         Whether the hardened test passed re-verification and replaced
                      the test.
        reason:       Why hardening was skipped or not kept.
        test:         The hardened test, when the model wrote one.
        verification: Re-verification of the hardened test, when attempted.
        after:        Strictness of the hardened test, when kept.
        probe_results_after: The same probe pool re-judged with the hardened test.
    """

    attempted: bool
    kept: bool = False
    reason: str = ""
    test: TestVersion | None = None
    verification: Verification | None = None
    after: StrictnessSummary | None = None
    probe_results_after: list[ProbeResult] = field(default_factory=list)


@dataclass
class ReproRunResult:
    """Everything one ``repro new`` / ``repro check`` run established."""

    command: str
    target: str
    test_id: str
    issue: ReproIssue | None
    mode: str = "bugsinpy"
    test_path: Path | None = None
    generation_rounds: int = 0
    generation_stop_reason: str = ""
    generation_classifications: list[FailureClassification] = field(
        default_factory=list
    )
    retrievals: list[str] = field(default_factory=list)
    test_versions: list[TestVersion] = field(default_factory=list)
    verification: Verification | None = None
    probe_results: list[ProbeResult] = field(default_factory=list)
    strictness_before: StrictnessSummary | None = None
    hardening: HardeningOutcome | None = None
    contamination_similarity: float | None = None
    integrity_note: str = ""
    fix_diff_text: str = ""
    checkout_clean_after: bool | None = None
    llm_calls: int = 0
    llm_usd: float = 0.0
    seconds: float = 0.0
    run_dir: Path | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def final_test(self) -> TestVersion | None:
        if self.hardening and self.hardening.kept and self.hardening.verification:
            return self.hardening.verification.test
        return self.verification.test if self.verification else None
