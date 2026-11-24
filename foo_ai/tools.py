"""The seven built-in tools."""

from __future__ import annotations

import difflib
import json
import locale
import os
import re
import subprocess
from pathlib import Path

TOOL_NAMES = ("read_file", "list_dir", "glob", "grep", "write_file", "edit_file", "shell")

SHELL_TIMEOUT_SECONDS = 120
RESULT_LIMIT = 20_000
GLOB_MATCH_LIMIT = 200
_SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", ".foo-ai"}

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read a UTF-8 text file in the workspace. Lines are numbered from 1. "
                "Use offset and limit to page through long files."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path relative to the workspace."},
                    "offset": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "1-based first line to return. Default 1.",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "Maximum lines to return. Default 400.",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List entries in a workspace directory. Pass '.' for the workspace root.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Directory relative to the workspace. Default '.'.",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "glob",
            "description": (
                "Find files matching a glob pattern. * and ? match inside one path segment, "
                "and ** crosses directories. A pattern with no / matches the start directory, "
                "then the whole tree only when that directory has no match. "
                "Use **/*.py to search every level. Results are workspace-relative paths using /."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Glob pattern, with / as the separator.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Directory to search. Default workspace root.",
                    },
                },
                "required": ["pattern"],
            },
        },
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
                    "path": {
                        "type": "string",
                        "description": "File or directory to search. Default workspace root.",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Create a new UTF-8 text file inside the workspace. "
                "Fails if the file already exists; use edit_file to change an existing file."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path relative to the workspace."},
                    "content": {"type": "string", "description": "Full file contents."},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "Replace one exact occurrence of old_string with new_string in an existing file. "
                "old_string is raw file text, without the line-number prefix shown by read_file. "
                "Fails if old_string is missing or matches more than once; add surrounding lines to disambiguate."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path relative to the workspace."},
                    "old_string": {"type": "string", "description": "Exact text to replace. Must match once."},
                    "new_string": {
                        "type": "string",
                        "description": "Replacement text. An empty string deletes the match.",
                    },
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "shell",
            "description": (
                "Run a command in the workspace and return combined stdout and stderr. "
                "The user must approve the command before it runs. "
                "Prefer read_file, list_dir, glob, grep, and edit_file for file work."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Command to execute."}
                },
                "required": ["command"],
            },
        },
    },
]


def parse_arguments(raw: object, *, mcp: bool = False) -> dict | str:
    if isinstance(raw, dict):
        return raw
    if raw is None or (isinstance(raw, str) and raw.strip() == ""):
        return {}
    if not isinstance(raw, str):
        return "error: arguments are not JSON (arguments must be a JSON object: line 1 column 1 (char 0))"
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        return f"error: arguments are not JSON ({exc})"
    if isinstance(value, dict):
        return value
    if mcp:
        return "error: arguments must be a JSON object"
    return "error: arguments are not JSON (arguments must be a JSON object: line 1 column 1 (char 0))"


def cap_result(result: str) -> str:
    if len(result) > RESULT_LIMIT:
        return result[:RESULT_LIMIT] + "\n…[truncated]"
    return result


def _stop_process(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
            stdin=subprocess.DEVNULL,
        )
    try:
        proc.kill()
    except OSError:
        pass


def _unified(old_lines: list[str], new_lines: list[str], posix_path: str) -> str:
    diff = list(
        difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=f"a/{posix_path}",
            tofile=f"b/{posix_path}",
            lineterm="",
        )
    )
    if not diff:
        return ""
    if len(diff) > 80:
        hidden = len(diff) - 80
        return "\n".join(diff[:80]) + f"\n…[{hidden} diff lines omitted]"
    return "\n".join(diff)


