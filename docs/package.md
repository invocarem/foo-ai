# Package

Foo AI is a Python package named `foo_ai`, version `0.1.0`. The project root contains the package, `pyproject.toml`, `foo-ai.example.toml`, and this `docs/` directory.

## Layout

Module boundaries inside `foo_ai` are the implementer's choice. The tree below is one workable split. Merging or renaming those modules still meets this specification when the behavior in the other documents is unchanged.

```text
pyproject.toml
foo-ai.example.toml
.gitignore
foo_ai/__init__.py      # __version__ = "0.1.0"
foo_ai/__main__.py      # calls main and raises SystemExit
foo_ai/prompt.py        # the system prompt text
foo_ai/cli.py           # arguments, printing, prompts, process exit codes
foo_ai/agent.py         # the step loop
foo_ai/client.py        # HTTP, SSE, stored assistant messages
foo_ai/config.py        # providers and MCP tables
foo_ai/context.py       # compress, summary, and discard
foo_ai/session.py       # .foo-ai/session.jsonl
foo_ai/tools.py         # the six built-in tools
foo_ai/skill.py         # skill files, run_skill, and subagent runs
foo_ai/skills/review/SKILL.md
foo_ai/mcp.py           # stdio MCP
foo_ai/usage.py         # usage parsing and the context line
```

`python -m foo_ai` requires `foo_ai/__main__.py`. That file calls `main` and, when run as `__main__`, raises `SystemExit(main())`. In the recommended split, `main` lives in `foo_ai.cli` and `__init__.py` is:

```python
"""Foo AI, a small coding agent."""

__version__ = "0.1.0"
```

The running program imports only the standard library. Parse `foo-ai.toml` with `tomllib`. That module is why Python 3.11 is the minimum, and it is the TOML parser: the program does not carry another TOML package and does not implement TOML itself. File diffs are `difflib.unified_diff` with its default of three context lines. Directory search uses `os.walk`. MCP names use `hashlib.sha256`. HTTP uses `urllib.request`. Processes use `subprocess`. The MCP client reads stdout and stderr on separate threads.

## Install

Setuptools is the build backend only. It is not imported by the running program. The project name is `foo-ai`, the version is `0.1.0`, `requires-python` is `>=3.11`, and `dependencies` is empty. The console script is named `foo-ai` and calls the same `main` as `python -m foo_ai`. `main` accepts an optional argv list and returns an exit code. `--version` prints `Foo AI 0.1.0` to standard output and exits 0.

With the recommended split, `pyproject.toml` is:

```toml
[build-system]
requires = ["setuptools>=61"]
build-backend = "setuptools.build_meta"

[project]
name = "foo-ai"
version = "0.1.0"
description = "A small Python coding agent."
requires-python = ">=3.11"
dependencies = []

[project.scripts]
foo-ai = "foo_ai.cli:main"

[tool.setuptools.packages.find]
include = ["foo_ai*"]

[tool.setuptools.package-data]
foo_ai = ["skills/*/SKILL.md"]
```

`.gitignore`:

```gitignore
# Local provider settings, including the API key.
foo-ai.toml

# Resumed conversations.
.foo-ai/
```

## Process order

1. Parse arguments. See [Agent loop](agent-loop.md).
2. Resolve `--workspace` to an absolute path. The default is the current directory.
3. Load the provider, the step budget, and the MCP server list from `foo-ai.toml` in the process's current directory. That directory is the launch directory. It can differ from `--workspace`. When `<workspace>/.foo-ai.toml` exists, merge its `[mcp]` tables over that list and let its `max_steps` replace the launch value. `--max-steps` replaces both. A config error prints its message on standard error and exits 1, before any server starts and before the banner.
4. Start each enabled MCP server. Ctrl-C during startup prints a newline and exits 1. Servers are stopped when the process exits, including after an error. See [MCP](mcp.md).
5. Load skills from the package and from `.foo-ai/skills` in the workspace. Print each skip line on standard error. Then load `.foo-ai/session.jsonl`. The system prompt includes the skill catalog. See [Skills](skills.md) and [Session](session.md).
6. Print the session note, if there is one, then the banner, then one MCP line per server that was started or that failed.
7. Run one prompt, or the interactive loop.

The workspace used by every tool is that resolved path. `foo-ai.toml` is read from the launch directory. `<workspace>/.foo-ai.toml` is read from the workspace and only adjusts MCP servers. See [MCP](mcp.md).

## What the model must see

Every normal model round sends the same tool list: the six built-in schemas, then `run_skill` when at least one skill is loaded, then the MCP schemas connected at startup, in server order and tool-list order. The summary call and the closing round after the step limit send no `tools` key at all. See [Agent loop](agent-loop.md). Assistant messages that remain in the transcript keep `reasoning_content`. Those two rules are what keep a thinking model from rejecting the next request. A skill subagent sends only its allowed built-in tools. See [Skills](skills.md).
