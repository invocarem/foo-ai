# MCP

Foo AI can connect to Model Context Protocol servers and add their tools to the list the model already sees. The client uses the standard library. The transport is stdio: the server is a subprocess, and each JSON-RPC message is one JSON object on a line, encoded as UTF-8 with non-ASCII kept as characters. A blank line, a line that is not JSON, or a JSON value that is not an object is ignored. Foo AI has no special case for a particular server. The Whitaker block below is only an example of a table.

Servers are declared in `foo-ai.toml` in the launch directory, the same file as [providers](provider.md).

```toml
[mcp.whitaker]
enabled = false
command = "docker"
args = ["exec", "-i", "whitaker-mcp", "python", "-u", "/app/whitaker_server.py"]
timeout_seconds = 120

[mcp.whitaker.env]
PYTHONUNBUFFERED = "1"
```

Run Foo AI the same way as before: `foo-ai`, or `foo-ai --provider deepseek-flash_1`. There is no separate MCP command. A server starts only when `enabled` is boolean `true`. `false`, a missing `enabled`, or any other value such as `1` keeps the block in the file and does not connect. A disabled table is not validated. Restart Foo AI after changing the file.

`command` is the executable and is required on an enabled server. A missing `command` key fails config with `mcp {name}: command is required` before any server starts. `args` defaults to an empty list. A single string becomes a one-item list. A list of strings, integers, or floats is stringified. A bool or any other item fails with `mcp {name}: args must be an array of strings`. `timeout_seconds` limits `initialize`, each `tools/list` page, and each `tools/call`. The default is 60 seconds. It must be a positive number; a bool or a non-positive value fails with `mcp {name}: timeout_seconds must be a positive number of seconds`. A float is truncated. `cwd` is the subprocess working directory; blank means the child's usual directory, which is the launch directory. `[mcp.{name}.env]` adds environment variables on top of a copy of the current process environment. Null values in that table are skipped. When `env` is omitted, the child inherits the environment by leaving it unset, not by passing an empty map. A non-table `env` fails with `mcp {name}: env must be a table of strings`.

If the whole `mcp` value is not a table, config fails with `mcp must be a table of servers, for example [mcp.whitaker]`. A server entry that is not a table fails with `mcp {name}: expected a table with command and args`. Enabled servers are kept in file order.

## Workspace file

A repository turns servers on or off without editing the launch file. After the launch `foo-ai.toml` is loaded, Foo AI reads `<workspace>/.foo-ai.toml` when that file exists and merges its `[mcp]` tables over the launch tables. The workspace is `--workspace`, already resolved. The default workspace is the launch directory, so a `.foo-ai.toml` beside `foo-ai.toml` still applies. A missing file changes nothing. `max_steps` in that file replaces the launch value, as [Agent loop](agent-loop.md) describes. Providers, `default_provider`, and every other key are ignored. API keys and the model stay in the launch file.

```toml
[mcp.whitaker]
enabled = true
```

That turns on `whitaker` when the launch file already declared `command` and `args` with `enabled = false`. Restart Foo AI after changing either file. The workspace file is safe to commit. It does not hold the provider key.

Merge rules:

- A server name already in the launch file keeps its place. Keys present in the workspace table replace those keys. `env`, when present, replaces the whole environment table. Keys the workspace file omits stay as the launch file set them.
- A server name only in the workspace file is appended after the launch servers, in the workspace file's order.
- `enabled` has to be boolean `true` after the merge. `false`, a missing `enabled`, or any other value drops the server. A dropped server is not validated, so invalid `args` on a server the workspace file turns off do not fail config.
- An enabled server is validated with the same rules as the launch file. A server the workspace file turns on still needs a `command`, from either file. A missing command fails with `mcp {name}: command is required`.

The workspace file is parsed with `tomllib`. A syntax error fails with `.foo-ai.toml: ` plus the parser message. A root value that is not a table fails with `.foo-ai.toml must be a table`. When `mcp` is present and is not a table, the message is `.foo-ai.toml: mcp must be a table of servers, for example [mcp.whitaker]`. An unreadable file fails with `cannot read {path}: {error}`.

Python speaking this protocol needs an unbuffered stdout. Inside Docker that is `python -u`. A buffered pipe holds the handshake response until the process exits, and launch then waits until the timeout.

The child is not started through a shell. The argument vector is the command followed by `args`. Standard input, output, and error are pipes. Decode text as UTF-8 and replace invalid bytes. Read stdout and stderr on separate threads so a log line cannot stall the handshake. Keep the last 4,000 characters of stderr for the failure text.

## Launch

Foo AI starts each enabled server, sends `initialize`, then `notifications/initialized`, then `tools/list`. Request ids are integers starting at 1. A request is:

```json
{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
  "protocolVersion": "2024-11-05",
  "capabilities": {},
  "clientInfo": {"name": "foo-ai", "version": "0.1.0"}
}}
```

The version is the package version. Capabilities stay empty: do not advertise roots, sampling, or other client features. A successful initialize result is ignored. Foo AI does not check the server's protocol version. An error result fails startup for that server.

The initialized notification has no id:

```json
{"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
```

`tools/list` starts with `{}`. When the result's `nextCursor` is a non-empty string that has not been seen, request the next page with `{"cursor": "{nextCursor}"}`. Stop on a missing, empty, or repeated cursor, and stop after 20 pages. Concatenate the `tools` arrays and ignore items that are not objects. A result that is not an object fails startup with `tools/list returned no result`.

The wait for each of those responses is the server's `timeout_seconds` deadline, not an idle timeout between bytes. On timeout the error is `timed out waiting for {method}`.

