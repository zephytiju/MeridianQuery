# SPDX-License-Identifier: Apache-2.0
"""Verify wheel/sdist identity, metadata, contents, and licensing."""

from __future__ import annotations

import argparse
import hashlib
import json
import tarfile
import zipfile
from email.parser import BytesParser
from pathlib import Path

PACKAGE = "meridian-storage-query"
VERSION = "1.0.1"
REQUIRED_DEPENDENCIES = {
    "meridian-storage-core==1.0.1",
    "meridian-storage-semantics==2.0.0",
}


def _one(values: list[Path], name: str) -> Path:
    if len(values) != 1:
        raise SystemExit(f"expected exactly one {name}, found {[item.name for item in values]}")
    return values[0]


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_wheel(path: Path) -> dict[str, object]:
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        metadata_name = _one(
            [Path(name) for name in names if name.endswith(".dist-info/METADATA")],
            "wheel METADATA",
        ).as_posix()
        metadata = BytesParser().parsebytes(archive.read(metadata_name))
        if metadata["Name"] != PACKAGE or metadata["Version"] != VERSION:
            raise SystemExit("wheel Name/Version metadata does not match the release")
        if metadata["License-Expression"] != "Apache-2.0":
            raise SystemExit("wheel lacks the Apache-2.0 License-Expression")
        dependencies = set(metadata.get_all("Requires-Dist", ()))
        if not dependencies >= REQUIRED_DEPENDENCIES:
            raise SystemExit(f"wheel dependency pins are incomplete: {sorted(dependencies)}")
        required = {
            "meridian_storage/query/__init__.py",
            "meridian_storage/query/py.typed",
            "meridian_storage/query/contracts/logical-query/meridian.operation.query.v1.schema.json",
            "meridian_storage/query/contracts/public-api/meridian-query.v1.json",
            "meridian_storage/query/compatibility.json",
        }
        missing = required - names
        if missing:
            raise SystemExit(f"wheel is missing required package data: {sorted(missing)}")
        license_names = {name for name in names if name.endswith(("/LICENSE", "/NOTICE"))}
        if not any(name.endswith("/LICENSE") for name in license_names) or not any(
            name.endswith("/NOTICE") for name in license_names
        ):
            raise SystemExit("wheel does not contain LICENSE and NOTICE")
        foreign = {
            name
            for name in names
            if name.startswith("meridian_storage/")
            and not name.startswith("meridian_storage/query/")
        }
        if foreign:
            raise SystemExit(f"wheel contains a second package: {sorted(foreign)}")
    return {"file": path.name, "sha256": _digest(path), "bytes": path.stat().st_size}


def verify_sdist(path: Path) -> dict[str, object]:
    with tarfile.open(path, "r:gz") as archive:
        names = set(archive.getnames())
        roots = {name.split("/", 1)[0] for name in names}
        if len(roots) != 1:
            raise SystemExit("sdist must contain exactly one archive root")
        root = next(iter(roots))
        required = {
            f"{root}/LICENSE",
            f"{root}/NOTICE",
            f"{root}/README.md",
            f"{root}/pyproject.toml",
            f"{root}/src/meridian_storage/query/__init__.py",
            f"{root}/contracts/logical-query/meridian.operation.query.v1.schema.json",
            f"{root}/tests/contract/test_contracts.py",
        }
        missing = required - names
        if missing:
            raise SystemExit(f"sdist is missing required evidence: {sorted(missing)}")
    return {"file": path.name, "sha256": _digest(path), "bytes": path.stat().st_size}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("dist", type=Path)
    arguments = parser.parse_args()
    wheel = _one(sorted(arguments.dist.glob("*.whl")), "wheel")
    sdist = _one(sorted(arguments.dist.glob("*.tar.gz")), "sdist")
    evidence = {
        "formatVersion": "meridian.release.artifacts.v1",
        "package": PACKAGE,
        "version": VERSION,
        "artifacts": [verify_wheel(wheel), verify_sdist(sdist)],
    }
    print(json.dumps(evidence, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
