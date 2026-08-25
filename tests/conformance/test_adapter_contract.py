# SPDX-License-Identifier: Apache-2.0
"""Engine-independent Adapter translation conformance surface."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from jsonschema import Draft202012Validator

from meridian_storage.query import (
    CompiledQuery,
    NormalizedQueryResult,
    QueryCapabilities,
    QueryTranslator,
    SafetyBudget,
    SemanticRequirement,
    TranslationContext,
    enforce_result_budget,
    query_capability_contract,
)

FINGERPRINT = "sha256:" + "a" * 64


def _capabilities(**changes: object) -> QueryCapabilities:
    values = {
        "adapter_id": "test.adapter",
        "operations": ("scan",),
        "native_semantics": ("*",),
        "operators": ("eq",),
        "logical_types": ("string",),
        "consistency_classes": ("strong",),
        "guarantees": ("single-binding",),
        "features": (),
        "limits": {"pageSize": 50},
        **changes,
    }
    return QueryCapabilities(**values)  # type: ignore[arg-type]


@pytest.mark.conformance
def test_capability_descriptor_matches_language_neutral_schema() -> None:
    capabilities = _capabilities()
    errors = list(
        Draft202012Validator(query_capability_contract()).iter_errors(capabilities.to_dict())
    )
    assert errors == []
    assert capabilities.fingerprint.startswith("sha256:")
    assert capabilities.is_native("any.semantic")


@pytest.mark.conformance
@pytest.mark.parametrize(
    "factory",
    [
        lambda: _capabilities(adapter_id=""),
        lambda: _capabilities(format_version="wrong"),
        lambda: _capabilities(contract_version="2.0.0"),
        lambda: _capabilities(operations=()),
        lambda: _capabilities(operations=("scan", "scan")),
        lambda: _capabilities(operators=("eq", 1)),
        lambda: _capabilities(consistency_classes=("linearizable",)),
        lambda: _capabilities(limits={"pageSize": True}),
        lambda: QueryCapabilities.from_mapping({}),
        lambda: QueryCapabilities.from_mapping(
            {
                **_capabilities().to_dict(),
                "operations": "scan",
            }
        ),
    ],
)
def test_invalid_capability_descriptors_are_rejected(factory: Callable[[], object]) -> None:
    with pytest.raises((TypeError, ValueError)):
        factory()


@pytest.mark.conformance
def test_supports_base_does_not_allow_semantic_bypass() -> None:
    capability = _capabilities(operators=(), features=())
    requirement = SemanticRequirement(
        "query.operator.eq",
        operators=("eq",),
        logical_types=("string",),
        required_limits={"pageSize": 50},
        transaction_guarantees=("single-binding",),
        optional_features=("semantic-feature",),
    )
    assert capability.supports_base(requirement, operation="scan") == (True, None)
    assert capability.supports(requirement, operation="scan") == (
        False,
        "required operator is not advertised",
    )


@pytest.mark.conformance
def test_translation_values_are_opaque_and_normalized() -> None:
    context = TranslationContext(
        "binding",
        FINGERPRINT,
        FINGERPRINT,
        {"structured:tests.users": FINGERPRINT},
        FINGERPRINT,
        1_000,
    )
    compiled = CompiledQuery(
        "test.adapter",
        context.plan_fingerprint,
        {"dialectCommand": "opaque"},
        {"p1": "value"},
        "search",
    )
    result = NormalizedQueryResult(
        [{"id": "1"}],
        "opaque-cursor",
        {"adapter": "test.adapter"},
    )
    assert compiled.command == {"dialectCommand": "opaque"}
    assert result.operation_data() == {
        "items": [{"id": "1"}],
        "cursor": "opaque-cursor",
    }
    assert context.schema_fingerprints["structured:tests.users"] == FINGERPRINT
    assert enforce_result_budget(result, SafetyBudget()) is result


@pytest.mark.conformance
def test_normalized_result_budget_is_enforced() -> None:
    rows = NormalizedQueryResult([{"id": "1"}, {"id": "2"}])
    with pytest.raises(Exception, match="more values"):
        enforce_result_budget(rows, SafetyBudget(max_result_values=1))
    with pytest.raises(Exception, match="byte budget"):
        enforce_result_budget(rows, SafetyBudget(max_normalized_bytes=1))


@pytest.mark.conformance
@pytest.mark.parametrize(
    "factory",
    [
        lambda: TranslationContext(
            "", FINGERPRINT, FINGERPRINT, {"r": FINGERPRINT}, FINGERPRINT, 1
        ),
        lambda: TranslationContext("b", "bad", FINGERPRINT, {"r": FINGERPRINT}, FINGERPRINT, 1),
        lambda: TranslationContext("b", FINGERPRINT, FINGERPRINT, {"r": "bad"}, FINGERPRINT, 1),
        lambda: TranslationContext(
            "b", FINGERPRINT, FINGERPRINT, {"r": FINGERPRINT}, FINGERPRINT, 0
        ),
        lambda: CompiledQuery("", FINGERPRINT, {}),
        lambda: CompiledQuery("adapter", "bad", {}),
        lambda: CompiledQuery("adapter", FINGERPRINT, {}, expected_result_shape="unknown"),
        lambda: NormalizedQueryResult([], ""),
    ],
)
def test_invalid_translation_values_are_rejected(factory: Callable[[], object]) -> None:
    with pytest.raises((TypeError, ValueError)):
        factory()


@pytest.mark.conformance
def test_protocol_is_runtime_checkable() -> None:
    class CompleteTranslator:
        capabilities = _capabilities()

        def compile(self, plan: object, context: TranslationContext) -> CompiledQuery:
            return CompiledQuery("test.adapter", context.plan_fingerprint, {})

        def normalize_result(
            self, compiled: CompiledQuery, raw_result: object
        ) -> NormalizedQueryResult:
            return NormalizedQueryResult([])

    assert isinstance(CompleteTranslator(), QueryTranslator)
    assert not isinstance(object(), QueryTranslator)
