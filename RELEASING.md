<!-- SPDX-License-Identifier: Apache-2.0 -->

# Releasing

1. Update `_version.py`, `pyproject.toml`, `compatibility.json`, the public API manifest, and
   `CHANGELOG.md` to the same semantic version.
2. Run `scripts/verify.sh` on a clean checkout using released dependencies from PyPI.
3. Build twice with `SOURCE_DATE_EPOCH` fixed and compare wheel/sdist SHA-256 digests.
4. Run `scripts/verify_artifacts.py dist` and install the wheel into a fresh environment.
5. Merge a green reviewed pull request to protected `main`.
6. Create an annotated `vX.Y.Z` tag on the merged `main` commit and push it.
7. The release workflow verifies tag/version/main ancestry, reruns all gates, builds artifacts,
   generates an SPDX SBOM, attests provenance, and creates the GitHub release.
8. PyPI publication uses the protected `pypi` environment and OIDC trusted publishing. The first
   publication requires the project owner to establish namespace/trusted-publisher ownership; do
   not use or request an account password or bypass MFA.

Tags are immutable. If a release fails after publication, issue a new patch version.
