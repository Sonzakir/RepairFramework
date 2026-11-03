"""``repro new`` and ``repro check``: the Strict Repro stages wired together.

Both modes share one flow; a ``ReproTarget`` says where tests run and what counts
as the fix:

- BugsInPy (``--project/--bug``): a fresh, compiled checkout in the executor; the
  developer fix (without its test sections) is the fix; the oracle for probes is
  the bug's trigger test plus its test file.
- Local (``--repo/--python``): the user's project and interpreter; no fix until
  ``repro check --patch``; the oracle is the optional ``--regression-cmd``.

``repro new``: issue → generate → verify → (with a fix) probes → hardening →
report and ``runs/run_NNN``. ``repro check``: verify an existing repro test
against a patch → probes → (``--harden``) hardening → report.
"""

import json
import shutil
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from apr_framework.benchmarks.bugsinpy import BugsInPyAdapter
from apr_framework.challenge.replay import (
    check_out_and_compile_into,
    fingerprint_checkout,
)
from apr_framework.core.exceptions import ConfigurationError, EvaluationError
from apr_framework.core.models import BugIdentifier, CheckoutResult
from apr_framework.evaluation.run_writer import RunWriter
from apr_framework.repair.evaluator_integrity import (
    EvaluatorGuard,
    read_declared_test_files,
)
from apr_framework.repair.llm.client import OpenAICompatibleClient
from apr_framework.repair.regression import RegressionContext, build_regression_context
from apr_framework.repair.unified_diff import touched_relative_paths
from apr_framework.repro.contamination import (
    max_function_similarity,
    read_developer_trigger_functions,
)
from apr_framework.repro.generator import (
    GenerationOptions,
    find_module_problem,
    generate_repro_test,
)
from apr_framework.repro.issues import (
    fetch_and_screen_issue,
    find_bugsinpy_issue_url,
    read_and_screen_issue_file,
    read_fix_commit_id,
)
from apr_framework.repro.models import (
    HardeningOutcome,
    ReproIssue,
    ReproRunResult,
    TestVersion,
    Verification,
)
from apr_framework.repro.probes import (
    ProbeOptions,
    build_deterministic_probes,
    generate_llm_probes,
    number_probes,
    rerun_repro_on_probes,
    run_probes,
    select_probes,
)
from apr_framework.repro.prompts import prompt_hashes
from apr_framework.repro.report import export_witness_bundle, write_run_artifacts
from apr_framework.repro.runner import (
    BugsInPyCommandRunner,
    LocalPytestRunner,
    TestCommandRunner,
    repro_test_file_name,
)
from apr_framework.repro.source_view import (
    LOCAL_REPRO_DIRECTORY,
    SourceView,
    build_bugsinpy_source_view,
    build_local_source_view,
)
from apr_framework.repro.spend import (
    MeteredLLMClient,
    SpendMeter,
    resolve_model_price,
)
from apr_framework.repro.strictness import (
    select_probes_for_hardening,
    summarise_strictness,
    write_hardened_test,
)
from apr_framework.repro.validation import (
    FixUnderTest,
    plan_fix_without_evaluator_files,
    validate_file_states_with_repro,
)
from apr_framework.repro.verifier import VerificationOptions, verify_repro_test

BUGSINPY_WORKSPACE_PREFIX = "strictrepro_"
SOURCE_VIEW_DIR = Path(".workspace") / "repro_views"
REPRO_STATE_FILE = "repro.json"


@dataclass(frozen=True)
class ReproLLMSettings:
    """Model and endpoint. Generation, probes and hardening use
    ``generation_temperature``; the judge uses ``judge_temperature``."""

    model_name: str = "gpt-5.4"
    generation_temperature: float = 1.0
    judge_temperature: float = 0.0
    base_url: str | None = None
    api_key_env_var: str = "GPT_AT_RUB_API_KEY"
    usd_per_mtok_in: float | None = None
    usd_per_mtok_out: float | None = None


@dataclass(frozen=True)
class ReproOptions:
    generation: GenerationOptions = field(default_factory=GenerationOptions)
    verification: VerificationOptions = field(default_factory=VerificationOptions)
    probes: ProbeOptions = field(default_factory=ProbeOptions)
    run_probes: bool = True
    harden: bool = True
    max_usd: float = 2.0
    runs_dir: Path = Path("runs")
    reuse_checkout: bool = False
    export_witness: Path | None = None


