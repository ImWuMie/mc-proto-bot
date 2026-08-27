# Repository Guidelines

## Project Structure & Module Organization

ProtoBot is a Python 3.12+ Minecraft protocol client. The package lives in `protobot/` at the repository root: `client.py` and `cli.py` provide the client and diagnostic entry points, while `state.py`, `world.py`, `navigation.py`, `modlist.py`, and `events.py` contain higher-level behavior. Low-level wire handling is grouped in `protobot/protocol/` (`codec.py`, framing, connection, NBT, and version tables). Movement and collision code belongs in `protobot/physics/`. Versioned block data is stored as compressed JSON under `protobot/data/`. Tests live in `tests/` at the repository root, outside the installed package. `login.py` and `run_bot.py` are top-level convenience scripts and are deliberately excluded from the distribution.

## Build, Test, and Development Commands

The checkout is managed with [uv](https://docs.astral.sh/uv/); `.venv` is created without `pip`, so prefer `uv` over `python -m pip`:

```text
uv sync --extra online
```

That installs the project in editable mode together with the optional `cryptography` extra and the `dev` dependency group. To work offline-only, drop `--extra online`.

Run a fast syntax check before submitting changes:

```text
uv run python -m compileall .
```

Run the tests with `uv run pytest` (narrow the run with a path such as `uv run pytest tests/test_auth.py`). The suite is plain `unittest`, so `python -m unittest discover -s tests -t .` works without installing anything. The diagnostic routines in `cli.py` are intended for local protocol/movement checks; use the corresponding `protobot-*` console command after packaging or call the function directly during development.

## Coding Style & Naming Conventions

Use four spaces, standard Python typing, and focused modules. Keep imports explicit and avoid introducing dependencies without updating `pyproject.toml` and `uv.lock`. Name functions, variables, and modules in `snake_case`; classes and exceptions in `PascalCase`; constants in `UPPER_SNAKE_CASE`. Preserve the existing async style for network operations and keep protocol parsing deterministic and bounds-checked. No formatter or linter is configured, so keep changes `black`-compatible and readable.

## Testing Guidelines

Add regression tests for protocol decoding, physics edge cases, and state transitions. Name files `test_*.py` and test functions `test_<behavior>`. Prefer small deterministic fixtures over live servers; reserve live movement/regression checks for explicitly configured local environments. Tests must not hard-require optional extras — guard `cryptography` imports so the suite still passes on a base install. Ensure `uv run pytest` passes before opening a pull request.

## Commit & Pull Request Guidelines

This checkout has no Git history available, so no established commit convention can be verified. Use concise imperative subjects (for example, `Fix chunk palette decoding`) and keep unrelated changes separate. Pull requests should explain the behavior change, identify affected modules, include test commands/results, and attach logs or screenshots when changing diagnostic output. Call out protocol-version or data-file compatibility implications explicitly.