class Toolbox:
    def __init__(self, workspace: Path, confirm, mcp=None, skills=None):
        self.workspace = workspace.resolve()
        self.confirm = confirm
        self.mcp = mcp
        self.skills = skills
        self.skill_runner = None

    def schemas(self) -> list[dict]:
        from foo_ai.skill import RUN_SKILL_SCHEMA

        extra = self.mcp.schemas() if self.mcp is not None else []
        skill_schemas = []
        if self.skills is not None and self.skills.entries:
            skill_schemas = [RUN_SKILL_SCHEMA]
        return [*TOOL_SCHEMAS, *skill_schemas, *extra]

    def run(self, name: str, raw: object) -> str:
        try:
            if name == "read_file":
                result = self._read_file(raw)
            elif name == "list_dir":
                result = self._list_dir(raw)
            elif name == "glob":
                result = self._glob(raw)
            elif name == "grep":
                result = self._grep(raw)
            elif name == "write_file":
                result = self._write_file(raw)
            elif name == "edit_file":
                result = self._edit_file(raw)
            elif name == "shell":
                result = self._shell(raw)
            elif name == "run_skill":
                result = self._run_skill(raw)
            elif self.mcp is not None and self.mcp.knows(name):
                result = self.mcp.call(name, raw)
            else:
                result = f"error: unknown tool {name!r}"
        except Exception as exc:
            result = f"error: {exc}"
        return cap_result(result)

    def _resolve(self, raw: object) -> Path | str:
        original = raw if isinstance(raw, str) else ""
        stripped = original.strip()
        relative = stripped if stripped else "."
        candidate = (self.workspace / relative).resolve()
        try:
            candidate.relative_to(self.workspace)
        except ValueError:
            return f"error: path escapes the workspace: {original}"
        return candidate

    def _read_file(self, raw: object) -> str:
        args = parse_arguments(raw)
        if isinstance(args, str):
            return args
        resolved = self._resolve(args.get("path", ""))
        if isinstance(resolved, str):
            return resolved
        if not resolved.is_file():
            return f"error: not a file: {_final_name(args.get('path', ''), resolved)}"
        text = resolved.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        offset = _offset(args.get("offset"))
        limit = _limit(args.get("limit"))
        window = lines[offset - 1 : offset - 1 + limit]
        total = len(lines)
        if not window:
            return f"[empty file, {total} total]"
        body = "\n".join(f"{offset + index}|{line}" for index, line in enumerate(window))
        return f"{body}\n[{len(window)} lines shown, {total} total]"

    def _list_dir(self, raw: object) -> str:
        args = parse_arguments(raw)
        if isinstance(args, str):
            return args
        resolved = self._resolve(args.get("path", ""))
        if isinstance(resolved, str):
            return resolved
        if not resolved.is_dir():
            return f"error: not a directory: {_final_name(args.get('path', ''), resolved)}"
        directories: list[str] = []
        files: list[str] = []
        for entry in resolved.iterdir():
            if entry.is_dir():
                directories.append(entry.name)
            else:
                files.append(entry.name)
        directories.sort(key=str.lower)
        files.sort(key=str.lower)
        lines = [name + "/" for name in directories] + files
        if not lines:
            return "[empty directory]"
        return "\n".join(lines)

    def _glob(self, raw: object) -> str:
        args = parse_arguments(raw)
        if isinstance(args, str):
            return args
        pattern = args.get("pattern")
        if not isinstance(pattern, str) or pattern.strip() == "":
            return "error: pattern is required"
        normalized, error = _normalize_glob(pattern)
        if error is not None:
            return error
        resolved = self._resolve(args.get("path", ""))
        if isinstance(resolved, str):
            return resolved
        if not resolved.is_dir():
            return f"error: not a directory: {_final_name(args.get('path', ''), resolved)}"
        entries = _glob_entries(resolved, self.workspace)
        matched = [display for relative, display in entries if _match_glob(relative, normalized)]
        if not matched and "/" not in normalized:
            matched = [display for relative, display in entries if _match_glob(relative, "**/" + normalized)]
        return _format_glob(matched)

    def _grep(self, raw: object) -> str:
        args = parse_arguments(raw)
        if isinstance(args, str):
            return args
        pattern = args.get("pattern")
        if not isinstance(pattern, str) or pattern.strip() == "":
            return "error: pattern is required"
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            return f"error: {exc}"
        resolved = self._resolve(args.get("path", ""))
        if isinstance(resolved, str):
            return resolved
        if not resolved.exists():
            return f"error: {_final_name(args.get('path', ''), resolved)}"
        files = [resolved] if resolved.is_file() else _walk_files(resolved)
        matches: list[str] = []
        limited = False
        for file_path in files:
            try:
                resolved_file = file_path.resolve()
                relative = str(resolved_file.relative_to(self.workspace))
            except (OSError, ValueError):
                continue
            try:
                text = resolved_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for number, line in enumerate(text.splitlines(), start=1):
                if compiled.search(line):
                    matches.append(f"{relative}:{number}:{line}")
                    if len(matches) >= 50:
                        limited = True
                        break
            if limited:
                break
        if not matches:
            return "[no matches]"
        text = "\n".join(matches)
        if limited:
            text += "\n…[match limit reached]"
        return text

    def _write_file(self, raw: object) -> str:
        args = parse_arguments(raw)
        if isinstance(args, str):
            return args
        path_value = args.get("path")
        if not isinstance(path_value, str) or path_value.strip() == "":
            return "error: path is required"
        if "content" not in args:
            return "error: content is required"
        content = str(args["content"])
        resolved = self._resolve(path_value)
        if isinstance(resolved, str):
            return resolved
        if resolved.exists():
            return "error: file exists; use edit_file to change it"
        resolved.parent.mkdir(parents=True, exist_ok=True)
        with resolved.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        size = resolved.stat().st_size
        posix = resolved.relative_to(self.workspace).as_posix()
        header = f"wrote {posix} ({size} bytes)"
        diff = _unified([], content.splitlines(), posix)
        if diff:
            return header + "\n" + diff
        return header

    def _edit_file(self, raw: object) -> str:
        args = parse_arguments(raw)
        if isinstance(args, str):
            return args
        resolved = self._resolve(args.get("path", ""))
        if isinstance(resolved, str):
            return resolved
        if not resolved.is_file():
            return f"error: not a file: {_final_name(args.get('path', ''), resolved)}"
        old = args.get("old_string")
        if not isinstance(old, str) or old == "":
            return "error: old_string is required"
        if "new_string" not in args:
            return "error: new_string is required"
        new = args["new_string"]
        if not isinstance(new, str):
            return "error: new_string must be a string"
        if old == new:
            return "error: old_string and new_string are the same"
        with resolved.open("r", encoding="utf-8", newline="") as handle:
            text = handle.read()
        count = text.count(old)
        if count == 0 and "\r\n" in text and "\n" in old and "\r" not in old:
            old = old.replace("\n", "\r\n")
            new = new.replace("\n", "\r\n")
            count = text.count(old)
        if count == 0:
            return "error: old_string not found"
        if count > 1:
            return f"error: old_string matched {count} times; add surrounding context"
        updated = text.replace(old, new, 1)
        with resolved.open("w", encoding="utf-8", newline="") as handle:
            handle.write(updated)
        posix = resolved.relative_to(self.workspace).as_posix()
        header = f"edited {posix}"
        diff = _unified(text.splitlines(), updated.splitlines(), posix)
        if diff:
            return header + "\n" + diff
        return header

    def _shell(self, raw: object) -> str:
        args = parse_arguments(raw)
        if isinstance(args, str):
            return args
        command = args.get("command")
        if not isinstance(command, str) or command.strip() == "":
            return "error: command is required"
        if not self.confirm(command):
            return "error: command not approved"
        return self.execute_shell(command)

    def execute_shell(self, command: str) -> str:
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=str(self.workspace),
            env=os.environ.copy(),
            stdin=None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            out, err = proc.communicate(timeout=SHELL_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            _stop_process(proc)
            proc.communicate()
            return f"error: Command '{command}' timed out after {SHELL_TIMEOUT_SECONDS} seconds"
        except KeyboardInterrupt:
            _stop_process(proc)
            try:
                proc.communicate(timeout=1)
            except Exception:
                pass
            raise
        encoding = locale.getpreferredencoding(False)
        try:
            out_text = out.decode(encoding)
            err_text = err.decode(encoding)
        except UnicodeDecodeError as exc:
            return f"error: {exc}"
        combined = (out_text + err_text).rstrip()
        if combined:
            return f"{combined}\n[exit {proc.returncode}]"
        return f"[exit {proc.returncode}]"

    def _run_skill(self, raw: object) -> str:
        args = parse_arguments(raw)
        if isinstance(args, str):
            return args
        if self.skills is None or not self.skills.entries:
            return "error: unknown tool 'run_skill'"
        name = args.get("name")
        if not isinstance(name, str) or name.strip() == "":
            return "error: name is required"
        if "arguments" in args and not isinstance(args["arguments"], str):
            return "error: arguments must be a string"
        arguments = args.get("arguments")
        if not isinstance(arguments, str):
            arguments = ""
        skill = self.skills.get(name.strip())
        if skill is None:
            return f"error: unknown skill {name.strip()!r}"
        if skill.run == "subagent" and arguments.strip() == "":
            return f"error: arguments are required for subagent skill {skill.name!r}"
        if self.skill_runner is None:
            return "error: skills are not available"
        return self.skill_runner(skill, arguments.strip())


def _offset(value: object) -> int:
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return 1
    number = _number(value)
    return 1 if number < 1 else number


def _limit(value: object) -> int:
    if value is None or (isinstance(value, str) and value.strip() == ""):
        return 400
    number = _number(value)
    if number == 0:
        return 400
    if number < 1:
        number = 1
    if number > 2000:
        number = 2000
    return number


def _number(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        return int(value.strip())
    raise TypeError(f"expected a number, got {type(value).__name__}")


def _final_name(raw: object, candidate: Path) -> str:
    if isinstance(raw, str):
        name = Path(raw.strip() or ".").name
        if name not in ("", ".", ".."):
            return name
    return candidate.name or str(candidate)


def _walk_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in _SKIP_DIRS]
        for name in filenames:
            files.append(Path(dirpath) / name)
            if len(files) >= 2000:
                return files
    return files


def _normalize_glob(pattern: str) -> tuple[str | None, str | None]:
    text = pattern.strip().replace("\\", "/")
    if text == "":
        return None, "error: pattern is required"
    first = text.split("/", 1)[0]
    if text.startswith("/") or (len(first) >= 2 and first[0].isalpha() and first[1] == ":"):
        return None, "error: pattern must be relative"
    parts = [part for part in text.split("/") if part]
    if any(part == ".." for part in parts):
        return None, "error: pattern must not contain '..'"
    if not parts:
        return None, "error: pattern is required"
    return "/".join(parts), None


def _glob_entries(root: Path, workspace: Path) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in _SKIP_DIRS]
        for name in filenames:
            file_path = Path(dirpath) / name
            try:
                file_path.resolve().relative_to(workspace)
                relative = file_path.relative_to(root).as_posix()
                display = file_path.relative_to(workspace).as_posix()
            except (OSError, ValueError):
                continue
            entries.append((relative, display))
    return entries


