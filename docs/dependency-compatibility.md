<!-- SPDX-License-Identifier: Apache-2.0 -->

# Public dependency compatibility

Query 1.0.3 consumes Core Expression, Operation, ResourceRef, SchemaRef, shared errors,
CapabilityRequirement and Adapter SPI validation, plus Semantics SchemaDocument,
logical types, canonical serialization, fingerprints and traversal resolution.
These APIs are exercised by the existing integration, contract, unit, property and
adapter-facing conformance suites. Query does not own a real Engine executor or gate;
real Engine regressions remain in the owning adapters and downstream conformance.

Core `>=1.0.1,<2` retains the original supported API floor and admits the compatible
1.1.0 release. Semantics `>=2.0.0,<3` retains the structured-write-mode major boundary
and admits the compatibility repair 2.0.1. Future major releases require review;
untested versions inside the ranges remain unverified, not automatically proven.
No Query runtime code, serialized contract versions or golden fingerprints change.

`contracts/release-validation/dependencies.json` records exact public wheels and
sdists for two tested closures: Core 1.0.1 / Semantics 2.0.0 and Core 1.1.0 /
Semantics 2.0.1. CI selects these exact coordinates, checks their downloaded wheel
hashes, runs normal dependency resolution and `pip check`, and runs all suites on
Python 3.12–3.14. These are reproducible validation selections, not runtime allowlists.
Deployment owners continue to select and hash-lock their own full artifact closure.

Migration: upgrade Query to 1.0.3 through normal package resolution. Select Core 1.1.0
and Semantics 2.0.1 for the repaired platform closure. No persisted Query data, cursor
or Operation migration is needed. The old Query 1.0.2 release remains immutable.
