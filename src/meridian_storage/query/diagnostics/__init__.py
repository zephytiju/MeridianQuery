# SPDX-License-Identifier: Apache-2.0
"""Credential-free diagnostics and explain records."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import cast


class Severity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


@dataclass(frozen=True, slots=True)
class Diagnostic:
    code: str
    message: str
    severity: Severity = Severity.ERROR
    path: str = ""
    requirement: str | None = None
    logical_references: tuple[str, ...] = ()
    hint: str | None = None

    def __post_init__(self) -> None:
        if not self.code.startswith("MERIDIAN_"):
            raise ValueError("diagnostic code must be a stable MERIDIAN_* token")
        if not self.message:
            raise ValueError("diagnostic message cannot be empty")
        object.__setattr__(self, "severity", Severity(self.severity))
        object.__setattr__(
            self,
            "logical_references",
            tuple(sorted(set(self.logical_references))),
        )

    @property
    def sort_key(self) -> tuple[str, str, str, str, tuple[str, ...]]:
        return (
            self.severity.value,
            self.code,
            self.path,
            self.requirement or "",
            self.logical_references,
        )

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "code": self.code,
            "severity": self.severity.value,
            "message": self.message,
            "path": self.path,
            "logicalReferences": list(self.logical_references),
        }
        if self.requirement is not None:
            result["requirement"] = self.requirement
        if self.hint is not None:
            result["hint"] = self.hint
        return result


_SENSITIVE_TOKENS = (
    "credential",
    "password",
    "secret",
    "token",
    "authorization",
    "endpoint",
    "statement",
    "native",
    "sql",
    "dsl",
)


def redact(value: object) -> object:
    """Recursively redact sensitive fields without echoing their values."""

    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key in sorted(value):
            item = value[key]
            if any(token in str(key).casefold() for token in _SENSITIVE_TOKENS):
                result[str(key)] = "<redacted>"
            else:
                result[str(key)] = redact(item)
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [redact(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class ExplainRecord:
    plan_fingerprint: str
    binding_id: str
    assignments: Mapping[str, str]
    limits: Mapping[str, int]
    diagnostics: tuple[Diagnostic, ...] = ()
    details: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.plan_fingerprint.startswith("sha256:"):
            raise ValueError("explain plan_fingerprint must be a sha256 fingerprint")
        object.__setattr__(
            self,
            "assignments",
            MappingProxyType(dict(sorted(self.assignments.items()))),
        )
        object.__setattr__(self, "limits", MappingProxyType(dict(sorted(self.limits.items()))))
        object.__setattr__(
            self,
            "diagnostics",
            tuple(sorted(set(self.diagnostics), key=lambda item: item.sort_key)),
        )
        object.__setattr__(
            self,
            "details",
            MappingProxyType(cast(dict[str, object], redact(self.details))),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "planFingerprint": self.plan_fingerprint,
            "bindingId": self.binding_id,
            "assignments": dict(self.assignments),
            "limits": dict(self.limits),
            "diagnostics": [item.to_dict() for item in self.diagnostics],
            "details": dict(self.details),
        }


__all__ = ["Diagnostic", "ExplainRecord", "Severity", "redact"]
