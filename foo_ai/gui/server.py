"""``python -m foo_ai.gui``: serve the desktop page on the loopback interface.

The page is one document with three panes, and this command is the server behind
it. It is the standard library only: ``http.server`` serves the files in
``static`` and answers the small JSON API the page polls. The agent itself is in
``hub``.

Examples
--------

Open the desktop page on this directory::

    python -m foo_ai.gui

Serve a named workspace on a chosen port, without opening a browser::

    python -m foo_ai.gui --workspace ../foo --provider deepseek-flash_1 \\
        --port 8765 --no-browser

The desktop shell in ``desktop/`` starts this command, waits for the
``foo-ai-desktop:`` line, and shows the URL it printed.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from foo_ai import __version__
from foo_ai.config import ConfigError, load_page_workspace, user_config_path
from foo_ai.gui.hub import KEEP_KEY, Hub

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 0
ANNOUNCE = "foo-ai-desktop:"
STATIC = Path(__file__).resolve().parent / "static"
_MAX_BODY = 1 << 20
_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
}
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _whole(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return 0


def _drive_roots() -> list[Path]:
    """Every drive of this machine, for the picker at the top of one of them.

    ``os.listdrives`` knows them on 3.12 and later. An older Windows reads the
    same answer from one kernel call, so a mapped but disconnected drive is not
    opened just to learn whether it is there.
    """
    if os.name != "nt":
        return []
    listed = getattr(os, "listdrives", None)
    if listed is not None:
        try:
            return [Path(name) for name in listed()]
        except OSError:
            return []
    try:
        import ctypes

        mask = ctypes.windll.kernel32.GetLogicalDrives()
    except (AttributeError, OSError):
        return []
    return [Path(f"{chr(ord('A') + index)}:\\") for index in range(26) if mask >> index & 1]


def browse_listing(raw: str, start: Path) -> dict:
    """The folders under one directory, for the workspace picker.

    An empty ``raw`` reads ``start``, the open workspace. A file names its own
    folder, a path that is not there is refused, and a folder that cannot be
    read says why. Only folders are listed, because a workspace is a folder, and
    dot entries such as ``.git`` are left out. The top of a drive offers the
    other drives, so the picker can leave the one it started on.
    """
    text = raw.strip()
    try:
        target = (start if text == "" else Path(text).expanduser()).resolve()
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    if not target.exists():
        return {"ok": False, "error": f"no such folder: {target}"}
    if target.is_file():
        target = target.parent
    if not target.is_dir():
        return {"ok": False, "error": f"not a folder: {target}"}
    try:
        children = [
            child
            for child in target.iterdir()
            if child.is_dir() and not child.name.startswith(".")
        ]
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    entries = [{"name": child.name, "path": str(child)} for child in children]
    parent = target.parent
    top = parent == target
    if top:
        entries.extend(
            {"name": root.drive or str(root), "path": str(root)}
            for root in _drive_roots()
            if root != target
        )
    entries.sort(key=lambda entry: entry["name"].lower())
    return {
        "ok": True,
        "path": str(target),
        "parent": None if top else str(parent),
        "entries": entries,
    }


def resolve_workspace(flag: str | None, user_config: Path | None) -> Path:
    """The folder the page opens: the flag, else the folder it opened last, else here.

    ``--workspace`` names the folder for this run and is not written, the way
    ``--provider`` names a provider for one run. The folder a switch in the page
    wrote is what a launch without the flag comes back to. A saved folder that is
    gone falls through to the current directory, so a deleted project does not
    stop the page from starting.
    """
    if flag:
        return Path(flag).expanduser().resolve()
    saved = load_page_workspace(user_config)
    if saved is not None:
        saved = saved.resolve()
        if saved.is_dir():
            return saved
    return Path.cwd().resolve()


class GuiServer(ThreadingHTTPServer):
    """A loopback HTTP server that carries the hub and the host names it answers to."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], hub: Hub):
        self.hub = hub
        super().__init__(address, Handler)
        host, port = self.server_address[0], self.server_address[1]
        names = set(_LOOPBACK) | {host}
        self.host_names = {f"{name}:{port}" for name in names}


