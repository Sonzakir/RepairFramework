"""Case bundles: the on-disk format of a Break This Fix case.

A case lives in ``challenges/cases/<case-id>/``:

    case.json                         metadata, contract, provenance, expectations
    candidate.diff                    the agent's test-passing patch, verbatim
    witness/test_witness_<id>.py      the witness test (Type C only)
    NOTES.md                          why the expected behaviour is the right one

Bundles never contain BugsInPy files (BugsInPy has no licence yet, see
soarsmu/BugsInPy#94): ``case.json`` names the bug by project and id, and the bug
is checked out from BugsInPy at run time.

Case status is set by people, not by tooling: cases created by the framework are
``candidate``; only the maintainer sets ``accepted`` after a second person has
reproduced the case; ``disputed`` and ``withdrawn`` keep their history visible.
The tooling here reads statuses and appends replay-log entries, never promotes.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from apr_framework.core.exceptions import ConfigurationError
from apr_framework.core.models import BugIdentifier

CHALLENGES_CASES_DIR = Path("challenges") / "cases"
CASE_FILE_NAME = "case.json"
CASE_STATUSES = ("candidate", "accepted", "disputed", "withdrawn")
CASE_TYPES = ("C", "T")
# Variants every case runs. Type T adds the candidate exactly as submitted (its
# edited tests included), shown beside the original test's verdict.
VARIANTS_BY_TYPE = {
    "C": ("buggy", "candidate", "developer_fixed"),
    "T": ("buggy", "candidate", "candidate_as_submitted", "developer_fixed"),
}
OUTCOME_FIELDS = ("trigger", "regression", "witness")
_REQUIRED_FIELDS = (
    "id",
    "version",
    "status",
    "type",
    "project",
    "bug_id",
    "subject_license",
    "promise",
    "contract_basis",
    "recorded_test_scope",
    "candidate",
    "expected",
    "replay_log",
    "reviewers",
    "dispute_history",
)


@dataclass
class CaseBundle:
    """One case, as loaded from its directory. ``data`` is the raw ``case.json``."""

    directory: Path
    data: dict[str, Any]

    @property
    def case_id(self) -> str:
        return str(self.data["id"])

    @property
    def case_type(self) -> str:
        return str(self.data["type"])

    @property
    def status(self) -> str:
        return str(self.data["status"])

    @property
    def bug(self) -> BugIdentifier:
        return BugIdentifier(
            benchmark="bugsinpy",
            project=str(self.data["project"]),
            bug_id=int(self.data["bug_id"]),
        )

    @property
    def variants(self) -> tuple[str, ...]:
        return VARIANTS_BY_TYPE[self.case_type]

    @property
    def candidate_diff_path(self) -> Path:
        return self.directory / self.data["candidate"]["diff"]

    @property
    def witness_path(self) -> Path | None:
        witness = self.data.get("witness")
        if not witness:
            return None
        return self.directory / witness["path"]

    def read_candidate_diff(self) -> str:
        return self.candidate_diff_path.read_text(encoding="utf-8")

    def expected_outcome(self, variant: str) -> dict[str, str | None]:
        """Expected ``trigger`` / ``regression`` / ``witness`` for one variant.

        A field that is missing or ``null`` is not checked.
        """
        variant_expectation = self.data["expected"].get(variant, {})
        return {name: variant_expectation.get(name) for name in OUTCOME_FIELDS}


def load_case(case_id: str, project_root: Path) -> CaseBundle:
    """Load and validate ``challenges/cases/<case_id>/case.json``."""
    case_directory = project_root / CHALLENGES_CASES_DIR / case_id
    case_file = case_directory / CASE_FILE_NAME
    if not case_file.is_file():
        known_case_ids = ", ".join(
            bundle.case_id for bundle in list_cases(project_root)
        )
        raise ConfigurationError(
            f"No case {case_id!r} at {case_file}. Known cases: {known_case_ids or 'none'}."
        )
    bundle = CaseBundle(
        directory=case_directory,
        data=json.loads(case_file.read_text(encoding="utf-8")),
    )
    problems = find_case_problems(bundle)
    if problems:
        raise ConfigurationError(
            f"Case {case_id!r} is malformed: " + "; ".join(problems)
        )
    return bundle


def list_cases(project_root: Path) -> list[CaseBundle]:
    """Return every case under ``challenges/cases``, sorted by id."""
    cases_directory = project_root / CHALLENGES_CASES_DIR
    if not cases_directory.is_dir():
        return []
    return [
        CaseBundle(
            directory=case_file.parent,
            data=json.loads(case_file.read_text(encoding="utf-8")),
        )
        for case_file in sorted(cases_directory.glob(f"*/{CASE_FILE_NAME}"))
    ]


def find_case_problems(bundle: CaseBundle) -> list[str]:
    """Return human-readable problems with a case (empty when well formed)."""
    data = bundle.data
    problems = [
        f"missing field {field_name!r}"
        for field_name in _REQUIRED_FIELDS
        if field_name not in data
    ]
    if problems:
        return problems
    if data["id"] != bundle.directory.name:
        problems.append(f"id {data['id']!r} differs from directory name")
    if data["status"] not in CASE_STATUSES:
        problems.append(f"status must be one of {CASE_STATUSES}")
    if data["type"] not in CASE_TYPES:
        problems.append(f"type must be one of {CASE_TYPES}")
        return problems
    if not bundle.candidate_diff_path.is_file():
        problems.append(f"candidate diff {bundle.candidate_diff_path.name} is missing")
    if data["type"] == "C" and (
        bundle.witness_path is None or not bundle.witness_path.is_file()
    ):
        problems.append("a Type C case needs witness/test_witness_<case-id>.py")
    if not (bundle.directory / "NOTES.md").is_file():
        problems.append("NOTES.md (the justification) is missing")
    missing_variants = [
        variant for variant in bundle.variants if variant not in data["expected"]
    ]
    if missing_variants:
        problems.append(f"expected results missing for {missing_variants}")
    return problems


def append_replay_log_entry(bundle: CaseBundle, entry: dict[str, Any]) -> None:
    """Append one replay summary to ``case.json`` (the status is never changed)."""
    bundle.data.setdefault("replay_log", []).append(entry)
    (bundle.directory / CASE_FILE_NAME).write_text(
        json.dumps(bundle.data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
