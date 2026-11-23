# Foo AI

A small Python coding agent. It sends a system prompt and an OpenAI-style tool list to an OpenAI-compatible chat server, runs the tool calls, and sends the results back until the model answers. The behavior to implement is specified in [docs/README.md](docs/README.md).

The default provider is **home**. `--provider deepseek-flash_1` selects the DeepSeek API model `deepseek-flash`.

| | home | deepseek-flash_1 |
| --- | --- | --- |
| Address | `http://127.0.0.1:8100/v1` | `https://api.deepseek.com/v1` |
| Model | `DeepSeek-V4.1-Flash-1017` | `deepseek-flash` |
| Extra body | `chat_template_kwargs.reasoning_effort = "max"`, `thinking = true` | none |

Python 3.11 or newer. No third-party packages.

## Run

From this directory, in the project you want the agent to edit:

```sh
python -m foo_ai
```

One request:

```sh
python -m foo_ai "Summarize the files in this directory"
```

Print the version and exit:

```sh
python -m foo_ai --version
```

That prints `Foo AI 0.1.0`.

Optional install so the `foo-ai` command is on your path:

```sh
pip install -r requirements.txt
foo-ai
```

`requirements.txt` has no third-party packages. It installs this directory with `-e .`, which is the same as `pip install -e .`.

Inside the session, `/reset` archives the conversation and starts a new one. `/exit` quits. The transcript is stored in `.foo-ai/session.jsonl` under the workspace and reloaded on the next launch. `--show-thinking` prints the model's reasoning. `--verbose` prints full tool output. `--yes` runs shell commands without asking.

Copy `foo-ai.example.toml` to `foo-ai.toml` in the launch directory to change providers. `foo-ai.toml` is local and holds API keys; do not commit it. `--provider` or `FOO_AI_PROVIDER` chooses a name. `FOO_AI_BASE_URL`, `FOO_AI_MODEL`, and `FOO_AI_API_KEY` override the selected provider.

## Tests

From this directory:

```sh
python -m unittest tests.test_foo_ai
```

The suite uses only the standard library. It does not call a model server.

## Tools

`read_file`, `list_dir`, `grep`, `edit_file`, `write_file`, and `shell`. Paths stay inside `--workspace`, which defaults to the current directory. `foo-ai.toml` is read from the launch directory, which can differ from the workspace.

`edit_file` replaces one exact span of raw file text and prints a diff. It fails when that span is missing or matches more than once. `write_file` creates a new file and fails if the path already exists. `shell` prints the command and waits for `y` unless `--yes` is set. It runs in the workspace with a 120 second timeout.

The launch command does not change. `foo-ai` and `foo-ai --provider deepseek-flash_1` are the same with or without MCP. A server in `foo-ai.toml` starts only when `enabled = true`, then its tools are added as `mcp__<name>__<tool>`. Leave it `false` until you want those tools, and restart after changing it. See [docs/mcp.md](docs/mcp.md).

```toml
[mcp.whitaker]
enabled = false
command = "docker"
args = ["exec", "-i", "whitaker-mcp", "python", "-u", "/app/whitaker_server.py"]
timeout_seconds = 120
```

Before each model request, Foo AI estimates the transcript in tokens. At 80% of the context window — 800,000 tokens on the default 1,000,000 window, printed `800K` — it compresses oversized tool results and, if that is not enough, summarizes earlier turns into the session. At the hard ceiling, 256 tokens under the window, it discards the oldest turns. The sample line `178.1K/1M` is 621.9K of room under that 80% line, so a session at 178.1K is left as it is. Assistant turns that remain in the transcript keep `reasoning_content`, which the thinking model requires.

Each request is a streaming `POST`. The URL is `base_url` with one trailing slash removed, then `/chat/completions` unless the URL already ends with that suffix. The request asks the server for token usage. Assistant turns are sent back with `reasoning_content`, and each tool call is followed by a `role: "tool"` result with the same `tool_call_id`.

When the server reports usage, Foo AI prints one line on standard error after that round, for example `context 178.1K/1M · usage total 178100 624 · cache 98.62%`. Context is input tokens against the model window. The two totals are input and output tokens. Cache is the prompt-cache hit rate.
