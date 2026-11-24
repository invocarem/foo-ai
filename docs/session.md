# Session

The conversation is stored at `.foo-ai/session.jsonl` inside the workspace. `grep` does not descend into `.foo-ai`. `list_dir` still shows that directory when it is asked to list the parent.

Each non-blank line is one JSON object. Write the file as UTF-8 with `\n` line endings, `json.dumps` of each message with non-ASCII kept as characters, and a newline after the last line. Write `.foo-ai/session.jsonl.tmp` first, then replace `session.jsonl` with it. A crash during the temp write leaves the previous transcript in place. The reader opens only `session.jsonl` and ignores a leftover temp file. Create `.foo-ai` on the first save.

The file is created on the first user message. A transcript that is only the system prompt is not written. If a save finds one message or zero, and the file exists, delete the file. After each user message, assistant message, tool result, compression, summary, and discard, the whole file is rewritten.

## Message shapes

Key order does not matter on load. These are the objects the agent writes.

System, always message 0 while the process is running. The content is the current system prompt, including the skill catalog when skills are loaded:

```json
{"role": "system", "content": "You are Foo AI, a coding agent.\n..."}
```

A subagent skill keeps its transcript in memory. Those messages are not appended here and are not written to another file. The parent stores the `run_skill` tool result. A slash invocation of a subagent stores the slash line as a user message and the child's answer as an assistant message. See [Skills](skills.md).

User:

```json
{"role": "user", "content": "the stripped user text"}
```

Assistant, final answer. `content` is a string, `""` when the server sent none. `reasoning_content` is always present, `""` when the server sent none. There is no `tool_calls` key.

```json
{"role": "assistant", "content": "Done.", "reasoning_content": ""}
```

Assistant, tool turn. `content` is JSON `null` when the server's content was empty or null. `tool_calls` is present only when there is at least one call. Each call's `arguments` stays the argument characters the server sent, as a string, and is not parsed and written again. A missing id becomes `call_0`, `call_1`, ... from the call's position in this message. `type` is `function`.

```json
{
  "role": "assistant",
  "content": null,
  "reasoning_content": "We need to read the file.",
  "tool_calls": [
    {
      "id": "call_0",
      "type": "function",
      "function": {"name": "read_file", "arguments": "{\"path\": \"a.py\"}"}
    }
  ]
}
```

While streaming, a non-string arguments fragment is appended with `json.dumps` of that fragment and the default separators. Fragments that share an index form one call. The id is replaced by the latest non-empty id fragment, not concatenated. The name and the arguments string are concatenated. See [Provider](provider.md).

Tool result. `name` is the function name. The synthetic repair result uses `""`.

```json
{"role": "tool", "tool_call_id": "call_0", "name": "read_file", "content": "1|hello\n[1 lines shown, 1 total]"}
```

Summary, stored as a user message. See [Context](context.md). Compression rewrites a long tool result's `content` in place. A discard inserts one user message, `Earlier turns were removed to fit the context window.`, where the dropped turns were.

```json
{"role": "user", "content": "Earlier conversation, summarized:\nRenamed the function."}
```

## Resume

The next launch reads the file. Skip blank lines. If any other line is not a JSON object, or its `role` is not a string, the file is unreadable. Delete an existing `.foo-ai/session.jsonl.broken` first, then rename the bad file to that name. The launch starts a new conversation and the note is:

```text
Session file was unreadable and was renamed to .foo-ai/session.jsonl.broken.
```

An empty file is deleted and produces no note. A missing file produces no note and starts at the system prompt.

Replace a leading system message with the current system prompt, dropping any extra keys on that stored message, so a prompt edit applies without dropping the conversation. If the first message is not a system message, insert the current system prompt at the front.

Then close unpaired tool calls in memory. The startup line counts every message except the system message, including those repair results:

```text
Resumed .foo-ai/session.jsonl (12 messages).
```

The repair is written to disk when the next request is saved. Quitting without sending a message leaves the previous file unchanged.

## Reset

`/reset` moves the active file to `.foo-ai/session-YYYYMMDD-HHMMSS.jsonl`, using local time. If that name exists, use `-2`, then `-3`, and so on: `session-20261123-140000-2.jsonl`. The in-memory conversation becomes a new system prompt only. No new session file is created until the next user message.

With an archived file, print `Conversation cleared. Archived .foo-ai/session-YYYYMMDD-HHMMSS.jsonl.` Use forward slashes in that relative path. With no session file, print `Conversation cleared.`

## Interrupted tool calls

A tool call has to be followed by a tool result with the same `tool_call_id` before the next non-tool message. Ctrl-C during a shell confirmation, or a crash after the assistant turn was saved, can leave the call unpaired.

Walk the messages. An assistant message sets the pending ids to its tool-call ids, as strings, using `""` when an id is missing. A following tool message consumes the matching id. If its `tool_call_id` is not pending, it consumes the oldest pending id instead. When a non-tool message arrives while ids are still pending, insert one repair result per pending id before that message. Do the same for ids still pending at the end.

Each repair result is:

```json
{"role": "tool", "tool_call_id": "call_0", "name": "", "content": "error: interrupted before the tool result was saved"}
```

`ask` runs this repair before it appends the new user message. If the in-memory list changes, it saves that list before appending the user text. Load also runs the repair in memory, so a launch that then sends a message writes the repair and the new user message together.
