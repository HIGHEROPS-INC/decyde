"""Command line: what agents call (ask/check/wait/ack/cancel) and what the human runs."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

from decyde import __version__
from decyde.store import (KNOWN_AGENTS, PORT, URGENCIES, connect, get_question, load_config, now,
                          row_dict, save_config)


def git_branch(cwd: str) -> str | None:
    try:
        out = subprocess.run(["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"],
                             capture_output=True, text=True, timeout=3)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return (out.stdout.strip() or None) if out.returncode == 0 else None


def exit_code(q: dict) -> int:
    return 0 if q["status"] in ("answered", "acknowledged") else 3 if q["status"] == "cancelled" else 2


def print_q(q: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(q, indent=2))
        return
    print(f"#{q['id']} [{q['status']}] {q['title']}")
    if q["status"] in ("answered", "acknowledged"):
        print(f"ANSWER: {q['answer']}")
    elif q["status"] == "cancelled":
        print(f"CANCELLED: {q.get('answer') or 'withdrawn'}")
    else:
        print("No answer yet. Keep working on anything this decision does not block, "
              f"and check again later with `decyde check {q['id']}`.")


# ---------------------------------------------------------------- finding the asker's pane

AGENT_KINDS = {"antigravity": ("antigravity", "agy"), "gemini": ("gemini",)}  # decyde name -> process/agent ids


def kinds(agent: str) -> tuple[str, ...]:
    return AGENT_KINDS.get(agent, (agent,))


def under(path: str | None, cwd: str) -> bool:
    return bool(path) and (cwd == path or cwd.startswith(path.rstrip("/") + "/"))


def pick(candidates: list[dict]) -> dict | None:
    """The asker, only when it is certain. Several panes may run the same agent in the same
    folder; the asker is busy running `decyde ask`, so prefer the one that is working.
    Anything still ambiguous returns None: a wrong guess would type the answer into
    another agent, which is worse than falling back to polling."""
    if len(candidates) == 1:
        return candidates[0]
    working = [c for c in candidates if c.get("working")]
    return working[0] if len(working) == 1 else None


def find_herdr_pane(agent: str, cwd: str) -> str | None:
    try:
        out = subprocess.run(["herdr", "agent", "list"], capture_output=True, text=True, timeout=5)
        agents = json.loads(out.stdout)["result"]["agents"]
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError, KeyError, TypeError):
        return None
    found = []
    for a in agents:
        paths = [p for p in (a.get("foreground_cwd"), a.get("cwd")) if under(p, cwd)]
        if a.get("agent") in kinds(agent) and paths:
            found.append({"pane": a["pane_id"], "working": a.get("agent_status") == "working",
                          "depth": max(len(p) for p in paths)})
    # An agent in ~/proj/app is a better match for ~/proj/app than one in ~/proj.
    deepest = max((f["depth"] for f in found), default=0)
    hit = pick([f for f in found if f["depth"] == deepest])
    return hit["pane"] if hit else None


def find_tmux_pane(agent: str, cwd: str) -> tuple[str | None, str | None]:
    """tmux cannot say whether an agent is working, so only a single match counts."""
    try:
        out = subprocess.run(["tmux", "list-panes", "-a", "-F",
                              "#{pane_id}\t#{socket_path}\t#{pane_current_command}\t#{pane_current_path}"],
                             capture_output=True, text=True, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None, None
    found = []
    for line in out.stdout.splitlines():
        pane, sock, cmd, path = (line.split("\t") + ["", "", "", ""])[:4]
        if cmd.lower() in kinds(agent) and under(path, cwd):
            found.append((pane, sock))
    return found[0] if len(found) == 1 else (None, None)


# ---------------------------------------------------------------- agent commands

def cmd_ask(args) -> int:
    agent = args.agent.lower()
    if agent not in KNOWN_AGENTS:
        print(f"--agent must be one of: {', '.join(KNOWN_AGENTS)}", file=sys.stderr)
        return 1
    cwd = os.getcwd()
    env = os.environ
    herdr = env.get("HERDR_PANE_ID") if env.get("HERDR_ENV") == "1" else None
    tmux_socket = env["TMUX"].split(",")[0] if env.get("TMUX") else None
    tmux_pane = env.get("TMUX_PANE") if tmux_socket else None
    if not herdr and not tmux_pane:
        # Some agents (Codex since 0.161) run commands from a shared background server
        # started outside the multiplexer, so the pane variables are missing. Ask instead.
        herdr = find_herdr_pane(agent, cwd)
        if not herdr:
            tmux_pane, tmux_socket = find_tmux_pane(agent, cwd)
    ts = now()
    conn = connect()
    cur = conn.execute(
        "INSERT INTO questions (created_at, updated_at, agent, agent_name, task, project, cwd, git_branch, "
        "herdr_pane, tmux_pane, tmux_socket, session_id, title, question, context, options, recommendation, "
        "urgency) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ts, ts, agent, args.name, args.task, args.project or Path(cwd).name.strip(), cwd, git_branch(cwd),
         herdr, tmux_pane, tmux_socket, env.get("GROK_SESSION_ID"), args.title, args.question,
         args.context, json.dumps(args.option) if args.option else None, args.recommend, args.urgency),
    )
    qid = cur.lastrowid
    if args.json:
        print(json.dumps(get_question(conn, qid), indent=2))
    else:
        print(f"Asked as decyde question #{qid}.")
        print(f"Check for the answer with: decyde check {qid}")
    return 0


def cmd_check(args) -> int:
    q = get_question(connect(), args.id)
    if not q:
        print(f"No question #{args.id}", file=sys.stderr)
        return 1
    print_q(q, args.json)
    return exit_code(q)


def cmd_wait(args) -> int:
    conn = connect()
    deadline = time.time() + args.timeout
    while True:
        q = get_question(conn, args.id)
        if not q:
            print(f"No question #{args.id}", file=sys.stderr)
            return 1
        if q["status"] != "open" or time.time() >= deadline:
            print_q(q, args.json)
            return exit_code(q)
        time.sleep(args.interval)


def cmd_ack(args) -> int:
    conn = connect()
    q = get_question(conn, args.id)
    if not q or q["status"] not in ("answered", "acknowledged"):
        print(f"#{args.id} has no answer to acknowledge", file=sys.stderr)
        return 1
    conn.execute(
        "UPDATE questions SET status='acknowledged', acknowledged_at=?, updated_at=?, "
        "delivery=CASE WHEN delivery='pending' THEN 'skipped: agent polled first' ELSE delivery END "
        "WHERE id=?", (now(), now(), args.id))
    print(f"#{args.id} acknowledged.")
    return 0


def cmd_cancel(args) -> int:
    conn = connect()
    q = get_question(conn, args.id)
    if not q or q["status"] != "open":
        print(f"#{args.id} is not open", file=sys.stderr)
        return 1
    conn.execute("UPDATE questions SET status='cancelled', answer=?, updated_at=? WHERE id=?",
                 (f"Withdrawn by agent: {args.reason}" if args.reason else "Withdrawn by agent.", now(), args.id))
    print(f"#{args.id} withdrawn.")
    return 0


def cmd_list(args) -> int:
    sql = "SELECT * FROM questions" + ("" if args.all else " WHERE status IN ('open','answered')")
    rows = [row_dict(r) for r in connect().execute(sql + " ORDER BY id DESC LIMIT 50")]
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        for q in rows:
            print(f"#{q['id']:<4} {q['status']:<12} {q['agent']:<11} {q['project'] or '':<20} {q['title']}")
    return 0


# ---------------------------------------------------------------- human commands

def cmd_tui(args) -> int:
    from decyde.tui import tui
    tui(show_splash=not getattr(args, "no_splash", False))
    return 0


def cmd_serve(args) -> int:
    from decyde.server import serve
    serve(args.port)
    return 0


def cmd_open(args) -> int:
    webbrowser.open(f"http://127.0.0.1:{PORT}")
    return 0


CONFIG_KEYS = {"name": str, "claude_stop_wait_minutes": int, "splash": lambda v: v.lower() in ("1", "true", "on", "yes")}


def cmd_config(args) -> int:
    cfg = load_config()
    if not args.key:
        print(json.dumps(cfg, indent=2))
        return 0
    if args.key not in CONFIG_KEYS:
        print(f"unknown key; one of: {', '.join(CONFIG_KEYS)}", file=sys.stderr)
        return 1
    if args.value is None:
        print(cfg.get(args.key, ""))
        return 0
    cfg[args.key] = CONFIG_KEYS[args.key](args.value)
    save_config(cfg)
    print(f"{args.key} = {cfg[args.key]}")
    if args.key == "name":
        print("Run `decyde setup` to update the name in your agents' instructions.")
    if args.key == "claude_stop_wait_minutes":
        from decyde import install
        install.edit_agent_hooks(install=True)  # the Stop hooks' timeout follows this setting
    return 0


def cmd_hook(args) -> int:
    from decyde.hooks import run
    return run(args.event)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="decyde", description="Your agents ask. You decide.")
    p.add_argument("--version", action="version", version=f"decyde {__version__}")
    p.add_argument("--no-splash", action="store_true", help="skip the launch animation")
    sub = p.add_subparsers(dest="cmd")

    a = sub.add_parser("ask", help="(agents) post a question")
    a.add_argument("--agent", required=True, help=f"one of {', '.join(KNOWN_AGENTS)}")
    a.add_argument("--name", help="your identity, e.g. 'Codex, billing lane'")
    a.add_argument("--task", required=True, help="what you are working on")
    a.add_argument("--title", required=True, help="one-line summary of the decision")
    a.add_argument("--question", required=True, help="the full question")
    a.add_argument("--context", help="background needed to decide")
    a.add_argument("--option", action="append", help="a choice (repeatable)")
    a.add_argument("--recommend", help="your recommended choice and why")
    a.add_argument("--urgency", choices=URGENCIES, default="normal", help="high = you are blocked until answered")
    a.add_argument("--project", help="defaults to the current directory name")
    a.add_argument("--json", action="store_true")
    a.set_defaults(fn=cmd_ask)

    for name, fn, helptext in (("check", cmd_check, "(agents) print the answer; exit 0 answered, 2 open, 3 dismissed"),
                               ("ack", cmd_ack, "(agents) confirm you received the answer")):
        s = sub.add_parser(name, help=helptext)
        s.add_argument("id", type=int)
        s.add_argument("--json", action="store_true")
        s.set_defaults(fn=fn)

    w = sub.add_parser("wait", help="(agents) block until answered or timeout")
    w.add_argument("id", type=int)
    w.add_argument("--timeout", type=int, default=600, help="seconds (default 600)")
    w.add_argument("--interval", type=int, default=15)
    w.add_argument("--json", action="store_true")
    w.set_defaults(fn=cmd_wait)

    c = sub.add_parser("cancel", help="(agents) withdraw a question")
    c.add_argument("id", type=int)
    c.add_argument("--reason")
    c.set_defaults(fn=cmd_cancel)

    l = sub.add_parser("list", help="list open and answered questions")
    l.add_argument("--all", action="store_true")
    l.add_argument("--json", action="store_true")
    l.set_defaults(fn=cmd_list)

    t = sub.add_parser("tui", help="terminal UI (also the default with no command)")
    t.add_argument("--no-splash", action="store_true", default=argparse.SUPPRESS)
    t.set_defaults(fn=cmd_tui)
    sub.add_parser("open", help="open the web UI in your browser").set_defaults(fn=cmd_open)
    sv = sub.add_parser("serve", help="run the web server (setup installs it as a service)")
    sv.add_argument("--port", type=int, default=PORT)
    sv.set_defaults(fn=cmd_serve)

    from decyde import install
    su = sub.add_parser("setup", help="install the service and teach your agents about decyde")
    su.add_argument("--name", help="what agents call you")
    su.add_argument("--no-service", action="store_true")
    su.add_argument("--no-agents", action="store_true", help="leave agent instruction files alone")
    su.add_argument("--no-hooks", action="store_true", help="leave agent hook settings alone")
    su.add_argument("--no-herdr", action="store_true", help="leave Herdr's sidebar config alone")
    su.set_defaults(fn=install.setup)
    un = sub.add_parser("uninstall", help="undo setup")
    un.add_argument("--purge", action="store_true", help="also delete the database")
    un.set_defaults(fn=install.uninstall)
    sub.add_parser("status", help="show what is installed and running").set_defaults(fn=install.status)

    cf = sub.add_parser("config", help="show or change settings")
    cf.add_argument("key", nargs="?")
    cf.add_argument("value", nargs="?")
    cf.set_defaults(fn=cmd_config)

    hk = sub.add_parser("hook")  # called by Claude Code, not people
    hk.add_argument("event", choices=("post-tool-use", "stop", "prompt"))
    hk.set_defaults(fn=cmd_hook)
    return p


def main() -> int:
    args = build_parser().parse_args()
    if not args.cmd:
        return cmd_tui(args)
    return args.fn(args) or 0
