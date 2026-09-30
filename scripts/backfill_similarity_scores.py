"""Score context similarity for every candidate patch in the committed artifacts.

``RepairEvaluationRunner`` originally scored context similarity only for *plausible*
patches. An approach whose candidates all failed the test suite therefore carried no
similarity score at all, and its column in the course-wide report read ``—`` — which
looks like "not measured" and makes the approach incomparable to one that got a patch
past the tests. Grading the near-misses is precisely what a graded metric is for.

The runner now scores every candidate, but the committed artifacts predate that. This
script backfills them **without re-running the matrix or spending any API budget**: each
candidate's ``diff_text`` is a unified diff against the original file, so applying it
reconstructs the ``patched_source`` that ``context_similarity_score`` needs, and the
score is then computed by the same function the live path calls.

    python scripts/backfill_similarity_scores.py [run_artifacts_dir]

Rewrites ``repair_results.json`` in place for every run directory found.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from apr_framework.core.models import BugIdentifier, PatchCandidate  # noqa: E402
from apr_framework.repair.correctness import (  # noqa: E402
    context_similarity_score,
    describe_similarity_score,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ARTIFACTS_DIR = (
    PROJECT_ROOT / "experiment_results/course_comparison/run_artifacts"
)

# The runs recorded paths as seen inside the container (/workspace/...); the same tree
# is the repository root on the host.
CONTAINER_PROJECT_ROOT = "/workspace/"

_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def resolve_source_path_on_host(source_path_str: str) -> Path:
    """Map a container-recorded source path onto this checkout."""
    if source_path_str.startswith(CONTAINER_PROJECT_ROOT):
        return PROJECT_ROOT / source_path_str[len(CONTAINER_PROJECT_ROOT) :]
    return Path(source_path_str)


def apply_unified_diff(original_source: str, diff_text: str) -> str | None:
    """Reconstruct the patched source by applying *diff_text* to *original_source*.

    The diff was produced by difflib against this exact file, so hunks are applied by
    line number. Returns None if any hunk's context does not line up — a mismatch means
    the reconstruction would be wrong, and a wrong score is worse than no score.
    """
    original_lines = original_source.splitlines(keepends=True)
    patched_lines: list[str] = []
    next_original_index = 0  # 0-based cursor into original_lines

    diff_lines = diff_text.splitlines()
    line_index = 0
    while line_index < len(diff_lines):
        diff_line = diff_lines[line_index]
        hunk_match = _HUNK_HEADER.match(diff_line)
        if not hunk_match:
            line_index += 1
            continue

        hunk_start = int(hunk_match.group(1))
        # difflib emits a 0 start for an empty original range; both are 1-based otherwise.
        hunk_start_index = max(hunk_start - 1, 0)
        if hunk_start_index < next_original_index:
            return None
        patched_lines.extend(original_lines[next_original_index:hunk_start_index])
        next_original_index = hunk_start_index

        line_index += 1
        while line_index < len(diff_lines) and not _HUNK_HEADER.match(
            diff_lines[line_index]
        ):
            body_line = diff_lines[line_index]
            if body_line.startswith("+"):
                patched_lines.append(body_line[1:] + "\n")
            elif body_line.startswith("-") or body_line.startswith(" "):
                if next_original_index >= len(original_lines):
                    return None
                original_line = original_lines[next_original_index]
                if original_line.rstrip("\n") != body_line[1:].rstrip("\n"):
                    return None  # context mismatch: refuse to guess
                next_original_index += 1
                if body_line.startswith(" "):
                    patched_lines.append(original_line)
            elif body_line.startswith("\\"):
                pass  # "\ No newline at end of file"
            line_index += 1

    patched_lines.extend(original_lines[next_original_index:])
    return "".join(patched_lines)


def read_reference_diff(project: str, bug_id: int) -> str | None:
    bug_patch_path = (
        PROJECT_ROOT
        / ".tools/bugsinpy/projects"
        / project
        / "bugs"
        / str(bug_id)
        / "bug_patch.txt"
    )
    if not bug_patch_path.exists():
        return None
    return bug_patch_path.read_text(encoding="utf-8")


def score_patch_payload(
    patch_payload: dict, reference_diff_text: str, bug: BugIdentifier
) -> tuple[float, str] | None:
    """Recompute one candidate's similarity score from its serialized diff."""
    metadata = patch_payload.get("metadata") or {}
    source_path_str = metadata.get("source_path")
    diff_text = patch_payload.get("diff_text")
    if not source_path_str or not diff_text:
        return None

    source_path = resolve_source_path_on_host(source_path_str)
    try:
        original_source = source_path.read_text(encoding="utf-8")
    except OSError:
        return None

    patched_source = apply_unified_diff(original_source, diff_text)
    if patched_source is None:
        return None

    candidate = PatchCandidate(
        bug=bug,
        patch_id=patch_payload.get("patch_id") or "backfilled",
        summary=patch_payload.get("summary") or "",
        diff_text=diff_text,
        metadata={
            **metadata,
            "source_path": str(source_path),
            "patched_source": patched_source,
        },
    )
    similarity_score = context_similarity_score(candidate, reference_diff_text)
    return similarity_score, describe_similarity_score(similarity_score)


