"""Loads config.yaml, with environment variables able to override any
value (env wins over yaml, so you can still use .env / real env vars
for secrets like DISCORD_TOKEN without putting them in the yaml file).

Usage in lily_bot.py:
    from config_loader import get_str, get_int, get_list
    OLLAMA_URL = get_str("ollama.url", "http://127.0.0.1:11434")
"""
import os
from typing import Any, List, Optional

import yaml

_DEFAULT_CONFIG_PATH = os.environ.get("LILY_CONFIG", "config.yaml")


def _deep_get(d: dict, path: str, default: Any = None) -> Any:
    cur: Any = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _load_yaml(path: str) -> dict:
    if not os.path.isfile(path):
        print(f"[Config] ⚠️ {path} not found — using built-in defaults / env vars only.")
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            data = {}
        print(f"[Config] Loaded {path}")
        return data
    except Exception as e:
        print(f"[Config] ❌ Failed to parse {path}: {e} — using defaults.")
        return {}


CONFIG: dict = _load_yaml(_DEFAULT_CONFIG_PATH)


def get(path: str, default: Any = None, env_var: Optional[str] = None) -> Any:
    """Raw untyped getter -- prefer get_str/get_int/get_list below where
    the value has a known expected type, since those give Pylance (and
    you) a real guarantee instead of `Any`."""
    if env_var:
        env_val = os.environ.get(env_var)
        if env_val is not None and env_val != "":
            return env_val
    return _deep_get(CONFIG, path, default)


def get_str(path: str, default: str = "", env_var: Optional[str] = None) -> str:
    val = get(path, default, env_var)
    if val is None:
        return default
    return str(val)


def get_int(path: str, default: int = 0, env_var: Optional[str] = None) -> int:
    val = get(path, default, env_var)
    if val is None:
        return default
    try:
        return int(val)
    except (TypeError, ValueError):
        print(f"[Config] ⚠️ {path!r} = {val!r} is not a valid int — using default {default}")
        return default


def get_list(path: str, default: Optional[List[str]] = None, env_var: Optional[str] = None) -> List[str]:
    if default is None:
        default = []
    val = get(path, default, env_var)
    if val is None:
        return default
    if isinstance(val, list):
        return [str(x) for x in val]
    print(f"[Config] ⚠️ {path!r} = {val!r} is not a list — using default {default}")
    return default