# SPDX-License-Identifier: Apache-2.0
"""Language-neutral wire contract and public-surface tests."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

import meridian_storage.query as query_api
from meridian_storage.query import (
    QueryOperation,
    query_capability_contract,
    query_cursor_contract,
    query_operation_contract,
    query_public_api_contract,
)

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = ROOT / "contracts" / "conformance" / "valid" / "basic-scan.json"


@pytest.mark.contract
def test_packaged_contracts_are_valid_draft_2020_12_schemas() -> None:
    contracts = (
        query_operation_contract(),
        query_capability_contract(),
        query_cursor_contract(),
    )
    for contract in contracts:
        Draft202012Validator.check_schema(contract)
        assert contract["$schema"] == "https://json-schema.org/draft/2020-12/schema"


@pytest.mark.contract
def test_golden_plan_round_trip_and_fingerprint() -> None:
    mapping = json.loads(GOLDEN.read_text(encoding="utf-8"))
    Draft202012Validator(query_operation_contract()).validate(mapping)
    operation = QueryOperation.from_mapping(mapping)
    assert operation.to_dict() == mapping
    assert operation.fingerprint == (
        "sha256:e60e5dc1d8923a169dc424c00b3010853700a96c121bc494cd843053361a2daa"
    )


@pytest.mark.contract
def test_native_query_is_not_a_v1_operation_or_extension() -> None:
    mapping = json.loads(GOLDEN.read_text(encoding="utf-8"))
    native_operation = copy.deepcopy(mapping)
    native_operation["operation"] = "NativeQuery"
    assert list(Draft202012Validator(query_operation_contract()).iter_errors(native_operation))
    with pytest.raises(ValueError, match="unsupported"):
        QueryOperation.from_mapping(native_operation)

    native_extension = copy.deepcopy(mapping)
    native_extension["extensions"] = {"example.org/native-sql": "SELECT 1"}
    with pytest.raises(ValueError, match="policy-enabled"):
        QueryOperation.from_mapping(native_extension)


@pytest.mark.contract
def test_schema_closes_root_options_and_geometry_shape() -> None:
    contract = Draft202012Validator(query_operation_contract())
    mapping = json.loads(GOLDEN.read_text(encoding="utf-8"))
    unsafe = copy.deepcopy(mapping)
    unsafe["options"]["engine"] = "postgresql"
    assert list(contract.iter_errors(unsafe))

    point = copy.deepcopy(mapping)
    point["filter"] = {
        "kind": "eq",
        "left": {"kind": "field", "name": "location"},
        "right": {"kind": "point", "coordinates": [1], "crs": "EPSG:4326"},
    }
    assert list(contract.iter_errors(point))


@pytest.mark.contract
def test_public_manifest_pins_authoritative_design_and_catalog_boundary() -> None:
    manifest = query_public_api_contract()
    assert manifest["package"] == "meridian-storage-query"
    assert manifest["catalogsOwned"] == []
    assert manifest["catalogsUsed"] == ["structured", "evidence"]
    assert manifest["design"] == {
        "hldRevision": 56,
        "catalogsRevision": 70,
        "queryLldRevision": 46,
        "adapterRevision": 24,
    }
    assert "NativeQuery" in manifest["forbiddenPublicConcepts"]


@pytest.mark.contract
def test_public_python_surface_contains_no_native_or_engine_selection_api() -> None:
    names = set(query_api.__all__)
    assert not any("NativeQuery" in name or "Engine" in name for name in names)
    assert "MeridianQuery" in names
    assert "QueryOperation" in names
    assert query_api.__version__ == "1.0.3"
