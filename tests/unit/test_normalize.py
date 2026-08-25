# SPDX-License-Identifier: Apache-2.0
"""Catalog Expression normalization tests."""

from __future__ import annotations

import pytest
from meridian_storage.semantics import (
    RecordReference,
    ResourceReference,
    TraversalResolution,
)

from meridian_storage import Expression, ResourceRef, SchemaRef
from meridian_storage.query import (
    Aggregate,
    ExtensionPolicy,
    Field,
    QueryOperation,
    field,
    normalize_builder,
    normalize_expression,
    query,
)

FINGERPRINT = "sha256:" + "2" * 64


def test_normalize_mapping_first_query_and_schema_resolution(users: ResourceRef) -> None:
    schema = SchemaRef("structured", "tests", "users", "1.0.0")
    expression = Expression(
        "structured",
        "query",
        {
            "resource": users.to_dict(),
            "where": {"age": {"$gte": 18}},
            "select": ["id", {"expression": field("name").to_dict(), "alias": "display"}],
            "orderBy": ["-age", {"field": "id", "direction": "asc", "nulls": "last"}],
            "limit": 20,
            "pointInTime": True,
        },
    )
    operation = normalize_expression(expression, schema_resolver=lambda _: schema)
    assert operation.targets[0].schema == schema
    assert operation.operation == "scan"
    assert operation.page.size == 20
    assert operation.page.point_in_time is True
    assert operation.filter is not None and "gte" in operation.filter.operators
    assert [item.alias for item in operation.result.projection] == [None, "display"]
    assert [item.direction for item in operation.order] == ["desc", "asc"]


def test_get_and_evidence_query_normalization() -> None:
    evidence = ResourceRef("evidence", "runtime", "logs")
    get = normalize_expression(Expression("structured", "get", {"resource": "tests.users"}))
    scan = normalize_expression(
        Expression("evidence", "query", {"resource": evidence.to_dict(), "limit": 10})
    )
    assert get.operation == "get"
    assert get.targets[0].resource == ResourceRef("structured", "tests", "users")
    assert scan.catalog == "evidence"
    assert scan.operation == "scan"


def test_search_builder_expression_round_trip(users: ResourceRef) -> None:
    built = (
        query(users)
        .where({"active": True})
        .full_text("storage", fields=("body",), highlights=("body",), facets=("name",))
        .order_by("id")
        .page(size=12)
    )
    direct = built.build()
    normalized = normalize_expression(built.expression())
    assert normalized.to_dict() == direct.to_dict()


def test_aggregate_builder_expression_round_trip(users: ResourceRef) -> None:
    built = query(users).where({"active": True}).group_by("active").measure("average", "avg", "age")
    assert normalize_expression(built.expression()).to_dict() == built.build().to_dict()


def test_join_builder_expression_round_trip(users: ResourceRef) -> None:
    teams = ResourceRef("structured", "tests", "teams")
    built = query(users).join(
        teams,
        on=field("team_id", users.canonical).eq(field("id", teams.canonical)),
        kind="left",
    )
    normalized = normalize_expression(built.expression())
    assert normalized.to_dict() == built.build().to_dict()


def test_join_schema_resolution(users: ResourceRef) -> None:
    teams = ResourceRef("structured", "tests", "teams")
    schema = SchemaRef("structured", "tests", "teams", "1.0.0")
    expression = (
        query(users)
        .join(
            teams,
            on=field("id", users.canonical).eq(field("id", teams.canonical)),
        )
        .expression()
    )
    normalized = normalize_expression(
        expression,
        schema_resolver=lambda resource: schema if resource == teams else None,
    )
    assert normalized.joins[0].target.schema == schema


def test_explicit_traversal_builder_expression_round_trip(users: ResourceRef) -> None:
    follows = ResourceRef("structured", "tests", "follows")
    start = RecordReference(ResourceReference("structured", "tests", "users"), "u1")
    built = query(users).traverse(
        start=start,
        relation_collections=(follows,),
        direction="any",
        min_depth=2,
        max_depth=4,
        record_where={"active": True},
        relation_where={follows.canonical: {"active": True}},
        result_shape="paths",
    )
    assert normalize_expression(built.expression()).to_dict() == built.build().to_dict()


