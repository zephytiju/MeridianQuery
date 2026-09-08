# SPDX-License-Identifier: Apache-2.0
"""Normally resolve a candidate against an exact, hash-verified public dependency selection."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("profile", choices=("legacy", "current"))
    parser.add_argument("target", nargs="?", default=".[test]")
    args = parser.parse_args()
    lock = json.loads((ROOT / "contracts/release-validation/dependencies.json").read_text())
    packages = lock["profiles"][args.profile]
    requirements = []
    for package in packages:
        wheel = next(item for item in package["artifacts"] if item["filename"].endswith(".whl"))
        requirements.append(f"{package['name']} @ {wheel['url']}#sha256={wheel['sha256']}")
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--index-url",
            "https://pypi.org/simple",
            args.target,
            *requirements,
        ],
        check=True,
    )
    subprocess.run([sys.executable, "-m", "pip", "check"], check=True)
    for package in packages:
        actual = version(package["name"])
        if actual != package["version"]:
            raise SystemExit(f"selected dependency drift: {package['name']}=={actual}")
        print(f"{package['name']}=={actual}")


if __name__ == "__main__":
    main()
