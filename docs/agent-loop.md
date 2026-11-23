# Agent loop

`Agent.ask` takes one user string. It strips the string. An empty result returns immediately and writes nothing.

Otherwise it repairs unpaired tool calls, appends one user message, and takes up to `--max-steps` model rounds. The default is 16. The iteration count follows `range(max_steps)`: zero or a negative value performs no model call. Each round does three things:

1. Fit the transcript to the context window. See [Context](context.md).
2. Stream one chat completion with the tool list captured at the start of this request. The list does not change between rounds.
3. Store the assistant message. If it has no tool calls, return its content, or `""` when there is no content. That is the answer. If it has tool calls, run each one in listed order, append a tool result, and take another round.

Tool calls in one turn are not concurrent. An `error:` result does not skip the later calls in that turn. Each assistant message and each tool result is saved before the next call runs. See [Session](session.md) for the saved objects.

If the loop ends without a final answer, emit `Stopped: tool-call limit reached before a final answer.` That includes a non-positive `--max-steps`, where the user message is already saved and no model call ran. When the limit is reached after a round that still asked for tools, those tools have already run. The notice is shown to the user as answer text and is not stored as an assistant message.

A provider error raises `ChatError` from the round that failed. Text already streamed is not saved, and no partial assistant message is stored. The user message for this request is already on disk. There is no automatic retry. See [Provider](provider.md) for the error text.

Ctrl-C during a tool aborts the remaining tools in that turn. The assistant message is already saved. The next request closes any tool call that has no result. See [Session](session.md).

## System prompt

The first transcript message is `role: "system"` with this content and no trailing newline:

```text
You are Foo AI, a coding agent.
Use the available tools when they help you complete the user's request.
Keep changes focused and responses concise.

Work practices: when the request names a specific approach, implement that. Keep scratch files out of the repository. Review what you changed before declaring done. Change existing files with edit_file, using exact raw text that matches once. Use write_file only to create files.

Reply in the same language as the user's latest message. Do not translate code, paths, or commands.
```

On resume, the stored system message is replaced with this text. The summary call does not use this prompt. Its prompt is in [Context](context.md).

## Command line

```sh
python -m foo_ai
python -m foo_ai "Summarize the files in this directory"
foo-ai --yes "Run the unit tests"
```

The words of a one-shot prompt are joined with single spaces, then stripped by `ask`.

| Flag | Effect |
| --- | --- |
| `--workspace` | Directory tools may read and write. Defaults to the current directory, then resolved to an absolute path. |
| `--max-steps` | Model rounds for one request. Default 16. |
| `--show-thinking` | Print `reasoning_content` as it streams, on standard error. |
| `--verbose` | Print every tool result. Edits and new files print their result either way. |
| `--yes` | Run shell commands without waiting for confirmation. |
| `--provider` | Named provider to use. See [Provider](provider.md). |
| `--version` | Print `Foo AI 0.1.0` and exit 0. |

An unknown flag is an argparse error and exits 2.

Interactive commands, compared after strip:

| Input | Effect |
| --- | --- |
| `/exit`, `/quit` | Leave the process with status 0. |
| `/reset` | Archive the session file and start a new conversation. See [Session](session.md). |
| empty line | Ignored. `ask` is not called. |

Anything else, including `/reset now`, is a user message.

Exit codes:

| Situation | Status |
| --- | --- |
| One-shot answer, `/exit`, `/quit`, EOF at the prompt, Ctrl-C at the prompt | 0 |
| `ChatError` or Ctrl-C during a one-shot request, config error, Ctrl-C while MCP servers start | 1 |
| argparse error | 2 |

Ctrl-C during an interactive request prints a newline and `Interrupted.` on standard output, then returns to the prompt. It does not exit. Ctrl-C or EOF while waiting for the prompt prints a newline and exits 0. A one-shot Ctrl-C prints a newline and the exception text on standard error; `KeyboardInterrupt` has empty text, so that is only a blank line, then status 1.

`ChatError` in an interactive session prints a leading newline and the error on standard error, then the prompt returns.

## What is printed

Startup order, after MCP connect and session load:

1. The session note, only when load or a broken file produced one.
2. `Foo AI · {provider name} · {model}` on standard output.
3. One MCP line per enabled server, on standard output. Disabled servers print nothing. See [MCP](mcp.md).
4. Interactive only: `Workspace {resolved path}` and then `/exit to quit, /reset to clear the conversation.` The path uses the platform's usual string form. A one-shot run does not print this.

The interactive prompt is a newline, then `> `.

The CLI prints stream events. It does not print `ask`'s return value a second time. The stop notice is delivered as one content event so it still appears.

| Event | Where |
| --- | --- |
| Answer text | Standard output, each chunk as it arrives, with no added separator. Flush each chunk. |
| Reasoning | Standard error, each chunk, only with `--show-thinking`. |
| `\n→ {tool name}` | Standard output when the first non-empty name fragment for that tool call arrives. If `--show-thinking`, print a newline on standard error first. The executed name is the full concatenation, which can be longer than this line when the server splits the name. |
| Shell confirmation | Standard output, from the CLI, before the tool result. See [Tools](tools.md). |
| Result of `edit_file` and `write_file` | Standard output, always. |
| Full result of any tool | Standard output, with `--verbose`. |
| `← {tool name} ({N} chars)` | Standard output for the other tools when `--verbose` is off. `N` is the character length of the result string. |
| `\nCompressed old tool results.` | Standard error, when compression rewrites at least one stored tool result. |
| `\nSummarizing earlier turns.` | Standard error, when a summary call starts. |
| `\nSummary skipped: {ChatError}` | Standard error, when that call fails. |
| `\nDiscarded earlier turns.` | Standard error, when a discard at the hard ceiling drops stored turns. |
| `\ncontext 178.1K/1M · usage total 178100 624 · cache 98.62%` | Standard error, after a model round that reports usage. The summary call does not print one. |
| MCP startup lines | Standard output, with the banner. |

After a successful one-shot answer or a successful interactive answer, print one extra newline on standard output. A failure prints on standard error and does not add that extra stdout newline.

Context in that usage line is the round's input tokens against the provider context window. The two totals are that round's input tokens and output tokens. Cache is the share of input tokens served from the prompt cache, and it is omitted when the server does not report cache fields. A round with no usage object prints nothing. The formula is in [Provider](provider.md).
