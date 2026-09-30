"""The repair approaches compared side by side across the whole course.

Each approach is one column of the course-wide comparison: the traditional
template technique, the two single-LLM repair variants, and the fully LLM-driven
pipeline (LLM fault localization -> LLM repair with context retrieval -> LLM
patch assessment).

Keeping the approaches as *data* rather than branches means the comparison runner
iterates over a list and the CLI validates against the same list — adding a
column later is one entry here, not a new branch in three places.
"""

from __future__ import annotations

from dataclasses import dataclass

TEMPLATE_APPROACH_LABEL = "a3-template"
SINGLE_SHOT_APPROACH_LABEL = "a4-single-shot"
ITERATIVE_APPROACH_LABEL = "a4-iterative"
FULL_LLM_APPROACH_LABEL = "a5-full-llm"


@dataclass(frozen=True)
class CourseApproach:
    """One column of the course-wide comparison.

    Attributes:
        label: Stable identifier used on the CLI, in results.json, and as the
            report's column name.
        column_title: Human-readable column header in the comparison table.
        technique: Repair backend — ``template`` or ``llm``.
        uses_llm_fault_localization: When True the approach localizes with the
            LLM backend and ignores the ``--fl-modes`` axis entirely (it has
            exactly one FL source). When False it is run once per requested FL
            mode (auto / perfect).
        context_enrichment: LLM repair toggle — inject the failing test's source
            and traceback into the prompt. Ignored by the template technique.
        iterative: LLM repair toggle — run the multi-turn test-failure feedback
            loop instead of one-shot sampling. Ignored by the template technique.
        retrieval_budget: Maximum codebase-retrieval steps the model may take
            before proposing a patch; ``0`` disables retrieval. Ignored by the
            template technique.
        repair_description: What the table's "Repair" row says for this column.
    """

    label: str
    column_title: str
    technique: str
    uses_llm_fault_localization: bool
    context_enrichment: bool
    iterative: bool
    retrieval_budget: int
    repair_description: str

    @property
    def is_llm_technique(self) -> bool:
        return self.technique == "llm"

    @property
    def fault_localization_description(self) -> str:
        """What the table's "FL source" row says for this column."""
        return "LLM-FL" if self.uses_llm_fault_localization else "auto/perfect"


# The retrieval budget of the full-LLM approach is a CLI knob, so the spec below
# carries a placeholder that the CLI overrides via `with_retrieval_budget`.
COURSE_APPROACHES: tuple[CourseApproach, ...] = (
    CourseApproach(
        label=TEMPLATE_APPROACH_LABEL,
        column_title="A3 (template)",
        technique="template",
        uses_llm_fault_localization=False,
        context_enrichment=False,
        iterative=False,
        retrieval_budget=0,
        repair_description="traditional",
    ),
    CourseApproach(
        label=SINGLE_SHOT_APPROACH_LABEL,
        column_title="A4 simple",
        technique="llm",
        uses_llm_fault_localization=False,
        context_enrichment=False,
        iterative=False,
        retrieval_budget=0,
        repair_description="LLM",
    ),
    CourseApproach(
        label=ITERATIVE_APPROACH_LABEL,
        column_title="A4 iterative",
        technique="llm",
        uses_llm_fault_localization=False,
        context_enrichment=False,
        iterative=True,
        retrieval_budget=0,
        repair_description="LLM (iterative)",
    ),
    CourseApproach(
        label=FULL_LLM_APPROACH_LABEL,
        column_title="A5 full LLM",
        technique="llm",
        uses_llm_fault_localization=True,
        context_enrichment=True,
        iterative=False,
        retrieval_budget=3,
        repair_description="LLM + retrieval",
    ),
)

COURSE_APPROACHES_BY_LABEL: dict[str, CourseApproach] = {
    approach.label: approach for approach in COURSE_APPROACHES
}


def resolve_course_approaches(
    labels: list[str], retrieval_budget: int
) -> list[CourseApproach]:
    """Look up the requested approach labels and apply the CLI retrieval budget.

    Args:
        labels: Approach labels in the order the caller wants them compared.
        retrieval_budget: Retrieval budget applied to approaches that use
            retrieval (currently only the full-LLM pipeline); approaches with a
            zero budget in their spec stay retrieval-free.

    Raises:
        ConfigurationError: If any label is not a known approach.
    """
    from apr_framework.core.exceptions import ConfigurationError

    resolved_approaches: list[CourseApproach] = []
    for label in labels:
        approach = COURSE_APPROACHES_BY_LABEL.get(label)
        if approach is None:
            known_labels = ", ".join(COURSE_APPROACHES_BY_LABEL)
            raise ConfigurationError(
                f"Unknown approach {label!r}. Known approaches: {known_labels}"
            )
        if approach.retrieval_budget > 0:
            approach = replace_retrieval_budget(approach, retrieval_budget)
        resolved_approaches.append(approach)
    return resolved_approaches


def replace_retrieval_budget(
    approach: CourseApproach, retrieval_budget: int
) -> CourseApproach:
    """Return a copy of *approach* with its retrieval budget overridden."""
    from dataclasses import replace

    return replace(approach, retrieval_budget=retrieval_budget)
