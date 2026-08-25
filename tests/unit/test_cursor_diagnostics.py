# SPDX-License-Identifier: Apache-2.0
"""Opaque cursor, diagnostic, and redaction tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from jsonschema import Draft202012Validator

from meridian_storage.query import (
    CursorExpectations,
    CursorPayload,
    CursorSigner,
    Diagnostic,
    ExplainRecord,
    InvalidCursor,
    QueryValidationError,
    Severity,
    query_cursor_contract,
    redact,
)

PLAN = "sha256:" + "a" * 64
SCHEMA = "sha256:" + "b" * 64
REGISTRY = "sha256:" + "c" * 64
SCOPE = "sha256:" + "d" * 64


def _expectations(**changes: object) -> CursorExpectations:
    values = {
        "plan_fingerprint": PLAN,
        "schema_fingerprints": {"structured:tests.users": SCHEMA},
        "registry_fingerprint": REGISTRY,
        "scope_fingerprint": SCOPE,
        "page_size": 50,
        **changes,
    }
    return CursorExpectations(**values)  # type: ignore[arg-type]


def test_cursor_issue_verify_and_json_contract() -> None:
    now = datetime(2026, 8, 25, 12, tzinfo=UTC)
    signer = CursorSigner({"key-1": b"a" * 32}, active_key_id="key-1", clock=lambda: now)
    token = signer.issue(
        plan_fingerprint=PLAN,
        schema_fingerprints={"structured:tests.users": SCHEMA},
        registry_fingerprint=REGISTRY,
        scope_fingerprint=SCOPE,
        sort_tuple=("Ada", 42),
        page_size=50,
    )
    assert token.startswith("mqc1.")
    payload = signer.verify(token, expected=_expectations())
    assert payload.sort_tuple == ("Ada", 42)
    assert payload.issued_at == int(now.timestamp())
    assert CursorPayload.from_mapping(payload.to_dict()) == payload
    assert list(Draft202012Validator(query_cursor_contract()).iter_errors(payload.to_dict())) == []


@pytest.mark.parametrize(
    "mutation",
    [
        lambda token: token[:-1] + ("A" if token[-1] != "A" else "B"),
        lambda token: token.replace("mqc1", "mqc2", 1),
        lambda token: token + ".extra",
        lambda token: "mqc1.!invalid.signature",
    ],
)
def test_cursor_tamper_and_malformed_tokens_are_stably_rejected(mutation: object) -> None:
    now = datetime(2026, 8, 25, 12, tzinfo=UTC)
    signer = CursorSigner({"key": b"k" * 32}, active_key_id="key", clock=lambda: now)
    token = signer.issue(
        plan_fingerprint=PLAN,
        schema_fingerprints={"r": SCHEMA},
        registry_fingerprint=None,
        scope_fingerprint=SCOPE,
        sort_tuple=(1,),
        page_size=10,
    )
    with pytest.raises(InvalidCursor) as failure:
        signer.verify(mutation(token))  # type: ignore[operator]
    assert failure.value.code == "MERIDIAN_QUERY_INVALID_CURSOR"
    assert failure.value.diagnostics[0].path == "/page/cursor"


def test_cursor_expiry_future_issuance_and_key_rotation() -> None:
    clock = [datetime(2026, 8, 25, 12, tzinfo=UTC)]
    old = CursorSigner(
        {"old": b"o" * 32},
        active_key_id="old",
        ttl_seconds=60,
        allowed_clock_skew_seconds=0,
        clock=lambda: clock[0],
    )
    token = old.issue(
        plan_fingerprint=PLAN,
        schema_fingerprints={"r": SCHEMA},
        registry_fingerprint=None,
        scope_fingerprint=SCOPE,
        sort_tuple=(1,),
        page_size=10,
    )
    rotated = CursorSigner(
        {"new": b"n" * 32, "old": b"o" * 32},
        active_key_id="new",
        ttl_seconds=60,
        allowed_clock_skew_seconds=0,
        clock=lambda: clock[0],
    )
    assert rotated.verify(token).key_id == "old"
    clock[0] += timedelta(seconds=60)
    with pytest.raises(InvalidCursor):
        rotated.verify(token)

    clock[0] -= timedelta(seconds=120)
    with pytest.raises(InvalidCursor):
        rotated.verify(token)


@pytest.mark.parametrize(
    "changes",
    [
        {"plan_fingerprint": "sha256:" + "e" * 64},
        {"schema_fingerprints": {"r": "sha256:" + "e" * 64}},
        {"registry_fingerprint": None},
        {"scope_fingerprint": "sha256:" + "e" * 64},
        {"page_size": 49},
    ],
)
def test_cursor_expectation_mismatches_are_rejected(changes: dict[str, object]) -> None:
    now = datetime(2026, 8, 25, 12, tzinfo=UTC)
    signer = CursorSigner({"key": b"k" * 32}, active_key_id="key", clock=lambda: now)
    token = signer.issue(
        plan_fingerprint=PLAN,
        schema_fingerprints={"structured:tests.users": SCHEMA},
        registry_fingerprint=REGISTRY,
        scope_fingerprint=SCOPE,
        sort_tuple=(1,),
        page_size=50,
    )
    with pytest.raises(InvalidCursor):
        signer.verify(token, expected=_expectations(**changes))


@pytest.mark.parametrize(
    "factory",
    [
        lambda: CursorSigner({}, active_key_id="missing"),
        lambda: CursorSigner({"key": b"short"}, active_key_id="key"),
        lambda: CursorSigner({"key": b"k" * 32}, active_key_id="key", ttl_seconds=0),
        lambda: CursorSigner(
            {"key": b"k" * 32}, active_key_id="key", allowed_clock_skew_seconds=301
        ),
        lambda: _expectations(plan_fingerprint="sha256:not-hex"),
        lambda: _expectations(schema_fingerprints={}),
        lambda: _expectations(page_size=0),
    ],
)
def test_invalid_cursor_configuration_is_rejected(factory: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        factory()  # type: ignore[operator]


def test_cursor_verify_rejects_non_string_and_unknown_key() -> None:
    now = datetime(2026, 8, 25, 12, tzinfo=UTC)
    old = CursorSigner({"old": b"o" * 32}, active_key_id="old", clock=lambda: now)
    token = old.issue(
        plan_fingerprint=PLAN,
        schema_fingerprints={"r": SCHEMA},
        registry_fingerprint=None,
        scope_fingerprint=SCOPE,
        sort_tuple=(1,),
        page_size=1,
    )
    current = CursorSigner({"new": b"n" * 32}, active_key_id="new", clock=lambda: now)
    with pytest.raises(InvalidCursor):
        current.verify(token)
    with pytest.raises(InvalidCursor):
        current.verify(None)  # type: ignore[arg-type]


def test_diagnostics_are_sorted_deduplicated_and_serializable() -> None:
    first = Diagnostic(
        "MERIDIAN_TEST_B",
        "second",
        path="/b",
        logical_references=("z", "a", "z"),
    )
    second = Diagnostic(
        "MERIDIAN_TEST_A",
        "first",
        severity=Severity.WARNING,
        path="/a",
        requirement="test",
        hint="fix it",
    )
    failure = QueryValidationError("MERIDIAN_TEST", "failed", diagnostics=(first, second, first))
    payload = failure.to_dict()
    assert len(failure.diagnostics) == 2
    assert payload["diagnostics"][0]["severity"] == "ERROR"
    assert first.logical_references == ("a", "z")
    assert second.to_dict()["hint"] == "fix it"


def test_explain_redacts_sensitive_physical_details() -> None:
    value = {
        "endpoint": "https://database.invalid",
        "nested": {"password": "nope", "logical": [1, {"token": "secret"}]},
    }
    assert redact(value) == {
        "endpoint": "<redacted>",
        "nested": {"password": "<redacted>", "logical": [1, {"token": "<redacted>"}]},
    }
    explain = ExplainRecord(
        PLAN,
        "binding",
        {"requirement": "NATIVE"},
        {"pageSize": 50},
        details=value,
    )
    payload = explain.to_dict()
    assert payload["details"]["endpoint"] == "<redacted>"


@pytest.mark.parametrize(
    "factory",
    [
        lambda: Diagnostic("INVALID", "message"),
        lambda: Diagnostic("MERIDIAN_TEST", ""),
        lambda: ExplainRecord("bad", "binding", {}, {}),
    ],
)
def test_invalid_diagnostics_are_rejected(factory: object) -> None:
    with pytest.raises(ValueError):
        factory()  # type: ignore[operator]
