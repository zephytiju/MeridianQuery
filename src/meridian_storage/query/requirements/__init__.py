# SPDX-License-Identifier: Apache-2.0
"""Shared semantic requirement graph and deterministic capability inference."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType

from meridian_storage.registry.resources import CapabilityRequirement
from meridian_storage.semantics import JsonValue, sha256_fingerprint

from ..ast import (
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
    NullTest,
    Parameter,
    TimestampRange,
    UnaryExpression,
    ValueExpression,
)
from ..wire import QUERY_CONTRACT_VERSION, QueryOperation

FieldTypeResolver = Callable[[Field], str | None]


def _tokens(values: Iterable[str]) -> tuple[str, ...]:
    result = tuple(sorted(set(values)))
    if any(not item for item in result):
        raise ValueError("semantic requirement tokens cannot be empty")
    return result


def _limits(values: Mapping[str, int]) -> Mapping[str, int]:
    result: dict[str, int] = {}
    for key, value in sorted(values.items()):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("semantic requirement limits must be non-negative integers")
        result[key] = value
    return MappingProxyType(result)


@dataclass(frozen=True, slots=True)
class SemanticRequirement:
    semantic_id: str
    semantic_version: str = QUERY_CONTRACT_VERSION
    operators: tuple[str, ...] = ()
    logical_types: tuple[str, ...] = ()
    required_limits: Mapping[str, int] = field(default_factory=dict)
    transaction_guarantees: tuple[str, ...] = ()
    consistency_class: str = "strong"
    optional_features: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.semantic_id or not self.semantic_version:
            raise ValueError("semantic id and version cannot be empty")
        if self.consistency_class not in {"strong", "session", "eventual"}:
            raise ValueError("semantic requirement consistency is invalid")
        object.__setattr__(self, "operators", _tokens(self.operators))
        object.__setattr__(self, "logical_types", _tokens(self.logical_types))
        object.__setattr__(self, "required_limits", _limits(self.required_limits))
        object.__setattr__(
            self,
            "transaction_guarantees",
            _tokens(self.transaction_guarantees),
        )
        object.__setattr__(self, "optional_features", _tokens(self.optional_features))

    @property
    def fingerprint(self) -> str:
        return sha256_fingerprint(self.to_dict())

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "semanticId": self.semantic_id,
            "semanticVersion": self.semantic_version,
            "operators": list(self.operators),
            "logicalTypes": list(self.logical_types),
            "requiredLimits": dict(self.required_limits),
            "transactionGuarantees": list(self.transaction_guarantees),
            "consistencyClass": self.consistency_class,
            "optionalFeatures": list(self.optional_features),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> SemanticRequirement:
        allowed = {
            "semanticId",
            "semanticVersion",
            "operators",
            "logicalTypes",
            "requiredLimits",
            "transactionGuarantees",
            "consistencyClass",
            "optionalFeatures",
        }
        if {"semanticId", "semanticVersion"} - set(value) or set(value) - allowed:
            raise ValueError("SemanticRequirement contains unknown or missing fields")
        return cls(
            semantic_id=str(value["semanticId"]),
            semantic_version=str(value["semanticVersion"]),
            operators=_string_values(value.get("operators", ()), "operators"),
            logical_types=_string_values(value.get("logicalTypes", ()), "logicalTypes"),
            required_limits=_integer_values(value.get("requiredLimits", {})),
            transaction_guarantees=_string_values(
                value.get("transactionGuarantees", ()), "transactionGuarantees"
            ),
            consistency_class=str(value.get("consistencyClass", "strong")),
            optional_features=_string_values(value.get("optionalFeatures", ()), "optionalFeatures"),
        )


@dataclass(frozen=True, slots=True)
class RequirementGraph:
    operation_contract: str
    requirements: tuple[SemanticRequirement, ...]

    def __post_init__(self) -> None:
        requirements = tuple(sorted(self.requirements, key=lambda item: item.semantic_id))
        if not requirements or len({item.semantic_id for item in requirements}) != len(
            requirements
        ):
            raise ValueError("requirement graph must contain unique semantic ids")
        object.__setattr__(self, "requirements", requirements)

    @property
    def fingerprint(self) -> str:
        return sha256_fingerprint(self.to_dict())

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "formatVersion": "meridian.query.requirements.v1",
            "operationContract": self.operation_contract,
            "requirements": [item.to_dict() for item in self.requirements],
        }

    def to_core_requirement(self) -> CapabilityRequirement:
        guarantees = _tokens(
            guarantee
            for requirement in self.requirements
            for guarantee in requirement.transaction_guarantees
        )
        limits: dict[str, int] = {}
        for requirement in self.requirements:
            for key, value in requirement.required_limits.items():
                limits[key] = max(limits.get(key, 0), value)
        return CapabilityRequirement(
            operation_contract=self.operation_contract,
            operation_version=QUERY_CONTRACT_VERSION,
            guarantees=guarantees,
            minimum_limits=limits,
        )


def infer_requirements(
    operation: QueryOperation,
    *,
    field_type_resolver: FieldTypeResolver | None = None,
) -> RequirementGraph:
    """Infer the complete graph from the normalized plan; no vendor allowlist is used."""

    expressions = _expressions(operation)
    logical_types = _logical_types(expressions, field_type_resolver)
    requirements: list[SemanticRequirement] = [
        SemanticRequirement(
            semantic_id=f"query.operation.{operation.operation}",
            operators=(operation.operation,),
            logical_types=logical_types,
            required_limits={
                "pageSize": operation.page.size,
                "resultValues": operation.budget.max_result_values,
                "resultBytes": operation.budget.max_normalized_bytes,
                "deadlineMs": operation.budget.deadline_ms,
            },
            transaction_guarantees=("single-binding",),
            consistency_class=operation.consistency,
            optional_features=("point-in-time",) if operation.page.point_in_time else (),
        )
    ]
    operators = sorted({operator for item in expressions for operator in item.operators})
    for operator in operators:
        limits: dict[str, int] = {}
        features: list[str] = []
        if operator in {"in", "notIn"}:
            limits["membershipNames"] = _membership_size(expressions)
        if operator == "fullText":
            matches = [
                item
                for expression in expressions
                for item in _walk(expression)
                if isinstance(item, FullTextMatch)
            ]
            limits["facetBuckets"] = max((len(item.facets) for item in matches), default=0)
            features.extend(
                feature
                for item in matches
                for feature in (
                    f"analyzer:{item.analyzer}",
                    f"ranking:{item.ranking}",
                    "highlights" if item.highlights else "",
                    "facets" if item.facets else "",
                )
                if feature
            )
        if operator in {"distance", "distanceWithin", "point"}:
            features.extend(("crs:EPSG:4326", "distance-unit:m"))
        requirements.append(
            SemanticRequirement(
                semantic_id=f"query.operator.{operator}",
                operators=(operator,),
                logical_types=logical_types,
                required_limits=limits,
                consistency_class=operation.consistency,
                optional_features=tuple(features),
            )
        )
    if operation.order:
        requirements.append(
            SemanticRequirement(
                semantic_id="query.pagination.keyset",
                operators=("order",),
                logical_types=_logical_types(
                    tuple(item.expression for item in operation.order), field_type_resolver
                ),
                required_limits={"pageSize": operation.page.size},
                consistency_class=operation.consistency,
                optional_features=("live-keyset",),
            )
        )
    if operation.traversal is not None:
        traversal = operation.traversal
        requirements.append(
            SemanticRequirement(
                semantic_id="query.traversal.bounded",
                operators=("traverse",),
                required_limits={
                    "traversalDepth": traversal.max_depth,
                    "relationResources": len(traversal.relation_collections),
                    "visitedValues": operation.budget.max_visited_values,
                    "returnedPaths": operation.budget.max_returned_paths,
                },
                consistency_class=operation.consistency,
                optional_features=(
                    f"direction:{traversal.direction}",
                    f"result:{traversal.result_shape}",
                    "all-neighbors" if traversal.all_neighbors else "explicit-relations",
                    "simple-path",
                ),
            )
        )
    contract_method = "query" if operation.operation == "scan" else operation.operation
    return RequirementGraph(
        f"meridian.{operation.catalog}.{contract_method}",
        tuple(requirements),
    )


def _expressions(operation: QueryOperation) -> tuple[ValueExpression, ...]:
    values: list[ValueExpression] = []
    if operation.filter is not None:
        values.append(operation.filter)
    values.extend(item.expression for item in operation.result.projection)
    values.extend(item.on for item in operation.joins)
    values.extend(operation.grouping)
    values.extend(item.aggregate for item in operation.aggregates)
    values.extend(item.expression for item in operation.order)
    if operation.traversal is not None:
        if operation.traversal.record_filter is not None:
            values.append(operation.traversal.record_filter)
        values.extend(operation.traversal.relation_predicates.values())
    return tuple(values)


def _logical_types(
    expressions: tuple[ValueExpression, ...],
    resolver: FieldTypeResolver | None,
) -> tuple[str, ...]:
    result: set[str] = set()
    for expression in expressions:
        for item in _walk(expression):
            if isinstance(item, (Literal, Parameter)):
                logical = item.logical_type
                if isinstance(logical, str):
                    result.add(logical)
                elif isinstance(logical, Mapping):
                    result.add(str(logical["kind"]))
        for referenced_field in expression.referenced_fields:
            if resolver is not None:
                resolved = resolver(referenced_field)
                if resolved is not None:
                    result.add(resolved)
    return tuple(sorted(result))


def _walk(expression: ValueExpression) -> tuple[ValueExpression, ...]:
    result = [expression]
    if isinstance(expression, BinaryExpression):
        result.extend(_walk(expression.left))
        result.extend(_walk(expression.right))
    elif isinstance(expression, BooleanExpression):
        for item in expression.operands:
            result.extend(_walk(item))
    elif isinstance(expression, UnaryExpression):
        result.extend(_walk(expression.operand))
    elif isinstance(expression, (NullTest, MembershipExpression)):
        result.extend(_walk(expression.operand))
        if isinstance(expression, MembershipExpression):
            for item in expression.values:
                result.extend(_walk(item))
    elif isinstance(expression, TimestampRange):
        result.extend(_walk(expression.operand))
        if expression.start is not None:
            result.extend(_walk(expression.start))
        if expression.end is not None:
            result.extend(_walk(expression.end))
    elif isinstance(expression, DocumentPath):
        result.extend(_walk(expression.document))
    elif isinstance(expression, (Distance, DistanceWithin)):
        result.extend(_walk(expression.left))
        result.extend(_walk(expression.right))
    elif isinstance(expression, Aggregate) and expression.operand is not None:
        result.extend(_walk(expression.operand))
    return tuple(result)


def _membership_size(expressions: tuple[ValueExpression, ...]) -> int:
    return max(
        (
            len(item.values)
            for expression in expressions
            for item in _walk(expression)
            if isinstance(item, MembershipExpression)
        ),
        default=0,
    )


def _string_values(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise TypeError(f"{name} must be an array")
    if any(not isinstance(item, str) for item in value):
        raise TypeError(f"{name} entries must be strings")
    return tuple(str(item) for item in value)


def _integer_values(value: object) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise TypeError("requiredLimits must be an object")
    result: dict[str, int] = {}
    for key, item in value.items():
        if not isinstance(key, str) or isinstance(item, bool) or not isinstance(item, int):
            raise TypeError("requiredLimits must map strings to integers")
        result[key] = item
    return result


__all__ = [
    "FieldTypeResolver",
    "RequirementGraph",
    "SemanticRequirement",
    "infer_requirements",
]
