# SPDX-License-Identifier: Apache-2.0
"""Compile-time and startup validation tests."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from meridian_storage.registry.resources import CapabilityRequirement
from meridian_storage.semantics import (
    FieldDefinition,
    FullTextProfile,
    GeospatialProfile,
    LogicalKind,
    LogicalType,
    RecordReference,
    RelationProfile,
    ResourceReference,
    SchemaDocument,
    SemanticKind,
    TimeSeriesProfile,
    TraversalResolution,
)
from meridian_storage.spi import (
    AdapterDescriptor,
    CapabilityManifest,
    OperationCapability,
)

from meridian_storage import ResourceRef, SchemaRef
from meridian_storage.query import (
    CrossBindingOperation,
    ExcessiveBudget,
    FieldTypeMismatch,
    InvalidTraversal,
    NondeterministicOrdering,
    QueryOperation,
    QueryTarget,
    QueryValidationError,
    RegistryView,
    ResolvedResource,
    SafetyBudget,
    StaleRegistryPlan,
    StartupResourceRequirement,
    UnknownResource,
    UnknownSchema,
    distance_within,
    field,
    normalize_builder,
    point,
    query,
    validate_compile,
    validate_startup,
)

FINGERPRINT = "sha256:" + "1" * 64
OTHER_FINGERPRINT = "sha256:" + "3" * 64


def test_compile_resolves_one_binding_appends_identity_and_infers_requirements(
    users: ResourceRef,
    registry_factory: Callable[..., RegistryView],
) -> None:
    operation = query(users).where({"age": {"$gte": 18}}).order_by("age").build()
    validation = validate_compile(operation, registry_factory((users,)))
    assert validation.binding_id == "binding-primary"
    assert [item.expression.to_dict()["name"] for item in validation.operation.order] == [
        "age",
        "id",
    ]
    assert any(
        item.code == "MERIDIAN_QUERY_ORDER_TIEBREAKER_APPENDED" for item in validation.diagnostics
    )
    ids = {item.semantic_id for item in validation.requirements.requirements}
    assert {"query.operation.scan", "query.operator.gte", "query.pagination.keyset"} <= ids


def test_unknown_resource_and_schema_are_deterministic(
    users: ResourceRef,
    registry_factory: Callable[..., RegistryView],
) -> None:
    operation = query(users).build()
    with pytest.raises(UnknownResource) as missing:
        validate_compile(operation, registry_factory(()))
    assert missing.value.code == "MERIDIAN_QUERY_UNKNOWN_RESOURCE"

    wrong = QueryOperation(
        "structured",
        (QueryTarget(users, SchemaRef("structured", "tests", "users", "2.0.0")),),
        "scan",
    )
    with pytest.raises(UnknownSchema) as stale:
        validate_compile(wrong, registry_factory((users,)))
    assert stale.value.code == "MERIDIAN_QUERY_UNKNOWN_SCHEMA"


def test_cross_binding_join_is_rejected(
    users: ResourceRef,
    registry_factory: Callable[..., RegistryView],
) -> None:
    teams = ResourceRef("structured", "tests", "teams")
    operation = (
        query(users)
        .join(
            teams,
            on=field("id", users.canonical).eq(field("id", teams.canonical)),
        )
        .build()
    )
    registry = registry_factory(
        (users, teams),
        bindings={users: "binding-a", teams: "binding-b"},
    )
    with pytest.raises(CrossBindingOperation) as failure:
        validate_compile(operation, registry)
    assert failure.value.code == "MERIDIAN_QUERY_CROSS_BINDING"


@pytest.mark.parametrize(
    "operation_factory",
    [
        lambda resource: query(resource).where({"missing": 1}).build(),
        lambda resource: query(resource).where({"age": {"$prefix": "1"}}).build(),
        lambda resource: query(resource).where(field("age").eq("eighteen")).build(),
        lambda resource: query(resource).where(field("name").lt(3)).build(),
        lambda resource: query(resource).where(field("name").in_([1, 2])).build(),
        lambda resource: (
            query(resource)
            .where(field("name").eq("x").and_(field("age").eq(1)))
            .join(
                ResourceRef("structured", "tests", "teams"),
                on=field("id").eq(field("id", "structured:tests.teams")),
            )
            .build()
        ),
    ],
)
def test_field_and_type_mismatches_are_rejected(
    operation_factory: Callable[[ResourceRef], QueryOperation],
    users: ResourceRef,
    registry_factory: Callable[..., RegistryView],
) -> None:
    operation = operation_factory(users)
    resources = operation.resources
    with pytest.raises(FieldTypeMismatch) as failure:
        validate_compile(operation, registry_factory(resources))
    assert failure.value.code == "MERIDIAN_QUERY_FIELD_TYPE_MISMATCH"


def test_identity_is_required_for_live_keyset(
    users: ResourceRef,
    schema_factory: Callable[..., SchemaDocument],
) -> None:
    schema = schema_factory(users, identity=())
    registry = RegistryView(
        {users: ResolvedResource(users, schema, "binding")},
        FINGERPRINT,
        1,
    )
    with pytest.raises(NondeterministicOrdering):
        validate_compile(query(users).build(), registry)


def test_scope_injection_is_nonremovable(
    users: ResourceRef,
    registry_factory: Callable[..., RegistryView],
) -> None:
    registry = registry_factory((users,), scopes={users: ("tenant",)})
    with pytest.raises(QueryValidationError) as missing_scope:
        validate_compile(query(users).build(), registry)
    assert any(
        item.code == "MERIDIAN_QUERY_SCOPE_REQUIRED" for item in missing_scope.value.diagnostics
    )

    scoped = normalize_builder(query(users), scope_filter={"id": "tenant-a"})
    assert validate_compile(scoped, registry).operation.options["scopeInjected"] is True


def test_reviewed_workload_profile_bounds_are_enforced(
    users: ResourceRef,
    registry_factory: Callable[..., RegistryView],
) -> None:
    elevated = SafetyBudget(max_result_values=1_000)
    with pytest.raises(ExcessiveBudget, match="reviewed profile"):
        validate_compile(query(users).budget(elevated).build(), registry_factory((users,)))

    named = SafetyBudget(max_result_values=1_000, workload_profile="batch")
    base = registry_factory((users,))
    registry = RegistryView(
        base.resources,
        base.registry_fingerprint,
        base.registry_revision,
        {"batch": named, "default": SafetyBudget()},
    )
    assert validate_compile(query(users).budget(named).page(size=500).build(), registry)

    unknown = SafetyBudget(workload_profile="unreviewed")
    with pytest.raises(ExcessiveBudget, match="not registered"):
        validate_compile(query(users).budget(unknown).build(), base)


def test_query_values_cannot_exceed_selected_budget(
    users: ResourceRef,
    registry_factory: Callable[..., RegistryView],
) -> None:
    budget = SafetyBudget(max_membership_names=2)
    operation = query(users).budget(budget).where({"id": {"$in": ["a", "b", "c"]}}).build()
    with pytest.raises(ExcessiveBudget, match="membership"):
        validate_compile(operation, registry_factory((users,)))


def test_full_text_requires_search_profile(
    users: ResourceRef,
    schema_factory: Callable[..., SchemaDocument],
) -> None:
    operation = query(users).full_text("query", fields=("body",)).build()
    relational = schema_factory(users)
    bad_registry = RegistryView(
        {users: ResolvedResource(users, relational, "binding")}, FINGERPRINT, 1
    )
    with pytest.raises(FieldTypeMismatch, match="full-text"):
        validate_compile(operation, bad_registry)

    search_schema = schema_factory(
        users,
        kind=SemanticKind.SEARCH,
        profile=FullTextProfile(("body",), facets=("name",), highlights=("body",)),
    )
    good_registry = RegistryView(
        {users: ResolvedResource(users, search_schema, "binding")}, FINGERPRINT, 1
    )
    assert validate_compile(operation, good_registry)


def test_search_operation_without_full_text_is_rejected(
    users: ResourceRef,
    schema_factory: Callable[..., SchemaDocument],
) -> None:
    schema = schema_factory(
        users,
        kind=SemanticKind.SEARCH,
        profile=FullTextProfile(("body",)),
    )
    registry = RegistryView({users: ResolvedResource(users, schema, "binding")}, FINGERPRINT, 1)
    operation = QueryOperation(
        "structured",
        (QueryTarget(users),),
        "search",
        result=query(users).full_text("x", fields=("body",)).build().result,
    )
    with pytest.raises(FieldTypeMismatch, match="full-text expression"):
        validate_compile(operation, registry)


def test_geospatial_and_time_series_profiles(
    users: ResourceRef,
    schema_factory: Callable[..., SchemaDocument],
) -> None:
    geo_operation = query(users).where(field("location").eq(point(-122.4, 37.8))).build()
    distance_operation = (
        query(users).where(distance_within(field("location"), point(-122.4, 37.8), 100)).build()
    )
    geo_schema = schema_factory(
        users,
        kind=SemanticKind.GEOSPATIAL,
        profile=GeospatialProfile(("location",)),
    )
    geo_registry = RegistryView(
        {users: ResolvedResource(users, geo_schema, "binding")}, FINGERPRINT, 1
    )
    assert validate_compile(geo_operation, geo_registry)
    assert validate_compile(distance_operation, geo_registry)

    time_operation = query(users).time_range("ts", start=datetime(2026, 1, 1, tzinfo=UTC)).build()
    time_schema = schema_factory(
        users,
        kind=SemanticKind.TIME_SERIES,
        profile=TimeSeriesProfile("ts", ("series",), (), ("value",)),
    )
    time_registry = RegistryView(
        {users: ResolvedResource(users, time_schema, "binding")}, FINGERPRINT, 1
    )
    assert validate_compile(time_operation, time_registry)


def _relation_schema(
    relation: ResourceRef,
    users: ResourceRef,
    schema_factory: Callable[..., SchemaDocument],
) -> SchemaDocument:
    record = LogicalType(LogicalKind.RECORD_REF)
    return schema_factory(
        relation,
        kind=SemanticKind.RELATION,
        profile=RelationProfile(
            "source",
            "target",
            True,
            (ResourceReference(users.catalog, users.namespace, users.name),),
            (ResourceReference(users.catalog, users.namespace, users.name),),
        ),
        fields=(
            FieldDefinition("id", LogicalType(LogicalKind.STRING), mutable=False),
            FieldDefinition("source", record),
            FieldDefinition("target", record),
            FieldDefinition("active", LogicalType(LogicalKind.BOOLEAN)),
        ),
    )


def test_traversal_validation_and_registry_fingerprint(
    users: ResourceRef,
    schema_factory: Callable[..., SchemaDocument],
) -> None:
    follows = ResourceRef("structured", "tests", "follows")
    schemas = {
        users: schema_factory(users),
        follows: _relation_schema(follows, users, schema_factory),
    }
    registry = RegistryView(
        {key: ResolvedResource(key, value, "binding") for key, value in schemas.items()},
        FINGERPRINT,
        1,
    )
    start = RecordReference(ResourceReference("structured", "tests", "users"), "u1")
    explicit = (
        query(users)
        .traverse(
            start=start,
            relation_collections=(follows,),
            max_depth=3,
        )
        .build()
    )
    assert validate_compile(explicit, registry)

    resolved = normalize_builder(
        query(users).traverse(start=start, all_neighbors=True),
        all_neighbors_resolver=lambda resource, depth: TraversalResolution(
            resource,
            (ResourceReference("structured", "tests", "follows"),),
            FINGERPRINT,
            depth,
        ),
    )
    assert validate_compile(resolved, registry)
    stale_registry = RegistryView(registry.resources, OTHER_FINGERPRINT, 2)
    with pytest.raises(StaleRegistryPlan):
        validate_compile(resolved, stale_registry)


def test_invalid_traversal_start_and_relation_profile(
    users: ResourceRef,
    schema_factory: Callable[..., SchemaDocument],
) -> None:
    follows = ResourceRef("structured", "tests", "follows")
    wrong_start = RecordReference(ResourceReference("structured", "tests", "other"), "u1")
    operation = (
        query(users)
        .traverse(
            start=wrong_start,
            relation_collections=(follows,),
        )
        .build()
    )
    registry = RegistryView(
        {
            users: ResolvedResource(users, schema_factory(users), "binding"),
            follows: ResolvedResource(follows, schema_factory(follows), "binding"),
        },
        FINGERPRINT,
        1,
    )
    with pytest.raises(InvalidTraversal):
        validate_compile(operation, registry)


def _manifest(*, page_size: int) -> CapabilityManifest:
    capability = OperationCapability(
        "meridian.structured.query",
        ("1.0.0",),
        guarantees=("single-binding",),
        limits={"pageSize": page_size},
    )
    descriptor = AdapterDescriptor(
        "test.adapter",
        "1.0.0",
        "test-driver",
        {"test": ("1",)},
        (capability,),
    )
    return CapabilityManifest(descriptor, "test", "1")


def test_startup_validation_uses_authenticated_capability_manifests(users: ResourceRef) -> None:
    requirement = StartupResourceRequirement(
        users,
        "binding",
        (
            CapabilityRequirement(
                "meridian.structured.query",
                "1.0.0",
                guarantees=("single-binding",),
                minimum_limits={"pageSize": 50},
            ),
        ),
    )
    missing = validate_startup((requirement,), {})
    assert missing.ready is False
    with pytest.raises(Exception, match="startup requirements"):
        missing.require_ready()

    insufficient = validate_startup((requirement,), {"binding": _manifest(page_size=10)})
    assert insufficient.ready is False
    ready = validate_startup((requirement,), {"binding": _manifest(page_size=100)})
    assert ready.ready is True
    ready.require_ready()
