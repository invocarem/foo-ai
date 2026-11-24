# Skills

A skill is a Markdown playbook. Adding a skill is adding a directory. The tool list gains one tool, `run_skill`, and does not gain another tool when a second skill appears.

`review` ships with the package. A project file with the same name replaces it. Files are read once, after the workspace path is resolved and before the session file is loaded. A skill written during a session is used on the next launch.

## Where files live

Built-in skills are `foo_ai/skills/<name>/SKILL.md` inside the package. Project skills are `.foo-ai/skills/<name>/SKILL.md` inside the workspace. Only that direct child directory is scanned. A nested folder, a flat `<name>.md` file, and any sibling of `SKILL.md` are ignored.

`Path.is_dir` and `Path.is_file` decide what is scanned, so a symlink to a skill directory is included. Directory names are visited in sorted order. Built-in directories are read first. A valid project skill then replaces the built-in with the same name. The catalog is sorted by name.

A directory whose name is not a skill name is skipped. A skill name matches `^[a-z][a-z0-9]*(-[a-z0-9]+)*$` and is 1 to 64 characters. `exit`, `quit`, and `reset` are reserved, because those slash commands are handled first.

## Frontmatter

`SKILL.md` is UTF-8, with invalid bytes replaced. The first line is exactly `---`. A later line that is exactly `---` closes the frontmatter. The body is the remaining lines joined with `\n`, with one leading newline removed when the first body line is empty, then stripped of trailing whitespace. The closed frontmatter is required.

Blank lines in the frontmatter are ignored. A key line is `key: value`, split on the first colon. The key is the stripped left side and matches `^[a-z][a-z0-9-]*$`. The value is the stripped right side. An empty value starts a list: following lines of the form `  - item` (two spaces, a hyphen, one space, then the item, stripped). A list item in any other position, a line without a colon, or a duplicate key makes the file invalid. Unknown keys are ignored, including a list under an unknown key.

| Key | Form | Meaning |
| --- | --- | --- |
| `name` | one line | Required. Equal to the directory name. |
| `description` | one line | Required. The catalog text. Non-empty after strip. |
| `run` | one line | Required. `inline` or `subagent`. |
| `tools` | comma-separated line | Tool names the subagent may call. Omitted on a subagent means `read_file`, `list_dir`, `glob`, `grep`. |
| `shell` | list | Command prefixes. Used only when `tools` includes `shell`. |
| `task` | one line | Default arguments for a slash invocation with an empty remainder. Omitted means there is no default. |

`tools` names one or more of `read_file`, `list_dir`, `glob`, `grep`, `write_file`, `edit_file`, and `shell`, separated by commas. Empty tokens are dropped. An unknown token invalidates the file. Duplicate tokens are ignored. The child sees the kept names in the seven-tool order, not the order written in the file.

`shell` is a list of prefixes. Each prefix is 1 to 200 characters. A prefix that is empty or that contains ``| & ; < > ` $ ( )`` or a newline invalidates the file. When `shell` is one of the tools, the list is required and non-empty. When `shell` is not one of the tools, a shell list is ignored. An inline skill stores these fields and does not use them; an invalid field still rejects the file.

A file that fails these rules is skipped. The built-in of the same name stays in the catalog when the project file is the one that failed. Each skip prints one line on standard error, before the session note:

```text
Skill skipped: .foo-ai/skills/nope/SKILL.md: frontmatter is missing
```

The path uses forward slashes. A built-in path looks like `foo_ai/skills/review/SKILL.md`. The reason is a short sentence with no period:

| Condition | Reason |
| --- | --- |
| Directory name is not a skill name | `invalid skill name` |
| The file cannot be read | `cannot read skill file` |
| The opening `---` is missing | `frontmatter is missing` |
| The closing `---` is missing | `frontmatter is not closed` |
| A frontmatter line is not a key or a list item | `invalid frontmatter line` |
| The same key appears twice | `duplicate frontmatter key` |
| `description`, `name`, `run`, or `task` is a list | `{key} must be a single line` |
| `name` is missing | `name is required` |
| `name` is not equal to the directory | `name does not match the directory` |
| `name` is `exit`, `quit`, or `reset` | `name is reserved` |
| `description` is missing or blank | `description is required` |
| `run` is not `inline` or `subagent` | `run must be inline or subagent` |
| `tools` is a list, or is present and empty | `tools must be a comma-separated line` |
| A tools token is not one of the seven | `unknown tool 'bash'` |
| `shell` has a scalar value | `shell must be a list` |
| A shell prefix is empty | `shell prefix is empty` |
| A shell prefix is longer than 200 characters | `shell prefix is too long` |
| A shell prefix contains a forbidden character | `shell prefix contains a forbidden character` |
| `shell` is allowed and the prefix list is missing or empty | `shell requires a shell list` |
| The body is empty after stripping | `body is empty` |

Checks run in that order. The first failure is the reason.

## Catalog

When at least one skill loaded, the system prompt is the prompt in [Agent loop](agent-loop.md), then a blank line, then one line per skill:

```text
Skills:
- explore (subagent): Investigate repository structure or behavior in a read-only subagent.
- review (subagent): Review the working tree for correctness, security, and missing tests.
```

The line is `- {name} ({run}): {description}`. Names are sorted. The full prompt has no trailing newline. When no skill loaded, the prompt is unchanged and `run_skill` is not offered.

On resume, the stored system message is replaced with that current prompt, catalog included. See [Session](session.md).

The interactive banner, after the `/exit` line, prints one more line when at least one skill loaded:

```text
Skills: /explore, /review.
```

Several names are comma-separated in catalog order: `Skills: /profile, /review.` A one-shot run does not print this line.

## run_skill

`run_skill` is sent after the nine built-in schemas and before the MCP schemas. Its schema is stable for every skill:

```json
{
  "type": "function",
  "function": {
    "name": "run_skill",
    "description": "Run a skill from the Skills list in the system prompt. Pass name as the skill identifier. A subagent skill requires arguments: that text is the only task it sees. An inline skill returns its playbook, with arguments appended when you pass them.",
    "parameters": {
      "type": "object",
      "properties": {
        "name": {"type": "string", "description": "Skill identifier from the Skills list."},
        "arguments": {"type": "string", "description": "Task for a subagent skill, or extra input for an inline skill."}
      },
      "required": ["name"]
    }
  }
}
```

Argument parsing follows [Tools](tools.md). These results are returned before a skill runs:

| Condition | Result |
| --- | --- |
| `name` is missing, blank, or not a string | `error: name is required` |
| `arguments` is present and is not a string | `error: arguments must be a string` |
| `name` is not a loaded skill | `error: unknown skill 'missing'` |
| The skill is `subagent` and `arguments` is missing or blank | `error: arguments are required for subagent skill 'review'` |

The name is stripped before the lookup. It is case-sensitive.

An inline skill returns its body when `arguments` is missing or blank. Otherwise it returns the body, a blank line, and the arguments:

```text
Run the tests that cover the parser.

