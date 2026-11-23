# Foo AI specification

Foo AI is a small Python coding agent. One process sends a conversation to an OpenAI-compatible chat server, runs the tool calls the model returns, and repeats until the model answers or the step limit is reached.

These documents are the specification. Implement the program from them. The root [README](../README.md) is the user guide; when it disagrees with a document here, follow the document here.

Truncation markers use the single character `…` (U+2026), not three periods.

| Document | What it specifies | Module |
| --- | --- | --- |
| [Package](package.md) | Language, layout, entry points, and process order | `foo_ai/` |
| [Agent loop](agent-loop.md) | The step loop, CLI, printing, and system prompt | `foo_ai/agent.py`, `foo_ai/cli.py`, `foo_ai/prompt.py` |
| [Tools](tools.md) | The six built-in tools, their schemas, and their result text | `foo_ai/tools.py` |
| [MCP](mcp.md) | Stdio MCP servers and the tools they add | `foo_ai/mcp.py`, `foo_ai/config.py` |
| [Context](context.md) | Compress, summarize, or discard at 80% of the context window | `foo_ai/context.py` |
| [Session](session.md) | The transcript file, resume, reset, and message shapes | `foo_ai/session.py` |
| [Provider](provider.md) | Provider selection, the HTTP request, streaming, and the usage line | `foo_ai/config.py`, `foo_ai/client.py`, `foo_ai/usage.py` |

Python 3.11 or newer. The running program has no third-party dependencies. Do not add a retry of a failed model call, parallel tool execution, extra built-in tools, or a special case for any one MCP server.
