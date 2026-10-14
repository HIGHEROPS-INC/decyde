"""Answer decyde questions from your phone through your own Telegram bot.

Opt-in (`decyde telegram setup`). While connected, the server sends each open question to
the bot's chat with one button per option, and long-polls Telegram for your taps and
replies. Only the Telegram account that set the bot up can answer; updates from anyone else
are ignored. Nothing listens on your machine: every call is outbound to api.telegram.org.
Turning it on means question text passes through Telegram.
"""
from __future__ import annotations

import html
import json
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from decyde.store import connect, get_question, load_config, now, save_config

API = "https://api.telegram.org/bot{token}/{method}"
MAX_TEXT = 3800          # Telegram allows 4096; leave room for the status line
SYNC_SECONDS = 2


def settings() -> dict | None:
    t = load_config().get("telegram")
    return t if t and t.get("token") and t.get("chat_id") and t.get("user_id") else None


def call(token: str, method: str, timeout: float = 15, **params) -> dict:
    data = urllib.parse.urlencode({k: json.dumps(v) if isinstance(v, (dict, list)) else v
                                   for k, v in params.items()}).encode()
    req = urllib.request.Request(API.format(token=token, method=method), data=data)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = json.loads(e.read() or b"{}")
    if not body.get("ok"):
        raise RuntimeError(body.get("description", f"telegram {method} failed"))
    return body["result"]


# ---------------------------------------------------------------- formatting

def esc(text) -> str:
    return html.escape(str(text or ""), quote=False)


def render(q: dict) -> str:
    who = q["agent"] + (f" · {q['agent_name']}" if q.get("agent_name") else "")
    where = (q.get("project") or "") + (f" ({q['git_branch']})" if q.get("git_branch") else "")
    parts = [f"<b>#{q['id']} {esc(q['title'])}</b>",
             f"<i>{esc(who)}</i>" + (f" · {esc(where)}" if where else ""),
             f"Working on: {esc(q['task'])}", "", esc(q["question"])]
    if q.get("context"):
        parts += ["", f"<b>Context</b>\n{esc(q['context'])}"]
    if q.get("options"):
        parts += ["", "<b>Options</b>"] + [f"{i}. {esc(o)}" for i, o in enumerate(q["options"], 1)]
    if q.get("recommendation"):
        parts += ["", f"<b>Recommends</b>\n{esc(q['recommendation'])}"]
    if q.get("urgency") == "high":
        parts.insert(0, "🔴 <b>Blocking</b>")
    text = "\n".join(parts)
    if len(text) > MAX_TEXT:
        text = text[:MAX_TEXT] + "…"
    return text


def keyboard(q: dict) -> dict:
    rows = [[{"text": f"{i}. {o}"[:60], "callback_data": f"a:{q['id']}:{i - 1}"}]
            for i, o in enumerate(q.get("options") or [], 1)]
    rows.append([{"text": "Dismiss", "callback_data": f"d:{q['id']}"}])
    return {"inline_keyboard": rows}


def closed_line(q: dict) -> str:
    if q["status"] == "cancelled":
        return "\n\n<b>Closed:</b> " + esc((q.get("answer") or "withdrawn")[:300])
    return "\n\n✅ <b>Answered:</b> " + esc((q.get("answer") or "")[:600])


# ---------------------------------------------------------------- server loops

class Bot:
    def __init__(self, cfg: dict):
        self.token, self.chat_id, self.user_id = cfg["token"], int(cfg["chat_id"]), int(cfg["user_id"])

    def api(self, method: str, **params):
        return call(self.token, method, **params)

    # outgoing: post new questions, close answered ones
    def sync(self, conn) -> None:
        for r in conn.execute("SELECT q.id FROM questions q LEFT JOIN telegram_messages t ON t.qid = q.id "
                              "WHERE q.status='open' AND t.qid IS NULL ORDER BY q.id"):
            q = get_question(conn, r["id"])
            msg = self.api("sendMessage", chat_id=self.chat_id, text=render(q), parse_mode="HTML",
                           reply_markup=keyboard(q))
            conn.execute("INSERT OR REPLACE INTO telegram_messages (qid, chat_id, message_id, closed) "
                         "VALUES (?,?,?,0)", (q["id"], self.chat_id, msg["message_id"]))
        for r in conn.execute("SELECT t.qid, t.message_id FROM telegram_messages t JOIN questions q ON q.id = t.qid "
                              "WHERE t.closed=0 AND q.status != 'open'").fetchall():
            q = get_question(conn, r["qid"])
            try:
                self.api("editMessageText", chat_id=self.chat_id, message_id=r["message_id"],
                         text=render(q) + closed_line(q), parse_mode="HTML")
            except RuntimeError as e:
                if "not modified" not in str(e) and "not found" not in str(e):
                    raise
            conn.execute("UPDATE telegram_messages SET closed=1 WHERE qid=?", (r["qid"],))

    # incoming: taps and replies from the owner only
    def handle(self, conn, update: dict) -> None:
        from decyde.delivery import dismiss_question, record_answer
        if "callback_query" in update:
            cb = update["callback_query"]
            if cb.get("from", {}).get("id") != self.user_id:
                return
            kind, _, rest = cb.get("data", "").partition(":")
            qid, _, idx = rest.partition(":")
            q = get_question(conn, int(qid)) if qid.isdigit() else None
            note = "Already closed"
            if q and q["status"] == "open":
                if kind == "a" and idx.isdigit() and int(idx) < len(q["options"]):
                    record_answer(conn, q["id"], q["options"][int(idx)])
                    note = "Answered"
                elif kind == "d":
                    dismiss_question(conn, q["id"])
                    note = "Dismissed"
            self.api("answerCallbackQuery", callback_query_id=cb["id"], text=note)
            return
        msg = update.get("message") or {}
        if msg.get("from", {}).get("id") != self.user_id or msg.get("chat", {}).get("id") != self.chat_id:
            return
        text = (msg.get("text") or "").strip()
        if text.startswith("/open") or text.startswith("/start"):
            self.list_open(conn)
            return
        replied = (msg.get("reply_to_message") or {}).get("message_id")
        row = conn.execute("SELECT qid FROM telegram_messages WHERE message_id=?", (replied,)).fetchone() \
            if replied else None
        if not row:
            self.api("sendMessage", chat_id=self.chat_id,
                     text="Reply to a question's message to answer it, or tap one of its buttons. /open lists what is waiting.")
            return
        q = get_question(conn, row["qid"])
        if not q or q["status"] != "open":
            self.api("sendMessage", chat_id=self.chat_id, reply_to_message_id=replied,
                     text=f"#{row['qid']} is already closed.")
            return
        if text:
            record_answer(conn, q["id"], text)

    def list_open(self, conn) -> None:
        rows = conn.execute("SELECT id, agent, title FROM questions WHERE status='open' ORDER BY id").fetchall()
        text = "Nothing is waiting on you." if not rows else "Waiting on you:\n" + "\n".join(
            f"#{r['id']} {r['agent']}: {r['title']}" for r in rows)
        self.api("sendMessage", chat_id=self.chat_id, text=text)
        conn.execute("DELETE FROM telegram_messages WHERE qid IN (SELECT id FROM questions WHERE status='open')")


