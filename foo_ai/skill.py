"""Skill files, the run_skill tool, and subagent runs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from foo_ai.prompt import SYSTEM_PROMPT
from foo_ai.session import Session

_NAME = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
_KEY = re.compile(r"^[a-z][a-z0-9-]*$")
_RESERVED = {"exit", "quit", "reset"}
_TOOL_ORDER = ("read_file", "list_dir", "glob", "grep", "write_file", "edit_file", "shell")
_DEFAULT_TOOLS = ("read_file", "list_dir", "glob", "grep")
_FORBIDDEN = set("|;&<>`$()\r\n")
_SCALAR_KEYS = ("name", "description", "run", "task")

RUN_SKILL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "run_skill",
        "description": (
            "Run a skill from the Skills list in the system prompt. "
            "Pass name as the skill identifier. "
            "A subagent skill requires arguments: that text is the only task it sees. "
            "An inline skill returns its playbook, with arguments appended when you pass them."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Skill identifier from the Skills list."},
                "arguments": {
                    "type": "string",
                    "description": "Task for a subagent skill, or extra input for an inline skill.",
                },
            },
            "required": ["name"],
        },
    },
}


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    run: str
    body: str
    tools: tuple[str, ...]
    shell: tuple[str, ...]
    task: str
    path: str


@dataclass
class SkillCatalog:
    entries: list[Skill]
    skipped: list[str]

    def get(self, name: str) -> Skill | None:
        for skill in self.entries:
            if skill.name == name:
                return skill
        return None


class MemorySession:
    """In-memory transcript. save() does not create a file."""

    def __init__(self, prompt: str):
        self.messages = [{"role": "system", "content": prompt}]

    def save(self) -> None:
        return None

    repair = Session.repair
    add_user = Session.add_user
    add_assistant = Session.add_assistant
    add_tool = Session.add_tool


class ChildSink:
    def __init__(self, parent, name: str, verbose: bool):
        self.parent = parent
        self.name = name
        self.verbose = verbose

    def content(self, text: str) -> None:
        return None

    def reasoning(self, text: str) -> None:
        return None

    def tool_name(self, name: str) -> None:
        if self.verbose and self.parent is not None:
            self.parent.content(f"\n[{self.name}] → {name}")

    def tool_result(self, name: str, result: str) -> None:
        if self.verbose and self.parent is not None:
            self.parent.content(f"\n[{self.name}] {result}\n")

    def status(self, text: str) -> None:
        if self.parent is None:
            return
        if text.startswith("\n"):
            text = "\n" + f"[{self.name}] " + text[1:]
        else:
            text = f"[{self.name}] " + text
        self.parent.status(text)


class RestrictedToolbox:
    def __init__(self, inner, tools: tuple[str, ...], shell: tuple[str, ...]):
        self.inner = inner
        self.allowed = set(tools)
        self.shell = shell
        self.skills = None
        self.skill_runner = None
        self.workspace = inner.workspace
        self.confirm = inner.confirm

    def schemas(self) -> list[dict]:
        from foo_ai.tools import TOOL_SCHEMAS

        return [item for item in TOOL_SCHEMAS if item["function"]["name"] in self.allowed]

    def run(self, name: str, raw: object) -> str:
        from foo_ai.tools import cap_result

        try:
            if name not in self.allowed:
                result = f"error: unknown tool {name!r}"
            elif name == "shell":
                result = self._shell(raw)
            else:
                return self.inner.run(name, raw)
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            result = f"error: {exc}"
        return cap_result(result)

    def _shell(self, raw: object) -> str:
        from foo_ai.tools import parse_arguments

        args = parse_arguments(raw)
        if isinstance(args, str):
            return args
        command = args.get("command")
        if not isinstance(command, str) or command.strip() == "":
            return "error: command is required"
        command = command.strip()
        if not command_allowed(command, self.shell):
            return "error: command is not allowed for this skill"
        return self.inner.execute_shell(command)


def builtin_root() -> Path:
    return Path(__file__).resolve().parent / "skills"


def system_prompt(catalog: SkillCatalog) -> str:
    if not catalog.entries:
        return SYSTEM_PROMPT
    lines = [f"- {skill.name} ({skill.run}): {skill.description}" for skill in catalog.entries]
    return SYSTEM_PROMPT + "\n\nSkills:\n" + "\n".join(lines)


def skills_banner(catalog: SkillCatalog) -> str | None:
    if not catalog.entries:
        return None
    names = ", ".join(f"/{skill.name}" for skill in catalog.entries)
    return f"Skills: {names}."


def render_inline(body: str, arguments: str) -> str:
    if arguments.strip() == "":
        return body
    return body + "\n\nArguments:\n" + arguments.strip()


def command_allowed(command: str, prefixes: tuple[str, ...]) -> bool:
    if any(char in command for char in _FORBIDDEN):
        return False
    return any(command == prefix or command.startswith(prefix + " ") for prefix in prefixes)


def match_slash(command: str, catalog: SkillCatalog | None) -> tuple[str, str] | None:
    if catalog is None or not command.startswith("/"):
        return None
    parts = command.split(None, 1)
    name = parts[0][1:]
    if catalog.get(name) is None:
        return None
    arguments = parts[1].strip() if len(parts) > 1 else ""
    return name, arguments


def load_skills(workspace: Path, root: Path | None = None) -> SkillCatalog:
    builtin: dict[str, Skill] = {}
    skipped: list[str] = []
    for path, directory_name, display in _skill_files(root or builtin_root(), "foo_ai/skills"):
        skill, reason = _load_file(path, directory_name, display)
        if skill is None:
            skipped.append(f"Skill skipped: {display}: {reason}\n")
            continue
        builtin[skill.name] = skill
    for path, directory_name, display in _skill_files(workspace / ".foo-ai" / "skills", ".foo-ai/skills"):
        skill, reason = _load_file(path, directory_name, display)
        if skill is None:
            skipped.append(f"Skill skipped: {display}: {reason}\n")
            continue
        builtin[skill.name] = skill
    entries = [builtin[name] for name in sorted(builtin)]
    return SkillCatalog(entries, skipped)


def run_subagent(skill: Skill, arguments: str, *, provider, client, sink, workspace: Path, confirm, max_steps: int) -> str:
    from foo_ai.agent import Agent
    from foo_ai.tools import Toolbox

    session = MemorySession(skill.body)
    verbose = bool(getattr(sink, "verbose", False))
    child_sink = ChildSink(sink, skill.name, verbose)
    inner = Toolbox(workspace, confirm)
    box = RestrictedToolbox(inner, skill.tools, skill.shell)
    agent = Agent(
        provider=provider,
        session=session,
        toolbox=box,
        client=client,
        sink=child_sink,
        max_steps=max_steps,
    )
    return agent.ask(arguments)


def _skill_files(root: Path, display_root: str) -> list[tuple[Path, str, str]]:
    if not root.is_dir():
        return []
    found: list[tuple[Path, str, str]] = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        file = child / "SKILL.md"
        if not file.is_file():
            continue
        display = f"{display_root}/{child.name}/SKILL.md"
        found.append((file, child.name, display))
    found.sort(key=lambda item: item[1])
    return found


def _load_file(path: Path, directory_name: str, display: str) -> tuple[Skill | None, str | None]:
    if not _valid_name(directory_name):
        return None, "invalid skill name"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, "cannot read skill file"
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        return None, "frontmatter is missing"
    try:
        close = lines.index("---", 1)
    except ValueError:
        return None, "frontmatter is not closed"
    scalars, lists, error = _frontmatter(lines[1:close])
    if error:
        return None, error
    for key in _SCALAR_KEYS:
        if key in lists:
            return None, f"{key} must be a single line"
    name = scalars.get("name", "")
    if name == "":
        return None, "name is required"
    if name != directory_name:
        return None, "name does not match the directory"
    if name in _RESERVED:
        return None, "name is reserved"
    description = scalars.get("description", "")
    if description == "":
        return None, "description is required"
    run = scalars.get("run", "")
    if run not in {"inline", "subagent"}:
        return None, "run must be inline or subagent"
    if "tools" in lists or ("tools" in scalars and scalars["tools"] == ""):
        return None, "tools must be a comma-separated line"
    if "tools" in scalars:
        tools, tool_error = _tools(scalars["tools"])
        if tool_error:
            return None, tool_error
    elif run == "subagent":
        tools = _DEFAULT_TOOLS
    else:
        tools = ()
    if "shell" in scalars:
        return None, "shell must be a list"
    shell, shell_error = _shell_prefixes(lists.get("shell", []))
    if shell_error:
        return None, shell_error
    if "shell" in tools and not shell:
        return None, "shell requires a shell list"
    if "shell" not in tools:
        shell = ()
    body_lines = lines[close + 1 :]
    body = "\n".join(body_lines)
    if body.startswith("\n"):
        body = body[1:]
    body = body.rstrip()
    if body == "":
        return None, "body is empty"
    task = scalars.get("task", "")
    return Skill(name, description, run, body, tools, shell, task, display), None


def _valid_name(name: str) -> bool:
    return 1 <= len(name) <= 64 and _NAME.fullmatch(name) is not None


def _frontmatter(lines: list[str]) -> tuple[dict[str, str], dict[str, list[str]], str | None]:
    scalars: dict[str, str] = {}
    lists: dict[str, list[str]] = {}
    current: str | None = None
    for line in lines:
        if line.strip() == "":
            continue
        if line.startswith("  - "):
            if current is None:
                return {}, {}, "invalid frontmatter line"
            item = line[4:].strip()
            lists.setdefault(current, []).append(item)
            continue
        if line[:1].isspace():
            return {}, {}, "invalid frontmatter line"
        if ":" not in line:
            return {}, {}, "invalid frontmatter line"
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if _KEY.fullmatch(key) is None:
            return {}, {}, "invalid frontmatter line"
        if key in scalars or key in lists:
            return {}, {}, "duplicate frontmatter key"
        current = None
        if value == "":
            current = key
            lists[key] = []
        else:
            scalars[key] = value
    return scalars, lists, None


def _tools(value: str) -> tuple[tuple[str, ...] | None, str | None]:
    tokens = [token.strip() for token in value.split(",")]
    tokens = [token for token in tokens if token]
    if not tokens:
        return None, "tools must be a comma-separated line"
    unknown = [token for token in tokens if token not in _TOOL_ORDER]
    if unknown:
        return None, f"unknown tool {unknown[0]!r}"
    ordered = tuple(name for name in _TOOL_ORDER if name in tokens)
    return ordered, None


def _shell_prefixes(items: list[str]) -> tuple[tuple[str, ...], str | None]:
    prefixes: list[str] = []
    for item in items:
        if item == "":
            return (), "shell prefix is empty"
        if len(item) > 200:
            return (), "shell prefix is too long"
        if any(char in item for char in _FORBIDDEN):
            return (), "shell prefix contains a forbidden character"
        prefixes.append(item)
    return tuple(prefixes), None
