<!-- SPDX-License-Identifier: Apache-2.0 -->

# Security policy

Report vulnerabilities privately through GitHub Security Advisories for this repository. Do not
include credentials, endpoints, production query text, cursor signing keys, or customer Data in a
public issue.

Version 1.0.x receives security fixes while it is the current V1 line. Cursor keys must contain at
least 256 bits, remain in deployment secret management, and rotate by retaining old verification
keys only for the maximum cursor lifetime. Treat serialized Operations as untrusted input: use the
strict `from_mapping` methods, validate against the active registry, and never execute adapter
commands that were not compiled from the validated plan fingerprint.
