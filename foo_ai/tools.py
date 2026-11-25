"""The built-in tools."""

from __future__ import annotations

import difflib
import hashlib
import json
import locale
import os
import re
import subprocess
from pathlib import Path

TOOL_NAMES = (
    "read_file",
    "list_dir",
    "glob",
    "grep",
    "write_file",
    "edit_file",
    "shell",
    "todo_write",
    "ask",
)

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
    {
        "type": "function",
        "function": {
            "name": "todo_write",
            "description": (
                "Replace this request's task list. Send the full list on every call. "
                "An empty list clears it. Each item has content and status: "
                "pending, in_progress, or completed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "todos": {
                        "type": "array",
                        "description": "The complete task list. Replaces the previous list. [] clears it.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "content": {
                                    "type": "string",
                                    "description": "Task text. Leading and trailing whitespace is removed.",
                                },
                                "status": {
                                    "type": "string",
                                    "enum": ["pending", "in_progress", "completed"],
                                },
                            },
                            "required": ["content", "status"],
                        },
                    }
                },
                "required": ["todos"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask",
            "description": (
                "Ask the user 1 to 3 multiple-choice questions when a decision is theirs to make "
                "and you cannot settle it from the request, the code, or a sensible default. "
                "Each question has a short header, the question text, 2 to 4 options, "
                "and multiSelect when more than one option may apply. "
                "Put any recommended option first. The user may also type their own answer. "
                "Do not use this for a decision with an obvious default. "
                "The same ambiguity is asked only once: a later call returns the accepted decision. "
                "To reopen it, pass that decision_id and new_evidence."
            ),
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
                                "header": {
                                    "type": "string",
                                    "description": "Very short label, such as Library.",
                                },
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
                                            "description": {
                                                "type": "string",
                                                "description": "Optional one-line explanation.",
                                            },
                                        },
                                        "required": ["label"],
                                    },
                                },
                                "multiSelect": {
                                    "type": "boolean",
                                    "description": "Allow selecting more than one option.",
                                },
                            },
                            "required": ["header", "question", "options"],
                        },
                    },
                    "decision_id": {
                        "type": "string",
                        "description": (
                            "Required when reopening a previously accepted decision. "
                            "Cite the decision_id from that answer."
                        ),
                    },
                    "new_evidence": {
                        "type": "string",
                        "description": (
                            "Required with decision_id when asking again after the user already accepted a choice."
                        ),
                    },
                },
                "required": ["questions"],
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


ASK_NO_USER = (
    "No interactive user answered. This is a model-assumption fallback, not a user answer. "
    "Proceed with your best judgment, state the assumption you made, and prefer the safest "
    "reversible option when choices differ in risk."
)
ASK_DISMISSED = (
    'The user dismissed the question without choosing — read this as "don\'t decide for me, let\'s just talk." '
    "Do not pick an option, run a tool, or take any further action toward this; stop and wait for the user's next message."
)
ASK_SKIPPED = "error: skipped because the user dismissed the question"
_TODO_STATUS = {"pending", "in_progress", "completed"}


def normalize_questions(raw: object) -> tuple[list[dict], str] | tuple[None, str]:
    """Return the questions the asker shows, or ``(None, error text)``."""
    if not isinstance(raw, list):
        return None, "error: questions is required"
    if not 1 <= len(raw) <= 3:
        return None, "error: questions must contain 1 to 3 items"
    questions: list[dict] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            return None, f"error: questions[{index}] must be an object"
        question = item.get("question")
        header = item.get("header")
        if not isinstance(question, str) or question.strip() == "":
            return None, f"error: questions[{index}].question must be a non-empty string"
        if not isinstance(header, str) or header.strip() == "":
            return None, f"error: questions[{index}].header must be a non-empty string"
        options = item.get("options")
        if not isinstance(options, list) or not 2 <= len(options) <= 4:
            return None, f"error: questions[{index}].options must contain 2 to 4 items"
        cleaned_options: list[dict] = []
        seen: set[str] = set()
        for option_index, option in enumerate(options):
            if not isinstance(option, dict):
                return None, f"error: questions[{index}].options[{option_index}] must be an object"
            label = option.get("label")
            if not isinstance(label, str) or label.strip() == "":
                return None, (
                    f"error: questions[{index}].options[{option_index}].label must be a non-empty string"
                )
            label = label.strip()
            if label in seen:
                return None, f"error: questions[{index}].options[{option_index}].label duplicates an earlier option"
            seen.add(label)
            if "description" in option and not isinstance(option["description"], str):
                return None, (
                    f"error: questions[{index}].options[{option_index}].description must be a string"
                )
            description = option.get("description")
            description = description.strip() if isinstance(description, str) else ""
            cleaned_options.append({"label": label, "description": description})
        if "multiSelect" in item and not isinstance(item["multiSelect"], bool):
            return None, f"error: questions[{index}].multiSelect must be a boolean"
        questions.append(
            {
                "id": f"q{index + 1}",
                "header": header.strip(),
                "question": question.strip(),
                "options": cleaned_options,
                "multiSelect": item.get("multiSelect") is True,
            }
        )
    return questions, ""


