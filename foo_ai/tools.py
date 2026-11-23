"""Workspace tools advertised to the model as OpenAI function definitions."""

from __future__ import annotations

import difflib
import json
import os
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

READ_DEFAULT_LIMIT = 400
READ_MAX_LIMIT = 2000
GREP_MAX_MATCHES = 50
TOOL_OUTPUT_LIMIT = 20000
SHELL_TIMEOUT_SECONDS = 120
DIFF_MAX_LINES = 80

ApproveShell = Callable[[str], bool]

SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", ".foo-ai"}


def tool_schemas() -> list[dict]:
    return [
        _function(
            "read_file",
            "Read a UTF-8 text file in the workspace. Lines are numbered from 1. Use offset and limit to page through long files.",
            {
                "path": {"type": "string", "description": "File path relative to the workspace."},
                "offset": {"type": "integer", "minimum": 1, "description": "1-based first line to return. Default 1."},
                "limit": {"type": "integer", "minimum": 1, "description": f"Maximum lines to return. Default {READ_DEFAULT_LIMIT}."},
            },
            ["path"],
        ),
        _function(
            "list_dir",
            "List entries in a workspace directory. Pass '.' for the workspace root.",
            {"path": {"type": "string", "description": "Directory relative to the workspace. Default '.'."}},
            [],
        ),
        _function(
            "grep",
            "Search workspace text files for a Python regular expression. Returns path:line:text matches.",
            {
                "pattern": {"type": "string", "description": "Regular expression."},
                "path": {"type": "string", "description": "File or directory to search. Default workspace root."},
            },
            ["pattern"],
        ),
        _function(
            "write_file",
            "Create a new UTF-8 text file inside the workspace. Fails if the file already exists; use edit_file to change an existing file.",
            {
                "path": {"type": "string", "description": "File path relative to the workspace."},
                "content": {"type": "string", "description": "Full file contents."},
            },
            ["path", "content"],
        ),
        _function(
            "edit_file",
            "Replace one exact occurrence of old_string with new_string in an existing file. old_string is raw file text, without the line-number prefix shown by read_file. Fails if old_string is missing or matches more than once; add surrounding lines to disambiguate.",
            {
                "path": {"type": "string", "description": "File path relative to the workspace."},
                "old_string": {"type": "string", "description": "Exact text to replace. Must match once."},
                "new_string": {"type": "string", "description": "Replacement text. An empty string deletes the match."},
            },
            ["path", "old_string", "new_string"],
        ),
        _function(
            "shell",
            "Run a command in the workspace and return combined stdout and stderr. The user must approve the command before it runs. Prefer read_file, list_dir, grep, and edit_file for file work.",
            {"command": {"type": "string", "description": "Command to execute."}},
            ["command"],
        ),
    ]


def run_tool(name: str, arguments: str | dict, workspace: Path, approve_shell: ApproveShell | None = None) -> str:
    try:
        args = _arguments(arguments)
    except json.JSONDecodeError as exc:
        return f"error: arguments are not JSON ({exc})"
    try:
        if name == "read_file":
            text = _read_file(workspace, args)
        elif name == "list_dir":
            text = _list_dir(workspace, args)
        elif name == "grep":
            text = _grep(workspace, args)
        elif name == "write_file":
            text = _write_file(workspace, args)
        elif name == "edit_file":
            text = _edit_file(workspace, args)
        elif name == "shell":
            text = _shell(workspace, args, approve_shell)
        else:
            text = f"error: unknown tool {name!r}"
    except Exception as exc:
        text = f"error: {exc}"
    if len(text) > TOOL_OUTPUT_LIMIT:
        text = text[:TOOL_OUTPUT_LIMIT] + "\n…[truncated]"
    return text


def workspace_path(workspace: Path, raw: str) -> Path:
    root = workspace.resolve()
    relative = raw.strip() or "."
    path = (root / relative).resolve()
    if path != root and root not in path.parents:
        raise ValueError(f"path escapes the workspace: {raw}")
    return path


def _function(name: str, description: str, properties: dict, required: list[str]) -> dict:
    schema: dict = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": schema},
    }


def _arguments(arguments: str | dict) -> dict:
    if isinstance(arguments, dict):
        return arguments
    if not arguments or not str(arguments).strip():
        return {}
    parsed = json.loads(arguments)
    if not isinstance(parsed, dict):
        raise json.JSONDecodeError("arguments must be a JSON object", str(arguments), 0)
    return parsed


def _read_file(workspace: Path, args: dict) -> str:
    path = workspace_path(workspace, _required(args, "path"))
    if not path.is_file():
        raise FileNotFoundError(f"not a file: {path.name}")
    offset = max(int(args.get("offset") or 1), 1)
    limit = int(args.get("limit") or READ_DEFAULT_LIMIT)
    limit = min(max(limit, 1), READ_MAX_LIMIT)
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    window = lines[offset - 1 : offset - 1 + limit]
    numbered = [f"{offset + index}|{line}" for index, line in enumerate(window)]
    footer = f"\n[{len(window)} lines shown, {len(lines)} total]"
    return ("\n".join(numbered) + footer) if numbered else f"[empty file, {len(lines)} total]"


