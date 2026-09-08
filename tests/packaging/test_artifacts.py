# SPDX-License-Identifier: Apache-2.0
"""Repository and built-artifact acceptance checks."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.packaging
def test_version_ledgers_match_release() -> None:
    subprocess.run(
        [sys.executable, "scripts/check_version.py", "v1.0.3"],
        cwd=ROOT,
        check=True,
    )


@pytest.mark.packaging
def test_comment_capable_files_have_spdx_headers() -> None:
    subprocess.run([sys.executable, "scripts/check_spdx.py"], cwd=ROOT, check=True)


@pytest.mark.packaging
def test_built_artifacts_when_dist_is_supplied() -> None:
    dist = os.environ.get("MERIDIAN_DIST_DIR")
    if dist is None:
        pytest.skip("set MERIDIAN_DIST_DIR after building artifacts")
    subprocess.run(
        [sys.executable, "scripts/verify_artifacts.py", dist],
        cwd=ROOT,
        check=True,
    )
