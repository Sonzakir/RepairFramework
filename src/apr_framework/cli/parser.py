"""
To parse Command Line Arguments
Define here:
    What top-level commands exits
    What nested subcommands exits
    What extra arguments each command needs
"""

import argparse


def build_parser() -> argparse.ArgumentParser:
    """
    Creates and configures the CLI grammar of the framework
    Returns:
        argparse.ArgumentParser:
    """
    parser = argparse.ArgumentParser(prog="apr_framework")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list-benchmarks")

    ## configure — interactively store an LLM API key in the local .env file
    configure_parser = subparsers.add_parser(
        "configure",
        help="Interactively store an LLM API key in the local .env file.",
    )
    configure_parser.add_argument(
        "--llm-api-key-env",
        dest="llm_api_key_env",
        type=str,
        default="GPT_AT_RUB_API_KEY",
        help="Environment variable name to store the key under (default: GPT_AT_RUB_API_KEY)",
    )

    # FauxPy CLI commands
    localize_parser = subparsers.add_parser("localize")
    localize_parser.add_argument(
        "--backend", choices=["fauxpy", "llm"], default="fauxpy"
    )
    localize_parser.add_argument("--project", required=True)
    localize_parser.add_argument("--bug", type=int, required=True)
    localize_parser.add_argument("--src", default=None)
    localize_parser.add_argument(
        "--family", type=str, choices=["sbfl", "mbfl", "hybrid"], default="sbfl"
    )
    localize_parser.add_argument("--mbfl", action="store_true")
    localize_parser.add_argument(
        "--granularity",
        type=str,
        default="statement",
        choices=["statement", "function"],
    )
    localize_parser.add_argument("--failing_tests", type=str, default=None)
    localize_parser.add_argument("--test-target", action="append", default=None)
    localize_parser.add_argument("--top-n", type=int, default=None)
    localize_parser.add_argument("--show-raw-output", action="store_true")
    localize_parser.add_argument(
        "--mutation-strategy",
        "--mutation_strategy",
        dest="mutation_strategy",
        type=str,
        default=None,
    )
    localize_parser.add_argument(
        "--budget",
        "--mutation-budget",
        "--mutation_budget",
        dest="mutation_budget",
        type=int,
        default=None,
    )
    localize_parser.add_argument("--seed", type=int, default=0)
    localize_parser.add_argument("--metric", type=str, default=None)
    localize_parser.add_argument("--sbfl-metric", type=str, default="ochiai")
    localize_parser.add_argument("--mbfl-metric", type=str, default="metallaxis")
    localize_parser.add_argument("--sbfl-weight", type=float, default=0.5)
    localize_parser.add_argument("--mbfl-weight", type=float, default=0.5)
    localize_parser.add_argument(
        "--wsbi-alpha",
        dest="wsbi_alpha",
        type=float,
        default=0.5,
        help=(
            "Passing-test weight for the WSBI metric: score = ef / (ef + alpha * ep). "
            "Must be > 0. alpha=1 reduces to plain SBI; alpha=0.5 (default) halves "
            "the influence of passing-test coverage."
        ),
    )
    localize_parser.add_argument("--runs-dir", default="runs")

    # LLM fault-localization backend (--backend llm). Ignored for --backend fauxpy.
    # GPT@RUB vs OpenAI is selected purely by --llm-base-url / --llm-api-key-env / --model.
    localize_parser.add_argument(
        "--model",
        type=str,
        default="gpt-4.1-2025-04-14",
        help="LLM model name for --backend llm (default: gpt-4.1-2025-04-14)",
    )
    localize_parser.add_argument(
        "--temperature",
        type=float,
        default=0.0,
        help=(
            "LLM sampling temperature in [0.0, 2.0] for --backend llm (default: 0.0 "
            "for stable rankings; some models such as gpt-5.* require 1.0)"
        ),
    )
    localize_parser.add_argument(
        "--llm-provider",
        dest="llm_provider",
        type=str,
        default="openai-compatible",
        help="LLM client implementation for --backend llm (default: openai-compatible)",
    )
    localize_parser.add_argument(
        "--llm-base-url",
        dest="llm_base_url",
        type=str,
        default=None,
        help="Override the LLM API endpoint URL for --backend llm (default: GPT@RUB)",
    )
    localize_parser.add_argument(
        "--llm-api-key-env",
        dest="llm_api_key_env",
        type=str,
        default="GPT_AT_RUB_API_KEY",
        help=(
            "Environment variable holding the LLM API key for --backend llm "
            "(default: GPT_AT_RUB_API_KEY)"
        ),
    )
    localize_parser.add_argument(
        "--fl-system-prompt",
        dest="fl_system_prompt",
        type=str,
        default="fl_prompt1",
        help=(
            "Name (file stem) of the system prompt under localization/prompts/ used "
            "for --backend llm (default: fl_prompt1)"
        ),
    )
    localize_parser.add_argument(
        "--max-source-lines",
        dest="max_source_lines",
        type=int,
        default=400,
        help=(
            "Upper bound on numbered source lines shown to the model for --backend "
            "llm (default: 400)"
        ),
    )
    localize_parser.add_argument(
        "--source-window",
        dest="source_window",
        type=int,
        default=40,
        help=(
            "Lines of context above/below each traceback line shown to the model "
            "for --backend llm (default: 40)"
        ),
    )

    ### BugsInPy CLI commands
    bugsinpy_parser = subparsers.add_parser("bugsinpy")
    bugsinpy_subparsers = bugsinpy_parser.add_subparsers(
        dest="bugsinpy_command", required=True
    )

    ## bugsinpy-setip
    bugsinpy_subparsers.add_parser("setup")
    bugsinpy_subparsers.add_parser("list-projects")

    ## bugsinpy-list-bugs
    list_bugs_parser = bugsinpy_subparsers.add_parser("list-bugs")
    list_bugs_parser.add_argument("project")

    ## bugsinpy-checkout
    checkout_parser = bugsinpy_subparsers.add_parser("checkout")
    checkout_parser.add_argument("project")
    checkout_parser.add_argument("bug_id", type=int)

    ## bugsinpy-compile (safe compile)
    compile_parser = bugsinpy_subparsers.add_parser("compile")
    compile_parser.add_argument("project")
    compile_parser.add_argument("bug_id", type=int)

    ## bugsinpy-test
    test_parser = bugsinpy_subparsers.add_parser("test")
    test_parser.add_argument("project")
    test_parser.add_argument("bug_id", type=int)

    ## bugsinpy-evaluate-dummy
    evaluate_dummy_parser = bugsinpy_subparsers.add_parser("evaluate-dummy")
    evaluate_dummy_parser.add_argument("--seed", type=int, default=None)
    evaluate_dummy_parser.add_argument("--runs-dir", default="runs")

    ## bugsinpy-evaluate-localization
    eval_loc_parser = bugsinpy_subparsers.add_parser(
        "evaluate-localization",
        help="Run SBFL/MBFL/Hybrid on a set of bugs and compare rankings against ground truth.",
    )
    eval_loc_parser.add_argument(
        "--output-dir",
        default="experiment_results",
        help="Directory to write results.json and README.md (default: experiment_results).",
    )
    eval_loc_parser.add_argument(
        "--top-ks",
        default="1,5,10",
        help="Comma-separated Top-k values to report (default: 1,5,10).",
    )
    eval_loc_parser.add_argument(
        "--budget",
        type=int,
        default=50,
        help="MBFL mutation budget — max mutants per bug (default: 50).",
    )
    eval_loc_parser.add_argument(
        "--seed",
        type=int,
        default=0,
        help="Random seed for MBFL mutation selection (default: 0).",
    )
    eval_loc_parser.add_argument(
        "--granularity",
        choices=["statement", "function"],
        default="statement",
        help="Localization granularity (default: statement).",
    )
    eval_loc_parser.add_argument(
        "--bugs",
        default=None,
        help=(
            "Comma-separated list of project:bug_id pairs to evaluate "
            "(e.g. black:1,black:3,black:7). "
            "Defaults to black:1,black:3,black:7."
        ),
    )
    eval_loc_parser.add_argument(
        "--traditional-budget",
        dest="traditional_budget",
        type=int,
        default=200,
        help=(
            "Mutation budget for the MBFL baseline technique (default: 200). "
            "Stock FauxPy MBFL is exhaustive but impractical on large projects; "
            "this caps the baseline at a larger budget than --budget (the extension) "
            "so both complete in reasonable time while preserving the comparison."
        ),
    )

    ## bugsinpy-evaluate-repair (Task 5: repair pipeline comparison)
    eval_repair_parser = bugsinpy_subparsers.add_parser(
        "evaluate-repair",
        help=(
            "Run the full repair pipeline on a set of bugs under both automated FL "
            "and perfect FL, apply the ranker, and write an aggregated comparison "
            "(Assignment 3 Task 5)."
        ),
    )
    eval_repair_parser.add_argument(
        "--bugs",
        default=None,
        help=(
            "Comma-separated project:bug_id pairs to evaluate "
            "(e.g. tornado:14,scrapy:2,black:1). "
            "Defaults to tornado:14,scrapy:2,black:1."
        ),
    )
    eval_repair_parser.add_argument(
        "--fl-modes",
        dest="fl_modes",
        default="auto,perfect",
        help="Comma-separated FL modes to run per bug (default: auto,perfect).",
    )
    eval_repair_parser.add_argument(
        "--fl-family",
        dest="fl_family",
        choices=["sbfl", "mbfl", "hybrid"],
        default="sbfl",
        help="Fault-localization family used in automated FL mode (default: sbfl).",
    )
    eval_repair_parser.add_argument(
        "--localization-metric",
        dest="localization_metric",
        default="ochiai",
        help="SBFL metric used in automated FL mode (default: ochiai).",
    )
    eval_repair_parser.add_argument(
        "--mbfl-metric",
        dest="mbfl_metric",
        default="metallaxis",
        help="MBFL metric for automated FL mode (default: metallaxis).",
    )
    eval_repair_parser.add_argument(
        "--mutation-budget",
        dest="mutation_budget",
        type=int,
        default=50,
        help="Max mutants per bug for MBFL/hybrid automated FL (default: 50).",
    )
    eval_repair_parser.add_argument(
        "--seed", type=int, default=0, help="Random seed for MBFL mutation selection."
    )
    eval_repair_parser.add_argument(
        "--operators",
        default="arith,comp,obo,bool,negate,return",
        help="Comma-separated enabled mutation operators (default: all).",
    )
    eval_repair_parser.add_argument(
        "--budget",
        type=int,
        default=200,
        help="Max patch validations per cell (default: 200).",
    )
    eval_repair_parser.add_argument(
        "--top-n",
        dest="top_n",
        type=int,
        default=5,
        help="Top-N suspicious locations to attempt (default: 5).",
    )
    eval_repair_parser.add_argument(
        "--granularity",
        choices=["statement", "function"],
        default="statement",
        help="Localization granularity for automated FL (default: statement).",
    )
    eval_repair_parser.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="Seconds allowed per test-suite invocation (default: 120).",
    )
    eval_repair_parser.add_argument(
        "--stop-on-first",
        dest="stop_on_first",
        action="store_true",
        help="Stop validating a cell after the first plausible patch.",
    )
    eval_repair_parser.add_argument(
        "--no-regression-check",
        dest="regression_check",
        action="store_false",
        help="Skip the regression half of plausibility (faster).",
    )
    eval_repair_parser.set_defaults(regression_check=True)
    eval_repair_parser.add_argument(
        "--ranker",
        choices=["weighted", "none"],
        default="weighted",
        help="Patch ranking strategy applied to plausible patches (default: weighted).",
    )
    eval_repair_parser.add_argument(
        "--ranker-weights",
        dest="ranker_weights",
        default=None,
        help="Comma-separated weights for the weighted ranker (default: 0.6,0.25,0.15).",
    )
    eval_repair_parser.add_argument(
        "--output-dir",
        default="experiment_results/repair",
        help="Directory for results.json and README.md (default: experiment_results/repair).",
    )
    eval_repair_parser.add_argument(
        "--runs-dir",
        dest="runs_dir",
        default="runs",
        help="Directory for per-cell run artifacts (default: runs).",
    )

    ## bugsinpy-evaluate-llm-repair (LLM repair evaluation & comparison)
    eval_llm_parser = bugsinpy_subparsers.add_parser(
        "evaluate-llm-repair",
        help=(
            "Run the LLM repair pipeline on a set of bugs across the "
            "{single-shot, context-enriched, iterative} x {auto FL, perfect FL} "
            "matrix, and write an aggregated comparison report."
        ),
    )
    eval_llm_parser.add_argument(
        "--bugs",
        default="black:1,tornado:14,scrapy:2,fastapi:3",
        help=(
            "Comma-separated project:bug_id pairs to evaluate "
            "(default: black:1,tornado:14,scrapy:2,fastapi:3)."
        ),
    )
    eval_llm_parser.add_argument(
        "--variants",
        default="single-shot,context-enriched,iterative",
        help=(
            "Comma-separated LLM variants to run per bug. Choices: single-shot "
            "(bare prompt), context-enriched (failing-test + traceback), iterative "
            "(multi-turn feedback loop). "
            "Default: single-shot,context-enriched,iterative."
        ),
    )
    eval_llm_parser.add_argument(
        "--fl-modes",
        dest="fl_modes",
        default="auto,perfect",
        help="Comma-separated FL modes to run per bug (default: auto,perfect).",
    )
    eval_llm_parser.add_argument(
        "--model",
        default="gpt-5.4",
        help="LLM model name used for every cell (default: gpt-5.4).",
    )
    eval_llm_parser.add_argument(
        "--temperature",
        type=float,
        default=1.0,
        help="Sampling temperature (default: 1.0).",
    )
    eval_llm_parser.add_argument(
        "--llm-provider",
        dest="llm_provider",
        default="openai-compatible",
        help="LLM provider adapter (default: openai-compatible).",
    )
    eval_llm_parser.add_argument(
        "--llm-base-url",
        dest="llm_base_url",
        default="https://api.openai.com/v1",
        help="Base URL of the OpenAI-compatible endpoint (default: OpenAI).",
    )
    eval_llm_parser.add_argument(
        "--llm-api-key-env",
        dest="llm_api_key_env",
        default="OPENAI_API_KEY",
        help="Env var holding the API key (default: OPENAI_API_KEY).",
    )
    eval_llm_parser.add_argument(
        "--system-prompt",
        dest="system_prompt",
        default="prompt1",
        help="Name of the system prompt file to use (default: prompt1).",
    )
    eval_llm_parser.add_argument(
        "--max-candidates",
        dest="max_candidates",
        type=int,
        default=3,
        help="Max candidate patches per FL location (default: 3).",
    )
    eval_llm_parser.add_argument(
        "--top-n",
        dest="top_n",
        type=int,
        default=3,
        help="Top-N suspicious locations to attempt (default: 3).",
    )
    eval_llm_parser.add_argument(
        "--max-iterations",
        dest="max_iterations",
        type=int,
        default=5,
        help="Max conversation turns per location in the iterative variant (default: 5).",
    )
    eval_llm_parser.add_argument(
        "--budget",
        type=int,
        default=200,
        help="Max patch validations per cell (default: 200).",
    )
    eval_llm_parser.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="Seconds allowed per test-suite invocation (default: 120).",
    )
    eval_llm_parser.add_argument(
        "--fl-family",
        dest="fl_family",
        choices=["sbfl", "mbfl", "hybrid"],
        default="sbfl",
        help="Fault-localization family used in automated FL mode (default: sbfl).",
    )
    eval_llm_parser.add_argument(
        "--localization-metric",
        dest="localization_metric",
        default="ochiai",
        help="SBFL metric used in automated FL mode (default: ochiai).",
    )
    eval_llm_parser.add_argument(
        "--mbfl-metric",
        dest="mbfl_metric",
        default="metallaxis",
        help="MBFL metric for automated FL mode (default: metallaxis).",
    )
    eval_llm_parser.add_argument(
        "--mutation-budget",
        dest="mutation_budget",
        type=int,
        default=50,
        help="Max mutants per bug for MBFL/hybrid automated FL (default: 50).",
    )
    eval_llm_parser.add_argument(
        "--seed", type=int, default=0, help="Random seed for MBFL mutation selection."
    )
    eval_llm_parser.add_argument(
        "--granularity",
        choices=["statement", "function"],
        default="statement",
        help="Localization granularity for automated FL (default: statement).",
    )
    eval_llm_parser.add_argument(
        "--stop-on-first",
        dest="stop_on_first",
        action="store_true",
        help="Stop validating a cell after the first plausible patch.",
    )
    eval_llm_parser.add_argument(
        "--no-regression-check",
        dest="regression_check",
        action="store_false",
        help="Skip the regression half of plausibility (faster).",
    )
    eval_llm_parser.set_defaults(regression_check=True)
    eval_llm_parser.add_argument(
        "--ranker",
        choices=["weighted", "none"],
        default="none",
        help="Patch ranking strategy applied to plausible patches (default: none).",
    )
    eval_llm_parser.add_argument(
        "--ranker-weights",
        dest="ranker_weights",
        default=None,
        help="Comma-separated weights for the weighted ranker (default: 0.6,0.25,0.15).",
    )
    eval_llm_parser.add_argument(
        "--output-dir",
        default="experiment_results/llm_repair/task5",
        help=(
            "Directory for results.json and README.md "
            "(default: experiment_results/llm_repair/task5)."
        ),
    )
    eval_llm_parser.add_argument(
        "--runs-dir",
        dest="runs_dir",
        default="runs",
        help="Directory for per-cell run artifacts (default: runs).",
    )

    ## bugsinpy-evaluate-course-comparison (all repair approaches, side by side)
    eval_course_parser = bugsinpy_subparsers.add_parser(
        "evaluate-course-comparison",
        help=(
            "Run every repair approach built over the course (template, single-shot "
            "LLM, iterative LLM, and the full LLM pipeline) on a set of bugs, with "
            "LLM patch assessment and context-similarity scoring on every cell, and "
            "write an aggregated course-wide comparison report."
        ),
    )
    eval_course_parser.add_argument(
        "--bugs",
        default="black:1,black:3",
        help=(
            "Comma-separated project:bug_id pairs to evaluate "
            "(default: black:1,black:3). The default set only "
            "contains bugs FauxPy can actually localize, so the auto-FL column means "
            "something: FauxPy 0.7.0 cannot install on Python 3.7 bugs (tornado:14, "
            "youtube-dl:12), conflicts irreconcilably with fastapi's pins, and ranks "
            "nothing at all on scrapy:2. Run those bugs with '--fl-modes perfect' "
            "instead of scoring an auto cell that never ran."
        ),
    )
    eval_course_parser.add_argument(
        "--approaches",
        default="a3-template,a4-single-shot,a4-iterative,a5-full-llm",
        help=(
            "Comma-separated approaches to compare. Choices: a3-template (traditional), "
            "a4-single-shot (bare LLM prompt), a4-iterative (multi-turn feedback loop), "
            "a5-full-llm (LLM-FL + retrieval + assessment). "
            "Default: all four."
        ),
    )
    eval_course_parser.add_argument(
        "--fl-modes",
        dest="fl_modes",
        default="auto,perfect",
        help=(
            "Comma-separated FL modes for the approaches that use FauxPy/oracle FL "
            "(default: auto,perfect). The a5-full-llm approach localizes with the LLM "
            "and ignores this flag."
        ),
    )
    eval_course_parser.add_argument(
        "--retrieval-budget",
        dest="retrieval_budget",
        type=int,
        default=3,
        help=(
            "Max codebase-retrieval steps the model may take before proposing a patch, "
            "for approaches that use retrieval (default: 3)."
        ),
    )
    eval_course_parser.add_argument(
        "--model",
        default="gpt-5.4",
        help="LLM model used for localization, repair, and assessment (default: gpt-5.4).",
    )
    eval_course_parser.add_argument(
        "--temperature",
        type=float,
        default=1.0,
        help="Sampling temperature (default: 1.0).",
    )
    eval_course_parser.add_argument(
        "--llm-provider",
        dest="llm_provider",
        default="openai-compatible",
        help="LLM provider adapter (default: openai-compatible).",
    )
    eval_course_parser.add_argument(
        "--llm-base-url",
        dest="llm_base_url",
        default="https://api.openai.com/v1",
        help="Base URL of the OpenAI-compatible endpoint (default: OpenAI).",
    )
    eval_course_parser.add_argument(
        "--llm-api-key-env",
        dest="llm_api_key_env",
        default="OPENAI_API_KEY",
        help="Env var holding the API key (default: OPENAI_API_KEY).",
    )
    eval_course_parser.add_argument(
        "--system-prompt",
        dest="system_prompt",
        default="prompt1",
        help="Name of the LLM repair system prompt (default: prompt1).",
    )
    eval_course_parser.add_argument(
        "--fl-system-prompt",
        dest="fl_system_prompt",
        default="fl_prompt1",
        help="Name of the LLM fault-localization system prompt (default: fl_prompt1).",
    )
    eval_course_parser.add_argument(
        "--max-source-lines",
        dest="max_source_lines",
        type=int,
        default=400,
        help="Upper bound on source lines shown to the LLM localizer (default: 400).",
    )
    eval_course_parser.add_argument(
        "--source-window",
        dest="source_window",
        type=int,
        default=40,
        help="Context lines around each anchor shown to the LLM localizer (default: 40).",
    )
    eval_course_parser.add_argument(
        "--assess-system-prompt",
        dest="assess_system_prompt",
        default="assess_prompt1",
        help="Name of the LLM patch-assessment system prompt (default: assess_prompt1).",
    )
    eval_course_parser.add_argument(
        "--assess-max-patches",
        dest="assess_max_patches",
        type=int,
        default=None,
        help="Max plausible patches assessed per cell (default: all).",
    )
    eval_course_parser.add_argument(
        "--max-candidates",
        dest="max_candidates",
        type=int,
        default=3,
        help="Max candidate patches per FL location (default: 3).",
    )
    eval_course_parser.add_argument(
        "--top-n",
        dest="top_n",
        type=int,
        default=3,
        help="Top-N suspicious locations to attempt (default: 3).",
    )
    eval_course_parser.add_argument(
        "--max-iterations",
        dest="max_iterations",
        type=int,
        default=5,
        help="Max conversation turns per location in the iterative approach (default: 5).",
    )
    eval_course_parser.add_argument(
        "--operators",
        default="arith,comp,obo,bool,negate,return",
        help="Mutation operators for the template approach (default: all six).",
    )
    eval_course_parser.add_argument(
        "--budget",
        type=int,
        default=200,
        help="Max patch validations per cell (default: 200).",
    )
    eval_course_parser.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="Seconds allowed per test-suite invocation (default: 120).",
    )
    eval_course_parser.add_argument(
        "--fl-family",
        dest="fl_family",
        choices=["sbfl", "mbfl", "hybrid"],
        default="sbfl",
        help="Fault-localization family used in automated FL mode (default: sbfl).",
    )
    eval_course_parser.add_argument(
        "--localization-metric",
        dest="localization_metric",
        default="ochiai",
        help="SBFL metric used in automated FL mode (default: ochiai).",
    )
    eval_course_parser.add_argument(
        "--mbfl-metric",
        dest="mbfl_metric",
        default="metallaxis",
        help="MBFL metric for automated FL mode (default: metallaxis).",
    )
    eval_course_parser.add_argument(
        "--mutation-budget",
        dest="mutation_budget",
        type=int,
        default=50,
        help="Max mutants per bug for MBFL/hybrid automated FL (default: 50).",
    )
    eval_course_parser.add_argument(
        "--seed", type=int, default=0, help="Random seed for MBFL mutation selection."
    )
    eval_course_parser.add_argument(
        "--granularity",
        choices=["statement", "function"],
        default="statement",
        help="Localization granularity for automated FL (default: statement).",
    )
    eval_course_parser.add_argument(
        "--stop-on-first",
        dest="stop_on_first",
        action="store_true",
        help="Stop validating a cell after the first plausible patch.",
    )
    eval_course_parser.add_argument(
        "--no-regression-check",
        dest="regression_check",
        action="store_false",
        help="Skip the regression half of plausibility (faster).",
    )
    eval_course_parser.set_defaults(regression_check=True)
    eval_course_parser.add_argument(
        "--ranker",
        choices=["weighted", "none"],
        default="none",
        help="Patch ranking strategy applied to plausible patches (default: none).",
    )
    eval_course_parser.add_argument(
        "--ranker-weights",
        dest="ranker_weights",
        default=None,
        help="Comma-separated weights for the weighted ranker (default: 0.6,0.25,0.15).",
    )
    eval_course_parser.add_argument(
        "--output-dir",
        default="experiment_results/course_comparison",
        help=(
            "Directory for results.json and README.md "
            "(default: experiment_results/course_comparison)."
        ),
    )
    eval_course_parser.add_argument(
        "--runs-dir",
        dest="runs_dir",
        default="runs",
        help="Directory for per-cell run artifacts (default: runs).",
    )

    # Repair command
    repair_parser = subparsers.add_parser(
        "repair",
        help="Apply template-based APR to a checked-out and localized bug.",
    )
    repair_parser.add_argument("--project", required=True, help="BugsInPy project name")
    repair_parser.add_argument("--bug", type=int, required=True, help="Bug ID")
    repair_parser.add_argument(
        "--technique",
        choices=["template", "llm"],
        default="template",
        help="Repair technique: 'template' for AST mutation, 'llm' for LLM-based repair (default: template)",
    )
    repair_parser.add_argument(
        "--llm-provider",
        dest="llm_provider",
        type=str,
        default="openai-compatible",
        help="LLM client implementation to use (default: openai-compatible)",
    )
    repair_parser.add_argument(
        "--model",
        type=str,
        default="gpt-4.1-2025-04-14",
        help=(
            "LLM model name sent to the API (default: gpt-4.1-2025-04-14). "
            "Must be a model your GPT@RUB account exposes via /models — "
            "currently only gpt-4.1-2025-04-14 is confirmed accessible. "
            "An unavailable model (e.g. gpt-4o-2024-05-13, codestral-22b) returns HTTP 400."
        ),
    )
    repair_parser.add_argument(
        "--temperature",
        type=float,
        default=0.8,
        help="LLM sampling temperature in [0.0, 2.0] (default: 0.8)",
    )
    repair_parser.add_argument(
        "--max-candidates",
        dest="max_candidates",
        type=int,
        default=5,
        help="Max LLM patch candidates generated per suspicious location (default: 5)",
    )
    repair_parser.add_argument(
        "--system-prompt",
        dest="system_prompt",
        type=str,
        default="prompt1",
        help=(
            "Name (file stem) of the system prompt under "
            "repair/llm/prompts/ used for LLM-based repair (default: prompt1)"
        ),
    )
    repair_parser.add_argument(
        "--context-enrichment",
        dest="context_enrichment",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Enrich each LLM repair prompt with the failing test's source and "
            "traceback (default: enabled). Use --no-context-enrichment for the "
            "original prompt containing only the buggy function. LLM technique only."
        ),
    )
    repair_parser.add_argument(
        "--few-shot",
        dest="few_shot_count",
        type=int,
        default=0,
        metavar="N",
        help=(
            "Prepend N (buggy → fixed) example pairs from other bugs of the same "
            "BugsInPy project to each LLM prompt (default: 0 = off). Toggled "
            "independently of --context-enrichment. LLM technique only."
        ),
    )
    repair_parser.add_argument(
        "--iterative",
        dest="iterative",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "Enable the multi-turn feedback loop: after each failed patch, feed the "
            "test-failure output back to the LLM and ask it to revise (default: "
            "disabled). Ignored when --technique template. LLM technique only."
        ),
    )
    repair_parser.add_argument(
        "--max-iterations",
        dest="max_iterations",
        type=int,
        default=5,
        help=(
            "Max conversation turns per FL location before giving up on it. Only "
            "meaningful with --iterative (default: 5). LLM technique only."
        ),
    )
    repair_parser.add_argument(
        "--retrieval-budget",
        dest="retrieval_budget",
        type=int,
        default=0,
        help=(
            "Maximum RETRIEVE tool calls before LLM patch generation "
            "(default: 0 = disabled). LLM technique only."
        ),
    )
    repair_parser.add_argument(
        "--llm-base-url",
        dest="llm_base_url",
        type=str,
        default=None,
        help="Override the LLM API endpoint URL (default: GPT@RUB endpoint)",
    )
    repair_parser.add_argument(
        "--llm-api-key-env",
        dest="llm_api_key_env",
        type=str,
        default="GPT_AT_RUB_API_KEY",
        help="Environment variable name holding the LLM API key (default: GPT_AT_RUB_API_KEY)",
    )
    repair_parser.add_argument(
        "--budget",
        type=int,
        default=200,
        help="Max patch validations (default: 200)",
    )
    repair_parser.add_argument(
        "--top-n",
        dest="top_n",
        type=int,
        default=5,
        help="Top-N suspicious locations to attempt (default: 5)",
    )
    repair_parser.add_argument(
        "--operators",
        type=str,
        default="arith,comp,obo,bool,negate,return",
        help="Comma-separated list of enabled mutation operators (default: all)",
    )
    repair_parser.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="Seconds allowed per test-suite invocation (default: 120)",
    )
    repair_parser.add_argument(
        "--stop-on-first",
        dest="stop_on_first",
        action="store_true",
        help="Stop after the first plausible patch is found",
    )
    repair_parser.add_argument(
        "--no-regression-check",
        dest="regression_check",
        action="store_false",
        help=(
            "Only require the bug's trigger test to pass for plausibility; skip the "
            "regression run that verifies no previously passing test broke (faster)"
        ),
    )
    repair_parser.set_defaults(regression_check=True)
    repair_parser.add_argument(
        "--fl-mode",
        dest="fl_mode",
        choices=["auto", "perfect"],
        default="auto",
        help=(
            "Fault-localization mode: 'auto' runs the FauxPy localizer "
            "(see --fl-family); 'perfect' uses the BugsInPy developer-fix lines as "
            "the oracle fault location, ignoring --fl-family and --skip-localize "
            "(default: auto)"
        ),
    )
    repair_parser.add_argument(
        "--fl-backend",
        dest="fl_backend",
        choices=["fauxpy", "llm"],
        default="fauxpy",
        help=(
            "Fault-localization backend used when --fl-mode auto: 'fauxpy' runs the "
            "SBFL/MBFL/hybrid localizer (see --fl-family); 'llm' asks the LLM to rank "
            "suspicious lines. Ignored under --fl-mode perfect (default: fauxpy)"
        ),
    )
    repair_parser.add_argument(
        "--fl-system-prompt",
        dest="fl_system_prompt",
        type=str,
        default="fl_prompt1",
        help=(
            "Name (file stem) of the system prompt under localization/prompts/ used "
            "for --fl-backend llm (default: fl_prompt1)"
        ),
    )
    repair_parser.add_argument(
        "--max-source-lines",
        dest="max_source_lines",
        type=int,
        default=400,
        help=(
            "Upper bound on numbered source lines shown to the model for "
            "--fl-backend llm (default: 400)"
        ),
    )
    repair_parser.add_argument(
        "--source-window",
        dest="source_window",
        type=int,
        default=40,
        help=(
            "Lines of context above/below each traceback line shown to the model "
            "for --fl-backend llm (default: 40)"
        ),
    )
    repair_parser.add_argument(
        "--fl-family",
        dest="fl_family",
        choices=["sbfl", "mbfl", "hybrid"],
        default="sbfl",
        help="Fault-localization family used to rank repair targets (default: sbfl)",
    )
    repair_parser.add_argument(
        "--localization-metric",
        dest="localization_metric",
        type=str,
        default="ochiai",
        help="SBFL metric used when re-running localization (default: ochiai)",
    )
    repair_parser.add_argument(
        "--mbfl-metric",
        dest="mbfl_metric",
        type=str,
        default="metallaxis",
        help="MBFL metric (used for --fl-family mbfl/hybrid; default: metallaxis)",
    )
    repair_parser.add_argument(
        "--mutation-budget",
        dest="mutation_budget",
        type=int,
        default=50,
        help="Max mutants per bug for MBFL/hybrid localization (default: 50)",
    )
    repair_parser.add_argument(
        "--seed",
        type=int,
        default=0,
        help="Random seed for MBFL mutation selection (default: 0)",
    )
    repair_parser.add_argument(
        "--sbfl-weight",
        dest="sbfl_weight",
        type=float,
        default=0.5,
        help="Weight of the SBFL backend for --fl-family hybrid (default: 0.5)",
    )
    repair_parser.add_argument(
        "--mbfl-weight",
        dest="mbfl_weight",
        type=float,
        default=0.5,
        help="Weight of the MBFL backend for --fl-family hybrid (default: 0.5)",
    )
    repair_parser.add_argument(
        "--skip-localize",
        dest="skip_localize",
        action="store_true",
        help="Skip re-running localization; load a cached result from disk",
    )
    repair_parser.add_argument(
        "--granularity",
        type=str,
        default="statement",
        choices=["statement", "function"],
        help="Localization granularity used when re-running (default: statement)",
    )
    repair_parser.add_argument(
        "--runs-dir",
        dest="runs_dir",
        default="runs",
        help="Directory for run output (default: runs)",
    )
    repair_parser.add_argument(
        "--ranker",
        choices=["weighted", "none"],
        default="none",
        help=(
            "Patch ranking strategy applied to plausible patches after validation. "
            "'weighted' uses a composite score of suspiciousness, simplicity, and "
            "operator priority; 'none' preserves generation order (default: none)"
        ),
    )
    repair_parser.add_argument(
        "--ranker-weights",
        dest="ranker_weights",
        type=str,
        default=None,
        help=(
            "Comma-separated weights for the weighted ranker: "
            "suspiciousness_weight,simplicity_weight,operator_priority_weight "
            "(default: 0.6,0.25,0.15). Only used when --ranker weighted."
        ),
    )
    repair_parser.add_argument(
        "--assess",
        dest="assess",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "Assess plausible patches with an LLM and re-rank them by semantic "
            "quality (default: disabled)."
        ),
    )
    repair_parser.add_argument(
        "--assess-max-patches",
        dest="assess_max_patches",
        type=int,
        default=None,
        help="Maximum number of plausible patches to assess (default: all).",
    )
    repair_parser.add_argument(
        "--assess-system-prompt",
        dest="assess_system_prompt",
        type=str,
        default="assess_prompt1",
        help=(
            "Name (file stem) of the system prompt under repair/assessment/prompts/ "
            "used for LLM patch assessment (default: assess_prompt1)."
        ),
    )
    repair_parser.add_argument(
        "--similarity-score",
        dest="similarity_score",
        action=argparse.BooleanOptionalAction,
        default=False,
        help=(
            "Score each plausible patch's graded closeness to the developer fix "
            "(0.0-1.0, plus a human-readable band) and print/record it alongside "
            "the unchanged exact-diff 'correct' verdict (default: disabled)."
        ),
    )

    return parser
