#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
set -euo pipefail

QUERY_PYTHON="${QUERY_PYTHON:-python3}"
QUERY_TMP="$(mktemp -d)"
trap 'rm -rf -- "${QUERY_TMP}"' EXIT

export SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-1787694698}"

"${QUERY_PYTHON}" -m build --outdir "${QUERY_TMP}/first"
"${QUERY_PYTHON}" -m build --outdir "${QUERY_TMP}/second"

FIRST_SUMS="${QUERY_TMP}/first.sha256"
SECOND_SUMS="${QUERY_TMP}/second.sha256"
(
  cd "${QUERY_TMP}/first"
  shasum -a 256 ./* | sed 's#  \./#  #' > "${FIRST_SUMS}"
)
(
  cd "${QUERY_TMP}/second"
  shasum -a 256 ./* | sed 's#  \./#  #' > "${SECOND_SUMS}"
)
diff -u "${FIRST_SUMS}" "${SECOND_SUMS}"

mkdir -p dist
cp "${QUERY_TMP}/first/"* dist/
"${QUERY_PYTHON}" scripts/verify_artifacts.py dist
