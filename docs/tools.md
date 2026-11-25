# Tools

The model sees nine built-in tools: `read_file`, `list_dir`, `glob`, `grep`, `write_file`, `edit_file`, `shell`, `todo_write`, and `ask`. When at least one skill is loaded, `run_skill` follows those nine. Its schema and result text are in [Skills](skills.md). MCP servers add more after that; see [MCP](mcp.md). Send them as an OpenAI tool list. `list_dir` has no `required` array. The other eight built-in tools do. A skill subagent still sees only the workspace tools named in its file, which are chosen from the first seven. See [Skills](skills.md).

```json
[
  {
    "type": "function",
    "function": {
      "name": "read_file",
      "description": "Read a UTF-8 text file in the workspace. Lines are numbered from 1. Use offset and limit to page through long files.",
      "parameters": {
        "type": "object",
        "properties": {
          "path": {"type": "string", "description": "File path relative to the workspace."},
          "offset": {"type": "integer", "minimum": 1, "description": "1-based first line to return. Default 1."},
          "limit": {"type": "integer", "minimum": 1, "description": "Maximum lines to return. Default 400."}
        },
        "required": ["path"]
      }
    }
  },
  {
    "type": "function",
    "function": {
      "name": "list_dir",
      "description": "List entries in a workspace directory. Pass '.' for the workspace root.",
      "parameters": {
        "type": "object",
        "properties": {
          "path": {"type": "string", "description": "Directory relative to the workspace. Default '.'."}
        }
      }
    }
  },
  {
    "type": "function",
    "function": {
      "name": "glob",
      "description": "Find files matching a glob pattern. * and ? match inside one path segment, and ** crosses directories. A pattern with no / matches the start directory, then the whole tree only when that directory has no match. Use **/*.py to search every level. Results are workspace-relative paths using /.",
      "parameters": {
        "type": "object",
        "properties": {
          "pattern": {"type": "string", "description": "Glob pattern, with / as the separator."},
          "path": {"type": "string", "description": "Directory to search. Default workspace root."}
        },
        "required": ["pattern"]
      }
    }
  },
  {
    "type": "function",
    "function": {
      "name": "grep",
      "description": "Search workspace text files for a Python regular expression. Returns path:line:text matches.",
      "parameters": {
        "type": "object",
        "properties": {
          "pattern": {"type": "string", "description": "Regular expression."},
          "path": {"type": "string", "description": "File or directory to search. Default workspace root."}
        },
        "required": ["pattern"]
      }
    }
  },
  {
    "type": "function",
    "function": {
      "name": "write_file",
      "description": "Create a new UTF-8 text file inside the workspace. Fails if the file already exists; use edit_file to change an existing file.",
      "parameters": {
        "type": "object",
        "properties": {
          "path": {"type": "string", "description": "File path relative to the workspace."},
          "content": {"type": "string", "description": "Full file contents."}
        },
        "required": ["path", "content"]
      }
    }
  },
  {
    "type": "function",
    "function": {
      "name": "edit_file",
      "description": "Replace one exact occurrence of old_string with new_string in an existing file. old_string is raw file text, without the line-number prefix shown by read_file. Fails if old_string is missing or matches more than once; add surrounding lines to disambiguate.",
      "parameters": {
        "type": "object",
        "properties": {
          "path": {"type": "string", "description": "File path relative to the workspace."},
          "old_string": {"type": "string", "description": "Exact text to replace. Must match once."},
          "new_string": {"type": "string", "description": "Replacement text. An empty string deletes the match."}
        },
        "required": ["path", "old_string", "new_string"]
      }
    }
  },
  {
    "type": "function",
    "function": {
      "name": "shell",
      "description": "Run a command in the workspace and return combined stdout and stderr. The user must approve the command before it runs. Prefer read_file, list_dir, glob, grep, and edit_file for file work.",
      "parameters": {
        "type": "object",
        "properties": {
          "command": {"type": "string", "description": "Command to execute."}
        },
        "required": ["command"]
      }
    }
  },
  {
    "type": "function",
    "function": {
      "name": "todo_write",
      "description": "Replace this request's task list. Send the full list on every call. An empty list clears it. Each item has content and status: pending, in_progress, or completed.",
      "parameters": {
        "type": "object",
        "properties": {
          "todos": {
            "type": "array",
            "description": "The complete task list. Replaces the previous list. [] clears it.",
            "items": {
              "type": "object",
              "properties": {
                "content": {"type": "string", "description": "Task text. Leading and trailing whitespace is removed."},
                "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}
              },
              "required": ["content", "status"]
            }
          }
        },
        "required": ["todos"]
      }
    }
  },
  {
    "type": "function",
    "function": {
      "name": "ask",
      "description": "Ask the user 1 to 3 multiple-choice questions when a decision is theirs to make and you cannot settle it from the request, the code, or a sensible default. Each question has a short header, the question text, 2 to 4 options, and multiSelect when more than one option may apply. Put any recommended option first. The user may also type their own answer. Do not use this for a decision with an obvious default. The same ambiguity is asked only once: a later call returns the accepted decision. To reopen it, pass that decision_id and new_evidence.",
      "parameters": {
        "type": "object",
        "properties": {
          "questions": {
            "type": "array",
            "minItems": 1,
            "maxItems": 3,
            "description": "1 to 3 related questions to ask together.",
            "items": {
              "type": "object",
              "properties": {
                "header": {"type": "string", "description": "Very short label, such as Library."},
                "question": {"type": "string", "description": "The full question."},
                "options": {
                  "type": "array",
                  "minItems": 2,
                  "maxItems": 4,
                  "description": "The choices. Put any recommended option first.",
                  "items": {
                    "type": "object",
                    "properties": {
                      "label": {"type": "string", "description": "The choice text."},
                      "description": {"type": "string", "description": "Optional one-line explanation."}
                    },
                    "required": ["label"]
                  }
                },
                "multiSelect": {"type": "boolean", "description": "Allow selecting more than one option."}
              },
              "required": ["header", "question", "options"]
            }
          },
          "decision_id": {"type": "string", "description": "Required when reopening a previously accepted decision. Cite the decision_id from that answer."},
          "new_evidence": {"type": "string", "description": "Required with decision_id when asking again after the user already accepted a choice."}
        },
        "required": ["questions"]
      }
    }
  }
]
```

