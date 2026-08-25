<!-- SPDX-License-Identifier: Apache-2.0 -->

# Adapter conformance

An adapter repository that implements query compilation must:

1. publish a `QueryCapabilities` descriptor for contract version `1.0.0`;
2. accept only a validated `PlannedQuery` and `TranslationContext`;
3. return a parameterized `CompiledQuery` whose adapter and plan fingerprints match the context;
4. preserve Meridian logical types, null behavior, ICU text semantics, WGS84 meters, deterministic
   ordering, live-keyset continuation, and result shape;
5. normalize backend output to `NormalizedQueryResult` and call `enforce_result_budget`;
6. connect deadline/cancellation to backend execution without exposing connection state;
7. produce credential-free diagnostics and pass the checked-in golden plans.

The shared suite covers native, exact-rewrite, bounded-residual, and rejected planning; nested
capability inference; all-neighbor closure; cross-Binding rejection; scope conjunction; cursor
tamper, staleness, key rotation, and expiry; result value/byte limits; and redacted explain output.

Adapter-specific suites additionally execute golden logical plans against their backend and compare
engine-independent normalized Data. Backend command snapshots may be kept privately in that adapter
repository, but they are not public consumer contracts.
