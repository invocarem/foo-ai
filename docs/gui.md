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
| `--workspace DIR` | Directory the tools may read and write. Without it the page opens the folder it opened last, and the current directory is the last resort. A flag is not written, so it names the folder for this run only. |
| `--provider NAME` | Named provider from `foo-ai.toml`. The page can switch it while idle. |
| `--config PATH` | The settings file the page reads last and writes. Defaults to the user's configuration directory: `%APPDATA%\foo-ai\foo-ai.toml` on Windows, `$XDG_CONFIG_HOME/foo-ai/foo-ai.toml` or `~/.config/foo-ai/foo-ai.toml` elsewhere. `FOO_AI_CONFIG_DIR` names that directory instead. |
| `--max-steps N` | Model rounds for one request. Overrides both config files, as in the CLI. |
| `--show-thinking` | Start with reasoning shown. The page can toggle it. |
| `--verbose` | Start with every tool result kept in full. The card stays folded. The page can toggle it. |
| `--yes` | Run shell commands without asking. The page can toggle it. |
| `--host ADDR` | Address to serve on. Defaults to `127.0.0.1`. |
| `--port PORT` | Port to serve on. `0` asks the system for a free one, which is the default. |
| `--read-timeout SECONDS` | Give up on a round that sends nothing for this long. Defaults to 300; `0` waits forever. |
| `--no-browser` | Do not open the page in a browser. |
| `--version` | Print `Foo AI 0.1.0` and exit 0. |

A configuration error prints its message on standard error and exits 1 before
anything is served, exactly as in the CLI.

## What the page shows

The page has a top bar, a left pane, the stage, and a right pane.

- **Top bar**: the two panel buttons, the brand and its prompt mark, the context
  meter for the last round, and the status light: `ready`, `working`,
  `approval`, `question`, or `error`. Each panel button draws a panel with its
  outer edge marked: the left one hides the left pane, the right one hides the
  activity pane. The stage keeps its room either way.
- **Left pane**: the open workspace in the path field, with a folder picker under
  it, the provider name with its base URL and window, the task list, the loaded
  skills, the session file, its message count, the step budget, and the MCP
  lines from startup. A skill button fills the composer with its slash command.
- **Stage**: the transcript and the composer. The head shows the folder's name,
  the provider, and the model, plus `thinking`, `verbose`, and `auto-approve`.
  The full path stays in the workspace field. An answer streams into one card
  per block; each tool call is a card whose result stays folded. Reasoning sits in
  a folded `thinking` block, and is only sent while `thinking` is on. A shell
  command opens the amber approval bar with `Deny` and `Run`.
- **Right pane**: the activity log. Skill skips, the session note, the MCP
  lines, the context line of every round, tool names, answers, and errors.

`Browse…` under the workspace field opens a picker: the folders of one
directory, the path it is listing, a button up to the parent folder, and a
`Use this folder` button under the list, which fills the path field. `Open` then
loads that folder, so a mis-click in the list cannot move the session. The
picker starts at the path in the field, or at the open workspace when the field
is empty. A file path names its own folder, dot folders such as `.git` are not
listed, and the top of a drive on Windows offers the other drives, so the picker
can leave the one it started on. `Esc`, or a second click on `Browse…`, closes
it.

The task list is the one `todo_write` wrote: the pane shows `2/5 done` with a
progress bar, the item in progress in the warning colour, and finished items
struck through. It stays after the answer so the work can be read, and the next
request clears it. Opening the page again paints the last successful list from
the session file, including a list the previous process wrote.

`ask` opens a popup sheet, because the request waits for the answer: one card
per question, with its header, the question, its options, and a line for the
user's own answer. A single-choice question takes one radio button, `pick any`
takes checkboxes, and each option carries its description. Text in that line
replaces the radio selection, and on `pick any` it is added to the checks.
`Send answer` posts the picks; `Dismiss`, `Esc`, or the close button posts no
choice, which tells the model to decide nothing and wait. A question that is
still waiting reopens if the page is reloaded.

`Enter` sends and `Shift+Enter` adds a line. `Ctrl+,` opens settings, `[` and `]`
collapse the side panes, and `Esc` dismisses a question or closes a sheet.
`/reset` archives the conversation, `/exit` closes the window, and a loaded
skill runs as `/name arguments`. Any other line, including one that begins with
`/`, is a user message, as in the CLI.

Settings is a sheet for the provider the page is on. **Provider** edits
its name, base URL, model, context window, and API key. `Save provider` writes
that provider to the settings file named in the section, switches to it, and
closes the sheet; `Enter` in one of its fields does the same. `Close` dismisses
the sheet without writing. When that
provider's table has no `extra_body`, the save also writes the one the request
already sends, so `home` keeps this block in the file:

```toml
[providers.home.extra_body.chat_template_kwargs]
reasoning_effort = "max"
thinking = true
```

A provider whose body carries `chat_template_kwargs` also shows a **Local
reasoning** block in that section, with `reasoning effort` and `thinking`, and
the save writes those two keys into the block. Those are the keys the sheet
owns: a blank `reasoning effort` drops that key, so the block can send
`thinking` alone, and any other key of the block passes through as it is. The
fields are hidden for a provider whose body carries no such block, which is
`deepseek-flash_1` and every other cloud provider, and a save that posts them
for one is refused.

The stage head's `thinking`, `verbose`, and `auto-approve` checkboxes are the
turn options. Each one is written to the settings file as it changes, so they
are still on after a restart; `--show-thinking`, `--verbose`, and `--yes` turn
one on for the run that names it. The step budget is shown under Session in the
left pane.

