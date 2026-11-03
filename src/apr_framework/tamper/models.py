"""Findings and the change set Tamper Check reads."""

from dataclasses import dataclass, field

WEAKENING = "weakening"
SUSPICIOUS = "suspicious"
STRENGTHENING = "strengthening"
NEUTRAL = "neutral"
SEVERITY_ORDER = (WEAKENING, SUSPICIOUS, STRENGTHENING, NEUTRAL)

TAMPERED = "tampered"
CLEAN = "clean"


@dataclass(frozen=True)
class FileChange:
    """One file before and after the change (None: absent on that side)."""

    relative_path: str
    old_text: str | None
    new_text: str | None

    @property
    def is_creation(self) -> bool:
        return self.old_text is None

    @property
    def is_deletion(self) -> bool:
        return self.new_text is None


@dataclass(frozen=True)
class Finding:
    """One thing the change did to the tests or to how they run.

    Fields:
        kind:     ``test_deleted``, ``assertion_loosened``, ``test_added``, ... (see
                  docs/tamper-check.md for the full list).
        severity: ``weakening``, ``suspicious``, ``strengthening`` or ``neutral``.
        file:     The file the finding is about.
        test:     The test's qualname, when the finding is about one test.
        line:     A line in the new file (the old one for deletions).
        before:   The relevant code before the change.
        after:    The relevant code after the change.
        message:  One sentence for the report.
    """

    kind: str
    severity: str
    file: str
    message: str
    test: str | None = None
    line: int | None = None
    before: str = ""
    after: str = ""

    def to_json(self) -> dict:
        return dict(self.__dict__)


@dataclass
class TamperReport:
    """Every finding, the verdict, and what the dynamic checks observed."""

    target: dict
    findings: list[Finding]
    changed_files: list[str]
    source_files_changed: list[str]
    dynamic: list[dict] = field(default_factory=list)
    dynamic_note: str = ""

    @property
    def verdict(self) -> str:
        severities = {finding.severity for finding in self.findings}
        if WEAKENING in severities:
            return TAMPERED
        if SUSPICIOUS in severities:
            return SUSPICIOUS
        return CLEAN

    def counts(self) -> dict[str, int]:
        counts = dict.fromkeys(SEVERITY_ORDER, 0)
        for finding in self.findings:
            counts[finding.severity] += 1
        return counts

    def to_json(self) -> dict:
        return {
            "verdict": self.verdict,
            "counts": self.counts(),
            "target": self.target,
            "changed_files": self.changed_files,
            "source_files_changed": self.source_files_changed,
            "findings": [finding.to_json() for finding in self.findings],
            "dynamic": self.dynamic,
            "dynamic_note": self.dynamic_note,
        }
