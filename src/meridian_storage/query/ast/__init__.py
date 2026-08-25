# SPDX-License-Identifier: Apache-2.0
"""Immutable engine-neutral query value-expression nodes."""

from __future__ import annotations

import base64
import math
import re
import unicodedata
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from types import MappingProxyType
from typing import cast
from uuid import UUID

from meridian_storage.semantics import JsonValue, canonical_json_bytes, sha256_fingerprint

_FIELD_RE = re.compile(r"^[^\x00-\x1f\x7f]+$")
_PARAMETER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
_COMPARISON_OPERATORS = frozenset({"eq", "ne", "lt", "lte", "gt", "gte"})
_ARITHMETIC_OPERATORS = frozenset({"add", "subtract", "multiply", "divide", "modulo"})
_VARIADIC_OPERATORS = frozenset({"and", "or"})


def _text(value: object, name: str, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    normalized = unicodedata.normalize("NFC", value)
    if len(normalized.encode("utf-8")) > maximum or _FIELD_RE.fullmatch(normalized) is None:
        raise ValueError(f"{name} is not a bounded UTF-8 string")
    return normalized


def _literal_text(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    if len(normalized.encode("utf-8")) > 1_048_576 or "\x00" in normalized:
        raise ValueError("literal string is not a bounded UTF-8 string")
    return normalized


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise TypeError(f"{name} must be numeric")
    try:
        result = float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def _freeze_json(value: JsonValue) -> JsonValue:
    if isinstance(value, Mapping):
        return cast(
            JsonValue,
            MappingProxyType({str(key): _freeze_json(item) for key, item in sorted(value.items())}),
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return cast(JsonValue, tuple(_freeze_json(item) for item in value))
    return value


def _thaw_json(value: JsonValue) -> JsonValue:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


class ValueExpression(ABC):
    """Base for every typed query-internal computation."""

    @abstractmethod
    def to_dict(self) -> dict[str, JsonValue]: ...

    @property
    def fingerprint(self) -> str:
        return sha256_fingerprint(self.to_dict())

    @property
    def operators(self) -> frozenset[str]:
        return frozenset({cast(str, self.to_dict()["kind"])})

    @property
    def referenced_fields(self) -> tuple[Field, ...]:
        return ()

    def and_(self, *others: ValueExpression) -> ValueExpression:
        return BooleanExpression("and", (self, *others))

    def or_(self, *others: ValueExpression) -> ValueExpression:
        return BooleanExpression("or", (self, *others))

    def not_(self) -> ValueExpression:
        return UnaryExpression("not", self)


@dataclass(frozen=True, slots=True)
class Field(ValueExpression):
    name: str
    resource: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _text(self.name, "field name"))
        if self.resource is not None:
            object.__setattr__(self, "resource", _text(self.resource, "field Resource", 768))

    @property
    def referenced_fields(self) -> tuple[Field, ...]:
        return (self,)

    def to_dict(self) -> dict[str, JsonValue]:
        result: dict[str, JsonValue] = {"kind": "field", "name": self.name}
        if self.resource is not None:
            result["resource"] = self.resource
        return result

    def eq(self, value: object) -> BinaryExpression:
        return BinaryExpression("eq", self, expression(value))

    def ne(self, value: object) -> BinaryExpression:
        return BinaryExpression("ne", self, expression(value))

    def lt(self, value: object) -> BinaryExpression:
        return BinaryExpression("lt", self, expression(value))

    def lte(self, value: object) -> BinaryExpression:
        return BinaryExpression("lte", self, expression(value))

    def gt(self, value: object) -> BinaryExpression:
        return BinaryExpression("gt", self, expression(value))

    def gte(self, value: object) -> BinaryExpression:
        return BinaryExpression("gte", self, expression(value))

    def is_null(self, expected: bool = True) -> NullTest:
        return NullTest(self, expected)

    def in_(self, values: Sequence[object]) -> MembershipExpression:
        return MembershipExpression(self, tuple(expression(item) for item in values))

    def prefix(self, value: str) -> BinaryExpression:
        return BinaryExpression("prefix", self, Literal(value))

    def contains(self, value: object) -> BinaryExpression:
        return BinaryExpression("contains", self, expression(value))

    def path(self, pointer: str) -> DocumentPath:
        return DocumentPath(self, pointer)


@dataclass(frozen=True, slots=True)
class Literal(ValueExpression):
    value: JsonValue
    logical_type: JsonValue

    def __init__(
        self, value: object, logical_type: str | Mapping[str, object] | None = None
    ) -> None:
        normalized, inferred = _literal(value)
        selected = _normalize_logical_type(logical_type) if logical_type is not None else inferred
        object.__setattr__(self, "value", _freeze_json(normalized))
        object.__setattr__(self, "logical_type", _freeze_json(selected))

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "kind": "literal",
            "logicalType": _thaw_json(self.logical_type),
            "value": _thaw_json(self.value),
        }


@dataclass(frozen=True, slots=True)
class Parameter(ValueExpression):
    name: str
    logical_type: JsonValue

    def __init__(self, name: str, logical_type: str | Mapping[str, object]) -> None:
        normalized = _text(name, "parameter name", 128)
        if _PARAMETER_RE.fullmatch(normalized) is None:
            raise ValueError("parameter name does not match the V1 grammar")
        object.__setattr__(self, "name", normalized)
        object.__setattr__(
            self, "logical_type", _freeze_json(_normalize_logical_type(logical_type))
        )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "kind": "parameter",
            "name": self.name,
            "logicalType": _thaw_json(self.logical_type),
        }