def _format_glob(paths: list[str]) -> str:
    if not paths:
        return "[no matches]"
    ordered = sorted(set(paths))
    limited = len(ordered) > GLOB_MATCH_LIMIT
    if limited:
        ordered = ordered[:GLOB_MATCH_LIMIT]
    text = "\n".join(ordered)
    if limited:
        text += "\n…[match limit reached]"
    return text


def _match_glob(relative: str, pattern: str) -> bool:
    parts = relative.split("/") if relative else []
    pats = pattern.split("/")
    return _match_parts(parts, pats, 0, 0, {})


def _match_parts(
    parts: list[str],
    pats: list[str],
    index: int,
    pattern_index: int,
    memo: dict[tuple[int, int], bool],
) -> bool:
    key = (index, pattern_index)
    cached = memo.get(key)
    if cached is not None:
        return cached
    while pattern_index < len(pats) and pats[pattern_index] == "**":
        pattern_index += 1
        for next_index in range(index, len(parts) + 1):
            if _match_parts(parts, pats, next_index, pattern_index, memo):
                memo[key] = True
                return True
        memo[key] = False
        return False
    if pattern_index == len(pats):
        memo[key] = index == len(parts)
        return memo[key]
    if index == len(parts) or not _segment_match(parts[index], pats[pattern_index]):
        memo[key] = False
        return False
    memo[key] = _match_parts(parts, pats, index + 1, pattern_index + 1, memo)
    return memo[key]


