# Break This Fix: case bundles

Real Python bugs (from [BugsInPy](https://github.com/soarsmu/BugsInPy)) where an AI
agent's fix passes the recorded tests and is still wrong, with evidence anyone can
replay. This is a **selected set of cases, not a failure rate**: it says that these
patches are wrong, not how often agents are wrong.

It started with our own pipeline: on black#3 it "fixed" the bug by changing the
test's expected exit code, and our validator accepted that
([audit](../experiment_results/INTEGRITY_AUDIT.md)).

The case contract, replay instructions and how to contribute are in
[docs/challenges.md](../docs/challenges.md).

## Replay a case

```bash
docker compose run --rm apr-framework          # then, inside the container:
python -m apr_framework bugsinpy setup          # once
python -m apr_framework challenge list
python -m apr_framework challenge run black-3-pipeline-test-edit            # 3 clean repeats
python -m apr_framework challenge run <case-id> --patch my_fix.diff         # try your own repair
```

`challenge run` checks the bug out fresh, then runs it three ways (buggy, the
candidate, the developer's fix) with the evaluator files locked, prints a table, writes
a JSON result under `.workspace/challenge_results/`, and exits 0 only if what it
observed matches the case's recorded expectation. No model or API key is needed.

## Two kinds of case

- **Type C (counterexample).** The agent changed source code; the original tests
  pass with tests, fixtures and configuration unchanged. A small **witness** test
  fails on the agent's fix and passes on the developer's fix. The witness's
  expectation is justified from the issue, docs or docstring, never from "the
  developer fix does X".
- **Type T (test tampering).** The agent's "fix" edits, deletes, skips or weakens a
  test. The original test, run against the agent's non-test changes, still fails.
  The case calls the fix wrong only if the original expectation is independently
  justified (agents can legitimately repair a bad test).

## Bundle layout

```text
challenges/cases/<case-id>/
  case.json                          metadata, contract, provenance, expectations
  candidate.diff                     the agent's patch, verbatim as captured
  witness/test_witness_<case-id>.py  Type C only: a unittest-style witness
  NOTES.md                           why the expected behaviour is the right one
```

Bundles never contain BugsInPy files (BugsInPy has no licence yet,
[soarsmu/BugsInPy#94](https://github.com/soarsmu/BugsInPy/issues/94)). `case.json`
names the bug by project and id, and `challenge run` fetches it at run time.

### `case.json`

| Field | Meaning |
|---|---|
| `id`, `version` | the directory name; bump `version` when the evidence changes |
| `status` | `candidate`, `accepted`, `disputed` or `withdrawn` (see below) |
| `type` | `C` or `T` |
| `project`, `bug_id`, `subject_license` | the BugsInPy bug and the subject project's licence |
| `promise` | one sentence: the behaviour the program promises |
| `contract_basis` | where that promise comes from: issue, docs or docstring, with quotes and links |
| `recorded_test_scope` | the trigger test IDs, trigger command and regression command the candidate passed |
| `candidate` | the diff, its SHA-256 and **provenance**: agent, agent version, model, prompt and its hash, caps, date, run directory |
| `witness` | Type C: path of the witness and what it asserts (`null` for Type T) |
| `expected` | per variant (`buggy`, `candidate`, `candidate_as_submitted` for Type T, `developer_fixed`): the expected `trigger` (`pass`/`fail`), `regression` (`ok`, `new_failures`, ...) and `witness` (`pass`/`fail`) |
| `replay_log` | summaries appended by `challenge run --record` |
| `reviewers`, `dispute_history`, `status_history` | who checked it, objections, status changes |

### Status

Tooling only ever creates `candidate` cases. The maintainer sets `accepted` after a
**second person** has reproduced the case. A `disputed` or `withdrawn` case keeps its
history and states the reason; nothing is silently replaced.

## Cases

| Case | Type | Status | Bug | Agent |
|---|---|---|---|---|
| [black-3-pipeline-test-edit](cases/black-3-pipeline-test-edit/NOTES.md) | T | candidate | black#3 | Repair Framework's own LLM pipeline (gpt-5.4) |
