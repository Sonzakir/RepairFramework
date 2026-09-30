"""Generate PatchCandidate objects by applying AST mutation operators to a source file.

Note: ast.unparse() reformats the entire file (normalises whitespace, removes
redundant parens, etc.).  The unified diff therefore includes cosmetic changes
beyond the actual mutation.  This is expected behaviour — the patched_source
stored in metadata is what actually gets applied to the file.
"""

import ast
import difflib
import sys
from pathlib import Path

from apr_framework.core.exceptions import ConfigurationError
from apr_framework.core.models import BugIdentifier, PatchCandidate, RankedLocation
from apr_framework.repair.template.operators import get_operator_class

if sys.version_info < (3, 9):
    raise ConfigurationError(
        "Template-based repair requires Python 3.9+ for ast.unparse() support. "
        f"Current version: {sys.version_info.major}.{sys.version_info.minor}"
    )


def generate_patches(
    source_path: Path,
    target_line: int,
    operators: list[str],
    location: RankedLocation,
    bug: BugIdentifier,
) -> list[PatchCandidate]:
    """Generate PatchCandidate objects for a single suspicious location.

    For each enabled operator × each AST variant:
      1. Parse the original source.
      2. Apply the operator transformer to get a mutated AST.
      3. Unparse the mutated AST to produce patched source text.
      4. Compute a unified diff for display / reporting.
      5. Wrap in a PatchCandidate with operator key and score in metadata.

    Args:
        source_path:  Absolute path to the source file to mutate.
        target_line:  Line number at which mutations should be applied.
        operators:    List of operator keys to try (e.g. ["arith", "comp"]).
        location:     The ranked location that selected this file/line.
        bug:          Bug identifier propagated into each PatchCandidate.

    Returns:
        List of PatchCandidate objects (may be empty if no operator matched).
    """
    try:
        original_text = source_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigurationError(
            f"Cannot read source file {source_path}: {exc}"
        ) from exc

    try:
        original_tree = ast.parse(original_text, filename=str(source_path))
    except SyntaxError:
        # If the file is not valid Python, skip silently.
        return []

    original_lines = original_text.splitlines(keepends=True)
    candidates: list[PatchCandidate] = []

    for op_key in operators:
        operator_cls = get_operator_class(op_key)
        operator = operator_cls(target_line=target_line)

        try:
            variants = operator.generate_variants(original_tree)
        except Exception:  # noqa: BLE001 — operator safety: never crash the loop
            continue

        for variant_idx, mutated_tree in enumerate(variants):
            try:
                ast.fix_missing_locations(mutated_tree)
                patched_text = ast.unparse(mutated_tree)
            except Exception:  # noqa: BLE001
                continue

            # ast.unparse() never emits a trailing newline. Normalise the patched
            # text to end with one *before* splitting so that every line kept by
            # splitlines(keepends=True) carries its own "\n". Appending "\n" as a
            # separate list element (the previous approach) produced corrupted
            # diffs like "+    return x - 1+" because the mutated last line had no
            # terminator and the lone "\n" was glued onto it by "".join(...).
            normalized_patched = (
                patched_text if patched_text.endswith("\n") else patched_text + "\n"
            )
            patched_lines = normalized_patched.splitlines(keepends=True)

            diff_lines = list(
                difflib.unified_diff(
                    original_lines,
                    patched_lines,
                    fromfile=f"a/{source_path.name}",
                    tofile=f"b/{source_path.name}",
                )
            )
            diff_text = "".join(diff_lines)

            if not diff_text.strip():
                # Operator produced no visible change — skip.
                continue

            patch_id = (
                f"{bug.project}-{bug.bug_id}-{op_key}-L{target_line}-v{variant_idx}"
            )
            summary = f"{op_key} mutation at {source_path.name}:{target_line} (variant {variant_idx})"

            candidates.append(
                PatchCandidate(
                    bug=bug,
                    patch_id=patch_id,
                    summary=summary,
                    diff_text=diff_text,
                    metadata={
                        "operator": op_key,
                        "source_path": str(source_path),
                        "target_line": target_line,
                        "variant_index": variant_idx,
                        "suspiciousness_score": location.score,
                        "location_rank": location.rank,
                        # Store the full patched source to avoid re-parsing in the validator.
                        "patched_source": normalized_patched,
                    },
                )
            )

    return candidates