class Handler(BaseHTTPRequestHandler):
    server_version = f"FooAI/{__version__}"
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - the base signature
        """Quiet: the page is the interface, not this process's stderr."""

    # ------------------------------------------------------------ responses

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: object, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _file(self, name: str) -> None:
        if name == "" or "/" in name or "\\" in name or name.startswith("."):
            return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        path = STATIC / name
        if not path.is_file():
            return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        content_type = _TYPES.get(path.suffix, "application/octet-stream")
        self._send(HTTPStatus.OK, path.read_bytes(), content_type)

    # --------------------------------------------------------------- guards

    def _host_ok(self) -> bool:
        """Only the name the server was reached by: a page that rebinds DNS fails here."""
        return self.headers.get("Host", "") in self.server.host_names

    def _origin_ok(self) -> bool:
        """Refuse a write a page of another origin sent to this loopback server."""
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        host = self.headers.get("Host", "")
        return origin in {f"http://{host}", f"https://{host}"}

    def _read_body(self) -> bytes | None:
        """The whole request body. ``None`` means it cannot be read; the caller closes."""
        raw = self.headers.get("Content-Length", "0")
        try:
            size = int(raw)
        except ValueError:
            return None
        if size < 0 or size > _MAX_BODY:
            return None
        return self.rfile.read(size) if size else b""

    def _parse_body(self, data: bytes) -> dict | None:
        if data == b"":
            return {}
        try:
            value = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    # ---------------------------------------------------------------- verbs

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        if not self._host_ok():
            return self._json({"error": "unknown host"}, HTTPStatus.FORBIDDEN)
        url = urlsplit(self.path)
        path = url.path
        if path in {"/", "/index.html"}:
            return self._file("index.html")
        if path.startswith("/static/"):
            return self._file(path[len("/static/") :])
        if path == "/favicon.svg":
            return self._file("favicon.svg")
        hub = self.server.hub
        if path == "/api/state":
            return self._json(hub.state())
        if path == "/api/history":
            return self._json({"messages": hub.history()})
        if path == "/api/events":
            after = _whole(parse_qs(url.query).get("after", ["0"])[0])
            return self._json(hub.events_after(after))
        if path == "/api/browse":
            wanted = parse_qs(url.query).get("path", [""])[0]
            return self._json(browse_listing(wanted, hub.workspace))
        return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        # Read the body before any answer: a refused write still has to drain it,
        # or the unread bytes are parsed as the next request on a kept-alive socket.
        raw = self._read_body()
        if raw is None:
            self.close_connection = True
            return self._json({"error": "expected a JSON object"}, HTTPStatus.BAD_REQUEST)
        if not self._host_ok():
            return self._json({"error": "unknown host"}, HTTPStatus.FORBIDDEN)
        if not self._origin_ok():
            return self._json({"error": "cross-origin request refused"}, HTTPStatus.FORBIDDEN)
        content_type = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if content_type != "application/json":
            return self._json({"error": "expected application/json"}, HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
        body = self._parse_body(raw)
        if body is None:
            return self._json({"error": "expected a JSON object"}, HTTPStatus.BAD_REQUEST)
        path = urlsplit(self.path).path
        hub = self.server.hub
        if path == "/api/ask":
            return self._json(hub.ask(_text(body.get("text"))))
        if path == "/api/slash":
            return self._json(hub.slash(_text(body.get("command"))))
        if path == "/api/reset":
            return self._json(hub.reset())
        if path == "/api/cancel":
            return self._json(hub.cancel())
        if path == "/api/approve":
            return self._json(hub.approve(body.get("approve") is True))
        if path == "/api/answer":
            return self._json(hub.answer(body.get("answers")))
        if path == "/api/settings":
            return self._json(
                hub.set_settings(
                    verbose=body.get("verbose") if isinstance(body.get("verbose"), bool) else None,
                    thinking=body.get("thinking") if isinstance(body.get("thinking"), bool) else None,
                    yes=body.get("yes") if isinstance(body.get("yes"), bool) else None,
                )
            )
        if path == "/api/notify":
            enabled = body.get("enabled")
            topic = body.get("topic", "")
            if not isinstance(enabled, bool):
                return self._json({"ok": False, "error": "enabled must be true or false"})
            if not isinstance(topic, str):
                return self._json({"ok": False, "error": "topic must be a string"})
            return self._json(hub.set_notify(enabled, topic))
        if path == "/api/notify/test":
            return self._json(hub.send_test())
        if path == "/api/provider":
            return self._json(hub.set_provider(_text(body.get("name"))))
        if path == "/api/provider/save":
            return self._json(
                hub.save_provider(
                    _text(body.get("name")),
                    base_url=_text(body.get("base_url")),
                    model=_text(body.get("model")),
                    context_window=body.get("context_window"),
                    api_key=body["api_key"] if "api_key" in body else KEEP_KEY,
                    chat_template_kwargs=body.get("chat_template_kwargs"),
                )
            )
        if path == "/api/workspace":
            return self._json(hub.set_workspace(_text(body.get("path"))))
        return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="foo-ai-desktop",
        description="Run the Foo AI desktop page on the loopback interface.",
        allow_abbrev=False,
    )
    parser.add_argument("--workspace", default=None, metavar="DIR")
    parser.add_argument("--provider", default=None, metavar="NAME")
    parser.add_argument("--config", default=None, metavar="PATH",
                        help="settings file the page reads last and writes (default: the user's config directory)")
    parser.add_argument("--max-steps", type=int, default=None, metavar="N")
    parser.add_argument("--show-thinking", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--host", default=DEFAULT_HOST, metavar="ADDR")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, metavar="PORT")
    parser.add_argument(
        "--read-timeout",
        type=float,
        default=300.0,
        metavar="SECONDS",
        help="give up on a round that sends nothing for this long; 0 waits forever (default: 300)",
    )
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--version", action="store_true")
    return parser


def make_server(hub: Hub, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> GuiServer:
    """Bind the page. Port 0 asks the system for a free one."""
    return GuiServer((host, port), hub)


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        return code if isinstance(code, int) else 2
    if args.version:
        print(f"Foo AI {__version__}", flush=True)
        return 0
    if args.max_steps is not None and args.max_steps <= 0:
        sys.stderr.write("max_steps must be a positive integer\n")
        return 1
    if not 0 <= args.port < 65536:
        sys.stderr.write(f"port {args.port} is outside 0..65535\n")
        return 1
    if args.read_timeout < 0:
        sys.stderr.write("read_timeout must be zero or a positive number of seconds\n")
        return 1
    user_config = Path(args.config).expanduser() if args.config else user_config_path()
    try:
        workspace = resolve_workspace(args.workspace, user_config)
    except ConfigError as exc:
        sys.stderr.write(f"{exc}\n")
        return 1
    hub = Hub(
        workspace,
        provider=args.provider,
        max_steps=args.max_steps,
        yes=args.yes,
        verbose=args.verbose,
        thinking=args.show_thinking,
        read_timeout=None if args.read_timeout == 0 else args.read_timeout,
        user_config=user_config,
    )
    try:
        hub.start()
    except ConfigError as exc:
        sys.stderr.write(f"{exc}\n")
        return 1
    except OSError as exc:
        sys.stderr.write(f"cannot read the workspace: {exc}\n")
        return 1
    if args.host not in _LOOPBACK:
        sys.stderr.write(
            f"warning: {args.host} serves the page off the loopback interface, "
            "and it has no password\n"
        )
    try:
        server = make_server(hub, args.host, args.port)
    except OSError as exc:
        sys.stderr.write(f"cannot listen on {args.host}:{args.port}: {exc}\n")
        hub.close()
        return 1
    host, port = server.server_address[0], server.server_address[1]
    url = f"http://{args.host}:{port}/"
    print(f"{ANNOUNCE} {url}", flush=True)
    if not args.no_browser:
        threading.Timer(0.6, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("\n")
    finally:
        server.server_close()
        hub.close()
    return 0


def free_port(host: str = DEFAULT_HOST) -> int:
    """A port nothing is listening on yet, for a caller that wants to pick one."""
    with socket.socket() as probe:
        probe.bind((host, 0))
        return probe.getsockname()[1]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