def format_answers(questions: list[dict], answers: object) -> str:
    """The model-facing summary of one answered ask, or the dismiss sentence."""
    selected: dict[str, list[str]] = {}
    if isinstance(answers, list):
        for item in answers:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                continue
            raw = item.get("selected")
            if not isinstance(raw, list):
                continue
            selected[item["id"]] = [label for label in raw if isinstance(label, str)]
    lines: list[str] = []
    answered = 0
    for question in questions:
        picks: list[str] = []
        for label in selected.get(question["id"], []):
            label = " ".join(label.split())
            if label and label not in picks:
                picks.append(label)
        if not question["multiSelect"]:
            picks = picks[:1]
        header = question["header"] or question["question"]
        if picks:
            answered += 1
            lines.append(f"- {header}: {', '.join(picks)}")
        else:
            lines.append(f"- {header}: (left unanswered — don't assume a choice)")
    if answered == 0:
        return ASK_DISMISSED
    return "The user answered:\n" + "\n".join(lines)


def decision_id_for(questions: list[dict]) -> str:
    """Stable id for one ambiguity: headers, question text, and option labels."""
    digest = hashlib.sha256()
    for question in questions:
        digest.update(question["header"].strip().lower().encode())
        digest.update(b"\0")
        digest.update(question["question"].strip().lower().encode())
        digest.update(b"\0")
        for option in question["options"]:
            digest.update(option["label"].strip().lower().encode())
            digest.update(b"\0")
    return "dec-" + digest.hexdigest()[:16]


def reuse_cited(item: dict) -> str:
    return (
        f"Host reused accepted decision {item['id']}. The user already chose: {item['answer']}. "
        "Continue with that decision unless you supply decision_id and new_evidence."
    )


def reuse_matched(item: dict) -> str:
    return (
        f"Host reused accepted decision {item['id']} for the same ambiguity. "
        f"The user already chose: {item['answer']}. "
        f"Continue with that decision; to reopen it, cite decision_id {item['id']} and supply new_evidence."
    )


