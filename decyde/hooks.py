"""Agent hooks for Claude Code, Codex and Grok, which share one hook protocol.

  prompt         turns decyde on or off for the session when the human says so
                 ("use decyde", "decyde off", /decyde on), and hands over any late answer
  post-tool-use  remembers which session ran `decyde ask`, and hands a working agent any
                 new answer at its next tool call (as typed messages arrive mid-turn)
  stop           when decyde is on and the agent tries to end its turn on an ask it never
                 posted, sends it back once to post it. In a plain terminal (no Herdr or
                 tmux pane to type into) a session with an open question waits for the
                 first answer (default up to 24h) and gets it handed back; Esc interrupts

Claude and Codex send snake_case fields, Grok camelCase; `field` reads either.
"""
from __future__ import annotations

import json
import re
import sys
import time

from decyde.delivery import answer_prompt
from decyde.store import connect, human_name, now, row_dict, stop_wait_minutes

ASK_RE = re.compile(r"\b(decyde|dhub)\s+ask\b")
ID_RE = re.compile(r"question #(\d+)|\"id\":\s*(\d+)")
POLL_SECONDS = 3
UNCLAIMED = "(delivery IS NULL OR (delivery NOT LIKE 'pushed%' AND delivery != 'pushing'))"

# Toggles are instructions, so they only count outside questions ("is decyde on by default?")
# and "decyde on/off" only when it ends the phrase ("decyde on", "decyde off for now"),
# not mid-sentence ("the version of decyde on this mac").
_TAIL = r"(?=\s*$|\s+(?:for|now|please|again|thanks)\b)"
TURN_ON = re.compile(r"\b(?:use|enable|start using|turn on)\s+decyde\b|\bdecyde\s+on" + _TAIL, re.I)
TURN_OFF = re.compile(r"\b(?:stop using|disable|turn off|don'?t use|do not use)\s+decyde\b|\bdecyde\s+off"
                      + _TAIL, re.I)


def toggle(text: str) -> bool | None:
    """True to turn on, False to turn off, None when the prompt is not a toggle."""
    for sentence in re.findall(r"[^.!?\n]*[.!?\n]?", text):
        if sentence.rstrip().endswith("?"):
            continue
        body = sentence.strip().rstrip(".!")
        if TURN_OFF.search(body):
            return False
        if TURN_ON.search(body):
            return True
    return None
# Phrases that hand something to the human. Deliberately specific: a false alarm costs
# the agent one extra step, but a vague pattern would fire on ordinary summaries.
ASK_PHRASES = re.compile(
    r"your call|up to you|leave (it|this|that) (to|with) you|let me know|want me to|would you like|"
    r"do you want|should i\b|shall i\b|tell me (to|if|whether|which)|say the word|"
    r"(need|needs|waiting on|waiting for) your|your (decision|approval|confirmation|go-ahead|sign-off)|"
    r"(please|can you|could you) (confirm|approve|decide|choose|pick|provide|log in|sign in|run|check)|"
    r"before i (proceed|continue|run|merge|deploy|push|delete)", re.I)


def field(data: dict, snake: str, camel: str):
    return data.get(snake) if data.get(snake) is not None else data.get(camel)


def session_of(data: dict) -> str | None:
    return field(data, "session_id", "sessionId")


# ---------------------------------------------------------------- session state

def set_enabled(conn, session: str, on: bool) -> None:
    conn.execute("INSERT INTO sessions (session_id, enabled, updated_at) VALUES (?,?,?) "
                 "ON CONFLICT(session_id) DO UPDATE SET enabled=excluded.enabled, updated_at=excluded.updated_at",
                 (session, int(on), now()))


def session_row(conn, session: str):
    return conn.execute("SELECT * FROM sessions WHERE session_id=?", (session,)).fetchone()


def note_turn(conn, session: str) -> None:
    conn.execute("UPDATE sessions SET turn_started_at=? WHERE session_id=?", (now(), session))


def looks_like_ask(message: str) -> bool:
    if not message or "decyde #" in message.lower():
        return False
    tail = message.strip()[-400:]
    return bool(ASK_PHRASES.search(message)) or tail.endswith("?")


# ---------------------------------------------------------------- answers

def claim_answers(conn, session: str) -> list[dict]:
    """Answers for this session nobody has delivered yet, claimed atomically."""
    rows = conn.execute(
        "SELECT * FROM questions WHERE session_id=? AND status='answered' "
        f"AND {UNCLAIMED}", (session,)).fetchall()
    claimed = []
    for r in rows:
        cur = conn.execute(
            "UPDATE questions SET delivery='pushed via agent hook', delivered_at=?, updated_at=? "
            f"WHERE id=? AND {UNCLAIMED}", (now(), now(), r["id"]))
        if cur.rowcount:
            claimed.append(row_dict(r))
    return claimed


