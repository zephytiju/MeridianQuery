# SPDX-License-Identifier: Apache-2.0
"""Fluent Python representation of the mapping-first structured Query contract."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import cast

from meridian_storage.semantics import JsonValue, RecordReference, ResourceReference

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
    full_text,
    parse_filter,
    timestamp_range,
)
from ..ast import (
    field as query_field,
)
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
from ..wire import (
    Join as WireJoin,
)


def _resource(value: ResourceRef | ResourceReference | str | Mapping[str, object]) -> ResourceRef:
    if isinstance(value, ResourceReference):
        return value.to_core()
    return ResourceRef.parse(value, catalog="structured")


def _schema(value: SchemaRef | Mapping[str, object] | None) -> SchemaRef | None:
    if value is None:
        return None
    return SchemaRef.parse(value)


@dataclass(frozen=True, slots=True)
class MeridianQuery:
    """Immutable fluent builder; it never contains Adapter or Engine choices."""

    target: QueryTarget
    predicate: ValueExpression | None = None
    projections: tuple[Projection, ...] = ()
    sorts: tuple[Sort, ...] = ()
    page_spec: PageSpec = field(default_factory=PageSpec)
    consistency_class: str = "strong"
    safety_budget: SafetyBudget = field(default_factory=SafetyBudget)
    operation_kind: str = "scan"
    search_expression: FullTextMatch | None = None
    grouping: tuple[ValueExpression, ...] = ()
    aggregates: tuple[NamedAggregate, ...] = ()
    traversal_spec: TraversalSpec | None = None
    joins: tuple[WireJoin, ...] = ()
    logical_options: Mapping[str, JsonValue] = field(default_factory=dict)
    logical_extensions: Mapping[str, JsonValue] = field(default_factory=dict)
    extension_policy: ExtensionPolicy = NO_EXTENSIONS

    @classmethod
    def from_resource(
        cls,
        resource: ResourceRef | ResourceReference | str | Mapping[str, object],
        *,
        schema: SchemaRef | Mapping[str, object] | None = None,
        extension_policy: ExtensionPolicy = NO_EXTENSIONS,
    ) -> MeridianQuery:
        return cls(
            QueryTarget(_resource(resource), _schema(schema)), extension_policy=extension_policy
        )

    def where(self, predicate: Mapping[str, object] | ValueExpression) -> MeridianQuery:
        parsed = parse_filter(predicate)
        if parsed is None:
            return self
        combined = (
            parsed if self.predicate is None else BooleanExpression("and", (self.predicate, parsed))
        )
        return replace(self, predicate=combined)

    def select(
        self,
        *items: str | Field | ValueExpression | Projection,
    ) -> MeridianQuery:
        projections: list[Projection] = list(self.projections)
        for item in items:
            if isinstance(item, Projection):
                projections.append(item)
            elif isinstance(item, str):
                projections.append(Projection(query_field(item)))
            else:
                projections.append(Projection(item))
        return replace(self, projections=tuple(projections))

    def project(self, alias: str, value: str | ValueExpression) -> MeridianQuery:
        selected = query_field(value) if isinstance(value, str) else value
        return replace(self, projections=(*self.projections, Projection(selected, alias)))

    def order_by(
        self,
        *items: str | Field | ValueExpression | Sort,
    ) -> MeridianQuery:
        sorts: list[Sort] = list(self.sorts)
        for item in items:
            if isinstance(item, Sort):
                sorts.append(item)
            elif isinstance(item, str):
                direction = "desc" if item.startswith("-") else "asc"
                name = item[1:] if item.startswith(("-", "+")) else item
                sorts.append(Sort(query_field(name), direction))
            else:
                sorts.append(Sort(item))
        return replace(self, sorts=tuple(sorts))

    def page(
        self,
        *,
        size: int = 50,
        cursor: str | None = None,
        point_in_time: bool = False,
    ) -> MeridianQuery:
        return replace(self, page_spec=PageSpec(size, cursor, point_in_time))

    def full_text(
        self,
        text: str,
        *,
        fields: Sequence[str | Field],
        analyzer: str = "icu",
        fuzzy_tolerance: int = 0,
        highlights: Sequence[str] = (),
        ranking: str = "bm25",
        facets: Sequence[str] = (),
    ) -> MeridianQuery:
        match = full_text(
            text,
            fields=fields,
            analyzer=analyzer,
            fuzzy_tolerance=fuzzy_tolerance,
            highlights=highlights,
            ranking=ranking,
            facets=facets,
        )
        return replace(
            self,
            operation_kind="search",
            search_expression=match,
        )

    def time_range(
        self,
        timestamp_field: str | Field,
        *,
        start: object | None = None,
        end: object | None = None,
        include_start: bool = True,
        include_end: bool = False,
    ) -> MeridianQuery:
        interval = timestamp_range(
            timestamp_field,
            start=start,
            end=end,
            include_start=include_start,
            include_end=include_end,
        )
        return self.where(interval)

    def group_by(self, *items: str | ValueExpression) -> MeridianQuery:
        values = tuple(query_field(item) if isinstance(item, str) else item for item in items)
        return replace(self, operation_kind="aggregate", grouping=values)

    def measure(
        self,
        name: str,
        function: str,
        operand: str | ValueExpression | None = None,
        *,
        distinct: bool = False,
    ) -> MeridianQuery:
        value = (
            None
            if operand is None
            else query_field(operand)
            if isinstance(operand, str)
            else operand
        )
        item = NamedAggregate(name, Aggregate(function, value, distinct))
        return replace(self, operation_kind="aggregate", aggregates=(*self.aggregates, item))

    def traverse(
        self,
        *,
        start: RecordReference | Mapping[str, object],
        relation_collections: Sequence[
            ResourceRef | ResourceReference | str | Mapping[str, object]
        ] = (),
        all_neighbors: bool = False,
        direction: str = "outbound",
        min_depth: int = 1,
        max_depth: int = 1,
        record_where: Mapping[str, object] | ValueExpression | None = None,
        relation_where: Mapping[str, Mapping[str, object] | ValueExpression] | None = None,
        uniqueness: str = "simple-path",
        result_shape: str = "records",
    ) -> MeridianQuery:
        record_ref = (
            start if isinstance(start, RecordReference) else RecordReference.from_mapping(start)
        )
        relations = tuple(_resource(item) for item in relation_collections)
        relation_predicates = {
            _resource(key).canonical: cast(ValueExpression, parse_filter(predicate))
            for key, predicate in (relation_where or {}).items()
        }
        traversal = TraversalSpec(
            start=record_ref,
            relation_collections=relations,
            all_neighbors=all_neighbors,
            direction=direction,
            min_depth=min_depth,
            max_depth=max_depth,
            record_filter=parse_filter(record_where),
            relation_predicates=relation_predicates,
            uniqueness=uniqueness,
            result_shape=result_shape,
        )
        return replace(self, operation_kind="traverse", traversal_spec=traversal)

    def join(
        self,
        resource: ResourceRef | ResourceReference | str | Mapping[str, object],
        *,
        on: Mapping[str, object] | ValueExpression,
        schema: SchemaRef | Mapping[str, object] | None = None,
        kind: str = "inner",
    ) -> MeridianQuery:
        predicate = parse_filter(on)
        if predicate is None:
            raise ValueError("join predicate cannot be empty")
        item = WireJoin(QueryTarget(_resource(resource), _schema(schema)), predicate, kind)
        return replace(self, joins=(*self.joins, item))

    def consistency(self, value: str) -> MeridianQuery:
        if value not in {"strong", "session", "eventual"}:
            raise ValueError("unsupported query consistency")
        return replace(self, consistency_class=value)

    def budget(self, value: SafetyBudget) -> MeridianQuery:
        return replace(self, safety_budget=value)

    def option(self, name: str, value: JsonValue) -> MeridianQuery:
        allowed = {
            "estimatedBytes",
            "estimatedRows",
            "resultByteLimit",
        }
        if name not in allowed:
            raise ValueError("only logical, engine-neutral query options are accepted")
        options = dict(self.logical_options)
        options[name] = value
        return replace(self, logical_options=options)

    def extension(self, key: str, value: JsonValue) -> MeridianQuery:
        extensions = dict(self.logical_extensions)
        extensions[key] = value
        normalized = self.extension_policy.normalize(extensions)
        return replace(self, logical_extensions=normalized)

    def build(self) -> QueryOperation:
        predicate = self.predicate
        if self.search_expression is not None:
            predicate = (
                self.search_expression
                if predicate is None
                else BooleanExpression("and", (predicate, self.search_expression))
            )
        shape = (
            self.traversal_spec.result_shape
            if self.traversal_spec is not None
            else "aggregate"
            if self.operation_kind == "aggregate"
            else "search"
            if self.operation_kind == "search"
            else "records"
        )
        targets = (self.target, *(item.target for item in self.joins))
        return QueryOperation(
            catalog="structured",
            targets=tuple(targets),
            operation=self.operation_kind,
            result=ResultSpec(shape, self.projections),
            filter=predicate,
            joins=self.joins,
            grouping=self.grouping,
            aggregates=self.aggregates,
            traversal=self.traversal_spec,
            order=self.sorts,
            page=self.page_spec,
            consistency=self.consistency_class,
            budget=self.safety_budget,
            options=self.logical_options,
            extensions=self.extension_policy.normalize(self.logical_extensions),
        )

    def expression(self) -> Expression:
        """Return the corresponding public ``structured`` Catalog Expression."""

        resource = self.target.resource.to_dict()
        where = {} if self.predicate is None else self.predicate.to_dict()
        if self.operation_kind == "search":
            match = cast(FullTextMatch, self.search_expression)
            arguments: dict[str, JsonValue] = {
                "resource": resource,
                "query": match.to_dict(),
                "where": where,
                "facets": list(match.facets),
                "highlights": list(match.highlights),
                "orderBy": [item.to_dict() for item in self.sorts],
                "limit": self.page_spec.size,
            }
        elif self.operation_kind == "aggregate":
            arguments = {
                "resource": resource,
                "metrics": [item.to_dict() for item in self.aggregates],
                "groupBy": [item.to_dict() for item in self.grouping],
                "where": where,
            }
        elif self.operation_kind == "traverse":
            traversal = cast(TraversalSpec, self.traversal_spec)
            arguments = {
                "resource": resource,
                "start": traversal.start.to_dict(),
                "relationCollections": [item.to_dict() for item in traversal.relation_collections],
                "allNeighbors": traversal.all_neighbors,
                "direction": traversal.direction,
                "minDepth": traversal.min_depth,
                "maxDepth": traversal.max_depth,
                "recordWhere": (
                    {} if traversal.record_filter is None else traversal.record_filter.to_dict()
                ),
                "relationWhere": {
                    key: predicate.to_dict()
                    for key, predicate in traversal.relation_predicates.items()
                },
                "uniqueness": traversal.uniqueness,
                "resultShape": traversal.result_shape,
            }
        else:
            arguments = {
                "resource": resource,
                "where": where,
                "select": [item.to_dict() for item in self.projections],
                "orderBy": [item.to_dict() for item in self.sorts],
                "limit": self.page_spec.size,
            }
        if self.joins:
            arguments["joins"] = [item.to_dict() for item in self.joins]
        arguments["consistency"] = self.consistency_class
        arguments["budget"] = self.safety_budget.to_dict()
        if self.logical_options:
            arguments["options"] = dict(self.logical_options)
        if self.logical_extensions:
            arguments["extensions"] = dict(self.logical_extensions)
        if self.page_spec.point_in_time and self.operation_kind in {"scan", "search"}:
            arguments["pointInTime"] = True
        if self.page_spec.cursor is not None and self.operation_kind in {"scan", "search"}:
            arguments["cursor"] = self.page_spec.cursor
        return Expression(
            "structured",
            "query" if self.operation_kind == "scan" else self.operation_kind,
            arguments,
        )


def query(
    resource: ResourceRef | ResourceReference | str | Mapping[str, object],
    *,
    schema: SchemaRef | Mapping[str, object] | None = None,
    extension_policy: ExtensionPolicy = NO_EXTENSIONS,
) -> MeridianQuery:
    return MeridianQuery.from_resource(
        resource,
        schema=schema,
        extension_policy=extension_policy,
    )


__all__ = ["MeridianQuery", "query"]
