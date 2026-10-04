# Contributing

Install Python 3.11+, run `uv sync --extra dev`, and use `uv run pytest`, `uv run ruff check .`,
`uv run ruff format --check .`, and `uv build` before submitting a change.

Keep command-line contracts, examples and documentation consistent. Changes to source access,
evidence delivery, feedback semantics or revision activation require meaningful regression tests.
Use synthetic conversations in tests. Never commit private traces, datasets, credentials, local
workspaces or generated experiment artifacts.

Separate fixed runtime guarantees from model instructions. New revision surfaces must have a
bounded schema, execute through reviewed runtime code, and declare how stale candidates and
regressions are handled. Do not infer quality gains from structural validation or an offline fixture.

Explain the concrete behavior before and after a change, its validation and remaining limits
in a pull request. The CI runs software checks without model keys or paid API calls.
