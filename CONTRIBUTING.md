<!-- SPDX-License-Identifier: Apache-2.0 -->

# Contributing

Use Python 3.12 or newer and install the test extra:

```bash
python -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/ruff format --check src tests
.venv/bin/ruff check src tests
.venv/bin/mypy src
.venv/bin/pytest --cov=meridian_storage.query
```

Public contract changes require tests, compatibility notes, and an approved design write-back.
Preserve the one-package repository boundary, mapping-first Expressions, one-Binding V1 limit,
deterministic rejection, and absence of NativeQuery/Engine-facing consumer syntax. New source,
tests, scripts, and configuration must carry an SPDX `Apache-2.0` identifier where the format
supports comments.

Submit changes through a pull request. CI must be green and required review/branch protection must
remain in force.
