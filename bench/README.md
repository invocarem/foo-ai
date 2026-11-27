# Benchmark

The model is measured against the tool schemas this program actually sends. One
round per probe: the real system prompt, the real `Toolbox.schemas()`, the user
turn, the calls a probe seeds to stand in for the context phase, and the tool
call that comes back. No agent loop, and no tool is executed, so the fixture
under `bench/workspace` is only there to make the seeded results true.

This is the middle tier of the measurement plan. A generic tool benchmark
answers "is this model good at tool calls" against its own schemas; the agent
benchmark answers "does the agent finish real tasks" with fixtures and a checker.
This answers the question in between, which neither of those can: does the model
pick the right tool from *these* schemas and fill them the way *this* program
reads them.

## Run it

From the repository root, so the launch file is the one the CLI reads:

```sh
python -m bench.run                  # all probes, results under bench/results/
python -m bench.run --dry-run        # the plan, with no client and no token
python -m bench.run --only edit      # the probes whose id contains "edit"
python -m bench.run --provider home  # the same flag the CLI has
python -m bench.run --out run.json   # a chosen report path
```

`--config` names the settings file the page writes, when it is not the default
one. `--workspace` names the workspace whose skills go into the system prompt;
it defaults to `bench/workspace`, the fixture the probes describe, and no tool
runs inside it. `--timeout` is the client's read timeout in seconds, 120 by
default.

The console table is plain ASCII, so a run redirected to a file on Windows still
writes it. The JSON report keeps the model's own text as it came.

```sh
python -m bench.selftest             # the runner's own checks, offline
```

## A probe file

One `*.toml` file per probe, in `bench/probes`, named so the file order is the
report order. The file name without `.toml` is the probe id.

```toml
prompt = """
It returns None for an empty line; make it return 0 instead. Change only that
line.
"""

[[history]]
tool = "read_file"
arguments = { path = "src/app.py" }
result = """\
1|def parse(line):
2|    if line == "":
3|        return None
4|    return int(line)
[4 lines shown, 4 total]\
"""

[expect]
tool = "edit_file"
forbid = ["write_file"]

[expect.args]
path = "src/app.py"
old_string = 're:return\s+None'
new_string = 're:return\s+0'
```

| Key | Meaning |
| --- | --- |
| `prompt` | The one user turn. Required, non-empty. |
| `note` | A line for the reader. `--dry-run` shows it, and it is not scored. |
| `history` | Calls the loop already made, each with the result the tool would have given. |
| `expect.tool` | The tool the turn should call. Required. |
| `expect.forbid` | Tool names that are wrong here even though they look plausible. |
| `expect.args` | One entry per argument that must be there, with its matcher. |
| `expect.types` | One entry per argument that must be there, with its JSON type. |
| `expect.check` | A foo-ai validator to run on the parsed arguments. |

A matcher is the exact text, `*` for any value, or `re:` for a search anywhere
in the text. A number matches its own digits, so `limit = "40"` holds for
`40`. A list or an object never matches a bare string: use `expect.types` for
those. `expect.types` takes `string`, `integer`, `number`, `boolean`, `array`,
and `object`, and a boolean is not an integer.

`expect.check` is how a probe holds a payload to the shape the program can use,
not just its JSON type. `questions` runs `normalize_questions`, the asker's own
validator, so the ask probe fails on a question with no header or one option.

Write regular expressions in TOML literal strings (`'...'`), so the backslashes
arrive as written. Write a `result` as `"""\` … `\"""` so the text is exact: the
backslash trims the newline at each end, and the selftest compares the text with
what the tool returns.

## Seeded history

A probe has one round, so a model that reads before it edits would spend that
round reading. `history` is how a probe starts from the state the loop is in
after the reading: each step becomes the assistant turn that called the tool and
the tool turn that answered it, in order, and the probe's round is the next
turn. A step is `tool`, `arguments`, and `result`, and its result has to be what
the tool would answer for `bench/workspace` — `bench/selftest.py` runs every step
against the real `Toolbox` and fails when a probe and the fixture drift apart.

Seed the whole context phase, not just the first read. This model reads the
file, lists the repository, and greps for callers before it edits, and a probe
that seeds only the read scores a look it did not need as a miss. If a probe
keeps failing on a call that only gathers context, the seed is short, not the
model wrong.

Keep the prompt to what a person would type: the change to make, and the reason
for it. The file text and the repository's state come from the seeded history,
not from a pasted snippet, so the probe measures the call rather than whether the
model trusts what it was told. Say in the prompt when a decision is the user's,
because the asker's validator can only score a payload the model actually sent.

## What a score means

| Score | What happened |
| --- | --- |
| `pass` | The wanted tool, and every check held. |
| `partial` | The wanted tool came, and a check did not hold or a forbidden call came with it. The reasons are listed under the row. |
| `fail` | No tool call, another tool, a forbidden one, or arguments that are not JSON. |

Only the wanted call is scored, wherever it sits in the round: the agent runs
every call of a round in order, so a model that reads and then edits in one round
still made the edit. A forbidden call beside it makes the row a `partial`, since
that call ran too. `calls` in the JSON report says how many came back and
`tools_called` says which ones. The row also carries the arguments of the call
that was named, the round time, and the prompt and completion tokens the server
reported. The console prints those arguments under the row, which is usually the
whole answer to why a probe failed.

## What a row is only comparable against

Every report starts with the treatment, because two runs are different
measurements when any of it moves: the provider and model, the base URL, the
`reasoning_effort` and `thinking` from the provider block, a fingerprint of the
schemas that were sent, the tool names in that set, and the commit. The API key
is not in a report and never goes into one.

The schema fingerprint is what makes a re-run honest: change a tool's
description or a required field, and the next run's rows are a new measurement,
not a regression.

## What it does not measure

- A tool is never executed, so nothing here says whether a call would have
  worked, only that the arguments would be accepted.
- One round after the seeded history. A model that wants more context than the
  seed carries shows up as a fail on the probe that wanted the action, and the
  fix is a longer seed, not a longer probe. The agent loop is the other tier.
- MCP tools are left out, so the schema set does not depend on the MCP servers a
  machine happens to run. Add them here only with a probe that needs one.
- The decision store is not consulted, so the ask probe measures a fresh
  question, not a reopen.

## Rules

1. `foo_ai/` never imports `bench`. The program stays simple; this is a sibling
   tool, like `desktop/`, and `pyproject.toml` packages only `foo_ai*`.
2. `tests/` never imports `bench`. The suite stays offline and deterministic;
   a probe run needs a live endpoint. The runner's own checks live in
   `bench/selftest.py`, which is not a `test_*.py` file on purpose.
3. Run output goes to `bench/results/`, which is ignored by git.

## The probes

| Probe | The mistake it looks for |
| --- | --- |
| `01-read-file` | The control. A failure here means the endpoint or the provider block is wrong. |
| `02-edit-existing` | `write_file` where `edit_file` is right, retyping a file that already exists. |
| `03-edit-unique` | An `old_string` that matches twice, when a second line of context would have matched once. |
| `04-ask-choice` | Guessing a choice the workspace cannot decide, or an `ask` payload the sheet cannot show. |
| `05-run-skill` | Reading the files itself where the `review` skill was named, or a skill call with no arguments. |

A generic benchmark that scores 97 leaves about three points, and they are not
spread evenly: they are the foo-ai-shaped calls. These five aim at those, so a
perfect score here means something, and a miss names the exact habit to fix.
