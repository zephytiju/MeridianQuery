# SPDX-License-Identifier: Apache-2.0
"""Shared released-Core/Semantics fixtures for query tests."""

from __future__ import annotations

from collections.abc import Callable, Mapping

import pytest
from meridian_storage.semantics import (
    FieldDefinition,
    LogicalKind,
    LogicalType,
    RelationalProfile,
    SchemaDocument,
    SchemaReference,
    SemanticKind,
)

from meridian_storage import ResourceRef
from meridian_storage.query import RegistryView, ResolvedResource

PROFILE_EXTENSION_KEY = "org.meridian.profile/v1"
FINGERPRINT = "sha256:" + "1" * 64


def standard_fields() -> tuple[FieldDefinition, ...]:
    return (
        FieldDefinition("id", LogicalType(LogicalKind.STRING), mutable=False),
        FieldDefinition("active", LogicalType(LogicalKind.BOOLEAN)),
        FieldDefinition("age", LogicalType(LogicalKind.INT64)),
        FieldDefinition("body", LogicalType(LogicalKind.STRING)),
        FieldDefinition("doc", LogicalType(LogicalKind.JSON)),
        FieldDefinition("location", LogicalType(LogicalKind.WGS84_POINT)),
        FieldDefinition("name", LogicalType(LogicalKind.STRING)),
        FieldDefinition("series", LogicalType(LogicalKind.STRING)),
        FieldDefinition("ts", LogicalType(LogicalKind.UTC_TIMESTAMP)),
        FieldDefinition("value", LogicalType(LogicalKind.FLOAT64)),
    )


@pytest.fixture
def schema_factory() -> Callable[..., SchemaDocument]:
    def make_schema(
        resource: ResourceRef,
        *,
        kind: SemanticKind = SemanticKind.RELATIONAL,
        profile: object | None = None,
        fields: tuple[FieldDefinition, ...] | None = None,
        identity: tuple[str, ...] = ("id",),
        version: str = "1.0.0",
    ) -> SchemaDocument:
        selected_profile = profile or RelationalProfile()
        to_dict = selected_profile.to_dict
        return SchemaDocument(
            SchemaReference(
                resource.catalog,
                resource.namespace,
                resource.name,
                version,
            ),
            kind,
            fields or standard_fields(),
            identity,
            extensions={PROFILE_EXTENSION_KEY: to_dict()},
        )

    return make_schema


@pytest.fixture
def users() -> ResourceRef:
    return ResourceRef("structured", "tests", "users")


@pytest.fixture
def registry_factory(
    schema_factory: Callable[..., SchemaDocument],
) -> Callable[..., RegistryView]:
    def make_registry(
        resources: tuple[ResourceRef, ...],
        *,
        bindings: Mapping[ResourceRef, str] | None = None,
        schemas: Mapping[ResourceRef, SchemaDocument] | None = None,
        scopes: Mapping[ResourceRef, tuple[str, ...]] | None = None,
        fingerprint: str = FINGERPRINT,
    ) -> RegistryView:
        binding_map = bindings or {}
        schema_map = schemas or {}
        scope_map = scopes or {}
        resolved = {
            resource: ResolvedResource(
                resource,
                schema_map.get(resource, schema_factory(resource)),
                binding_map.get(resource, "binding-primary"),
                scope_map.get(resource, ()),
            )
            for resource in resources
        }
        return RegistryView(resolved, fingerprint, 1)

    return make_registry