def context(event: str, text: str) -> None:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}))


# ---------------------------------------------------------------- events

def prompt(data: dict) -> None:
    session = session_of(data)
    if not session:
        return
    conn = connect()
    text = str(data.get("prompt") or "")
    notes = []
    state = toggle(text)
    if state is False:
        set_enabled(conn, session, False)
        notes.append("decyde is now OFF for this session: ask in chat as usual.")
    elif state:
        set_enabled(conn, session, True)
        notes.append(f"decyde is now ON for this session: every ask for {human_name()} goes through "
                     "`decyde ask` (see the decyde section of your instructions), not just chat.")
    note_turn(conn, session)
    notes += [answer_prompt(q) for q in claim_answers(conn, session)]
    if notes:
        context("UserPromptSubmit", "\n\n".join(notes))


def post_tool_use(data: dict) -> None:
    session = session_of(data)
    if not session:
        return
    conn = connect()
    if ASK_RE.search(json.dumps(field(data, "tool_input", "toolInput"))):
        output = json.dumps(field(data, "tool_response", "toolResult")).replace('\\"', '"')
        for a, b in ID_RE.findall(output):  # one command may ask several questions
            conn.execute("UPDATE questions SET session_id=? WHERE id=? AND session_id IS NULL",
                         (session, int(a or b)))
    got = claim_answers(conn, session)
    if got:
        context("PostToolUse", "\n\n".join(answer_prompt(q) for q in got))


def unposted_ask(conn, session: str, data: dict) -> bool:
    """decyde is on, the agent's final message hands something to the human, it posted
    nothing this turn, and it has not been sent back for this already this turn."""
    row = session_row(conn, session)
    if not row or not row["enabled"]:
        return False
    if not looks_like_ask(str(field(data, "last_assistant_message", "lastAssistantMessage") or "")):
        return False
    since = row["turn_started_at"] or "0"
    if row["nudged_at"] and row["nudged_at"] >= since:
        return False  # already sent back once this turn; never loop
    asked = conn.execute("SELECT 1 FROM questions WHERE session_id=? AND created_at >= ?",
                         (session, since)).fetchone()
    return not asked


def stop(data: dict) -> None:
    session = session_of(data)
    if not session:
        return
    conn = connect()
    if unposted_ask(conn, session, data):
        conn.execute("UPDATE sessions SET nudged_at=? WHERE session_id=?", (now(), session))
        name = human_name()
        print(json.dumps({"decision": "block", "reason": (
            f"[decyde] decyde is on for this session and your last message hands something to {name} "
            f"(a decision, a confirmation, or a step only {name} can take), but nothing was posted to "
            f"decyde this turn. Post it now with `decyde ask` (one question per decision, with your "
            f"recommendation), then end your turn with 'Waiting on decyde #N'. If nothing in it actually "
            f"needs {name}, end your turn without posting.")}))
        return
    got = claim_answers(conn, session)
    if got:
        print(json.dumps({"decision": "block", "reason": "\n\n".join(answer_prompt(q) for q in got)}))
        return
    open_ids = [r["id"] for r in conn.execute(
        "SELECT id FROM questions WHERE session_id=? AND status='open'", (session,))]
    if not open_ids or has_push_route(conn, session):
        # decyde can wake this session when the answer comes (pane, Claude inbox, Codex
        # app-server, or a background `decyde wait`). Holding the turn here would only make
        # the answer queue up behind the hook, so let the agent stop.
        return
    # Nothing can wake this session (an older agent version, say), so hold the turn until
    # any one answer arrives (not all of them), then hand it back so the agent continues.
    deadline = time.time() + stop_wait_minutes() * 60
    marks = ",".join("?" * len(open_ids))
    while time.time() < deadline:
        time.sleep(POLL_SECONDS)
        got = claim_answers(conn, session)
        if got:
            print(json.dumps({"decision": "block", "reason": "\n\n".join(answer_prompt(q) for q in got)}))
            return
        if conn.execute(f"SELECT 1 FROM questions WHERE id IN ({marks}) AND status != 'open'",
                        open_ids).fetchone():
            return  # answered or withdrawn some other way; let the agent pick it up


def has_push_route(conn, session: str) -> bool:
    """Can decyde wake this session when an answer comes? Then the hook must not hold it."""
    from decyde.delivery import waiter_alive
    rows = conn.execute("SELECT * FROM questions WHERE session_id=? AND status='open'", (session,)).fetchall()
    return any(r["herdr_pane"] or r["tmux_pane"] or r["inbox_socket"] or r["codex_thread"]
               or waiter_alive(dict(r)) for r in rows)


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
