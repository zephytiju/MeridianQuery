# SPDX-License-Identifier: Apache-2.0
"""Deterministic normalization from Catalog Expressions to query wire plans."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from typing import cast

from meridian_storage.semantics import (
    JsonValue,
    RecordReference,
    ResourceReference,
    TraversalResolution,
)

from meridian_storage import Expression, ResourceRef, SchemaRef

from ..ast import (
    Aggregate,
    BooleanExpression,
    Field,
    FullTextMatch,
    NamedAggregate,
    Projection,
    Sort,
    ValueExpression,
    expression_from_dict,
    full_text,
    parse_filter,
)
from ..builder import MeridianQuery
from ..wire import (
    NO_EXTENSIONS,
    ExtensionPolicy,
    PageSpec,
    QueryOperation,
    QueryTarget,
    ResultSpec,
    SafetyBudget,
    TraversalSpec,
)
from ..wire import Join as WireJoin

SchemaResolver = Callable[[ResourceRef], SchemaRef | None]
AllNeighborsResolver = Callable[[ResourceReference, int], TraversalResolution]


def normalize_expression(
    value: Expression | Mapping[str, object],
    *,
    schema_resolver: SchemaResolver | None = None,
    all_neighbors_resolver: AllNeighborsResolver | None = None,
    scope_filter: Mapping[str, object] | ValueExpression | None = None,
    extension_policy: ExtensionPolicy = NO_EXTENSIONS,
) -> QueryOperation:
    """Normalize one public Catalog Expression without resolving an Adapter or Engine."""

    expression = value if isinstance(value, Expression) else Expression.from_mapping(value)
    if expression.catalog not in {"structured", "evidence"}:
        raise ValueError("query normalization is only defined for structured and evidence Catalogs")
    if expression.method not in {"get", "query", "search", "aggregate", "traverse"}:
        raise ValueError(f"Catalog method {expression.method!r} is not a V1 query Expression")
    arguments = cast(Mapping[str, object], expression.arguments)
    target_ref = _resource(arguments.get("resource"), expression.catalog)
    schema = None if schema_resolver is None else schema_resolver(target_ref)
    target = QueryTarget(target_ref, schema)
    consumer_filter = _filter(arguments.get("where"))
    required_scope = parse_filter(scope_filter)
    predicate = _and(consumer_filter, required_scope)
    point_in_time = arguments.get("pointInTime", False)
    if not isinstance(point_in_time, bool):
        raise TypeError("pointInTime must be boolean")
    page = PageSpec(
        _integer(arguments.get("limit", 50), "limit"),
        _optional_string(arguments.get("cursor"), "cursor"),
        point_in_time,
    )
    projections: tuple[Projection, ...] = ()
    sorts: tuple[Sort, ...] = ()
    grouping: tuple[ValueExpression, ...] = ()
    aggregates: tuple[NamedAggregate, ...] = ()
    traversal: TraversalSpec | None = None
    joins = _joins(arguments.get("joins", ()), expression.catalog, schema_resolver)
    operation = "get" if expression.method == "get" else "scan"
    result_shape = "records"
    if expression.method == "query":
        projections = _projections(arguments.get("select", ()))
        sorts = _sorts(arguments.get("orderBy", ()))
    elif expression.method == "search":
        operation = "search"
        result_shape = "search"
        search = _full_text(arguments)
        predicate = _and(predicate, search)
        sorts = _sorts(arguments.get("orderBy", ()))
    elif expression.method == "aggregate":
        operation = "aggregate"
        result_shape = "aggregate"
        grouping = _expressions(arguments.get("groupBy", ()), "groupBy")
        aggregates = _aggregates(arguments.get("metrics", ()))
        page = PageSpec()
    elif expression.method == "traverse":
        operation = "traverse"
        start = arguments.get("start")
        if not isinstance(start, Mapping):
            raise TypeError("traverse start must be a RecordRef mapping")
        record_ref = RecordReference.from_mapping(start)
        raw_relations = _array(arguments.get("relationCollections", ()), "relationCollections")
        relations = tuple(_resource(item, "structured") for item in raw_relations)
        all_neighbors = arguments.get("allNeighbors", False)
        if not isinstance(all_neighbors, bool):
            raise TypeError("allNeighbors must be boolean")
        max_depth = _integer(arguments.get("maxDepth", 1), "maxDepth")
        traversal = TraversalSpec(
            start=record_ref,
            relation_collections=relations,
            all_neighbors=all_neighbors,
            direction=_optional_string(arguments.get("direction"), "direction") or "outbound",
            min_depth=_integer(arguments.get("minDepth", 1), "minDepth"),
            max_depth=max_depth,
            record_filter=_filter(arguments.get("recordWhere")),
            relation_predicates=_relation_filters(arguments.get("relationWhere", {})),
            uniqueness=_optional_string(arguments.get("uniqueness"), "uniqueness") or "simple-path",
            result_shape=_optional_string(arguments.get("resultShape"), "resultShape") or "records",
        )
        if all_neighbors and all_neighbors_resolver is not None:
            resolution = all_neighbors_resolver(record_ref.collection_ref, max_depth)
            traversal = replace(
                traversal,
                relation_collections=tuple(
                    item.to_core() for item in resolution.relation_collections
                ),
                registry_fingerprint=resolution.registry_fingerprint,
            )
        result_shape = traversal.result_shape
        page = PageSpec()
    extensions_value = arguments.get("extensions", {})
    if not isinstance(extensions_value, Mapping):
        raise TypeError("query extensions must be an object")
    extensions = extension_policy.normalize(cast(Mapping[str, object], extensions_value))
    options_value = arguments.get("options", {})
    if not isinstance(options_value, Mapping):
        raise TypeError("query options must be an object")
    options = {str(key): cast(JsonValue, item) for key, item in options_value.items()}
    if required_scope is not None:
        options["scopeInjected"] = True
    budget_value = arguments.get("budget")
    if budget_value is not None and not isinstance(budget_value, Mapping):
        raise TypeError("query budget must be an object")
    budget = (
        SafetyBudget()
        if budget_value is None
        else SafetyBudget.from_mapping(cast(Mapping[str, object], budget_value))
    )
    return QueryOperation(
        catalog=expression.catalog,
        targets=(target, *(item.target for item in joins)),
        operation=operation,
        result=ResultSpec(result_shape, projections),
        filter=predicate,
        joins=joins,
        grouping=grouping,
        aggregates=aggregates,
        traversal=traversal,
        order=sorts,
        page=page,
        consistency=cast(str, arguments.get("consistency", "strong")),
        budget=budget,
        options=options,
        extensions=extensions,
    )


def normalize_builder(
    value: MeridianQuery,
    *,
    all_neighbors_resolver: AllNeighborsResolver | None = None,
    scope_filter: Mapping[str, object] | ValueExpression | None = None,
) -> QueryOperation:
    """Resolve builder-only dynamic selectors and inject non-removable scope."""

    operation = value.build()
    traversal = operation.traversal
    if traversal is not None and traversal.all_neighbors and all_neighbors_resolver is not None:
        resolution = all_neighbors_resolver(traversal.start.collection_ref, traversal.max_depth)
        traversal = replace(
            traversal,
            relation_collections=tuple(item.to_core() for item in resolution.relation_collections),
            registry_fingerprint=resolution.registry_fingerprint,
        )
    scope = parse_filter(scope_filter)
    options = dict(operation.options)
    if scope is not None:
        options["scopeInjected"] = True
    return replace(
        operation,
        filter=_and(operation.filter, scope),
        traversal=traversal,
        options=options,
    )


def _resource(value: object, catalog: str) -> ResourceRef:
    if isinstance(value, ResourceReference):
        return value.to_core()
    if isinstance(value, ResourceRef):
        return ResourceRef.parse(value, catalog=catalog)
    if isinstance(value, (str, Mapping)):
        return ResourceRef.parse(value, catalog=catalog)
    raise TypeError("query Expression requires a logical Resource reference")


def _filter(value: object) -> ValueExpression | None:
    if value is None or value == {}:
        return None
    if isinstance(value, ValueExpression):
        return value
    if not isinstance(value, Mapping):
        raise TypeError("where must be an object")
    if isinstance(value.get("kind"), str):
        return expression_from_dict(value)
    return parse_filter(cast(Mapping[str, object], value))


def _and(
    left: ValueExpression | None,
    right: ValueExpression | None,
) -> ValueExpression | None:
    if left is None:
        return right
    if right is None:
        return left
    return BooleanExpression("and", (left, right))


def _projections(value: object) -> tuple[Projection, ...]:
    result: list[Projection] = []
    for item in _array(value, "select"):
        if isinstance(item, str):
            result.append(Projection(Field(item)))
        elif isinstance(item, Mapping):
            raw = item.get("expression", item)
            if not isinstance(raw, Mapping):
                raise TypeError("projection expression must be an object")
            result.append(
                Projection(
                    expression_from_dict(raw),
                    cast(str | None, item.get("alias")),
                )
            )
        else:
            raise TypeError("select entries must be field names or projection objects")
    return tuple(result)


def _sorts(value: object) -> tuple[Sort, ...]:
    result: list[Sort] = []
    for item in _array(value, "orderBy"):
        if isinstance(item, str):
            direction = "desc" if item.startswith("-") else "asc"
            name = item[1:] if item.startswith(("-", "+")) else item
            result.append(Sort(Field(name), direction))
        elif isinstance(item, Mapping):
            raw = item.get("expression")
            if raw is None and "field" in item:
                raw = {"kind": "field", "name": item["field"]}
            if not isinstance(raw, Mapping):
                raise TypeError("orderBy expression must be an object")
            result.append(
                Sort(
                    expression_from_dict(raw),
                    cast(str, item.get("direction", "asc")),
                    cast(str, item.get("nulls", "last")),
                )
            )
        else:
            raise TypeError("orderBy entries must be strings or objects")
    return tuple(result)


def _joins(
    value: object,
    catalog: str,
    schema_resolver: SchemaResolver | None,
) -> tuple[WireJoin, ...]:
    result: list[WireJoin] = []
    for item in _array(value, "joins"):
        if not isinstance(item, Mapping):
            raise TypeError("join entries must be objects")
        parsed = WireJoin.from_mapping(cast(Mapping[str, object], item))
        if parsed.target.resource.catalog != catalog:
            raise ValueError("join Resource must belong to the query Catalog")
        if parsed.target.schema is None and schema_resolver is not None:
            parsed = replace(
                parsed,
                target=QueryTarget(
                    parsed.target.resource,
                    schema_resolver(parsed.target.resource),
                ),
            )
        result.append(parsed)
    return tuple(result)


def _relation_filters(value: object) -> dict[str, ValueExpression]:
    if not isinstance(value, Mapping):
        raise TypeError("relationWhere must be an object")
    result: dict[str, ValueExpression] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise TypeError("relationWhere keys must be Collection references")
        parsed = _filter(item)
        if parsed is None:
            raise ValueError("relationWhere predicates cannot be empty")
        result[key] = parsed
    return result


def _full_text(arguments: Mapping[str, object]) -> FullTextMatch:
    query = arguments.get("query")
    if isinstance(query, Mapping):
        if query.get("kind") == "fullText":
            parsed = expression_from_dict(query)
            if not isinstance(parsed, FullTextMatch):
                raise TypeError("search query must be a fullText value expression")
            return parsed
        fields = query.get("fields", ("*",))
        if not isinstance(fields, Sequence) or isinstance(fields, (str, bytes)):
            raise TypeError("search query fields must be an array")
        return full_text(
            cast(str, query.get("text", query.get("query"))),
            fields=tuple(cast(str, item) for item in fields),
            analyzer=cast(str, query.get("analyzer", "icu")),
            fuzzy_tolerance=cast(int, query.get("fuzzyTolerance", 0)),
            highlights=tuple(cast(Sequence[str], query.get("highlights", ()))),
            ranking=cast(str, query.get("ranking", "bm25")),
            facets=tuple(cast(Sequence[str], query.get("facets", ()))),
        )
    if not isinstance(query, str):
        raise TypeError("search query must be a string or mapping")
    raw_fields = arguments.get("fields", ("*",))
    if not isinstance(raw_fields, Sequence) or isinstance(raw_fields, (str, bytes)):
        raise TypeError("search fields must be an array")
    return full_text(
        query,
        fields=tuple(cast(str, item) for item in raw_fields),
        highlights=tuple(cast(Sequence[str], arguments.get("highlights", ()))),
        facets=tuple(cast(Sequence[str], arguments.get("facets", ()))),
    )


def _expressions(value: object, name: str) -> tuple[ValueExpression, ...]:
    result: list[ValueExpression] = []
    for item in _array(value, name):
        if isinstance(item, str):
            result.append(Field(item))
        elif isinstance(item, Mapping):
            result.append(expression_from_dict(item))
        else:
            raise TypeError(f"{name} entries must be strings or expression objects")
    return tuple(result)


def _aggregates(value: object) -> tuple[NamedAggregate, ...]:
    result: list[NamedAggregate] = []
    for index, item in enumerate(_array(value, "metrics")):
        if not isinstance(item, Mapping):
            raise TypeError("aggregate metric must be an object")
        if "expression" in item:
            raw = item["expression"]
            if not isinstance(raw, Mapping):
                raise TypeError("aggregate expression must be an object")
            aggregate = expression_from_dict(raw)
            if not isinstance(aggregate, Aggregate):
                raise TypeError("metric expression must be an aggregate")
            result.append(NamedAggregate(cast(str, item.get("name", f"metric_{index}")), aggregate))
            continue
        function = cast(str, item.get("function"))
        operand_value = item.get("field", item.get("operand"))
        operand: ValueExpression | None
        if operand_value is None:
            operand = None
        elif isinstance(operand_value, str):
            operand = Field(operand_value)
        elif isinstance(operand_value, Mapping):
            operand = expression_from_dict(operand_value)
        else:
            raise TypeError("aggregate operand must be a field or expression")
        result.append(
            NamedAggregate(
                cast(str, item.get("name", f"metric_{index}")),
                Aggregate(function, operand, cast(bool, item.get("distinct", False))),
            )
        )
    return tuple(result)


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    return value


def _optional_string(value: object, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    return value


def _array(value: object, name: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise TypeError(f"{name} must be an array")
    return value


__all__ = [
    "AllNeighborsResolver",
    "SchemaResolver",
    "normalize_builder",
    "normalize_expression",
]
