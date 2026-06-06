# forge

Small local coding agent for real repositories.

Forge runs inside your repo, reads code, executes tools, edits files, keeps local session evidence, and carries forward useful project memory across sessions.

![Forge TUI](assets/screenshots/forge-tui-latest.png)

## What Forge is

Forge is a local coding agent with a real runtime, not just a single prompt loop.

It is built around a few practical ideas:

- The agent should run against an actual repository, not an abstract chat.
- Tool use should be observable, reviewable, and bounded by policy.
- Context should be assembled deliberately from workspace facts, memory, skills, and recent history.
- Sessions should leave behind evidence you can inspect later.
- Long-term knowledge should be stored as local files, not hidden in a giant chat transcript.

## What Forge can do

- Read and search code in a local repository
- Run shell commands with approval and optional sandboxing
- Write files and patch existing files
- Work in TUI, REPL, or one-shot CLI mode
- Maintain working memory and durable project memory
- Persist session history, event streams, run traces, task state, and reports
- Support plan mode, todo tracking, worker agents, and reusable skills
- Load OpenAI-compatible and Anthropic-compatible providers, including DeepSeek profiles

## Why the name Forge

Forge is meant to feel like a workshop for local code work:

- the model is the planner
- the tools are the hands
- the repository is the material
- the runtime is the discipline that keeps the work coherent

The goal is not just to generate text, but to shape changes safely inside a real codebase.

## Interface

Forge ships with a Textual TUI and a plain terminal REPL on top of the same runtime.

| TUI intro | Tools and actions |
| --- | --- |
| ![Forge intro](assets/screenshots/forge-tui-intro.png) | ![Forge tools](assets/screenshots/forge-tui-tools.png) |

| Skills and help | Memory and workspace context |
| --- | --- |
| ![Forge skills](assets/screenshots/forge-tui-skills-help.png) | ![Forge memory](assets/screenshots/forge-tui-memory-skills.png) |

## Core runtime pieces

- `provider profile`: chooses model, endpoint, protocol, and auth
- `context assembly`: builds prompts from system rules, workspace facts, memory, skills, and history
- `tool runtime`: validates and executes file tools, shell, plan tools, workers, and user prompts
- `approval and sandbox`: gates risky actions before they run
- `session and run evidence`: records events, traces, reports, and task state under `.forge/`
- `memory and auto-dream`: turns local observations into reusable project memory

## Install

Requirements:

- Python 3.10+
- At least one working model provider

Install from source:

```bash
git clone <your-repo-url>
cd forge
pip install -e .
```

You can also run it directly in a development checkout:

```bash
python -m forge
```

## Quick start

Copy the example config:

```bash
cp .forge.toml.example .forge.toml
```

Then fill in a provider profile, for example:

```toml
provider = "deepseek"

[providers.deepseek]
protocol = "anthropic"
api_key = "sk-..."
base_url = "https://api.deepseek.com/anthropic"
model = "deepseek-v4-pro"
```

Start Forge:

```bash
forge
```

Common entrypoints:

```bash
forge
forge --repl
forge "find the cause of the flaky login 500"
forge --resume latest
forge --cwd /path/to/repo
forge --approval ask
forge --sandbox best_effort
```

## Configuration

Forge resolves configuration in this order:

```text
CLI args > environment variables > project .forge.toml > global config > built-in defaults
```

Supported provider styles:

- OpenAI-compatible
- Anthropic-compatible
- DeepSeek via profile configuration

Useful environment variables:

- `FORGE_PROVIDER`
- `FORGE_API_KEY`
- `FORGE_BASE_URL`
- `FORGE_MODEL`
- `OPENAI_API_KEY`
- `ANTHROPIC_API_KEY`
- `DEEPSEEK_API_KEY`

More detail:

- [Configuration](docs/configuration.md)
- [Sandbox](docs/sandbox.md)

## Daily workflow

Inside TUI or REPL, you can use natural language or slash commands:

```text
/help
/skills
/session
/context
/usage
/memory
/remember This repo uses pytest, not unittest.
/dream
/plan Refactor provider loading flow
/agents
/compact
```

## Memory model

Forge uses layered local memory instead of replaying an entire transcript forever.

- working memory for the current session
- daily logs for append-only observations
- durable topic files for long-lived project knowledge
- `MEMORY.md` as a compact index

It can also run auto-dream consolidation in the background to turn scattered notes into durable memory files.

More detail:

- [Memory](docs/memory.md)

## Skills

Skills are reusable markdown-based workflows that run through the same runtime and tool system.

Built-in patterns include things like:

- review
- test
- commit
- simplify

You can also add your own project or user skills through `SKILL.md` files.

More detail:

- [Skills](docs/skills.md)

## Project layout

```text
forge/
|-- forge/
|   |-- cli.py
|   |-- core/
|   |-- providers/
|   |-- tools/
|   |-- features/
|   `-- tui/
|-- docs/
|-- tests/
|-- assets/
`-- release/
```

Key directories:

- `forge/core/`: runtime, engine, evidence, workers, context, permissions
- `forge/providers/`: provider adapters
- `forge/tools/`: tool registry and tool implementations
- `forge/features/`: memory, skills, sandboxing
- `forge/tui/`: Textual UI
- `tests/`: runtime and acceptance tests
- `release/v3/`: changelog, review pack, learning notes, testing material

## Local state

Forge keeps runtime state in local workspace files such as:

- `.forge/sessions/<id>.json`
- `.forge/sessions/<id>.events.jsonl`
- `.forge/runs/<run_id>/`
- `.forge/memory/`

This is intentional: sessions, traces, and memory stay inspectable and local.

## Testing

Install dev dependencies and run tests:

```bash
pip install -e ".[dev]"
pytest tests/ -q
```

There is also a release pack under [`release/v3`](release/v3/README.md) with:

- changelog
- review notes
- testing summaries
- architecture learning material

## Docs

- [Configuration](docs/configuration.md)
- [Memory](docs/memory.md)
- [Skills](docs/skills.md)
- [Sandbox](docs/sandbox.md)
- [v3 Release Pack](release/v3/README.md)
- [v3 Changelog](release/v3/CHANGELOG.md)

## Status

This repository is the renamed Forge version of the local coding agent runtime you have been evolving from the earlier Pico codebase.

The branding, TUI surface, state directory, and documentation are being aligned around `forge`, while the core runtime keeps the same practical focus: local coding, observable tool use, memory, and evidence.

## License

MIT
