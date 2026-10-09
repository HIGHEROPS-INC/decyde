"""SQLite storage and settings. Agents write here directly, so the CLI works without the server."""
from __future__ import annotations

import getpass
import json
import os
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

HOME = Path(os.environ.get("DECYDE_HOME", Path.home() / ".decyde"))
DB_PATH = HOME / "decyde.db"
CONFIG_PATH = HOME / "config.json"
PORT = int(os.environ.get("DECYDE_PORT", "7717"))
KNOWN_AGENTS = ("claude", "codex", "grok", "antigravity", "gemini", "other")
URGENCIES = ("low", "normal", "high")

SCHEMA = """
CREATE TABLE IF NOT EXISTS questions (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL,
  agent           TEXT NOT NULL,          -- claude | codex | grok | antigravity | gemini | other
  agent_name      TEXT,                   -- free-form identity, e.g. "Codex, billing lane"
  task            TEXT NOT NULL,          -- what the agent is working on
  project         TEXT,
  cwd             TEXT,
  git_branch      TEXT,
  herdr_pane      TEXT,                   -- where to push the answer, if asked inside Herdr
  tmux_pane       TEXT,                   -- ... or inside tmux
  tmux_socket     TEXT,
  session_id      TEXT,                   -- agent session, for hook delivery
  inbox_socket    TEXT,                   -- Claude Code session inbox, to message it directly
  inbox_token     TEXT,
  codex_thread    TEXT,                   -- Codex thread, to start a turn through its app-server
  waiter_pid      INTEGER,                -- a running `decyde wait` that will hand the answer over
  title           TEXT NOT NULL,
  question        TEXT NOT NULL,
  context         TEXT,
  options         TEXT,                   -- JSON array of strings
  recommendation  TEXT,
  urgency         TEXT NOT NULL DEFAULT 'normal',
  status          TEXT NOT NULL DEFAULT 'open',  -- open | answered | acknowledged | cancelled
  answer          TEXT,
  answered_at     TEXT,
  delivery        TEXT,                   -- pending | pushed via ... | skipped: ... | failed: ...
  delivered_at    TEXT,
  acknowledged_at TEXT
);
CREATE INDEX IF NOT EXISTS questions_status ON questions(status);
CREATE TABLE IF NOT EXISTS sessions (     -- agent sessions that turned decyde on or off
  session_id      TEXT PRIMARY KEY,
  enabled         INTEGER NOT NULL DEFAULT 0,
  updated_at      TEXT NOT NULL,
  turn_started_at TEXT,                   -- last prompt, so the stop check knows the turn
  nudged_at       TEXT                    -- last time the stop check sent the agent back
);
"""
LATE_COLUMNS = ("tmux_pane", "tmux_socket", "session_id", "inbox_socket", "inbox_token", "codex_thread",
                "waiter_pid")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    HOME.mkdir(mode=0o700, parents=True, exist_ok=True)  # holds per-session inbox tokens
    conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.executescript(SCHEMA)
    have = {r[1] for r in conn.execute("PRAGMA table_info(questions)")}
    for col in LATE_COLUMNS:
        if col not in have:
            conn.execute(f"ALTER TABLE questions ADD COLUMN {col} {'INTEGER' if col == 'waiter_pid' else 'TEXT'}")
    return conn


def row_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    d["options"] = json.loads(d["options"]) if d.get("options") else []
    return d


def get_question(conn, qid: int) -> dict | None:
    return row_dict(conn.execute("SELECT * FROM questions WHERE id=?", (qid,)).fetchone())


def change_marker(conn) -> str:
    r = conn.execute("SELECT COUNT(*), COALESCE(MAX(updated_at),'') FROM questions").fetchone()
    return f"{r[0]}|{r[1]}"


# ---------------------------------------------------------------- settings

def default_name() -> str:
    try:
        out = subprocess.run(["git", "config", "--global", "user.name"], capture_output=True, text=True, timeout=3)
        if out.stdout.strip():
            return out.stdout.split()[0]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return getpass.getuser()


def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_config(cfg: dict) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n")


def stop_wait_minutes() -> int:
    """How long a Claude Code session waits at the end of its turn for an open question."""
    return int(load_config().get("claude_stop_wait_minutes", 24 * 60))


def human_name() -> str:
    """The person agents are asking. Used in answer prompts and the UI."""
    return load_config().get("name") or default_name()
