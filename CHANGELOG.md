<!-- SPDX-License-Identifier: Apache-2.0 -->

# Changelog

All notable changes follow Keep a Changelog and semantic versioning.

## 1.0.3 — 2026-09-08

- Replace historical exact dependency recipes with Core `>=1.0.1,<2` and Semantics
  `>=2.0.0,<3` public API compatibility bounds.
- Validate historical and Core 1.1.0 dependency closures independently, retaining
  expression/type validation, bounded planning, cursor and fingerprint contracts.
- Record exact public dependency hashes separately from package compatibility metadata.

## 1.0.2 - 2026-09-06

- Accept complete pip requirement arguments in the installed-package verifier, so public release
  checks can place test extras before the version. Dependency pins and Query behavior are unchanged.

## 1.0.1 - 2026-09-06

- Consume the published Core 1.0.1 and Semantics 2.0.0 release set through normal dependency resolution.
- Align compatibility metadata and release verification; preserve Query v1 plans, fingerprints,
  builders, normalization, ordering, cursors, and errors.

## 1.0.0 - 2026-08-25

- Added the immutable Meridian V1 query AST and mapping-first fluent API.
- Added filters, projection, ordering, pagination, full-text, WGS84 distance, time-series,
  aggregation, joins, explicit relation traversal, and registry-closed all-neighbor traversal.
- Added canonical `meridian.operation.query.v1` serialization and released Core integration.
- Added nested semantic requirement inference, startup/compile validation, reviewed budgets, and
  single-Binding enforcement.
- Added native/exact-rewrite/proved-residual/rejected planning and adapter translation contracts.
- Added signed live-keyset cursors, redacted diagnostics, JSON Schemas, conformance fixtures, CI,
  deterministic artifacts, and Apache-2.0 licensing.
- Explicitly excluded `NativeQuery` and all Engine-selection/public-backend syntax from V1.