@dataclass(frozen=True, slots=True)
class UnaryExpression(ValueExpression):
    operator: str
    operand: ValueExpression

    def __post_init__(self) -> None:
        if self.operator not in {"not", "negate"}:
            raise ValueError("unsupported unary operator")

    @property
    def operators(self) -> frozenset[str]:
        return frozenset({self.operator}) | self.operand.operators

    @property
    def referenced_fields(self) -> tuple[Field, ...]:
        return self.operand.referenced_fields

    def to_dict(self) -> dict[str, JsonValue]:
        return {"kind": self.operator, "operand": self.operand.to_dict()}


@dataclass(frozen=True, slots=True)
class BinaryExpression(ValueExpression):
    operator: str
    left: ValueExpression
    right: ValueExpression

    def __post_init__(self) -> None:
        allowed = _COMPARISON_OPERATORS | _ARITHMETIC_OPERATORS | {"prefix", "contains"}
        if self.operator not in allowed:
            raise ValueError(f"unsupported binary operator: {self.operator!r}")

    @property
    def operators(self) -> frozenset[str]:
        return frozenset({self.operator}) | self.left.operators | self.right.operators

    @property
    def referenced_fields(self) -> tuple[Field, ...]:
        return _unique_fields((*self.left.referenced_fields, *self.right.referenced_fields))

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "kind": self.operator,
            "left": self.left.to_dict(),
            "right": self.right.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class BooleanExpression(ValueExpression):
    operator: str
    operands: tuple[ValueExpression, ...]

    def __post_init__(self) -> None:
        if self.operator not in _VARIADIC_OPERATORS or len(self.operands) < 2:
            raise ValueError("and/or expressions require at least two operands")
        flattened: list[ValueExpression] = []
        for operand in self.operands:
            if isinstance(operand, BooleanExpression) and operand.operator == self.operator:
                flattened.extend(operand.operands)
            else:
                flattened.append(operand)
        flattened.sort(key=lambda item: canonical_json_bytes(item.to_dict()))
        object.__setattr__(self, "operands", tuple(flattened))

    @property
    def operators(self) -> frozenset[str]:
        result = frozenset({self.operator})
        for operand in self.operands:
            result |= operand.operators
        return result

    @property
    def referenced_fields(self) -> tuple[Field, ...]:
        return _unique_fields(
            tuple(field for operand in self.operands for field in operand.referenced_fields)
        )

    def to_dict(self) -> dict[str, JsonValue]:
        return {"kind": self.operator, "operands": [item.to_dict() for item in self.operands]}


