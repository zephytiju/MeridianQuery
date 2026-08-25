<!-- SPDX-License-Identifier: Apache-2.0 -->

# Wire contracts

## Query Operation

`meridian.operation.query.v1` has the closed root fields `formatVersion`, `catalog`, `targets`,
`operation`, `result`, `filter`, `joins`, `grouping`, `aggregates`, `traversal`, `order`, `page`,
`consistency`, `options`, and `extensions`. `QueryOperation.to_dict()` is canonical input to the
released Core `Operation` envelope. Unknown fields and unknown AST node kinds are rejected.

V1 supports typed fields, literals, parameters, boolean composition, equality/order comparisons,
arithmetic, null and membership tests, string prefix/contains, UTC timestamp ranges, JSON pointer
lookup, full-text match, WGS84/meter distance, projections, sorting, and aggregate inputs.

`QueryOperation.from_mapping()` is strict. It does not accept `NativeQuery`, Engine commands,
backend DSL, or implicit extensions. A non-empty extension requires an `ExtensionPolicy` whose
allowlist contains a valid namespaced logical key.

## Capabilities

`meridian.query.capabilities.v1` declares adapter identity, contract version, operations, native
semantic IDs, operators, logical types, consistency classes, guarantees, logical features, and
limits. Capability comparison is set/limit based and vendor-neutral. The descriptor fingerprint is
part of every plan and is rechecked immediately before compilation.

## Cursors

`meridian.query.cursor.v1` is the signed payload format. The API only exposes the opaque `mqc1.*`
token. Payload fields bind plan, Schemas, registry when applicable, scope, sort tuple, page size,
key ID, issuance, and expiry. HMAC verification uses constant-time comparison and accepts only
base64url segments and exact lowercase SHA-256 fingerprints.

The JSON Schemas under `contracts/logical-query/` are shipped inside both wheel and source
distribution. `meridian_storage.query.contracts` loads the same files in source and installed modes.
