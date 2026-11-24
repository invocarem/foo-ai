"""Providers and MCP tables from the built-in catalog and foo-ai.toml."""

from __future__ import annotations

import copy
import math
import os
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
_HOME_EXTRA = {"chat_template_kwargs": {"reasoning_effort": "max", "thinking": True}}


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


def _load_file(catalog: dict[str, Provider], path: Path) -> tuple[str, dict, int | None]:
    default_name = "home"
    data = _read_toml(path)
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
        if named:
            if "default_provider" not in data:
                default_name = named
            target = named
        else:
            target = default_name
        _apply_fields(_ensure(catalog, target), legacy)
    return default_name, _mcp_table(data), _parse_max_steps(data, None)


def load_config(
    provider_flag: str | None = None,
    directory: Path | None = None,
    workspace: Path | None = None,
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
        default_name, servers, file_steps = _load_file(catalog, path)
        if file_steps is not None:
            max_steps = file_steps
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
