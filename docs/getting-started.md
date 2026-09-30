# Getting started

## Requirements

- **Docker** with a reachable Docker daemon. The BugsInPy integration needs it: the
  framework builds and controls a BugsInPy executor container.
- **Git**
- **Python 3.10+**, only if you want a local editable install for development or
  unit tests
- An **OpenAI-compatible API key**, only for the LLM features (LLM localization,
  LLM repair, patch assessment)

## Install with Docker Compose (recommended)

Run these from the repository root (not from inside `src/`):

```bash
# (optional) export APR_HOST_PROJECT_ROOT="$(pwd)"
docker compose build
docker compose run --rm apr-framework
```

Inside the framework container, bootstrap BugsInPy:

```bash
python -m apr_framework bugsinpy setup
```

`bugsinpy setup` does four things:

1. clones the [multi-Python BugsInPy fork](architecture.md#design-decisions) into `.tools/bugsinpy` if it isn't there yet;
2. normalizes the helper scripts;
3. builds the local `apr-bugsinpy:local` image;
4. starts the long-lived executor container, `apr-bugsinpy-executor`.

### Optional: local editable install

Use this for import checks and framework development without running BugsInPy:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .            # add ".[dev]" for ruff
pytest tests/
```

A local install is enough for unit tests and package imports. BugsInPy commands
still need Docker and a completed `bugsinpy setup`.

### Starting from a clean Ubuntu 24.04 container

```bash
docker run -it --rm \
  -v "$(pwd)":/repo -w /repo \
  -v /var/run/docker.sock:/var/run/docker.sock \
  ubuntu:24.04 bash
```

```bash
apt-get update && apt-get install -y docker.io docker-compose-v2
```

```bash
export APR_HOST_PROJECT_ROOT=/path/to/project

docker compose build
docker compose run --rm apr-framework
```

```bash
# One-time: clone BugsInPy, build apr-bugsinpy:local, start the executor
python -m apr_framework bugsinpy setup

# Sanity checks
python -m apr_framework list-benchmarks
python -m apr_framework bugsinpy list-projects
python -m apr_framework bugsinpy list-bugs black

# Run a bug end-to-end
python -m apr_framework bugsinpy checkout black 1
python -m apr_framework bugsinpy test black 1
```

## Configure an LLM provider

The LLM features talk to any **OpenAI-compatible** endpoint. The recommended setup
is OpenAI itself:

```bash
# Option A: interactive, masked prompt that writes the key to a gitignored .env
python -m apr_framework configure --llm-api-key-env OPENAI_API_KEY

# Option B: export it for the current shell
export OPENAI_API_KEY="<your-openai-key>"
```

Then pass the endpoint and key variable on each LLM command:

```bash
--llm-base-url https://api.openai.com/v1 --llm-api-key-env OPENAI_API_KEY --model gpt-5.4
```

> **Always pass `--llm-base-url` and `--llm-api-key-env` explicitly.** On the
> `localize` and `repair` commands, the built-in defaults still point at the legacy
> GPT@RUB gateway: `--llm-api-key-env GPT_AT_RUB_API_KEY`, and `--llm-base-url`
> resolves to `https://gpt.ruhr-uni-bochum.de/external/v1`. That gateway is only
> reachable from the RUB network/VPN. The `evaluate-llm-repair` and
> `evaluate-course-comparison` matrices already default to OpenAI.

`--llm-base-url` and `--llm-api-key-env` are independent overrides, so any
OpenAI-compatible provider works:

```bash
--llm-base-url https://my.endpoint/api --llm-api-key-env MY_LLM_KEY
```

When you go directly to OpenAI, `--model` can be any model your account can use
(e.g. `gpt-5.4`, `gpt-5`, `o3`, `gpt-4o`). `gpt-4.1-2025-04-14` and `gpt-5.4` are the
models used in the published runs.

`OpenAICompatibleClient` also self-throttles to **60 requests/minute**. It tracks its
own request timestamps in a sliding 60-second window and sleeps before a call when
needed. This originally honoured GPT@RUB's cap and applies whatever endpoint you use.

## Your first run

List the registered benchmarks (currently `bugsinpy`):

```bash
python -m apr_framework list-benchmarks
```

Explore BugsInPy:

```bash
python -m apr_framework bugsinpy list-projects
python -m apr_framework bugsinpy list-bugs black
```

Check out a buggy version, prepare it, and run its failing tests:

```bash
python -m apr_framework bugsinpy checkout black 1
python -m apr_framework bugsinpy compile  black 1   # optional: `test` compiles too
python -m apr_framework bugsinpy test     black 1
```

```text
Project: black
Bug ID: 1
Checkout success: True
Prepared: True
Tests run: 1
Passing: 0
Failing: 1
```

Not every BugsInPy bug is reproducible (missing requirements and so on). If
localization later misbehaves on a bug, `localize --show-raw-output` prints FauxPy's
raw output for debugging.

From there:

- localize the fault: [Fault localization](fault-localization.md)
- repair it: [Template repair](template-repair.md) · [LLM repair](llm-repair.md)
- run a whole matrix: [Evaluation harnesses](evaluation.md)

## Full clean rebuild

```bash
docker compose down --remove-orphans
docker rm -f apr-bugsinpy-executor 2>/dev/null || true
docker rmi apr-framework:local apr-bugsinpy:local 2>/dev/null || true

docker compose build --no-cache
docker compose run --rm apr-framework
```

```bash
python -m apr_framework bugsinpy setup
python -m apr_framework bugsinpy checkout black 1
python -m apr_framework bugsinpy compile black 1
```

## Troubleshooting

- **Docker Compose cannot infer the host repository path.** Set it explicitly:

  ```bash
  export APR_HOST_PROJECT_ROOT="$(pwd)"
  ```

  If the framework runs inside Docker without `APR_HOST_PROJECT_ROOT`, BugsInPy
  setup fails, because the sibling executor container cannot mount the same
  repository files.

- **The executor container has stale mounts.** Remove it and run setup again:

  ```bash
  docker rm -f apr-bugsinpy-executor
  python -m apr_framework bugsinpy setup
  ```

- **Windows.** Convert shell scripts from CRLF to LF line endings.

- **FauxPy localization needs a checked-out, compiled bug.** Run `checkout` then
  `compile` (or `test`, which does both) before `localize`.

- **FauxPy needs `run_test.sh` to invoke pytest directly.** Projects that only use
  `unittest discover` are not supported.

- **FauxPy reports a missing `Jaccard` or `WSBI` metric.** The framework's SBFL patch
  was not applied. Check that the checkout's virtual environment is intact and re-run
  `compile`.

- **`localize --backend llm` ranks only test-file lines.** Symbol anchoring found
  nothing. Check `results.json → metadata.files_shown`. Usually the failing test
  doesn't `patch()`/mock a project symbol, or the patched module didn't resolve to a
  file under the worktree.

- **LLM calls time out against the default endpoint.** You are hitting the legacy
  GPT@RUB gateway, which is only reachable from the RUB network/VPN. Pass
  `--llm-base-url https://api.openai.com/v1 --llm-api-key-env OPENAI_API_KEY`
  instead.
