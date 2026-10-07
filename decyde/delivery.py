"""Getting an answer back to the agent that asked: Herdr, tmux, or Claude Code hooks.

Answers go out as soon as they are given: agents queue a message typed mid-turn and read it
at their next step. The only thing worth waiting for is an approval dialog, which typed text
would answer. Polling (`decyde check`) always works too.
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from datetime import datetime

from decyde.store import connect, get_question, human_name, now

SHELLS = {"zsh", "bash", "fish", "sh", "dash", "ksh", "tcsh", "csh", "nu", "login", "-zsh", "-bash"}
# Text that means the agent is showing an approval dialog. Typing then could answer the dialog,
# so wait. A working agent is fine: Claude Code and Codex queue what is typed mid-turn.
DIALOG_MARKERS = ("esc to cancel", "do you want to", "(y/n)", "[y/n]", "allow command", "yes, proceed",
                  "press enter to")
RETRY_HOURS = 6


def answer_prompt(q: dict) -> str:
    return (
        f"[decyde] {human_name()} answered your question #{q['id']} (\"{q['title']}\"):\n\n"
        f"{q['answer']}\n\n"
        f"Run `decyde ack {q['id']}` to mark it received, then continue your task with this decision."
    )


def run(cmd: list[str], timeout: float = 10, stdin: str | None = None) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, input=stdin)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


# ---------------------------------------------------------------- herdr

def push_herdr(q: dict) -> str:
    out = run(["herdr", "agent", "get", q["herdr_pane"]])
    if out is None:
        return "skipped: herdr unavailable"
    try:
        agent = json.loads(out.stdout)["result"]["agent"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return "skipped: herdr pane closed or no agent in it"
    kind = (agent.get("agent") or "").lower()
    if q["agent"] in ("claude", "codex") and kind and kind != q["agent"]:
        return f"skipped: herdr pane now runs {kind}"
    if agent.get("agent_status") == "blocked":
        return "pending"  # an approval dialog is up; typing would answer it
    res = run(["herdr", "agent", "prompt", q["herdr_pane"], answer_prompt(q)])
    if res is None:
        return "pending"
    if res.returncode != 0:
        err = (res.stderr or res.stdout).strip()
        return "pending" if "blocked" in err else f"failed: herdr {err[:120]}"
    return "pushed via herdr"


# ---------------------------------------------------------------- tmux

def push_tmux(q: dict) -> str:
    base = ["tmux"] + (["-S", q["tmux_socket"]] if q.get("tmux_socket") else [])
    target = ["-t", q["tmux_pane"]]
    cur = run(base + ["display-message", "-p", *target, "#{pane_current_command}"])
    if cur is None:
        return "skipped: tmux unavailable"
    if cur.returncode != 0:
        return "skipped: tmux pane closed"
    if cur.stdout.strip().lower() in SHELLS:
        return "skipped: agent no longer running in that tmux pane"
    screen = run(base + ["capture-pane", "-p", *target, "-S", "-20"])
    if screen is None or any(m in screen.stdout.lower() for m in DIALOG_MARKERS):
        return "pending"
    if run(base + ["load-buffer", "-b", "decyde", "-"], stdin=answer_prompt(q)) is None:
        return "pending"
    run(base + ["paste-buffer", "-p", "-d", "-b", "decyde", *target])
    time.sleep(0.4)
    res = run(base + ["send-keys", *target, "Enter"])
    return "pushed via tmux" if res and res.returncode == 0 else "failed: tmux send-keys"


# ---------------------------------------------------------------- dispatch

HOOK_QUEUED = "queued: arrives at the agent's next tool call or message"


def try_push(q: dict) -> str:
    state = None
    for pane, push in (("herdr_pane", push_herdr), ("tmux_pane", push_tmux)):
        if q.get(pane):
            state = push(q)
            if not state.startswith("skipped"):
                return state
    if q.get("session_id"):
        return HOOK_QUEUED  # a Claude Code hook hands it over; nothing to retry here
    return state or "skipped: no push route (agent polls)"


def deliver(conn, qid: int) -> None:
    # Claim the answer first so a Claude Code hook cannot deliver it at the same moment.
    if not conn.execute("UPDATE questions SET delivery='pushing' WHERE id=? AND status='answered' "
                        "AND delivery='pending'", (qid,)).rowcount:
        return
    state = "pending"
    try:
        state = try_push(get_question(conn, qid))
    finally:
        conn.execute(
            "UPDATE questions SET delivery=?, delivered_at=?, updated_at=? WHERE id=? AND delivery='pushing'",
            (state, now() if state.startswith("pushed") else None, now(), qid))


def mark_delivered(conn, qid: int, state: str) -> None:
    conn.execute(
        "UPDATE questions SET delivery=?, delivered_at=?, updated_at=? WHERE id=? AND delivery='pending'",
        (state, now() if state.startswith("pushed") else None, now(), qid),
    )


def delivery_loop(stop: threading.Event) -> None:
    """Retry pushes for answers that were waiting on an approval dialog."""
    conn = connect()
    while not stop.wait(15):
        rows = conn.execute(
            "SELECT id, answered_at FROM questions WHERE status='answered' AND delivery='pending'").fetchall()
        for r in rows:
            age = time.time() - datetime.fromisoformat(r["answered_at"]).timestamp()
            if age > RETRY_HOURS * 3600:
                mark_delivered(conn, r["id"], "skipped: approval dialog never cleared")
            else:
                try:
                    deliver(conn, r["id"])
                except Exception as e:  # keep the loop alive whatever one push does
                    print(f"delivery #{r['id']} failed: {e}", file=sys.stderr, flush=True)


def record_answer(conn, qid: int, text: str, background: bool = True) -> None:
    conn.execute(
        "UPDATE questions SET status='answered', answer=?, answered_at=?, updated_at=?, "
        "delivery='pending', delivered_at=NULL, acknowledged_at=NULL WHERE id=?",
        (text, now(), now(), qid),
    )
    if background:
        threading.Thread(target=lambda: deliver(connect(), qid), daemon=True).start()
    else:
        deliver(conn, qid)


def dismiss_question(conn, qid: int) -> None:
    conn.execute(
        "UPDATE questions SET status='cancelled', updated_at=?, "
        "answer=COALESCE(answer, ?) WHERE id=?",
        (now(), f"Dismissed by {human_name()} without an answer.", qid),
    )


# ---------------------------------------------------------------- new-question alerts

def notify_new(q: dict) -> None:
    title = f"decyde: {q['agent']} needs a decision"
    body = q["title"][:180]
    if sys.platform == "darwin":
        script = f"display notification {json.dumps(body)} with title {json.dumps(title)} sound name \"Glass\""
        cmds = [["osascript", "-e", script]]
    else:
        cmds = [["notify-send", title, body]]
    cmds.append(["herdr", "notification", "show", title, "--body", body, "--sound", "request"])
    for cmd in cmds:
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            pass
