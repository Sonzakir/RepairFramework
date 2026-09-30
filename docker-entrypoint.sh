#!/usr/bin/env bash
set -euo pipefail

mkdir -p /workspace/.workspace/bugsinpy

if [ -d "${BUGSINPY_HOME}/framework/bin" ]; then
  find "${BUGSINPY_HOME}/framework/bin" -type f -name 'bugsinpy-*' -exec sed -i 's/\r$//' {} +
  chmod +x "${BUGSINPY_HOME}"/framework/bin/bugsinpy-* || true
fi

if [ "$#" -gt 0 ]; then
  exec "$@"
fi

echo 'APR framework container ready.'
echo 'Run commands such as: python -m apr_framework list-benchmarks'
exec bash
