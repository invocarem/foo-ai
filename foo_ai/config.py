"""Providers and MCP tables from the built-in catalog, foo-ai.toml, and the
desktop page's settings file."""

from __future__ import annotations

import copy
import json
import math
import os
import re
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


class ConfigError(Exception):
    """The launch configuration cannot be used."""


@dataclass
class Provider:
    name: str
    base_url: str
    model: str
    context_window: int
    api_key: str
    extra_body: dict


@dataclass
class McpSpec:
    name: str
    command: str
    args: list[str]
    timeout: int
    cwd: str | None
    env: dict[str, str] | None


@dataclass
class Config:
    provider: Provider
    mcp_servers: list[McpSpec] = field(default_factory=list)
    max_steps: int = 200


_HOME_URL = "http://127.0.0.1:8100/v1"
_HOME_MODEL = "DeepSeek-V4.1-Flash-1017"
_CHAT_TEMPLATE = "chat_template_kwargs"
_HOME_EXTRA = {_CHAT_TEMPLATE: {"reasoning_effort": "max", "thinking": True}}
_CONFIG_DIR_ENV = "FOO_AI_CONFIG_DIR"
_CONFIG_FILE = "foo-ai.toml"


def local_reasoning(extra_body: dict) -> dict | None:
    """The local reasoning block a request sends, or ``None`` when it sends none.

    ``home`` carries ``chat_template_kwargs``, so the page offers the reasoning
    fields for it. A provider that carries no such block, which is every cloud
    provider in the catalog, hides the fields and refuses one on a save.
    """
    value = extra_body.get(_CHAT_TEMPLATE)
    return copy.deepcopy(value) if isinstance(value, dict) else None


def merge_reasoning(extra_body: dict, fields: dict) -> dict:
    """The body a request sends, with the settings sheet's reasoning fields edited.

    The sheet shows ``reasoning_effort`` and ``thinking``, so those two keys are
    the ones it owns: a non-blank effort sets the key and a blank one drops it,
    and ``thinking`` becomes the box's value. Every other key of the block, and
    every other key of the body, passes through as the request already sends it.
    """
    body = copy.deepcopy(extra_body)
    block = body.get(_CHAT_TEMPLATE)
    block = dict(block) if isinstance(block, dict) else {}
    effort = fields.get("reasoning_effort")
    if effort:
        block["reasoning_effort"] = effort
    else:
        block.pop("reasoning_effort", None)
    if "thinking" in fields:
        block["thinking"] = fields["thinking"]
    body[_CHAT_TEMPLATE] = block
    return body


def user_config_path() -> Path:
    """The settings file the desktop page reads last and writes.

    ``FOO_AI_CONFIG_DIR`` names its directory. Otherwise it is the user's
    configuration directory: ``%APPDATA%\\foo-ai`` on Windows, and
    ``$XDG_CONFIG_HOME/foo-ai`` or ``~/.config/foo-ai`` elsewhere. The page
    keeps its providers there because a packaged app has no launch directory
    of its own to hold a ``foo-ai.toml``.
    """
    override = os.environ.get(_CONFIG_DIR_ENV, "")
    if isinstance(override, str) and override.strip():
        return Path(override.strip()).expanduser() / _CONFIG_FILE
    if os.name == "nt":
        base = os.environ.get("APPDATA", "")
        root = Path(base.strip()) if base.strip() else Path.home() / "AppData" / "Roaming"
    else:
        base = os.environ.get("XDG_CONFIG_HOME", "")
        root = Path(base.strip()).expanduser() if base.strip() else Path.home() / ".config"
    return root / "foo-ai" / _CONFIG_FILE


def _template(name: str) -> Provider:
    return Provider(
        name=name,
        base_url=_HOME_URL,
        model=_HOME_MODEL,
        context_window=1_000_000,
        api_key="",
        extra_body=copy.deepcopy(_HOME_EXTRA),
    )


def _builtin_catalog() -> dict[str, Provider]:
    return {
        "home": _template("home"),
        "deepseek-flash_1": Provider(
            name="deepseek-flash_1",
            base_url="https://api.deepseek.com/v1",
            model="deepseek-flash",
            context_window=1_000_000,
            api_key="",
            extra_body={},
        ),
    }


