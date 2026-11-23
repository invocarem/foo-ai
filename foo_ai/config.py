"""Named providers and stdio MCP servers from foo-ai.toml."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from foo_ai.usage import DEFAULT_CONTEXT_WINDOW

DEFAULT_MCP_TIMEOUT_SECONDS = 60


HOME_EXTRA_BODY: dict = {
    "chat_template_kwargs": {
        "reasoning_effort": "max",
        "thinking": True,
    }
}


class ConfigError(ValueError):
    """The selected provider name is not in the catalog."""


@dataclass
class Provider:
    name: str = "home"
    base_url: str = "http://127.0.0.1:8100/v1"
    model: str = "DeepSeek-V4.1-Flash-1017"
    api_key: str = ""
    context_window: int = DEFAULT_CONTEXT_WINDOW
    extra_body: dict = field(default_factory=lambda: _copy_extra(HOME_EXTRA_BODY))

    @property
    def chat_url(self) -> str:
        base = self.base_url.rstrip("/")
        suffix = "/chat/completions"
        if base.endswith(suffix):
            return base
        return base + suffix


def builtin_providers() -> dict[str, Provider]:
    return {
        "home": Provider(),
        "deepseek-flash_1": Provider(
            name="deepseek-flash_1",
            base_url="https://api.deepseek.com/v1",
            model="deepseek-flash",
            api_key="",
            extra_body={},
        ),
    }


def load_provider(path: Path | None = None, name: str | None = None) -> Provider:
    """Load foo-ai.toml over the built-in catalog, then select one provider.

    Selection order: the name argument, FOO_AI_PROVIDER, default_provider in the
    file, then home. FOO_AI_BASE_URL, FOO_AI_MODEL, and FOO_AI_API_KEY then
    override that provider's fields.
    """
    providers, default_name = _catalog(path)
    selected = _text(name) or _text(os.environ.get("FOO_AI_PROVIDER")) or default_name
    provider = providers.get(selected)
    if provider is None:
        known = ", ".join(sorted(providers))
        raise ConfigError(f"unknown provider {selected!r}; known providers: {known}")
    if base_url := _text(os.environ.get("FOO_AI_BASE_URL")):
        provider.base_url = base_url
    if model := _text(os.environ.get("FOO_AI_MODEL")):
        provider.model = model
    if "FOO_AI_API_KEY" in os.environ:
        provider.api_key = os.environ["FOO_AI_API_KEY"]
    return provider


def _catalog(path: Path | None) -> tuple[dict[str, Provider], str]:
    providers = builtin_providers()
    default_name = "home"
    config_path = path if path is not None else Path.cwd() / "foo-ai.toml"
    if not config_path.is_file():
        return providers, default_name
    with config_path.open("rb") as handle:
        raw = tomllib.load(handle)
    if isinstance(raw.get("default_provider"), str) and _text(raw.get("default_provider")):
        default_name = _text(raw.get("default_provider"))
    _overlay_table(providers, raw.get("providers"))
    legacy = raw.get("provider")
    if isinstance(legacy, dict):
        legacy_name = _text(legacy.get("name")) or default_name
        _apply_section(providers, legacy_name, legacy)
        if "default_provider" not in raw:
            default_name = legacy_name
    return providers, default_name


def _overlay_table(providers: dict[str, Provider], table: object) -> None:
    if not isinstance(table, dict):
        return
    for key, section in table.items():
        if isinstance(section, dict):
            _apply_section(providers, str(key), section)


def _apply_section(providers: dict[str, Provider], name: str, section: dict) -> None:
    provider = providers.get(name)
    if provider is None:
        provider = Provider(name=name)
        providers[name] = provider
    if base_url := _text(section.get("base_url")):
        provider.base_url = base_url
    if model := _text(section.get("model")):
        provider.model = model
    if "api_key" in section and section["api_key"] is not None:
        provider.api_key = str(section["api_key"])
    if "context_window" in section:
        provider.context_window = _count(section.get("context_window"))
    extra = section.get("extra_body")
    if isinstance(extra, dict):
        provider.extra_body = extra


@dataclass
class McpServer:
    name: str
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str = ""
    timeout_seconds: int = DEFAULT_MCP_TIMEOUT_SECONDS


def load_mcp_servers(path: Path | None = None) -> list[McpServer]:
    """Read `[mcp.<name>]` tables from foo-ai.toml. A missing file is no servers."""
    config_path = path if path is not None else Path.cwd() / "foo-ai.toml"
    if not config_path.is_file():
        return []
    with config_path.open("rb") as handle:
        raw = tomllib.load(handle)
    return _mcp_servers(raw.get("mcp"))


def _mcp_servers(table: object) -> list[McpServer]:
    if table is None:
        return []
    if not isinstance(table, dict):
        raise ConfigError("mcp must be a table of servers, for example [mcp.whitaker]")
    servers: list[McpServer] = []
    for name, section in table.items():
        if not isinstance(section, dict):
            raise ConfigError(f"mcp {name}: expected a table with command and args")
        # Off unless this server is explicitly enabled. A listed server stays configured and idle.
        if section.get("enabled") is not True:
            continue
        if "command" not in section:
            raise ConfigError(f"mcp {name}: command is required")
        label = f"mcp {name}"
        servers.append(
            McpServer(
                name=str(name),
                command=_text(section.get("command")),
                args=_string_list(section.get("args"), label),
                env=_env_table(section.get("env"), label),
                cwd=_text(section.get("cwd")),
                timeout_seconds=_timeout(section.get("timeout_seconds"), label),
            )
        )
    return servers


def _copy_extra(body: dict) -> dict:
    kwargs = dict(body.get("chat_template_kwargs") or {})
    return {"chat_template_kwargs": kwargs}


def _text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _string_list(value: object, label: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list) or any(isinstance(item, bool) or not isinstance(item, str | int | float) for item in value):
        raise ConfigError(f"{label}: args must be an array of strings")
    return [str(item) for item in value]


def _env_table(value: object, label: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{label}: env must be a table of strings")
    env: dict[str, str] = {}
    for key, item in value.items():
        if item is None:
            continue
        env[str(key)] = str(item)
    return env


def _timeout(value: object, label: str) -> int:
    if value is None:
        return DEFAULT_MCP_TIMEOUT_SECONDS
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(f"{label}: timeout_seconds must be a positive number of seconds")
    seconds = int(value)
    if seconds <= 0:
        raise ConfigError(f"{label}: timeout_seconds must be a positive number of seconds")
    return seconds


def _count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return 0
    if isinstance(value, str):
        text = value.strip().replace("_", "")
        if not text.isdigit():
            return 0
        return int(text)
    return max(0, int(value))
