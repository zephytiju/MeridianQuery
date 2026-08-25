# SPDX-License-Identifier: Apache-2.0
"""Require Apache-2.0 SPDX headers on comment-capable repository sources."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SUFFIXES = {".py", ".sh", ".md", ".toml", ".yml", ".yaml"}
IGNORED_PARTS = {".git", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".venv", "dist"}


def main() -> None:
    missing: list[str] = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.suffix not in SUFFIXES:
            continue
        if any(part in IGNORED_PARTS for part in path.parts):
            continue
        head = "\n".join(path.read_text(encoding="utf-8").splitlines()[:3])
        if "SPDX-License-Identifier: Apache-2.0" not in head:
            missing.append(path.relative_to(ROOT).as_posix())
    if missing:
        raise SystemExit(f"files missing SPDX Apache-2.0 headers: {missing}")


if __name__ == "__main__":
    main()
