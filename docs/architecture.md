<!-- SPDX-License-Identifier: Apache-2.0 -->

# Architecture

## Boundary

Meridian Query is a reusable library behind registered Catalog Expressions. It owns no Catalog,
does not choose a Binding, Adapter, or Engine, and carries no physical identifiers. The V1 wire
format is `meridian.operation.query.v1`; one normalized Operation must resolve through exactly one
internal Binding.

The authoritative implementation baseline is Meridian HLD revision 56, Catalogs/Public Interfaces
revision 70, Query LLD revision 46, and Adapter LLD revision 24. The final program redesign removes
the future native Expression from V1 entirely. Namespaced extensions are logical-only, disabled by
default, explicitly allowlisted, and reject adapter/engine/native/SQL/DSL/endpoint/credential terms.

## Components

| Component | Responsibility |
|---|---|
| `ast` | Immutable typed value expressions and strict mapping-first filter parsing. |
| `builder` | Immutable fluent API producing Catalog Expressions or canonical Operations. |
| `wire` | Language-neutral Operation, targets, joins, result, page, budget, and traversal contracts. |
| `normalize` | Catalog Expression normalization, scope conjunction, Schema selection, and all-neighbor closure. |
| `requirements` | Complete semantic/operator/type/limit/consistency requirement derivation. |
| `validation` | Exact Resource/Schema resolution, one-Binding enforcement, type/profile/budget checks, startup probes. |
| `planner` | Internal native, exact-rewrite, bounded-residual, or rejected assignment. |
| `adapter` | Engine-independent capability and parameterized translation protocol. |
| `cursor` | Opaque signed live-keyset cursor issuance, rotation, expiry, and context binding. |
| `diagnostics` | Stable credential-free failures and redacted explain records. |

## Deterministic planning

Normalization captures logical defaults and resolves dynamic relation selectors to an explicit,
closed Collection tuple plus registry fingerprint. Validation resolves every Resource to one exact
Schema and rejects a plan before adapter compilation if any field, type, profile, scope, ordering,
budget, traversal, or Binding invariant fails. Requirement derivation walks nested AST nodes, so a
capability cannot hide inside boolean composition.

The planner selects one mode per requirement:

- `NATIVE`: the adapter advertises the exact semantic and every constraint.
- `EXACT_REWRITE`: a registered equivalence maps the semantic to advertised lower semantics.
- `BOUNDED_RESIDUAL`: a registered exact function proves row and byte bounds against the validated
  operation and reviewed budget.
- `REJECTED`: no exact bounded implementation exists.

No mode is consumer-selectable. Missing capability never falls back to a broader scan.

## Safety

Default limits are 30 seconds, 500 result values, 16 MiB normalized output, traversal depth 8, 32
relation Resources, 100,000 visited values, 10,000 returned paths, 10,000 membership values, 100
facets, 10,000 residual rows, and 16 MiB residual bytes. An Operation may only use limits at or below
a `RegistryView` workload profile. Higher named profiles must be registered by deployment policy.

Live pagination ends in an immutable unique Schema identity tuple. Validation appends missing
identity fields deterministically. A cursor binds every logical fingerprint and expires; snapshot
semantics are only available when an adapter advertises the explicit point-in-time feature.

## Adapter ownership

Adapter compilers remain in their one-package engine repositories. They receive `PlannedQuery` and
`TranslationContext`, return an opaque parameterized `CompiledQuery`, normalize logical Data, and
must pass the shared conformance contract. Credentials, endpoints, dialect strings, execution,
cancellation wiring, and Engine lifecycle stay inside the adapter/deployment boundary.
