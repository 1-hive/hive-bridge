"""Where a hive keeps things, for the Python tools. hive-runtime has the same file, with the same names and defaults.

Each setting is an environment variable. The defaults are 1-hive's layout, so 1-hive sets none of them.
"""
from __future__ import annotations

import os
from pathlib import Path

HOME = Path.home()


def _path(name: str, default: Path) -> Path:
    return Path(os.environ.get(name) or default)


HIVE_ID = os.environ.get("HIVE_ID") or "1-hive"
GATEWAY_PORT = int(os.environ.get("HIVE_GATEWAY_PORT") or 8470)
HIVE_URL = os.environ.get("HIVE_URL") or f"http://127.0.0.1:{GATEWAY_PORT}"
CONFIG_DIR = _path("HIVE_CONFIG_DIR", HOME / ".config" / "hive")         # keys and secrets
WORK_DIR = _path("HIVE_WORK_DIR", HOME / "work" / "1hive")               # task folders and tools' state
REPOS_DIR = _path("HIVE_REPOS_DIR", HOME / "repos")                      # the hive's git remotes (<name>.git)
DEPLOY_DIR = _path("HIVE_DEPLOY_DIR", REPOS_DIR / "1-hive" / "deploy")   # the hive's declared configuration
WORKSPACE_REPO = _path("HIVE_WORKSPACE_REPO", REPOS_DIR / "hive-workspace.git")
LOCAL_BIN = HOME / ".local" / "bin"                                      # hive, uv, claude, codex


def tool_env(key_file: Path | str, via: str | None = None) -> dict:
    """The environment for `hive` CLI calls signed with key_file."""
    env = {**os.environ, "HIVE_URL": HIVE_URL, "HIVE_ID": HIVE_ID, "HIVE_KEY_FILE": str(key_file),
           "PATH": f"{LOCAL_BIN}:{os.environ.get('PATH', '')}"}
    if via:
        env["HIVE_VIA"] = via
    return env
