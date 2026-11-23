"""Provider defaults for the home DeepSeek server."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


DEFAULT_EXTRA_BODY: dict = {
    "chat_template_kwargs": {
        "reasoning_effort": "max",
        "thinking": True,
    }
}


@dataclass
class Provider:
    name: str = "home"
    base_url: str = "http://100.109.56.33:81/v1"
    model: str = "DeepSeek-V4.1-Flash-1017"
    api_key: str = ""
    extra_body: dict = field(default_factory=lambda: _copy_extra(DEFAULT_EXTRA_BODY))

    @property
    def chat_url(self) -> str:
        return self.base_url.rstrip("/") + "/chat/completions"


def _copy_extra(body: dict) -> dict:
    kwargs = dict(body.get("chat_template_kwargs") or {})
    return {"chat_template_kwargs": kwargs}


def load_provider(path: Path | None = None) -> Provider:
    """Load cwd foo-ai.toml when present, then apply environment overrides."""
    provider = Provider()
    config_path = path if path is not None else Path.cwd() / "foo-ai.toml"
    if config_path.is_file():
        with config_path.open("rb") as handle:
            raw = tomllib.load(handle)
        section = raw.get("provider") or {}
        if name := _text(section.get("name")):
            provider.name = name
        if base_url := _text(section.get("base_url")):
            provider.base_url = base_url
        if model := _text(section.get("model")):
            provider.model = model
        if "api_key" in section and section["api_key"] is not None:
            provider.api_key = str(section["api_key"])
        extra = section.get("extra_body")
        if isinstance(extra, dict):
            provider.extra_body = extra

    if name := _text(os.environ.get("FOO_AI_PROVIDER")):
        provider.name = name
    if base_url := _text(os.environ.get("FOO_AI_BASE_URL")):
        provider.base_url = base_url
    if model := _text(os.environ.get("FOO_AI_MODEL")):
        provider.model = model
    if "FOO_AI_API_KEY" in os.environ:
        provider.api_key = os.environ["FOO_AI_API_KEY"]
    return provider


def _text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()
