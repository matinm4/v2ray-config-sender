"""Configuration loading: config.json + environment overrides.

Every tunable lives in config.json. Any value can be overridden at run time
with an environment variable (handy for GitHub Actions ``workflow_dispatch``
inputs) using the ``V2TG_`` prefix and a double underscore for nesting, e.g.

    V2TG_SCHEDULE__BATCH_SIZE=20
    V2TG_RUNTIME__DRY_RUN=true
    V2TG_CHANNELS='[{"username": "@a", "daily_quota": 50}]'
"""

from __future__ import annotations

import copy
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

LOG = logging.getLogger(__name__)

ENV_PREFIX = "V2TG_"

DEFAULTS: dict[str, Any] = {
    "source_file": "working.txt",
    "channels": [],
    "schedule": {
        "window_hours": 24,
        "batch_size": 10,
        "grace_minutes": 10,
        "max_messages_per_run": 6,
        "inter_message_delay_seconds": 5,
        "rebuild_when_exhausted": True,
    },
    "distribution": {"mode": "round_robin", "shuffle": True, "seed": None},
    "cleaning": {
        "aggressive": False,
        "replace_remark": True,
        "remark_template": "{channel}",
        "strip_ad_params": True,
        "extra_ad_keywords": [],
        "extra_ad_param_names": [],
    },
    "cache": {"file": "state/cache.json", "ttl_days": 30, "max_entries": 200000},
    "message": {
        "header": "🌐 <b>V2Ray Configs</b> — {date}\n📦 Batch {batch_index}/{batch_total} • {count} configs\n",
        "footer": "\n🔗 {channel}",
        "code_block": True,
        "max_chars": 4000,
    },
    "runtime": {"dry_run": False, "log_level": "INFO", "state_dir": "state"},
}


@dataclass(frozen=True)
class Channel:
    """A destination channel and how many configs it should receive per window."""

    username: str
    daily_quota: int

    @property
    def key(self) -> str:
        """Stable identifier used in state files (no leading ``@``)."""
        return self.username.lstrip("@").lower()


@dataclass
class Settings:
    raw: dict[str, Any]
    root: Path
    channels: list[Channel] = field(default_factory=list)

    # -- convenience accessors -------------------------------------------------
    @property
    def source_path(self) -> Path:
        return self.root / str(self.raw["source_file"])

    @property
    def state_dir(self) -> Path:
        return self.root / str(self.raw["runtime"]["state_dir"])

    @property
    def cache_path(self) -> Path:
        return self.root / str(self.raw["cache"]["file"])

    @property
    def queue_path(self) -> Path:
        return self.state_dir / "queue.json"

    @property
    def schedule(self) -> dict[str, Any]:
        return self.raw["schedule"]

    @property
    def distribution(self) -> dict[str, Any]:
        return self.raw["distribution"]

    @property
    def cleaning(self) -> dict[str, Any]:
        return self.raw["cleaning"]

    @property
    def cache_cfg(self) -> dict[str, Any]:
        return self.raw["cache"]

    @property
    def message(self) -> dict[str, Any]:
        return self.raw["message"]

    @property
    def dry_run(self) -> bool:
        return bool(self.raw["runtime"]["dry_run"])

    @property
    def total_quota(self) -> int:
        return sum(c.daily_quota for c in self.channels)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _coerce(current: Any, text: str) -> Any:
    """Coerce an env-var string into the type of the value it replaces."""
    text = text.strip()
    if isinstance(current, bool):
        return text.lower() in {"1", "true", "yes", "on"}
    if isinstance(current, int) and not isinstance(current, bool):
        return int(float(text))
    if isinstance(current, float):
        return float(text)
    if isinstance(current, (list, dict)):
        return json.loads(text)
    if current is None:
        # Unknown target type — try JSON, fall back to the raw string.
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    return text


def _apply_env_overrides(cfg: dict[str, Any]) -> dict[str, Any]:
    for env_key, env_val in os.environ.items():
        if not env_key.startswith(ENV_PREFIX) or env_val == "":
            continue
        path = env_key[len(ENV_PREFIX) :].lower().split("__")
        node: Any = cfg
        for part in path[:-1]:
            if not isinstance(node, dict) or part not in node:
                node = None
                break
            node = node[part]
        leaf = path[-1]
        if not isinstance(node, dict) or leaf not in node:
            LOG.warning("ignoring unknown override %s", env_key)
            continue
        try:
            node[leaf] = _coerce(node[leaf], env_val)
        except (ValueError, json.JSONDecodeError) as exc:
            LOG.warning("ignoring malformed override %s: %s", env_key, exc)
    return cfg


def _parse_channels(raw_channels: Any, default_quota: int) -> list[Channel]:
    """Accept a list of dicts, a list of strings, or a comma/newline separated string."""
    if isinstance(raw_channels, str):
        raw_channels = [c for c in raw_channels.replace("\n", ",").split(",") if c.strip()]

    channels: list[Channel] = []
    seen: set[str] = set()
    for item in raw_channels or []:
        if isinstance(item, str):
            username, quota = item.strip(), default_quota
        elif isinstance(item, dict):
            username = str(item.get("username") or item.get("name") or "").strip()
            quota = int(item.get("daily_quota", default_quota))
        else:
            LOG.warning("skipping unrecognised channel entry: %r", item)
            continue

        if not username:
            continue
        if not username.startswith("@") and not username.lstrip("-").isdigit():
            username = "@" + username
        if quota <= 0:
            LOG.warning("channel %s has a non-positive quota; skipping", username)
            continue
        key = username.lstrip("@").lower()
        if key in seen:
            LOG.warning("duplicate channel %s ignored", username)
            continue
        seen.add(key)
        channels.append(Channel(username=username, daily_quota=quota))
    return channels


def load(config_path: str | Path = "config.json", root: str | Path | None = None) -> Settings:
    """Load config.json, merge defaults, then apply ``V2TG_*`` env overrides."""
    config_path = Path(config_path)
    project_root = Path(root) if root is not None else (config_path.parent.resolve() or Path.cwd())

    file_cfg: dict[str, Any] = {}
    if config_path.is_file():
        file_cfg = json.loads(config_path.read_text(encoding="utf-8"))
    else:
        LOG.warning("%s not found — using built-in defaults", config_path)

    cfg = _apply_env_overrides(_deep_merge(DEFAULTS, file_cfg))

    logging.getLogger().setLevel(str(cfg["runtime"]["log_level"]).upper())

    channels = _parse_channels(cfg.get("channels"), int(cfg["schedule"]["batch_size"]) * 10)
    if not channels:
        raise SystemExit(
            "No destination channels configured. Add them to config.json under "
            '"channels", e.g. [{"username": "@mychannel", "daily_quota": 100}].'
        )

    sched = cfg["schedule"]
    if int(sched["window_hours"]) <= 0:
        raise SystemExit("schedule.window_hours must be greater than 0")
    if int(sched["batch_size"]) <= 0:
        raise SystemExit("schedule.batch_size must be greater than 0")

    return Settings(raw=cfg, root=project_root, channels=channels)