@dataclass(frozen=True, slots=True)
class NullTest(ValueExpression):
    operand: ValueExpression
    is_null: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.is_null, bool):
            raise TypeError("null-test expected value must be boolean")

    @property
    def operators(self) -> frozenset[str]:
        return frozenset({"isNull"}) | self.operand.operators

    @property
    def referenced_fields(self) -> tuple[Field, ...]:
        return self.operand.referenced_fields

    def to_dict(self) -> dict[str, JsonValue]:
        return {"kind": "isNull", "operand": self.operand.to_dict(), "expected": self.is_null}


@dataclass(frozen=True, slots=True)
class MembershipExpression(ValueExpression):
    operand: ValueExpression
    values: tuple[ValueExpression, ...]
    negated: bool = False

    def __post_init__(self) -> None:
        if not self.values:
            raise ValueError("membership requires at least one candidate")
        if not isinstance(self.negated, bool):
            raise TypeError("membership negation must be boolean")

    @property
    def operators(self) -> frozenset[str]:
        result = frozenset({"notIn" if self.negated else "in"}) | self.operand.operators
        for item in self.values:
            result |= item.operators
        return result

    @property
    def referenced_fields(self) -> tuple[Field, ...]:
        fields = list(self.operand.referenced_fields)
        for item in self.values:
            fields.extend(item.referenced_fields)
        return _unique_fields(tuple(fields))

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "kind": "notIn" if self.negated else "in",
            "operand": self.operand.to_dict(),
            "values": [item.to_dict() for item in self.values],
        }


@dataclass(frozen=True, slots=True)
class TimestampRange(ValueExpression):
    operand: ValueExpression
    start: ValueExpression | None = None
    end: ValueExpression | None = None
    include_start: bool = True
    include_end: bool = False

    def __post_init__(self) -> None:
        if self.start is None and self.end is None:
            raise ValueError("timestamp range requires a start or end")
        if not isinstance(self.include_start, bool) or not isinstance(self.include_end, bool):
            raise TypeError("timestamp range boundary flags must be boolean")

    @property
    def operators(self) -> frozenset[str]:
        result = frozenset({"timestampRange"}) | self.operand.operators
        if self.start is not None:
            result |= self.start.operators
        if self.end is not None:
            result |= self.end.operators
        return result

    @property
    def referenced_fields(self) -> tuple[Field, ...]:
        return self.operand.referenced_fields

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "kind": "timestampRange",
            "operand": self.operand.to_dict(),
            "start": None if self.start is None else self.start.to_dict(),
            "end": None if self.end is None else self.end.to_dict(),
            "includeStart": self.include_start,
            "includeEnd": self.include_end,
        }


@dataclass(frozen=True, slots=True)
class DocumentPath(ValueExpression):
    document: ValueExpression
    pointer: str

    def __post_init__(self) -> None:
        pointer = _text(self.pointer, "document JSON pointer", 2048)
        if not pointer.startswith("/"):
            raise ValueError("document path must be an RFC 6901 JSON pointer")
        object.__setattr__(self, "pointer", pointer)

    @property
    def operators(self) -> frozenset[str]:
        return frozenset({"documentPath"}) | self.document.operators

    @property
    def referenced_fields(self) -> tuple[Field, ...]:
        return self.document.referenced_fields

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "kind": "documentPath",
            "document": self.document.to_dict(),
            "pointer": self.pointer,
        }


