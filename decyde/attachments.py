"""Images sent with an answer.

Every route to an agent carries text, so an image travels as a file: it is saved under
~/.decyde/attachments/<question id>/ and the answer names the path, which the agent opens
with its own image tool. The answer text refers to each one as [Image #N].
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

from decyde.store import HOME, human_name

DIR = HOME / "attachments"
MAX_BYTES = 20 * 1024 * 1024
MAX_IMAGES = 10
KEEP_DAYS = 30
CONVERT = ("heic", "tiff", "bmp")  # formats agents may not open; turned into PNG where `sips` exists
TYPES = {"png": "image/png", "jpg": "image/jpeg", "gif": "image/gif", "webp": "image/webp",
         "heic": "image/heic", "tiff": "image/tiff", "bmp": "image/bmp"}
TOKEN = re.compile(r"\[Image #(\d+)\]")
EXT_END = re.compile(r"\.(?:png|jpe?g|gif|webp|heic|heif|tiff?|bmp)(?![\w/\\-])", re.I)


def sniff(data: bytes) -> str | None:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[4:8] == b"ftyp" and data[8:12] in (b"heic", b"heix", b"heif", b"hevc", b"mif1", b"msf1"):
        return "heic"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    if data[:2] == b"BM":
        return "bmp"
    return None


def save(qid: int, data: bytes) -> Path:
    """Store one image for a question. ValueError says why it was refused."""
    kind = sniff(data)
    if not kind:
        raise ValueError("that is not an image (PNG, JPEG, GIF, WebP, HEIC, TIFF or BMP)")
    if len(data) > MAX_BYTES:
        raise ValueError(f"the image is larger than {MAX_BYTES // 2 ** 20} MB")
    folder = DIR / str(qid)
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    n = max((int(p.stem) for p in folder.iterdir() if p.stem.isdigit()), default=0) + 1
    path = folder / f"{n}.{kind}"
    path.write_bytes(data)
    path.chmod(0o600)
    if kind in CONVERT and shutil.which("sips"):
        png = path.with_suffix(".png")
        try:
            done = subprocess.run(["sips", "-s", "format", "png", str(path), "--out", str(png)],
                                  capture_output=True, timeout=30).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            done = False
        if done and png.exists():
            path.unlink()
            png.chmod(0o600)
            path = png
    return path


def entry(path, source: str | None = None) -> dict:
    return {"path": str(path), **({"from": source} if source else {})}


def discard(entries) -> None:
    for e in entries:
        try:
            os.unlink(e["path"])
        except OSError:
            pass


def with_tokens(text: str, count: int) -> str:
    """The answer text, naming every image: ones it does not mention are added at the end."""
    have = {int(n) for n in TOKEN.findall(text)}
    missing = " ".join(f"[Image #{i}]" for i in range(1, count + 1) if i not in have)
    return (text + ("\n\n" if text and missing else "") + missing).strip()


def note(q: dict) -> str:
    """What an agent is told about the images, after the answer text."""
    items = q.get("attachments") or []
    if not items:
        return ""
    lines = "\n".join(f"[Image #{i}] {a['path']}" + (f" (copied from {a['from']})" if a.get("from") else "")
                      for i, a in enumerate(items, 1))
    what = "an image" if len(items) == 1 else f"{len(items)} images"
    return (f"\n\n{human_name()} attached {what}. Open each file with your image or file read tool "
            f"before you act on the answer:\n{lines}")


# ---------------------------------------------------------------- the terminal: clipboard and dropped paths

def clipboard_image() -> bytes | None:
    """The image on the clipboard (or the image file copied in Finder), if there is one."""
    def out(cmd):
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return r.stdout if r.returncode == 0 else None

    if sys.platform == "darwin":
        copied = out(["osascript", "-e", "POSIX path of (the clipboard as «class furl»)"])
        if copied:
            try:
                data = Path(copied.decode().strip()).read_bytes()
            except OSError:
                data = b""
            if sniff(data):
                return data
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "clip")
            for cls in ("PNGf", "JPEG", "TIFF"):
                if out(["osascript", "-e", f"set d to the clipboard as «class {cls}»",
                        "-e", f'set f to open for access POSIX file "{target}" with write permission',
                        "-e", "write d to f", "-e", "close access f"]) is not None:
                    data = Path(target).read_bytes()
                    if sniff(data):
                        return data
        return None
    for cmd in (["wl-paste", "--no-newline", "--type", "image/png"],
                ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"]):
        data = out(cmd) if shutil.which(cmd[0]) else None
        if data and sniff(data):
            return data
    return None


def absorb_paths(qid: int, text: str, files: dict) -> str:
    """Swap image file paths in the text (as a terminal types them when a file is dropped)
    for [Image #N], copying each file so a temporary screenshot outlives its folder.
    `files` maps N to its entry and is added to."""
    pos = 0
    while len(files) < MAX_IMAGES:
        end = EXT_END.search(text, pos)
        if not end:
            break
        pos = end.end()
        for i in range(end.start()):
            if not (text.startswith("/", i) or text.startswith("~/", i)):
                continue
            if i and not (text[i - 1].isspace() or text[i - 1] in "'\"(["):
                continue
            raw = text[i:end.end()]
            source = next((p for p in (os.path.expanduser(re.sub(r"\\(.)", r"\1", raw)), os.path.expanduser(raw))
                           if os.path.isfile(p)), None)
            if not source:
                continue
            try:
                if os.path.getsize(source) > MAX_BYTES:
                    raise ValueError("too large")
                saved = save(qid, Path(source).read_bytes())
            except (OSError, ValueError):
                break  # leave the path as typed
            n = max(files, default=0) + 1
            files[n] = entry(saved, source)
            a, b = i, end.end()
            if a and b < len(text) and text[a - 1] == text[b] and text[b] in "'\"":
                a, b = a - 1, b + 1  # the terminal quoted the path
            token = f"[Image #{n}]"
            text = text[:a] + token + text[b:]
            pos = a + len(token)
            break
    return text


def finalize(text: str, files: dict) -> tuple[str, list[dict]]:
    """Number the images in the order the text mentions them and drop any whose mention
    was deleted. Returns the text and the entries, in order."""
    order = []
    for n in (int(n) for n in TOKEN.findall(text)):
        if n in files and n not in order:
            order.append(n)
    discard(e for n, e in files.items() if n not in order)
    text = TOKEN.sub(lambda m: f"[Image #{order.index(int(m[1])) + 1}]" if int(m[1]) in order else m[0], text)
    return text, [files[n] for n in order]


# ---------------------------------------------------------------- housekeeping

def prune(conn) -> None:
    """Delete images whose question is gone, withdrawn, or settled more than KEEP_DAYS ago."""
    if not DIR.is_dir():
        return
    for folder in DIR.iterdir():
        row = conn.execute("SELECT status, updated_at FROM questions WHERE id=?", (folder.name,)).fetchone() \
            if folder.name.isdigit() else None
        old = row and row["status"] != "open" and \
            time.time() - datetime.fromisoformat(row["updated_at"]).timestamp() > KEEP_DAYS * 86400
        if not row or row["status"] == "cancelled" or old:
            shutil.rmtree(folder, ignore_errors=True)
