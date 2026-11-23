# Foo AI specification

Foo AI is a small Python coding agent. One process sends a conversation to an OpenAI-compatible chat server, runs the tool calls the model returns, and repeats until the model answers or the step limit is reached.

These documents are the specification. Implement the program from them. The root [README](../README.md) is the user guide; when it disagrees with a document here, follow the document here.

The specification fixes observable behavior: files on disk, request and message shapes, tool results, printed text, and exit codes. Splitting that behavior across modules is the implementer's choice. [Package](package.md) shows one workable layout. A program that matches the behavior with a different split still meets this specification.

Truncation markers use the single character `…` (U+2026), not three periods.

| Document | What it specifies |
| --- | --- |
| [Package](package.md) | Language, entry points, dependencies, and process order |
| [Agent loop](agent-loop.md) | The step loop, CLI, printing, and system prompt |
| [Tools](tools.md) | The six built-in tools, their schemas, and their result text |
| [MCP](mcp.md) | Stdio MCP servers and the tools they add |
| [Context](context.md) | Compress, summarize, or discard at 80% of the context window |
| [Session](session.md) | The transcript file, resume, reset, and message shapes |
| [Provider](provider.md) | Provider selection, the HTTP request, streaming, and the usage line |

Python 3.11 or newer. The running program has no third-party dependencies. Parse TOML with the standard-library `tomllib` module. Do not add a retry of a failed model call, parallel tool execution, extra built-in tools, or a special case for any one MCP server.
