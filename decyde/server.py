"""The web UI and its JSON/SSE API, bound to localhost only."""
from __future__ import annotations

import base64
import binascii
import json
import queue
import re
import socket
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from decyde import __version__, attachments
from decyde.delivery import deliver, delivery_loop, dismiss_question, notify_new, record_answer
from decyde import update
from decyde.herdr_sidebar import REFRESH_SECONDS, Sidebar
from decyde.store import DB_PATH, change_marker, connect, get_question, human_name, row_dict

INDEX_HTML = Path(__file__).resolve().parent / "web" / "index.html"
IMAGE_URL = re.compile(r"/api/questions/(\d+)/images/(\d+)")
MAX_BODY = 64 * 1024 * 1024  # an answer with its images, base64 encoded


class Hub:
    def __init__(self):
        self.subscribers: list[queue.Queue] = []
        self.lock = threading.Lock()

    def broadcast(self, event: str) -> None:
        with self.lock:
            for s in list(self.subscribers):
                s.put(event)

    def watch(self, stop: threading.Event) -> None:
        """Agents write straight to SQLite, so poll for changes and fan them out over SSE."""
        conn = connect()
        marker = change_marker(conn)
        last_id = conn.execute("SELECT COALESCE(MAX(id), 0) FROM questions").fetchone()[0]
        sidebar = Sidebar()
        sidebar.sync(conn, time.time())
        while not stop.wait(1):
            m = change_marker(conn)
            if m == marker:
                if time.time() - sidebar.last_full >= REFRESH_SECONDS:
                    sidebar.sync(conn, time.time())  # renew the TTL on marks still showing
                continue
            sidebar.sync(conn, time.time())
            marker = m
            for r in conn.execute("SELECT * FROM questions WHERE id > ? ORDER BY id", (last_id,)):
                q = row_dict(r)
                last_id = q["id"]
                if q["status"] == "open":
                    notify_new(q)
            self.broadcast("changed")
            # Answers saved elsewhere (the TUI) are pushed from here, within a second.
            for r in conn.execute("SELECT id FROM questions WHERE status='answered' AND delivery='pending'"):
                threading.Thread(target=lambda qid=r["id"]: deliver(connect(), qid), daemon=True).start()


HUB = Hub()


class Handler(BaseHTTPRequestHandler):
    server_version = f"decyde/{__version__}"

    def log_message(self, *a):
        pass

    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ValueError("the images are too large to send together")
        return json.loads(self.rfile.read(n) or b"{}")

    def send_image(self, qid: str, number: str) -> None:
        q = get_question(connect(), int(qid))
        items = q["attachments"] if q else []
        path = Path(items[int(number) - 1]["path"]) if 0 < int(number) <= len(items) else None
        if not path or not path.is_file():
            return self.send_json({"error": "not found"}, 404)
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", attachments.TYPES.get(attachments.sniff(body), "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "private, max-age=3600")
        self.end_headers()
        self.wfile.write(body)

    def local_host(self) -> bool:
        # Refuse DNS-rebinding: a hostile page that resolves its own name to 127.0.0.1
        # would otherwise be able to read questions and type answers into agents.
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        return host in ("127.0.0.1", "localhost")

    def local_origin(self) -> bool:
        # Reject cross-site writes from other pages open in the browser.
        origin = self.headers.get("Origin")
        return origin is None or urlparse(origin).hostname in ("127.0.0.1", "localhost")

    def do_GET(self):
        if not self.local_host():
            return self.send_json({"error": "forbidden"}, 403)
        path = urlparse(self.path).path
        if path == "/":
            body = INDEX_HTML.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/meta":
            self.send_json({"name": human_name(), "host": socket.gethostname().split(".")[0],
                            "version": __version__, "update": update.available()})
        elif path == "/api/questions":
            since = datetime.fromtimestamp(time.time() - 14 * 86400, timezone.utc).isoformat()
            rows = connect().execute(
                "SELECT * FROM questions WHERE status='open' OR created_at > ? ORDER BY id DESC LIMIT 300",
                (since,)).fetchall()
            self.send_json([row_dict(r) for r in rows])
        elif path == "/api/events":
            self.stream_events()
        elif IMAGE_URL.fullmatch(path):  # /api/questions/<id>/images/<n>
            self.send_image(*IMAGE_URL.fullmatch(path).groups())
        else:
            self.send_json({"error": "not found"}, 404)

    def stream_events(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        q: queue.Queue = queue.Queue()
        with HUB.lock:
            HUB.subscribers.append(q)
        try:
            self.wfile.write(b"event: hello\ndata: {}\n\n")
            self.wfile.flush()
            while True:
                try:
                    self.wfile.write(f"event: {q.get(timeout=20)}\ndata: {{}}\n\n".encode())
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except OSError:
            pass
        finally:
            with HUB.lock:
                HUB.subscribers.remove(q)

    def do_POST(self):
        if not self.local_host() or not self.local_origin():
            return self.send_json({"error": "forbidden"}, 403)
        parts = urlparse(self.path).path.strip("/").split("/")
        # /api/questions/<id>/answer | /dismiss
        if len(parts) != 4 or parts[:2] != ["api", "questions"] or not parts[2].isdigit():
            return self.send_json({"error": "not found"}, 404)
        qid, action = int(parts[2]), parts[3]
        conn = connect()
        q = get_question(conn, qid)
        if not q:
            return self.send_json({"error": "no such question"}, 404)
        if action == "answer":
            try:
                body = self.read_json()
            except ValueError as e:
                return self.send_json({"error": str(e)}, 413)
            text = (body.get("answer") or "").strip()
            images = body.get("images") or []  # base64 strings
            if not text and not images:
                return self.send_json({"error": "answer is empty"}, 400)
            if q["status"] == "cancelled":
                return self.send_json({"error": "the agent withdrew this question"}, 409)
            if len(images) > attachments.MAX_IMAGES:
                return self.send_json({"error": f"an answer takes at most {attachments.MAX_IMAGES} images"}, 400)
            saved = []
            try:
                for data in images:
                    saved.append(attachments.entry(attachments.save(qid, base64.b64decode(data))))
            except (ValueError, TypeError, binascii.Error, OSError) as e:
                attachments.discard(saved)
                return self.send_json({"error": f"could not attach: {e}"}, 400)
            record_answer(conn, qid, text, attachments=saved)
        elif action == "dismiss":
            dismiss_question(conn, qid)
        else:
            return self.send_json({"error": "unknown action"}, 404)
        HUB.broadcast("changed")
        self.send_json(get_question(conn, qid))


def update_loop(stop: threading.Event) -> None:
    """Hourly: refresh the cached latest version (check() itself only hits GitHub once a
    day) and clear out old answer images."""
    while True:
        update.check()
        try:
            attachments.prune(connect())
        except Exception as e:  # housekeeping must not stop the update check
            print(f"attachment cleanup: {e}", file=sys.stderr, flush=True)
        if stop.wait(3600):
            return


def serve(port: int) -> None:
    connect()
    stop = threading.Event()
    threading.Thread(target=HUB.watch, args=(stop,), daemon=True).start()
    threading.Thread(target=delivery_loop, args=(stop,), daemon=True).start()
    threading.Thread(target=update_loop, args=(stop,), daemon=True).start()
    from decyde import telegram
    threading.Thread(target=telegram.run, args=(stop,), daemon=True).start()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    print(f"decyde on http://127.0.0.1:{port}  (db: {DB_PATH})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        stop.set()
