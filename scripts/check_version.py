# SPDX-License-Identifier: Apache-2.0
"""Verify that a release tag matches every public version ledger."""

from __future__ import annotations

import argparse
import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("tag")
    arguments = parser.parse_args()
    match = re.fullmatch(r"v(\d+\.\d+\.\d+)", arguments.tag)
    if match is None:
        raise SystemExit("release tag must be vMAJOR.MINOR.PATCH")
    expected = match.group(1)
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    compatibility = json.loads((ROOT / "compatibility.json").read_text(encoding="utf-8"))
    public_api = json.loads(
        (ROOT / "contracts/public-api/meridian-query.v1.json").read_text(encoding="utf-8")
    )
    version_source = (ROOT / "src/meridian_storage/query/_version.py").read_text(encoding="utf-8")
    versions = {
        pyproject["project"]["version"],
        compatibility["version"],
        public_api["version"],
        re.search(r'__version__ = "([^"]+)"', version_source).group(1),  # type: ignore[union-attr]
    }
    if versions != {expected}:
        raise SystemExit(f"release versions do not match {expected}: {sorted(versions)}")


if __name__ == "__main__":
    main()
