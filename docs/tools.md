# Tools

The model sees six built-in tools: `read_file`, `list_dir`, `grep`, `write_file`, `edit_file`, and `shell`. MCP servers add more; see [MCP](mcp.md). Send them as an OpenAI tool list. `list_dir` has no `required` array. The other five do.

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
      "description": "Run a command in the workspace and return combined stdout and stderr. The user must approve the command before it runs. Prefer read_file, list_dir, grep, and edit_file for file work.",
      "parameters": {
        "type": "object",
        "properties": {
          "command": {"type": "string", "description": "Command to execute."}
        },
        "required": ["command"]
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

`read_file` and `edit_file` require the resolved path to be a file. `list_dir` requires a directory. `grep` accepts either.

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

`list_dir` does not skip `.git`, `.foo-ai`, or other hidden names. `grep` does.

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