Unknown arguments are ignored. A tool failure is text beginning with `error:`, and the model can try another step. The file is left unchanged when `edit_file` or `write_file` fails.

Arguments arrive as a JSON string. Parse it. Blank or missing text means `{}`. A value that is already a dict is used as-is. When the text is not JSON, or it is JSON but not an object, return `error: arguments are not JSON ({parser message})`. For a JSON array that message is `arguments must be a JSON object: line 1 column 1 (char 0)`. An unknown tool name returns `error: unknown tool {Python repr of the name}`.

Any exception the tool body raises is returned as `error: ` plus the exception text. After the result string is built, including errors, keep the first 20,000 characters when it is longer and append a newline and `…[truncated]`.

## Paths

Strip the path. An empty path is `.`. Join it onto the resolved workspace and resolve the result, which follows symlinks. The resolved path must be the workspace itself or a descendant. Otherwise return `error: path escapes the workspace: {path as the model sent it}`.

`read_file` and `edit_file` require the resolved path to be a file. `list_dir` and `glob` require a directory. `grep` accepts either.

## read_file

Read UTF-8 and replace invalid bytes. `offset` defaults to 1. A missing, blank, or zero offset is 1. Values below 1 become 1. `limit` defaults to 400. A missing or zero limit is 400. Clamp the limit to the range 1 through 2,000.

Split the file into logical lines, dropping line endings. Take `limit` lines starting at the 1-based offset. Prefix each returned line with that line's number, then `|`, then the line text. There is no space and no padding. After the joined lines, append a newline and `[{shown} lines shown, {total} total]`.

```text
1|hello
2|world
[2 lines shown, 2 total]
```

When the window is empty, including an offset past the end, return `[empty file, {total} total]` and do not use the footer above. A path that is not a file returns `error: not a file: {the path's final name}`.

## list_dir

The default path is the workspace root. List only the directory's own entries, not children. Sort directories first, then files. Sort each group by `name.lower()`. A symlink to a directory counts as a directory. Directory names end with `/`, including on Windows. One entry per line, with no trailing newline after the last entry. An empty directory returns `[empty directory]`. A path that is not a directory returns `error: not a directory: {final name}`.

`list_dir` does not skip `.git`, `.foo-ai`, or other hidden names. `grep` and `glob` do.

## glob