@dataclass(frozen=True, slots=True)
class FullTextMatch(ValueExpression):
    query: str
    fields: tuple[Field, ...]
    analyzer: str = "icu"
    fuzzy_tolerance: int = 0
    highlights: tuple[str, ...] = ()
    ranking: str = "bm25"
    facets: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "query", _text(self.query, "full-text query", 16_384))
        if not self.fields:
            raise ValueError("full-text match requires at least one field")
        object.__setattr__(self, "fields", _unique_fields(self.fields))
        object.__setattr__(self, "analyzer", _text(self.analyzer, "analyzer profile", 128))
        object.__setattr__(self, "ranking", _text(self.ranking, "ranking profile", 128))
        if isinstance(self.fuzzy_tolerance, bool) or not 0 <= self.fuzzy_tolerance <= 2:
            raise ValueError("full-text fuzzy tolerance must be 0, 1, or 2")
        for name in ("highlights", "facets"):
            normalized = tuple(sorted({_text(item, name, 512) for item in getattr(self, name)}))
            if len(normalized) != len(getattr(self, name)):
                raise ValueError(f"full-text {name} must be unique")
            object.__setattr__(self, name, normalized)

    @property
    def operators(self) -> frozenset[str]:
        return frozenset({"fullText"})

    @property
    def referenced_fields(self) -> tuple[Field, ...]:
        return self.fields

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "kind": "fullText",
            "query": self.query,
            "fields": [item.to_dict() for item in self.fields],
            "analyzer": self.analyzer,
            "fuzzyTolerance": self.fuzzy_tolerance,
            "highlights": list(self.highlights),
            "ranking": self.ranking,
            "facets": list(self.facets),
        }


@dataclass(frozen=True, slots=True)
class Point(ValueExpression):
    longitude: float
    latitude: float
    crs: str = "EPSG:4326"

    def __post_init__(self) -> None:
        if not math.isfinite(self.longitude) or not -180.0 <= self.longitude <= 180.0:
            raise ValueError("longitude must be finite and between -180 and 180")
        if not math.isfinite(self.latitude) or not -90.0 <= self.latitude <= 90.0:
            raise ValueError("latitude must be finite and between -90 and 90")
        crs = _text(self.crs, "coordinate reference system", 128)
        if crs != "EPSG:4326":
            raise ValueError("Meridian V1 point expressions require WGS84 EPSG:4326")
        object.__setattr__(self, "crs", crs)

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "kind": "point",
            "coordinates": [self.longitude, self.latitude],
            "crs": self.crs,
        }


@dataclass(frozen=True, slots=True)
class Distance(ValueExpression):
    left: ValueExpression
    right: ValueExpression
    unit: str = "m"

    def __post_init__(self) -> None:
        if self.unit != "m":
            raise ValueError("Meridian V1 public distance unit is meters")

    @property
    def operators(self) -> frozenset[str]:
        return frozenset({"distance"}) | self.left.operators | self.right.operators

    @property
    def referenced_fields(self) -> tuple[Field, ...]:
        return _unique_fields((*self.left.referenced_fields, *self.right.referenced_fields))

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "kind": "distance",
            "left": self.left.to_dict(),
            "right": self.right.to_dict(),
            "unit": self.unit,
        }


@dataclass(frozen=True, slots=True)
class DistanceWithin(ValueExpression):
    left: ValueExpression
    right: ValueExpression
    distance: Decimal
    unit: str = "m"

    def __post_init__(self) -> None:
        if isinstance(self.distance, bool):
            raise TypeError("distance_within distance must be numeric")
        distance = Decimal(self.distance)
        if not distance.is_finite() or distance < 0:
            raise ValueError("distance_within distance must be finite and non-negative")
        if self.unit != "m":
            raise ValueError("Meridian V1 public distance unit is meters")
        object.__setattr__(self, "distance", distance)

    @property
    def operators(self) -> frozenset[str]:
        return frozenset({"distanceWithin"}) | self.left.operators | self.right.operators

    @property
    def referenced_fields(self) -> tuple[Field, ...]:
        return _unique_fields((*self.left.referenced_fields, *self.right.referenced_fields))

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "kind": "distanceWithin",
            "left": self.left.to_dict(),
            "right": self.right.to_dict(),
            "distance": format(self.distance, "f"),
            "unit": self.unit,
        }


