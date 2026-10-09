"""Knowing about and installing new releases.

Once a day the server reads the version string from the repo's main branch on GitHub
(a plain GET; nothing about you or your questions is sent) and caches it, so both UIs
can say when an update is out. `decyde config update_check off` disables it.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import urllib.request

from decyde import __version__
from decyde.store import HOME, load_config

VERSION_URL = "https://raw.githubusercontent.com/HIGHEROPS-INC/decyde/main/decyde/__init__.py"
INSTALL_URL = "https://decyde.dev/install"
CACHE = HOME / "update.json"
CHECK_EVERY = 24 * 3600


def parse(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def enabled() -> bool:
    return load_config().get("update_check", True)


def cached() -> dict:
    try:
        return json.loads(CACHE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def check(force: bool = False) -> str | None:
    """Refresh the cache if it is stale; return the latest version known, if any."""
    data = cached()
    if not enabled():
        return None
    if force or time.time() - data.get("checked_at", 0) >= CHECK_EVERY:
        try:
            with urllib.request.urlopen(VERSION_URL, timeout=5) as r:
                m = re.search(r'__version__\s*=\s*"([^"]+)"', r.read().decode())
            data = {"checked_at": time.time(), "latest": m.group(1) if m else data.get("latest")}
            HOME.mkdir(parents=True, exist_ok=True)
            CACHE.write_text(json.dumps(data))
        except OSError:
            pass  # offline: try again next time
    return data.get("latest")


def available() -> str | None:
    """A newer version than this one, from the cache only (never touches the network)."""
    latest = cached().get("latest") if enabled() else None
    return latest if latest and parse(latest) > parse(__version__) else None


def checkout_root():
    from decyde.install import PKG_ROOT
    return PKG_ROOT if (PKG_ROOT / ".git").exists() else None


def run_update(args) -> int:
    latest = check(force=True)
    if latest and parse(latest) <= parse(__version__) and not getattr(args, "force", False):
        print(f"decyde {__version__} is the latest version.")
        return 0
    root = checkout_root()
    if root:
        print(f"This decyde runs from a git checkout ({root}). Update it with:\n"
              f"  git -C '{root}' pull && decyde setup")
        return 1
    print(f"Updating decyde {__version__} -> {latest or 'latest'}")
    return subprocess.call(["sh", "-c", f"curl -fsSL {INSTALL_URL} | sh"], stdin=sys.stdin)