`pattern` is required and must be a non-blank string, or the result is `error: pattern is required`. Strip it, then replace every `\` with `/`. Repeated `/` separators count as one, and a trailing `/` is dropped by that rule. A pattern that starts with `/`, or whose first segment is a drive prefix such as `C:`, returns `error: pattern must be relative`. A `..` segment returns `error: pattern must not contain '..'`.

`path` is optional. The default is the workspace root. It must be a directory. A missing path or a file returns `error: not a directory: {final name}`.

Search files under that directory. Do not descend into a directory whose name is `.git`, `.venv`, `venv`, `__pycache__`, `node_modules`, or `.foo-ai`. Do not follow directory symlinks. Skip a file whose resolved path is outside the workspace. Directories are not results.

Match the pattern against each file's path relative to `path`, using `/`. The printed path is relative to the workspace and uses `/`.

| Token | Meaning |
| --- | --- |
| `*` | Any run of characters in one segment, including a leading `.`. |
| `?` | One character in one segment. |
| `[abc]`, `[a-z]` | One character in that set. A range is inclusive when the start code point is less than or equal to the end. A `-` at either end of the class is literal. |
| `[!abc]`, `[^abc]` | One character outside that set. |
| `**` | When the whole segment is `**`, zero or more segments. Inside a longer segment, each `*` is the single-segment wildcard. |
| other | That character. |

An unclosed `[` is a literal `[`. A `]` immediately after `[`, `[!`, or `[^` is a member of the class. There is no escape character.

A pattern with no `/` matches files in the start directory only. When that set is empty, match `**/` plus the pattern. `*.py` therefore finds Python files in nested directories when the start directory itself has none, and it does not look further when the start directory already has a match. A pattern that contains `/` is not retried. `**/*.py` matches at every level, including the start directory.

Print one workspace-relative path per line, sorted by the path string, case-sensitive. No trailing newline after the last path. No matches returns `[no matches]`. Keep the first 200 paths and append a newline and `…[match limit reached]`.

```text
src/a.py
src/b.py
```

## grep

`pattern` is required and must be a non-blank string, or the result is `error: pattern is required`. Compile it with Python's `re` module, with no flags. An invalid pattern returns `error: ` plus the compiler's message.

The default path is the workspace root. A file is searched alone. A directory is walked with `os.walk`, which does not follow directory symlinks. Do not descend into a directory whose name is `.git`, `.venv`, `venv`, `__pycache__`, `node_modules`, or `.foo-ai`. Stop collecting files after 2,000. That cap is silent: the result does not say the walk stopped. Visit files in walk order, not sorted order. A missing start path returns `error: {final name}`.

Read each file as UTF-8 with invalid bytes replaced. A file that raises an OS error is skipped. Test the pattern with a search against each logical line, so `.` does not cross lines. Record `{relative path}:{line number}:{line text}`. The line number is 1-based. The relative path is the pathlib form, so Windows uses backslashes. Stop at 50 matches and append a newline and `…[match limit reached]`. Do not read further files after that. No matches returns `[no matches]`.

## write_file

`path` must be a non-blank string. The `content` key must be present, or the result is `error: content is required`. Convert the value with `str`. An empty string is valid.

If the path already exists, including as a directory, return `error: file exists; use edit_file to change it` and do not write. Create missing parent directories. Write UTF-8 with newline translation set to `\n`, so a `\n` in the content stays `\n` on Windows and is not expanded to `\r\n`.

The result starts with `wrote {posix relative path} ({byte size} bytes)`. The byte size is the file size after the write. The path uses forward slashes. Then, when the diff is non-empty, a newline and a unified diff of the new file against an empty old file. `difflib.unified_diff` defaults: three lines of context, `fromfile` `a/{posix path}`, `tofile` `b/{posix path}`, and no line terminator on the diff lines. Cap the diff at 80 lines. When more lines exist, keep 80 and append `…[{hidden} diff lines omitted]`, where `hidden` is the number of diff lines not shown.

```text
wrote a.txt (6 bytes)
--- a/a.txt
+++ b/a.txt
@@ -0,0 +1 @@
+hello
```

That example is the content `hello\n`, which is 6 bytes.

## edit_file

The path must be an existing file. `old_string` must be a non-empty string, or the result is `error: old_string is required`. The `new_string` key must be present, or `error: new_string is required`. The value must be a string, or `error: new_string must be a string`. An empty `new_string` deletes the matched span. When the two strings are equal, return `error: old_string and new_string are the same`.

Read and write the file with newline translation disabled, so `\r\n` stays in the buffer and is written back unchanged. Count exact occurrences of `old_string`. `read_file` shows logical lines, so the model usually sends `\n`. If the count is zero, the file contains `\r\n`, `old_string` contains `\n`, and `old_string` contains no `\r`, retry once with every `\n` in both strings replaced by `\r\n`. Do not retry a second time.

Zero matches returns `error: old_string not found`. More than one match returns `error: old_string matched {count} times; add surrounding context`. On either failure the file is not written.

On success, replace that one occurrence and write the buffer back. The result is `edited {posix relative path}`, then a newline and the unified diff of the logical lines, using the same diff rules as `write_file`. A one-line change looks like this:

```text
edited a.txt
--- a/a.txt
+++ b/a.txt
@@ -1 +1 @@
-hello
+hello world
```

## shell

`command` must be a non-blank string, or the result is `error: command is required`.

The CLI confirms the command before the process starts. Print a newline, `$ `, and the command on standard output, and flush. Then:

| Condition | Result |
| --- | --- |
| `--yes` | Run it. Do not read stdin. |
| Standard input is not a terminal | Print `Skipped: pass --yes to run shell commands.` and do not run it. |
| The user types `y` or `yes` | Run it. Compare after strip and lower-case, so `Y` and `YES` count. |
| Any other answer, or EOF | Do not run it. EOF also prints a newline. |

The prompt is `[y/N] `. A refusal returns `error: command not approved` from the tool. The skip line, when it applies, is printed in addition to that tool error. The `← shell (N chars)` line still follows unless `--verbose` prints the error text.

A confirmed command runs through the system shell: `shell=True`, one command string, not a split argument list. On Windows the shell is `cmd.exe`. On POSIX it is `/bin/sh`. The working directory is the workspace. The environment is a copy of the current process environment. Standard input is inherited. Standard output and standard error are captured, not streamed. Decode with the platform's preferred encoding and strict errors. A decode error becomes `error: ` plus that exception.

Join stdout and then stderr, `rstrip` the combined text, and append the exit status. When the combined text is non-empty the result is `{text}\n[exit {code}]`. When it is empty the result is `[exit {code}]`.

Stop the process after 120 seconds. Do not include partial output. The result is `error: Command '{command}' timed out after 120 seconds`.

## todo_write

`todos` is required and must be an array, or the result is `error: todos is required` or `error: todos must be an array`. An empty array clears the list. This is the only place the list is stored. It is not written to the session file except as this tool result.

Each item must be an object. `content` must be a non-empty string after strip. `status` must be `pending`, `in_progress`, or `completed`. Any other status, including a missing one, returns `error: todos[{index}].status {Python repr} is invalid; use pending, in_progress, or completed`. A duplicate `content`, compared after strip, returns `error: todos[{index}].content duplicates todos[{earlier}].content {Python repr}`. Indexes are 0-based. Extra keys are ignored. A failed call leaves the previous list unchanged.

On success the result is one JSON object, compact in the sense of `json.dumps` with its default spaces, with `ensure_ascii` left off:

```json
{"todos": [{"content": "ship", "status": "in_progress"}], "counts": {"total": 1, "pending": 0, "in_progress": 1, "completed": 0}}
```

`counts.total` is the number of items. The other three counts are how many items have that status. Key order is `todos`, then `counts`, and inside `counts` it is `total`, `pending`, `in_progress`, `completed`. Item order is the order sent. `content` is the stripped text.

The list belongs to the current user request. Each user request, including a subagent slash command, clears it before the model runs. `/reset` clears it too. When a clear drops a non-empty list, the CLI prints the empty list. A successful `todo_write`, including a clear, prints the list as well.

The CLI print is a leading newline, `Todos:`, then one line per item, `[ ]`, `[>]`, or `[x]`, a space, and the content. Pending is a space, in progress is `>`, completed is `x`. An empty list prints a leading newline and `Todos: (none)`. Flush after the block.

## ask

`questions` is required and must be an array of 1 to 3 objects. Fewer or more returns `error: questions must contain 1 to 3 items`. A missing or non-array value returns `error: questions is required`.

Each question requires a non-empty `header`, a non-empty `question`, and `options` of 2 to 4 objects. Strip `header`, `question`, `label`, and `description`. An empty string after strip fails. `multiSelect` is optional and defaults to false. When it is present and not a boolean, the result is `error: questions[{index}].multiSelect must be a boolean`. Each option requires a non-empty `label`. `description` is optional. When it is present and not a string, the result is `error: questions[{index}].options[{index}].description must be a string`. Duplicate labels in one question, compared after strip, return `error: questions[{index}].options[{index}].label duplicates an earlier option`. Indexes are 0-based. The host assigns ids `q1`, `q2`, and `q3` in order.

The CLI asks only when standard input is a terminal. `--yes` does not answer it. A pipe, including a one-shot prompt whose stdin is not a terminal, does not read stdin and does not print the choices. The tool result is this sentence:

```text
No interactive user answered. This is a model-assumption fallback, not a user answer. Proceed with your best judgment, state the assumption you made, and prefer the safest reversible option when choices differ in risk.
```

On a terminal, print each question before reading its answer. A leading newline, `? `, the header, a newline, the question, a newline, then one option per line: two spaces, the 1-based number, `. `, the label, and when the description is non-empty, ` - ` and the description. Then the prompt, with no extra newline before it:

| `multiSelect` | Prompt |
| --- | --- |
| false | `Choose one [1-{count}], or type your own answer: ` |
| true | `Choose any [1-{count}], comma-separated, or type your own answer: ` |

Flush, then read one line. A blank line leaves that question unanswered. EOF prints a newline and dismisses the whole batch, including questions already answered. A line whose tokens are all numbers, but not a valid choice, prints an error, prints the same prompt again, and reads another line. Any other non-blank line is the user's own answer: the stripped line, as one selection.

| Line | Result |
| --- | --- |
| one in-range number | That option, for either mode. |
| several in-range numbers, separated by commas or whitespace | Those options, in order, duplicates removed, only when `multiSelect` is true. |
| several numbers when `multiSelect` is false | `Enter one number from 1 to {count}.` |
| numbers outside that range | `Enter a number from 1 to {count}.`, or for `multiSelect`, `Enter numbers from 1 to {count}, separated by commas.` |
| any other non-blank line | That stripped line is the user's own answer. |

`decision_id` and `new_evidence` are optional. A present `decision_id` must be a string, or the result is `error: decision_id must be a string`. A blank or whitespace-only `decision_id` is the same as a missing one: the call uses the ambiguity id, as if the key were absent. A present `new_evidence` must be a string, or the result is `error: new_evidence must be a string`. Whitespace-only `new_evidence` does not reopen a decision.

The same ambiguity is not asked twice. The ambiguity id is `dec-` plus the first 16 hex characters of the SHA-256 of each question's stripped lower-case header, question, and option labels, in that order, with a NUL after each piece. Accepted answers are stored in `.foo-ai/decisions.json`. See [Session](session.md).

| Call | Result, without asking |
| --- | --- |
| no `decision_id`, and the ambiguity id is already stored | `Host reused accepted decision {id} for the same ambiguity. The user already chose: {answer}. Continue with that decision; to reopen it, cite decision_id {id} and supply new_evidence.` |
| `decision_id` names a stored decision, and `new_evidence` is blank | `Host reused accepted decision {id}. The user already chose: {answer}. Continue with that decision unless you supply decision_id and new_evidence.` |
| `decision_id` names nothing, and some decision is already stored | `error: unknown decision_id {Python repr}; cite the original accepted decision_id and include new_evidence to reopen it` |

A matching `decision_id` with non-blank `new_evidence` asks again and replaces that decision. A `decision_id` that names nothing, when nothing is stored yet, asks and stores the answer under the cited id. The headless fallback and a dismiss are not stored.

The tool result when at least one question has a selection:

```text
The user answered:
- Library: requests
- Scope: (left unanswered — don't assume a choice)

decision_id: dec-0123abcd4567ef89
```

The header is the label before the colon. Selections are joined with `, `. A question with no selection uses `(left unanswered — don't assume a choice)`. A selection is kept after its whitespace is collapsed, including text the user typed that is not one of the option labels. When `multiSelect` is false, only the first remaining selection is kept. The stored answer is the summary above the blank line. The tool result appends a blank line and `decision_id: {id}`.

When every question is unanswered, or the user hits EOF, the result is this sentence. Later tool calls in that assistant message do not run. Do not take another model round. Do not store another assistant message.

```text
The user dismissed the question without choosing — read this as "don't decide for me, let's just talk." Do not pick an option, run a tool, or take any further action toward this; stop and wait for the user's next message.
```
