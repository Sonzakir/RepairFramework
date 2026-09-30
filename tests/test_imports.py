import importlib

import pytest

MODULES = [
    "apr_framework",
    "apr_framework.__main__",
    "apr_framework.benchmarks",
    "apr_framework.benchmarks.base",
    "apr_framework.core",
    "apr_framework.core.exceptions",
    "apr_framework.core.models",
    "apr_framework.evaluation",
    "apr_framework.evaluation.base",
    "apr_framework.evaluation.course_approaches",
    "apr_framework.evaluation.course_comparison_runner",
    "apr_framework.evaluation.dummy_runner",
    "apr_framework.evaluation.repair_runner",
    "apr_framework.localization",
    "apr_framework.localization.base",
    "apr_framework.localization.hybrid",
    "apr_framework.localization.llm",
    "apr_framework.localization.perfect",
    "apr_framework.repair",
    "apr_framework.repair.base",
    "apr_framework.repair.dummy",
    "apr_framework.repair.assessment",
    "apr_framework.repair.assessment.base",
    "apr_framework.repair.assessment.config",
    "apr_framework.repair.assessment.llm",
    "apr_framework.repair.assessment.response_parser",
    "apr_framework.repair.correctness",
    "apr_framework.repair.regression",
    "apr_framework.repair.run_loop",
    "apr_framework.repair.llm.retrieval_loop",
    "apr_framework.repair.llm.retrieval_protocol",
    "apr_framework.repair.llm.retrieval_tools",
    "apr_framework.repair.template.algorithm",
    "apr_framework.repair.template.config",
    "apr_framework.repair.template.operators",
    "apr_framework.repair.template.patch_generator",
    "apr_framework.repair.template.validator",
    "apr_framework.reporting",
    "apr_framework.reporting.base",
    "apr_framework.reporting.archive",
]


@pytest.mark.parametrize("module_name", MODULES)
def test_public_modules_import(module_name) -> None:
    importlib.import_module(module_name)
