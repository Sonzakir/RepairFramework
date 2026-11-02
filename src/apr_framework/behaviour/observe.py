"""Run the observation driver on one version of the project.

``ObservationRunner`` copies ``driver.py`` into a scratch directory the target's
runner can see, then, per version: applies the version (exact restore
guaranteed), writes a spec with *that version's* focus spans, runs the driver in
the target's environment, reads its JSON, and restores the checkout.
"""

import json
import re
import shutil
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from apr_framework.behaviour import driver as driver_module
from apr_framework.behaviour.amplify import VARIANT_MARKER
from apr_framework.behaviour.focus import driver_focus_entries
from apr_framework.challenge.multi_file_patch import PatchApplyError
from apr_framework.evidence.target import EvidenceTarget, SuiteCommand
from apr_framework.evidence.versions import FixVersion, applied

DRIVER_FILE_NAME = "apr_evidence_driver.py"
OBSERVED = "observed"
NOT_APPLIED = "not_applied"
TIMED_OUT = "timeout"
NO_OUTPUT = "no_output"


@dataclass
class RecordedTest:
    """One test's observation (see ``driver.py`` for the meaning of each field)."""

    test_id: str
    outcome: str
    detail: str = ""
    file: str | None = None
    qualname: str | None = None
    line: int | None = None
    calls: list[dict] = field(default_factory=list)
    dropped_calls: int = 0
    lines: list[str] = field(default_factory=list)

    @classmethod
    def from_json(cls, payload: dict) -> "RecordedTest":
        return cls(
            test_id=payload["id"],
            outcome=payload.get("outcome", "error"),
            detail=payload.get("detail", ""),
            file=payload.get("file"),
            qualname=payload.get("qualname"),
            line=payload.get("line"),
            calls=list(payload.get("calls") or []),
            dropped_calls=int(payload.get("dropped_calls") or 0),
            lines=list(payload.get("lines") or []),
        )


@dataclass
class Observation:
    """Everything one driver run established about one version.

    Fields:
        label:        The version's label (``buggy#2`` for the calibration rerun).
        status:       ``observed``, ``not_applied``, ``timeout`` or ``no_output``.
        detail:       Why the status is not ``observed``; the driver's error if any.
        seconds:      Wall-clock seconds of the suite run (driver-reported).
        python:       The target interpreter's version.
        tests:        Test records by id, in execution order.
        focus_ids:    The focus functions this version defines.
        output_tail:  The last lines of the command output, for diagnosis.
    """

    label: str
    status: str
    detail: str = ""
    seconds: float = 0.0
    python: str = ""
    tests: dict[str, RecordedTest] = field(default_factory=dict)
    focus_ids: tuple[str, ...] = ()
    output_tail: str = ""
    wall_seconds: float = 0.0

    @property
    def observed(self) -> bool:
        return self.status == OBSERVED

    def outcome_counts(self, *, variants: bool | None = None) -> dict[str, int]:
        """Outcomes per kind; ``variants`` True/False counts only variants/originals."""
        return dict(
            Counter(
                record.outcome
                for record in self.tests.values()
                if variants is None or (VARIANT_MARKER in record.test_id) == variants
            )
        )

    def to_json(self) -> dict:
        return {
            "label": self.label,
            "status": self.status,
            "detail": self.detail,
            "seconds": self.seconds,
            "python": self.python,
            "focus_ids": list(self.focus_ids),
            "outcome_counts": self.outcome_counts(variants=False),
            "variant_outcome_counts": self.outcome_counts(variants=True),
            "tests": [
                {
                    "id": record.test_id,
                    "outcome": record.outcome,
                    "detail": record.detail,
                    "file": record.file,
                    "qualname": record.qualname,
                    "line": record.line,
                    "calls": record.calls,
                    "dropped_calls": record.dropped_calls,
                    "lines": record.lines,
                }
                for record in self.tests.values()
            ],
            "output_tail": self.output_tail,
        }


@dataclass(frozen=True)
class DriverLimits:
    """Caps on what one run records."""

    max_calls_per_test: int = 200
    max_calls_per_function: int = 40
    max_repr: int = 200


