# SPDX-License-Identifier: Apache-2.0
"""Integration with the released Core and Semantics public API contracts."""

from __future__ import annotations

from collections.abc import Callable
from importlib.metadata import version

import meridian_storage.semantics
import pytest
from meridian_storage.semantics import SchemaDocument
from packaging.specifiers import SpecifierSet

import meridian_storage
from meridian_storage import Expression, Operation, ResourceRef
from meridian_storage.query import (
    RegistryView,
    infer_requirements,
    normalize_expression,
    query,
    validate_compile,
)


@pytest.mark.integration
def test_compatible_released_dependencies_are_loaded() -> None:
    assert version("meridian-storage-core") in SpecifierSet(">=1.0.1,<2")
    assert version("meridian-storage-semantics") in SpecifierSet(">=2.0.0,<3")
    assert "site-packages" in str(meridian_storage.__file__)
    assert "site-packages" in str(meridian_storage.semantics.__file__)


@pytest.mark.integration
def test_public_core_expression_normalizes_and_validates_against_semantics(
    users: ResourceRef,
    registry_factory: Callable[..., RegistryView],
) -> None:
    expression = Expression(
        "structured",
        "query",
        {
            "resource": users.to_dict(),
            "where": {"age": {"$gte": 21}},
            "select": ["id", "name"],
            "orderBy": ["name"],
            "limit": 50,
        },
    )
    operation = normalize_expression(expression)
    validation = validate_compile(operation, registry_factory((users,)))
    assert isinstance(validation.resolved_resources[0].schema, SchemaDocument)
    assert validation.requirements.operation_contract == "meridian.structured.query"


@pytest.mark.integration
def test_query_plan_embeds_in_released_core_operation(users: ResourceRef) -> None:
    query_operation = query(users).where({"id": "u1"}).build()
    requirements = infer_requirements(query_operation)
    core_operation = query_operation.to_core_operation((requirements.to_core_requirement(),))
    rebuilt = Operation.from_mapping(core_operation.to_dict())
    assert rebuilt.operation_contract == "meridian.structured.query"
    assert rebuilt.operation_version == "1.0.0"
    assert rebuilt.read_only is True
    assert rebuilt.resources == (users,)


@pytest.mark.integration
def test_evidence_query_uses_same_reusable_operation_contract() -> None:
    resource = ResourceRef("evidence", "runtime", "logs")
    expression = Expression(
        "evidence",
        "query",
        {
            "resource": resource.to_dict(),
            "where": {"severity": {"$in": ["ERROR", "WARN"]}},
            "limit": 25,
        },
    )
    operation = normalize_expression(expression)
    assert operation.catalog == "evidence"
    assert operation.to_core_operation().operation_contract == "meridian.evidence.query"
