# Desktop page

The CLI stays the program described in [the specification](README.md). The
desktop page is an addition beside it: one HTML document served on the loopback
interface, with the same agent, the same transcript file, and the same tools
behind it. Nothing in the CLI changes, and the Python program still imports only
the standard library.

```sh
python -m foo_ai.gui
```

That command reads the provider and the workspace the way `python -m foo_ai`
does, prints one line

```text
foo-ai-desktop: http://127.0.0.1:8765/
```

and opens that address in a browser. `--no-browser` skips the browser, which is
what the desktop shell uses. After `pip install -e .`, `foo-ai-desktop` is the
same command.

## Flags

| Flag | Effect |
| --- | --- |
| `--workspace DIR` | Directory the tools may read and write. Defaults to the current directory. |
| `--provider NAME` | Named provider from `foo-ai.toml`. The page can switch it while idle. |
| `--max-steps N` | Model rounds for one request. Overrides both config files, as in the CLI. |
| `--show-thinking` | Start with reasoning shown. The page can toggle it. |
| `--verbose` | Start with every tool result shown in full. The page can toggle it. |
| `--yes` | Run shell commands without asking. The page can toggle it. |
| `--host ADDR` | Address to serve on. Defaults to `127.0.0.1`. |
| `--port PORT` | Port to serve on. `0` asks the system for a free one, which is the default. |
| `--read-timeout SECONDS` | Give up on a round that sends nothing for this long. Defaults to 300; `0` waits forever. |
| `--no-browser` | Do not open the page in a browser. |
| `--version` | Print `Foo AI 0.1.0` and exit 0. |

A configuration error prints its message on standard error and exits 1 before
anything is served, exactly as in the CLI.

## What the page shows

The page has the three panes of Burst, with the same tokens and component
shapes: a top bar, a left pane, the stage, and a right pane.

- **Top bar**: the brand, the context meter for the last round, and the status
  light: `ready`, `working`, `approval`, `question`, or `error`.
- **Left pane**: the workspace and its step budget, the provider and its model,
  base URL and window, the task list, the loaded skills, the session file and
  its message count, and the MCP lines from startup. A skill button fills the
  composer with its slash command.
- **Stage**: the transcript and the composer. An answer streams into one card
  per block; each tool call is a card whose result folds open. Reasoning sits in
  a folded `thinking` block, and is only sent while `thinking` is on. A shell
  command opens the amber approval bar with `Deny` and `Run`.
- **Right pane**: the activity log. Skill skips, the session note, the MCP
  lines, the context line of every round, tool names, answers, and errors.

The task list is the one `todo_write` wrote: the pane shows `2/5 done` with a
progress bar, the item in progress in the warning colour, and finished items
struck through. It stays after the answer so the work can be read, and the next
request clears it.

`ask` opens a popup sheet, because the request waits for the answer: one card
per question, with its header, the question, and its options. A single-choice
question takes one radio button, `pick any` takes checkboxes, and each option
carries its description. `Send answer` posts the picks; `Dismiss`, `Esc`, or the
close button posts no choice, which tells the model to decide nothing and wait.
A question that is still waiting reopens if the page is reloaded.

`Enter` sends and `Shift+Enter` adds a line. `Ctrl+,` opens settings, `[` and `]`
collapse the side panes, and `Esc` dismisses a question or closes a sheet.
`/reset` archives the conversation, `/exit` closes the window, and a loaded
skill runs as `/name arguments`. Any other line, including one that begins with
`/`, is a user message, as in the CLI.

## While a request runs

One request runs at a time. The status light reads `working` and carries the time
since the model last sent something, so a round that has gone quiet reads
`working · 4:12` instead of an unchanged word. `Stop` interrupts it, and `Send`
refuses a second prompt with a toast that says so; the typed text stays in the
composer, so nothing is lost.

`reasoning_content` is not streamed unless `thinking` is on, so a long thinking
phase looks like a silent round. Turn `thinking` on in the stage head to watch it
arrive.

A round that sends nothing for `--read-timeout` seconds fails with a `ChatError`
instead of waiting forever: the page writes that error into the transcript and
the activity pane, and the status returns to `ready`. A model server that is
down, or one that has dropped a connection without closing it, therefore shows
up as an error rather than a request that never ends.

## The HTTP API

The page is a document plus a small JSON API. Every write is a `POST` with
`Content-Type: application/json`.

| Route | Purpose |
| --- | --- |
| `GET /` | The document. |
| `GET /static/<file>` | `styles.css`, `app.js`, and `favicon.svg` from the package. |
| `GET /api/state` | Workspace, provider, providers, step budget, skills, MCP lines, session facts, toggles, and the last context line. |
| `GET /api/history` | The resumed transcript, one object per stored message. |
| `GET /api/events?after=N` | The events after sequence `N`, the status, the pending command, and the last context line. |
| `POST /api/ask` | `{"text": "..."}` starts one request. |
| `POST /api/slash` | `{"command": "/review focus"}` runs a loaded skill. |
| `POST /api/reset` | Archives the session file and starts a new conversation. |
| `POST /api/cancel` | Stops the running request. |
| `POST /api/approve` | `{"approve": true}` answers a waiting shell command. |
| `POST /api/settings` | `{"thinking": ..., "verbose": ..., "yes": ...}` sets the toggles. |
| `POST /api/provider` | `{"name": "..."}` switches provider while idle. |
| `POST /api/workspace` | `{"path": "..."}` loads another workspace while idle. |
| `POST /api/answer` | `{"answers": [{"id": "...", "selected": ["..."]}]}` answers a waiting `ask` question. |

