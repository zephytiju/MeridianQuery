# SPDX-License-Identifier: Apache-2.0
"""Signed opaque live-keyset cursor implementation."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import cast

from meridian_storage.semantics import JsonValue, canonical_json_bytes

from ..diagnostics import Diagnostic
from ..errors import InvalidCursor

CURSOR_FORMAT_VERSION = "meridian.query.cursor.v1"
_TOKEN_PREFIX = "mqc1"
_FINGERPRINT_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _fingerprint(value: str | None, name: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or _FINGERPRINT_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a sha256 fingerprint")
    return value


@dataclass(frozen=True, slots=True)
class CursorPayload:
    key_id: str
    plan_fingerprint: str
    schema_fingerprints: Mapping[str, str]
    registry_fingerprint: str | None
    scope_fingerprint: str
    sort_tuple: tuple[JsonValue, ...]
    page_size: int
    issued_at: int
    expires_at: int
    format_version: str = CURSOR_FORMAT_VERSION

    def __post_init__(self) -> None:
        if self.format_version != CURSOR_FORMAT_VERSION or not self.key_id:
            raise ValueError("invalid cursor format or key id")
        _fingerprint(self.plan_fingerprint, "cursor plan fingerprint")
        _fingerprint(self.registry_fingerprint, "cursor registry fingerprint", optional=True)
        _fingerprint(self.scope_fingerprint, "cursor scope fingerprint")
        schemas = dict(sorted(self.schema_fingerprints.items()))
        if any(
            _fingerprint(item, "cursor Schema fingerprint") is None for item in schemas.values()
        ):
            raise ValueError("invalid cursor Schema fingerprint")
        if not schemas:
            raise ValueError("cursor must bind at least one Schema fingerprint")
        if isinstance(self.page_size, bool) or not 1 <= self.page_size <= 500:
            raise ValueError("cursor page size must be between 1 and 500")
        if (
            isinstance(self.issued_at, bool)
            or isinstance(self.expires_at, bool)
            or self.issued_at < 0
            or self.expires_at <= self.issued_at
        ):
            raise ValueError("cursor issuance and expiry are invalid")
        if not self.sort_tuple:
            raise ValueError("cursor sort tuple must contain the unique keyset order")
        canonical_json_bytes(self.sort_tuple)
        object.__setattr__(self, "schema_fingerprints", MappingProxyType(schemas))
        object.__setattr__(self, "sort_tuple", tuple(self.sort_tuple))

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "formatVersion": self.format_version,
            "keyId": self.key_id,
            "planFingerprint": self.plan_fingerprint,
            "schemaFingerprints": dict(self.schema_fingerprints),
            "registryFingerprint": self.registry_fingerprint,
            "scopeFingerprint": self.scope_fingerprint,
            "sortTuple": list(self.sort_tuple),
            "pageSize": self.page_size,
            "issuedAt": self.issued_at,
            "expiresAt": self.expires_at,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> CursorPayload:
        required = {
            "formatVersion",
            "keyId",
            "planFingerprint",
            "schemaFingerprints",
            "registryFingerprint",
            "scopeFingerprint",
            "sortTuple",
            "pageSize",
            "issuedAt",
            "expiresAt",
        }
        if set(value) != required:
            raise ValueError("cursor payload contains unknown or missing fields")
        schemas = value["schemaFingerprints"]
        sort_tuple = value["sortTuple"]
        if not isinstance(schemas, Mapping):
            raise TypeError("cursor Schema fingerprints must be an object")
        if any(
            not isinstance(key, str) or not isinstance(item, str) for key, item in schemas.items()
        ):
            raise TypeError("cursor Schema fingerprints must map strings to strings")
        if not isinstance(sort_tuple, Sequence) or isinstance(sort_tuple, (str, bytes)):
            raise TypeError("cursor sort tuple must be an array")
        return cls(
            key_id=cast(str, value["keyId"]),
            plan_fingerprint=cast(str, value["planFingerprint"]),
            schema_fingerprints={str(key): str(item) for key, item in schemas.items()},
            registry_fingerprint=cast(str | None, value["registryFingerprint"]),
            scope_fingerprint=cast(str, value["scopeFingerprint"]),
            sort_tuple=tuple(cast(Sequence[JsonValue], sort_tuple)),
            page_size=cast(int, value["pageSize"]),
            issued_at=cast(int, value["issuedAt"]),
            expires_at=cast(int, value["expiresAt"]),
            format_version=cast(str, value["formatVersion"]),
        )


@dataclass(frozen=True, slots=True)
class CursorExpectations:
    plan_fingerprint: str
    schema_fingerprints: Mapping[str, str]
    scope_fingerprint: str
    page_size: int
    registry_fingerprint: str | None = None

    def __post_init__(self) -> None:
        _fingerprint(self.plan_fingerprint, "expected plan fingerprint")
        _fingerprint(self.registry_fingerprint, "expected registry fingerprint", optional=True)
        _fingerprint(self.scope_fingerprint, "expected scope fingerprint")
        schemas = dict(sorted(self.schema_fingerprints.items()))
        if not schemas or any(
            not isinstance(key, str) or _fingerprint(value, "expected Schema fingerprint") is None
            for key, value in schemas.items()
        ):
            raise ValueError("expected Schema fingerprints are invalid")
        if isinstance(self.page_size, bool) or not 1 <= self.page_size <= 500:
            raise ValueError("expected page size must be between 1 and 500")
        object.__setattr__(self, "schema_fingerprints", MappingProxyType(schemas))


class CursorSigner:
    """HMAC signer with explicit key rotation and an injectable UTC clock."""

    def __init__(
        self,
        keys: Mapping[str, bytes],
        *,
        active_key_id: str,
        ttl_seconds: int = 900,
        clock: Callable[[], datetime] | None = None,
        allowed_clock_skew_seconds: int = 30,
    ) -> None:
        if active_key_id not in keys:
            raise ValueError("active cursor key id is not configured")
        normalized = {key: bytes(value) for key, value in sorted(keys.items())}
        if any(not key or len(value) < 32 for key, value in normalized.items()):
            raise ValueError("cursor signing keys require ids and at least 256 bits")
        if isinstance(ttl_seconds, bool) or not 1 <= ttl_seconds <= 86_400:
            raise ValueError("cursor TTL must be between 1 and 86400 seconds")
        if (
            isinstance(allowed_clock_skew_seconds, bool)
            or not 0 <= allowed_clock_skew_seconds <= 300
        ):
            raise ValueError("cursor clock skew must be between 0 and 300 seconds")
        self._keys = MappingProxyType(normalized)
        self._active_key_id = active_key_id
        self._ttl_seconds = ttl_seconds
        self._clock = clock or (lambda: datetime.now(UTC))
        self._skew = allowed_clock_skew_seconds

    def issue(
        self,
        *,
        plan_fingerprint: str,
        schema_fingerprints: Mapping[str, str],
        registry_fingerprint: str | None,
        scope_fingerprint: str,
        sort_tuple: Sequence[JsonValue],
        page_size: int,
    ) -> str:
        now = self._now()
        payload = CursorPayload(
            key_id=self._active_key_id,
            plan_fingerprint=plan_fingerprint,
            schema_fingerprints=schema_fingerprints,
            registry_fingerprint=registry_fingerprint,
            scope_fingerprint=scope_fingerprint,
            sort_tuple=tuple(sort_tuple),
            page_size=page_size,
            issued_at=now,
            expires_at=now + self._ttl_seconds,
        )
        encoded = _encode(canonical_json_bytes(payload.to_dict()))
        signed = f"{_TOKEN_PREFIX}.{encoded}".encode()
        signature = hmac.new(self._keys[payload.key_id], signed, hashlib.sha256).digest()
        return f"{_TOKEN_PREFIX}.{encoded}.{_encode(signature)}"

    def verify(
        self,
        token: str,
        *,
        expected: CursorExpectations | None = None,
    ) -> CursorPayload:
        try:
            if not isinstance(token, str):
                raise TypeError("cursor token must be a string")
            prefix, encoded, signature = token.split(".")
            if prefix != _TOKEN_PREFIX:
                raise ValueError("unsupported cursor prefix")
            raw = _decode(encoded)
            mapping = json.loads(raw)
            if not isinstance(mapping, Mapping):
                raise TypeError("cursor payload must be an object")
            payload = CursorPayload.from_mapping(mapping)
            key = self._keys.get(payload.key_id)
            if key is None:
                raise ValueError("cursor key id is no longer accepted")
            actual = _decode(signature)
            expected_signature = hmac.new(
                key,
                f"{prefix}.{encoded}".encode(),
                hashlib.sha256,
            ).digest()
            if not hmac.compare_digest(actual, expected_signature):
                raise ValueError("cursor signature does not match")
            now = self._now()
            if payload.issued_at > now + self._skew:
                raise ValueError("cursor issuance is in the future")
            if payload.expires_at <= now - self._skew:
                raise ValueError("cursor has expired")
            if expected is not None:
                _verify_expectations(payload, expected)
            return payload
        except InvalidCursor:
            raise
        except (TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            diagnostic = Diagnostic(
                "MERIDIAN_QUERY_INVALID_CURSOR",
                "cursor is invalid, expired, stale, or was issued for another query scope",
                path="/page/cursor",
                requirement="cursor.valid",
            )
            raise InvalidCursor(diagnostic.message, diagnostics=(diagnostic,)) from exc

    def _now(self) -> int:
        value = self._clock()
        if value.tzinfo is None:
            raise ValueError("cursor clock must return a timezone-aware datetime")
        return int(value.astimezone(UTC).timestamp())


def _verify_expectations(payload: CursorPayload, expected: CursorExpectations) -> None:
    checks = {
        "plan fingerprint": payload.plan_fingerprint == expected.plan_fingerprint,
        "Schema fingerprints": dict(payload.schema_fingerprints)
        == dict(expected.schema_fingerprints),
        "registry fingerprint": payload.registry_fingerprint == expected.registry_fingerprint,
        "scope fingerprint": payload.scope_fingerprint == expected.scope_fingerprint,
        "page size": payload.page_size == expected.page_size,
    }
    failed = [name for name, valid in checks.items() if not valid]
    if failed:
        raise ValueError(f"cursor does not match expected {', '.join(failed)}")


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    if not value or any(
        character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        for character in value
    ):
        raise ValueError("cursor segment is not base64url")
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


__all__ = [
    "CURSOR_FORMAT_VERSION",
    "CursorExpectations",
    "CursorPayload",
    "CursorSigner",
]
