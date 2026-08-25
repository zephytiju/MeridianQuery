# SPDX-License-Identifier: Apache-2.0
"""Adapter-author translation contracts for validated logical query plans."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Protocol, cast, runtime_checkable

from meridian_storage.semantics import JsonValue, canonical_json_bytes, sha256_fingerprint

from ..diagnostics import Diagnostic
from ..errors import ExcessiveBudget
from ..requirements import SemanticRequirement
from ..wire import QUERY_CAPABILITY_FORMAT_VERSION, QUERY_CONTRACT_VERSION, SafetyBudget


def _tokens(values: object, name: str, *, allow_empty: bool = True) -> tuple[str, ...]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        raise TypeError(f"{name} must be an array")
    if any(not isinstance(item, str) for item in values):
        raise TypeError(f"{name} entries must be strings")
    result = tuple(sorted(set(values)))
    if (not allow_empty and not result) or any(not item for item in result):
        raise ValueError(f"{name} contains empty or missing tokens")
    if len(result) != len(values):
        raise ValueError(f"{name} entries must be unique")
    return result


def _limits(values: object) -> Mapping[str, int]:
    if not isinstance(values, Mapping):
        raise TypeError("query Capability limits must be an object")
    result: dict[str, int] = {}
    for key, value in sorted(values.items()):
        if (
            not isinstance(key, str)
            or isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
        ):
            raise ValueError("query Capability limits must be non-negative integers")
        result[key] = value
    return MappingProxyType(result)


@dataclass(frozen=True, slots=True)
class QueryCapabilities:
    """Versioned semantic support advertised by an Adapter query compiler."""

    adapter_id: str
    operations: tuple[str, ...]
    native_semantics: tuple[str, ...]
    operators: tuple[str, ...]
    logical_types: tuple[str, ...]
    consistency_classes: tuple[str, ...]
    guarantees: tuple[str, ...] = ("single-binding",)
    features: tuple[str, ...] = ()
    limits: Mapping[str, int] = field(default_factory=dict)
    contract_version: str = QUERY_CONTRACT_VERSION
    format_version: str = QUERY_CAPABILITY_FORMAT_VERSION

    def __post_init__(self) -> None:
        if not self.adapter_id or self.format_version != QUERY_CAPABILITY_FORMAT_VERSION:
            raise ValueError("invalid query Capability identity or format")
        if self.contract_version != QUERY_CONTRACT_VERSION:
            raise ValueError("unsupported query Capability contract version")
        for name in (
            "operations",
            "native_semantics",
            "operators",
            "logical_types",
            "consistency_classes",
            "guarantees",
            "features",
        ):
            object.__setattr__(
                self,
                name,
                _tokens(
                    getattr(self, name),
                    name,
                    allow_empty=name
                    not in {"operations", "native_semantics", "consistency_classes"},
                ),
            )
        unknown_consistency = set(self.consistency_classes) - {"strong", "session", "eventual"}
        if unknown_consistency:
            raise ValueError("query Capability has an unknown consistency class")
        object.__setattr__(self, "limits", _limits(self.limits))

    @property
    def fingerprint(self) -> str:
        return sha256_fingerprint(self.to_dict())

    def supports(
        self,
        requirement: SemanticRequirement,
        *,
        operation: str,
    ) -> tuple[bool, str | None]:
        supported, reason = self.supports_base(requirement, operation=operation)
        if not supported:
            return supported, reason
        if not set(requirement.operators) <= set(self.operators):
            return False, "required operator is not advertised"
        if not set(requirement.optional_features) <= set(self.features):
            return False, "required optional feature is not advertised"
        return True, None

    def supports_base(
        self,
        requirement: SemanticRequirement,
        *,
        operation: str,
    ) -> tuple[bool, str | None]:
        """Check invariants that rewrites and residual evaluation cannot bypass."""

        if operation not in self.operations:
            return False, "query Operation is not advertised"
        if requirement.consistency_class not in self.consistency_classes:
            return False, "required consistency class is not advertised"
        if not set(requirement.logical_types) <= set(self.logical_types):
            return False, "required logical type is not advertised"
        if not set(requirement.transaction_guarantees) <= set(self.guarantees):
            return False, "required guarantee is not advertised"
        for name, minimum in requirement.required_limits.items():
            if self.limits.get(name, -1) < minimum:
                return False, f"limit {name!r} is below the required minimum"
        return True, None

    def is_native(self, semantic_id: str) -> bool:
        return "*" in self.native_semantics or semantic_id in self.native_semantics

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "formatVersion": self.format_version,
            "contractVersion": self.contract_version,
            "adapterId": self.adapter_id,
            "operations": list(self.operations),
            "nativeSemantics": list(self.native_semantics),
            "operators": list(self.operators),
            "logicalTypes": list(self.logical_types),
            "consistencyClasses": list(self.consistency_classes),
            "guarantees": list(self.guarantees),
            "features": list(self.features),
            "limits": dict(self.limits),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> QueryCapabilities:
        required = {
            "formatVersion",
            "contractVersion",
            "adapterId",
            "operations",
            "nativeSemantics",
            "operators",
            "logicalTypes",
            "consistencyClasses",
            "guarantees",
            "features",
            "limits",
        }
        if set(value) != required:
            raise ValueError("QueryCapabilities contains unknown or missing fields")
        return cls(
            adapter_id=cast(str, value["adapterId"]),
            operations=_tokens(value["operations"], "operations", allow_empty=False),
            native_semantics=_tokens(
                value["nativeSemantics"], "nativeSemantics", allow_empty=False
            ),
            operators=_tokens(value["operators"], "operators"),
            logical_types=_tokens(value["logicalTypes"], "logicalTypes"),
            consistency_classes=_tokens(
                value["consistencyClasses"], "consistencyClasses", allow_empty=False
            ),
            guarantees=_tokens(value["guarantees"], "guarantees"),
            features=_tokens(value["features"], "features"),
            limits=_limits(value["limits"]),
            contract_version=cast(str, value["contractVersion"]),
            format_version=cast(str, value["formatVersion"]),
        )


@dataclass(frozen=True, slots=True)
class TranslationContext:
    """Logical compilation inputs; physical connection state stays inside the Adapter."""

    binding_id: str
    plan_fingerprint: str
    registry_fingerprint: str
    schema_fingerprints: Mapping[str, str]
    scope_fingerprint: str
    deadline_ms: int

    def __post_init__(self) -> None:
        if not self.binding_id:
            raise ValueError("translation context requires a Binding id")
        for name in ("plan_fingerprint", "registry_fingerprint", "scope_fingerprint"):
            if not cast(str, getattr(self, name)).startswith("sha256:"):
                raise ValueError(f"{name} must be a sha256 fingerprint")
        if isinstance(self.deadline_ms, bool) or self.deadline_ms <= 0:
            raise ValueError("translation deadline must be a positive integer")
        fingerprints = dict(sorted(self.schema_fingerprints.items()))
        if any(not value.startswith("sha256:") for value in fingerprints.values()):
            raise ValueError("Schema fingerprints must be sha256 fingerprints")
        object.__setattr__(self, "schema_fingerprints", MappingProxyType(fingerprints))


@dataclass(frozen=True, slots=True)
class CompiledQuery:
    """Opaque, parameterized command returned only across the Adapter SPI."""

    adapter_id: str
    plan_fingerprint: str
    command: JsonValue
    parameters: Mapping[str, JsonValue] = field(default_factory=dict)
    expected_result_shape: str = "records"

    def __post_init__(self) -> None:
        if not self.adapter_id or not self.plan_fingerprint.startswith("sha256:"):
            raise ValueError("compiled query identity is invalid")
        canonical_json_bytes(self.command)
        canonical_json_bytes(self.parameters)
        if self.expected_result_shape not in {
            "records",
            "relations",
            "paths",
            "aggregate",
            "search",
        }:
            raise ValueError("compiled query result shape is invalid")
        object.__setattr__(
            self, "parameters", MappingProxyType(dict(sorted(self.parameters.items())))
        )


@dataclass(frozen=True, slots=True)
class NormalizedQueryResult:
    """Logical Data returned by an Adapter compiler/executor pair."""

    data: JsonValue
    cursor: str | None = None
    provenance: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        canonical_json_bytes(self.data)
        if self.cursor is not None and not self.cursor:
            raise ValueError("result cursor must be an opaque non-empty string")
        object.__setattr__(
            self, "provenance", MappingProxyType(dict(sorted(self.provenance.items())))
        )

    def operation_data(self) -> JsonValue:
        """Place pagination inside Data because Core 1.0.0 has no result cursor attribute."""

        return {
            "items": self.data,
            "cursor": self.cursor,
        }


def enforce_result_budget(
    result: NormalizedQueryResult,
    budget: SafetyBudget,
) -> NormalizedQueryResult:
    """Reject normalized output that exceeds the validated logical result budget."""

    encoded_size = len(canonical_json_bytes(result.data))
    value_count = (
        len(result.data)
        if isinstance(result.data, Sequence) and not isinstance(result.data, (str, bytes))
        else 0
        if result.data is None
        else 1
    )
    diagnostics: list[Diagnostic] = []
    if value_count > budget.max_result_values:
        diagnostics.append(
            Diagnostic(
                "MERIDIAN_QUERY_EXCESSIVE_BUDGET",
                "normalized result contains more values than the validated budget",
                path="/result",
                requirement="budget.result-values",
            )
        )
    if encoded_size > budget.max_normalized_bytes:
        diagnostics.append(
            Diagnostic(
                "MERIDIAN_QUERY_EXCESSIVE_BUDGET",
                "normalized result exceeds the validated byte budget",
                path="/result",
                requirement="budget.result-bytes",
            )
        )
    if diagnostics:
        raise ExcessiveBudget(diagnostics[0].message, diagnostics=diagnostics)
    return result


@runtime_checkable
class QueryTranslator(Protocol):
    @property
    def capabilities(self) -> QueryCapabilities: ...

    def compile(self, plan: object, context: TranslationContext) -> CompiledQuery: ...

    def normalize_result(
        self,
        compiled: CompiledQuery,
        raw_result: object,
    ) -> NormalizedQueryResult: ...


def assert_translation_contract(
    translator: QueryTranslator,
    plan: object,
    context: TranslationContext,
) -> CompiledQuery:
    """Small shared conformance assertion used by Adapter repositories."""

    compiled = translator.compile(plan, context)
    if compiled.adapter_id != translator.capabilities.adapter_id:
        raise AssertionError("compiled command Adapter id does not match advertised capabilities")
    if compiled.plan_fingerprint != context.plan_fingerprint:
        raise AssertionError("compiled command is not bound to the validated plan fingerprint")
    return compiled


__all__ = [
    "CompiledQuery",
    "NormalizedQueryResult",
    "QueryCapabilities",
    "QueryTranslator",
    "TranslationContext",
    "assert_translation_contract",
    "enforce_result_budget",
]