A refused request answers `{"ok": false, "error": "..."}`. The page polls
`/api/events` every 200 ms while a request runs and every 900 ms when idle.

## Events

| Kind | Meaning |
| --- | --- |
| `user` | The prompt that started the request. |
| `start`, `done` | The request began, and the worker finished. |
| `content` | A chunk of the answer. |
| `reasoning` | A chunk of `reasoning_content`, only while `thinking` is on. |
| `tool_name` | A tool call started. |
| `tool_result` | A tool result, with `name`, `chars`, and `full`. |
| `status` | A status line, including the context line of a round. |
| `notice` | A startup line: skill skips, the session note, MCP lines, the banner. |
| `confirm`, `confirm_done` | A shell command waits for an answer, and it was answered. |
| `ask`, `ask_done` | The `ask` tool waits for a choice, and it was answered. |
| `todos` | The task list `todo_write` wrote, as `content` and `status` pairs. |
| `error` | A `ChatError` or another failure. |
| `reset` | The conversation was archived. |

## The hub

One worker thread runs a request, so the server keeps answering while the model
streams. The sink turns each stream event into an entry in the log; it is also
where `Stop` takes effect, because the next chunk raises and the round ends the
way Ctrl-C ends one in the CLI. A stopped or failed request is not retried, and
the next request repairs the tool calls that never got a result, as in
[Session](session.md).

A shell command blocks the worker on a `threading.Event` until the page answers.
`Deny`, `Stop`, and a cancelled request all resolve it with `false`, and the
tool then returns `error: command not approved`.

`ask` blocks the same way. While it waits, the status is `asking` and
`GET /api/events` repeats the questions as `question`; `POST /api/answer` posts
`{"answers": [{"id": "q1", "selected": ["Label"]}]}`. An empty list is a
dismissal: the tool returns the dismiss sentence and the turn ends without
another model round. `Stop` cancels instead, which ends the turn as interrupted.
Auto-approve does not answer a question, because the choice is the user's.

`todo_write` publishes the list the moment it runs, as a `todos` event and as
`todos` in `GET /api/state`. The next request starts by clearing it.

Switching provider rebuilds the client and keeps the transcript. Switching
workspace rebuilds everything for that folder: its `.foo-ai.toml`, its skills,
and its session file. Both are refused while a request runs.

## Security

The page can run shell commands, so the server is narrow on purpose.

- It binds `127.0.0.1` by default and warns when `--host` names anything else.
  There is no password, so an off-loopback address is the operator's choice.
- Every request must carry the `Host` the server was reached by. A page that
  rebinds DNS to the loopback address fails this check.
- Every `POST` must carry `Content-Type: application/json` and, when a browser
  sends one, an `Origin` equal to that host. A page of another origin cannot
  drive the agent.
- `GET /static/<file>` serves one file name in the package directory. There is
  no path traversal and no directory listing.

## The desktop window

`desktop/` is an Electron shell around the page, the way Burst wraps its viewer.
It starts `python -m foo_ai.gui --no-browser --host 127.0.0.1 --port <free>`,
waits for the `foo-ai-desktop:` line, and shows that address in a window with a
hidden title bar in the page's own colour. Closing the window stops the server.

```sh
cd desktop
npm install
npm start
```

`npm run dist` builds for the machine you are on: an AppImage on Linux, an NSIS
installer on Windows. `npm run dist:win` still builds that Windows installer.
The build copies `foo_ai/` beside the executable. `desktop/node_modules` and
`desktop/release` are not committed.

The app icon is `desktop/build/icon.svg`: the prompt chevron and the cursor from
the page's favicon, in the page's blue-to-violet gradient on the window's dark
plate. The window hands the ICO to Electron, and the installer build embeds it
in `FooAI.exe`, in the installer, and in its shortcuts. `signExecutable: false`
skips the signature only; the icon and the version fields are still written.
`icon.png` (512×512) and a multi-size `icon.ico` are rendered from the SVG and
committed with it, so a build needs no image tool; changing the drawing means
re-rendering both with ImageMagick:

```sh
magick -background none desktop/build/icon.svg -resize 512x512 PNG32:desktop/build/icon.png
magick desktop/build/icon.png -define icon:auto-resize=256,128,64,48,32,24,16 desktop/build/icon.ico
```

| Variable | Effect |
| --- | --- |
| `FOO_AI_PYTHON` | Interpreter to start. Otherwise the repository's `.venv`, otherwise `python`. |
| `FOO_AI_ROOT` | Repository root, when `main.js` is not sitting in `desktop/`. |

The window's menu opens a workspace folder, starts a new conversation, opens
settings, and shows the version. Those three actions reach the page through the
preload bridge, which only adds the desktop class and those handlers; the page
keeps no Node access.

Electron is a development dependency of the shell, not a dependency of the
Python program. `python -m foo_ai.gui` needs no Node at all, and the browser is
then the window.

## Tests

```sh
python -m unittest tests.test_gui
```

The suite runs the real server on a free port with a scripted client, so it
needs no model server: it covers the context line, the provider list, the
static files, every API route, the approval and cancel paths, the guards, and
the command line.