A table that already has `extra_body` keeps it, apart from the block the sheet
edited: a reasoning save writes the whole body the request sends, so a body the
launch file set keeps its other keys. The key is
write-only: the page never reads it back, and the field only says whether one is
stored. Leaving the key blank keeps the stored one, and `clear the stored key`
removes it. The workspace is not in the sheet: the left pane opens a folder, and
that is the only place it changes.

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
| `GET /api/state` | Workspace, provider, providers, the settings file, step budget, skills, MCP lines, session facts, toggles, and the last context line. The provider carries `api_key_set`, never the key, and `chat_template_kwargs`, or `null` for a provider that sends no local reasoning block. |
| `GET /api/history` | The resumed transcript, one object per stored message. |
| `GET /api/events?after=N` | The events after sequence `N`, the status, the pending command, and the last context line. |
| `GET /api/browse?path=DIR` | The folders under one directory, for the workspace picker: `{"ok": true, "path": "...", "parent": "..." or null, "entries": [{"name": "...", "path": "..."}]}`. No `path` reads the open workspace, a file path names its own folder, and a path that is not there answers `{"ok": false, "error": "no such folder: ..."}`. |
| `POST /api/ask` | `{"text": "..."}` starts one request. |
| `POST /api/slash` | `{"command": "/review focus"}` runs a loaded skill. |
| `POST /api/reset` | Archives the session file and starts a new conversation. |
| `POST /api/cancel` | Stops the running request. |
| `POST /api/approve` | `{"approve": true}` answers a waiting shell command. |
| `POST /api/settings` | `{"thinking": ..., "verbose": ..., "yes": ...}` sets the toggles. |
| `POST /api/provider` | `{"name": "..."}` switches provider while idle. |
| `POST /api/provider/save` | `{"name": "...", "base_url": "...", "model": "...", "context_window": N, "api_key": "...", "chat_template_kwargs": {"reasoning_effort": "...", "thinking": true}}` writes that provider to the settings file and switches to it. An absent `api_key` keeps the stored key, and `""` clears it. An absent `chat_template_kwargs` keeps the body as it is, and a provider that sends none refuses one. |
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
`todos` in `GET /api/state`. The next request starts by clearing it. A new
process reads the last successful `todo_write` result out of the session file
and returns that list from `GET /api/state` before the next request.

Switching provider rebuilds the client and keeps the transcript. Switching
workspace rebuilds everything for that folder: its `.foo-ai.toml`, its skills,
and its session file, and it writes that folder to the settings file, so the next
launch opens it. Both are refused while a request runs. Saving a provider
writes the settings file and then switches to it, so it is refused in the same
way.

The settings file is a `foo-ai.toml` the page owns. It is read after the launch
file, so the values the sheet shows are the values a request sends, and it is
written whole and atomically, private to the user where the platform has file
modes. It carries `workspace` for the folder the page opened last,
`default_provider`, `[providers.*]`, and `[page]` for the turn
options, and nothing else: MCP servers and the step budget belong to the launch
file and the workspace. A packaged app needs it because the install directory has
no `foo-ai.toml` of its own, and a user cannot write to that directory anyway.

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
  no path traversal.
- `GET /api/browse` lists the folders of one directory, because the workspace
  picker needs them. It is a read for the page, so the `Host` check guards it,
  and it changes nothing the agent may read or write: the tools stay inside the
  open workspace.

## The desktop window

`desktop/` is an Electron shell around the page.
It starts `python -m foo_ai.gui --no-browser --host 127.0.0.1 --port <free>`,
waits for the `foo-ai-desktop:` line, and shows that address in a window with a
hidden title bar in the page's own colour. Closing the window stops the server.

```sh
cd desktop
npm install

# Open the desktop window.
npm start

# Build for this machine: an NSIS installer on Windows, an AppImage on Linux.
npm run dist

# Build the Windows NSIS installer.
npm run dist:win

# Build the Linux AppImage.
npm run dist:linux
```

The build copies `foo_ai/` beside the executable. `desktop/node_modules` and
`desktop/release` are not committed.

On Linux, Electron's user-namespace sandbox is often blocked by AppArmor
(`kernel.apparmor_restrict_unprivileged_userns = 1`). `npm start` then refuses
to open and exits with `SIGTRAP` until `chrome-sandbox` is owned by root and
mode `4755`. From `desktop/`:

```sh
sudo chown root:root node_modules/electron/dist/chrome-sandbox
sudo chmod 4755 node_modules/electron/dist/chrome-sandbox
npm start
```

`npm install` replaces that file, so the `chown` and `chmod` have to be run
again after each install.

`npm run dist` and `npm run dist:linux` write
`release/Foo AI-0.1.0.AppImage`. The image needs FUSE 2 (`libfuse.so.2`):
`libfuse2t64` on current Ubuntu, `libfuse2` on older releases. The
`chrome-sandbox` inside the image stays owned by the user, so start it with
`--no-sandbox`:

```sh
sudo apt install libfuse2t64
./"Foo AI-0.1.0.AppImage" --no-sandbox
```

When `libfuse.so.2` is absent, the same file extracts itself and runs:

```sh
./"Foo AI-0.1.0.AppImage" --appimage-extract-and-run --no-sandbox
```

The app icon is `desktop/build/icon.svg`: the page's prompt mark, the name's F
and the prompt's underscore in two cells, in the page's blue-to-violet gradient
on the window's dark plate. Two cells and not three, because at 16 pixels a
third cell leaves each stroke thinner than a pixel. The window hands the ICO to
Electron, and the installer build embeds it in `FooAI.exe`, in the installer,
and in its shortcuts. `signExecutable: false`
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
