"""`decyde setup`, `uninstall` and `status`: wire decyde into this machine and its agents."""
from __future__ import annotations

import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from decyde import herdr_sidebar, protocol
from decyde.store import (CONFIG_PATH, DB_PATH, HOME, PORT, connect, default_name, load_config, save_config,
                          stop_wait_minutes)

HOMEDIR = Path.home()
PKG_ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = PKG_ROOT / "bin" / "decyde"
LINK = HOMEDIR / ".local" / "bin" / "decyde"
LABEL = "dev.decyde.server"
PLIST = HOMEDIR / "Library" / "LaunchAgents" / f"{LABEL}.plist"
UNIT = HOMEDIR / ".config" / "systemd" / "user" / "decyde.service"
CLAUDE_SETTINGS = HOMEDIR / ".claude" / "settings.json"
HOOK_FILES = (  # (label, hooks file, agent home). All three agents share Claude's hook format.
    ("Claude Code", CLAUDE_SETTINGS, HOMEDIR / ".claude"),
    ("Codex", HOMEDIR / ".codex" / "hooks.json", HOMEDIR / ".codex"),
    ("Grok", HOMEDIR / ".grok" / "hooks" / "decyde.json", HOMEDIR / ".grok"),
)
SLASH_COMMAND = HOMEDIR / ".claude" / "commands" / "decyde.md"
SLASH_COMMAND_TEXT = """---
description: Turn decyde on or off for this session
argument-hint: on | off
---
decyde $ARGUMENTS for this session. If on: from now on route every ask for me through
`decyde ask`, as the decyde section of your instructions describes. If off: ask in chat as
usual. Confirm in one short line.
"""
AGENT_FILES = (  # (label, config dir, instruction file). Only agents whose dir exists are touched.
    ("Claude Code", HOMEDIR / ".claude", "CLAUDE.md"),
    ("Codex", HOMEDIR / ".codex", "AGENTS.md"),
    ("Grok", HOMEDIR / ".grok", "AGENTS.md"),
    ("Gemini CLI / Antigravity", HOMEDIR / ".gemini", "GEMINI.md"),
)
HOOKS = (  # (event, matcher, subcommand, timeout seconds; None = follow the stop-wait setting)
    ("PostToolUse", None, "post-tool-use", 10),  # every tool, so answers arrive mid-turn
    ("Stop", None, "stop", None),
    ("UserPromptSubmit", None, "prompt", 10),
)
LEGACY_DB = HOMEDIR / ".decision-hub" / "hub.db"


def say(msg: str) -> None:
    print(f"  {msg}")


def cmd_path() -> str:
    return str(LINK if LINK.exists() else LAUNCHER)


# ---------------------------------------------------------------- pieces

def link_cli() -> None:
    LINK.parent.mkdir(parents=True, exist_ok=True)
    if LINK.is_symlink() or LINK.exists():
        if LINK.resolve() == LAUNCHER.resolve():
            return
        LINK.unlink()
    LINK.symlink_to(LAUNCHER)
    say(f"linked {LINK}")
    if str(LINK.parent) not in os.environ.get("PATH", "").split(os.pathsep):
        say(f"note: add {LINK.parent} to your PATH so agents can run `decyde`")


def init_db() -> None:
    if not DB_PATH.exists() and LEGACY_DB.exists():
        src, dst = sqlite3.connect(LEGACY_DB), sqlite3.connect(DB_PATH)
        src.backup(dst)
        src.close(), dst.close()
        say(f"imported questions from {LEGACY_DB}")
    n = connect().execute("SELECT COUNT(*) FROM questions").fetchone()[0]
    say(f"database ready at {DB_PATH} ({n} questions)")