def _segment_match(text: str, pattern: str) -> bool:
    return _segment_at(text, 0, pattern, 0)


def _segment_at(text: str, text_index: int, pattern: str, pattern_index: int) -> bool:
    while pattern_index < len(pattern):
        token = pattern[pattern_index]
        if token == "*":
            pattern_index += 1
            while pattern_index < len(pattern) and pattern[pattern_index] == "*":
                pattern_index += 1
            if pattern_index == len(pattern):
                return True
            for next_index in range(text_index, len(text) + 1):
                if _segment_at(text, next_index, pattern, pattern_index):
                    return True
            return False
        if text_index >= len(text):
            return False
        if token == "?":
            text_index += 1
            pattern_index += 1
            continue
        if token == "[":
            matched, pattern_index = _class_match(text[text_index], pattern, pattern_index)
            if not matched:
                return False
            text_index += 1
            continue
        if token != text[text_index]:
            return False
        text_index += 1
        pattern_index += 1
    return text_index == len(text)


def _class_match(char: str, pattern: str, start: int) -> tuple[bool, int]:
    index = start + 1
    if index >= len(pattern):
        return char == "[", start + 1
    negate = pattern[index] in "^!"
    if negate:
        index += 1
    if index >= len(pattern):
        return char == "[", start + 1
    members: set[str] = set()
    if pattern[index] == "]":
        members.add("]")
        index += 1
    while index < len(pattern) and pattern[index] != "]":
        range_end = index + 2
        if (
            pattern[index] != "-"
            and range_end < len(pattern)
            and pattern[index + 1] == "-"
            and pattern[range_end] != "]"
        ):
            first = ord(pattern[index])
            last = ord(pattern[range_end])
            if first <= last:
                members.update(chr(code) for code in range(first, last + 1))
            index += 3
            continue
        members.add(pattern[index])
        index += 1
    if index >= len(pattern):
        return char == "[", start + 1
    matched = char in members
    if negate:
        matched = not matched
    return matched, index + 1