def _parse_context_window(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value if value >= 0 else 0
    if isinstance(value, float):
        if not math.isfinite(value) or value < 0:
            return 0
        return int(value)
    if isinstance(value, str):
        digits = value.replace("_", "")
        if digits.isdigit():
            return int(digits)
    return 0


def _apply_fields(provider: Provider, table: dict) -> None:
    if "base_url" in table:
        value = table["base_url"]
        if isinstance(value, str) and value.strip():
            provider.base_url = value
    if "model" in table:
        value = table["model"]
        if isinstance(value, str) and value.strip():
            provider.model = value
    if "api_key" in table and table["api_key"] is not None:
        value = table["api_key"]
        provider.api_key = value if isinstance(value, str) else str(value)
    if "context_window" in table:
        provider.context_window = _parse_context_window(table["context_window"])
    if "extra_body" in table and isinstance(table["extra_body"], dict):
        provider.extra_body = copy.deepcopy(table["extra_body"])


def _ensure(catalog: dict[str, Provider], name: str) -> Provider:
    provider = catalog.get(name)
    if provider is None:
        provider = _template(name)
        catalog[name] = provider
    return provider


def _parse_mcp_args(name: str, value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        args: list[str] = []
        for item in value:
            if isinstance(item, bool) or not isinstance(item, (str, int, float)):
                raise ConfigError(f"mcp {name}: args must be an array of strings")
            args.append(str(item))
        return args
    raise ConfigError(f"mcp {name}: args must be an array of strings")


def _parse_timeout(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"mcp {name}: timeout_seconds must be a positive number of seconds")
    if isinstance(value, float) and not math.isfinite(value):
        raise ConfigError(f"mcp {name}: timeout_seconds must be a positive number of seconds")
    number = int(value)
    if number <= 0:
        raise ConfigError(f"mcp {name}: timeout_seconds must be a positive number of seconds")
    return number


def _parse_env(name: str, value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ConfigError(f"mcp {name}: env must be a table of strings")
    env: dict[str, str] = {}
    for key, item in value.items():
        if item is None:
            continue
        if not isinstance(item, str):
            raise ConfigError(f"mcp {name}: env must be a table of strings")
        env[str(key)] = item
    return env


def _parse_mcp(data: dict) -> list[McpSpec]:
    if "mcp" not in data:
        return []
    table = data["mcp"]
    if not isinstance(table, dict):
        raise ConfigError("mcp must be a table of servers, for example [mcp.whitaker]")
    servers: list[McpSpec] = []
    for name, entry in table.items():
        if not isinstance(entry, dict):
            raise ConfigError(f"mcp {name}: expected a table with command and args")
        if entry.get("enabled") is not True:
            continue
        if "command" not in entry or not isinstance(entry["command"], str):
            raise ConfigError(f"mcp {name}: command is required")
        args = _parse_mcp_args(name, entry["args"]) if "args" in entry else []
        timeout = _parse_timeout(name, entry["timeout_seconds"]) if "timeout_seconds" in entry else 60
        cwd_value = entry.get("cwd")
        cwd = cwd_value if isinstance(cwd_value, str) and cwd_value.strip() else None
        env = _parse_env(name, entry["env"]) if "env" in entry else None
        servers.append(
            McpSpec(
                name=str(name),
                command=entry["command"],
                args=args,
                timeout=timeout,
                cwd=cwd,
                env=env,
            )
        )
    return servers


def _apply_providers(catalog: dict[str, Provider], data: dict) -> str | None:
    """Apply ``default_provider``, ``[providers.*]``, and the legacy ``[provider]``.

    Returns the default name the tables imply, or ``None`` when they name none.
    """
    default_name: str | None = None
    if isinstance(data.get("default_provider"), str) and data["default_provider"].strip():
        default_name = data["default_provider"].strip()
    providers = data.get("providers")
    if providers is not None:
        if not isinstance(providers, dict):
            raise ConfigError("providers must be a table")
        for name, table in providers.items():
            if not isinstance(table, dict):
                raise ConfigError(f"providers.{name}: expected a table")
            _apply_fields(_ensure(catalog, str(name)), table)
    legacy = data.get("provider")
    if legacy is not None:
        if not isinstance(legacy, dict):
            raise ConfigError("provider must be a table")
        raw_name = legacy.get("name")
        named = raw_name.strip() if isinstance(raw_name, str) and raw_name.strip() else ""
        if named and "default_provider" not in data:
            default_name = named
        _apply_fields(_ensure(catalog, named or default_name or "home"), legacy)
    return default_name


def _read_settings(path: Path) -> dict:
    """Read a settings file, naming it in a parse error."""
    try:
        return _read_toml(path)
    except ConfigError as exc:
        text = str(exc)
        if text.startswith("cannot read "):
            raise
        raise ConfigError(f"{path}: {text}") from None


def _apply_user_config(catalog: dict[str, Provider], user_config: Path | None) -> str | None:
    """Apply the settings file the desktop page writes, when it exists.

    Only providers and ``default_provider`` come from it: MCP servers and the
    step budget belong to the launch file and the workspace.
    """
    if user_config is None:
        return None
    try:
        present = user_config.is_file()
    except OSError as exc:
        raise ConfigError(f"cannot read {user_config}: {exc}") from None
    if not present:
        return None
    data = _read_settings(user_config)
    try:
        return _apply_providers(catalog, data)
    except ConfigError as exc:
        raise ConfigError(f"{user_config}: {exc}") from None


def _read_toml(path: Path) -> dict:
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(str(exc)) from None
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from None
    if not isinstance(data, dict):
        raise ConfigError(f"{path.name} must be a table")
    return data


def _mcp_table(data: dict) -> dict:
    if "mcp" not in data:
        return {}
    table = data["mcp"]
    if not isinstance(table, dict):
        raise ConfigError("mcp must be a table of servers, for example [mcp.whitaker]")
    return table


def _merge_mcp(base: dict, overlay: dict) -> dict:
    merged = dict(base)
    for name, entry in overlay.items():
        current = merged.get(name)
        if isinstance(current, dict) and isinstance(entry, dict):
            combined = dict(current)
            combined.update(entry)
            merged[name] = combined
        else:
            merged[name] = entry
    return merged


def _parse_max_steps(data: dict, source: str | None) -> int | None:
    if "max_steps" not in data:
        return None
    value = data["max_steps"]
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        message = "max_steps must be a positive integer"
        if source:
            message = f"{source}: {message}"
        raise ConfigError(message)
    return value


def _workspace_settings(workspace: Path) -> tuple[dict, int | None]:
    path = workspace / ".foo-ai.toml"
    try:
        present = path.is_file()
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from None
    if not present:
        return {}, None
    try:
        data = _read_toml(path)
    except ConfigError as exc:
        text = str(exc)
        if text.startswith("cannot read ") or text.startswith(".foo-ai.toml"):
            raise
        raise ConfigError(f".foo-ai.toml: {text}") from None
    try:
        table = _mcp_table(data)
    except ConfigError as exc:
        raise ConfigError(f".foo-ai.toml: {exc}") from None
    return table, _parse_max_steps(data, ".foo-ai.toml")


def _load_file(catalog: dict[str, Provider], path: Path) -> tuple[str | None, dict, int | None]:
    """Apply one launch ``foo-ai.toml``: its providers, MCP servers, and budget."""
    data = _read_toml(path)
    return _apply_providers(catalog, data), _mcp_table(data), _parse_max_steps(data, None)


def load_config(
    provider_flag: str | None = None,
    directory: Path | None = None,
    workspace: Path | None = None,
    user_config: Path | None = None,
) -> Config:
    catalog = _builtin_catalog()
    default_name = "home"
    servers: dict = {}
    max_steps = 200
    path = (directory if directory is not None else Path.cwd()) / "foo-ai.toml"
    try:
        present = path.is_file()
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from None
    if present:
        named, servers, file_steps = _load_file(catalog, path)
        default_name = named or default_name
        if file_steps is not None:
            max_steps = file_steps
    # The page's own settings come after the launch file: what its Settings
    # sheet shows is what a request sends, even in a repository that has a
    # foo-ai.toml of its own.
    default_name = _apply_user_config(catalog, user_config) or default_name
    if workspace is not None:
        overlay, workspace_steps = _workspace_settings(workspace)
        servers = _merge_mcp(servers, overlay)
        if workspace_steps is not None:
            max_steps = workspace_steps
    parsed = _parse_mcp({"mcp": servers}) if servers else []

    selected = ""
    if isinstance(provider_flag, str) and provider_flag.strip():
        selected = provider_flag.strip()
    else:
        from_env = os.environ.get("FOO_AI_PROVIDER", "")
        if isinstance(from_env, str) and from_env.strip():
            selected = from_env.strip()
        else:
            selected = default_name or "home"
    provider = catalog.get(selected)
    if provider is None:
        known = ", ".join(sorted(catalog))
        raise ConfigError(f"unknown provider {selected!r}; known providers: {known}")

    base_url = os.environ.get("FOO_AI_BASE_URL", "")
    if isinstance(base_url, str) and base_url.strip():
        provider.base_url = base_url
    model = os.environ.get("FOO_AI_MODEL", "")
    if isinstance(model, str) and model.strip():
        provider.model = model
    if "FOO_AI_API_KEY" in os.environ:
        provider.api_key = os.environ["FOO_AI_API_KEY"]
    return Config(provider=provider, mcp_servers=parsed, max_steps=max_steps)


def completions_url(base_url: str) -> str:
    url = base_url[:-1] if base_url.endswith("/") else base_url
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"
    return url


def known_providers(directory: Path | None = None, user_config: Path | None = None) -> list[str]:
    """Every provider name the catalog, the settings file, and the launch file define."""
    catalog = _builtin_catalog()
    _apply_user_config(catalog, user_config)
    path = (directory if directory is not None else Path.cwd()) / "foo-ai.toml"
    try:
        present = path.is_file()
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from None
    if present:
        _load_file(catalog, path)
    return sorted(catalog)


# The settings file is the one document this program writes, so it carries its
# own writer: tomllib only reads. The shapes are the ones the page saves plus
# whatever a hand-written table already holds.


def _toml_string(value: str) -> str:
    """A TOML basic string. JSON's escapes are the ones TOML also accepts."""
    return json.dumps(value, ensure_ascii=False)


def _toml_key(key: str) -> str:
    bare = bool(key) and all(ch.isascii() and (ch.isalnum() or ch in "_-") for ch in key)
    return key if bare else _toml_string(key)


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return _toml_string(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise ConfigError(f"cannot write a {type(value).__name__} to TOML")


def _toml_sections(header: str | None, data: dict, lines: list[str]) -> None:
    """Write one table: its keys first, then a header and body for each nested table.

    A table that holds only nested tables gets no header of its own, because
    the child headers already open it. An empty table keeps one, so it exists.
    """
    scalars = {key: value for key, value in data.items() if not isinstance(value, dict)}
    children = {key: value for key, value in data.items() if isinstance(value, dict)}
    if header is not None and (scalars or not children):
        lines.append(f"[{header}]")
    for key, value in scalars.items():
        lines.append(f"{_toml_key(str(key))} = {_toml_value(value)}")
    for key, value in children.items():
        if lines and lines[-1] != "":
            lines.append("")
        name = _toml_key(str(key))
        _toml_sections(f"{header}.{name}" if header else name, value, lines)


def _toml_document(data: dict) -> str:
    lines: list[str] = []
    _toml_sections(None, data, lines)
    return "\n".join(lines) + "\n"


def _write_settings(path: Path, data: dict) -> None:
    """Write the whole document, private to this user, and atomically."""
    text = _toml_document(data)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f"{path.name}.",
            suffix=".tmp",
            delete=False,
        )
    except OSError as exc:
        raise ConfigError(f"cannot write {path}: {exc}") from None
    try:
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            os.chmod(handle.name, 0o600)
        os.replace(handle.name, path)
    except OSError as exc:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise ConfigError(f"cannot write {path}: {exc}") from None


def write_provider_settings(
    path: Path,
    name: str,
    *,
    base_url: str,
    model: str,
    context_window: int,
    api_key: str | None = None,
    extra_body: dict | None = None,
    replace_extra_body: bool = False,
) -> None:
    """Write one provider into the settings file, keeping its other tables.

    ``api_key=None`` keeps the key already in the file, and ``""`` clears it.
    The name becomes ``default_provider`` so the page reopens on it.

    ``extra_body`` is the whole body the page edited, written over the table's
    own when ``replace_extra_body`` is set. Otherwise it is written only when
    this provider's table does not already have one, and only when it is
    non-empty. That records the body the request already sends, such as
    ``home``'s ``chat_template_kwargs``, without replacing a table someone
    edited by hand. An empty body stays omitted.
    """
    try:
        present = path.is_file()
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from None
    data = _read_settings(path) if present else {}
    providers = data.get("providers")
    if not isinstance(providers, dict):
        providers = {}
    table = providers.get(name)
    if not isinstance(table, dict):
        table = {}
    table["base_url"] = base_url
    table["model"] = model
    table["context_window"] = context_window
    if api_key is not None:
        table["api_key"] = api_key
    if replace_extra_body:
        table["extra_body"] = copy.deepcopy(extra_body) if extra_body else {}
    elif "extra_body" not in table and extra_body:
        table["extra_body"] = copy.deepcopy(extra_body)
    providers[name] = table
    data["providers"] = providers
    data["default_provider"] = name
    _write_settings(path, data)


_PAGE_KEYS = ("thinking", "verbose", "yes")


def load_page_settings(path: Path | None) -> dict[str, bool]:
    """The page's turn options from its settings file: thinking, verbose, yes.

    A missing file, or a missing ``[page]`` table, leaves them all off. Only a
    TOML boolean counts; another value keeps the option off.
    """
    settings = {key: False for key in _PAGE_KEYS}
    if path is None:
        return settings
    try:
        present = path.is_file()
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from None
    if not present:
        return settings
    data = _read_settings(path)
    table = data.get("page")
    if table is None:
        return settings
    if not isinstance(table, dict):
        raise ConfigError(f"{path}: page must be a table")
    for key in _PAGE_KEYS:
        if isinstance(table.get(key), bool):
            settings[key] = table[key]
    return settings


_TOPIC = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def valid_topic(topic: str) -> bool:
    """A ntfy topic: 1 to 64 letters, digits, underscores, or dashes."""
    return _TOPIC.fullmatch(topic) is not None


def load_notify_settings(path: Path | None) -> dict[str, bool | str]:
    """``enabled`` and ``topic`` from the page's ``[notify]`` table.

    A missing file, or a missing table, leaves the setting off and the topic
    empty. Only the boolean ``true`` turns it on. A topic that is not 1 to 64
    letters, digits, underscores, or dashes counts as empty, so a hand-edited
    file never stops a launch.
    """
    settings: dict[str, bool | str] = {"enabled": False, "topic": ""}
    if path is None:
        return settings
    try:
        present = path.is_file()
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from None
    if not present:
        return settings
    data = _read_settings(path)
    table = data.get("notify")
    if table is None:
        return settings
    if not isinstance(table, dict):
        raise ConfigError(f"{path}: notify must be a table")
    if table.get("enabled") is True:
        settings["enabled"] = True
    topic = table.get("topic")
    if isinstance(topic, str) and valid_topic(topic):
        settings["topic"] = topic
    return settings


def save_notify_settings(path: Path, *, enabled: bool, topic: str) -> None:
    """Write ``[notify]``, keeping every other table and any extra key."""
    try:
        present = path.is_file()
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from None
    data = _read_settings(path) if present else {}
    table = data.get("notify")
    if not isinstance(table, dict):
        table = {}
    table["enabled"] = bool(enabled)
    table["topic"] = topic
    data["notify"] = table
    _write_settings(path, data)


def save_page_settings(path: Path, *, thinking: bool, verbose: bool, yes: bool) -> None:
    """Write the page's turn options, keeping the provider tables."""
    try:
        present = path.is_file()
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from None
    data = _read_settings(path) if present else {}
    page = data.get("page")
    if not isinstance(page, dict):
        page = {}
    page["thinking"] = bool(thinking)
    page["verbose"] = bool(verbose)
    page["yes"] = bool(yes)
    data["page"] = page
    _write_settings(path, data)


def load_page_workspace(path: Path | None) -> Path | None:
    """The folder the page opened last, or ``None`` when it saved none.

    ``workspace`` is a top-level key beside ``default_provider``, because it is
    the same kind of value: a folder the page opens with. A value that is not a
    non-blank string counts as none, so a hand-edited file never stops a launch.
    """
    if path is None:
        return None
    try:
        present = path.is_file()
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from None
    if not present:
        return None
    value = _read_settings(path).get("workspace")
    if not isinstance(value, str) or value.strip() == "":
        return None
    return Path(value.strip()).expanduser()


def save_page_workspace(path: Path, workspace: Path) -> None:
    """Write the folder the page opened, keeping every other table."""
    try:
        present = path.is_file()
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from None
    data = _read_settings(path) if present else {}
    data["workspace"] = str(workspace)
    _write_settings(path, data)