class ObservationRunner:
    """Observes versions of one target with the driver."""

    def __init__(
        self,
        target: EvidenceTarget,
        scratch_directory: Path,
        *,
        timeout_seconds: float,
        limits: DriverLimits | None = None,
    ) -> None:
        self._target = target
        self._scratch = scratch_directory
        self._timeout_seconds = timeout_seconds
        self._limits = limits or DriverLimits()
        self._driver_path = scratch_directory / DRIVER_FILE_NAME
        shutil.copyfile(Path(driver_module.__file__), self._driver_path)
        self._runs = 0

    def observe(
        self,
        version: FixVersion,
        focus_ids: list[str],
        *,
        label: str | None = None,
        record_calls: bool = True,
        watch: dict[str, list[int]] | None = None,
        suite: SuiteCommand | None = None,
        timeout_seconds: float | None = None,
    ) -> Observation:
        """Apply ``version``, run the suite (or ``suite``) under the driver, restore."""
        label = label or version.label
        timeout = timeout_seconds or self._timeout_seconds
        worktree = self._target.worktree
        try:
            with applied(version, worktree):
                sources = _read_sources(worktree, focus_ids)
                focus_entries = driver_focus_entries(focus_ids, sources)
                return self._run_driver(
                    label,
                    focus_entries,
                    record_calls,
                    watch,
                    suite or self._target.suite,
                    timeout,
                )
        except PatchApplyError as error:
            return Observation(label=label, status=NOT_APPLIED, detail=str(error))

    def _run_driver(
        self,
        label: str,
        focus_entries: list[dict],
        record_calls: bool,
        watch: dict[str, list[int]] | None,
        suite: SuiteCommand,
        timeout_seconds: float,
    ) -> Observation:
        self._runs += 1
        stem = f"{self._runs:03d}-{_slug(label)}"
        spec_path = self._scratch / f"{stem}.spec.json"
        output_path = self._scratch / f"{stem}.out.json"
        spec = {
            "runner": suite.runner,
            "arguments": list(suite.arguments),
            "focus": focus_entries,
            "watch": watch or {},
            "record_calls": record_calls,
            "max_calls_per_test": self._limits.max_calls_per_test,
            "max_calls_per_function": self._limits.max_calls_per_function,
            "max_repr": self._limits.max_repr,
            "output": self._target.runner_path(output_path),
        }
        spec_path.write_text(json.dumps(spec, indent=2), encoding="utf-8")
        command = self._target.python_command(self._driver_path, spec_path)
        started = time.monotonic()
        result = self._target.run(command, timeout_seconds)
        wall_seconds = time.monotonic() - started
        focus_ids = tuple(str(entry["id"]) for entry in focus_entries)
        tail = "\n".join(result.output.strip().splitlines()[-25:])
        if result.timed_out:
            return Observation(
                label=label,
                status=TIMED_OUT,
                detail=f"the suite did not finish within {timeout_seconds:.0f}s",
                focus_ids=focus_ids,
                output_tail=tail,
            )
        if not output_path.is_file():
            return Observation(
                label=label,
                status=NO_OUTPUT,
                detail="the driver wrote no output (the interpreter crashed or was killed)",
                focus_ids=focus_ids,
                output_tail=tail,
            )
        payload = json.loads(output_path.read_text(encoding="utf-8"))
        tests: dict[str, RecordedTest] = {}
        for test_payload in payload.get("tests") or []:
            record = RecordedTest.from_json(test_payload)
            tests[record.test_id] = record
        return Observation(
            wall_seconds=wall_seconds,
            label=label,
            status=OBSERVED,
            detail=(payload.get("error") or "").strip(),
            seconds=float(payload.get("seconds") or 0.0),
            python=str(payload.get("python") or ""),
            tests=tests,
            focus_ids=focus_ids,
            output_tail=tail,
        )


def _read_sources(worktree: Path, focus_ids: list[str]) -> dict[str, str]:
    sources: dict[str, str] = {}
    for identifier in focus_ids:
        relative_path = identifier.split("::", 1)[0]
        if relative_path in sources:
            continue
        path = worktree / relative_path
        sources[relative_path] = (
            path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
        )
    return sources


def _slug(label: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", label)[:60]