def test_all_neighbors_resolution_and_scope_injection(users: ResourceRef) -> None:
    follows = ResourceReference("structured", "tests", "follows")
    start = RecordReference(ResourceReference("structured", "tests", "users"), "u1")
    calls: list[tuple[ResourceReference, int]] = []

    def resolve(resource: ResourceReference, depth: int) -> TraversalResolution:
        calls.append((resource, depth))
        return TraversalResolution(resource, (follows,), FINGERPRINT, depth)

    built = query(users).traverse(start=start, all_neighbors=True, max_depth=3)
    operation = normalize_builder(
        built,
        all_neighbors_resolver=resolve,
        scope_filter={"tenant_id": "tenant-a"},
    )
    assert calls == [(start.collection_ref, 3)]
    assert operation.traversal is not None
    assert operation.traversal.registry_fingerprint == FINGERPRINT
    assert operation.traversal.relation_collections == (follows.to_core(),)
    assert operation.options["scopeInjected"] is True
    assert operation.filter is not None
    assert Field("tenant_id") in operation.filter.referenced_fields


def test_all_neighbors_expression_resolution(users: ResourceRef) -> None:
    relation = ResourceReference("structured", "tests", "follows")
    start = RecordReference(ResourceReference("structured", "tests", "users"), "u1")
    expression = query(users).traverse(start=start, all_neighbors=True, max_depth=2).expression()
    operation = normalize_expression(
        expression,
        all_neighbors_resolver=lambda resource, depth: TraversalResolution(
            resource, (relation,), FINGERPRINT, depth
        ),
    )
    assert operation.traversal is not None and operation.traversal.resolved


def test_extension_policy_applies_during_normalization(users: ResourceRef) -> None:
    policy = ExtensionPolicy(frozenset({"example.org/logical-hint"}))
    expression = Expression(
        "structured",
        "query",
        {
            "resource": users.to_dict(),
            "extensions": {"example.org/logical-hint": {"value": 1}},
        },
    )
    operation = normalize_expression(expression, extension_policy=policy)
    assert operation.extensions["example.org/logical-hint"] == {"value": 1}


@pytest.mark.parametrize(
    "expression",
    [
        Expression(
            "object", "list", {"resource": {"catalog": "object", "namespace": "x", "name": "y"}}
        ),
        Expression("structured", "put", {"resource": "tests.users"}),
        Expression("structured", "query", {}),
        Expression("structured", "query", {"resource": "tests.users", "pointInTime": "yes"}),
        Expression("structured", "query", {"resource": "tests.users", "select": 1}),
        Expression("structured", "search", {"resource": "tests.users", "query": 1}),
        Expression("structured", "aggregate", {"resource": "tests.users", "metrics": [1]}),
        Expression(
            "structured",
            "traverse",
            {"resource": "tests.users", "start": "not-a-record"},
        ),
    ],
)
def test_invalid_catalog_expressions_are_rejected(expression: Expression) -> None:
    with pytest.raises((TypeError, ValueError)):
        normalize_expression(expression)


def test_normalize_builder_preserves_no_scope_marker_without_scope(users: ResourceRef) -> None:
    operation = normalize_builder(query(users).where({"active": True}))
    assert "scopeInjected" not in operation.options


def test_serialized_expression_input(users: ResourceRef) -> None:
    original = query(users).where({"id": "u1"}).expression()
    operation = normalize_expression(original.to_dict())
    assert isinstance(operation, QueryOperation)