class DecisionStore:
    """Accepted ask answers, kept beside the transcript so a summary cannot drop them."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.path = directory / "decisions.json"
        self.items: list[dict] = []

    def load(self) -> None:
        self.items = []
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return
        rows = raw.get("decisions") if isinstance(raw, dict) else None
        if not isinstance(rows, list):
            return
        for row in rows:
            if not isinstance(row, dict):
                continue
            ident = row.get("id")
            key = row.get("key")
            answer = row.get("answer")
            if not isinstance(ident, str) or not isinstance(key, str) or not isinstance(answer, str):
                continue
            ident = ident.strip()
            key = key.strip()
            if ident and key and answer.strip():
                self.items.append({"id": ident, "key": key, "answer": answer})

    def save(self) -> None:
        """Write the whole list through ``decisions.json.tmp``, then replace the file.

        A write that fails removes its temporary file before the error leaves, so
        the directory keeps no half-written copy.
        """
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = self.directory / "decisions.json.tmp"
        payload = {"decisions": self.items}
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, indent=2))
                handle.write("\n")
            temporary.replace(self.path)
        except OSError:
            try:
                temporary.unlink()
            except OSError:
                pass
            raise

    def clear(self) -> None:
        self.items = []
        if self.path.exists():
            self.path.unlink()
        temporary = self.directory / "decisions.json.tmp"
        if temporary.exists():
            temporary.unlink()

    def get(self, ident: str) -> dict | None:
        for item in self.items:
            if item["id"] == ident:
                return item
        return None

    def match_key(self, key: str) -> dict | None:
        for item in self.items:
            if item["key"] == key:
                return item
        return None

    def remember(self, ident: str, key: str, answer: str) -> bool:
        """Keep one answer, then persist the list. ``False`` means the file was not written.

        The user has already answered when this runs, so a failed write keeps the
        answer for this session instead of losing it: the tool result still goes
        back, and only the copy a restart reads is missing.
        """
        kept = [item for item in self.items if item["id"] != ident and item["key"] != key]
        kept.append({"id": ident, "key": key, "answer": answer})
        self.items = kept
        try:
            self.save()
        except OSError:
            return False
        return True


def _ask_reopen(args: dict) -> tuple[str, str, str]:
    """Return ``(decision_id, new_evidence, error)``. ``error`` is empty when the fields are valid.

    A blank ``decision_id`` reads as no id at all: a model that fills every
    property of the schema sends ``""`` for an id it does not have, and that ask
    still belongs to the user.
    """
    explicit = ""
    if "decision_id" in args and args["decision_id"] is not None:
        value = args["decision_id"]
        if not isinstance(value, str):
            return "", "", "error: decision_id must be a string"
        explicit = value.strip()
    evidence = ""
    if "new_evidence" in args and args["new_evidence"] is not None:
        value = args["new_evidence"]
        if not isinstance(value, str):
            return "", "", "error: new_evidence must be a string"
        evidence = value.strip()
    return explicit, evidence, ""


def latest_todos(messages: list[dict]) -> list[dict] | None:
    """The last successful ``todo_write`` list in a transcript, or ``None`` when there is none."""
    found: list[dict] | None = None
    for message in messages:
        if message.get("role") != "tool" or message.get("name") != "todo_write":
            continue
        content = message.get("content")
        if not isinstance(content, str) or content.startswith("error:"):
            continue
        try:
            value = json.loads(content)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict) or not isinstance(value.get("todos"), list):
            continue
        items: list[dict] = []
        ok = True
        for item in value["todos"]:
            if not isinstance(item, dict):
                ok = False
                break
            text = item.get("content")
            status = item.get("status")
            if not isinstance(text, str) or status not in _TODO_STATUS:
                ok = False
                break
            items.append({"content": text, "status": status})
        if ok:
            found = items
    return found


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
    def __init__(self, workspace: Path, confirm, mcp=None, skills=None, asker=None, decisions=None):
        self.workspace = workspace.resolve()
        self.confirm = confirm
        self.mcp = mcp
        self.skills = skills
        self.asker = asker
        self.skill_runner = None
        self.todos: list[dict] = []
        self.decisions = decisions if decisions is not None else DecisionStore(self.workspace / ".foo-ai")
        if decisions is None:
            self.decisions.load()

    def clear_todos(self) -> None:
        self.todos = []

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
            elif name == "todo_write":
                result = self._todo_write(raw)
            elif name == "ask":
                result = self._ask(raw)
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

    def _todo_write(self, raw: object) -> str:
        args = parse_arguments(raw)
        if isinstance(args, str):
            return args
        if "todos" not in args:
            return "error: todos is required"
        todos = args["todos"]
        if not isinstance(todos, list):
            return "error: todos must be an array"
        cleaned: list[dict] = []
        seen: dict[str, int] = {}
        counts = {"total": 0, "pending": 0, "in_progress": 0, "completed": 0}
        for index, item in enumerate(todos):
            if not isinstance(item, dict):
                return f"error: todos[{index}] must be an object with content and status"
            content = item.get("content")
            if not isinstance(content, str) or content.strip() == "":
                return f"error: todos[{index}].content must be a non-empty string"
            content = content.strip()
            if content in seen:
                return (
                    f"error: todos[{index}].content duplicates todos[{seen[content]}].content {content!r}"
                )
            seen[content] = index
            status = item.get("status")
            if status not in _TODO_STATUS:
                return (
                    f"error: todos[{index}].status {status!r} is invalid; "
                    "use pending, in_progress, or completed"
                )
            cleaned.append({"content": content, "status": status})
            counts[status] += 1
        counts["total"] = len(cleaned)
        self.todos = cleaned
        return json.dumps({"todos": cleaned, "counts": counts}, ensure_ascii=False)

    def _ask(self, raw: object) -> str:
        args = parse_arguments(raw)
        if isinstance(args, str):
            return args
        explicit, evidence, error = _ask_reopen(args)
        if error:
            return error
        questions, error = normalize_questions(args.get("questions"))
        if questions is None:
            return error
        key = decision_id_for(questions)
        if explicit:
            found = self.decisions.get(explicit)
            if found is not None and evidence == "":
                return reuse_cited(found)
            if found is None and self.decisions.items:
                return (
                    f"error: unknown decision_id {explicit!r}; cite the original accepted decision_id "
                    "and include new_evidence to reopen it"
                )
        else:
            found = self.decisions.match_key(key)
            if found is not None:
                return reuse_matched(found)
        if self.asker is None:
            return ASK_NO_USER
        answers = self.asker(questions)
        if answers is None:
            return ASK_NO_USER
        summary = format_answers(questions, answers)
        if summary == ASK_DISMISSED:
            return summary
        stored_id = explicit or key
        # A write that fails must not swallow the answer the user just gave, so
        # the summary goes back either way; only the restart copy is missing.
        self.decisions.remember(stored_id, key, summary)
        return summary + "\n\ndecision_id: " + stored_id


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
