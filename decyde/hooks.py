"""Claude Code hooks: deliver answers to plain-terminal Claude sessions.

Claude Code cannot be woken once idle, so:
  post-tool-use  remembers which session ran `decyde ask`, and hands a working Claude
                 any new answer at its next tool call (as typed messages arrive mid-turn)
  stop           when that session tries to stop with an open question, waits for
                 the answer (default 10 min) and hands it back so Claude continues
  prompt         injects any late answer into the next message the human sends
"""
from __future__ import annotations

import json
import re
import sys
import time

from decyde.delivery import answer_prompt
from decyde.store import connect, load_config, now, row_dict

ASK_RE = re.compile(r"\b(decyde|dhub)\s+ask\b")
ID_RE = re.compile(r"question #(\d+)|\"id\":\s*(\d+)")
POLL_SECONDS = 3
MAX_WAIT = 590  # stays inside Claude Code's default 600s hook timeout
UNCLAIMED = "(delivery IS NULL OR (delivery NOT LIKE 'pushed%' AND delivery != 'pushing'))"


def claim_answers(conn, session: str) -> list[dict]:
    """Answers for this session nobody has delivered yet, claimed atomically."""
    rows = conn.execute(
        "SELECT * FROM questions WHERE session_id=? AND status='answered' "
        f"AND {UNCLAIMED}", (session,)).fetchall()
    claimed = []
    for r in rows:
        cur = conn.execute(
            "UPDATE questions SET delivery='pushed via claude hook', delivered_at=?, updated_at=? "
            f"WHERE id=? AND {UNCLAIMED}", (now(), now(), r["id"]))
        if cur.rowcount:
            claimed.append(row_dict(r))
    return claimed


def post_tool_use(data: dict) -> None:
    session = data.get("session_id")
    if not session:
        return
    conn = connect()
    cmd = str((data.get("tool_input") or {}).get("command", ""))
    if data.get("tool_name") == "Bash" and ASK_RE.search(cmd):
        m = ID_RE.search(json.dumps(data.get("tool_response")).replace('\\"', '"'))
        if m:
            conn.execute("UPDATE questions SET session_id=? WHERE id=? AND session_id IS NULL",
                         (session, int(m.group(1) or m.group(2))))
    got = claim_answers(conn, session)
    if got:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": "\n\n".join(answer_prompt(q) for q in got)}}))


def stop(data: dict) -> None:
    session = data.get("session_id")
    if not session:
        return
    conn = connect()
    wait = min(MAX_WAIT, int(load_config().get("claude_stop_wait_minutes", 10)) * 60)
    deadline = time.time() + wait
    while True:
        got = claim_answers(conn, session)
        if got:
            print(json.dumps({"decision": "block", "reason": "\n\n".join(answer_prompt(q) for q in got)}))
            return
        still_open = conn.execute("SELECT 1 FROM questions WHERE session_id=? AND status='open'",
                                  (session,)).fetchone()
        if not still_open or time.time() >= deadline:
            return
        time.sleep(POLL_SECONDS)


def prompt(data: dict) -> None:
    session = data.get("session_id")
    got = claim_answers(connect(), session) if session else []
    if got:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "\n\n".join(answer_prompt(q) for q in got)}}))


HANDLERS = {"post-tool-use": post_tool_use, "stop": stop, "prompt": prompt}


def run(event: str) -> int:
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    try:
        HANDLERS[event](data)
    except Exception as e:  # a broken hook must never break the agent
        print(f"decyde hook {event}: {e}", file=sys.stderr)
    return 0
