#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
set -euo pipefail

QUERY_PYTHON="${QUERY_PYTHON:-python3}"
QUERY_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
QUERY_TMP="$(mktemp -d)"
trap 'rm -rf -- "${QUERY_TMP}"' EXIT

# Resolve the candidate wheel (or an exact public release) and all dependencies normally.
"${QUERY_PYTHON}" -m venv "${QUERY_TMP}/venv"
"${QUERY_TMP}/venv/bin/python" "${QUERY_ROOT}/scripts/install_validation.py" "${2:-current}" \
  "${1:-${QUERY_ROOT}/dist/meridian_storage_query-1.0.3-py3-none-any.whl[test]}"
cd "${QUERY_TMP}"
unset PYTHONPATH
"${QUERY_TMP}/venv/bin/python" - <<'PY'
from importlib.metadata import version
from pathlib import Path

import meridian_storage.query

assert "site-packages" in Path(meridian_storage.query.__file__).parts
for package in ("core", "semantics", "query"):
    print(f"meridian-storage-{package}=={version(f'meridian-storage-{package}')}")
PY
"${QUERY_TMP}/venv/bin/python" -m pytest -o pythonpath= --import-mode=importlib \
  "${QUERY_ROOT}/tests/unit" "${QUERY_ROOT}/tests/contract" \
  "${QUERY_ROOT}/tests/property" "${QUERY_ROOT}/tests/integration" \
  "${QUERY_ROOT}/tests/conformance"
