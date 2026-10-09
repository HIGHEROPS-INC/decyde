"""Start a turn in a running Codex session through its app-server daemon.

Codex (0.161+) runs every session through a shared app-server reachable on a Unix socket
(a WebSocket upgrade, then JSON-RPC). A turn started here appears in the session's own
terminal as if typed, and wakes an idle session. Stdlib only.
"""
from __future__ import annotations

import base64
import json
import os
import socket
import struct
from pathlib import Path

from decyde import __version__

CONTROL_SOCKET = Path.home() / ".codex" / "app-server-control" / "app-server-control.sock"


class Client:
    def __init__(self, path: Path = CONTROL_SOCKET, timeout: float = 10):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect(str(path))
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(1)
            if not chunk:
                raise ConnectionError("codex app-server closed during handshake")
            head += chunk
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise ConnectionError("codex app-server refused the upgrade")
        self.next_id = 0
        self.call("initialize", {"clientInfo": {"name": "decyde", "title": "decyde", "version": __version__}})
        self.send({"jsonrpc": "2.0", "method": "initialized", "params": {}})

    def close(self):
        self.sock.close()

    def send(self, obj: dict) -> None:
        data = json.dumps(obj).encode()
        n, mask = len(data), os.urandom(4)
        if n < 126:
            hdr = bytes([0x81, 0x80 | n])
        elif n < 65536:
            hdr = bytes([0x81, 0x80 | 126]) + struct.pack(">H", n)
        else:
            hdr = bytes([0x81, 0x80 | 127]) + struct.pack(">Q", n)
        self.sock.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _exact(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("codex app-server closed the connection")
            buf += chunk
        return buf

    def _frame(self) -> tuple[int, bytes]:
        b1, b2 = self._exact(2)
        n = b2 & 0x7F
        if n == 126:
            n = struct.unpack(">H", self._exact(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", self._exact(8))[0]
        if b2 & 0x80:
            self._exact(4)
        return b1 & 0x0F, self._exact(n)

    def call(self, method: str, params: dict) -> dict:
        self.next_id += 1
        rid = self.next_id
        self.send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        while True:
            op, payload = self._frame()
            if op == 8:
                raise ConnectionError("codex app-server closed the connection")
            if op == 1:
                msg = json.loads(payload)
                if msg.get("id") == rid:
                    if "error" in msg:
                        raise RuntimeError(msg["error"].get("message", str(msg["error"])))
                    return msg.get("result", {})


def deliver(thread_id: str, text: str) -> str:
    """Start a turn with `text`, or add it to the turn already running. Returns a delivery state."""
    if not CONTROL_SOCKET.exists():
        return "skipped: codex app-server not running"
    client = Client()
    try:
        thread = client.call("thread/read", {"threadId": thread_id, "includeTurns": False}).get("thread") or {}
        item = [{"type": "text", "text": text}]
        if (thread.get("status") or {}).get("type") == "idle":
            client.call("turn/start", {"threadId": thread_id, "input": item})
            return "pushed via codex app-server"
        # Busy: add the answer to the running turn, which the agent reads at its next step.
        try:
            turns = client.call("thread/read", {"threadId": thread_id, "includeTurns": True})["thread"]["turns"]
            running = [t["id"] for t in turns or [] if t.get("status") == "inProgress"]
        except (RuntimeError, KeyError, TypeError):
            running = []
        if not running:
            return "pending"  # retried shortly; starts a turn once the session is idle
        client.call("turn/steer", {"threadId": thread_id, "input": item, "expectedTurnId": running[-1]})
        return "pushed via codex app-server"
    finally:
        client.close()