def _list_dir(workspace: Path, args: dict) -> str:
    path = workspace_path(workspace, str(args.get("path") or "."))
    if not path.is_dir():
        raise NotADirectoryError(f"not a directory: {path.name}")
    entries = []
    for child in sorted(path.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
        suffix = "/" if child.is_dir() else ""
        entries.append(child.name + suffix)
    return "\n".join(entries) if entries else "[empty directory]"


def _grep(workspace: Path, args: dict) -> str:
    pattern = _required(args, "pattern")
    regex = re.compile(pattern)
    start = workspace_path(workspace, str(args.get("path") or "."))
    files = [start] if start.is_file() else _text_files(start)
    matches: list[str] = []
    for file in files:
        try:
            text = file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                relative = file.relative_to(workspace.resolve())
                matches.append(f"{relative}:{number}:{line}")
                if len(matches) >= GREP_MAX_MATCHES:
                    return "\n".join(matches) + "\n…[match limit reached]"
    return "\n".join(matches) if matches else "[no matches]"


def _write_file(workspace: Path, args: dict) -> str:
    path = workspace_path(workspace, _required(args, "path"))
    if "content" not in args:
        raise ValueError("content is required")
    if path.exists():
        raise FileExistsError("file exists; use edit_file to change it")
    content = str(args["content"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    relative = path.relative_to(workspace.resolve())
    return _with_diff(f"wrote {relative.as_posix()} ({path.stat().st_size} bytes)", workspace, path, "", content)


def _edit_file(workspace: Path, args: dict) -> str:
    path = workspace_path(workspace, _required(args, "path"))
    if not path.is_file():
        raise FileNotFoundError(f"not a file: {path.name}")
    old = args.get("old_string")
    if not isinstance(old, str) or old == "":
        raise ValueError("old_string is required")
    if "new_string" not in args:
        raise ValueError("new_string is required")
    new = args.get("new_string")
    if not isinstance(new, str):
        raise ValueError("new_string must be a string")
    if old == new:
        raise ValueError("old_string and new_string are the same")
    before = _read_preserving_newlines(path)
    after = _replace_once(before, old, new)
    _write_preserving_newlines(path, after)
    relative = path.relative_to(workspace.resolve())
    return _with_diff(f"edited {relative.as_posix()}", workspace, path, before, after)


def _read_preserving_newlines(path: Path) -> str:
    # Path.read_text(newline=) requires Python 3.13. open() keeps CRLF on 3.11.
    with path.open(encoding="utf-8", newline="") as handle:
        return handle.read()


def _write_preserving_newlines(path: Path, text: str) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def _replace_once(text: str, old: str, new: str) -> str:
    count = text.count(old)
    # read_file shows logical lines, so the model sends \n. Match a CRLF file once.
    if count == 0 and "\r\n" in text and "\n" in old and "\r" not in old:
        return _replace_once(text, old.replace("\n", "\r\n"), new.replace("\n", "\r\n"))
    if count == 0:
        raise ValueError("old_string not found")
    if count > 1:
        raise ValueError(f"old_string matched {count} times; add surrounding context")
    return text.replace(old, new, 1)


def _with_diff(header: str, workspace: Path, path: Path, before: str, after: str) -> str:
    diff = _unified(workspace, path, before, after)
    return f"{header}\n{diff}" if diff else header


def _unified(workspace: Path, path: Path, before: str, after: str) -> str:
    relative = path.relative_to(workspace.resolve()).as_posix()
    lines = list(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile=f"a/{relative}",
            tofile=f"b/{relative}",
            lineterm="",
        )
    )
    if len(lines) > DIFF_MAX_LINES:
        hidden = len(lines) - DIFF_MAX_LINES
        lines = lines[:DIFF_MAX_LINES]
        lines.append(f"…[{hidden} diff lines omitted]")
    return "\n".join(lines)


def _shell(workspace: Path, args: dict, approve_shell: ApproveShell | None) -> str:
    command = _required(args, "command")
    if approve_shell is not None and not approve_shell(command):
        return "error: command not approved"
    completed = subprocess.run(
        command,
        shell=True,
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=SHELL_TIMEOUT_SECONDS,
        env=os.environ.copy(),
    )
    output = (completed.stdout or "") + (completed.stderr or "")
    output = output.rstrip()
    status = f"\n[exit {completed.returncode}]"
    return (output + status) if output else status.strip()


def _text_files(root: Path) -> list[Path]:
    found: list[Path] = []
    if not root.exists():
        raise FileNotFoundError(root.name)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in SKIP_DIRS]
        for name in filenames:
            found.append(Path(dirpath) / name)
            if len(found) >= 2000:
                return found
    return found


def _required(args: dict, key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} is required")
    return value
