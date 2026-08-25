# SPDX-License-Identifier: Apache-2.0
"""Single-Binding planner with native, exact rewrite, and proved residual modes."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType

from meridian_storage.semantics import JsonValue, sha256_fingerprint

from ..adapter import (
    CompiledQuery,
    QueryCapabilities,
    QueryTranslator,
    TranslationContext,
    assert_translation_contract,
)
from ..diagnostics import Diagnostic, ExplainRecord, Severity
from ..errors import StaleRegistryPlan, UnsafeResidual, UnsupportedSemantic
from ..requirements import RequirementGraph, SemanticRequirement
from ..validation import CompileValidation
from ..wire import QueryOperation, SafetyBudget


class ImplementationMode(StrEnum):
    NATIVE = "NATIVE"
    EXACT_REWRITE = "EXACT_REWRITE"
    BOUNDED_RESIDUAL = "BOUNDED_RESIDUAL"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class RewriteRule:
    semantic_id: str
    lower_semantics: tuple[str, ...]
    equivalence_rule: str

    def __post_init__(self) -> None:
        if not self.semantic_id or not self.lower_semantics or not self.equivalence_rule:
            raise ValueError("exact rewrite requires semantic, lower semantics, and equivalence id")
        object.__setattr__(self, "lower_semantics", tuple(sorted(set(self.lower_semantics))))


@dataclass(frozen=True, slots=True)
class ResidualInput:
    operation: QueryOperation
    requirement: SemanticRequirement
    estimated_rows: int
    estimated_bytes: int
    budget: SafetyBudget


ResidualProof = Callable[[ResidualInput], bool]


@dataclass(frozen=True, slots=True)
class ResidualRule:
    semantic_id: str
    exact_function: str
    proof_id: str
    max_rows: int
    max_bytes: int
    proof: ResidualProof = field(compare=False, repr=False)

    def __post_init__(self) -> None:
        if not self.semantic_id or not self.exact_function or not self.proof_id:
            raise ValueError("residual registration requires stable semantic/function/proof ids")
        if (
            isinstance(self.max_rows, bool)
            or isinstance(self.max_bytes, bool)
            or self.max_rows <= 0
            or self.max_bytes <= 0
        ):
            raise ValueError("residual registration limits must be positive")


@dataclass(frozen=True, slots=True)
class PlannedQuery:
    operation: QueryOperation
    binding_id: str
    requirements: RequirementGraph
    assignments: Mapping[str, ImplementationMode]
    capability_fingerprint: str
    registry_fingerprint: str
    schema_fingerprints: Mapping[str, str]
    diagnostics: tuple[Diagnostic, ...] = ()
    empty_result: bool = False

    def __post_init__(self) -> None:
        if not self.binding_id:
            raise ValueError("planned query requires one Binding id")
        for name in ("capability_fingerprint", "registry_fingerprint"):
            if not getattr(self, name).startswith("sha256:"):
                raise ValueError(f"{name} must be a sha256 fingerprint")
        expected = {item.semantic_id for item in self.requirements.requirements}
        if set(self.assignments) != expected:
            raise ValueError("planner assignments must cover every semantic requirement")
        object.__setattr__(
            self,
            "assignments",
            MappingProxyType(dict(sorted(self.assignments.items()))),
        )
        schemas = dict(sorted(self.schema_fingerprints.items()))
        if any(not value.startswith("sha256:") for value in schemas.values()):
            raise ValueError("planned query Schema fingerprints must use sha256")
        object.__setattr__(self, "schema_fingerprints", MappingProxyType(schemas))
        object.__setattr__(
            self,
            "diagnostics",
            tuple(sorted(set(self.diagnostics), key=lambda item: item.sort_key)),
        )

    @property
    def fingerprint(self) -> str:
        return sha256_fingerprint(self.to_dict())

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "formatVersion": "meridian.query.plan.v1",
            "operation": self.operation.to_dict(),
            "bindingId": self.binding_id,
            "requirementsFingerprint": self.requirements.fingerprint,
            "assignments": {key: value.value for key, value in self.assignments.items()},
            "capabilityFingerprint": self.capability_fingerprint,
            "registryFingerprint": self.registry_fingerprint,
            "schemaFingerprints": dict(self.schema_fingerprints),
            "emptyResult": self.empty_result,
        }

    def explain(self) -> ExplainRecord:
        return ExplainRecord(
            plan_fingerprint=self.fingerprint,
            binding_id=self.binding_id,
            assignments={key: value.value for key, value in self.assignments.items()},
            limits={
                "deadlineMs": self.operation.budget.deadline_ms,
                "pageSize": self.operation.page.size,
                "resultBytes": self.operation.budget.max_normalized_bytes,
                "resultValues": self.operation.budget.max_result_values,
            },
            diagnostics=self.diagnostics,
            details={
                "operation": self.operation.operation,
                "options": dict(self.operation.options),
                "emptyResult": self.empty_result,
            },
        )

    def translation_context(self, *, scope_fingerprint: str) -> TranslationContext:
        return TranslationContext(
            binding_id=self.binding_id,
            plan_fingerprint=self.fingerprint,
            registry_fingerprint=self.registry_fingerprint,
            schema_fingerprints=self.schema_fingerprints,
            scope_fingerprint=scope_fingerprint,
            deadline_ms=self.operation.budget.deadline_ms,
        )


class QueryPlanner:
    def __init__(
        self,
        capabilities: QueryCapabilities,
        *,
        rewrites: Sequence[RewriteRule] = (),
        residuals: Sequence[ResidualRule] = (),
    ) -> None:
        self.capabilities = capabilities
        rewrite_map = {item.semantic_id: item for item in rewrites}
        if len(rewrite_map) != len(rewrites):
            raise ValueError("rewrite rules require unique semantic ids")
        residual_map = {item.semantic_id: item for item in residuals}
        if len(residual_map) != len(residuals):
            raise ValueError("residual rules require unique semantic ids")
        self.rewrites: Mapping[str, RewriteRule] = MappingProxyType(
            dict(sorted(rewrite_map.items()))
        )
        self.residuals: Mapping[str, ResidualRule] = MappingProxyType(
            dict(sorted(residual_map.items()))
        )

    def plan(
        self,
        validation: CompileValidation,
        *,
        registry_fingerprint: str,
    ) -> PlannedQuery:
        traversal = validation.operation.traversal
        if (
            traversal is not None
            and traversal.registry_fingerprint is not None
            and registry_fingerprint != traversal.registry_fingerprint
        ):
            diagnostic = Diagnostic(
                "MERIDIAN_QUERY_STALE_REGISTRY",
                "registry changed after all_neighbors planning",
                path="/traversal/relationSelector/registryFingerprint",
                requirement="traversal.registry-fingerprint",
            )
            raise StaleRegistryPlan(diagnostic.message, diagnostics=(diagnostic,))
        empty = bool(
            validation.operation.traversal is not None
            and validation.operation.traversal.all_neighbors
            and not validation.operation.traversal.relation_collections
        )
        assignments: dict[str, ImplementationMode] = {}
        diagnostics: list[Diagnostic] = list(validation.diagnostics)
        for requirement in validation.requirements.requirements:
            mode, reason = self._select(requirement, validation.operation, empty=empty)
            assignments[requirement.semantic_id] = mode
            diagnostics.append(
                Diagnostic(
                    "MERIDIAN_QUERY_IMPLEMENTATION_SELECTED",
                    f"selected {mode.value} for {requirement.semantic_id}",
                    severity=Severity.INFO
                    if mode is not ImplementationMode.REJECTED
                    else Severity.ERROR,
                    path="/requirements",
                    requirement=requirement.semantic_id,
                    hint=reason,
                )
            )
            if mode is ImplementationMode.REJECTED:
                raise UnsupportedSemantic(
                    f"no exact bounded implementation exists for {requirement.semantic_id}",
                    diagnostics=tuple(diagnostics),
                    operation_contract=validation.requirements.operation_contract,
                )
        schemas = {
            item.resource.canonical: item.schema.fingerprint
            for item in validation.resolved_resources
        }
        return PlannedQuery(
            operation=validation.operation,
            binding_id=validation.binding_id,
            requirements=validation.requirements,
            assignments=assignments,
            capability_fingerprint=self.capabilities.fingerprint,
            registry_fingerprint=registry_fingerprint,
            schema_fingerprints=schemas,
            diagnostics=tuple(diagnostics),
            empty_result=empty,
        )

    def _select(
        self,
        requirement: SemanticRequirement,
        operation: QueryOperation,
        *,
        empty: bool,
    ) -> tuple[ImplementationMode, str | None]:
        if empty:
            return ImplementationMode.EXACT_REWRITE, "Meridian empty-closure equivalence"
        supported, reason = self.capabilities.supports(
            requirement,
            operation=operation.operation,
        )
        if supported and self.capabilities.is_native(requirement.semantic_id):
            return ImplementationMode.NATIVE, None
        base_supported, base_reason = self.capabilities.supports_base(
            requirement,
            operation=operation.operation,
        )
        if not base_supported:
            return ImplementationMode.REJECTED, base_reason
        rewrite = self.rewrites.get(requirement.semantic_id)
        if rewrite is not None and all(
            self.capabilities.is_native(item) for item in rewrite.lower_semantics
        ):
            return ImplementationMode.EXACT_REWRITE, rewrite.equivalence_rule
        residual = self.residuals.get(requirement.semantic_id)
        if residual is not None:
            proved, proof_reason = _prove_residual(operation, requirement, residual)
            if proved:
                return ImplementationMode.BOUNDED_RESIDUAL, residual.proof_id
            reason = proof_reason
        return ImplementationMode.REJECTED, reason


def compile_query(
    plan: PlannedQuery,
    translator: QueryTranslator,
    *,
    scope_fingerprint: str,
) -> CompiledQuery:
    if plan.empty_result:
        raise ValueError("an empty-result plan must not be sent to an Adapter compiler")
    if translator.capabilities.fingerprint != plan.capability_fingerprint:
        raise StaleRegistryPlan(
            "query compiler capabilities changed after planning",
            diagnostics=(
                Diagnostic(
                    "MERIDIAN_QUERY_CAPABILITY_CHANGED",
                    "query compiler capabilities changed after planning",
                    path="/capabilityFingerprint",
                    requirement="planning.capability-fingerprint",
                ),
            ),
        )
    context = plan.translation_context(scope_fingerprint=scope_fingerprint)
    return assert_translation_contract(translator, plan, context)


def _prove_residual(
    operation: QueryOperation,
    requirement: SemanticRequirement,
    rule: ResidualRule,
) -> tuple[bool, str | None]:
    rows = operation.options.get("estimatedRows")
    size = operation.options.get("estimatedBytes")
    if (
        isinstance(rows, bool)
        or not isinstance(rows, int)
        or rows < 0
        or isinstance(size, bool)
        or not isinstance(size, int)
        or size < 0
    ):
        return False, "residual requires proved estimatedRows and estimatedBytes"
    maximum_rows = min(rule.max_rows, operation.budget.max_residual_rows)
    maximum_bytes = min(rule.max_bytes, operation.budget.max_residual_bytes)
    if rows > maximum_rows or size > maximum_bytes:
        return False, "residual estimate exceeds its reviewed bound"
    value = ResidualInput(operation, requirement, rows, size, operation.budget)
    try:
        proved = rule.proof(value) is True
    except Exception as exc:
        raise UnsafeResidual(
            "residual proof raised instead of proving exact bounded behavior",
            diagnostics=(
                Diagnostic(
                    "MERIDIAN_QUERY_UNSAFE_RESIDUAL",
                    f"residual proof failed with {type(exc).__name__}",
                    path="/requirements",
                    requirement=requirement.semantic_id,
                ),
            ),
        ) from exc
    return proved, None if proved else "residual proof did not establish its bound"


__all__ = [
    "ImplementationMode",
    "PlannedQuery",
    "QueryPlanner",
    "ResidualInput",
    "ResidualProof",
    "ResidualRule",
    "RewriteRule",
    "compile_query",
]