def test_search_mapping_and_string_forms(users: ResourceRef) -> None:
    mapping_form = normalize_expression(
        Expression(
            "structured",
            "search",
            {
                "resource": users.to_dict(),
                "query": {
                    "text": "storage",
                    "fields": ["body"],
                    "analyzer": "icu",
                    "fuzzyTolerance": 0,
                    "highlights": ["body"],
                    "ranking": "bm25",
                    "facets": ["name"],
                },
            },
        )
    )
    string_form = normalize_expression(
        Expression(
            "structured",
            "search",
            {
                "resource": users.to_dict(),
                "query": "storage",
                "fields": ["body"],
                "highlights": ["body"],
                "facets": ["name"],
            },
        )
    )
    assert mapping_form.filter is not None
    assert string_form.filter is not None
    assert mapping_form.filter.to_dict() == string_form.filter.to_dict()


def test_aggregate_mapping_forms(users: ResourceRef) -> None:
    operation = normalize_expression(
        Expression(
            "structured",
            "aggregate",
            {
                "resource": users.to_dict(),
                "groupBy": ["active", field("name").to_dict()],
                "metrics": [
                    {"name": "count", "function": "count"},
                    {"name": "average", "function": "avg", "field": "age"},
                    {
                        "name": "sum",
                        "function": "sum",
                        "operand": field("age").to_dict(),
                        "distinct": True,
                    },
                    {
                        "name": "maximum",
                        "expression": Aggregate("max", field("age")).to_dict(),
                    },
                ],
            },
        )
    )
    assert [item.name for item in operation.aggregates] == [
        "count",
        "average",
        "sum",
        "maximum",
    ]


@pytest.mark.parametrize(
    "method, arguments",
    [
        ("query", {"resource": "tests.users", "where": 1}),
        ("query", {"resource": "tests.users", "select": [{"expression": "bad"}]}),
        ("query", {"resource": "tests.users", "orderBy": [{"direction": "asc"}]}),
        ("query", {"resource": "tests.users", "orderBy": [1]}),
        ("query", {"resource": "tests.users", "joins": [1]}),
        (
            "query",
            {
                "resource": "tests.users",
                "joins": [
                    {
                        "kind": "inner",
                        "target": {
                            "resource": {
                                "catalog": "evidence",
                                "namespace": "tests",
                                "name": "logs",
                            },
                            "schema": None,
                        },
                        "on": field("id").eq(1).to_dict(),
                    }
                ],
            },
        ),
        ("search", {"resource": "tests.users", "query": {"text": "x", "fields": "body"}}),
        ("search", {"resource": "tests.users", "query": "x", "fields": "body"}),
        ("aggregate", {"resource": "tests.users", "groupBy": [1], "metrics": []}),
        ("aggregate", {"resource": "tests.users", "metrics": [{"expression": "bad"}]}),
        (
            "aggregate",
            {"resource": "tests.users", "metrics": [{"expression": field("age").to_dict()}]},
        ),
        ("aggregate", {"resource": "tests.users", "metrics": [{"function": "sum", "operand": 1}]}),
        (
            "traverse",
            {
                "resource": "tests.users",
                "start": {
                    "collectionRef": {
                        "catalog": "structured",
                        "namespace": "tests",
                        "name": "users",
                    },
                    "recordId": "1",
                },
                "allNeighbors": "yes",
            },
        ),
        (
            "traverse",
            {
                "resource": "tests.users",
                "start": {
                    "collectionRef": {
                        "catalog": "structured",
                        "namespace": "tests",
                        "name": "users",
                    },
                    "recordId": "1",
                },
                "allNeighbors": True,
                "relationWhere": [],
            },
        ),
        ("query", {"resource": "tests.users", "extensions": []}),
        ("query", {"resource": "tests.users", "options": []}),
        ("query", {"resource": "tests.users", "budget": []}),
        ("query", {"resource": "tests.users", "limit": True}),
        ("query", {"resource": "tests.users", "cursor": 1}),
    ],
)
def test_more_invalid_normalization_paths(
    method: str,
    arguments: dict[str, object],
) -> None:
    with pytest.raises((TypeError, ValueError)):
        normalize_expression(Expression("structured", method, arguments))
