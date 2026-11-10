# Feedback task starting environment

Upstream: https://github.com/sqlalchemy/sqlalchemy
Upstream commit: `babad5f9065472310c5c018337389357c388712a`
Prepared on macOS with CPython 3.12.11. No external database, queue, or cloud account is required.
The dependency file pins the installed runtime and test packages for the selected baseline. It is not a claim that all optional upstream test suites are installed.

From the repository root:

```sh
export NO_INTERNET=1 TZ=UTC
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r .feedback/requirements.txt -e .
.venv/bin/python -m pytest -q -o addopts= test/sql/test_quote.py test/sql/test_compiler.py -k 'schema_translate or expanding or literal_execute or QuoteTest'
```

Scope note: The scope is the command below, not all optional upstream integrations.

Observed baseline (2026-11-10): `99 passed, 519 deselected in 0.36s`
Whole-command wall time: 4.363 seconds, exit code 0. This is one local measurement, not a runtime guarantee. The first install is excluded.

This seed adds preparation metadata only. Application sources and upstream tests are unchanged; there is no task solution in this commit.
