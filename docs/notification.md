# Notification

The desktop page can tell a phone that a request needs someone. The message
goes to [ntfy.sh](https://ntfy.sh), which delivers it to the ntfy app
subscribed to the same topic. There is no account, no phone number, and no
app of this project's. The CLI does not send a message, and its flags, printed
text, and exit codes stay as they are.

The computer being muted is the reason this exists. The page sends while the
setting is on, including when its window is in front. Focus does not gate it.

## The topic

The topic is the address, and with no signup it is also the password. Anyone
who knows it can publish to it and can read what this page posts: the prompt,
a shell command, a question, or an error. A topic is 1 to 64 characters from
`A-Z`, `a-z`, `0-9`, `_`, and `-`. Anything else is refused on save.

There is no shared default, because a default every install knows is not a
password. When no topic is stored, an enabled save makes one: `foo-ai-` and
three words from the 1296-word list in `foo_ai/gui/words.py`, drawn without
repeats, as in `foo-ai-quiet-otter-lantern`. Three words are about 31 bits,
which is enough that the topic is not found by trying the words, and short
enough to type into the phone by hand.

A blank topic on an enabled save makes a new one, replacing the stored topic.
The field holds the stored topic when the sheet opens, so a blank field is a
deliberate clear: the page then makes a new topic and shows it, so it can be
typed into the ntfy app. A topic typed in the field replaces the stored one.
Turning the setting off keeps the topic, so the phone stays subscribed.
Clearing the field and saving while the setting is off stores an empty topic.

The topic is stored in the settings file and shown in the settings sheet. It
is not written into the transcript, a tool result, or the activity pane.

## The settings file

`[notify]` lives in the page's settings file, beside `[page]`.

```toml
[notify]
enabled = true
topic = "foo-ai-quiet-otter-lantern"
```

A missing file, a missing table, or a value of the wrong shape leaves the
setting off and the topic empty, and a launch still succeeds. Only the TOML
boolean `true` turns it on. A topic that does not match the pattern counts as
empty, so a hand-edited file never stops a launch and a message is not sent
until a valid topic is stored. `enabled` and `topic` are the keys this table
owns. Any other key in the table is left in place. The other tables in the
file are left in place.

The server that sends the message is `https://ntfy.sh`. The page cannot name
another host.

## When a message is sent

One message per moment, and only while `enabled` is true and the stored topic
is valid. The worker does not wait for ntfy.sh. The POST runs on its own
thread, with a 10 second timeout, and it is not retried.

| Moment | Title | Body | Tag |
| --- | --- | --- | --- |
| The request finished with an answer | `Foo AI is ready` | The prompt, or the slash command | `white_check_mark` |
| The request failed | `Foo AI hit an error` | The error text the page also writes | `warning` |
| A shell command is waiting for approval | `Foo AI needs approval` | The command | `hourglass_flowing_sand` |
| `ask` is waiting for an answer | `Foo AI has a question` | Each question's text, joined with ` · ` | `question` |

The body is one line: leading and trailing whitespace is removed, and each
run of whitespace, including a newline, becomes one space. A line longer than
160 characters is cut to its first 159 characters, trailing spaces on that cut
are removed, and `…` is added. An empty line sends the title as the body, so
the phone still has a message.

A request that was interrupted sends nothing for that ending. An approval or
a question that already sent keeps that message. Auto-approve runs the command
without waiting, and it sends nothing. Reloading the page does not send the
waiting message again: the send happens once, when the wait starts, not when
the page paints it.

`Send test` is the other send. It uses the stored settings, waits for ntfy.sh,
and posts the title `Foo AI`, the body `Foo AI can reach this phone.`, and the
tag `white_check_mark`. The sheet saves the form first, so the test matches
the topic just written. While the setting is off the test is refused with
`notification is off`. A stored setting that is on with an empty topic is
refused with `notification has no topic`.

## The POST

```text
POST https://ntfy.sh/<topic>
Title: Foo AI is ready
Priority: high
Tags: white_check_mark
Content-Type: text/plain; charset=utf-8

summarize the parser
```

The body is the raw UTF-8 message, not JSON. `Priority` is `high`. A redirect
is a failure: the message is not sent on to the new address, and the failure
names the status code.

A failure does not fail the request. The activity pane gets one `notice`:

| Failure | Notice |
| --- | --- |
| HTTP status, including a redirect | `notification failed: HTTP <code>` |
| The connection, the timeout, or any other socket error | `notification failed: the notification server did not answer` |
| Any other error from the send | `notification failed` |

The notice does not contain the topic. A successful send writes nothing in
the transcript or the activity pane. The test's failure is the JSON error on
`POST /api/notify/test`, in those same words, and it is not also a notice.

## The sheet

Settings gains a **Notification** block under the provider: a `notify me`
checkbox, the topic, `Save notification`, and `Send test`. The topic field's
placeholder is `leave blank to make one`. Saving with the field blank makes a
three-word topic and shows it in the field, so it can be typed into the app.
`Enter` in the topic field saves it. `Close` writes nothing. The block can be
saved while a request runs. The turn options in the stage head are unchanged.

## The HTTP API

Both writes are `POST` with `Content-Type: application/json`, and the guards
on the page's other writes apply.

| Route | Body | Result |
| --- | --- | --- |
| `POST /api/notify` | `{"enabled": true, "topic": "foo-abc"}` | Writes `[notify]` and answers `{"ok": true, "enabled": true, "topic": "foo-abc"}`. An empty `topic` while enabled answers with a new three-word one. |
| `POST /api/notify/test` | `{}` | Sends the test message and answers `{"ok": true}`. |

`enabled` has to be a boolean, and `topic` has to be a string. A missing
`topic` is an empty string, which while enabled makes a new three-word one. A
topic that is not empty and does not match the
pattern is refused with `topic must be 1 to 64 letters, digits, underscores, or dashes`,
and the file is left as it was. `enabled` that is not a boolean is refused
with `enabled must be true or false`. `topic` that is not a string is refused
with `topic must be a string`. Without a settings file the save is refused
with `there is no settings file to write`.

`GET /api/state` carries the stored values as `notify`, with `enabled` and
`topic`. The page reads the topic back, because the phone has to be subscribed
to it.
