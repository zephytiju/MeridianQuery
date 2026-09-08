#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
set -euo pipefail

QUERY_PYTHON="${QUERY_PYTHON:-python3}"

"${QUERY_PYTHON}" -m ruff format --check src tests scripts
"${QUERY_PYTHON}" -m ruff check src tests scripts
"${QUERY_PYTHON}" -m mypy src
"${QUERY_PYTHON}" -m build
MERIDIAN_DIST_DIR=dist "${QUERY_PYTHON}" -m pytest --cov=meridian_storage.query --cov-report=term-missing
"${QUERY_PYTHON}" scripts/verify_artifacts.py dist
