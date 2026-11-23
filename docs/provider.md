# Provider

Foo AI keeps a catalog of named OpenAI-compatible servers and uses one of them for a run. Two names are built in. Both use a context window of `1000000`.

| Name | Address | Model | Extra body |
| --- | --- | --- | --- |
| `home` | `http://127.0.0.1:8100/v1` | `DeepSeek-V4.1-Flash-1017` | `chat_template_kwargs.reasoning_effort = "max"`, `thinking = true` |
| `deepseek-flash_1` | `https://api.deepseek.com/v1` | `deepseek-flash` | `{}` |

`thinking` is a JSON boolean, not the string `"true"`. `home` is the default. Choose another name with `--provider` or `FOO_AI_PROVIDER`:

```sh
foo-ai --provider deepseek-flash_1
```

An unknown name stops the process before a request. Print this on standard error and exit 1. Names are sorted, separated by a comma and a space:

```text
unknown provider 'missing'; known providers: deepseek-flash_1, home
```

Each provider keeps its own API key and extra body. Selecting `deepseek-flash_1` does not send `home`'s key or `chat_template_kwargs`.

The request URL is `base_url` with one trailing slash removed, then `/chat/completions` if that suffix is not already the end of the URL. `https://api.deepseek.com/v1` becomes `https://api.deepseek.com/v1/chat/completions`. A base URL that already ends with `/chat/completions` is used as written.

## Where settings come from

1. The built-in catalog above.
2. `foo-ai.toml` in the current directory, when that file exists. This is the launch directory, which can differ from `--workspace`. Parse it with `tomllib` from the standard library.
3. The selected provider, chosen by the first non-blank value among `--provider`, `FOO_AI_PROVIDER`, and `default_provider`. If none of those is set, the name is `home`.
4. `FOO_AI_BASE_URL` and `FOO_AI_MODEL`, when non-blank, replace the selected provider's fields. If `FOO_AI_API_KEY` is set at all, including as an empty string, it replaces the key. An empty key clears it. These variables do not change `extra_body`.

Inside the file, apply tables in this order:

1. `default_provider`, when it is a non-blank string.
2. Each `[providers.{name}]` table. A name that is not built in starts as a copy of `home`: the same URL, model, context window, empty key, and home's extra body. The table then replaces a field only when that key is present. A non-blank `base_url` or `model` replaces that field. An `api_key` key that is not null replaces the key, and `""` clears it. `context_window` becomes an integer. A bool, a negative number, or any other value that is not an integer, a float, or a digit string becomes `0`. A float is truncated toward zero. Underscores in a digit string are removed, so `"1_000_000"` is `1000000`. When `extra_body` is a table, that object replaces the provider's extra body. It is not deep-merged. Omitting `extra_body` on a new name leaves home's thinking flags in place.
3. A legacy `[provider]` table, if present. Its `name` field selects which provider it updates. With no `name`, it updates the default chosen so far. The same field rules apply. If the file has no `default_provider` key, this name becomes the default. If `default_provider` is present, it stays the selection and the legacy table only updates that named provider.

`foo-ai.example.toml` shows the file shape. Copy it to `foo-ai.toml`. The local file can contain API keys and is gitignored.

```toml
default_provider = "home"

[providers.home]
base_url = "http://127.0.0.1:8100/v1"
model = "DeepSeek-V4.1-Flash-1017"
# context_window = 1000000
# api_key = ""

[providers.home.extra_body.chat_template_kwargs]
reasoning_effort = "max"
thinking = true

[providers.deepseek-flash_1]
base_url = "https://api.deepseek.com/v1"
model = "deepseek-flash"
# api_key = ""
```

`deepseek-flash_1` needs its own `api_key` in the file, or `FOO_AI_API_KEY` for that run. The same file can define MCP servers. See [MCP](mcp.md).

`context_window` is the token limit used in the context line and the limit that drives maintenance. The automatic trigger is 80% of it, `800000` tokens on the built-in window of `1000000`. See [Context](context.md). `0` turns maintenance off and leaves the limit off the line. The same sample round then prints `context 178.1K · usage total 178100 624 · cache 98.62%`.

## Request

Each completion is a streaming `POST` with no socket timeout. The client follows HTTP redirects. Headers are `Content-Type: application/json`, `Accept: text/event-stream`, and, when the API key is non-empty, `Authorization: Bearer {key}`.

Build the JSON object in this order. A later step overwrites an earlier key of the same name. `extra_body` is a shallow update: a nested table replaces that key, it does not merge into it.

1. `model`, `messages`, and `stream: true`.
2. `tools`, only when the caller passed a list. The summary call passes no list, and the key is omitted. An empty list would send `"tools": []`. Normal rounds send the six built-in tools plus MCP tools.
3. Shallow-merge `extra_body`.
4. If `stream_options` is still absent, set it to `{"include_usage": true}`. An extra body that already set `stream_options` wins.

Do not add `temperature` or `max_tokens` unless they arrive inside `extra_body`. A home request therefore has this shape:

```json
{
  "model": "DeepSeek-V4.1-Flash-1017",
  "messages": [],
  "stream": true,
  "tools": [],
  "chat_template_kwargs": {"reasoning_effort": "max", "thinking": true},
  "stream_options": {"include_usage": true}
}
```

HTTP errors raise `ChatError` with `HTTP {status} from {url}: {body}`. Decode the body as UTF-8 with invalid bytes replaced, and keep the first 2,000 characters. A connection failure raises `ChatError` with `cannot reach {url}: {reason}`. The agent loop does not retry either one.

## Response

Prefer a server-sent event stream. If the content type contains `application/json` and does not contain `text/event-stream`, read one JSON body instead and skip the SSE rules.

SSE lines are UTF-8, with invalid bytes replaced. Strip each line. Ignore an empty line and a line that starts with `:`. A line that starts with `data:` continues after that prefix, stripped once. `data: [DONE]` ends the stream. Any other `data:` payload that is not JSON raises `ChatError` with `invalid stream event: {the first 300 characters of the payload}`.

An event whose `error` is an object raises `ChatError` with that object's `message`, or with the object's text when `message` is absent. An `error` that is not an object raises `ChatError` with its text.

From each event, parse `usage` when present. A later event can carry usage with an empty `choices` array; keep that usage. When `choices` is non-empty, read `choices[0].delta`:

| Delta field | Handling |
| --- | --- |
| `reasoning_content`, else `reasoning` | Append it and stream it to the CLI. |
| `content` | Append it and stream it to the CLI. Null or missing content adds nothing. |
| `tool_calls[]` | Join fragments that share `index`. A missing index is 0. |

Tool-call assembly: the latest non-empty `id` replaces the id; `function.name` is concatenated; `function.arguments` is concatenated when it is a string, and a non-string fragment is appended as `json.dumps` of that value. The first time the name becomes non-empty, tell the CLI to print `→` with the name so far. Order the finished calls by the first time each index was seen.

If the stream produced no content, no reasoning, and no tool calls, but it did produce raw lines and none of those lines started with `data:`, join the raw lines with newlines and parse them as one JSON chat body. If that parse fails, raise `ChatError` with `server closed the stream without a completed response`.

A one-shot JSON body uses `choices[0].message`, not `delta`. `content` defaults to `""`. Reasoning is `reasoning_content`, else `reasoning`, else `""`. `tool_calls` defaults to an empty list. A body without `choices[0].message` raises `ChatError` with `unexpected response: ` plus the first 500 characters of the JSON text. Parse `usage` from the top-level object.

The finished assistant message has `content` and `reasoning_content` as strings. Add `tool_calls` only when the list is non-empty. Normalize each call to `type: "function"`, an id or `call_{position}`, and string arguments. The session layer then stores that message, setting `content` to null for a tool turn whose content is empty. See [Session](session.md).

## Usage line

Parse a usage object into prompt tokens, completion tokens, cache hit tokens, and cache miss tokens. A bool counts as 0, not 1. Null counts as 0. An int below 0 counts as 0. A float is truncated toward zero and then clamped at 0. A string of digits, after strip, is an int. Anything else is 0.

- Prompt is `prompt_tokens`. When that count is 0, use `input_tokens + cache_creation_input_tokens + cache_read_input_tokens`.
- Completion is `completion_tokens`. When that count is 0, use `output_tokens`.
- Hit is `prompt_cache_hit_tokens`. When that is 0, use `prompt_tokens_details.cached_tokens` if that details value is an object. When that is also 0, use `cache_read_input_tokens`.
- Miss is `prompt_cache_miss_tokens`. When miss is 0, hit is greater than 0, and prompt is greater than hit, set miss to `prompt - hit`.
- `completion_tokens_details.reasoning_tokens` may be present. It is not printed.

When prompt, completion, hit, and miss are all 0, print no line. Otherwise:

- Compact a token count for the context field only. `0` or below prints `0`. A count of at least `1000000` is divided by `1000000`, printed with one decimal place, with a trailing `.0` removed, then `M`. A count of at least `1000` uses `1000` and `K`. A smaller count is the decimal integer. So `999` is `999`, `142000` is `142K`, `178100` is `178.1K`, `1000000` is `1M`, and `1500000` is `1.5M`.
- When `context_window` is greater than 0, the line starts `context {compact prompt}/{compact window}`. Otherwise it starts `context {compact prompt}`.
- Append ` · usage total {prompt} {completion}` using the raw integers, not the compact form.
- When hit plus miss is greater than 0, append ` · cache {percent}%`. The percent is `hit / (hit + miss) * 100`, formatted with two digits after the decimal.

`prompt_tokens` 178100, `completion_tokens` 624, `prompt_cache_hit_tokens` 175641, and `prompt_cache_miss_tokens` 2459 against a window of 1000000 produce:

```text
context 178.1K/1M · usage total 178100 624 · cache 98.62%
```

The same prompt and completion with `prompt_tokens_details.cached_tokens` of 600 and no explicit miss, against a window of 0, produce `context 1K · usage total 1000 10 · cache 60.00%` when completion is 10 and prompt is 1000. Miss was filled in as `1000 - 600`.