@dataclass(frozen=True, slots=True)
class Aggregate(ValueExpression):
    function: str
    operand: ValueExpression | None = None
    distinct: bool = False

    def __post_init__(self) -> None:
        allowed = {"count", "sum", "avg", "min", "max", "percentile"}
        if self.function not in allowed:
            raise ValueError("unsupported aggregate function")
        if self.function != "count" and self.operand is None:
            raise ValueError(f"aggregate {self.function} requires an operand")
        if not isinstance(self.distinct, bool):
            raise TypeError("aggregate distinct flag must be boolean")

    @property
    def operators(self) -> frozenset[str]:
        result = frozenset({f"aggregate.{self.function}"})
        if self.operand is not None:
            result |= self.operand.operators
        return result

    @property
    def referenced_fields(self) -> tuple[Field, ...]:
        return () if self.operand is None else self.operand.referenced_fields

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "kind": "aggregate",
            "function": self.function,
            "operand": None if self.operand is None else self.operand.to_dict(),
            "distinct": self.distinct,
        }


@dataclass(frozen=True, slots=True)
class Projection:
    expression: ValueExpression
    alias: str | None = None

    def __post_init__(self) -> None:
        if self.alias is not None:
            object.__setattr__(self, "alias", _text(self.alias, "projection alias"))

    def to_dict(self) -> dict[str, JsonValue]:
        result: dict[str, JsonValue] = {"expression": self.expression.to_dict()}
        if self.alias is not None:
            result["alias"] = self.alias
        return result


@dataclass(frozen=True, slots=True)
class Sort:
    expression: ValueExpression
    direction: str = "asc"
    nulls: str = "last"

    def __post_init__(self) -> None:
        if self.direction not in {"asc", "desc"}:
            raise ValueError("sort direction must be asc or desc")
        if self.nulls not in {"first", "last"}:
            raise ValueError("sort null placement must be first or last")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "expression": self.expression.to_dict(),
            "direction": self.direction,
            "nulls": self.nulls,
        }


@dataclass(frozen=True, slots=True)
class NamedAggregate:
    name: str
    aggregate: Aggregate

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _text(self.name, "aggregate name"))

    def to_dict(self) -> dict[str, JsonValue]:
        return {"name": self.name, "expression": self.aggregate.to_dict()}


def field(name: str, resource: str | None = None) -> Field:
    return Field(name, resource)


def literal(value: object, logical_type: str | Mapping[str, object] | None = None) -> Literal:
    return Literal(value, logical_type)


def parameter(name: str, logical_type: str | Mapping[str, object]) -> Parameter:
    return Parameter(name, logical_type)


def point(longitude: float, latitude: float, *, crs: str = "EPSG:4326") -> Point:
    return Point(longitude, latitude, crs)


def distance(left: object, right: object) -> Distance:
    return Distance(expression(left), expression(right))


def distance_within(left: object, right: object, meters: Decimal | int | str) -> DistanceWithin:
    return DistanceWithin(expression(left), expression(right), Decimal(meters))


def full_text(
    query: str,
    *,
    fields: Sequence[str | Field],
    analyzer: str = "icu",
    fuzzy_tolerance: int = 0,
    highlights: Sequence[str] = (),
    ranking: str = "bm25",
    facets: Sequence[str] = (),
) -> FullTextMatch:
    return FullTextMatch(
        query,
        tuple(item if isinstance(item, Field) else Field(item) for item in fields),
        analyzer,
        fuzzy_tolerance,
        tuple(highlights),
        ranking,
        tuple(facets),
    )


def timestamp_range(
    value: str | Field | ValueExpression,
    *,
    start: object | None = None,
    end: object | None = None,
    include_start: bool = True,
    include_end: bool = False,
) -> TimestampRange:
    return TimestampRange(
        Field(value) if isinstance(value, str) else expression(value),
        None if start is None else expression(start),
        None if end is None else expression(end),
        include_start,
        include_end,
    )


def expression(value: object) -> ValueExpression:
    if isinstance(value, ValueExpression):
        return value
    if isinstance(value, str) and value.startswith("$") and not value.startswith("$$"):
        return Field(value[1:])
    return Literal(value[1:] if isinstance(value, str) and value.startswith("$$") else value)


