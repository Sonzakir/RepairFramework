"""Load Strict Repro's prompt templates (package data under ``repro/prompts``).

Templates use ``$name`` placeholders (``string.Template``), so issue text and
code containing braces are inserted verbatim. Every template a run uses is
recorded with its SHA-256 in the run directory.
"""

from importlib import resources
from string import Template

from apr_framework.repro.models import sha256_text

PROMPT_NAMES = (
    "generate_system",
    "generate_user",
    "feedback_user",
    "assertflip_system",
    "assertflip_pass_user",
    "assertflip_invert_user",
    "judge_system",
    "judge_user",
    "probe_system",
    "probe_user",
    "harden_system",
    "harden_user",
)


def load_prompt(name: str) -> str:
    return (
        resources.files("apr_framework.repro")
        .joinpath(f"prompts/{name}.txt")
        .read_text(encoding="utf-8")
    )


def render_prompt(name: str, **values: object) -> str:
    """Fill a template; a missing value raises, so no placeholder is sent."""
    return Template(load_prompt(name)).substitute(
        {key: str(value) for key, value in values.items()}
    )


def prompt_hashes() -> dict[str, str]:
    """SHA-256 of every template, as shipped."""
    return {name: sha256_text(load_prompt(name)) for name in PROMPT_NAMES}
