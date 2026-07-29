# AGENTS.md

## Project overview

forge is a local terminal coding agent (Python 3.10+). It connects to OpenAI-compatible or Anthropic-compatible model providers, reads code, runs tools, and maintains persistent memory across sessions.

## Commands

```bash
# Install in dev mode
pip install -e ".[dev]"

# Run the agent (TUI auto-launches in a terminal)
uv run forge
uv run forge --repl          # plain terminal REPL
uv run forge "do something"  # one-shot mode

# Tests (all deterministic, no network needed)
pytest tests/ -q

# Single test file
pytest tests/test_forge.py -q

# Single test function
pytest tests/test_forge.py::test_agent_runs_tool_then_final -q

# Live smoke tests (requires a configured provider key)
FORGE_LIVE_SMOKE=1 pytest tests/test_release_smoke.py -q

# Lint
ruff check forge/ tests/
```

No Makefile, tox, or pre-commit config exists. No conftest.py. No async tests.

## Architecture

| Directory | Purpose |
|---|---|
| `forge/cli.py` | CLI arg parsing, REPL loop, one-shot mode. Entry point: `forge.cli:main` |
| `forge/core/runtime.py` | `Pico` class — the agent runtime. Owns session, memory, tools, engine loop |
| `forge/core/engine.py` | Turn control loop (model → tool → model) |
| `forge/config/__init__.py` | Provider config resolution, TOML/env loading |
| `forge/providers/` | `OpenAICompatibleModelClient`, `AnthropicCompatibleModelClient` |
| `forge/tools/` | Tool registry and built-in tools (read_file, run_shell, write_file, patch, agent, etc.) |
| `forge/features/` | Memory system, skills, sandbox |
| `forge/tui/` | Textual TUI app |
| `forge/evaluation/` | Run evidence, metrics, evaluator |
| `tests/` | All tests; deterministic via `ScriptedModelClient` |

The main runtime class is `Pico` (legacy name), aliased as `Forge` in `forge/__init__.py`.

## Config system

Priority: CLI args > `FORGE_*` env vars > project `.forge.toml` > `~/.config/forge/config.toml` > code defaults.

**Provider profile ≠ protocol.** A profile name (e.g. `deepseek`) selects a config section; `protocol` within that section determines the wire format. Example: `provider = "deepseek"` with `protocol = "anthropic"` sends Anthropic Messages API requests to DeepSeek's endpoint.

Supported protocols: `openai`, `anthropic`. No other protocol values are valid.

## Testing patterns

- Use `ScriptedModelClient` (from `forge.testing`) for deterministic tests. It replays canned model outputs.
- Build test agents via the `build_agent(tmp_path, outputs)` helper pattern — see `tests/test_forge.py:31`.
- `tmp_path` (pytest builtin) is the standard workspace root for tests; create `README.md` in it so `WorkspaceContext.build()` succeeds.
- Live smoke tests are gated by `FORGE_LIVE_SMOKE=1` env var.
- No async tests exist. `pytest-asyncio` is a dev dep but unused.

## Legacy migration

The project was renamed from "pico" to "forge". Key implications:
- `.pico/` → `.forge/` workspace state dir (auto-migrated by `migrate_workspace_layout()`)
- `PICO_*` env vars still work as fallbacks
- `Pico` is still the canonical runtime class name; `Forge` is the public alias

## Conventions

- Code comments and docstrings are in Chinese. Follow this convention.
- No ruff config section in `pyproject.toml` — run `ruff check` with defaults.
- `.forge.toml` and `.env` are gitignored. Never commit real API keys.
- Four docs are tracked in git: `docs/configuration.md`, `docs/memory.md`, `docs/skills.md`, `docs/sandbox.md`. All other `docs/` content is gitignored.
- `.forge/` directory holds all runtime state (sessions, runs, memory, plans) — also gitignored.