A message from the server that has a method and an id is a server request. Answer `ping` with `{"jsonrpc":"2.0","id":{id},"result":{}}`. Answer any other method with error code `-32601` and message `method not found: {method}`. This has to work during `initialize`, because a server may ping before it answers. A message with an id and no method is matched to the waiting request. A message with no id is ignored.

One startup line is printed for each enabled server:

```text
mcp whitaker: analyze_latin, analyze_latin_batch, analyze_latin_text
mcp other: unavailable (cannot start docker: ...)
mcp demo: connected, no tools
mcp demo: connected, tool names already in use
```

The first form lists the server's original tool names, separated by a comma and a space, for tools that were actually added. A server that cannot be started is skipped. The built-in tools still run, and the process does not exit. Collapse the failure text to one line: if stderr was captured and the process has a return code, use `exit {code}: {stderr}`; otherwise use stderr, or `exit {code}`, or `server closed stdout`. Then turn all whitespace into single spaces and, if the text is longer than 200 characters, keep 200 and append `…`. A spawn failure uses `cannot start {command}: {operating-system error}` inside that same one-line form. A blank command fails with `command is required`.

The tool list from a successful server stays fixed for the process, including later rounds of the same request. Connect is idempotent: calling it again returns the same lines and does not start the servers again.

The server process is stopped when Foo AI exits. Close its stdin, wait 2 seconds, terminate, wait 2 seconds, kill, and wait 2 seconds.

## Names and schemas

The advertised name is `mcp__{server}__{tool}`, for example `mcp__whitaker__analyze_latin`. Sanitize the server part and the tool part separately. Keep ASCII letters, digits, `_`, and `-`. Every other character becomes `_`, and a run of replaced characters becomes one `_`. Strip `_` from both ends of that part. If nothing remains, use `tool`. `a b` and `c/d` become `mcp__a_b__c_d`.

When that full name is longer than 64 characters, take the SHA-256 hex digest of the full name's UTF-8 bytes, keep the first 8 hex characters, and use the first 55 characters of the full name with trailing underscores removed, then `_`, then those 8 hex characters. The result matches `^[A-Za-z0-9_-]+$` and is at most 64 characters. Two long names that share a 55-character prefix still differ in the digest.

If the sanitized name is one of the six built-in names, or it was already added by this process, skip that tool. Do not rename it further. If every named tool was skipped, the startup line is `mcp {name}: connected, tool names already in use`. If the server listed no named tools, the line is `mcp {name}: connected, no tools`.

The schema sent to the model is:

```json
{
  "type": "function",
  "function": {
    "name": "mcp__whitaker__analyze_latin",
    "description": "[whitaker] Morphological analysis of one Latin word.",
    "parameters": {"type": "object", "properties": {"word": {"type": "string"}}}
  }
}
```

The description is `[{server table name}] ` plus the server's description, stripped. The bracketed name is the toml name, not the sanitized name. When the description is missing or blank, use the raw tool name after the prefix. If the description is longer than 2,000 characters, keep 2,000 and append `…`.

`parameters` is the tool's `inputSchema` with `$schema` removed. If that schema is missing or not an object, use `{"type":"object","properties":{}}`. If `type` or `properties` is missing, set `type` to `object` and `properties` to `{}`.

## Calls

A model call becomes `tools/call` with the server's original tool name and the arguments object:

```json
{"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "analyze_latin", "arguments": {"word": "ave"}}}
```

Parse the model's arguments the same way as a built-in tool, except a JSON value that is not an object returns `error: arguments must be a JSON object`. An unknown advertised name returns `error: unknown tool {Python repr}`.

Turn the result into text, then apply the same 20,000-character cap as the built-in tools: first 20,000 characters, a newline, and `…[truncated]`.

Join content parts with newlines and strip the result. A string `content` is one part. In a list, a string block is a part, and these objects count:

| Block | Text |
| --- | --- |
| `type: "text"` with a string `text` | That text. |
| `type: "image"` | `[image]` |
| `type: "resource"` whose `resource.text` is a string | That text. |
| `type: "resource"` with a string `resource.uri` and no text | `[resource {uri}]` |
| `type: "resource"` otherwise | `[resource]` |
| Any other non-empty `type` | `[{type}]` |

So text `a`, an image, and a resource whose text is `body` become `a\n[image]\nbody`.

When the server sets `isError`, an empty body becomes `tool failed`. If the text does not already start with `error:`, prefix `error: `. A non-error result that is empty after strip is `[empty tool result]`. A result that is not an object is `error: tool result was not an object`.

A timeout or a dead process is also `error:` text, using `timed out waiting for tools/call` or the one-line failure from launch. The next call does not reconnect. Once stdout has closed, later calls fail immediately with that same failure text.

## Whitaker

`whitaker-mcp` is a Docker container that exposes Whitaker's Words. The container has to already be running. Foo AI does not attach to the container's own stdin, and it does not use the server's SSE mode. Each Foo AI process runs a new session:

```text
docker exec -i whitaker-mcp python -u /app/whitaker_server.py
```

| Tool | Arguments | Result |
| --- | --- | --- |
| `analyze_latin` | `word` | Morphological analysis of one Latin word. |
| `analyze_latin_batch` | `words` | The same analysis for each word. |
| `analyze_latin_text` | `text` | Tokenises a passage and analyses each unique word. |

`analyze_latin_text` runs the Words binary once per unique word, so a long passage needs a higher `timeout_seconds` than a single lookup. The block above uses 120. Those argument names come from that server's `inputSchema`. Foo AI forwards the schema; it does not define these three tools itself.
