"""Atomic voice channel config persistence.

Uses write-to-tmp + os.replace to eliminate the half-written-file failure mode.
Config path defaults to VOICE_CONFIG_PATH env var (set from Settings at startup),
or can be passed explicitly so tests don't need to invoke get_settings().
"""
from __future__ import annotations

import json
import os
import tempfile
from threading import Lock

_config_lock = Lock()

# Module-level path — set once at startup via set_config_path() or env var.
_CONFIG_PATH: str = os.environ.get("VOICE_CONFIG_PATH", "data/voice_config.json")


def set_config_path(path: str) -> None:
    """Called during app startup to wire the path from Settings."""
    global _CONFIG_PATH
    _CONFIG_PATH = path


def load_config(path: str | None = None) -> dict:
    p = path or _CONFIG_PATH
    try:
        with open(p) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"voice_channel_enabled": False, "after_hours_mode": "reject"}


def save_config(cfg: dict, path: str | None = None) -> None:
    p = path or _CONFIG_PATH
    dir_ = os.path.dirname(p) or "."
    with _config_lock:
        os.makedirs(dir_, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=dir_, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(cfg, f, indent=2)
            os.replace(tmp, p)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def is_voice_enabled() -> bool:
    return bool(load_config().get("voice_channel_enabled", False))
