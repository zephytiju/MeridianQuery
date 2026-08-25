# SPDX-License-Identifier: Apache-2.0
"""Strict wire deserialization and deterministic rejection coverage."""

from __future__ import annotations

import copy
from collections.abc import Callable

import pytest
from meridian_storage.semantics import RecordReference, ResourceReference

from meridian_storage import ResourceRef, SchemaRef
from meridian_storage.query import (
    Aggregate,
    Join,
    NamedAggregate,
    PageSpec,
    Projection,
    QueryOperation,
    QueryTarget,
    ResultSpec,
    SafetyBudget,
    TraversalSpec,
    field,
    query,
)


def test_component_mapping_round_trips(users: ResourceRef) -> None:
    target = QueryTarget(users, SchemaRef("structured", "tests", "users", "1.0.0"))
    assert QueryTarget.from_mapping(target.to_dict()) == target
    result = ResultSpec("records", (Projection(field("id"), "record_id"),), True)
    assert ResultSpec.from_mapping(result.to_dict()) == result
    assert PageSpec.from_mapping(PageSpec(20, "cursor", True).to_dict()) == PageSpec(
        20, "cursor", True
    )
    assert SafetyBudget.from_mapping(SafetyBudget().to_dict()) == SafetyBudget()


def test_aggregate_operation_round_trip(users: ResourceRef) -> None:
    operation = query(users).group_by("active").measure("count", "count").build()
    rebuilt = QueryOperation.from_mapping(operation.to_dict())
    assert rebuilt.to_dict() == operation.to_dict()


def test_traversal_mapping_round_trip_with_filters(users: ResourceRef) -> None:
    follows = ResourceRef("structured", "tests", "follows")
    start = RecordReference(ResourceReference("structured", "tests", "users"), "u1")
    traversal = TraversalSpec(
        start,
        (follows,),
        direction="any",
        min_depth=2,
        max_depth=4,
        record_filter=field("active").eq(True),
        relation_predicates={follows.canonical: field("active").eq(True)},
        result_shape="paths",
    )
    assert TraversalSpec.from_mapping(traversal.to_dict()) == traversal
    operation = QueryOperation(
        "structured",
        (QueryTarget(users),),
        "traverse",
        result=ResultSpec("paths"),
        traversal=traversal,
    )
    assert QueryOperation.from_mapping(operation.to_dict()).to_dict() == operation.to_dict()


@pytest.mark.parametrize(
    "factory",
    [
        lambda: QueryTarget(
            ResourceRef("structured", "tests", "users"),
            SchemaRef("structured", "other", "users", "1.0.0"),
        ),
        lambda: QueryTarget.from_mapping({}),
        lambda: QueryTarget.from_mapping({"resource": "bad", "schema": None}),
        lambda: QueryTarget.from_mapping(
            {
                "resource": {"catalog": "structured", "namespace": "tests", "name": "users"},
                "schema": "bad",
            }
        ),
        lambda: Join.from_mapping({}),
        lambda: Join.from_mapping({"kind": "inner", "target": "bad", "on": "bad"}),
        lambda: ResultSpec("records", (Projection(field("id"), "x"), Projection(field("id"), "x"))),
        lambda: ResultSpec.from_mapping({}),
        lambda: ResultSpec.from_mapping(
            {"shape": "records", "projection": [1], "includeTotal": False}
        ),
        lambda: ResultSpec.from_mapping(
            {
                "shape": "records",
                "projection": [{"expression": "bad"}],
                "includeTotal": False,
            }
        ),
        lambda: PageSpec(1, point_in_time=1),
        lambda: PageSpec.from_mapping({}),
        lambda: SafetyBudget(workload_profile=""),
        lambda: SafetyBudget.from_mapping({}),
    ],
)
def test_component_rejections(factory: Callable[[], object]) -> None:
    with pytest.raises((TypeError, ValueError)):
        factory()


