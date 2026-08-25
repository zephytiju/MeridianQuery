<!-- SPDX-License-Identifier: Apache-2.0 -->

# Meridian Storage Query

`meridian-storage-query` is the engine-neutral Meridian V1 query library. It provides an
immutable query AST, a mapping-first fluent API, canonical serialized Operations, capability
inference, compile/startup validation, single-Binding planning, adapter translation contracts,
and signed live-keyset cursors.

The library does **not** define a `query` Catalog. Consumers use a registered Catalog such as
`structured` or `evidence`; deployment code selects Bindings and adapters. V1 has no
`NativeQuery`, native-expression method, Engine selector, endpoint, or backend DSL escape hatch.

## Install

```bash
python -m pip install meridian-storage-query==1.0.0
```

Python 3.12–3.14 is supported. Version 1.0.0 consumes the released
`meridian-storage-core==1.0.0` and `meridian-storage-semantics==1.0.0` contracts.

## Mapping-first query

```python
from meridian_storage.query import distance_within, field, point, query

plan = (
    query("accounts.users")
    .where({"status": "active", "age": {"$gte": 18}})
    .select("id", "display_name")
    .order_by("display_name", "id")
    .page(size=50)
    .build()
)

assert plan.format_version == "meridian.operation.query.v1"
```

Specialized clauses use the same immutable builder:

```python
search = (
    query("knowledge.documents")
    .where({"visibility": "public"})
    .full_text(
        "storage semantics",
        fields=("title", "body"),
        analyzer="icu",
        highlights=("body",),
        ranking="bm25",
        facets=("category",),
    )
    .build()
)

nearby = query("places.locations").where(
    distance_within(field("location"), point(-122.4, 37.8), 1_000)
).build()
```

For spatial predicates, use `point`, `distance`, or `distance_within`; for time-series windows,
use `time_range`. Aggregation uses `group_by` and `measure`. `traverse` accepts either an explicit
closed relation-Collection set or `all_neighbors=True`, which must be resolved against a captured
registry fingerprint before execution.

## Public pipeline

```text
Catalog Expression
  -> normalized QueryOperation
  -> Schema/Resource/Binding validation
  -> inferred RequirementGraph
  -> NATIVE | EXACT_REWRITE | BOUNDED_RESIDUAL | REJECTED
  -> adapter-owned parameterized compilation
  -> normalized Data and signed cursor
```

`validate_compile` resolves exact Schemas, rejects cross-Binding plans, injects a deterministic
identity tie-breaker, checks logical types and semantic profiles, and enforces a registered
workload budget. `validate_startup` compares declared deployment requirements with authenticated
Core capability manifests. `QueryPlanner` never silently broadens an unsupported query into a
scan: rewrites require a registered equivalence and residuals require explicit row/byte bounds
plus a proof function.

Adapter repositories implement `QueryTranslator`. Its input is a validated `PlannedQuery` and a
logical `TranslationContext`; connection and Engine state remain private to the adapter.
`enforce_result_budget` applies the validated value/byte limits after result normalization.

## Pagination and diagnostics

`CursorSigner` issues opaque HMAC-SHA256 cursors bound to the plan, exact Schema fingerprints,
registry fingerprint when applicable, scope fingerprint, unique sort tuple, page size, issuance,
and expiry. Key rotation accepts old verification keys while issuing with one active key.

Diagnostics use stable `MERIDIAN_*` codes and logical references. Explain records redact secrets,
credentials, endpoints, native statements, and backend DSL details.

## Contracts and compatibility

- [Operation, capability, and cursor contracts](contracts/logical-query/)
- [Public API manifest](contracts/public-api/meridian-query.v1.json)
- [Architecture](docs/architecture.md)
- [Adapter conformance](docs/conformance.md)
- [Compatibility ledger](compatibility.json)
- [Release procedure](RELEASING.md)

The implementation is pinned to Meridian HLD revision 56 and Catalogs/Public Interfaces revision
70, with Query LLD revision 46 and Adapter LLD revision 24 as package-level authorities.

## License

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
