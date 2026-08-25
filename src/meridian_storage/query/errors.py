# SPDX-License-Identifier: Apache-2.0
"""Stable, deterministic query failures."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, cast

from meridian_storage.errors import CompatibilityError, ValidationError

from .diagnostics import Diagnostic


class _QueryDetails:
    diagnostics: tuple[Diagnostic, ...]

    def _set_diagnostics(self, diagnostics: Iterable[Diagnostic]) -> None:
        self.diagnostics = tuple(sorted(set(diagnostics), key=lambda item: item.sort_key))

    def to_dict(self) -> dict[str, Any]:
        payload = cast(dict[str, Any], super().to_dict())  # type: ignore[misc]
        if self.diagnostics:
            payload["diagnostics"] = [item.to_dict() for item in self.diagnostics]
        return payload


class QueryValidationError(_QueryDetails, ValidationError):
    """A query is not valid against its logical contract."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        diagnostics: Iterable[Diagnostic] = (),
        **details: Any,
    ) -> None:
        super().__init__(code, message, **details)
        self._set_diagnostics(diagnostics)


class QueryCompatibilityError(_QueryDetails, CompatibilityError):
    """A valid query cannot be satisfied by the selected deployment."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        diagnostics: Iterable[Diagnostic] = (),
        **details: Any,
    ) -> None:
        super().__init__(code, message, **details)
        self._set_diagnostics(diagnostics)


class UnknownResource(QueryValidationError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__("MERIDIAN_QUERY_UNKNOWN_RESOURCE", message, **details)


class UnknownSchema(QueryValidationError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__("MERIDIAN_QUERY_UNKNOWN_SCHEMA", message, **details)


class FieldTypeMismatch(QueryValidationError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__("MERIDIAN_QUERY_FIELD_TYPE_MISMATCH", message, **details)


class CrossBindingOperation(QueryCompatibilityError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__("MERIDIAN_QUERY_CROSS_BINDING", message, **details)


class UnsupportedSemantic(QueryCompatibilityError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__("MERIDIAN_UNSUPPORTED_SEMANTIC", message, **details)


class UnsafeResidual(QueryCompatibilityError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__("MERIDIAN_QUERY_UNSAFE_RESIDUAL", message, **details)


class ExcessiveBudget(QueryValidationError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__("MERIDIAN_QUERY_EXCESSIVE_BUDGET", message, **details)


class NondeterministicOrdering(QueryValidationError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__("MERIDIAN_QUERY_NONDETERMINISTIC_ORDER", message, **details)


class InvalidTraversal(QueryValidationError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__("MERIDIAN_QUERY_INVALID_TRAVERSAL", message, **details)


class StaleRegistryPlan(QueryCompatibilityError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__("MERIDIAN_QUERY_STALE_REGISTRY", message, **details)


class InvalidCursor(QueryValidationError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__("MERIDIAN_QUERY_INVALID_CURSOR", message, **details)


__all__ = [
    "CrossBindingOperation",
    "ExcessiveBudget",
    "FieldTypeMismatch",
    "InvalidCursor",
    "InvalidTraversal",
    "NondeterministicOrdering",
    "QueryCompatibilityError",
    "QueryValidationError",
    "StaleRegistryPlan",
    "UnknownResource",
    "UnknownSchema",
    "UnsafeResidual",
    "UnsupportedSemantic",
]