def test_traversal_constructor_rejections(users: ResourceRef) -> None:
    relation = ResourceRef("structured", "tests", "follows")
    start = RecordReference(ResourceReference("structured", "tests", "users"), "u1")
    factories = (
        lambda: TraversalSpec(start),
        lambda: TraversalSpec(start, (relation,), all_neighbors=True),
        lambda: TraversalSpec(start, (relation,), all_neighbors=1),
        lambda: TraversalSpec(start, (relation,), registry_fingerprint="sha256:" + "1" * 64),
        lambda: TraversalSpec(start, (relation,), direction="sideways"),
        lambda: TraversalSpec(start, (relation,), min_depth=3, max_depth=2),
        lambda: TraversalSpec(start, (relation,), uniqueness="global"),
        lambda: TraversalSpec(start, (relation,), result_shape="trees"),
        lambda: TraversalSpec(
            start,
            (relation,),
            relation_predicates={"structured:tests.other": field("id").eq(1)},
        ),
    )
    for factory in factories:
        with pytest.raises((TypeError, ValueError)):
            factory()


def test_traversal_mapping_rejections(users: ResourceRef) -> None:
    relation = ResourceRef("structured", "tests", "follows")
    start = RecordReference(ResourceReference("structured", "tests", "users"), "u1")
    valid = TraversalSpec(start, (relation,)).to_dict()
    mutations: list[dict[str, object]] = []
    for change in (
        {"unexpected": True},
        {"start": "bad"},
        {"relationSelector": "bad"},
        {"relationPredicates": "bad"},
        {
            "relationSelector": {
                "kind": "explicit",
                "relationCollections": ["bad"],
                "registryFingerprint": None,
            }
        },
        {"recordFilter": "bad"},
        {"relationPredicates": {1: field("id").eq(1).to_dict()}},
        {
            "relationSelector": {
                "kind": "mystery",
                "relationCollections": [relation.to_dict()],
                "registryFingerprint": None,
            }
        },
    ):
        candidate = copy.deepcopy(valid)
        candidate.update(change)
        mutations.append(candidate)
    for candidate in mutations:
        with pytest.raises((TypeError, ValueError)):
            TraversalSpec.from_mapping(candidate)


def test_operation_constructor_rejections(users: ResourceRef) -> None:
    target = QueryTarget(users)
    other_catalog = QueryTarget(ResourceRef("evidence", "tests", "logs"))
    duplicate_aggregates = (
        NamedAggregate("same", Aggregate("count")),
        NamedAggregate("same", Aggregate("count")),
    )
    factories = (
        lambda: QueryOperation("structured", (target,), "scan", format_version="wrong"),
        lambda: QueryOperation("structured", (other_catalog,), "scan"),
        lambda: QueryOperation("structured", (target,), "scan", consistency="causal"),
        lambda: QueryOperation(
            "structured",
            (target,),
            "aggregate",
            result=ResultSpec("aggregate"),
            aggregates=duplicate_aggregates,
        ),
        lambda: QueryOperation("structured", (target,), "scan", options={"scopeInjected": "yes"}),
        lambda: QueryOperation(
            "structured", (target,), "scan", extensions={"not namespaced": True}
        ),
    )
    for factory in factories:
        with pytest.raises((TypeError, ValueError)):
            factory()


def test_operation_mapping_rejections(users: ResourceRef) -> None:
    valid = query(users).where({"id": "u1"}).order_by("id").build().to_dict()
    mutations: list[dict[str, object]] = []
    for path, value in (
        (("targets",), "bad"),
        (("result",), "bad"),
        (("filter",), 1),
        (("joins",), [1]),
        (("grouping",), [1]),
        (("aggregates",), [{"name": "x"}]),
        (
            ("aggregates",),
            [{"name": "x", "expression": {"kind": "field", "name": "id"}}],
        ),
        (("traversal",), "bad"),
        (("order",), [{"expression": field("id").to_dict()}]),
        (("page",), "bad"),
        (("options",), {}),
        (("extensions",), "bad"),
    ):
        candidate = copy.deepcopy(valid)
        candidate[path[0]] = value
        mutations.append(candidate)
    for candidate in mutations:
        with pytest.raises((TypeError, ValueError)):
            QueryOperation.from_mapping(candidate)
