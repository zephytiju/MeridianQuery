<!-- SPDX-License-Identifier: Apache-2.0 -->

# Releasing

1. Update `_version.py`, `pyproject.toml`, `compatibility.json`, the public API manifest, and
   `CHANGELOG.md` to the same semantic version.
2. Run `scripts/verify.sh` on a clean checkout using released dependencies from PyPI.
3. Build twice with `SOURCE_DATE_EPOCH` fixed and compare wheel/sdist SHA-256 digests.
4. Run `scripts/verify_artifacts.py dist` and `scripts/verify_installed.sh` to test the wheel
   in a fresh environment with normal PyPI dependency resolution. After publication, repeat with
   `bash scripts/verify_installed.sh 'meridian-storage-query[test]==1.0.2'` for registry-only evidence.
5. Merge a green reviewed pull request to protected `main`.
6. Create an annotated `vX.Y.Z` tag on the merged `main` commit and push it.
7. The release workflow verifies tag/version/main ancestry, reruns all gates, builds artifacts,
   generates an SPDX SBOM, attests provenance, and creates the GitHub release.
8. PyPI publication uses the protected `pypi` environment and OIDC trusted publishing. The first
   publication requires the project owner to establish namespace/trusted-publisher ownership; do
   not use or request an account password or bypass MFA.

The workflow can also be dispatched with an existing tag and `publish_pypi=false`. That recovery
path checks out the tag, reruns every build gate, and compares the rebuilt wheel, sdist, checksum
manifest, and normalized SPDX SBOM with the existing GitHub release without mutating it. Never
enable PyPI publication when re-verifying a version that is already present.

Tags and published distributions are immutable. If package content must change after publication,
issue a new patch version.
