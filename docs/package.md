# Package

Foo AI is a Python package named `foo_ai`, version `0.1.0`. The project root contains the package, `pyproject.toml`, `foo-ai.example.toml`, and this `docs/` directory.

## Layout

```text
pyproject.toml
foo-ai.example.toml
.gitignore
foo_ai/__init__.py      # __version__ = "0.1.0"
foo_ai/__main__.py      # calls cli.main and raises SystemExit
foo_ai/prompt.py        # the system prompt text
foo_ai/cli.py           # arguments, printing, prompts, process exit codes
foo_ai/agent.py         # the step loop
foo_ai/client.py        # HTTP, SSE, stored assistant messages
foo_ai/config.py        # providers and MCP tables
foo_ai/context.py       # compress, summary, and discard
foo_ai/session.py       # .foo-ai/session.jsonl
foo_ai/tools.py         # the six built-in tools
foo_ai/mcp.py           # stdio MCP
foo_ai/usage.py         # usage parsing and the context line
```

`foo_ai/__init__.py` is:

```python
"""Foo AI, a small coding agent."""

__version__ = "0.1.0"
```

`foo_ai/__main__.py` imports `main` from `foo_ai.cli` and, when run as `__main__`, raises `SystemExit(main())`.

Runtime imports stay in the standard library. The ones this program needs are `argparse`, `json`, `tomllib`, `urllib.request`, `subprocess`, `difflib`, `hashlib`, `threading`, `pathlib`, `os`, `re`, `dataclasses`, and `datetime`. `tomllib` is why Python 3.11 is the minimum. File diffs are `difflib.unified_diff` with its default of three context lines. Directory search uses `os.walk`. MCP names use `hashlib.sha256`.

## Install

`pyproject.toml`:

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
```

Setuptools is a build backend only. It is not imported by the running program.

Both `python -m foo_ai` and the `foo-ai` script call `foo_ai.cli.main`. `main` accepts an optional argv list and returns an exit code. `--version` prints `Foo AI 0.1.0` to standard output and exits 0.

`.gitignore`:

```gitignore
# Local provider settings, including the API key.
foo-ai.toml

# Resumed conversations.
.foo-ai/
```

## Process order

1. Parse arguments. See [Agent loop](agent-loop.md).
2. Load the provider and the MCP server list from `foo-ai.toml` in the process's current directory. That directory is the launch directory. It can differ from `--workspace`. A config error prints its message on standard error and exits 1, before any server starts and before the banner.
3. Start each enabled MCP server. Ctrl-C during startup prints a newline and exits 1. Servers are stopped when the process exits, including after an error. See [MCP](mcp.md).
4. Resolve `--workspace` to an absolute path and load `.foo-ai/session.jsonl` from it. See [Session](session.md).
5. Print the session note, if there is one, then the banner, then one MCP line per server that was started or that failed.
6. Run one prompt, or the interactive loop.

The workspace used by every tool is that resolved path. `foo-ai.toml` is never read from the workspace unless the workspace happens to be the launch directory.

## What the model must see

Every normal model round sends the same tool list: the six built-in schemas, then the MCP schemas connected at startup, in server order and tool-list order. The summary call sends no `tools` key at all. Assistant messages that remain in the transcript keep `reasoning_content`. Those two rules are what keep a thinking model from rejecting the next request.