@dataclass(frozen=True)
class _ConnectionConfig:
    """Satisfies ``LLMConnectionConfig`` for one temperature."""

    model_name: str
    temperature: float
    base_url: str | None
    api_key_env_var: str


@dataclass
class ReproClients:
    generator: MeteredLLMClient
    judge: MeteredLLMClient
    meter: SpendMeter


@dataclass
class ReproTarget:
    """Where tests run and what counts as the fix.

    Fields:
        label:               ``tqdm:5`` or the local project's name.
        mode:                ``bugsinpy`` or ``local``.
        test_id:             Names the test file (``test_repro_<id>.py``).
        runner:              The runner seam for this target.
        fix:                 The fix the test must pass on (None: no fix yet).
        view:                What the generator may see (None for ``check``).
        build_regression_context: The test-file baseline for probe confirmation
                             (takes the per-command timeout).
        has_trigger_baseline: Whether the oracle is a BugsInPy trigger test.
        trigger_functions:   The developer's trigger test sources (contamination).
        integrity_note:      Evaluator files stripped from the fix, if any.
    """

    label: str
    mode: str
    test_id: str
    runner: TestCommandRunner
    fix: FixUnderTest | None
    view: SourceView | None
    build_regression_context: Callable[[int], RegressionContext]
    has_trigger_baseline: bool
    trigger_functions: list[str] = field(default_factory=list)
    integrity_note: str = ""

    @property
    def test_file_name(self) -> str:
        return repro_test_file_name(self.test_id)

    def build_guard(self) -> EvaluatorGuard:
        """A fresh evaluator snapshot (taken right before changes are applied)."""
        return EvaluatorGuard.for_worktree(self.runner.checkout.worktree)


# --------------------------------------------------------------- repro new


def run_repro_new_on_bugsinpy(
    bug: BugIdentifier,
    adapter: BugsInPyAdapter,
    project_root: Path,
    settings: ReproLLMSettings,
    options: ReproOptions,
    meter: SpendMeter | None = None,
) -> ReproRunResult:
    """Issue → verified, probed, hardened test for one BugsInPy bug."""
    started = time.monotonic()
    clients = build_repro_clients(settings, meter or SpendMeter(options.max_usd))
    writer = create_run_writer_and_record_config(
        project_root, options, settings, "repro new", f"{bug.project}:{bug.bug_id}"
    )
    checkout = check_out_and_compile_bug(bug, adapter, project_root, options)
    issue = fetch_and_screen_issue(
        find_bugsinpy_issue_url(bug),
        project_root,
        read_fix_commit_id(checkout.worktree),
    )
    target = prepare_bugsinpy_target(
        bug,
        adapter,
        checkout,
        project_root,
        fix_label="developer fix",
        fix_diff_text=adapter.get_reference_patch(bug) or "",
        with_view=True,
    )
    clean_fingerprint = fingerprint_checkout(checkout.worktree)
    try:
        result = generate_verify_and_probe(issue, target, clients, options, writer)
    finally:
        remove_source_view(target)
    result.checkout_clean_after = (
        fingerprint_checkout(checkout.worktree) == clean_fingerprint
    )
    return finish_run(result, writer, clients, options, started)


def run_repro_new_locally(
    repo: Path,
    python: Path,
    issue_reference: str,
    project_root: Path,
    settings: ReproLLMSettings,
    options: ReproOptions,
    *,
    allow_local_exec: bool,
    regression_command: str | None = None,
    test_id: str | None = None,
) -> ReproRunResult:
    """Issue → verified test for a local pytest project (no fix yet)."""
    started = time.monotonic()
    resolved_test_id = test_id or derive_local_test_id(issue_reference)
    runner = LocalPytestRunner(
        repo,
        python,
        resolved_test_id,
        regression_command,
        allow_local_exec=allow_local_exec,
    )
    clients = build_repro_clients(settings, SpendMeter(options.max_usd))
    writer = create_run_writer_and_record_config(
        project_root, options, settings, "repro new", str(runner.repo)
    )
    issue = load_local_issue(issue_reference, project_root)
    target = prepare_local_target(runner, resolved_test_id, project_root, fix=None)
    try:
        result = generate_verify_and_probe(issue, target, clients, options, writer)
    finally:
        remove_source_view(target)
    write_local_repro_directory(runner, result, issue_reference, regression_command)
    return finish_run(result, writer, clients, options, started)


