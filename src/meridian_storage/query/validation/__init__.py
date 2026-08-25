# SPDX-License-Identifier: Apache-2.0
"""Compile-time and startup validation using one deterministic rule set."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from types import MappingProxyType

from meridian_storage.registry.resources import CapabilityRequirement
from meridian_storage.semantics import (
    FieldDefinition,
    FullTextProfile,
    GeospatialProfile,
    LogicalKind,
    RelationProfile,
    SchemaDocument,
    TimeSeriesProfile,
)
from meridian_storage.spi import CapabilityManifest, capability_violations

from meridian_storage import ResourceRef

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
    Point,
    Sort,
    TimestampRange,
    UnaryExpression,
    ValueExpression,
)
from ..diagnostics import Diagnostic, Severity
from ..errors import (
    CrossBindingOperation,
    ExcessiveBudget,
    FieldTypeMismatch,
    InvalidTraversal,
    NondeterministicOrdering,
    QueryCompatibilityError,
    QueryValidationError,
    StaleRegistryPlan,
    UnknownResource,
    UnknownSchema,
)
from ..requirements import FieldTypeResolver, RequirementGraph, infer_requirements
from ..wire import QueryOperation, SafetyBudget

_ORDERABLE = frozenset(
    {
        LogicalKind.INT8,
        LogicalKind.INT16,
        LogicalKind.INT32,
        LogicalKind.INT64,
        LogicalKind.DECIMAL,
        LogicalKind.FLOAT64,
        LogicalKind.STRING,
        LogicalKind.UUID,
        LogicalKind.UTC_TIMESTAMP,
        LogicalKind.DATE,
        LogicalKind.DURATION,
        LogicalKind.ENUM,
    }
)
_NUMERIC = frozenset(
    {
        LogicalKind.INT8,
        LogicalKind.INT16,
        LogicalKind.INT32,
        LogicalKind.INT64,
        LogicalKind.DECIMAL,
        LogicalKind.FLOAT64,
    }
)


@dataclass(frozen=True, slots=True)
class ResolvedResource:
    resource: ResourceRef
    schema: SchemaDocument
    binding_id: str
    required_scope: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        resource = ResourceRef.parse(self.resource)
        schema_ref = self.schema.ref.to_core()
        if (resource.catalog, resource.namespace) != (schema_ref.catalog, schema_ref.namespace):
            raise ValueError("resolved Resource and Schema must share Catalog and Namespace")
        if not self.binding_id:
            raise ValueError("resolved Resource requires an internal binding id")
        object.__setattr__(self, "resource", resource)
        object.__setattr__(self, "required_scope", tuple(sorted(set(self.required_scope))))


@dataclass(frozen=True, slots=True)
class RegistryView:
    resources: Mapping[ResourceRef, ResolvedResource]
    registry_fingerprint: str
    registry_revision: int
    budget_profiles: Mapping[str, SafetyBudget] = field(
        default_factory=lambda: {"default": SafetyBudget()}
    )

    def __post_init__(self) -> None:
        if not self.registry_fingerprint.startswith("sha256:"):
            raise ValueError("registry fingerprint must be a sha256 fingerprint")
        if isinstance(self.registry_revision, bool) or self.registry_revision < 1:
            raise ValueError("registry revision must be a positive integer")
        normalized = {
            ResourceRef.parse(key): value for key, value in sorted(self.resources.items())
        }
        if any(key != value.resource for key, value in normalized.items()):
            raise ValueError("registry Resource keys must match their resolved values")
        profiles = dict(sorted(self.budget_profiles.items()))
        if not profiles or any(
            not key or key != profile.workload_profile for key, profile in profiles.items()
        ):
            raise ValueError("registry budget profiles must be named consistently")
        object.__setattr__(self, "resources", MappingProxyType(normalized))
        object.__setattr__(self, "budget_profiles", MappingProxyType(profiles))


@dataclass(frozen=True, slots=True)
class CompileValidation:
    operation: QueryOperation
    requirements: RequirementGraph
    resolved_resources: tuple[ResolvedResource, ...]
    binding_id: str
    diagnostics: tuple[Diagnostic, ...] = ()


@dataclass(frozen=True, slots=True)
class StartupResourceRequirement:
    resource: ResourceRef
    binding_id: str
    requirements: tuple[CapabilityRequirement, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "resource", ResourceRef.parse(self.resource))
        if not self.binding_id or not self.requirements:
            raise ValueError("startup requirement needs a binding and capabilities")


@dataclass(frozen=True, slots=True)
class StartupValidation:
    ready: bool
    diagnostics: tuple[Diagnostic, ...]

    def require_ready(self) -> None:
        if not self.ready:
            raise QueryCompatibilityError(
                "MERIDIAN_QUERY_STARTUP_VALIDATION",
                "installed query capabilities do not satisfy startup requirements",
                diagnostics=self.diagnostics,
            )


def validate_compile(operation: QueryOperation, registry: RegistryView) -> CompileValidation:
    """Resolve Schemas and one Binding, then validate the exact dynamic plan."""

    resources = _resolve_resources(operation, registry)
    bindings = tuple(sorted({item.binding_id for item in resources}))
    if len(bindings) != 1:
        refs = tuple(item.resource.canonical for item in resources)
        diagnostic = Diagnostic(
            "MERIDIAN_QUERY_CROSS_BINDING",
            "one V1 Query Operation resolved through more than one Binding",
            path="/targets",
            requirement="query.single-binding",
            logical_references=refs,
        )
        raise CrossBindingOperation(
            diagnostic.message,
            diagnostics=(diagnostic,),
            operation_contract="meridian.operation.query.v1",
        )
    diagnostics: list[Diagnostic] = []
    normalized = _ensure_order(operation, resources, diagnostics)
    _validate_budget(normalized, registry, diagnostics)
    _validate_scope(normalized, resources, diagnostics)
    _validate_expressions(normalized, resources, diagnostics)
    _validate_profiles(normalized, resources, registry, diagnostics)
    errors = tuple(
        sorted(
            (item for item in diagnostics if item.severity is Severity.ERROR),
            key=lambda item: item.sort_key,
        )
    )
    if errors:
        _raise(errors)
    type_lookup = _field_type_lookup(normalized, resources)
    graph = infer_requirements(normalized, field_type_resolver=type_lookup)
    return CompileValidation(
        normalized,
        graph,
        resources,
        bindings[0],
        tuple(sorted(diagnostics, key=lambda item: item.sort_key)),
    )


def validate_startup(
    resources: Iterable[StartupResourceRequirement],
    capabilities: Mapping[str, CapabilityManifest],
) -> StartupValidation:
    """Compare installed authenticated Adapter probes with declared query requirements."""

    diagnostics: list[Diagnostic] = []
    for item in sorted(resources, key=lambda value: value.resource.canonical):
        manifest = capabilities.get(item.binding_id)
        if manifest is None:
            diagnostics.append(
                Diagnostic(
                    "MERIDIAN_QUERY_STARTUP_BINDING_MISSING",
                    "startup query Binding has no authenticated Capability manifest",
                    path=f"/resources/{item.resource.canonical}",
                    requirement="startup.capability-probe",
                    logical_references=(item.resource.canonical,),
                )
            )
            continue
        for violation in capability_violations(manifest, item.requirements):
            diagnostics.append(
                Diagnostic(
                    "MERIDIAN_QUERY_STARTUP_CAPABILITY",
                    violation.reason,
                    path=f"/resources/{item.resource.canonical}",
                    requirement=violation.requirement.operation_contract,
                    logical_references=(item.resource.canonical,),
                )
            )
    ordered = tuple(sorted(diagnostics, key=lambda item: item.sort_key))
    return StartupValidation(not ordered, ordered)


def _resolve_resources(
    operation: QueryOperation,
    registry: RegistryView,
) -> tuple[ResolvedResource, ...]:
    result: list[ResolvedResource] = []
    targets = {item.resource: item.schema for item in operation.targets}
    for resource in operation.resources:
        resolved = registry.resources.get(resource)
        if resolved is None:
            diagnostic = Diagnostic(
                "MERIDIAN_QUERY_UNKNOWN_RESOURCE",
                f"logical Resource {resource.canonical!r} is not registered",
                path="/targets",
                requirement="resource.exists",
                logical_references=(resource.canonical,),
            )
            raise UnknownResource(
                diagnostic.message,
                diagnostics=(diagnostic,),
                resource_ref=resource.canonical,
            )
        declared_schema = targets.get(resource)
        if declared_schema is not None and declared_schema != resolved.schema.ref.to_core():
            diagnostic = Diagnostic(
                "MERIDIAN_QUERY_UNKNOWN_SCHEMA",
                "query target Schema version does not match the resolved exact Schema",
                path="/targets/schema",
                requirement="schema.exact-resolution",
                logical_references=(str(declared_schema), resolved.schema.ref.canonical),
            )
            raise UnknownSchema(
                diagnostic.message,
                diagnostics=(diagnostic,),
                resource_ref=resource.canonical,
            )
        result.append(resolved)
    return tuple(sorted(result, key=lambda item: item.resource))


def _ensure_order(
    operation: QueryOperation,
    resources: tuple[ResolvedResource, ...],
    diagnostics: list[Diagnostic],
) -> QueryOperation:
    if operation.operation not in {"scan", "search"}:
        return operation
    primary_ref = operation.targets[0].resource
    primary = next(item for item in resources if item.resource == primary_ref)
    identity = primary.schema.identity
    if not identity:
        diagnostics.append(
            Diagnostic(
                "MERIDIAN_QUERY_NONDETERMINISTIC_ORDER",
                "live keyset pagination requires an immutable unique identity tuple",
                path="/order",
                requirement="pagination.unique-order",
                logical_references=(primary.resource.canonical,),
            )
        )
        return operation
    order = list(operation.order)
    existing = {
        (item.expression.resource, item.expression.name)
        for item in order
        if isinstance(item.expression, Field)
    }
    qualifier = primary.resource.canonical if len(operation.resources) > 1 else None
    appended = False
    for name in identity:
        key = (qualifier, name)
        unqualified = (None, name)
        if key not in existing and unqualified not in existing:
            order.append(Sort(Field(name, qualifier), "asc", "last"))
            appended = True
    if appended:
        diagnostics.append(
            Diagnostic(
                "MERIDIAN_QUERY_ORDER_TIEBREAKER_APPENDED",
                "logical Record identity was appended as the keyset tie-breaker",
                severity=Severity.INFO,
                path="/order",
                requirement="pagination.unique-order",
                logical_references=(primary.resource.canonical,),
            )
        )
    return replace(operation, order=tuple(order))


def _validate_budget(
    operation: QueryOperation,
    registry: RegistryView,
    diagnostics: list[Diagnostic],
) -> None:
    budget = operation.budget
    profile = registry.budget_profiles.get(budget.workload_profile)
    if profile is None:
        diagnostics.append(
            Diagnostic(
                "MERIDIAN_QUERY_EXCESSIVE_BUDGET",
                f"workload profile {budget.workload_profile!r} is not registered",
                path="/options/budget/workloadProfile",
                requirement="budget.reviewed-profile",
            )
        )
    else:
        for attribute in (
            "deadline_ms",
            "max_result_values",
            "max_normalized_bytes",
            "max_traversal_depth",
            "max_relation_resources",
            "max_visited_values",
            "max_returned_paths",
            "max_membership_names",
            "max_facets",
            "max_residual_rows",
            "max_residual_bytes",
        ):
            actual_limit = getattr(budget, attribute)
            reviewed_limit = getattr(profile, attribute)
            if actual_limit > reviewed_limit:
                diagnostics.append(
                    Diagnostic(
                        "MERIDIAN_QUERY_EXCESSIVE_BUDGET",
                        f"{attribute} exceeds reviewed profile {budget.workload_profile!r}",
                        path=f"/options/budget/{attribute}",
                        requirement="budget.reviewed-profile",
                    )
                )
    checks = {
        "page.size": (operation.page.size, budget.max_result_values),
        "membership": (_maximum_membership(operation), budget.max_membership_names),
        "facets": (_maximum_facets(operation), budget.max_facets),
    }
    if operation.traversal is not None:
        checks.update(
            {
                "traversal.depth": (
                    operation.traversal.max_depth,
                    budget.max_traversal_depth,
                ),
                "traversal.relations": (
                    len(operation.traversal.relation_collections),
                    budget.max_relation_resources,
                ),
            }
        )
    for name, (actual, maximum) in sorted(checks.items()):
        if actual > maximum:
            diagnostics.append(
                Diagnostic(
                    "MERIDIAN_QUERY_EXCESSIVE_BUDGET",
                    f"{name} value {actual} exceeds the allowed budget {maximum}",
                    path=f"/options/budget/{name}",
                    requirement=f"budget.{name}",
                )
            )


def _validate_scope(
    operation: QueryOperation,
    resources: tuple[ResolvedResource, ...],
    diagnostics: list[Diagnostic],
) -> None:
    missing = sorted(
        {
            scope
            for resource in resources
            for scope in resource.required_scope
            if operation.options.get("scopeInjected") is not True
        }
    )
    if missing:
        diagnostics.append(
            Diagnostic(
                "MERIDIAN_QUERY_SCOPE_REQUIRED",
                "required deployment scope was not injected before planning",
                path="/filter",
                requirement="query.scope-injection",
                logical_references=tuple(missing),
            )
        )


def _validate_expressions(
    operation: QueryOperation,
    resources: tuple[ResolvedResource, ...],
    diagnostics: list[Diagnostic],
) -> None:
    for path, expression in _operation_expressions(operation):
        _validate_node(expression, path, operation, resources, diagnostics)


def _validate_node(
    expression: ValueExpression,
    path: str,
    operation: QueryOperation,
    resources: tuple[ResolvedResource, ...],
    diagnostics: list[Diagnostic],
) -> None:
    for referenced_field in expression.referenced_fields:
        _field_definition(referenced_field, path, operation, resources, diagnostics)
    if isinstance(expression, BinaryExpression):
        if expression.operator in {"eq", "ne", "lt", "lte", "gt", "gte"}:
            _require_compatible(
                expression.left,
                expression.right,
                path,
                operation,
                resources,
                diagnostics,
            )
        if expression.operator in {"lt", "lte", "gt", "gte"}:
            _require_kinds(expression.left, _ORDERABLE, path, operation, resources, diagnostics)
            _require_kinds(expression.right, _ORDERABLE, path, operation, resources, diagnostics)
        elif expression.operator in {"add", "subtract", "multiply", "divide", "modulo"}:
            _require_kinds(expression.left, _NUMERIC, path, operation, resources, diagnostics)
            _require_kinds(expression.right, _NUMERIC, path, operation, resources, diagnostics)
        elif expression.operator == "prefix":
            _require_kinds(
                expression.left,
                frozenset({LogicalKind.STRING}),
                path,
                operation,
                resources,
                diagnostics,
            )
        elif expression.operator == "contains":
            _require_kinds(
                expression.left,
                frozenset({LogicalKind.STRING, LogicalKind.JSON}),
                path,
                operation,
                resources,
                diagnostics,
            )
        _validate_node(expression.left, f"{path}/left", operation, resources, diagnostics)
        _validate_node(expression.right, f"{path}/right", operation, resources, diagnostics)
    elif isinstance(expression, BooleanExpression):
        for index, item in enumerate(expression.operands):
            _validate_node(item, f"{path}/operands/{index}", operation, resources, diagnostics)
    elif isinstance(expression, UnaryExpression):
        _validate_node(expression.operand, f"{path}/operand", operation, resources, diagnostics)
    elif isinstance(expression, (NullTest, MembershipExpression)):
        _validate_node(expression.operand, f"{path}/operand", operation, resources, diagnostics)
        if isinstance(expression, MembershipExpression):
            for index, item in enumerate(expression.values):
                _require_compatible(
                    expression.operand,
                    item,
                    f"{path}/values/{index}",
                    operation,
                    resources,
                    diagnostics,
                )
                _validate_node(item, f"{path}/values/{index}", operation, resources, diagnostics)
    elif isinstance(expression, TimestampRange):
        _require_kinds(
            expression.operand,
            frozenset({LogicalKind.UTC_TIMESTAMP}),
            path,
            operation,
            resources,
            diagnostics,
        )
        for boundary in (expression.start, expression.end):
            if boundary is not None:
                _require_kinds(
                    boundary,
                    frozenset({LogicalKind.UTC_TIMESTAMP}),
                    path,
                    operation,
                    resources,
                    diagnostics,
                )
    elif isinstance(expression, DocumentPath):
        _require_kinds(
            expression.document,
            frozenset({LogicalKind.JSON}),
            path,
            operation,
            resources,
            diagnostics,
        )
    elif isinstance(expression, FullTextMatch):
        for referenced_field in expression.fields:
            if referenced_field.name != "*":
                _require_kinds(
                    referenced_field,
                    frozenset({LogicalKind.STRING}),
                    path,
                    operation,
                    resources,
                    diagnostics,
                )
    elif isinstance(expression, (Distance, DistanceWithin)):
        for item in (expression.left, expression.right):
            if isinstance(item, Field):
                _require_kinds(
                    item,
                    frozenset({LogicalKind.WGS84_POINT}),
                    path,
                    operation,
                    resources,
                    diagnostics,
                )
    elif isinstance(expression, Aggregate) and expression.operand is not None:
        if expression.function in {"sum", "avg", "percentile"}:
            _require_kinds(
                expression.operand,
                _NUMERIC,
                path,
                operation,
                resources,
                diagnostics,
            )


def _validate_profiles(
    operation: QueryOperation,
    resources: tuple[ResolvedResource, ...],
    registry: RegistryView,
    diagnostics: list[Diagnostic],
) -> None:
    primary = next(item for item in resources if item.resource == operation.targets[0].resource)
    has_full_text = any(
        isinstance(node, FullTextMatch)
        for _, expression in _operation_expressions(operation)
        for node in _walk(expression)
    )
    if operation.operation == "search" and not has_full_text:
        diagnostics.append(
            Diagnostic(
                "MERIDIAN_QUERY_FIELD_TYPE_MISMATCH",
                "search Operation requires one full-text expression",
                path="/filter",
                requirement="operator.full-text",
                logical_references=(primary.schema.ref.canonical,),
            )
        )
    if operation.operation == "search" and not isinstance(primary.schema.profile, FullTextProfile):
        diagnostics.append(
            Diagnostic(
                "MERIDIAN_QUERY_FIELD_TYPE_MISMATCH",
                "search Operation requires a full-text Schema profile",
                path="/operation",
                requirement="profile.search",
                logical_references=(primary.schema.ref.canonical,),
            )
        )
    if isinstance(primary.schema.profile, FullTextProfile):
        profile = primary.schema.profile
        matches = (
            node
            for _, expression in _operation_expressions(operation)
            for node in _walk(expression)
            if isinstance(node, FullTextMatch)
        )
        for match in matches:
            requested_fields = {item.name for item in match.fields if item.name != "*"}
            mismatches = (
                requested_fields - set(profile.source_fields)
                or set(match.facets) - set(profile.facets)
                or set(match.highlights) - set(profile.highlights)
                or ({match.analyzer} if match.analyzer != profile.analyzer_profile else set())
                or ({match.ranking} if match.ranking != profile.ranking else set())
            )
            if mismatches:
                diagnostics.append(
                    Diagnostic(
                        "MERIDIAN_QUERY_FIELD_TYPE_MISMATCH",
                        "full-text request exceeds its Schema search profile",
                        path="/filter",
                        requirement="profile.search",
                        logical_references=tuple(sorted(mismatches)),
                    )
                )
    distance_nodes = tuple(
        node
        for _, expression in _operation_expressions(operation)
        for node in _walk(expression)
        if isinstance(node, (Distance, DistanceWithin))
    )
    for node in distance_nodes:
        referenced_resources = {
            resolved.resource: resolved
            for referenced in node.referenced_fields
            if (resolved := _resolved_field_resource(referenced, operation, resources)) is not None
        }
        if not referenced_resources:
            referenced_resources = {primary.resource: primary}
        required_operation = "distance-within" if isinstance(node, DistanceWithin) else "distance"
        for resolved in referenced_resources.values():
            geo_profile = resolved.schema.profile
            if not isinstance(geo_profile, GeospatialProfile) or required_operation not in set(
                geo_profile.operations
            ):
                diagnostics.append(
                    Diagnostic(
                        "MERIDIAN_QUERY_FIELD_TYPE_MISMATCH",
                        "distance semantics require a compatible geospatial Schema profile",
                        path="/filter",
                        requirement="profile.geospatial",
                        logical_references=(resolved.schema.ref.canonical,),
                    )
                )
    has_time_range = any(
        isinstance(node, TimestampRange)
        for _, expression in _operation_expressions(operation)
        for node in _walk(expression)
    )
    if (
        has_time_range
        and primary.schema.semantic_kind.value == "time-series"
        and not isinstance(primary.schema.profile, TimeSeriesProfile)
    ):
        diagnostics.append(
            Diagnostic(
                "MERIDIAN_QUERY_FIELD_TYPE_MISMATCH",
                "time-series Schema is missing its TimeSeriesProfile",
                path="/filter",
                requirement="profile.time-series",
                logical_references=(primary.schema.ref.canonical,),
            )
        )
    if has_time_range and isinstance(primary.schema.profile, TimeSeriesProfile):
        timestamp_fields = {
            referenced.name
            for _, expression in _operation_expressions(operation)
            for node in _walk(expression)
            if isinstance(node, TimestampRange)
            for referenced in node.operand.referenced_fields
        }
        if timestamp_fields != {primary.schema.profile.timestamp_field}:
            diagnostics.append(
                Diagnostic(
                    "MERIDIAN_QUERY_FIELD_TYPE_MISMATCH",
                    "timestamp range must use the TimeSeriesProfile timestamp field",
                    path="/filter",
                    requirement="profile.time-series.timestamp",
                    logical_references=tuple(sorted(timestamp_fields)),
                )
            )
    traversal = operation.traversal
    if traversal is None:
        return
    if traversal.start.collection_ref.to_core() != primary.resource:
        diagnostics.append(
            Diagnostic(
                "MERIDIAN_QUERY_INVALID_TRAVERSAL",
                "traversal start RecordRef does not belong to the target Collection",
                path="/traversal/start",
                requirement="traversal.start",
            )
        )
    if traversal.all_neighbors:
        if traversal.registry_fingerprint is None:
            diagnostics.append(
                Diagnostic(
                    "MERIDIAN_QUERY_INVALID_TRAVERSAL",
                    "all_neighbors must resolve to an explicit closed relation set",
                    path="/traversal/relationSelector",
                    requirement="traversal.registry-resolution",
                )
            )
        elif traversal.registry_fingerprint != registry.registry_fingerprint:
            diagnostic = Diagnostic(
                "MERIDIAN_QUERY_STALE_REGISTRY",
                "all_neighbors plan was derived from a stale registry fingerprint",
                path="/traversal/relationSelector/registryFingerprint",
                requirement="traversal.registry-fingerprint",
            )
            raise StaleRegistryPlan(diagnostic.message, diagnostics=(diagnostic,))
    for relation in traversal.relation_collections:
        resolved = registry.resources.get(relation)
        if resolved is None or not isinstance(resolved.schema.profile, RelationProfile):
            diagnostics.append(
                Diagnostic(
                    "MERIDIAN_QUERY_INVALID_TRAVERSAL",
                    "traversal selector contains a non-relation Collection",
                    path="/traversal/relationSelector/relationCollections",
                    requirement="traversal.relation-profile",
                    logical_references=(relation.canonical,),
                )
            )


def _field_definition(
    referenced_field: Field,
    path: str,
    operation: QueryOperation,
    resources: tuple[ResolvedResource, ...],
    diagnostics: list[Diagnostic],
) -> FieldDefinition | None:
    if referenced_field.name == "*":
        return None
    candidates = resources
    if referenced_field.resource is not None:
        try:
            target = ResourceRef.parse(referenced_field.resource, catalog=operation.catalog)
        except (TypeError, ValueError):
            target = next(
                (
                    item.resource
                    for item in resources
                    if item.resource.logical_name == referenced_field.resource
                ),
                None,
            )
        candidates = tuple(item for item in resources if item.resource == target)
    elif len(operation.resources) > 1:
        diagnostics.append(
            Diagnostic(
                "MERIDIAN_QUERY_FIELD_TYPE_MISMATCH",
                "field references must be Resource-qualified when multiple Resources participate",
                path=path,
                requirement="field.qualified",
                logical_references=(referenced_field.name,),
            )
        )
        return None
    matches = [item.schema.field_map.get(referenced_field.name) for item in candidates]
    definitions = [item for item in matches if item is not None]
    if len(definitions) != 1:
        diagnostics.append(
            Diagnostic(
                "MERIDIAN_QUERY_FIELD_TYPE_MISMATCH",
                f"field {referenced_field.name!r} does not resolve to exactly one Schema field",
                path=path,
                requirement="field.exists",
                logical_references=(referenced_field.name,),
            )
        )
        return None
    return definitions[0]


def _resolved_field_resource(
    referenced_field: Field,
    operation: QueryOperation,
    resources: tuple[ResolvedResource, ...],
) -> ResolvedResource | None:
    if referenced_field.resource is None:
        return resources[0] if len(resources) == 1 else None
    try:
        target = ResourceRef.parse(referenced_field.resource, catalog=operation.catalog)
    except (TypeError, ValueError):
        target = next(
            (
                item.resource
                for item in resources
                if item.resource.logical_name == referenced_field.resource
            ),
            None,
        )
    return next((item for item in resources if item.resource == target), None)


def _require_kinds(
    expression: ValueExpression,
    allowed: frozenset[LogicalKind],
    path: str,
    operation: QueryOperation,
    resources: tuple[ResolvedResource, ...],
    diagnostics: list[Diagnostic],
) -> None:
    kinds = _expression_kinds(expression, operation, resources, diagnostics, path)
    if kinds and not kinds <= allowed:
        diagnostics.append(
            Diagnostic(
                "MERIDIAN_QUERY_FIELD_TYPE_MISMATCH",
                "expression has an incompatible logical type",
                path=path,
                requirement="operator.logical-type",
                logical_references=tuple(sorted(item.value for item in kinds)),
            )
        )
    for referenced_field in expression.referenced_fields:
        definition = _field_definition(referenced_field, path, operation, resources, diagnostics)
        if definition is not None and definition.logical_type.kind not in allowed:
            diagnostics.append(
                Diagnostic(
                    "MERIDIAN_QUERY_FIELD_TYPE_MISMATCH",
                    f"field {referenced_field.name!r} has incompatible logical type",
                    path=path,
                    requirement="operator.logical-type",
                    logical_references=(referenced_field.name,),
                )
            )


def _require_compatible(
    left: ValueExpression,
    right: ValueExpression,
    path: str,
    operation: QueryOperation,
    resources: tuple[ResolvedResource, ...],
    diagnostics: list[Diagnostic],
) -> None:
    left_kinds = _expression_kinds(left, operation, resources, diagnostics, path)
    right_kinds = _expression_kinds(right, operation, resources, diagnostics, path)
    if not left_kinds or not right_kinds:
        return
    compatible = bool(left_kinds & right_kinds) or (
        left_kinds <= _NUMERIC and right_kinds <= _NUMERIC
    )
    if not compatible:
        diagnostics.append(
            Diagnostic(
                "MERIDIAN_QUERY_FIELD_TYPE_MISMATCH",
                "comparison operands have incompatible logical types",
                path=path,
                requirement="operator.compatible-types",
                logical_references=tuple(sorted({item.value for item in left_kinds | right_kinds})),
            )
        )


def _expression_kinds(
    expression: ValueExpression,
    operation: QueryOperation,
    resources: tuple[ResolvedResource, ...],
    diagnostics: list[Diagnostic],
    path: str,
) -> frozenset[LogicalKind]:
    if isinstance(expression, Field):
        definition = _field_definition(expression, path, operation, resources, diagnostics)
        return frozenset() if definition is None else frozenset({definition.logical_type.kind})
    if isinstance(expression, (Literal, Parameter)):
        logical = expression.logical_type
        if isinstance(logical, str):
            kind_value: object = logical
        elif isinstance(logical, Mapping):
            kind_value = logical.get("kind")
        else:
            return frozenset()
        try:
            return frozenset({LogicalKind(str(kind_value))})
        except ValueError:
            return frozenset()
    if isinstance(expression, Point):
        return frozenset({LogicalKind.WGS84_POINT})
    if isinstance(expression, Distance):
        return frozenset({LogicalKind.FLOAT64})
    if isinstance(expression, DocumentPath):
        return frozenset({LogicalKind.JSON})
    if isinstance(expression, UnaryExpression) and expression.operator == "negate":
        return _expression_kinds(expression.operand, operation, resources, diagnostics, path)
    if isinstance(expression, BinaryExpression) and expression.operator in {
        "add",
        "subtract",
        "multiply",
        "divide",
        "modulo",
    }:
        return _expression_kinds(expression.left, operation, resources, diagnostics, path) | (
            _expression_kinds(expression.right, operation, resources, diagnostics, path)
        )
    return frozenset()


def _field_type_lookup(
    operation: QueryOperation,
    resources: tuple[ResolvedResource, ...],
) -> FieldTypeResolver:
    def resolve(referenced_field: Field) -> str | None:
        scratch: list[Diagnostic] = []
        definition = _field_definition(referenced_field, "", operation, resources, scratch)
        if definition is None:
            return None
        return definition.logical_type.kind.value

    return resolve


def _operation_expressions(operation: QueryOperation) -> tuple[tuple[str, ValueExpression], ...]:
    result: list[tuple[str, ValueExpression]] = []
    if operation.filter is not None:
        result.append(("/filter", operation.filter))
    result.extend(
        (f"/result/projection/{index}", item.expression)
        for index, item in enumerate(operation.result.projection)
    )
    result.extend((f"/joins/{index}/on", item.on) for index, item in enumerate(operation.joins))
    result.extend((f"/grouping/{index}", item) for index, item in enumerate(operation.grouping))
    result.extend(
        (f"/aggregates/{index}", item.aggregate) for index, item in enumerate(operation.aggregates)
    )
    result.extend(
        (f"/order/{index}", item.expression) for index, item in enumerate(operation.order)
    )
    if operation.traversal is not None:
        if operation.traversal.record_filter is not None:
            result.append(("/traversal/recordFilter", operation.traversal.record_filter))
        result.extend(
            (f"/traversal/relationPredicates/{key}", item)
            for key, item in operation.traversal.relation_predicates.items()
        )
    return tuple(result)


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
    elif isinstance(expression, FullTextMatch):
        result.extend(expression.fields)
    elif isinstance(expression, (Distance, DistanceWithin)):
        result.extend(_walk(expression.left))
        result.extend(_walk(expression.right))
    elif isinstance(expression, Aggregate) and expression.operand is not None:
        result.extend(_walk(expression.operand))
    return tuple(result)


def _maximum_membership(operation: QueryOperation) -> int:
    return max(
        (
            len(node.values)
            for _, expression in _operation_expressions(operation)
            for node in _walk(expression)
            if isinstance(node, MembershipExpression)
        ),
        default=0,
    )


def _maximum_facets(operation: QueryOperation) -> int:
    return max(
        (
            len(node.facets)
            for _, expression in _operation_expressions(operation)
            for node in _walk(expression)
            if isinstance(node, FullTextMatch)
        ),
        default=0,
    )


def _raise(diagnostics: Sequence[Diagnostic]) -> None:
    by_code = {item.code: item for item in diagnostics}
    selected = by_code.get("MERIDIAN_QUERY_EXCESSIVE_BUDGET")
    if selected is not None:
        raise ExcessiveBudget(selected.message, diagnostics=diagnostics)
    selected = by_code.get("MERIDIAN_QUERY_INVALID_TRAVERSAL")
    if selected is not None:
        raise InvalidTraversal(selected.message, diagnostics=diagnostics)
    selected = by_code.get("MERIDIAN_QUERY_NONDETERMINISTIC_ORDER")
    if selected is not None:
        raise NondeterministicOrdering(selected.message, diagnostics=diagnostics)
    selected = by_code.get("MERIDIAN_QUERY_FIELD_TYPE_MISMATCH")
    if selected is not None:
        raise FieldTypeMismatch(selected.message, diagnostics=diagnostics)
    raise QueryValidationError(
        diagnostics[0].code,
        diagnostics[0].message,
        diagnostics=diagnostics,
    )


__all__ = [
    "CompileValidation",
    "RegistryView",
    "ResolvedResource",
    "StartupResourceRequirement",
    "StartupValidation",
    "validate_compile",
    "validate_startup",
]
