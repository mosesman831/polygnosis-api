# Contributing

Thanks for helping improve PolyGnosis API. This is a small, self-contained
FastAPI package — setup is quick.

## Dev setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

There is no `requirements.txt` — all dependencies (runtime and dev) live in
`pyproject.toml`. The `[dev]` extra pulls in `pytest` and `ruff`.

## Lint

Ruff is the linter and import sorter (rules `E, F, I, B, UP`, `E501` ignored):

```bash
python3 -m ruff check src tests
python3 -m ruff check --fix src tests   # auto-fix where possible
```

## Test

```bash
python3 -m pytest
```

Tests are network-free: the pipeline suite drives a canned `FakeLLM` and the API
suite stubs `BoardroomPipeline.run` with an isolated tmp SQLite store. Please add
or extend tests for any behavior change and keep the suite green.

## Branching

- Branch off the current development tip; never commit directly to `main`.
- Use short, descriptive branch names (e.g. `fix/lease-reclaim`,
  `feat/list-endpoint`).
- Keep each commit a single logical change with a clear message.
- Make sure `ruff check src tests` and `pytest` both pass before opening a PR.
- CI runs ruff + pytest on Python 3.11 and 3.12.

## Scope

See `docs/BUILD_SPEC_v0.3.md` for the current build target and `SPEC.md` for the
protocol contract. Deferred items (Hermes/Eve, Redis/Postgres fabric,
mid-pipeline resume, etc.) are listed there — please open an issue before
starting on anything outside the current spec.