Arguments:
the parser
```

`Arguments:` is that exact label. The arguments are stripped.

A subagent skill starts a child loop. The result is the child's final answer. `run_skill` counts as one parent tool call. The child's rounds do not count toward `--max-steps`. The result string then follows the usual 20,000-character cap.

A `ChatError` from the child becomes `error: ` plus the error text, and the parent loop continues. `KeyboardInterrupt` still propagates.

## Subagent

The child has its own transcript. Message 0 is the skill body, with no Foo AI prompt and no catalog. The next message is the arguments. The child does not see the parent transcript. Nothing the child stores is written to `.foo-ai/session.jsonl` or to any other file. The parent session gains only the assistant tool call and the `run_skill` tool result.

The child uses the same provider, client, and context fit as the parent. Its context ratio starts at `0.25` and does not read or change the parent's ratio. Fit may rewrite the child transcript in memory. Status lines from that fit, and the child's usage line, are printed on standard error with the skill name inserted after the leading newline:

```text
[review] context 178.1K/1M · usage total 10 2
[review] Compressed old tool results.
```

The child's answer text and reasoning are not printed on their own. With `--verbose`, each child tool prints on standard output:

```text
[review] → read_file
[review] 1|hello
[1 lines shown, 1 total]
```

The name line is a newline, `[{skill}] → {tool}`, and no trailing newline. The result line is a newline, `[{skill}] `, the result, and a newline. Without `--verbose`, child tools print nothing. `--show-thinking` does not print child reasoning.

The child tool list is its allowed names, in seven-tool order. It does not include `run_skill` or MCP tools. The child uses the same step budget as the parent. Its rounds do not reduce the parent's remaining rounds. The closing round in [Agent loop](agent-loop.md) applies after that budget. Reaching the budget with no answer returns the same stop sentence the parent uses, as the tool result, without printing that sentence as parent answer text.

`shell` inside a child does not ask for confirmation and does not print the `$` line. The command is stripped. It runs only when it contains none of the forbidden characters and it equals a prefix or continues with a space after a prefix. `git diff` allows `git diff --stat` and rejects `git difftool`. Any other command returns `error: command is not allowed for this skill` and does not run. An allowed command then follows the parent shell's timeout, output, and exit line.

## Slash commands

In the interactive loop, after `/exit`, `/quit`, and `/reset`, a line whose first token is `/` plus a loaded skill name runs that skill. The remainder of the line, stripped, is the arguments. `/review now` passes `now`. A one-shot prompt is not a slash command; `foo-ai /review` is an ordinary user message.

A subagent slash with an empty remainder uses `task` when the skill has one. `/review` passes `Review the uncommitted changes.` `/explore` has no task, so an empty remainder prints the error below with `explore` in place of `review`. When the remainder and `task` are both empty, print this line on standard error and do not change the session:

```text
error: arguments are required for subagent skill 'review'
```

Otherwise repair the session, save when the repair changed it, save the slash line as the user message, run the child, then save the child's answer as an assistant message with empty reasoning and no tool calls. Print that answer as content. A `ChatError` leaves the user message saved and stores no assistant message. The answer is not capped at 20,000 characters.

An inline slash does not store the slash line. It sends the rendered playbook through the normal `ask` path, so the stored user message is that playbook.

## Shipped skills

`foo_ai/skills/explore/SKILL.md` is the built-in explore. It is a read-only subagent. Its tools are `read_file`, `list_dir`, `glob`, and `grep`. It has no shell list and no task. Its description is `Investigate repository structure or behavior in a read-only subagent.`

The body tells the explorer to answer from the code with `file:line` evidence. It uses `glob` to find files, `grep` to find text, and `list_dir` to see one directory, then reads the files that support the answer. It does not edit files. When a search finds nothing, it names the searches it ran.

`foo_ai/skills/review/SKILL.md` is the built-in review. It is a read-only subagent. Its tools are `read_file`, `list_dir`, `grep`, and `shell`. Its prefixes are `git status`, `git diff`, `git log`, and `git show`. Its task is `Review the uncommitted changes.` Its description is `Review the working tree for correctness, security, and missing tests.`

The body tells the reviewer to inspect the diff and the files it touches, then write the review without searching the rest of the tree. It reports correctness, security, behavior changes, and missing tests with `file:line` evidence, and it does not edit files.
