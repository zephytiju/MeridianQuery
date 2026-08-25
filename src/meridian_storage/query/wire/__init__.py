# SPDX-License-Identifier: Apache-2.0
"""Canonical language-neutral ``meridian.operation.query.v1`` contract."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import cast

from meridian_storage.registry.resources import CapabilityRequirement
from meridian_storage.semantics import JsonValue, RecordReference, canonical_json_bytes

from meridian_storage import Operation, ResourceRef, SchemaRef

from ..ast import (
    NamedAggregate,
    Projection,
    Sort,
    ValueExpression,
    expression_from_dict,
)

QUERY_FORMAT_VERSION = "meridian.operation.query.v1"
QUERY_CONTRACT_VERSION = "1.0.0"
QUERY_CAPABILITY_FORMAT_VERSION = "meridian.query.capabilities.v1"

_FINGERPRINT_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_EXTENSION_RE = re.compile(
    r"^(?:[A-Za-z][A-Za-z0-9+.-]*://\S+|[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?/[A-Za-z0-9._/-]+)$"
)
_FORBIDDEN_EXTENSION_TOKENS = frozenset(
    {"adapter", "credential", "dsl", "endpoint", "engine", "native", "password", "secret", "sql"}
)
_OPTION_KEYS = frozenset(
    {
        "estimatedBytes",
        "estimatedRows",
        "resultByteLimit",
        "scopeInjected",
    }
)
_OPERATIONS = frozenset(
    {
        "get",
        "scan",
        "search",
        "aggregate",
        "traverse",
        "insert",
        "upsert",
        "patch",
        "delete",
        "compareAndSet",
        "atomicClaim",
    }
)


def _fingerprint(value: str, name: str) -> str:
    if not isinstance(value, str) or _FINGERPRINT_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a sha256 fingerprint")
    return value


def _positive(value: int, name: str, *, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} exceeds the V1 maximum {maximum}")
    return value


def _immutable_json(value: JsonValue) -> JsonValue:
    if isinstance(value, Mapping):
        return cast(
            JsonValue,
            MappingProxyType(
                {str(key): _immutable_json(item) for key, item in sorted(value.items())}
            ),
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return cast(JsonValue, tuple(_immutable_json(item) for item in value))
    return value


def _mutable_json(value: JsonValue) -> JsonValue:
    if isinstance(value, Mapping):
        return {key: _mutable_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_mutable_json(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class ExtensionPolicy:
    """Explicit allowlist for non-engine, namespaced logical extensions."""

    allowed_keys: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        normalized = frozenset(self.validate_key(key) for key in self.allowed_keys)
        object.__setattr__(self, "allowed_keys", normalized)

    @staticmethod
    def validate_key(key: str) -> str:
        if not isinstance(key, str) or _EXTENSION_RE.fullmatch(key) is None:
            raise ValueError("query extension key must be a namespaced URI or reverse-domain key")
        lowered = key.casefold()
        if any(token in lowered for token in _FORBIDDEN_EXTENSION_TOKENS):
            raise ValueError("query extensions cannot carry Adapter, Engine, or native syntax")
        return key

    def normalize(self, extensions: Mapping[str, object]) -> Mapping[str, JsonValue]:
        unknown = set(extensions) - self.allowed_keys
        if unknown:
            raise ValueError(f"query extension keys are not policy-enabled: {sorted(unknown)!r}")
        normalized: dict[str, JsonValue] = {}
        for key, value in sorted(extensions.items()):
            self.validate_key(key)
            canonical_json_bytes(value)
            normalized[key] = _immutable_json(cast(JsonValue, value))
        return MappingProxyType(normalized)


NO_EXTENSIONS = ExtensionPolicy()


@dataclass(frozen=True, slots=True)
class QueryTarget:
    resource: ResourceRef
    schema: SchemaRef | None = None

    def __post_init__(self) -> None:
        resource = ResourceRef.parse(self.resource)
        object.__setattr__(self, "resource", resource)
        if self.schema is not None:
            schema = SchemaRef.parse(self.schema)
            if (schema.catalog, schema.namespace) != (resource.catalog, resource.namespace):
                raise ValueError(
                    "query target Schema and Resource must share Catalog and Namespace"
                )
            object.__setattr__(self, "schema", schema)

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "resource": self.resource.to_dict(),
            "schema": None if self.schema is None else self.schema.to_dict(),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> QueryTarget:
        if set(value) != {"resource", "schema"}:
            raise ValueError("query target requires only resource and schema")
        resource = value["resource"]
        schema = value["schema"]
        if not isinstance(resource, Mapping):
            raise TypeError("query target resource must be an object")
        if schema is not None and not isinstance(schema, Mapping):
            raise TypeError("query target schema must be null or an object")
        return cls(
            ResourceRef.parse(resource),
            None if schema is None else SchemaRef.parse(schema),
        )


@dataclass(frozen=True, slots=True)
class Join:
    target: QueryTarget
    on: ValueExpression
    kind: str = "inner"

    def __post_init__(self) -> None:
        if self.kind not in {"inner", "left"}:
            raise ValueError("V1 join kind must be inner or left")

    def to_dict(self) -> dict[str, JsonValue]:
        return {"kind": self.kind, "target": self.target.to_dict(), "on": self.on.to_dict()}

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> Join:
        if set(value) != {"kind", "target", "on"}:
            raise ValueError("join contains unknown or missing fields")
        if not isinstance(value["target"], Mapping) or not isinstance(value["on"], Mapping):
            raise TypeError("join target and predicate must be objects")
        return cls(
            QueryTarget.from_mapping(value["target"]),
            expression_from_dict(value["on"]),
            cast(str, value["kind"]),
        )


@dataclass(frozen=True, slots=True)
class ResultSpec:
    shape: str = "records"
    projection: tuple[Projection, ...] = ()
    include_total: bool = False

    def __post_init__(self) -> None:
        if self.shape not in {"records", "relations", "paths", "aggregate", "search"}:
            raise ValueError("unsupported query result shape")
        aliases = tuple(item.alias for item in self.projection if item.alias is not None)
        if len(set(aliases)) != len(aliases):
            raise ValueError("projection aliases must be unique")
        if not isinstance(self.include_total, bool):
            raise TypeError("result include_total flag must be boolean")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "shape": self.shape,
            "projection": [item.to_dict() for item in self.projection],
            "includeTotal": self.include_total,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> ResultSpec:
        if set(value) != {"shape", "projection", "includeTotal"}:
            raise ValueError("result contains unknown or missing fields")
        projection = _array(value["projection"], "result.projection")
        items: list[Projection] = []
        for item in projection:
            if not isinstance(item, Mapping) or set(item) - {"expression", "alias"}:
                raise ValueError("projection item is invalid")
            raw_expression = item.get("expression")
            if not isinstance(raw_expression, Mapping):
                raise TypeError("projection expression must be an object")
            items.append(
                Projection(
                    expression_from_dict(raw_expression),
                    cast(str | None, item.get("alias")),
                )
            )
        return cls(cast(str, value["shape"]), tuple(items), cast(bool, value["includeTotal"]))


@dataclass(frozen=True, slots=True)
class PageSpec:
    size: int = 50
    cursor: str | None = None
    point_in_time: bool = False

    def __post_init__(self) -> None:
        _positive(self.size, "page size", maximum=500)
        if self.cursor is not None and (not isinstance(self.cursor, str) or not self.cursor):
            raise ValueError("cursor must be a non-empty opaque string")
        if not isinstance(self.point_in_time, bool):
            raise ValueError("point_in_time must be boolean")

    def to_dict(self) -> dict[str, JsonValue]:
        return {"size": self.size, "cursor": self.cursor, "pointInTime": self.point_in_time}

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> PageSpec:
        if set(value) != {"size", "cursor", "pointInTime"}:
            raise ValueError("page contains unknown or missing fields")
        return cls(
            cast(int, value["size"]),
            cast(str | None, value["cursor"]),
            cast(bool, value["pointInTime"]),
        )


@dataclass(frozen=True, slots=True)
class SafetyBudget:
    deadline_ms: int = 30_000
    max_result_values: int = 500
    max_normalized_bytes: int = 16 * 1024 * 1024
    max_traversal_depth: int = 8
    max_relation_resources: int = 32
    max_visited_values: int = 100_000
    max_returned_paths: int = 10_000
    max_membership_names: int = 10_000
    max_facets: int = 100
    max_residual_rows: int = 10_000
    max_residual_bytes: int = 16 * 1024 * 1024
    workload_profile: str = "default"

    def __post_init__(self) -> None:
        for name in (
            "deadline_ms",
            "max_result_values",
            "max_normalized_bytes",
            "max_traversal_depth",
            "max_relation_resources",
            "max_visited_values",
            "max_returned_paths",
            "max_membership_names",
            "max_facets",
            "max_residual_rows",
            "max_residual_bytes",
        ):
            _positive(getattr(self, name), name)
        if not isinstance(self.workload_profile, str) or not self.workload_profile:
            raise ValueError("workload profile must be a named profile")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "deadlineMs": self.deadline_ms,
            "maxResultValues": self.max_result_values,
            "maxNormalizedBytes": self.max_normalized_bytes,
            "maxTraversalDepth": self.max_traversal_depth,
            "maxRelationResources": self.max_relation_resources,
            "maxVisitedValues": self.max_visited_values,
            "maxReturnedPaths": self.max_returned_paths,
            "maxMembershipNames": self.max_membership_names,
            "maxFacets": self.max_facets,
            "maxResidualRows": self.max_residual_rows,
            "maxResidualBytes": self.max_residual_bytes,
            "workloadProfile": self.workload_profile,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> SafetyBudget:
        keys = {
            "deadlineMs",
            "maxResultValues",
            "maxNormalizedBytes",
            "maxTraversalDepth",
            "maxRelationResources",
            "maxVisitedValues",
            "maxReturnedPaths",
            "maxMembershipNames",
            "maxFacets",
            "maxResidualRows",
            "maxResidualBytes",
            "workloadProfile",
        }
        if set(value) != keys:
            raise ValueError("safety budget contains unknown or missing fields")
        return cls(
            deadline_ms=cast(int, value["deadlineMs"]),
            max_result_values=cast(int, value["maxResultValues"]),
            max_normalized_bytes=cast(int, value["maxNormalizedBytes"]),
            max_traversal_depth=cast(int, value["maxTraversalDepth"]),
            max_relation_resources=cast(int, value["maxRelationResources"]),
            max_visited_values=cast(int, value["maxVisitedValues"]),
            max_returned_paths=cast(int, value["maxReturnedPaths"]),
            max_membership_names=cast(int, value["maxMembershipNames"]),
            max_facets=cast(int, value["maxFacets"]),
            max_residual_rows=cast(int, value["maxResidualRows"]),
            max_residual_bytes=cast(int, value["maxResidualBytes"]),
            workload_profile=cast(str, value["workloadProfile"]),
        )


@dataclass(frozen=True, slots=True)
class TraversalSpec:
    start: RecordReference
    relation_collections: tuple[ResourceRef, ...] = ()
    all_neighbors: bool = False
    direction: str = "outbound"
    min_depth: int = 1
    max_depth: int = 1
    record_filter: ValueExpression | None = None
    relation_predicates: Mapping[str, ValueExpression] = field(default_factory=dict)
    uniqueness: str = "simple-path"
    result_shape: str = "records"
    registry_fingerprint: str | None = None

    def __post_init__(self) -> None:
        relations = tuple(sorted({ResourceRef.parse(item) for item in self.relation_collections}))
        if not isinstance(self.all_neighbors, bool):
            raise TypeError("all_neighbors must be boolean")
        if bool(relations) == self.all_neighbors and self.registry_fingerprint is None:
            raise ValueError(
                "traversal requires explicit relations or unresolved all_neighbors, "
                "not both/neither"
            )
        if self.registry_fingerprint is not None:
            _fingerprint(self.registry_fingerprint, "traversal registry fingerprint")
            if not self.all_neighbors:
                raise ValueError("registry fingerprint is only valid for all_neighbors closure")
        if self.direction not in {"outbound", "inbound", "any"}:
            raise ValueError("traversal direction must be outbound, inbound, or any")
        _positive(self.min_depth, "traversal minimum depth", maximum=32)
        _positive(self.max_depth, "traversal maximum depth", maximum=32)
        if self.min_depth > self.max_depth:
            raise ValueError("traversal minimum depth cannot exceed maximum depth")
        if self.uniqueness != "simple-path":
            raise ValueError("Meridian V1 traversal uniqueness is simple-path")
        if self.result_shape not in {"records", "relations", "paths"}:
            raise ValueError("traversal result shape is invalid")
        predicates: dict[str, ValueExpression] = {}
        for key, predicate in sorted(self.relation_predicates.items()):
            ref = ResourceRef.parse(key, catalog="structured")
            if ref not in relations:
                raise ValueError("relation predicate must be qualified by a selected Collection")
            predicates[ref.canonical] = predicate
        object.__setattr__(self, "relation_collections", relations)
        object.__setattr__(self, "relation_predicates", MappingProxyType(predicates))

    @property
    def resolved(self) -> bool:
        return not self.all_neighbors or self.registry_fingerprint is not None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "start": self.start.to_dict(),
            "relationSelector": {
                "kind": "allNeighbors" if self.all_neighbors else "explicit",
                "relationCollections": [item.to_dict() for item in self.relation_collections],
                "registryFingerprint": self.registry_fingerprint,
            },
            "direction": self.direction,
            "minDepth": self.min_depth,
            "maxDepth": self.max_depth,
            "recordFilter": None if self.record_filter is None else self.record_filter.to_dict(),
            "relationPredicates": {
                key: predicate.to_dict() for key, predicate in self.relation_predicates.items()
            },
            "uniqueness": self.uniqueness,
            "resultShape": self.result_shape,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> TraversalSpec:
        required = {
            "start",
            "relationSelector",
            "direction",
            "minDepth",
            "maxDepth",
            "recordFilter",
            "relationPredicates",
            "uniqueness",
            "resultShape",
        }
        if set(value) != required:
            raise ValueError("traversal contains unknown or missing fields")
        start = value["start"]
        selector = value["relationSelector"]
        predicates = value["relationPredicates"]
        if not isinstance(start, Mapping):
            raise TypeError("traversal start must be an object")
        if not isinstance(selector, Mapping) or set(selector) != {
            "kind",
            "relationCollections",
            "registryFingerprint",
        }:
            raise ValueError("traversal relation selector is invalid")
        if not isinstance(predicates, Mapping):
            raise TypeError("traversal relation predicates must be an object")
        relations = _array(selector["relationCollections"], "relationCollections")
        relation_values: list[ResourceRef] = []
        for item in relations:
            if not isinstance(item, Mapping):
                raise TypeError("relation Collection reference must be an object")
            relation_values.append(ResourceRef.parse(item))
        record_filter = value["recordFilter"]
        if record_filter is not None and not isinstance(record_filter, Mapping):
            raise TypeError("traversal record filter must be null or an object")
        parsed_predicates: dict[str, ValueExpression] = {}
        for key, item in predicates.items():
            if not isinstance(key, str) or not isinstance(item, Mapping):
                raise TypeError("relation predicates must map Collection refs to expressions")
            parsed_predicates[key] = expression_from_dict(item)
        kind = selector["kind"]
        if kind not in {"explicit", "allNeighbors"}:
            raise ValueError("traversal relation selector kind is invalid")
        return cls(
            start=RecordReference.from_mapping(start),
            relation_collections=tuple(relation_values),
            all_neighbors=kind == "allNeighbors",
            direction=cast(str, value["direction"]),
            min_depth=cast(int, value["minDepth"]),
            max_depth=cast(int, value["maxDepth"]),
            record_filter=(None if record_filter is None else expression_from_dict(record_filter)),
            relation_predicates=parsed_predicates,
            uniqueness=cast(str, value["uniqueness"]),
            result_shape=cast(str, value["resultShape"]),
            registry_fingerprint=cast(str | None, selector["registryFingerprint"]),
        )


@dataclass(frozen=True, slots=True)
class QueryOperation:
    catalog: str
    targets: tuple[QueryTarget, ...]
    operation: str
    result: ResultSpec = field(default_factory=ResultSpec)
    filter: ValueExpression | None = None
    joins: tuple[Join, ...] = ()
    grouping: tuple[ValueExpression, ...] = ()
    aggregates: tuple[NamedAggregate, ...] = ()
    traversal: TraversalSpec | None = None
    order: tuple[Sort, ...] = ()
    page: PageSpec = field(default_factory=PageSpec)
    consistency: str = "strong"
    budget: SafetyBudget = field(default_factory=SafetyBudget)
    options: Mapping[str, JsonValue] = field(default_factory=dict)
    extensions: Mapping[str, JsonValue] = field(default_factory=dict)
    format_version: str = QUERY_FORMAT_VERSION

    def __post_init__(self) -> None:
        if self.format_version != QUERY_FORMAT_VERSION:
            raise ValueError(f"query formatVersion must be {QUERY_FORMAT_VERSION!r}")
        if self.catalog not in {"structured", "evidence"}:
            raise ValueError("V1 query Operations belong to structured or evidence Catalogs")
        if self.operation not in _OPERATIONS:
            raise ValueError("unsupported query Operation")
        targets = tuple(self.targets)
        if not targets or len({item.resource for item in targets}) != len(targets):
            raise ValueError("query targets must be non-empty and unique")
        if any(item.resource.catalog != self.catalog for item in targets):
            raise ValueError("query targets must belong to the Operation Catalog")
        target_map = {item.resource: item for item in targets}
        for join in self.joins:
            target = target_map.get(join.target.resource)
            if target is None or target != join.target:
                raise ValueError("every join target must appear exactly in query targets")
        if self.operation == "traverse" and self.traversal is None:
            raise ValueError("traverse Operation requires a traversal plan")
        if self.operation != "traverse" and self.traversal is not None:
            raise ValueError("traversal plan is only valid for traverse Operation")
        if self.operation == "aggregate" and not self.aggregates:
            raise ValueError("aggregate Operation requires at least one aggregate")
        if self.operation != "aggregate" and (self.grouping or self.aggregates):
            raise ValueError("grouping and aggregates are only valid for aggregate Operation")
        expected_shape = (
            self.traversal.result_shape
            if self.traversal is not None
            else "aggregate"
            if self.operation == "aggregate"
            else "search"
            if self.operation == "search"
            else "records"
        )
        if self.result.shape != expected_shape:
            raise ValueError("query result shape does not match its Operation")
        if self.consistency not in {"strong", "session", "eventual"}:
            raise ValueError("unsupported query consistency")
        aliases = tuple(item.name for item in self.aggregates)
        if len(set(aliases)) != len(aliases):
            raise ValueError("aggregate names must be unique")
        unknown_options = set(self.options) - _OPTION_KEYS
        if unknown_options:
            raise ValueError(
                f"query options are not V1 logical options: {sorted(unknown_options)!r}"
            )
        for name in ("estimatedBytes", "estimatedRows", "resultByteLimit"):
            if name in self.options:
                option = self.options[name]
                if isinstance(option, bool) or not isinstance(option, int) or option < 0:
                    raise ValueError(f"query option {name} must be a non-negative integer")
        if "scopeInjected" in self.options and not isinstance(self.options["scopeInjected"], bool):
            raise TypeError("query option scopeInjected must be boolean")
        for key in self.extensions:
            ExtensionPolicy.validate_key(key)
        canonical_json_bytes(self.options)
        canonical_json_bytes(self.extensions)
        object.__setattr__(self, "targets", targets)
        immutable_options = _immutable_json(cast(JsonValue, self.options))
        immutable_extensions = _immutable_json(cast(JsonValue, self.extensions))
        object.__setattr__(
            self,
            "options",
            cast(Mapping[str, JsonValue], immutable_options),
        )
        object.__setattr__(
            self,
            "extensions",
            cast(Mapping[str, JsonValue], immutable_extensions),
        )

    @property
    def fingerprint(self) -> str:
        import hashlib

        return f"sha256:{hashlib.sha256(self.canonical_bytes).hexdigest()}"

    @property
    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    @property
    def resources(self) -> tuple[ResourceRef, ...]:
        resources = {target.resource for target in self.targets}
        resources.update(join.target.resource for join in self.joins)
        if self.traversal is not None:
            resources.update(self.traversal.relation_collections)
        return tuple(sorted(resources))

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "formatVersion": self.format_version,
            "catalog": self.catalog,
            "targets": [item.to_dict() for item in self.targets],
            "operation": self.operation,
            "result": self.result.to_dict(),
            "filter": None if self.filter is None else self.filter.to_dict(),
            "joins": [item.to_dict() for item in self.joins],
            "grouping": [item.to_dict() for item in self.grouping],
            "aggregates": [item.to_dict() for item in self.aggregates],
            "traversal": None if self.traversal is None else self.traversal.to_dict(),
            "order": [item.to_dict() for item in self.order],
            "page": self.page.to_dict(),
            "consistency": self.consistency,
            "options": {
                "budget": self.budget.to_dict(),
                **cast(dict[str, JsonValue], _mutable_json(cast(JsonValue, self.options))),
            },
            "extensions": cast(
                dict[str, JsonValue], _mutable_json(cast(JsonValue, self.extensions))
            ),
        }

    def to_core_operation(
        self,
        requirements: Sequence[CapabilityRequirement] = (),
    ) -> Operation:
        """Embed this wire plan in the released Core 1.0.0 Operation envelope."""

        method = "query" if self.operation == "scan" else self.operation
        base = CapabilityRequirement(
            operation_contract=f"meridian.{self.catalog}.{method}",
            operation_version=QUERY_CONTRACT_VERSION,
            guarantees=("single-binding",),
            minimum_limits={"pageSize": self.page.size},
        )
        merged = {
            (item.operation_contract, item.operation_version): item
            for item in (base, *requirements)
        }
        return Operation(
            catalog=self.catalog,
            operation_contract=f"meridian.{self.catalog}.{method}",
            operation_version=QUERY_CONTRACT_VERSION,
            resources=self.resources,
            input={"queryPlan": self.to_dict()},
            requirements=tuple(merged[key] for key in sorted(merged)),
            read_only=self.operation in {"get", "scan", "search", "aggregate", "traverse"},
            idempotent=self.operation not in {"insert"},
        )

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, object],
        *,
        extension_policy: ExtensionPolicy = NO_EXTENSIONS,
    ) -> QueryOperation:
        required = {
            "formatVersion",
            "catalog",
            "targets",
            "operation",
            "result",
            "filter",
            "joins",
            "grouping",
            "aggregates",
            "traversal",
            "order",
            "page",
            "consistency",
            "options",
            "extensions",
        }
        if set(value) != required:
            raise ValueError("query Operation contains unknown or missing root fields")
        targets = tuple(
            QueryTarget.from_mapping(_mapping(item, "target"))
            for item in _array(value["targets"], "targets")
        )
        result = ResultSpec.from_mapping(_mapping(value["result"], "result"))
        filter_value = value["filter"]
        if filter_value is not None and not isinstance(filter_value, Mapping):
            raise TypeError("query filter must be null or an object")
        joins = tuple(
            Join.from_mapping(_mapping(item, "join")) for item in _array(value["joins"], "joins")
        )
        grouping = tuple(
            expression_from_dict(_mapping(item, "grouping expression"))
            for item in _array(value["grouping"], "grouping")
        )
        aggregates: list[NamedAggregate] = []
        for item in _array(value["aggregates"], "aggregates"):
            mapping = _mapping(item, "aggregate")
            if set(mapping) != {"name", "expression"}:
                raise ValueError("named aggregate contains unknown or missing fields")
            raw = _mapping(mapping["expression"], "aggregate expression")
            parsed = expression_from_dict(raw)
            from ..ast import Aggregate

            if not isinstance(parsed, Aggregate):
                raise TypeError("named aggregate expression must be an aggregate")
            aggregates.append(NamedAggregate(cast(str, mapping["name"]), parsed))
        traversal_value = value["traversal"]
        if traversal_value is not None and not isinstance(traversal_value, Mapping):
            raise TypeError("traversal must be null or an object")
        order: list[Sort] = []
        for item in _array(value["order"], "order"):
            mapping = _mapping(item, "sort")
            if set(mapping) != {"expression", "direction", "nulls"}:
                raise ValueError("sort contains unknown or missing fields")
            order.append(
                Sort(
                    expression_from_dict(_mapping(mapping["expression"], "sort expression")),
                    cast(str, mapping["direction"]),
                    cast(str, mapping["nulls"]),
                )
            )
        options = _mapping(value["options"], "options")
        if "budget" not in options:
            raise ValueError("query options require a safety budget")
        budget = SafetyBudget.from_mapping(_mapping(options["budget"], "budget"))
        logical_options = {
            key: cast(JsonValue, item) for key, item in options.items() if key != "budget"
        }
        extensions = _mapping(value["extensions"], "extensions")
        normalized_extensions = extension_policy.normalize(extensions)
        return cls(
            format_version=cast(str, value["formatVersion"]),
            catalog=cast(str, value["catalog"]),
            targets=targets,
            operation=cast(str, value["operation"]),
            result=result,
            filter=(None if filter_value is None else expression_from_dict(filter_value)),
            joins=joins,
            grouping=grouping,
            aggregates=tuple(aggregates),
            traversal=(
                None if traversal_value is None else TraversalSpec.from_mapping(traversal_value)
            ),
            order=tuple(order),
            page=PageSpec.from_mapping(_mapping(value["page"], "page")),
            consistency=cast(str, value["consistency"]),
            budget=budget,
            options=logical_options,
            extensions=normalized_extensions,
        )


def _array(value: object, name: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise TypeError(f"{name} must be an array")
    return value


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be an object")
    return cast(Mapping[str, object], value)


__all__ = [
    "NO_EXTENSIONS",
    "QUERY_CAPABILITY_FORMAT_VERSION",
    "QUERY_CONTRACT_VERSION",
    "QUERY_FORMAT_VERSION",
    "ExtensionPolicy",
    "Join",
    "PageSpec",
    "QueryOperation",
    "QueryTarget",
    "ResultSpec",
    "SafetyBudget",
    "TraversalSpec",
]
