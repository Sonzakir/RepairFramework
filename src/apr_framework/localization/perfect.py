"""Perfect (oracle) fault localization for the FL-guided repair baseline (T-3).

Two FL conditions feeding the repair algorithm:

  * **Automated FL** — the integrated FauxPy localizers (SBFL/MBFL/hybrid).
        Selected with ``repair --fl-mode auto --fl-family ...``.
  * **Perfect FL** — the *ground-truth* fault location from BugsInPy, bypassing any
    localizer.
        This module implements that "upper bound" condition.

The developer fix is stored as a unified diff at
``projects/<project>/bugs/<id>/bug_patch.txt`` (exposed by
``BugsInPyAdapter.get_reference_patch``). The lines the developer changed on the
**buggy side** of that diff *are* the perfect fault location. We parse the hunk
headers and bodies to recover those buggy-side line numbers, wrap them in
``RankedLocation`` objects, and return a ``LocalizationResult`` — exactly the type
the repair algorithm already consumes. So perfect FL is just a different *source* of
suspicious locations; the repair/validation pipeline is untouched.


"""

import re

from apr_framework.benchmarks.base import BenchmarkAdapter
from apr_framework.core.exceptions import ConfigurationError
from apr_framework.core.models import (
    BugIdentifier,
    CheckoutResult,
    LocalizationResult,
    RankedLocation,
    TestRunResult,
)
from apr_framework.localization.base import FaultLocalizer

# Matches a unified-diff hunk header, capturing the OLD (buggy) side start/count:
#   @@ -<old_start>[,<old_count>] +<new_start>[,<new_count>] @@
_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@")


def derive_oracle_locations(reference_diff: str | None) -> list[RankedLocation]:
    """Parse a developer-fix unified diff into buggy-side ranked locations.

    Walks each hunk tracking the *old/buggy* line number. ``-`` lines are buggy
    lines the developer removed or replaced and become locations directly; ``+``
    lines (pure insertions, no matching ``-``) are anchored to the buggy line at the
    insertion point. Locations are de-duplicated per (file, line), ranked in
    first-seen order, and given descending synthetic scores so ordering is
    deterministic.

    File paths are taken from the ``+++ b/<path>`` header with the ``b/`` prefix
    stripped, i.e. worktree-relative — the same form FauxPy emits, so
    ``TemplateRepairAlgorithm._resolve_source_path`` resolves them unchanged.

    Args:
        reference_diff: The ``bug_patch.txt`` unified diff, or None.

    Returns:
        Ranked oracle locations (possibly empty for an absent/empty diff).
    """
    if not reference_diff:
        return []

    ordered: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()
    current_file: str | None = None
    old_line = 0
    # True while inside a contiguous change run that already removed a buggy line.
    # Lets us treat a `+` after a `-` as part of a *replacement* (already captured by
    # the `-` line) rather than a fresh insertion. Reset on every context line.
    run_has_minus = False

    def _emit(file_path: str, line: int) -> None:
        key = (file_path, line)
        if key in seen:
            return
        seen.add(key)
        ordered.append(key)

    for raw in reference_diff.splitlines():
        if raw.startswith("+++ "):
            path = raw[4:].strip().split("\t", 1)[0].split(" ", 1)[0]
            current_file = path[2:] if path.startswith(("a/", "b/")) else path
            continue
        if (
            raw.startswith("--- ")
            or raw.startswith("diff ")
            or raw.startswith("index ")
        ):
            continue

        header = _HUNK_HEADER.match(raw)
        if header is not None:
            old_line = int(header.group(1))
            run_has_minus = False
            continue

        if current_file is None:
            continue

        if raw.startswith("-"):
            _emit(current_file, old_line)
            old_line += 1
            run_has_minus = True
        elif raw.startswith("+"):
            # A `+` after a `-` is part of a replacement already captured by the `-`
            # line. A `+` with no preceding `-` in this run is a pure insertion —
            # anchor it to the buggy line at the insertion point.
            if not run_has_minus:
                _emit(current_file, old_line)
        elif raw.startswith("\\"):
            # "\ No newline at end of file" meta-marker — not a source line, so
            # old_line must NOT advance and run_has_minus must NOT be cleared.
            pass
        else:
            # Context line — advance the buggy-side counter and end any
            # in-progress change run.
            old_line += 1
            run_has_minus = False

    # Build frozen RankedLocations in first-seen order, with descending synthetic
    # scores (1.0, 0.99, ...) so the ranking is deterministic.
    return [
        RankedLocation(
            rank=index + 1,
            file_path=file_path,
            location=f"{file_path}:{line}",
            score=max(0.0, 1.0 - index * 0.01),
            line=line,
        )
        for index, (file_path, line) in enumerate(ordered)
    ]


class PerfectFaultLocalizer(FaultLocalizer):
    """Oracle localizer that returns the developer-fix lines as the fault location.

    Constructed with the benchmark adapter so it can fetch the ground-truth fix via
    ``get_reference_patch``. It runs no analysis and needs no test evidence — it is
    the perfect-FL upper bound for Task 3.
    """

    def __init__(self, adapter: BenchmarkAdapter) -> None:
        self._adapter = adapter

    @property
    def name(self) -> str:
        return "perfect-fl"

    def localize(
        self,
        bug: BugIdentifier,
        checkout: CheckoutResult,
        test_result: TestRunResult | None = None,
    ) -> LocalizationResult:
        """Return ranked locations taken from the BugsInPy developer fix.

        Args:
            bug: Bug identifier.
            checkout: Checked-out worktree (unused — the oracle needs no analysis).
            test_result: Accepted for ABC compatibility; ignored.

        Returns:
            A LocalizationResult whose ranked_locations are the buggy-side lines of
            the developer fix, with ``backend="perfect-fl"``.

        Raises:
            ConfigurationError: If the bug has no ``bug_patch.txt`` reference fix, or
                the fix yields no parseable changed lines.
        """
        getter = getattr(self._adapter, "get_reference_patch", None)
        reference_diff = getter(bug) if getter is not None else None
        if not reference_diff:
            raise ConfigurationError(
                f"Perfect FL needs the developer fix, but no bug_patch.txt was found "
                f"for {bug.project}#{bug.bug_id}. Use --fl-mode auto instead."
            )

        locations = derive_oracle_locations(reference_diff)
        if not locations:
            raise ConfigurationError(
                f"Perfect FL could not derive any fault locations from the developer "
                f"fix for {bug.project}#{bug.bug_id} (empty or unparseable diff)."
            )

        return LocalizationResult(
            bug=bug,
            backend="perfect-fl",
            ranked_locations=locations,
            metadata={
                "fl_mode": "perfect",
                "score_formula": "oracle: developer-fix lines from bug_patch.txt",
                "location_count": len(locations),
            },
        )
