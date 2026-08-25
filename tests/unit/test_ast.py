# SPDX-License-Identifier: Apache-2.0
"""Value-expression and mapping-first syntax tests."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from meridian_storage.query import (
    Aggregate,
    BinaryExpression,
    BooleanExpression,
    Distance,
    DistanceWithin,
    DocumentPath,
    Field,
    FullTextMatch,
    Literal,
    MembershipExpression,
    NamedAggregate,
    NullTest,
    Parameter,
    Point,
    Projection,
    Sort,
    TimestampRange,
    UnaryExpression,
    distance,
    distance_within,
    expression,
    expression_from_dict,
    field,
    full_text,
    literal,
    parameter,
    parse_filter,
    point,
    timestamp_range,
)


@pytest.mark.parametrize(
    ("value", "logical_type", "wire_value"),
    [
        (None, "null", None),
        (True, "boolean", True),
        (42, "int64", 42),
        (1.25, "float64", 1.25),
        ("", "string", ""),
        ("e\u0301", "string", "é"),
        (date(2026, 8, 25), "date", "2026-08-25"),
        (
            datetime(2026, 8, 25, 1, 2, 3, 4, tzinfo=UTC),
            "utcTimestamp",
            "2026-08-25T01:02:03.000004Z",
        ),
        (
            UUID("12345678-1234-5678-1234-567812345678"),
            "uuid",
            "12345678-1234-5678-1234-567812345678",
        ),
        (b"bytes", "bytes", "Ynl0ZXM"),
        ({"b": 2, "a": [1]}, "json", {"a": [1], "b": 2}),
    ],
)
def test_literal_canonical_types(value: object, logical_type: str, wire_value: object) -> None:
    item = Literal(value)
    assert item.to_dict()["logicalType"] == logical_type
    assert item.to_dict()["value"] == wire_value


def test_decimal_and_declared_logical_type() -> None:
    decimal = literal(Decimal("12.340"))
    assert decimal.to_dict() == {
        "kind": "literal",
        "logicalType": {"kind": "decimal", "precision": 5, "scale": 3},
        "value": "12.340",
    }
    declared = literal("READY", {"kind": "enum", "values": ["READY", "DONE"]})
    assert declared.to_dict()["logicalType"] == {
        "kind": "enum",
        "values": ["READY", "DONE"],
    }


@pytest.mark.parametrize(
    "value",
    [float("inf"), float("nan"), Decimal("NaN"), datetime(2026, 1, 1), object()],
)
def test_literal_rejects_noncanonical_values(value: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        Literal(value)


def test_field_fluent_operators_and_references() -> None:
    age = field("age", "structured:tests.users")
    expressions = (
        age.eq(18),
        age.ne(19),
        age.lt(20),
        age.lte(21),
        age.gt(17),
        age.gte(16),
        age.prefix("1"),
        age.contains("8"),
        age.is_null(False),
        age.in_([18, 21]),
        age.path("/nested/value"),
    )
    assert all(age in item.referenced_fields for item in expressions)
    assert expressions[0].and_(expressions[1]).operators >= {"and", "eq", "ne"}
    assert expressions[0].or_(expressions[1]).operators >= {"or", "eq", "ne"}
    assert expressions[0].not_().operators >= {"not", "eq"}


def test_boolean_normalization_is_flat_sorted_and_deterministic() -> None:
    left = field("name").eq("Ada")
    right = field("age").gte(18)
    first = BooleanExpression("and", (left, BooleanExpression("and", (right, left))))
    second = BooleanExpression("and", (right, left, left))
    assert first.to_dict() == second.to_dict()
    assert first.fingerprint == second.fingerprint
    assert len(first.operands) == 3


def test_mapping_first_filter_covers_composition_and_special_operators() -> None:
    parsed = parse_filter(
        {
            "$and": [
                {"age": {"$gte": 18, "$lt": 65}},
                {"name": {"$prefix": "A"}},
            ],
            "active": True,
            "id": {"$in": ["a", "b"]},
        }
    )
    assert isinstance(parsed, BooleanExpression)
    assert parsed.operators >= {"and", "gte", "lt", "prefix", "eq", "in"}
    assert {item.name for item in parsed.referenced_fields} == {"active", "age", "id", "name"}

    inverted = parse_filter({"$not": {"active": False}})
    assert isinstance(inverted, UnaryExpression)
    assert isinstance(parse_filter({}), Literal)
    assert parse_filter(None) is None


def test_full_text_time_document_and_spatial_nodes() -> None:
    search = full_text(
        "meridian storage",
        fields=("body", "name"),
        analyzer="icu",
        fuzzy_tolerance=1,
        highlights=("body",),
        ranking="bm25",
        facets=("name",),
    )
    interval = timestamp_range(
        "ts",
        start=datetime(2026, 1, 1, tzinfo=UTC),
        end=datetime(2026, 2, 1, tzinfo=UTC),
    )
    location = point(-122.4, 37.8)
    within = distance_within(field("location"), location, Decimal("1000"))
    metric = distance(field("location"), location)
    document = field("doc").path("/owner/name")
    assert isinstance(search, FullTextMatch)
    assert isinstance(interval, TimestampRange)
    assert isinstance(location, Point)
    assert isinstance(within, DistanceWithin)
    assert isinstance(metric, Distance)
    assert isinstance(document, DocumentPath)
    assert within.to_dict()["distance"] == "1000"


def test_all_expression_nodes_round_trip() -> None:
    nodes = (
        Field("name"),
        Literal(3),
        Parameter("minimum_age", "int64"),
        UnaryExpression("negate", Literal(2)),
        BinaryExpression("add", Literal(1), Literal(2)),
        BooleanExpression("or", (field("active").eq(True), field("age").gt(1))),
        NullTest(Field("name")),
        MembershipExpression(Field("id"), (Literal("a"), Literal("b")), True),
        TimestampRange(Field("ts"), Literal(datetime(2026, 1, 1, tzinfo=UTC))),
        DocumentPath(Field("doc"), "/a"),
        full_text("text", fields=("body",)),
        Point(1.0, 2.0),
        Distance(Field("location"), Point(1.0, 2.0)),
        DistanceWithin(Field("location"), Point(1.0, 2.0), Decimal("3")),
        Aggregate("sum", Field("value"), True),
    )
    for node in nodes:
        rebuilt = expression_from_dict(node.to_dict())
        assert rebuilt.to_dict() == node.to_dict()
        assert rebuilt.fingerprint == node.fingerprint


def test_projection_sort_and_named_aggregate() -> None:
    projection = Projection(field("name"), "display_name")
    sort = Sort(field("age"), "desc", "first")
    aggregate = NamedAggregate("total", Aggregate("count"))
    assert projection.to_dict()["alias"] == "display_name"
    assert sort.to_dict()["direction"] == "desc"
    assert aggregate.to_dict()["name"] == "total"


@pytest.mark.parametrize(
    "factory",
    [
        lambda: Field(""),
        lambda: Parameter("bad name", "string"),
        lambda: UnaryExpression("unknown", Literal(1)),
        lambda: BinaryExpression("unknown", Literal(1), Literal(2)),
        lambda: BooleanExpression("and", (Literal(True),)),
        lambda: NullTest(Literal(1), 1),
        lambda: MembershipExpression(Field("id"), ()),
        lambda: TimestampRange(Field("ts")),
        lambda: DocumentPath(Field("doc"), "not-a-pointer"),
        lambda: full_text("x", fields=()),
        lambda: full_text("x", fields=("body",), fuzzy_tolerance=3),
        lambda: Point(181, 0),
        lambda: Point(0, -91),
        lambda: Distance(Field("location"), Point(0, 0), "km"),
        lambda: DistanceWithin(Field("location"), Point(0, 0), Decimal("-1")),
        lambda: Aggregate("unknown"),
        lambda: Aggregate("sum"),
        lambda: Sort(Field("id"), "sideways"),
        lambda: Projection(Field("id"), ""),
    ],
)
def test_invalid_nodes_are_rejected(factory: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        factory()  # type: ignore[operator]


@pytest.mark.parametrize(
    "mapping",
    [
        {},
        {"kind": "mystery"},
        {"kind": "field", "name": "id", "unexpected": True},
        {"kind": "point", "coordinates": [1], "crs": "EPSG:4326"},
        {"kind": "point", "coordinates": [True, 1], "crs": "EPSG:4326"},
        {"kind": "isNull", "operand": {"kind": "field", "name": "id"}, "expected": 1},
    ],
)
def test_invalid_serialized_nodes_are_rejected(mapping: dict[str, object]) -> None:
    with pytest.raises((TypeError, ValueError)):
        expression_from_dict(mapping)


def test_expression_string_conventions_and_helpers() -> None:
    assert expression("$name") == Field("name")
    assert expression("$$literal") == Literal("$literal")
    assert expression(3) == Literal(3)
    assert parameter("p", "string") == Parameter("p", "string")
