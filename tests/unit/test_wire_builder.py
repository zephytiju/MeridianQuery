# SPDX-License-Identifier: Apache-2.0
"""Fluent builder and canonical wire-plan tests."""

from __future__ import annotations

from collections.abc import Callable
from types import MappingProxyType

import pytest
from jsonschema import Draft202012Validator
from meridian_storage.semantics import (
    RecordReference,
    ResourceReference,
    canonical_json_bytes,
)

from meridian_storage import ResourceRef
from meridian_storage.query import (
    Aggregate,
    ExtensionPolicy,
    Join,
    PageSpec,
    QueryOperation,
    QueryTarget,
    ResultSpec,
    SafetyBudget,
    TraversalSpec,
    field,
    query,
    query_operation_contract,
)


def test_builder_creates_canonical_scan_and_core_expression(users: ResourceRef) -> None:
    built = (
        query(users)
        .where({"active": True, "age": {"$gte": 18}})
        .select("id", "name")
        .project("age_plus_one", field("age").eq(20))
        .order_by("-age", "+id")
        .page(size=25, cursor="opaque", point_in_time=True)
        .consistency("session")
        .option("estimatedRows", 100)
    )
    operation = built.build()
    assert operation.catalog == "structured"
    assert operation.operation == "scan"
    assert operation.page == PageSpec(25, "opaque", True)
    assert operation.consistency == "session"
    assert operation.result.shape == "records"
    assert operation.options["estimatedRows"] == 100
    expression = built.expression()
    assert expression.catalog == "structured"
    assert expression.method == "query"
    assert expression.arguments["cursor"] == "opaque"


def test_search_expression_does_not_duplicate_full_text(users: ResourceRef) -> None:
    built = (
        query(users)
        .where({"active": True})
        .full_text(
            "storage",
            fields=("body",),
            highlights=("body",),
            facets=("name",),
        )
    )
    operation = built.build()
    expression = built.expression()
    assert operation.operation == "search"
    assert operation.filter is not None
    assert "fullText" in operation.filter.operators
    assert expression.arguments["query"]["kind"] == "fullText"  # type: ignore[index]
    assert expression.arguments["where"] == field("active").eq(True).to_dict()


def test_aggregate_builder(users: ResourceRef) -> None:
    built = (
        query(users)
        .group_by("active")
        .measure("count", "count")
        .measure("average_age", "avg", "age")
    )
    operation = built.build()
    assert operation.operation == "aggregate"
    assert operation.result.shape == "aggregate"
    assert [item.name for item in operation.aggregates] == ["count", "average_age"]
    assert built.expression().method == "aggregate"


def test_explicit_traversal_builder(users: ResourceRef) -> None:
    follows = ResourceRef("structured", "tests", "follows")
    start = RecordReference(ResourceReference("structured", "tests", "users"), "u-1")
    built = query(users).traverse(
        start=start,
        relation_collections=(follows,),
        direction="outbound",
        min_depth=1,
        max_depth=3,
        record_where={"active": True},
        relation_where={follows.canonical: {"active": True}},
        result_shape="paths",
    )
    operation = built.build()
    assert operation.operation == "traverse"
    assert operation.result.shape == "paths"
    assert operation.traversal is not None
    assert operation.traversal.relation_collections == (follows,)
    assert built.expression().method == "traverse"


def test_join_targets_are_explicit_and_round_trip(users: ResourceRef) -> None:
    teams = ResourceRef("structured", "tests", "teams")
    user_name = field("team_id", users.canonical)
    team_id = field("id", teams.canonical)
    operation = (
        query(users)
        .join(teams, on=user_name.eq(team_id), kind="left")
        .select(field("name", users.canonical), field("name", teams.canonical))
        .build()
    )
    assert operation.resources == (teams, users)
    rebuilt = QueryOperation.from_mapping(operation.to_dict())
    assert rebuilt.to_dict() == operation.to_dict()
    assert rebuilt.fingerprint == operation.fingerprint


def test_wire_contract_validates_builder_output(users: ResourceRef) -> None:
    operation = query(users).where({"id": {"$in": ["a", "b"]}}).build()
    validator = Draft202012Validator(query_operation_contract())
    assert list(validator.iter_errors(operation.to_dict())) == []
    assert operation.canonical_bytes == operation.canonical_bytes
    core = operation.to_core_operation()
    assert core.operation_contract == "meridian.structured.query"
    assert canonical_json_bytes(core.input["queryPlan"]) == operation.canonical_bytes
    assert core.read_only is True


