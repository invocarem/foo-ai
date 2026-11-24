# Context

Every stored assistant message includes `reasoning_content`. Thinking models reject a later request when a tool-call turn is missing that field, so Foo AI keeps it on every assistant message that remains in the transcript. Old reasoning leaves only when that whole earlier turn is replaced by a summary or dropped by a discard.

Before each model round, fit the stored transcript to the context window, then send that transcript. Below the trigger nothing is rewritten: the request is the stored messages, not a shortened copy. Compression, summary, and discard each change the stored session and are saved before the request is sent. A later step in the same fit sees the saved result.

The character size of a transcript is the sum, over its messages, of the character lengths of `content`, `reasoning_content`, `name`, and each tool call's function name and arguments. A missing or null value adds nothing. Do not count roles, tool-call ids, JSON punctuation, or the tools schema.

## Token estimate

Uncalibrated, one token is four characters:

```text
tokens = int(character size × 0.25)
```

`int` truncates toward zero. This is the only estimate used for the trigger, the tail budget, the 400-token summary minimum, and the hard ceiling.

After a normal model round reports usage, replace `0.25` with `prompt_tokens / character size` of the messages that were just sent, when that character size is greater than 0, `prompt_tokens` is greater than 0, and the ratio is strictly inside `(0.05, 2)`. Otherwise keep the ratio already in use. The summary call does not update it. The ratio is process memory: a launch starts at `0.25`, and `/reset` sets it back to `0.25`. Prompt tokens include the tool schemas, so a learned ratio sits a little above the transcript alone and crosses the trigger slightly early.

`context_window` of `0` or below turns this whole procedure off. Send the stored transcript.

A subagent skill fits a separate in-memory transcript with this same procedure. That fit does not write the session file. Its ratio starts at `0.25` and does not read or change the parent conversation's ratio. `/reset` still sets only the parent ratio back to `0.25`. Child status lines are prefixed as [Skills](skills.md) describes.

## Trigger

The only automatic trigger is 80% of the provider context window, the same rule Reasonix uses for `compact_ratio`:

```text
trigger = context_window × 80 // 100
```

When `context_window` is positive and that division is 0, the trigger is 1. For the built-in window of `1000000` the trigger is `800000` tokens. The usage line prints that as `800K`.

The sample round in [Provider](provider.md) reports `178100` input tokens, printed `178.1K/1M`. The room from that sample up to the trigger is `800000 - 178100 = 621900` tokens, printed `621.9K`. That gap is not a second threshold. Maintenance does not run at `621.9K`. It runs when the estimate reaches `800000`.

The hard ceiling is a physical bound, not a second user threshold:

```text
hard = max(1, context_window - 256)
```

For the built-in window that is `999744`. The tail kept verbatim by a summary is 16% of the window:

```text
tail = max(1, context_window × 16 // 100)
```

For the built-in window that is `160000` tokens.

If the estimate is below the trigger, send the stored transcript and stop.

## Compress tool results

When the estimate is at least the trigger, compress every tool result whose `content` is a string longer than 8192 characters. Character here means a Unicode code point, which is what `len` counts on a Python string. The new content is the first 4096 characters, then `[... tool result middle pruned ...]`, then the last 1024 characters. Those two slices do not overlap, because a string has to be longer than 8192 and `4096 + 1024` is 5120. The marker is three periods, copied from Reasonix. It is not the `…` truncation marker.

Other messages stay as they are, including `reasoning_content`. If no tool result changed, do not save and do not print a line. If one changed, save the session and print `Compressed old tool results.` on standard error. Measure again. If the estimate is now below the trigger, send the stored transcript and stop.

The 20,000-character cap applied when a tool runs is unchanged. Compression is an additional cut, and only after the trigger.

## One summary

If the transcript is still at least the trigger, choose the prefix to summarize.

The tail starts as the latest user message through the end. A transcript with no user message, or whose latest user message is the first message after a leading system message with nothing before it, has an empty prefix. A long first request is not summarized.

If the tail's estimate is at most the tail budget, walk backward one message at a time and include it while the tail's estimate stays at most the budget. Then, while the tail starts on a tool message and an earlier message still sits after the system message, include that earlier message too, even when the tail then exceeds the budget. This keeps an assistant tool call with its results.

The prefix is everything after a leading system message and before that tail. If the prefix is empty, or its estimate is under 400 tokens, do not summarize.

Otherwise print `Summarizing earlier turns.` on standard error and make one summary call.

1. If that call raises `ChatError`, print `Summary skipped: {error text}` on standard error. The stored transcript stays as compression left it. Continue to discard.
2. On success, replace the stored prefix with one summary message, save the session, and continue to discard.

Do not summarize again in this fit. The next model round starts this procedure over, so a later round can summarize.

The summary call is one streaming completion on the same provider, with the same extra body, and with no `tools` key. Its text is not streamed to the user and it prints no usage line. The request has two messages:

```json
[
  {"role": "system", "content": "Summarize this coding session for the assistant who will continue it. Record the user's goal, files changed, commands and their results, and unfinished work. Omit reasoning traces. Be concise."},
  {"role": "user", "content": "<transcript built below>"}
]
```

Build that user content from the prefix just chosen, after compression:

1. Shorten every tool result in the prefix at 300 characters, using a newline and `…[snipped, {N} chars total]`. `N` is the length before this cut. This shortening is only the summary request. It is not saved.
2. Drop `reasoning_content`.
3. Render each message on its own, then join the blocks with newlines.
   - A tool message is `[tool {name}] {content}`.
   - An assistant message is `[assistant] {content}`. When it has tool calls, append a newline and `[called {names}]` after non-empty content. When the content is empty, the block is `[assistant] [called {names}]`. Names are comma-separated, and empty names are left out.
   - Any other role is `[{role}] {content}`.
4. If the joined text is longer than 40,000 characters, keep the first 40,000 and append a newline and `…[truncated]`.

A prefix that read a file and then asked to continue renders as:

```text
[user] rename it
[assistant] reading
[called read_file]
[tool read_file] body
```

Store the summary as a user message placed where the prefix was. The system prompt stays message 0. The tail stays as it was. Strip the summary text. If it is empty, use `Earlier turns were removed to fit the context window.` If the stripped text is longer than 8,000 characters, keep the first 8,000 and append a newline and `…[summary truncated]`. The 8,000-character cap does not include the lead-in. The stored content is:

```text
Earlier conversation, summarized:
{stripped summary}
```

The summary model's own assistant message is not stored. Only this user message is. Assistant turns that remain after the replacement still carry their `reasoning_content`.

## Discard

Discard only when the estimate is still at least the hard ceiling. Below that ceiling a failed or skipped summary leaves the compressed transcript in place, and that is what is sent.

The protected suffix is the latest user message through the end. If there is no user message, do not discard. If the system message plus that suffix already estimates at least the trigger, do not discard: dropping earlier turns cannot put this request under the trigger, and the live turn is sent as it is.

Otherwise group the messages after the system message and before the protected suffix. An assistant message with a `tool_calls` list takes the tool messages that follow it immediately. Every other message is its own group. Drop groups from oldest to newest. After the first drop, count the marker below in the estimate. Keep dropping while the estimate is at least the trigger and a group remains.

If nothing was dropped, send the transcript unchanged. If something was dropped, save this user message where the dropped groups were, print `Discarded earlier turns.` on standard error, and send the result:

```json
{"role": "user", "content": "Earlier turns were removed to fit the context window."}
```

Groups that were not dropped stay, in order, after that marker. The protected suffix stays. A result that is still at least the trigger, because the marker itself pushed it over after the last group was dropped, is still saved and sent.
