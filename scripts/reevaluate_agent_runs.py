"""Re-evaluate saved agent-run diffs in a fresh clean checkout with today's gate.

``challenge agent-run`` evaluates the captured diff once, right after the agent
finishes. When the evaluation path changes later (a fixed parser, a stricter
gate), this script judges each saved ``patches/agent.diff`` again exactly as
``agent-run`` does (fresh checkout, evaluator-integrity gate, trigger test,
regression suite) and writes ``reevaluation.json`` next to the original
``repair_results.json``, which is left untouched.

    python scripts/reevaluate_agent_runs.py runs/run_405 [runs/run_406 ...]

Needs the executor (run on the host or in the framework container). Makes no
LLM calls.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from apr_framework.benchmarks.registry import create_bugsinpy_adapter  # noqa: E402
from apr_framework.challenge.agent_run import (  # noqa: E402
    serialise_evaluation,
    evaluate_candidate_in_clean_checkout,
)
from apr_framework.core.models import BugIdentifier  # noqa: E402
from apr_framework.evaluation.run_writer import RunWriter  # noqa: E402

TEST_TIMEOUT_SECONDS = 300


def main(run_dirs: list[str]) -> int:
    if not run_dirs:
        print(__doc__)
        return 2
    adapter = create_bugsinpy_adapter(Path.cwd())
    adapter.toolchain.ensure_installed()
    for run_dir in run_dirs:
        reevaluate_run(Path(run_dir), adapter)
    return 0


def reevaluate_run(run_dir: Path, adapter) -> None:
    results = json.loads((run_dir / "repair_results.json").read_text("utf-8"))
    diff_path = run_dir / "patches" / "agent.diff"
    if not diff_path.is_file():
        print(f"{run_dir}: no agent.diff, nothing to re-evaluate")
        return
    bug = BugIdentifier("bugsinpy", results["project"], int(results["bug_id"]))
    writer = RunWriter(run_dir)
    writer.log("Re-evaluating patches/agent.diff in a fresh checkout")
    evaluation = evaluate_candidate_in_clean_checkout(
        bug,
        adapter,
        Path.cwd(),
        diff_path.read_text("utf-8"),
        writer,
        TEST_TIMEOUT_SECONDS,
    )
    serialised = serialise_evaluation(evaluation)
    writer.write_json(
        "reevaluation.json",
        {
            "reevaluated_at": datetime.now(timezone.utc).isoformat(),
            "original_outcome": results.get("agent_run_outcome"),
            "evaluation": serialised,
        },
    )
    print(
        f"{run_dir}: original={results.get('agent_run_outcome')} "
        f"plausible={serialised and serialised.get('is_plausible')} "
        f"regression_checked={serialised and serialised.get('regression_checked')} "
        f"integrity={serialised and serialised.get('integrity')}"
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