def generate_verify_and_probe(
    issue: ReproIssue,
    target: ReproTarget,
    clients: ReproClients,
    options: ReproOptions,
    writer: RunWriter,
) -> ReproRunResult:
    """Generate the test, verify it, and (with a fix) measure and harden it."""
    result = ReproRunResult(
        command="repro new",
        target=target.label,
        test_id=target.test_id,
        issue=issue,
        mode=target.mode,
    )
    result.integrity_note = target.integrity_note
    generation = generate_repro_test(
        issue,
        target.view,
        target.runner,
        clients.generator,
        target.test_file_name,
        options.generation,
        on_version=lambda version: show_and_log_version(version, target, writer),
    )
    record_generation(result, generation)
    if generation.final is None or find_module_problem(generation.final.source):
        result.notes.append("no runnable test was generated")
        return result
    result.verification = verify_test_against_target(
        generation.final, issue, target, clients, options
    )
    result.contamination_similarity = max_function_similarity(
        generation.final.source, target.trigger_functions
    )
    if target.fix is not None and options.run_probes:
        measure_strictness_and_harden(result, issue, target, clients, options)
    return result


# --------------------------------------------------------------- repro check


def run_repro_check(
    repro_directory: Path,
    patch_path: Path,
    project_root: Path,
    settings: ReproLLMSettings,
    options: ReproOptions,
    *,
    adapter_factory: Callable[[], BugsInPyAdapter],
    allow_local_exec: bool,
    regression_command: str | None = None,
) -> ReproRunResult:
    """Judge a patch with an existing repro test: F→P, probes, optional hardening."""
    started = time.monotonic()
    state = load_repro_state(repro_directory)
    test = TestVersion(label="repro", source=read_repro_test(repro_directory, state))
    clients = build_repro_clients(settings, SpendMeter(options.max_usd))
    writer = create_run_writer_and_record_config(
        project_root, options, settings, "repro check", state["target"]
    )
    issue = reload_recorded_issue(state, project_root)
    target, checkout = prepare_check_target(
        state,
        patch_path,
        project_root,
        options,
        adapter_factory,
        allow_local_exec,
        regression_command,
    )
    clean_fingerprint = fingerprint_checkout(checkout.worktree)
    result = check_patch_with_repro_test(test, issue, target, clients, options)
    result.checkout_clean_after = (
        fingerprint_checkout(checkout.worktree) == clean_fingerprint
    )
    return finish_run(result, writer, clients, options, started)


def check_patch_with_repro_test(
    test: TestVersion,
    issue: ReproIssue,
    target: ReproTarget,
    clients: ReproClients,
    options: ReproOptions,
) -> ReproRunResult:
    result = ReproRunResult(
        command="repro check",
        target=target.label,
        test_id=target.test_id,
        issue=issue,
        mode=target.mode,
    )
    result.integrity_note = target.integrity_note
    result.fix_diff_text = target.fix.diff_text
    result.test_versions.append(test)
    result.verification = verify_test_against_target(
        test, issue, target, clients, options
    )
    if options.run_probes:
        measure_strictness_and_harden(result, issue, target, clients, options)
    return result


# --------------------------------------------------------------- shared stages


def verify_test_against_target(
    test: TestVersion,
    issue: ReproIssue,
    target: ReproTarget,
    clients: ReproClients,
    options: ReproOptions,
) -> Verification:
    return verify_repro_test(
        test,
        issue,
        target.runner,
        clients.judge,
        target.test_file_name,
        options.verification,
        fix=target.fix,
        build_guard=target.build_guard,
    )


def measure_strictness_and_harden(
    result: ReproRunResult,
    issue: ReproIssue,
    target: ReproTarget,
    clients: ReproClients,
    options: ReproOptions,
) -> None:
    """Probe the fix, measure strictness, then (optionally) harden and re-measure."""
    verification = result.verification
    if verification is None or not verification.is_fail_to_pass:
        result.notes.append(
            "probes skipped: the test is not fail-to-pass with the fix, so "
            "strictness would be meaningless"
        )
        return
    timeout_seconds = options.verification.timeout_seconds
    regression_context = target.build_regression_context(timeout_seconds)
    oracle_usable = is_oracle_passing_on_fix(
        target, verification.test, regression_context, timeout_seconds
    )
    probes = build_probe_pool(target, verification.test, issue, clients, options)
    result.probe_results = run_probes(
        probes,
        target.fix,
        verification.test,
        target.runner,
        target.build_guard,
        regression_context,
        oracle_usable,
        timeout_seconds,
    )
    result.strictness_before = summarise_strictness(
        verification.test.label, result.probe_results, target.has_trigger_baseline
    )
    if options.harden:
        result.hardening = harden_and_remeasure(result, issue, target, clients, options)
        if result.hardening.test is not None:
            result.test_versions.append(result.hardening.test)


