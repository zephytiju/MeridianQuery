# SPDX-License-Identifier: Apache-2.0
"""Capability inference and single-Binding planner tests."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from meridian_storage.semantics import RecordReference, ResourceReference, TraversalResolution

from meridian_storage import ResourceRef
from meridian_storage.query import (
    CompiledQuery,
    CompileValidation,
    ImplementationMode,
    NormalizedQueryResult,
    QueryCapabilities,
    QueryPlanner,
    RegistryView,
    RequirementGraph,
    ResidualInput,
    ResidualRule,
    RewriteRule,
    SemanticRequirement,
    StaleRegistryPlan,
    TranslationContext,
    UnsafeResidual,
    UnsupportedSemantic,
    assert_translation_contract,
    compile_query,
    infer_requirements,
    normalize_builder,
    query,
    validate_compile,
)

FINGERPRINT = "sha256:" + "1" * 64
OTHER_FINGERPRINT = "sha256:" + "4" * 64
SCOPE_FINGERPRINT = "sha256:" + "5" * 64


def _capabilities_for(
    validation: CompileValidation,
    *,
    omit_operators: tuple[str, ...] = (),
    native_semantics: tuple[str, ...] = ("*",),
) -> QueryCapabilities:
    requirements = validation.requirements.requirements
    operators = {
        operator for requirement in requirements for operator in requirement.operators
    } - set(omit_operators)
    logical_types = {
        logical for requirement in requirements for logical in requirement.logical_types
    }
    features = {
        feature for requirement in requirements for feature in requirement.optional_features
    }
    guarantees = {
        guarantee
        for requirement in requirements
        for guarantee in requirement.transaction_guarantees
    }
    limits: dict[str, int] = {}
    for requirement in requirements:
        for name, value in requirement.required_limits.items():
            limits[name] = max(limits.get(name, 0), value)
    return QueryCapabilities(
        "test.adapter",
        (validation.operation.operation,),
        native_semantics,
        tuple(operators),
        tuple(logical_types),
        (validation.operation.consistency,),
        tuple(guarantees),
        tuple(features),
        limits,
    )


def test_inference_walks_nested_literals_memberships_and_full_text(users: ResourceRef) -> None:
    operation = (
        query(users)
        .where({"$and": [{"id": {"$in": ["a", "b", "c"]}}, {"age": {"$gte": 18}}]})
        .full_text("query", fields=("body",), facets=("name",), highlights=("body",))
        .build()
    )
    graph = infer_requirements(operation, field_type_resolver=lambda value: "string")
    by_id = {item.semantic_id: item for item in graph.requirements}
    assert by_id["query.operator.in"].required_limits["membershipNames"] == 3
    assert by_id["query.operator.fullText"].required_limits["facetBuckets"] == 1
    assert {"string", "int64"} <= set(by_id["query.operation.search"].logical_types)
    assert {"analyzer:icu", "ranking:bm25", "facets", "highlights"} <= set(
        by_id["query.operator.fullText"].optional_features
    )
    assert graph.to_core_requirement().operation_contract == "meridian.structured.search"


def test_requirement_serialization_and_validation() -> None:
    requirement = SemanticRequirement(
        "query.operator.eq",
        operators=("eq",),
        logical_types=("string",),
        required_limits={"pageSize": 10},
        transaction_guarantees=("single-binding",),
        optional_features=("exact",),
    )
    rebuilt = SemanticRequirement.from_mapping(requirement.to_dict())
    assert rebuilt == requirement
    graph = RequirementGraph("meridian.structured.query", (requirement,))
    assert graph.fingerprint.startswith("sha256:")
    with pytest.raises(ValueError, match="unique"):
        RequirementGraph("x", (requirement, requirement))
    with pytest.raises(TypeError, match="array"):
        SemanticRequirement.from_mapping(
            {
                "semanticId": "x",
                "semanticVersion": "1.0.0",
                "operators": "eq",
            }
        )


def test_capabilities_round_trip_support_and_reasons() -> None:
    requirement = SemanticRequirement(
        "query.operator.eq",
        operators=("eq",),
        logical_types=("string",),
        required_limits={"pageSize": 10},
        transaction_guarantees=("single-binding",),
        optional_features=("exact",),
    )
    capabilities = QueryCapabilities(
        "test.adapter",
        ("scan",),
        ("query.operator.eq",),
        ("eq",),
        ("string",),
        ("strong",),
        ("single-binding",),
        ("exact",),
        {"pageSize": 10},
    )
    assert capabilities.supports(requirement, operation="scan") == (True, None)
    assert QueryCapabilities.from_mapping(capabilities.to_dict()) == capabilities
    assert capabilities.is_native("query.operator.eq")
    assert capabilities.supports(requirement, operation="search")[1] == (
        "query Operation is not advertised"
    )

    for changed, reason in (
        ({"consistency_classes": ("eventual",)}, "consistency"),
        ({"operators": ()}, "operator"),
        ({"logical_types": ("int64",)}, "logical type"),
        ({"guarantees": ()}, "guarantee"),
        ({"features": ()}, "optional feature"),
        ({"limits": {"pageSize": 9}}, "below"),
    ):
        values = {
            "adapter_id": "test.adapter",
            "operations": ("scan",),
            "native_semantics": ("query.operator.eq",),
            "operators": ("eq",),
            "logical_types": ("string",),
            "consistency_classes": ("strong",),
            "guarantees": ("single-binding",),
            "features": ("exact",),
            "limits": {"pageSize": 10},
            **changed,
        }
        candidate = QueryCapabilities(**values)  # type: ignore[arg-type]
        assert reason in (candidate.supports(requirement, operation="scan")[1] or "")


def test_native_planning_and_explain_are_deterministic(
    users: ResourceRef,
    registry_factory: Callable[..., RegistryView],
) -> None:
    validation = validate_compile(
        query(users).where({"age": {"$gte": 18}}).build(),
        registry_factory((users,)),
    )
    planner = QueryPlanner(_capabilities_for(validation))
    first = planner.plan(validation, registry_fingerprint=FINGERPRINT)
    second = planner.plan(validation, registry_fingerprint=FINGERPRINT)
    assert first.fingerprint == second.fingerprint
    assert set(first.assignments.values()) == {ImplementationMode.NATIVE}
    explain = first.explain().to_dict()
    assert explain["bindingId"] == "binding-primary"
    assert explain["planFingerprint"] == first.fingerprint


def test_exact_rewrite_requires_native_lower_semantics_and_base_capability(
    users: ResourceRef,
    registry_factory: Callable[..., RegistryView],
) -> None:
    validation = validate_compile(
        query(users).where({"name": {"$prefix": "A"}}).build(),
        registry_factory((users,)),
    )
    target = "query.operator.prefix"
    native = (
        *(
            item.semantic_id
            for item in validation.requirements.requirements
            if item.semantic_id != target
        ),
        "query.operator.range",
    )
    capabilities = _capabilities_for(
        validation,
        omit_operators=("prefix",),
        native_semantics=native,
    )
    planner = QueryPlanner(
        capabilities,
        rewrites=(RewriteRule(target, ("query.operator.range",), "prefix-range-v1"),),
    )
    plan = planner.plan(validation, registry_fingerprint=FINGERPRINT)
    assert plan.assignments[target] is ImplementationMode.EXACT_REWRITE

    insufficient = QueryCapabilities(
        capabilities.adapter_id,
        ("search",),
        capabilities.native_semantics,
        capabilities.operators,
        capabilities.logical_types,
        capabilities.consistency_classes,
        capabilities.guarantees,
        capabilities.features,
        capabilities.limits,
    )
    with pytest.raises(UnsupportedSemantic, match="exact bounded"):
        QueryPlanner(
            insufficient,
            rewrites=(RewriteRule(target, ("query.operator.range",), "prefix-range-v1"),),
        ).plan(validation, registry_fingerprint=FINGERPRINT)


def test_bounded_residual_requires_estimates_limits_and_proof(
    users: ResourceRef,
    registry_factory: Callable[..., RegistryView],
) -> None:
    operation = (
        query(users)
        .where({"name": {"$contains": "x"}})
        .option("estimatedRows", 10)
        .option("estimatedBytes", 100)
        .build()
    )
    validation = validate_compile(operation, registry_factory((users,)))
    target = "query.operator.contains"
    native = tuple(
        item.semantic_id
        for item in validation.requirements.requirements
        if item.semantic_id != target
    )
    capabilities = _capabilities_for(
        validation,
        omit_operators=("contains",),
        native_semantics=native,
    )
    seen: list[ResidualInput] = []

    def proof(value: ResidualInput) -> bool:
        seen.append(value)
        return value.estimated_rows == 10 and value.estimated_bytes == 100

    rule = ResidualRule(target, "unicode-contains-v1", "proof.contains.v1", 20, 200, proof)
    plan = QueryPlanner(capabilities, residuals=(rule,)).plan(
        validation, registry_fingerprint=FINGERPRINT
    )
    assert plan.assignments[target] is ImplementationMode.BOUNDED_RESIDUAL
    assert len(seen) == 1

    no_estimates = validate_compile(
        query(users).where({"name": {"$contains": "x"}}).build(),
        registry_factory((users,)),
    )
    with pytest.raises(UnsupportedSemantic):
        QueryPlanner(capabilities, residuals=(rule,)).plan(
            no_estimates, registry_fingerprint=FINGERPRINT
        )


def test_residual_failure_and_exceptions_never_broaden_scan(
    users: ResourceRef,
    registry_factory: Callable[..., RegistryView],
) -> None:
    validation = validate_compile(
        query(users)
        .where({"name": {"$contains": "x"}})
        .option("estimatedRows", 100)
        .option("estimatedBytes", 100)
        .build(),
        registry_factory((users,)),
    )
    target = "query.operator.contains"
    capabilities = _capabilities_for(
        validation,
        omit_operators=("contains",),
        native_semantics=tuple(
            item.semantic_id
            for item in validation.requirements.requirements
            if item.semantic_id != target
        ),
    )
    bounded = ResidualRule(target, "contains", "bounded", 10, 1_000, lambda _: True)
    with pytest.raises(UnsupportedSemantic):
        QueryPlanner(capabilities, residuals=(bounded,)).plan(
            validation, registry_fingerprint=FINGERPRINT
        )

    def raises(_: ResidualInput) -> bool:
        raise RuntimeError("proof bug")

    exploding = ResidualRule(target, "contains", "exploding", 1_000, 1_000, raises)
    with pytest.raises(UnsafeResidual, match="raised"):
        QueryPlanner(capabilities, residuals=(exploding,)).plan(
            validation, registry_fingerprint=FINGERPRINT
        )


def test_rejection_without_implementation_is_stable(
    users: ResourceRef,
    registry_factory: Callable[..., RegistryView],
) -> None:
    validation = validate_compile(
        query(users).where({"name": {"$contains": "x"}}).build(),
        registry_factory((users,)),
    )
    capabilities = _capabilities_for(
        validation,
        omit_operators=("contains",),
        native_semantics=("query.operation.scan",),
    )
    with pytest.raises(UnsupportedSemantic) as failure:
        QueryPlanner(capabilities).plan(validation, registry_fingerprint=FINGERPRINT)
    assert failure.value.code == "MERIDIAN_UNSUPPORTED_SEMANTIC"


def test_registry_dependent_and_empty_closure_planning(
    users: ResourceRef,
    registry_factory: Callable[..., RegistryView],
) -> None:
    start = RecordReference(ResourceReference("structured", "tests", "users"), "u1")
    operation = normalize_builder(
        query(users).traverse(start=start, all_neighbors=True),
        all_neighbors_resolver=lambda resource, depth: TraversalResolution(
            resource, (), FINGERPRINT, depth
        ),
    )
    validation = validate_compile(operation, registry_factory((users,)))
    minimal = QueryCapabilities(
        "test.adapter",
        ("traverse",),
        ("unused",),
        (),
        (),
        ("strong",),
    )
    plan = QueryPlanner(minimal).plan(validation, registry_fingerprint=FINGERPRINT)
    assert plan.empty_result is True
    assert set(plan.assignments.values()) == {ImplementationMode.EXACT_REWRITE}
    with pytest.raises(StaleRegistryPlan):
        QueryPlanner(minimal).plan(validation, registry_fingerprint=OTHER_FINGERPRINT)


class _Translator:
    def __init__(
        self,
        capabilities: QueryCapabilities,
        *,
        adapter_id: str | None = None,
        plan_fingerprint: str | None = None,
    ) -> None:
        self._capabilities = capabilities
        self._adapter_id = adapter_id
        self._plan_fingerprint = plan_fingerprint

    @property
    def capabilities(self) -> QueryCapabilities:
        return self._capabilities

    def compile(self, plan: object, context: TranslationContext) -> CompiledQuery:
        return CompiledQuery(
            self._adapter_id or self.capabilities.adapter_id,
            self._plan_fingerprint or context.plan_fingerprint,
            {"command": "parameterized"},
            {"p1": 1},
        )

    def normalize_result(
        self,
        compiled: CompiledQuery,
        raw_result: object,
    ) -> NormalizedQueryResult:
        return NormalizedQueryResult([raw_result])


def test_translation_contract_and_staleness(
    users: ResourceRef,
    registry_factory: Callable[..., RegistryView],
) -> None:
    validation = validate_compile(query(users).build(), registry_factory((users,)))
    capabilities = _capabilities_for(validation)
    plan = QueryPlanner(capabilities).plan(validation, registry_fingerprint=FINGERPRINT)
    translator = _Translator(capabilities)
    compiled = compile_query(plan, translator, scope_fingerprint=SCOPE_FINGERPRINT)
    assert compiled.parameters == {"p1": 1}
    assert translator.normalize_result(compiled, {"id": "1"}).operation_data() == {
        "items": [{"id": "1"}],
        "cursor": None,
    }

    changed = QueryCapabilities(
        "other.adapter",
        capabilities.operations,
        capabilities.native_semantics,
        capabilities.operators,
        capabilities.logical_types,
        capabilities.consistency_classes,
        capabilities.guarantees,
        capabilities.features,
        capabilities.limits,
    )
    with pytest.raises(StaleRegistryPlan, match="capabilities changed"):
        compile_query(plan, _Translator(changed), scope_fingerprint=SCOPE_FINGERPRINT)

    context = plan.translation_context(scope_fingerprint=SCOPE_FINGERPRINT)
    with pytest.raises(AssertionError, match="Adapter id"):
        assert_translation_contract(_Translator(capabilities, adapter_id="wrong"), plan, context)
    with pytest.raises(AssertionError, match="fingerprint"):
        assert_translation_contract(
            _Translator(capabilities, plan_fingerprint=OTHER_FINGERPRINT), plan, context
        )


def test_empty_plan_is_never_sent_to_adapter(
    users: ResourceRef,
    registry_factory: Callable[..., RegistryView],
) -> None:
    start = RecordReference(ResourceReference("structured", "tests", "users"), "u1")
    operation = normalize_builder(
        query(users).traverse(start=start, all_neighbors=True),
        all_neighbors_resolver=lambda resource, depth: TraversalResolution(
            resource, (), FINGERPRINT, depth
        ),
    )
    validation = validate_compile(operation, registry_factory((users,)))
    capabilities = QueryCapabilities(
        "test.adapter", ("traverse",), ("unused",), (), (), ("strong",)
    )
    plan = QueryPlanner(capabilities).plan(validation, registry_fingerprint=FINGERPRINT)
    with pytest.raises(ValueError, match="empty-result"):
        compile_query(plan, _Translator(capabilities), scope_fingerprint=SCOPE_FINGERPRINT)