def parse_filter(value: Mapping[str, object] | ValueExpression | None) -> ValueExpression | None:
    """Parse the mapping-first filter syntax into the canonical AST."""

    if value is None:
        return None
    if isinstance(value, ValueExpression):
        return value
    if not isinstance(value, Mapping):
        raise TypeError("query filter must be a mapping or ValueExpression")
    predicates: list[ValueExpression] = []
    for key in sorted(value):
        item = value[key]
        if key in {"$and", "$or"}:
            if not isinstance(item, Sequence) or isinstance(item, (str, bytes)) or len(item) < 2:
                raise ValueError(f"{key} requires an array with at least two mappings")
            operands = tuple(parse_filter(cast(Mapping[str, object], part)) for part in item)
            predicates.append(
                BooleanExpression(key[1:], tuple(cast(ValueExpression, part) for part in operands))
            )
            continue
        if key == "$not":
            nested = parse_filter(cast(Mapping[str, object], item))
            if nested is None:
                raise ValueError("$not cannot contain an empty filter")
            predicates.append(UnaryExpression("not", nested))
            continue
        if key == "$fullText":
            if not isinstance(item, Mapping):
                raise TypeError("$fullText requires an object")
            raw_fields = item.get("fields")
            if not isinstance(raw_fields, Sequence) or isinstance(raw_fields, (str, bytes)):
                raise TypeError("$fullText fields must be an array")
            predicates.append(
                full_text(
                    cast(str, item.get("query")),
                    fields=tuple(cast(str, part) for part in raw_fields),
                    analyzer=cast(str, item.get("analyzer", "icu")),
                    fuzzy_tolerance=cast(int, item.get("fuzzyTolerance", 0)),
                    highlights=tuple(cast(Sequence[str], item.get("highlights", ()))),
                    ranking=cast(str, item.get("ranking", "bm25")),
                    facets=tuple(cast(Sequence[str], item.get("facets", ()))),
                )
            )
            continue
        target = Field(key)
        predicates.extend(_field_predicates(target, item))
    if not predicates:
        return Literal(True)
    if len(predicates) == 1:
        return predicates[0]
    return BooleanExpression("and", tuple(predicates))