def is_oracle_passing_on_fix(
    target: ReproTarget,
    test: TestVersion,
    regression_context: RegressionContext,
    timeout_seconds: int,
) -> bool:
    """The oracle confirms probes only if the fix itself passes it (and its suite)."""
    if not target.runner.has_oracle:
        return False
    outcome, _ = validate_file_states_with_repro(
        target.fix.files,
        target.runner,
        target.build_guard(),
        regression_context,
        test,
        timeout_seconds,
    )
    return outcome.is_plausible


def build_probe_pool(
    target: ReproTarget,
    test: TestVersion,
    issue: ReproIssue,
    clients: ReproClients,
    options: ReproOptions,
) -> list:
    deterministic = select_probes(
        build_deterministic_probes(target.fix),
        options.probes.max_probes,
        seed=target.test_id,
    )
    test_aware = generate_llm_probes(
        target.fix, test, issue, clients.generator, options.probes.max_llm_probes
    )
    return number_probes(deterministic + test_aware)


def harden_and_remeasure(
    result: ReproRunResult,
    issue: ReproIssue,
    target: ReproTarget,
    clients: ReproClients,
    options: ReproOptions,
) -> HardeningOutcome:
    """One hardening round; keep the hardened test only if it re-verifies."""
    lax_spots = select_probes_for_hardening(result.probe_results)
    if not lax_spots:
        return HardeningOutcome(
            attempted=False,
            reason="no confirmed-wrong probe is accepted by the test",
        )
    hardened = write_hardened_test(
        issue, result.verification.test, lax_spots, clients.generator
    )
    problem = find_module_problem(hardened.source)
    if problem:
        return HardeningOutcome(attempted=True, reason=problem, test=hardened)
    verification = verify_test_against_target(hardened, issue, target, clients, options)
    if not (verification.labelled_reproduces and verification.is_fail_to_pass):
        return HardeningOutcome(
            attempted=True,
            reason="the hardened test failed re-verification; the original is kept",
            test=hardened,
            verification=verification,
        )
    rerun = rerun_repro_on_probes(
        result.probe_results,
        target.fix,
        hardened,
        target.runner,
        target.build_guard,
        options.verification.timeout_seconds,
    )
    return HardeningOutcome(
        attempted=True,
        kept=True,
        test=hardened,
        verification=verification,
        after=summarise_strictness("hardened", rerun, target.has_trigger_baseline),
        probe_results_after=rerun,
    )


# --------------------------------------------------------------- targets


def check_out_and_compile_bug(
    bug: BugIdentifier,
    adapter: BugsInPyAdapter,
    project_root: Path,
    options: ReproOptions,
) -> CheckoutResult:
    destination = (
        project_root
        / ".workspace"
        / "bugsinpy"
        / f"{BUGSINPY_WORKSPACE_PREFIX}{bug.project}_{bug.bug_id}"
    )
    return check_out_and_compile_into(bug, adapter, destination, options.reuse_checkout)


def prepare_bugsinpy_target(
    bug: BugIdentifier,
    adapter: BugsInPyAdapter,
    checkout: CheckoutResult,
    project_root: Path,
    *,
    fix_label: str,
    fix_diff_text: str,
    with_view: bool,
) -> ReproTarget:
    """Runner, fix (evaluator sections stripped), answer-free view, baselines."""
    test_id = f"{bug.project}_{bug.bug_id}"
    declared_test_files = read_declared_test_files(checkout.worktree)
    fix = plan_fix_without_evaluator_files(
        fix_label, fix_diff_text, checkout.worktree, declared_test_files
    )
    developer_fix_paths = frozenset(
        touched_relative_paths(
            adapter.get_reference_patch(bug) or "", checkout.worktree.name
        )
    )
    view = (
        build_bugsinpy_source_view(
            checkout,
            declared_test_files,
            developer_fix_paths,
            source_view_destination(project_root, test_id),
        )
        if with_view
        else None
    )
    runner = BugsInPyCommandRunner(adapter, checkout, project_root, test_id)
    return ReproTarget(
        label=f"{bug.project}:{bug.bug_id}",
        mode="bugsinpy",
        test_id=test_id,
        runner=runner,
        fix=fix,
        view=view,
        build_regression_context=lambda timeout: build_regression_context(
            adapter, checkout, enabled=True, timeout=timeout
        ),
        has_trigger_baseline=True,
        trigger_functions=read_developer_trigger_functions(checkout.worktree),
        integrity_note=describe_stripped_paths(fix),
    )