def install_service() -> None:
    args = [sys.executable, str(LAUNCHER), "serve"]
    path = os.pathsep.join([str(LINK.parent), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"])
    if sys.platform == "darwin":
        PLIST.parent.mkdir(parents=True, exist_ok=True)
        items = "".join(f"<string>{a}</string>" for a in args)
        log = HOME / "server.log"
        PLIST.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key><array>{items}</array>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>{path}</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
</dict>
</plist>
""")
        domain = f"gui/{os.getuid()}"
        subprocess.run(["launchctl", "bootout", f"{domain}/{LABEL}"], capture_output=True)
        # bootout returns before launchd has let go of the job; bootstrapping too soon fails
        # with "5: Input/output error", so wait for it to disappear and retry briefly.
        for _ in range(50):
            if subprocess.run(["launchctl", "print", f"{domain}/{LABEL}"], capture_output=True).returncode:
                break
            time.sleep(0.1)
        for attempt in range(5):
            res = subprocess.run(["launchctl", "bootstrap", domain, str(PLIST)], capture_output=True, text=True)
            if res.returncode == 0:
                break
            time.sleep(0.5 * (attempt + 1))
        else:
            raise SystemExit(f"could not start the background server: {res.stderr.strip()}")
        say(f"background server installed (launchd {LABEL})")
    elif shutil.which("systemctl"):
        UNIT.parent.mkdir(parents=True, exist_ok=True)
        UNIT.write_text(f"""[Unit]
Description=decyde decision inbox for coding agents

[Service]
ExecStart={shlex.join(args)}
Environment=PATH={path}
Restart=always

[Install]
WantedBy=default.target
""")
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", "--now", "decyde.service"], check=True)
        subprocess.run(["systemctl", "--user", "restart", "decyde.service"], check=True)
        say("background server installed (systemd --user decyde.service)")
    else:
        say("no launchd or systemd found: run `decyde serve` yourself")


def remove_service() -> None:
    if PLIST.exists():
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
        PLIST.unlink()
        say("removed launchd service")
    if UNIT.exists():
        subprocess.run(["systemctl", "--user", "disable", "--now", "decyde.service"], capture_output=True)
        UNIT.unlink()
        say("removed systemd service")


def configure_agents(name: str) -> None:
    for label, cfg_dir, fname in AGENT_FILES:
        if not cfg_dir.is_dir():
            continue
        f = cfg_dir / fname
        old = f.read_text() if f.exists() else ""
        new = protocol.upsert(old, name)
        if new != old:
            f.write_text(new)
            say(f"{label}: protocol {'updated' if protocol.BEGIN in old else 'added'} in {f}")
        else:
            say(f"{label}: protocol already current in {f}")


def unconfigure_agents() -> None:
    for label, cfg_dir, fname in AGENT_FILES:
        f = cfg_dir / fname
        if f.exists() and protocol.BEGIN in f.read_text():
            rest = protocol.remove(f.read_text())
            f.write_text(rest) if rest.strip() else f.unlink()
            say(f"{label}: protocol removed from {f}")


def is_ours(entry: dict) -> bool:
    return any(" hook " in h.get("command", "") and "decyde" in h.get("command", "")
               for h in entry.get("hooks", []))


def edit_hook_file(label: str, path: Path, install: bool) -> None:
    """Merge (or remove) decyde's hooks in a Claude-format hooks file, keeping everything else."""
    owned = path.name == "decyde.json"  # Grok's file is ours alone; the others are shared
    try:
        settings = json.loads(path.read_text()) if path.exists() else {}
    except json.JSONDecodeError:
        say(f"{label}: {path} is not valid JSON, hooks not changed")
        return
    hooks = settings.setdefault("hooks", {})
    for event, matcher, sub, timeout in HOOKS:
        entries = [e for e in hooks.get(event, []) if not is_ours(e)]
        if install:
            entry = {"hooks": [{"type": "command", "command": f"{shlex.quote(cmd_path())} hook {sub}",
                                "timeout": timeout or stop_wait_minutes() * 60 + 60}]}
            if matcher:
                entry = {"matcher": matcher, **entry}
            entries.append(entry)
        if entries:
            hooks[event] = entries
        else:
            hooks.pop(event, None)
    if not hooks:
        settings.pop("hooks")
    if owned and not settings:
        path.unlink(missing_ok=True)
    else:
        if path.exists() and not owned:
            shutil.copy2(path, path.with_name(path.name + ".decyde-backup"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(settings, indent=2) + "\n")
    say(f"{label}: hooks {'installed' if install else 'removed'} in {path}")


def edit_agent_hooks(install: bool) -> None:
    for label, path, home in HOOK_FILES:
        if home.is_dir():
            edit_hook_file(label, path, install)
    if install and HOOK_FILES[1][2].is_dir():
        say("Codex: approve the new decyde hooks when Codex asks you to review them on its next start")


def edit_slash_command(install: bool) -> None:
    if not SLASH_COMMAND.parent.parent.is_dir():
        return
    if install:
        SLASH_COMMAND.parent.mkdir(parents=True, exist_ok=True)
        SLASH_COMMAND.write_text(SLASH_COMMAND_TEXT)
        say(f"Claude Code: /decyde on|off command at {SLASH_COMMAND}")
    elif SLASH_COMMAND.exists():
        SLASH_COMMAND.unlink()
        say(f"Claude Code: removed {SLASH_COMMAND}")


def server_up() -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/meta", timeout=2) as r:
            return r.status == 200
    except OSError:
        return False


# ---------------------------------------------------------------- commands

def setup(args) -> int:
    cfg = load_config()
    name = args.name or cfg.get("name")
    if not name:
        guess = default_name()
        if sys.stdin.isatty():
            name = input(f"What should agents call you? [{guess}] ").strip() or guess
        else:
            name = guess
    cfg["name"] = name
    save_config(cfg)
    print(f"Setting up decyde for {name}")
    link_cli()
    init_db()
    if not args.no_service:
        install_service()
    if not args.no_agents:
        configure_agents(name)
    if not args.no_hooks:
        edit_agent_hooks(install=True)
        edit_slash_command(install=True)
    if not args.no_herdr and (msg := herdr_sidebar.install_rows()):
        say(msg)
    print(f"\nDone. Open http://127.0.0.1:{PORT} or run `decyde` for the terminal UI.")
    return 0


def uninstall(args) -> int:
    print("Removing decyde")
    remove_service()
    unconfigure_agents()
    edit_agent_hooks(install=False)
    edit_slash_command(install=False)
    if msg := herdr_sidebar.remove_rows():
        say(msg)
    if LINK.is_symlink():
        LINK.unlink()
        say(f"removed {LINK}")
    if args.purge:
        shutil.rmtree(HOME, ignore_errors=True)
        say(f"deleted {HOME} (database and settings)")
    else:
        say(f"kept your questions in {HOME} (use --purge to delete)")
    return 0


def status(args) -> int:
    conn = connect()
    n_open = conn.execute("SELECT COUNT(*) FROM questions WHERE status='open'").fetchone()[0]
    from decyde import __version__, update
    newer = update.available()
    print(f"decyde {__version__} for {load_config().get('name', '(not set up)')}"
          + (f"  ·  {newer} available: run `decyde update`" if newer else ""))
    say(f"database   {DB_PATH} ({n_open} open)")
    say(f"config     {CONFIG_PATH}")
    say(f"server     {'running' if server_up() else 'NOT running'} on http://127.0.0.1:{PORT}")
    for label, cfg_dir, fname in AGENT_FILES:
        f = cfg_dir / fname
        state = "configured" if f.exists() and protocol.BEGIN in f.read_text() else \
            "not configured" if cfg_dir.is_dir() else "not installed"
        say(f"{label:<25}{state}")
    for label, path, home in HOOK_FILES:
        try:
            hooked = any(is_ours(e) for es in json.loads(path.read_text()).get("hooks", {}).values() for e in es)
        except (FileNotFoundError, json.JSONDecodeError):
            hooked = False
        if home.is_dir():
            say(f"{label + ' hooks':<25}{'installed' if hooked else 'not installed'}")
    return 0