def backfill_run(run_dir: Path) -> tuple[int, int]:
    """Score every unscored candidate in one run. Returns (scored, unscorable)."""
    repair_results_path = run_dir / "repair_results.json"
    config_path = run_dir / "config.json"
    if not repair_results_path.exists() or not config_path.exists():
        return (0, 0)

    config = json.loads(config_path.read_text(encoding="utf-8"))
    reference_diff_text = read_reference_diff(config["project"], config["bug_id"])
    if not reference_diff_text:
        return (0, 0)

    bug = BugIdentifier(
        benchmark="bugsinpy", project=config["project"], bug_id=config["bug_id"]
    )
    payload = json.loads(repair_results_path.read_text(encoding="utf-8"))

    # The same patch appears in several lists (all_results, plausible_patches,
    # ranked_plausible_patches, assessed_plausible_patches). Score once per patch_id,
    # then write that score into every list so the report reads a consistent number.
    score_by_patch_id: dict[str, tuple[float, str]] = {}
    scored_count = 0
    unscorable_count = 0

    for patch_payload in payload.get("all_results") or []:
        patch_id = patch_payload.get("patch_id")
        if patch_payload.get("context_similarity_score") is not None:
            if patch_id:
                score_by_patch_id[patch_id] = (
                    patch_payload["context_similarity_score"],
                    patch_payload.get("similarity_band"),
                )
            continue
        scored = score_patch_payload(patch_payload, reference_diff_text, bug)
        if scored is None:
            unscorable_count += 1
            continue
        patch_payload["context_similarity_score"], patch_payload["similarity_band"] = (
            scored
        )
        if patch_id:
            score_by_patch_id[patch_id] = scored
        scored_count += 1

    for list_name in (
        "plausible_patches",
        "ranked_plausible_patches",
        "assessed_plausible_patches",
    ):
        for patch_payload in payload.get(list_name) or []:
            patch_id = patch_payload.get("patch_id")
            if (
                patch_id in score_by_patch_id
                and patch_payload.get("context_similarity_score") is None
            ):
                (
                    patch_payload["context_similarity_score"],
                    patch_payload["similarity_band"],
                ) = score_by_patch_id[patch_id]

    repair_results_path.write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8"
    )
    return (scored_count, unscorable_count)


def main() -> None:
    artifacts_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_ARTIFACTS_DIR
    run_dirs = sorted(artifacts_dir.glob("run_*"))
    total_scored = 0
    total_unscorable = 0
    for run_dir in run_dirs:
        scored_count, unscorable_count = backfill_run(run_dir)
        total_scored += scored_count
        total_unscorable += unscorable_count
        if scored_count or unscorable_count:
            print(
                f"  {run_dir.name}: scored {scored_count}, unscorable {unscorable_count}"
            )
    print(
        f"\nBackfilled {total_scored} candidate patch(es) across {len(run_dirs)} run(s); "
        f"{total_unscorable} could not be reconstructed from their diff."
    )


if __name__ == "__main__":
    main()
