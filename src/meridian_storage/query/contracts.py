# SPDX-License-Identifier: Apache-2.0
"""Access to packaged language-neutral query contracts."""

from __future__ import annotations

import json
from collections.abc import Mapping
from importlib import resources
from pathlib import Path
from typing import cast


def _contract(relative: str) -> Mapping[str, object]:
    packaged = resources.files("meridian_storage.query").joinpath("contracts", relative)
    if packaged.is_file():
        return cast(Mapping[str, object], json.loads(packaged.read_text(encoding="utf-8")))
    source = Path(__file__).resolve().parents[3] / "contracts" / relative
    return cast(Mapping[str, object], json.loads(source.read_text(encoding="utf-8")))


def query_operation_contract() -> Mapping[str, object]:
    return _contract("logical-query/meridian.operation.query.v1.schema.json")


def query_capability_contract() -> Mapping[str, object]:
    return _contract("logical-query/meridian.query.capabilities.v1.schema.json")


def query_cursor_contract() -> Mapping[str, object]:
    return _contract("logical-query/meridian.query.cursor.v1.schema.json")


def query_public_api_contract() -> Mapping[str, object]:
    return _contract("public-api/meridian-query.v1.json")


__all__ = [
    "query_capability_contract",
    "query_cursor_contract",
    "query_operation_contract",
    "query_public_api_contract",
]