def expression_from_dict(value: Mapping[str, object]) -> ValueExpression:
    """Deserialize the canonical AST without accepting unknown node fields."""

    kind = value.get("kind")
    if not isinstance(kind, str):
        raise ValueError("serialized value expression requires kind")
    if kind == "field":
        _keys(value, {"kind", "name"}, {"resource"})
        return Field(cast(str, value["name"]), cast(str | None, value.get("resource")))
    if kind == "literal":
        _keys(value, {"kind", "logicalType", "value"})
        return Literal(value["value"], cast(str | Mapping[str, object], value["logicalType"]))
    if kind == "parameter":
        _keys(value, {"kind", "name", "logicalType"})
        return Parameter(
            cast(str, value["name"]), cast(str | Mapping[str, object], value["logicalType"])
        )
    if kind in {"not", "negate"}:
        _keys(value, {"kind", "operand"})
        return UnaryExpression(kind, _nested(value["operand"]))
    if kind in _COMPARISON_OPERATORS | _ARITHMETIC_OPERATORS | {"prefix", "contains"}:
        _keys(value, {"kind", "left", "right"})
        return BinaryExpression(kind, _nested(value["left"]), _nested(value["right"]))
    if kind in _VARIADIC_OPERATORS:
        _keys(value, {"kind", "operands"})
        operands = _array(value["operands"], "operands")
        return BooleanExpression(kind, tuple(_nested(item) for item in operands))
    if kind == "isNull":
        _keys(value, {"kind", "operand", "expected"})
        return NullTest(_nested(value["operand"]), cast(bool, value["expected"]))
    if kind in {"in", "notIn"}:
        _keys(value, {"kind", "operand", "values"})
        return MembershipExpression(
            _nested(value["operand"]),
            tuple(_nested(item) for item in _array(value["values"], "values")),
            kind == "notIn",
        )
    if kind == "timestampRange":
        _keys(
            value,
            {"kind", "operand", "start", "end", "includeStart", "includeEnd"},
        )
        return TimestampRange(
            _nested(value["operand"]),
            None if value["start"] is None else _nested(value["start"]),
            None if value["end"] is None else _nested(value["end"]),
            cast(bool, value["includeStart"]),
            cast(bool, value["includeEnd"]),
        )
    if kind == "documentPath":
        _keys(value, {"kind", "document", "pointer"})
        return DocumentPath(_nested(value["document"]), cast(str, value["pointer"]))
    if kind == "fullText":
        _keys(
            value,
            {
                "kind",
                "query",
                "fields",
                "analyzer",
                "fuzzyTolerance",
                "highlights",
                "ranking",
                "facets",
            },
        )
        fields = tuple(cast(Field, _nested(item)) for item in _array(value["fields"], "fields"))
        return FullTextMatch(
            cast(str, value["query"]),
            fields,
            cast(str, value["analyzer"]),
            cast(int, value["fuzzyTolerance"]),
            tuple(cast(Sequence[str], value["highlights"])),
            cast(str, value["ranking"]),
            tuple(cast(Sequence[str], value["facets"])),
        )
    if kind == "point":
        _keys(value, {"kind", "coordinates", "crs"})
        coordinates = _array(value["coordinates"], "coordinates")
        if len(coordinates) != 2:
            raise ValueError("point coordinates require longitude and latitude")
        return Point(
            _number(coordinates[0], "point longitude"),
            _number(coordinates[1], "point latitude"),
            cast(str, value["crs"]),
        )
    if kind == "distance":
        _keys(value, {"kind", "left", "right", "unit"})
        return Distance(_nested(value["left"]), _nested(value["right"]), cast(str, value["unit"]))
    if kind == "distanceWithin":
        _keys(value, {"kind", "left", "right", "distance", "unit"})
        return DistanceWithin(
            _nested(value["left"]),
            _nested(value["right"]),
            Decimal(cast(str, value["distance"])),
            cast(str, value["unit"]),
        )
    if kind == "aggregate":
        _keys(value, {"kind", "function", "operand", "distinct"})
        return Aggregate(
            cast(str, value["function"]),
            None if value["operand"] is None else _nested(value["operand"]),
            cast(bool, value["distinct"]),
        )
    raise ValueError(f"unknown value-expression kind: {kind!r}")


def _field_predicates(target: Field, value: object) -> list[ValueExpression]:
    if not isinstance(value, Mapping) or not any(str(key).startswith("$") for key in value):
        return [target.eq(value)]
    result: list[ValueExpression] = []
    operators = {
        "$eq": "eq",
        "$ne": "ne",
        "$lt": "lt",
        "$lte": "lte",
        "$gt": "gt",
        "$gte": "gte",
        "$prefix": "prefix",
        "$contains": "contains",
    }
    for key in sorted(value):
        item = value[key]
        if key in operators:
            result.append(BinaryExpression(operators[key], target, expression(item)))
        elif key in {"$in", "$notIn"}:
            candidates = _array(item, key)
            result.append(
                MembershipExpression(
                    target,
                    tuple(expression(candidate) for candidate in candidates),
                    key == "$notIn",
                )
            )
        elif key == "$isNull":
            if not isinstance(item, bool):
                raise TypeError("$isNull requires a boolean")
            result.append(NullTest(target, item))
        elif key == "$timestampRange":
            if not isinstance(item, Mapping):
                raise TypeError("$timestampRange requires an object")
            result.append(
                timestamp_range(
                    target,
                    start=item.get("start"),
                    end=item.get("end"),
                    include_start=cast(bool, item.get("includeStart", True)),
                    include_end=cast(bool, item.get("includeEnd", False)),
                )
            )
        elif key == "$distanceWithin":
            if not isinstance(item, Mapping):
                raise TypeError("$distanceWithin requires an object")
            point_value = item.get("point")
            if not isinstance(point_value, Sequence) or isinstance(point_value, (str, bytes)):
                raise TypeError("$distanceWithin point requires [longitude, latitude]")
            coordinates = tuple(point_value)
            if len(coordinates) != 2:
                raise ValueError("$distanceWithin point requires two coordinates")
            result.append(
                distance_within(
                    target,
                    Point(
                        _number(coordinates[0], "point longitude"),
                        _number(coordinates[1], "point latitude"),
                    ),
                    cast(str | int, item.get("meters")),
                )
            )
        else:
            raise ValueError(f"unknown filter operator: {key!r}")
    return result


