"""Getting an answer back to the agent that asked: Herdr, tmux, or Claude Code hooks.

Polling (`decyde check`) always works; these routes wake an agent that has gone idle.
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
# Text that means the agent is mid-turn or showing a dialog. Typing then could answer the dialog.
BUSY_MARKERS = ("esc to interrupt", "esc to cancel", "do you want to", "(y/n)", "[y/n]", "allow command",
                "yes, proceed", "approve", "press enter to")
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
    if agent.get("agent_status") in ("working", "blocked"):
        return "pending"  # retry later; never interrupt a working or blocked agent
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
    # tmux cannot tell us the agent's state, so require a still screen with no dialog on it.
    snap = lambda: (run(base + ["capture-pane", "-p", *target, "-S", "-20"]) or subprocess.CompletedProcess([], 1)).stdout
    first = snap()
    time.sleep(2)
    second = snap()
    if first != second or any(m in second.lower() for m in BUSY_MARKERS):
        return "pending"
    if run(base + ["load-buffer", "-b", "decyde", "-"], stdin=answer_prompt(q)) is None:
        return "pending"
    run(base + ["paste-buffer", "-p", "-d", "-b", "decyde", *target])
    time.sleep(0.4)
    res = run(base + ["send-keys", *target, "Enter"])
    return "pushed via tmux" if res and res.returncode == 0 else "failed: tmux send-keys"


# ---------------------------------------------------------------- dispatch

def try_push(q: dict) -> str:
    if q.get("herdr_pane"):
        state = push_herdr(q)
        if not state.startswith("skipped") or not q.get("tmux_pane"):
            return state
    if q.get("tmux_pane"):
        return push_tmux(q)
    if q.get("session_id"):
        return "pending"  # the Claude Code hook delivers it on the agent's next stop or prompt
    return "skipped: no push route (agent polls)"


def deliver(conn, qid: int) -> None:
    q = get_question(conn, qid)
    if not q or q["status"] != "answered" or q["delivery"] != "pending":
        return
    state = try_push(q)
    if state == "pending":
        return
    mark_delivered(conn, qid, state)


def mark_delivered(conn, qid: int, state: str) -> None:
    conn.execute(
        "UPDATE questions SET delivery=?, delivered_at=?, updated_at=? WHERE id=? AND delivery='pending'",
        (state, now() if state.startswith("pushed") else None, now(), qid),
    )


def delivery_loop(stop: threading.Event) -> None:
    """Retry pushes for answers whose agent was busy when the human replied."""
    conn = connect()
    while not stop.wait(15):
        rows = conn.execute(
            "SELECT id, answered_at FROM questions WHERE status='answered' AND delivery='pending'").fetchall()
        for r in rows:
            age = time.time() - datetime.fromisoformat(r["answered_at"]).timestamp()
            if age > RETRY_HOURS * 3600:
                mark_delivered(conn, r["id"], "skipped: agent stayed busy")
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
