"""The desktop session behind the page: agent, event log, approvals, cancel.

``server`` speaks HTTP. This module owns what ``cli.main`` owns in the console
program: the provider, the MCP pool, the skills, the transcript, and the agent.
One worker thread runs a request, and the sink turns each stream event into an
entry in a growing log the page reads with ``GET /api/events``.

``ask`` emits ``kind: "ask"`` with a ``questions`` list and sets ``status`` to
``asking``. ``GET /api/events`` repeats that list as ``question`` until the page
posts ``/api/answer``. The body is ``{"answers": [{"id": "q1", "selected": ["Label"]}]}``.
An empty ``answers`` list dismisses the question and ends the turn. Auto-approve
does not answer it. Stop cancels the turn instead of choosing.

``todo_write`` emits ``kind: "todos"`` with a ``todos`` list of ``content`` and
``status``. ``GET /api/state`` returns the same list as ``todos``.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path

from foo_ai.agent import Agent
from foo_ai.client import ChatError, Client
from foo_ai.config import ConfigError, known_providers, load_config
from foo_ai.mcp import McpPool
from foo_ai.session import Session
from foo_ai.skill import load_skills, match_slash, system_prompt
from foo_ai.tools import Toolbox

_EVENT_LIMIT = 20_000
_WAIT = 0.1
# A round that sends nothing for this long is a stall, not a slow answer: the
# page would otherwise show "working" forever. 0 turns the bound off.
_READ_TIMEOUT = 300.0
_USAGE = re.compile(
    r"^context (?P<used>[0-9.]+[KM]?)(?:/(?P<window>[0-9.]+[KM]?))?"
    r" · usage total (?P<prompt>\d+) (?P<completion>\d+)"
    r"(?: · cache (?P<cache>[0-9.]+)%)?$"
)


class Cancelled(BaseException):
    """The user stopped the request. A tool catches only ``Exception``, so this passes it."""


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _number(text: str | None) -> float:
    """``178.1K`` is 178100.0, ``1M`` is 1000000.0, ``800`` is 800.0."""
    if not text:
        return 0.0
    try:
        if text.endswith("K"):
            return float(text[:-1]) * 1_000
        if text.endswith("M"):
            return float(text[:-1]) * 1_000_000
        return float(text)
    except ValueError:
        return 0.0


def usage_from_line(line: str) -> dict | None:
    """The context line, as the numbers the meter needs. ``None`` for another line."""
    match = _USAGE.match(line)
    if match is None:
        return None
    used = _number(match.group("used"))
    window = _number(match.group("window"))
    cache = match.group("cache")
    return {
        "line": line,
        "used": match.group("used"),
        "window": match.group("window") or "",
        "fraction": min(1.0, max(0.0, used / window)) if window > 0 else 0.0,
        "prompt": int(match.group("prompt")),
        "completion": int(match.group("completion")),
        "cache": float(cache) if cache is not None else None,
    }


class _HubSink:
    """The CLI's printing, turned into events. It is also where Stop takes effect."""

    def __init__(self, hub: "Hub") -> None:
        self.hub = hub

    @property
    def verbose(self) -> bool:
        return self.hub.verbose

    def content(self, text: str) -> None:
        self.hub._check_cancel()
        self.hub._emit("content", text)

    def reasoning(self, text: str) -> None:
        self.hub._check_cancel()
        if self.hub.thinking:
            self.hub._emit("reasoning", text)

    def tool_name(self, name: str) -> None:
        self.hub._emit("tool_name", name)

    def tool_result(self, name: str, result: str) -> None:
        full = self.hub.verbose or name in {"edit_file", "write_file"}
        self.hub._emit("tool_result", result, name=name, full=full, chars=len(result))

    def status(self, text: str) -> None:
        line = text.strip()
        if line == "":
            return
        self.hub._emit("status", line)
        usage = usage_from_line(line)
        if usage is not None:
            self.hub._usage = usage

    def todos(self, items: list[dict]) -> None:
        copied = [dict(item) for item in items]
        with self.hub._lock:
            self.hub._todos = copied
        self.hub._emit("todos", "", todos=copied)