def test_namespaced_extensions_require_explicit_policy(users: ResourceRef) -> None:
    policy = ExtensionPolicy(frozenset({"example.org/query-hint"}))
    built = query(users, extension_policy=policy).extension(
        "example.org/query-hint", {"logical": [1, 2]}
    )
    operation = built.build()
    assert operation.extensions["example.org/query-hint"] == {"logical": (1, 2)}
    rebuilt = QueryOperation.from_mapping(operation.to_dict(), extension_policy=policy)
    assert rebuilt.to_dict() == operation.to_dict()

    with pytest.raises(ValueError, match="policy-enabled"):
        query(users).extension("example.org/query-hint", True)
    with pytest.raises(ValueError, match="native syntax"):
        ExtensionPolicy(frozenset({"example.org/native-sql"}))


def test_wire_values_are_deeply_immutable(users: ResourceRef) -> None:
    operation = QueryOperation(
        "structured",
        (QueryTarget(users),),
        "scan",
        options={"estimatedRows": 1},
        extensions={},
    )
    assert isinstance(operation.options, MappingProxyType)
    with pytest.raises(TypeError):
        operation.options["estimatedRows"] = 2  # type: ignore[index]


@pytest.mark.parametrize(
    "factory",
    [
        lambda: PageSpec(0),
        lambda: PageSpec(501),
        lambda: PageSpec(1, ""),
        lambda: SafetyBudget(deadline_ms=0),
        lambda: ResultSpec("unknown"),
        lambda: ResultSpec("records", include_total=1),
        lambda: Join(
            QueryTarget(ResourceRef("structured", "tests", "x")), field("id").eq(1), "right"
        ),
    ],
)
def test_invalid_wire_components(factory: Callable[[], object]) -> None:
    with pytest.raises((TypeError, ValueError)):
        factory()


def test_invalid_operation_combinations(users: ResourceRef) -> None:
    target = QueryTarget(users)
    other = QueryTarget(ResourceRef("structured", "tests", "other"))
    cases = (
        lambda: QueryOperation("object", (target,), "scan"),
        lambda: QueryOperation("structured", (), "scan"),
        lambda: QueryOperation("structured", (target, target), "scan"),
        lambda: QueryOperation("structured", (target,), "unknown"),
        lambda: QueryOperation("structured", (target,), "traverse"),
        lambda: QueryOperation(
            "structured",
            (target,),
            "scan",
            traversal=TraversalSpec(
                RecordReference(ResourceReference("structured", "tests", "users"), "1"),
                (ResourceRef("structured", "tests", "relations"),),
            ),
        ),
        lambda: QueryOperation("structured", (target,), "aggregate"),
        lambda: QueryOperation(
            "structured",
            (target,),
            "scan",
            aggregates=(Aggregate("count"),),  # type: ignore[arg-type]
        ),
        lambda: QueryOperation("structured", (target,), "scan", result=ResultSpec("search")),
        lambda: QueryOperation("structured", (target,), "scan", options={"engine": "x"}),
        lambda: QueryOperation("structured", (target,), "scan", options={"estimatedRows": True}),
        lambda: QueryOperation(
            "structured",
            (target,),
            "scan",
            joins=(Join(other, field("id").eq(1)),),
        ),
    )
    for build in cases:
        with pytest.raises((TypeError, ValueError)):
            build()


def test_mapping_rejects_unknown_fields_and_unapproved_extensions(users: ResourceRef) -> None:
    mapping = query(users).build().to_dict()
    mapping["unexpected"] = True
    with pytest.raises(ValueError, match="root fields"):
        QueryOperation.from_mapping(mapping)

    extension_mapping = query(users).build().to_dict()
    extension_mapping["extensions"] = {"example.org/hint": True}
    with pytest.raises(ValueError, match="policy-enabled"):
        QueryOperation.from_mapping(extension_mapping)


def test_builder_rejects_nonlogical_consumer_options(users: ResourceRef) -> None:
    with pytest.raises(ValueError, match="logical"):
        query(users).option("endpoint", "https://example.invalid")
    with pytest.raises(ValueError, match="consistency"):
        query(users).consistency("linearizable")


def test_all_neighbors_selector_can_be_unresolved(users: ResourceRef) -> None:
    start = RecordReference(ResourceReference("structured", "tests", "users"), "u-1")
    traversal = query(users).traverse(start=start, all_neighbors=True).build().traversal
    assert traversal is not None
    assert traversal.all_neighbors is True
    assert traversal.resolved is False