def prepare_local_target(
    runner: LocalPytestRunner,
    test_id: str,
    project_root: Path,
    fix: FixUnderTest | None,
    with_view: bool = True,
) -> ReproTarget:
    view = (
        build_local_source_view(
            runner.checkout, source_view_destination(project_root, test_id)
        )
        if with_view
        else None
    )
    return ReproTarget(
        label=runner.repo.name,
        mode="local",
        test_id=test_id,
        runner=runner,
        fix=fix,
        view=view,
        build_regression_context=lambda timeout: RegressionContext(
            enabled=False,
            disabled_reason="local mode: --regression-cmd is the oracle",
        ),
        has_trigger_baseline=False,
        integrity_note=describe_stripped_paths(fix) if fix else "",
    )


def prepare_check_target(
    state: dict,
    patch_path: Path,
    project_root: Path,
    options: ReproOptions,
    adapter_factory: Callable[[], BugsInPyAdapter],
    allow_local_exec: bool,
    regression_command: str | None,
) -> tuple[ReproTarget, CheckoutResult]:
    """The target recorded in ``repro.json``, with ``--patch`` as the fix."""
    patch_text = patch_path.read_text(encoding="utf-8")
    if state["mode"] == "bugsinpy":
        adapter = adapter_factory()
        bug = BugIdentifier("bugsinpy", state["project"], int(state["bug_id"]))
        checkout = check_out_and_compile_bug(bug, adapter, project_root, options)
        target = prepare_bugsinpy_target(
            bug,
            adapter,
            checkout,
            project_root,
            fix_label=patch_path.name,
            fix_diff_text=patch_text,
            with_view=False,
        )
        return target, checkout
    runner = LocalPytestRunner(
        Path(state["repo"]),
        Path(state["python"]),
        state["test_id"],
        regression_command or state.get("regression_command"),
        allow_local_exec=allow_local_exec,
    )
    fix = plan_fix_without_evaluator_files(
        patch_path.name, patch_text, runner.repo, frozenset()
    )
    target = prepare_local_target(
        runner, state["test_id"], project_root, fix, with_view=False
    )
    return target, runner.checkout


def describe_stripped_paths(fix: FixUnderTest) -> str:
    if not fix.stripped_paths:
        return "ok (no test, fixture or config edits)"
    return "judged without its evaluator-file edits: " + ", ".join(fix.stripped_paths)


def source_view_destination(project_root: Path, test_id: str) -> Path:
    return project_root / SOURCE_VIEW_DIR / f"{test_id}-{uuid.uuid4().hex[:8]}"


def remove_source_view(target: ReproTarget) -> None:
    if target.view is not None:
        shutil.rmtree(target.view.checkout.worktree, ignore_errors=True)


# --------------------------------------------------------------- local mode


def load_local_issue(issue_reference: str, project_root: Path) -> ReproIssue:
    if issue_reference.startswith(("http://", "https://")):
        return fetch_and_screen_issue(issue_reference, project_root)
    return read_and_screen_issue_file(Path(issue_reference))


def derive_local_test_id(issue_reference: str) -> str:
    """``issue_412`` for an issue URL, the file stem for an issue file."""
    if issue_reference.startswith(("http://", "https://")):
        return "issue_" + issue_reference.rstrip("/").rsplit("/", 1)[-1]
    return Path(issue_reference).stem


def write_local_repro_directory(
    runner: LocalPytestRunner,
    result: ReproRunResult,
    issue_reference: str,
    regression_command: str | None,
) -> None:
    """``.apr/repro/<id>/``: the final test and ``repro.json`` for ``repro check``."""
    final_test = result.final_test
    if final_test is None:
        return
    directory = runner.repo / LOCAL_REPRO_DIRECTORY / "repro" / result.test_id
    directory.mkdir(parents=True, exist_ok=True)
    test_path = directory / repro_test_file_name(result.test_id)
    test_path.write_text(final_test.source, encoding="utf-8")
    result.test_path = test_path
    state = {
        "mode": "local",
        "target": result.target,
        "test_id": result.test_id,
        "test_file": test_path.name,
        "repo": str(runner.repo),
        "python": str(runner.python),
        "issue": issue_reference,
        "issue_sha256": result.issue.sha256 if result.issue else "",
        "regression_command": regression_command,
    }
    (directory / REPRO_STATE_FILE).write_text(
        json.dumps(state, indent=2) + "\n", encoding="utf-8"
    )