def run(stop: threading.Event) -> None:
    """Server thread: one sync loop and one long-poll loop, while Telegram is configured."""
    cfg = settings()
    if not cfg:
        return
    bot = Bot(cfg)

    def syncing():
        conn = connect()
        while not stop.wait(SYNC_SECONDS):
            try:
                bot.sync(conn)
            except Exception as e:  # network blips must not kill the server
                print(f"telegram sync: {e}", file=sys.stderr, flush=True)
                stop.wait(10)

    threading.Thread(target=syncing, daemon=True).start()
    conn, offset = connect(), None
    while not stop.is_set():
        try:
            params = {"timeout": 25, "allowed_updates": ["message", "callback_query"]}
            if offset is not None:
                params["offset"] = offset
            for update in bot.api("getUpdates", timeout=35, **params):
                offset = update["update_id"] + 1
                try:
                    bot.handle(conn, update)
                except Exception as e:
                    print(f"telegram update: {e}", file=sys.stderr, flush=True)
        except Exception as e:
            print(f"telegram poll: {e}", file=sys.stderr, flush=True)
            stop.wait(10)


# ---------------------------------------------------------------- CLI

def restart_server() -> None:
    from decyde.install import restart_service
    restart_service()


def cmd_setup(args) -> int:
    cfg = load_config()
    if cfg.pop("telegram", None):
        save_config(cfg)
        restart_server()  # stop the old poller, or the two would fight over updates
    token = args.token or input("Bot token from @BotFather (create a new bot, not one another app uses): ").strip()
    try:
        bot = call(token, "getMe")
    except (RuntimeError, OSError) as e:
        print(f"That token did not work: {e}", file=sys.stderr)
        return 1
    call(token, "deleteWebhook")
    print(f"Connected to @{bot['username']}. Open https://t.me/{bot['username']} on your phone and press Start.")
    print("Waiting for your message (5 minutes)...")
    deadline, offset = time.time() + 300, None
    while time.time() < deadline:
        params = {"timeout": 20, "allowed_updates": ["message"]}
        if offset is not None:
            params["offset"] = offset
        for u in call(token, "getUpdates", timeout=30, **params):
            offset = u["update_id"] + 1
            m = u.get("message") or {}
            if m.get("chat", {}).get("type") == "private" and m.get("from"):
                cfg = load_config()
                cfg["telegram"] = {"token": token, "bot": bot["username"], "chat_id": m["chat"]["id"],
                                   "user_id": m["from"]["id"], "connected_at": now()}
                save_config(cfg)
                call(token, "getUpdates", offset=offset, timeout=0)  # don't replay the Start to the server
                call(token, "sendMessage", chat_id=m["chat"]["id"],
                     text="decyde is connected. Questions from your agents will arrive here; tap an option "
                          "or reply to a question to answer it. /open lists what is waiting.")
                restart_server()
                print(f"Done: questions now go to @{bot['username']}, and only your account can answer.")
                return 0
    print("No message arrived; run `decyde telegram setup` again.", file=sys.stderr)
    return 1


def cmd_off(args) -> int:
    cfg = load_config()
    if cfg.pop("telegram", None):
        save_config(cfg)
        restart_server()
        print("Telegram disconnected. (Delete the bot in @BotFather if you no longer need it.)")
    else:
        print("Telegram is not connected.")
    return 0


def cmd_status(args) -> int:
    t = settings()
    print(f"connected to @{t['bot']} since {t['connected_at']}" if t else "not connected")
    return 0
