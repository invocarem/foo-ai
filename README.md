# Foo AI

A small Python coding agent. It sends a short system prompt and an OpenAI-style tool list to a local chat server, runs the tool calls, and sends the results back until the model answers.

The default provider is **home**:

| | |
| --- | --- |
| Address | `http://100.109.56.33:81/v1` |
| Model | `DeepSeek-V4.1-Flash-1017` |
| Extra body | `chat_template_kwargs.reasoning_effort = "max"`, `thinking = true` |

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

Optional install so the `foo-ai` command is on your path:

```sh
pip install -e .
foo-ai
```

Inside the session, `/reset` clears the conversation and `/exit` quits. `--show-thinking` prints the model's reasoning. `--verbose` prints full tool output.

Copy `foo-ai.example.toml` to `foo-ai.toml` in the launch directory to change the provider. Environment variables `FOO_AI_PROVIDER`, `FOO_AI_BASE_URL`, `FOO_AI_MODEL`, and `FOO_AI_API_KEY` override that file.

## Tools

`read_file`, `list_dir`, `grep`, `write_file`, and `shell`. Paths stay inside the launch directory. `shell` runs there with a 120 second timeout.

Each request is a streaming `POST` to `/v1/chat/completions`. Assistant turns are sent back with `reasoning_content`, and each tool call is followed by a `role: "tool"` result with the same `tool_call_id`.
