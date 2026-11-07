"""Small, explicit JSON settings; no credentials or remote endpoints are used."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
from typing import Any, Mapping


def default_data_dir() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    return Path(local) / "DingTalkBridge" if local else Path.home() / ".local" / "share" / "DingTalkBridge"


@dataclass(frozen=True)
class Config:
    data_dir: Path = field(default_factory=default_data_dir)
    db_path: Path | None = None
    allowed_conversations: tuple[str, ...] = ()
    excluded_conversations: tuple[str, ...] = ()
    retention_days: int = 30
    max_tool_results: int = 200

    def __post_init__(self) -> None:
        object.__setattr__(self, "data_dir", Path(os.path.expandvars(str(self.data_dir))).expanduser())
        if self.db_path is not None:
            object.__setattr__(self, "db_path", Path(os.path.expandvars(str(self.db_path))).expanduser())
        for name in ("allowed_conversations", "excluded_conversations"):
            value = getattr(self, name)
            if not isinstance(value, (list, tuple)) or not all(isinstance(item, str) and item for item in value):
                raise ValueError(f"{name} must be a list of nonempty strings")
            object.__setattr__(self, name, tuple(dict.fromkeys(value)))
        for name, maximum in (("retention_days", 36500), ("max_tool_results", 200)):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
                raise ValueError(f"{name} must be an integer between 1 and {maximum}")

    @property
    def database_path(self) -> Path:
        return self.db_path if self.db_path is not None else self.data_dir / "messages.sqlite3"

    def permits(self, conversation_id: str) -> bool:
        return conversation_id not in self.excluded_conversations and (
            not self.allowed_conversations or conversation_id in self.allowed_conversations
        )


def load_config(path: str | Path | None = None) -> Config:
    """Resolve explicit path, process-local config override, then default settings.

    A selected explicit/environment file must exist and be readable. Falling back
    from a broken override could silently split collection and MCP across stores.
    """
    selected = path
    if selected is None:
        selected = os.environ.get("DINGTALKBRIDGE_CONFIG")
        if selected is not None and not selected.strip():
            raise ValueError("DINGTALKBRIDGE_CONFIG must be a nonempty settings path")
    overridden = selected is not None
    settings_path = (
        Path(os.path.expandvars(str(selected))).expanduser()
        if overridden else default_data_dir() / "settings.json"
    )
    if not settings_path.exists():
        if overridden:
            raise ValueError("selected settings file does not exist")
        return Config()
    if not settings_path.is_file():
        raise ValueError("selected settings path must be a file")
    try:
        with settings_path.open("r", encoding="utf-8-sig") as handle:
            values = json.load(handle)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("settings must contain valid UTF-8 JSON") from exc
    except RecursionError as exc:
        raise ValueError("settings exceed the nesting limit") from exc
    except OSError as exc:
        raise ValueError("selected settings file could not be read") from exc
    if not isinstance(values, Mapping):
        raise ValueError("settings must be a JSON object")
    allowed = {"data_dir", "db_path", "allowed_conversations", "excluded_conversations", "retention_days", "max_tool_results"}
    if set(values) - allowed:
        raise ValueError("settings contain unsupported fields: " + ", ".join(sorted(set(values) - allowed)))
    for key in ("data_dir", "db_path"):
        if key in values and values[key] is not None and not isinstance(values[key], str):
            raise ValueError(f"{key} must be a path string")
    if values.get("data_dir") is None and "data_dir" in values:
        raise ValueError("data_dir must be a path string")
    return Config(**dict(values))