# --------------------------------------------------------------- repro state


def load_repro_state(repro_directory: Path) -> dict:
    state_path = repro_directory / REPRO_STATE_FILE
    if not state_path.is_file():
        raise ConfigurationError(
            f"{repro_directory} is not a repro directory (no {REPRO_STATE_FILE}). "
            "Pass the runs/run_NNN of a BugsInPy `repro new`, or .apr/repro/<id>."
        )
    return json.loads(state_path.read_text(encoding="utf-8"))


def read_repro_test(repro_directory: Path, state: dict) -> str:
    return (repro_directory / state["test_file"]).read_text(encoding="utf-8")


def reload_recorded_issue(state: dict, project_root: Path) -> ReproIssue:
    """Fetch the issue again (it is never stored) and warn if it changed."""
    issue = load_local_issue(state["issue"], project_root)
    if state.get("issue_sha256") and issue.sha256 != state["issue_sha256"]:
        print("warning: the issue text changed since the repro test was generated")
    return issue


# --------------------------------------------------------------- run lifecycle


def build_repro_clients(settings: ReproLLMSettings, meter: SpendMeter) -> ReproClients:
    price = resolve_model_price(
        settings.model_name, settings.usd_per_mtok_in, settings.usd_per_mtok_out
    )

    def metered_client(temperature: float) -> MeteredLLMClient:
        connection = _ConnectionConfig(
            model_name=settings.model_name,
            temperature=temperature,
            base_url=settings.base_url,
            api_key_env_var=settings.api_key_env_var,
        )
        return MeteredLLMClient(
            OpenAICompatibleClient(connection), meter, price, temperature
        )

    return ReproClients(
        generator=metered_client(settings.generation_temperature),
        judge=metered_client(settings.judge_temperature),
        meter=meter,
    )


def create_run_writer_and_record_config(
    project_root: Path,
    options: ReproOptions,
    settings: ReproLLMSettings,
    command: str,
    target: str,
) -> RunWriter:
    runs_dir = (
        options.runs_dir
        if options.runs_dir.is_absolute()
        else project_root / options.runs_dir
    )
    writer = RunWriter.create(runs_dir)
    price = resolve_model_price(
        settings.model_name, settings.usd_per_mtok_in, settings.usd_per_mtok_out
    )
    writer.write_json(
        "config.json",
        {
            "command": command,
            "target": target,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "llm": asdict(settings) | {"price_usd_per_mtok": asdict(price)},
            "options": json.loads(json.dumps(asdict(options), default=str)),
            "prompt_sha256": prompt_hashes(),
        },
    )
    writer.log(f"Started {command} for {target}")
    return writer


def show_and_log_version(
    version: TestVersion, target: ReproTarget, writer: RunWriter
) -> None:
    """Log every version; in local mode also print it before it runs."""
    writer.log(f"Generated test version {version.label} ({version.sha256[:12]})")
    if isinstance(target.runner, LocalPytestRunner):
        print(f"--- generated test {version.label} (runs next, locally) ---")
        print(version.source)


def record_generation(result: ReproRunResult, generation) -> None:
    result.test_versions.extend(generation.versions)
    result.generation_rounds = generation.rounds
    result.generation_stop_reason = generation.stop_reason
    result.generation_classifications = list(generation.classifications)
    result.retrievals = list(generation.retrievals)


def finish_run(
    result: ReproRunResult,
    writer: RunWriter,
    clients: ReproClients,
    options: ReproOptions,
    started: float,
) -> ReproRunResult:
    """Stamp spend and time, write the artifacts, refuse a dirty checkout."""
    result.llm_calls = clients.meter.call_count
    result.llm_usd = clients.meter.spent_usd
    result.seconds = round(time.monotonic() - started, 1)
    result.run_dir = writer.run_dir
    write_run_artifacts(result, writer, clients.meter)
    if options.export_witness is not None:
        export_witness_bundle(result, options.export_witness)
    if result.checkout_clean_after is False:
        raise EvaluationError(
            f"The checkout changed during {result.command}; see {writer.run_dir}."
        )
    return result