class Hub:
    """One session: the agent, its transcript, and the requests running against it."""

    def __init__(
        self,
        workspace: Path,
        *,
        provider: str | None = None,
        max_steps: int | None = None,
        yes: bool = False,
        verbose: bool = False,
        thinking: bool = False,
        client_factory=None,
        read_timeout: float | None = _READ_TIMEOUT,
    ):
        self.workspace = Path(workspace).expanduser().resolve()
        self.provider_flag = provider
        self.max_steps_flag = max_steps
        self.yes = bool(yes)
        self.verbose = bool(verbose)
        self.thinking = bool(thinking)
        self.client_factory = client_factory
        self.read_timeout = read_timeout
        self.sink = _HubSink(self)
        self.events: list[dict] = []
        self.status = "idle"
        self.config = None
        self.pool: McpPool | None = None
        self.skills = None
        self.session: Session | None = None
        self.agent: Agent | None = None
        self.note: str | None = None
        self.mcp_lines: list[str] = []
        self.skipped: list[str] = []
        self.providers: list[str] = []
        self._lock = threading.RLock()
        self._seq = 0
        self._worker: threading.Thread | None = None
        self._cancelled = threading.Event()
        self._approval: dict | None = None
        self._question: dict | None = None
        self._todos: list[dict] = []
        self._usage: dict | None = None

    # ------------------------------------------------------------- lifecycle

    def start(self) -> None:
        """Load the configuration, start the MCP servers, and read the session."""
        self._build()
        for line in self.skipped:
            self._emit("notice", line.strip())
        if self.note:
            self._emit("notice", self.note)
        for line in self.mcp_lines:
            self._emit("notice", line)
        self._emit(
            "notice",
            f"Foo AI · {self.config.provider.name} · {self.config.provider.model}",
        )
        self._emit("notice", f"Workspace {self.workspace}")

    def close(self) -> None:
        self.cancel()
        worker = self._worker
        if worker is not None:
            worker.join(timeout=5)
        pool = self.pool
        self.pool = None
        if pool is not None:
            pool.close()

    def _build(self, workspace: Path | None = None) -> None:
        """Build a whole session for one workspace, then swap it in.

        Nothing is assigned until every step worked, so a failure leaves the
        session that was already loaded in place.
        """
        target = self.workspace if workspace is None else workspace
        config = load_config(self.provider_flag, workspace=target)
        pool = McpPool(config.mcp_servers)
        try:
            mcp_lines = pool.connect()
            skills = load_skills(target)
            session = Session(target, prompt=system_prompt(skills))
            note = session.load()
            agent = Agent(
                provider=config.provider,
                session=session,
                toolbox=Toolbox(target, self.confirm, pool, skills, asker=self.ask_user),
                client=self._make_client(config.provider),
                sink=self.sink,
                max_steps=self._budget(config),
            )
        except BaseException:
            pool.close()
            raise
        old, self.pool = self.pool, pool
        self.workspace = target
        self.config = config
        self.skills = skills
        self.session = session
        self.note = note
        self.mcp_lines = mcp_lines
        self.skipped = list(skills.skipped)
        self.agent = agent
        self.providers = self._providers()
        self._usage = None
        if old is not None:
            old.close()

    def _budget(self, config) -> int:
        return self.max_steps_flag if self.max_steps_flag is not None else config.max_steps

    def _make_client(self, provider):
        """The client for a provider: the caller's factory, else one with the stall bound."""
        if self.client_factory is not None:
            return self.client_factory(provider)
        return Client(provider, timeout=self.read_timeout)

    # ---------------------------------------------------------------- events

    def _emit(self, kind: str, text: str = "", **extra) -> dict:
        with self._lock:
            self._seq += 1
            event = {"seq": self._seq, "kind": kind, "text": text}
            event.update(extra)
            self.events.append(event)
            if len(self.events) > _EVENT_LIMIT:
                del self.events[: len(self.events) - _EVENT_LIMIT]
            return event

    def _check_cancel(self) -> None:
        if self._cancelled.is_set():
            raise Cancelled()

    def events_after(self, after: int) -> dict:
        with self._lock:
            events = [dict(event) for event in self.events if event["seq"] > after]
            return {
                "events": events,
                "status": self.status,
                "usage": self._usage,
                "approval": None if self._approval is None else self._approval["command"],
                "question": None if self._question is None else self._question["questions"],
                "seq": self._seq,
            }

    def state(self) -> dict:
        with self._lock:
            provider = self.config.provider if self.config is not None else None
            session = self.session
            messages = 0 if session is None else max(0, len(session.messages) - 1)
            return {
                "workspace": str(self.workspace),
                "provider": None
                if provider is None
                else {
                    "name": provider.name,
                    "model": provider.model,
                    "base_url": provider.base_url,
                    "context_window": provider.context_window,
                },
                "providers": list(self.providers),
                "max_steps": None if self.agent is None else self.agent.max_steps,
                "skills": []
                if self.skills is None
                else [
                    {"name": skill.name, "run": skill.run, "description": skill.description}
                    for skill in self.skills.entries
                ],
                "skipped": list(self.skipped),
                "mcp": list(self.mcp_lines),
                "note": self.note,
                "session": {
                    "file": ".foo-ai/session.jsonl",
                    "messages": messages,
                },
                "status": self.status,
                "verbose": self.verbose,
                "thinking": self.thinking,
                "yes": self.yes,
                "usage": self._usage,
                "todos": [dict(item) for item in self._todos],
            }

    def _providers(self) -> list[str]:
        try:
            return known_providers()
        except ConfigError:
            return []

    def history(self) -> list[dict]:
        """The resumed transcript, as the cards the page draws on load."""
        with self._lock:
            session = self.session
            if session is None:
                return []
            items: list[dict] = []
            for message in session.messages:
                role = message.get("role")
                if role == "user":
                    text = _text(message.get("content"))
                    if text.startswith("[todo] "):
                        continue
                    items.append({"role": "user", "text": text})
                elif role == "assistant":
                    calls = [
                        call["function"]["name"]
                        for call in message.get("tool_calls") or []
                        if isinstance(call, dict)
                        and isinstance(call.get("function"), dict)
                        and isinstance(call["function"].get("name"), str)
                    ]
                    items.append(
                        {
                            "role": "assistant",
                            "text": _text(message.get("content")),
                            "reasoning": _text(message.get("reasoning_content")),
                            "tools": calls,
                        }
                    )
                elif role == "tool":
                    items.append(
                        {
                            "role": "tool",
                            "name": _text(message.get("name")),
                            "text": _text(message.get("content")),
                        }
                    )
            return items

    # -------------------------------------------------------------- requests

    def ask(self, text: str) -> dict:
        text = text.strip()
        if text == "":
            return {"ok": False, "error": "the prompt is empty"}
        return self._launch(lambda: self.agent.ask(text), prompt=text)

    def slash(self, command: str) -> dict:
        command = command.strip()
        matched = match_slash(command, self.skills)
        if matched is None:
            return {"ok": False, "error": f"unknown command {command!r}"}
        name, arguments = matched
        return self._launch(
            lambda: self.agent.run_slash(command, name, arguments),
            prompt=command,
        )

    def _launch(self, call, prompt: str) -> dict:
        with self._lock:
            if self.agent is None:
                return {"ok": False, "error": "the session is not loaded"}
            if self.status != "idle":
                return {"ok": False, "error": f"a request is already {self.status}"}
            self.status = "running"
            self._cancelled.clear()
            self._approval = None
            self._question = None
            self._emit("user", prompt)
            self._emit("start", "")
            worker = threading.Thread(target=self._work, args=(call,), name="foo-ai-request")
            worker.daemon = True
            self._worker = worker
        worker.start()
        return {"ok": True}

    def _work(self, call) -> None:
        try:
            call()
        except Cancelled:
            self._emit("status", "Interrupted.")
        except ChatError as exc:
            self._emit("error", str(exc))
        except Exception as exc:
            self._emit("error", f"{type(exc).__name__}: {exc}")
        finally:
            with self._lock:
                self.status = "idle"
                self._worker = None
                self._approval = None
                self._question = None
            self._emit("done", "")

    def cancel(self) -> dict:
        with self._lock:
            if self.status == "idle":
                return {"ok": False, "error": "nothing is running"}
            self._cancelled.set()
            pending = self._approval
            question = self._question
        if pending is not None:
            pending["answer"] = False
            pending["event"].set()
        if question is not None:
            question["answers"] = None
            question["event"].set()
        return {"ok": True}

    def approve(self, answer: bool) -> dict:
        with self._lock:
            pending = self._approval
        if pending is None:
            return {"ok": False, "error": "no command is waiting for an answer"}
        pending["answer"] = bool(answer)
        pending["event"].set()
        return {"ok": True}

    def confirm(self, command: str) -> bool:
        """The shell tool's gate. It blocks this request until the page answers."""
        if self.yes:
            self._emit("confirm", command, auto=True)
            return True
        self._check_cancel()
        pending = {"command": command, "event": threading.Event(), "answer": False}
        with self._lock:
            self._approval = pending
            self.status = "approving"
        self._emit("confirm", command)
        try:
            while not pending["event"].wait(_WAIT):
                self._check_cancel()
            return bool(pending["answer"])
        finally:
            with self._lock:
                self._approval = None
                if self.status == "approving":
                    self.status = "running"
            self._emit("confirm_done", "allowed" if pending["answer"] else "denied")

    def answer(self, answers: object) -> dict:
        """Resolve a pending ``ask``. An empty list dismisses it."""
        if not isinstance(answers, list):
            return {"ok": False, "error": "answers must be a list"}
        cleaned: list[dict] = []
        for item in answers:
            if not isinstance(item, dict):
                return {"ok": False, "error": "each answer needs an id and a selected list"}
            selected = item.get("selected")
            if not isinstance(item.get("id"), str) or not isinstance(selected, list):
                return {"ok": False, "error": "each answer needs an id and a selected list"}
            if not all(isinstance(label, str) for label in selected):
                return {"ok": False, "error": "each answer needs an id and a selected list"}
            cleaned.append({"id": item["id"], "selected": list(selected)})
        with self._lock:
            pending = self._question
        if pending is None:
            return {"ok": False, "error": "no question is waiting for an answer"}
        pending["answers"] = cleaned
        pending["event"].set()
        return {"ok": True}

    def ask_user(self, questions: list[dict]) -> list[dict] | None:
        """The ask tool's gate. ``None`` means Stop; ``[]`` means the user dismissed it.

        Auto-approve does not answer this. The page posts ``/api/answer``.
        """
        self._check_cancel()
        pending = {
            "questions": [dict(question) for question in questions],
            "event": threading.Event(),
            "answers": None,
        }
        with self._lock:
            self._question = pending
            self.status = "asking"
        self._emit("ask", "", questions=pending["questions"])
        try:
            while not pending["event"].wait(_WAIT):
                self._check_cancel()
            self._check_cancel()
            answers = pending["answers"]
            return [] if answers is None else answers
        finally:
            with self._lock:
                self._question = None
                if self.status == "asking":
                    self.status = "running"
            self._emit("ask_done", "")

    # ------------------------------------------------------------- settings

    def set_settings(
        self,
        *,
        verbose: bool | None = None,
        thinking: bool | None = None,
        yes: bool | None = None,
    ) -> dict:
        with self._lock:
            if verbose is not None:
                self.verbose = bool(verbose)
            if thinking is not None:
                self.thinking = bool(thinking)
            if yes is not None:
                self.yes = bool(yes)
            return {
                "ok": True,
                "verbose": self.verbose,
                "thinking": self.thinking,
                "yes": self.yes,
            }

    def set_provider(self, name: str) -> dict:
        name = name.strip()
        if name == "":
            return {"ok": False, "error": "the provider name is empty"}
        with self._lock:
            if self.status != "idle":
                return {"ok": False, "error": "a request is running"}
            try:
                config = load_config(name, workspace=self.workspace)
            except ConfigError as exc:
                return {"ok": False, "error": str(exc)}
            self.config = config
            self.provider_flag = config.provider.name
            self.agent.provider = config.provider
            self.agent.client = self._make_client(config.provider)
            self.agent.max_steps = self._budget(config)
            self._usage = None
            provider = config.provider
        self._emit("notice", f"Provider {provider.name} · {provider.model}")
        return {"ok": True}

    def set_workspace(self, raw: str) -> dict:
        raw = raw.strip()
        if raw == "":
            return {"ok": False, "error": "the workspace path is empty"}
        try:
            resolved = Path(raw).expanduser().resolve()
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        if not resolved.is_dir():
            return {"ok": False, "error": f"not a directory: {resolved}"}
        with self._lock:
            if self.status != "idle":
                return {"ok": False, "error": "a request is running"}
            if resolved == self.workspace:
                return {"ok": True}
            try:
                self._build(resolved)
            except ConfigError as exc:
                return {"ok": False, "error": str(exc)}
        for line in self.skipped:
            self._emit("notice", line.strip())
        if self.note:
            self._emit("notice", self.note)
        for line in self.mcp_lines:
            self._emit("notice", line)
        self._emit("notice", f"Workspace {self.workspace}")
        return {"ok": True}

    def reset(self) -> dict:
        with self._lock:
            if self.status != "idle":
                return {"ok": False, "error": "a request is running"}
            note = self.agent.reset()
        self._emit("reset", note)
        return {"ok": True, "note": note}

    def wait(self, timeout: float | None = None) -> str:
        """Block until the running request finishes. The tests use this."""
        worker = self._worker
        if worker is not None:
            worker.join(timeout)
        return self.status
