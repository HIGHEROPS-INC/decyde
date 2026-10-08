"""Mark agents that are waiting on you in Herdr's sidebar.

The server reports a `$decyde` token on the pane of every agent with an open question,
and on its workspace, and clears it once nothing is open. Tokens carry a TTL and are
refreshed while open, so a stopped server never leaves a stale mark behind.
`decyde setup` adds the rows that display the token to Herdr's config.toml.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tomllib
from pathlib import Path

SOURCE = "decyde"
TOKEN = "decyde"
TTL_MS = 15 * 60 * 1000
REFRESH_SECONDS = 5 * 60
COLOR = "#a78bfa"

BEGIN = "# >>> decyde: sidebar rows (managed by `decyde setup`, removed by `decyde uninstall`) >>>"
END = "# <<< decyde <<<"
STYLED = f'{{ token = "${TOKEN}", fg = "{COLOR}", bold = true }}'
# Herdr's documented default layouts plus one row for the decyde mark.
BLOCK = f"""{BEGIN}
[ui.sidebar.agents]
rows = [
  ["state_icon", "machine", "workspace", "tab"],
  ["agent"],
  [{STYLED}],
]

[ui.sidebar.spaces]
rows = [
  ["state_icon", "workspace"],
  ["branch", "git_status"],
  [{STYLED}],
]
{END}"""


def available() -> bool:
    return shutil.which("herdr") is not None


def herdr(*args: str) -> dict | None:
    try:
        out = subprocess.run(["herdr", *args], capture_output=True, text=True, timeout=10)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    try:
        return json.loads(out.stdout) if out.stdout.strip() else {}
    except json.JSONDecodeError:
        return {}


def clip(text: str, n: int = 80) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


# ---------------------------------------------------------------- marks

class Sidebar:
    def __init__(self):
        self.shown: dict[tuple[str, str], str] = {}  # (kind, id) -> value currently reported
        self.last_full = 0.0

    def report(self, kind: str, target: str, value: str | None) -> None:
        args = [kind, "report-metadata", target, "--source", SOURCE]
        args += ["--token", f"{TOKEN}={value}", "--ttl-ms", str(TTL_MS)] if value else ["--clear-token", TOKEN]
        if herdr(*args) is not None:
            if value:
                self.shown[(kind, target)] = value
            else:
                self.shown.pop((kind, target), None)

    def sync(self, conn, now: float) -> None:
        """Bring the sidebar in line with the open questions. Cheap when nothing changed."""
        if not available():
            return
        rows = conn.execute("SELECT id, title, herdr_pane FROM questions WHERE status='open' "
                            "AND herdr_pane IS NOT NULL ORDER BY id").fetchall()
        listing = herdr("pane", "list")
        if listing is None:
            return
        live = {p["pane_id"]: p.get("workspace_id") for p in (listing.get("result") or {}).get("panes", [])}
        by_pane: dict[str, list] = {}
        for r in rows:
            if r["herdr_pane"] in live:
                by_pane.setdefault(r["herdr_pane"], []).append(r)
        want: dict[tuple[str, str], str] = {}
        per_space: dict[str, int] = {}
        for pane, qs in by_pane.items():
            first = qs[0]
            more = f" (+{len(qs) - 1} more)" if len(qs) > 1 else ""
            want[("pane", pane)] = clip(f"? #{first['id']} {first['title']}{more}")
            if live[pane]:
                per_space[live[pane]] = per_space.get(live[pane], 0) + len(qs)
        for space, n in per_space.items():
            want[("workspace", space)] = f"? {n} waiting on you"

        refresh = now - self.last_full >= REFRESH_SECONDS
        if refresh:
            self.last_full = now
        for key, value in want.items():
            if refresh or self.shown.get(key) != value:
                self.report(*key, value)
        for key in [k for k in self.shown if k not in want]:
            self.report(*key, None)

    def clear_all(self) -> None:
        for key in list(self.shown):
            self.report(*key, None)


# ---------------------------------------------------------------- config.toml rows

def config_path() -> Path:
    return Path.home() / ".config" / "herdr" / "config.toml"


def install_rows() -> str:
    """Add the managed block to Herdr's config. Returns a one-line status for setup to print."""
    if not available():
        return ""
    path = config_path()
    text = path.read_text() if path.exists() else ""
    if BEGIN in text:
        return "Herdr: sidebar rows already installed"
    try:
        current = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return f"Herdr: {path} does not parse, sidebar rows not added"
    sidebar = current.get("ui", {}).get("sidebar", {})
    taken = [name for name in ("agents", "spaces") if "rows" in sidebar.get(name, {})]
    if taken:
        return ("Herdr: you already customise the sidebar rows; to see decyde marks, add "
                f"[{STYLED}] to the rows of [ui.sidebar.agents] and [ui.sidebar.spaces]")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text((text.rstrip("\n") + "\n\n" if text.strip() else "") + BLOCK + "\n")
    check = subprocess.run(["herdr", "config", "check"], capture_output=True, text=True, timeout=10)
    if "parse error" in (check.stdout + check.stderr):
        path.write_text(text)
        return "Herdr: config check failed, sidebar rows rolled back"
    subprocess.run(["herdr", "server", "reload-config"], capture_output=True, timeout=10)
    return f"Herdr: sidebar rows added to {path}"


def remove_rows() -> str:
    path = config_path()
    if not path.exists() or BEGIN not in path.read_text():
        return ""
    text = path.read_text()
    head, rest = text.split(BEGIN, 1)
    path.write_text(head.rstrip("\n") + "\n" + rest.split(END, 1)[1].lstrip("\n"))
    subprocess.run(["herdr", "server", "reload-config"], capture_output=True, timeout=10)
    return f"Herdr: sidebar rows removed from {path}"
