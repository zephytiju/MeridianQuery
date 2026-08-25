# SPDX-License-Identifier: Apache-2.0
"""Generative determinism and bounded-input properties."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from meridian_storage import ResourceRef
from meridian_storage.query import (
    BooleanExpression,
    CursorSigner,
    Literal,
    QueryOperation,
    expression_from_dict,
    field,
    parse_filter,
    query,
)

FINGERPRINT = "sha256:" + "f" * 64


@pytest.mark.property
@given(
    values=st.dictionaries(
        st.sampled_from(["active", "age", "id", "name"]),
        st.one_of(
            st.booleans(),
            st.integers(-1_000, 1_000),
            st.text(alphabet=st.characters(blacklist_characters="\x00"), max_size=30),
        ),
        min_size=1,
        max_size=4,
    )
)
@settings(max_examples=75)
def test_mapping_order_never_changes_filter_fingerprint(values: dict[str, object]) -> None:
    forward = parse_filter(values)
    reverse = parse_filter(dict(reversed(tuple(values.items()))))
    assert forward is not None and reverse is not None
    assert forward.to_dict() == reverse.to_dict()
    assert forward.fingerprint == reverse.fingerprint


@pytest.mark.property
@given(st.lists(st.integers(-10_000, 10_000), min_size=2, max_size=10, unique=True))
@settings(max_examples=50)
def test_boolean_permutation_has_one_canonical_form(values: list[int]) -> None:
    operands = tuple(field("age").ne(value) for value in values)
    forward = BooleanExpression("or", operands)
    reverse = BooleanExpression("or", tuple(reversed(operands)))
    assert forward.to_dict() == reverse.to_dict()
    assert forward.fingerprint == reverse.fingerprint


@pytest.mark.property
@given(
    st.one_of(
        st.none(),
        st.booleans(),
        st.integers(-(2**63), 2**63 - 1),
        st.floats(allow_nan=False, allow_infinity=False, width=64),
        st.text(max_size=100),
        st.binary(max_size=100),
    )
)
@settings(max_examples=100)
def test_literal_wire_round_trip_is_idempotent(value: object) -> None:
    original = Literal(value)
    rebuilt = expression_from_dict(original.to_dict())
    assert rebuilt.to_dict() == original.to_dict()


@pytest.mark.property
@given(
    size=st.integers(1, 500),
    estimated_rows=st.integers(0, 1_000_000),
    estimated_bytes=st.integers(0, 16 * 1024 * 1024),
)
@settings(max_examples=50)
def test_query_operation_round_trip_preserves_canonical_bytes(
    size: int,
    estimated_rows: int,
    estimated_bytes: int,
) -> None:
    resource = ResourceRef("structured", "property", "records")
    operation = (
        query(resource)
        .page(size=size)
        .option("estimatedRows", estimated_rows)
        .option("estimatedBytes", estimated_bytes)
        .build()
    )
    rebuilt = QueryOperation.from_mapping(operation.to_dict())
    assert rebuilt.canonical_bytes == operation.canonical_bytes
    assert rebuilt.fingerprint == operation.fingerprint


@pytest.mark.property
@given(st.lists(st.one_of(st.integers(), st.text(max_size=20)), min_size=1, max_size=5))
@settings(max_examples=40)
def test_cursor_sign_verify_preserves_sort_tuple(sort_tuple: list[object]) -> None:
    clock = datetime(2026, 8, 25, tzinfo=UTC)
    signer = CursorSigner({"key": b"k" * 32}, active_key_id="key", clock=lambda: clock)
    token = signer.issue(
        plan_fingerprint=FINGERPRINT,
        schema_fingerprints={"structured:property.records": FINGERPRINT},
        registry_fingerprint=None,
        scope_fingerprint=FINGERPRINT,
        sort_tuple=sort_tuple,  # type: ignore[arg-type]
        page_size=50,
    )
    assert list(signer.verify(token).sort_tuple) == sort_tuple
