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

Inside the session, `/reset` archives the conversation and starts a new one. `/exit` quits. `/explore how does config load` runs the built-in explore skill. `/review` runs the built-in review skill, and `/review focus on auth` passes that remainder as its task. The transcript is stored in `.foo-ai/session.jsonl` under the workspace and reloaded on the next launch. `--show-thinking` prints the model's reasoning. `--verbose` prints full tool output. `--yes` runs shell commands without asking.

Copy `foo-ai.example.toml` to `foo-ai.toml` in the launch directory to change providers. `foo-ai.toml` is local and holds API keys; do not commit it. `--provider` or `FOO_AI_PROVIDER` chooses a name. `FOO_AI_BASE_URL`, `FOO_AI_MODEL`, and `FOO_AI_API_KEY` override the selected provider.

## Desktop app

```sh
python -m foo_ai.gui
```

That serves the same agent as a page on the loopback interface and opens it in a browser. `foo-ai-desktop` is the same command after the install above, and `--no-browser` skips the browser. It takes `--workspace`, `--provider`, `--config`, `--max-steps`, `--show-thinking`, `--verbose`, `--yes`, `--host`, and `--port`, so it reads providers and the workspace as the CLI does; a launch without `--workspace` opens the folder the page opened last, and `--config` names the settings file the page writes, and defaults to the user's configuration directory.

The page has a workspace pane, the transcript, and an activity pane. The answer streams into the transcript, each tool call is a card whose result stays folded, reasoning is a folded `thinking` block, and a shell command opens an amber bar with `Deny` and `Run`. A question from the `ask` tool opens a popup with its options and a line for your own answer, and the `todo_write` list shows in the left pane with a progress bar. Reloading the page shows that list again. The provider can be switched while idle and configured in settings, where its base URL, model, and API key are saved to a settings file of the page's own, beside the turn options; the local provider also edits its `chat_template_kwargs` reasoning fields there, and a cloud provider has none; the workspace can be opened from the page, or browsed for with its `Browse…` picker. `/reset`, `/exit`, and a loaded skill as `/name arguments` work as they do at the prompt. Everything is served by the standard library: the program still has no third-party packages.

`desktop/` wraps that page in an Electron window with a hidden title bar, so it opens as a desktop app rather than a tab:

```sh
cd desktop
npm install
npm start
```

Node is needed for the window only; `python -m foo_ai.gui` runs without it. On Linux, AppArmor usually blocks the sandbox Electron expects, and the AppImage also needs FUSE 2. See [docs/gui.md](docs/gui.md) for those steps, and for the API, the events, the guards, and the window's menu.

## Tests

From this directory:

```sh
python -m unittest tests.test_foo_ai tests.test_gui
```

The suite uses only the standard library. It does not call a model server; the desktop tests run the page's server on a free port with a scripted client.

## Tools

`read_file`, `list_dir`, `glob`, `grep`, `edit_file`, `write_file`, `shell`, `todo_write`, and `ask`, plus `run_skill` when a skill is loaded. Paths stay inside `--workspace`, which defaults to the current directory. `foo-ai.toml` is read from the launch directory, which can differ from the workspace. A repository can turn MCP servers on or off for itself with `.foo-ai.toml` in that workspace. The same file can set `max_steps` for that repository. One number covers the request and `/review`. `--max-steps` overrides it. When none of those is set, the budget is 200.

A skill is a `SKILL.md` playbook. The package ships `explore` and `review`. A file at `.foo-ai/skills/<name>/SKILL.md` adds a skill, or replaces the built-in of the same name, on the next launch. See [docs/skills.md](docs/skills.md).

`edit_file` replaces one exact span of raw file text and prints a diff. It fails when that span is missing or matches more than once. `write_file` creates a new file and fails if the path already exists. `shell` prints the command and waits for `y` unless `--yes` is set. It runs in the workspace with a 120 second timeout. `todo_write` replaces the task list for the current request. While an item is still open, the agent keeps going, up to two reminders, unless this request already received an answer from `ask`. `ask` stops for a multiple-choice answer, and you can type your own. The same decision is not asked twice. `--yes` does not pick one. A non-interactive run tells the model to state an assumption and continue.

The launch command does not change. `foo-ai` and `foo-ai --provider deepseek-flash_1` are the same with or without MCP. A server in `foo-ai.toml` starts only when `enabled = true`, then its tools are added as `mcp__<name>__<tool>`. Leave it `false` in the launch file, and turn it on for one repository with `.foo-ai.toml` in that workspace. Restart after changing either file. See [docs/mcp.md](docs/mcp.md).

```toml
# lectio-latin-reader/.foo-ai.toml
[mcp.whitaker]
enabled = true
```

The launch file still holds the command:

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