def _literal(value: object) -> tuple[JsonValue, JsonValue]:
    if value is None:
        return None, "null"
    if isinstance(value, bool):
        return value, "boolean"
    if isinstance(value, int):
        return value, "int64"
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite floating-point literals are prohibited")
        return value, "float64"
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("non-finite decimal literals are prohibited")
        sign, digits, exponent_value = value.as_tuple()
        del sign
        exponent = cast(int, exponent_value)
        precision = max(len(digits), -exponent, 1)
        scale = max(-exponent, 0)
        return format(value, "f"), {"kind": "decimal", "precision": precision, "scale": scale}
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("datetime literals must carry an explicit timezone")
        timestamp_text = (
            value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
        )
        return timestamp_text, "utcTimestamp"
    if isinstance(value, date):
        return value.isoformat(), "date"
    if isinstance(value, UUID):
        return str(value), "uuid"
    if isinstance(value, (bytes, bytearray, memoryview)):
        return base64.urlsafe_b64encode(bytes(value)).rstrip(b"=").decode("ascii"), "bytes"
    if isinstance(value, str):
        return _literal_text(value), "string"
    if isinstance(value, Mapping):
        mapping_value = {str(key): _literal(item)[0] for key, item in sorted(value.items())}
        return cast(JsonValue, mapping_value), "json"
    if isinstance(value, Sequence):
        return cast(JsonValue, [_literal(item)[0] for item in value]), "json"
    raise TypeError(f"unsupported query literal type: {type(value).__name__}")


def _normalize_logical_type(value: str | Mapping[str, object]) -> JsonValue:
    if isinstance(value, str):
        return _text(value, "logical type", 128)
    if not isinstance(value, Mapping) or "kind" not in value:
        raise TypeError("logical type must be a string or kind mapping")
    result: dict[str, JsonValue] = {}
    for key, item in sorted(value.items()):
        if key not in {"kind", "precision", "scale", "values"}:
            raise ValueError(f"unknown logical type field: {key!r}")
        normalized, _ = _literal(item)
        result[key] = normalized
    return result


def _unique_fields(values: tuple[Field, ...]) -> tuple[Field, ...]:
    result = {(item.resource or "", item.name): item for item in values}
    return tuple(result[key] for key in sorted(result))


def _keys(
    value: Mapping[str, object],
    required: set[str],
    optional: set[str] | None = None,
) -> None:
    optional = optional or set()
    if required - set(value) or set(value) - required - optional:
        raise ValueError("serialized expression contains unknown or missing fields")


def _nested(value: object) -> ValueExpression:
    if not isinstance(value, Mapping):
        raise TypeError("nested value expression must be an object")
    return expression_from_dict(value)


def _array(value: object, name: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise TypeError(f"{name} must be an array")
    return value


__all__ = [
    "Aggregate",
    "BinaryExpression",
    "BooleanExpression",
    "Distance",
    "DistanceWithin",
    "DocumentPath",
    "Field",
    "FullTextMatch",
    "Literal",
    "MembershipExpression",
    "NamedAggregate",
    "NullTest",
    "Parameter",
    "Point",
    "Projection",
    "Sort",
    "TimestampRange",
    "UnaryExpression",
    "ValueExpression",
    "distance",
    "distance_within",
    "expression",
    "expression_from_dict",
    "field",
    "full_text",
    "literal",
    "parameter",
    "parse_filter",
    "point",
    "timestamp_range",
]
