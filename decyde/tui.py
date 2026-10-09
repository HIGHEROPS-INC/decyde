"""Terminal UI: keyboard and mouse, purple/teal/grey."""
from __future__ import annotations

import base64
import os
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from decyde import splash, update
from decyde.delivery import dismiss_question, record_answer
from decyde.store import PORT, change_marker, connect, get_question, human_name, load_config, row_dict

def tui(show_splash: bool = True) -> None:
    import curses
    import tempfile
    import textwrap

    VIEWS = (("open", "needs you", ("open",)),
             ("answered", "answered", ("answered", "acknowledged")),
             ("all", "history", ("open", "answered", "acknowledged", "cancelled")))
    # The system ncurses on macOS cannot report wheel-down, so read SGR mouse
    # sequences (xterm 1000 + 1006) ourselves instead of using curses.getmouse.
    # 1002 adds motion-while-pressed, which drag-to-copy needs.
    MOUSE_ON, MOUSE_OFF = "\x1b[?1000h\x1b[?1002h\x1b[?1006h", "\x1b[?1006l\x1b[?1002l\x1b[?1000l"
    ESC_KEYS = {"[A": "up", "OA": "up", "[B": "down", "OB": "down", "[5~": "pgup", "[6~": "pgdn",
                "[H": "home", "OH": "home", "[F": "end", "OF": "end", "[Z": "backtab"}

    def mouse(on: bool):
        sys.stdout.write(MOUSE_ON if on else MOUSE_OFF)
        sys.stdout.flush()

    def run(scr):
        curses.curs_set(0)
        curses.use_default_colors()
        scr.keypad(False)
        rich = curses.COLORS >= 256
        pal = {  # name: (256-colour, 8-colour fallback)
            "purple1": (99, 5), "purple2": (135, 5), "purple3": (141, 5), "purple4": (183, 5),
            "teal1": (30, 6), "teal2": (37, 6), "teal3": (80, 6),
            "grey1": (238, 7), "grey2": (243, 7), "grey3": (247, 7), "grey4": (252, 7), "white": (255, 7),
        }
        C = {}
        for i, (name, (fg, fb)) in enumerate(pal.items(), start=1):
            curses.init_pair(i, fg if rich else fb, -1)
            C[name] = curses.color_pair(i)
        for i, (name, fg, bg, fb_fg, fb_bg) in enumerate((
                ("tabsel", 16, 141, 0, 5), ("btn", 252, 237, 7, 0), ("btnhot", 16, 80, 0, 6),
                ("rowsel", 255, 236, 7, 0), ("selhl", 16, 141, 0, 5)), start=30):
            curses.init_pair(i, fg if rich else fb_fg, bg if rich else fb_bg)
            C[name] = curses.color_pair(i)
        C.update(dim=C["grey2"], faint=C["grey1"], text=C["grey4"], accent=C["purple3"], ok=C["teal3"],
                 claude=C["purple4"], codex=C["teal3"], grok=C["grey4"], antigravity=C["teal2"],
                 gemini=C["teal2"], other=C["purple3"])
        if show_splash and load_config().get("splash", True):
            splash.play(scr, C)

        conn = connect()
        st = {"view": 0, "sel": 0, "scroll": 0, "dscroll": 0, "dsel": None, "flash": "",
              "marker": None, "seen": None, "rows": [], "hits": [], "list": (0, 0), "detail": (0, 0),
              "grid": [], "press": None, "selection": None}

        # ---- input

        def read_event(wait_ms):
            scr.timeout(wait_ms)
            try:
                ch = scr.get_wch()
            except curses.error:
                return None
            if ch == curses.KEY_RESIZE:
                return ("key", "resize")
            if ch == "\x1b":
                scr.timeout(30)
                seq = ""
                while len(seq) < 32:
                    try:
                        c = scr.get_wch()
                    except curses.error:
                        break
                    if not isinstance(c, str):
                        break
                    seq += c
                    if seq.startswith("[<"):
                        if c in "Mm":
                            break
                    elif len(seq) >= 2 and (c.isalpha() or c == "~"):
                        break
                if not seq:
                    return ("key", "esc")
                if seq.startswith("[<"):
                    try:
                        b, x, y = (int(v) for v in seq[2:-1].split(";"))
                    except ValueError:
                        return None
                    if seq[-1] == "m":
                        return ("release", x - 1, y - 1)
                    if b & 64:
                        return ("wheel", 1 if b & 1 else -1, x - 1, y - 1)
                    if b & 32 and b & 3 == 0:
                        return ("drag", x - 1, y - 1)
                    if b & ~28 == 0:
                        return ("click", x - 1, y - 1)
                    return None
                return ("key", ESC_KEYS.get(seq, "?"))
            if ch in ("\n", "\r"):
                return ("key", "enter")
            if ch in ("\x7f", "\b", curses.KEY_BACKSPACE):
                return ("key", "backspace")
            if ch == "\t":
                return ("key", "tab")
            return ("key", ch if isinstance(ch, str) else "?")

        def hit_at(x, y):
            for hy, x0, x1, action in st["hits"]:
                if hy == y and x0 <= x < x1:
                    return action
            return None

        # ---- drawing helpers

        def put(y, x, text, attr=0, w=None):
            h, W = scr.getmaxyx()
            if y < 0 or y >= h or x >= W:
                return
            w = W - x if w is None else min(w, W - x)
            row = st["grid"][y] if y < len(st["grid"]) else None
            if row is not None:
                for i, ch in enumerate(text[:max(0, w)]):
                    if x + i < len(row):
                        row[x + i] = ch
            try:
                scr.addnstr(y, x, text, max(0, w), attr)
            except curses.error:
                pass

        def button(y, x, label, action, attr=None):
            text = f" {label} "
            put(y, x, text, C["btn"] if attr is None else attr)
            st["hits"].append((y, x, x + len(text), action))
            return x + len(text) + 1

        def age(iso):
            s = time.time() - datetime.fromisoformat(iso).timestamp()
            return "now" if s < 60 else f"{int(s // 60)}m" if s < 3600 else f"{int(s // 3600)}h" if s < 86400 else f"{int(s // 86400)}d"

        # ---- data

        def load():
            rows = [row_dict(r) for r in conn.execute("SELECT * FROM questions ORDER BY id DESC LIMIT 300")]
            if st["seen"] is not None:
                new = [q for q in rows if q["id"] not in st["seen"] and q["status"] == "open"]
                if new:
                    curses.beep()
                    st["flash"] = f"new from {new[0]['agent']}: {new[0]['title']}"
            st["seen"] = {q["id"] for q in rows}
            st["rows"] = rows

        def visible():
            want = VIEWS[st["view"]][2]
            items = [q for q in st["rows"] if q["status"] in want]
            if VIEWS[st["view"]][0] == "open":
                items.sort(key=lambda q: (q["urgency"] != "high", q["id"]))
            return items

        def current():
            items = visible()
            return items[st["sel"]] if items else None

        def detail_lines(q, wrapw):
            lines = []  # (text, attr, action)
            def add(label, text, attr=0):
                if label:
                    lines.append(("## " + label, C["teal2"], None))
                for para in str(text or "").splitlines() or [""]:
                    for ln in textwrap.wrap(para, wrapw) or [""]:
                        lines.append((ln, attr, None))
            ident = q["agent"] + (f" / {q['agent_name']}" if q["agent_name"] else "")
            add(None, f"{ident}  ·  {q['project']}" + (f" ({q['git_branch']})" if q["git_branch"] else "")
                + f"  ·  pane {q['herdr_pane'] or 'none'}  ·  asked {age(q['created_at'])} ago", C["dim"])
            add("working on", q["task"], C["dim"])
            add("question", q["question"], curses.A_BOLD)
            if q["context"]:
                add("context", q["context"])
            if q["options"]:
                tip = "  (click one to answer with it)" if q["status"] == "open" else ""
                lines.append(("## options" + tip, C["teal2"], None))
                for i, o in enumerate(q["options"]):
                    act = ("opt", i) if q["status"] == "open" else None
                    for j, ln in enumerate(textwrap.wrap(o, wrapw - 5) or [""]):
                        lines.append(((f"[{i + 1}]  " if j == 0 else "     ") + ln, C["purple3"], act))
            if q["recommendation"]:
                add("agent recommends", q["recommendation"], C["purple4"])
            if q["status"] != "open":
                add("your answer" if q["status"] != "cancelled" else "closed", q["answer"], C["teal3"])
                if q["delivery"]:
                    lines.append((f"delivery: {q['delivery']}" + (" · acknowledged" if q["acknowledged_at"] else ""),
                                  C["dim"], None))
            return lines

        def draw(bar=None):
            scr.erase()
            st["hits"] = []
            h, W = scr.getmaxyx()
            st["grid"] = [[" "] * W for _ in range(h)]
            opn = [q for q in st["rows"] if q["status"] == "open"]
            prompt = f"{human_name().lower()}@{socket.gethostname().split('.')[0]}"
            stats = f"open {len(opn)}  ·  blocking {sum(q['urgency'] == 'high' for q in opn)}  ·  " \
                    f"awaiting ack {sum(q['status'] == 'answered' for q in st['rows'])}"
            stat_attr = C["purple4"] | curses.A_BOLD if opn else C["dim"]
            if splash.big_header_fits(h, W):
                newer = update.available()
                header.draw(scr, C, put, [(prompt, C["dim"]), (splash.TAGLINE, C["teal2"]), ("", 0),
                                     (stats, stat_attr), (f"http://127.0.0.1:{PORT}", C["faint"]),
                                     (f"decyde {newer} is available: run `decyde update`" if newer else "",
                                      C["purple4"] | curses.A_BOLD)])
                tabs_y = splash.HEADER_ROWS
            else:
                put(0, 1, prompt + ":~$ ", C["dim"])
                put(0, 1 + len(prompt) + 4, "decyde", C["purple3"] | curses.A_BOLD)
                put(0, max(30, W - len(stats) - 2), stats, stat_attr)
                tabs_y = 1
            x = 1
            for i, (_, label, want) in enumerate(VIEWS):
                n = sum(q["status"] in want for q in st["rows"])
                x = button(tabs_y, x, f"{i + 1}:{label} {n}", ("tab", i), C["tabsel"] if i == st["view"] else C["dim"])
            put(tabs_y + 1, 0, "─" * W, C["faint"])
            first = tabs_y + 2

            items = visible()
            st["sel"] = max(0, min(st["sel"], len(items) - 1))
            list_h = max(3, min(len(items), (h - first - 4) * 2 // 5))
            st["scroll"] = max(0, min(st["scroll"], max(0, len(items) - list_h)))
            if st["sel"] < st["scroll"]:
                st["scroll"] = st["sel"]
            if st["sel"] >= st["scroll"] + list_h:
                st["scroll"] = st["sel"] - list_h + 1
            st["list"] = (first, first + list_h)
            if not items:
                put(first + 1, 3, "all clear. no agent is waiting on you." if st["view"] == 0 else "nothing here yet.", C["dim"])
            for row, q in enumerate(items[st["scroll"]:st["scroll"] + list_h]):
                y, idx = first + row, st["scroll"] + row
                selected = idx == st["sel"]
                blocking = q["status"] == "open" and q["urgency"] == "high"
                color = C["purple4"] | curses.A_BOLD if blocking else C["purple2"] if q["status"] == "open" \
                    else C["teal2"] if q["status"] != "cancelled" else C["faint"]
                if selected:
                    put(y, 0, " " * W, C["rowsel"])
                base = C["rowsel"] if selected else 0
                put(y, 0, "▶" if selected else " ", C["teal3"] | base)
                put(y, 2, "!" if blocking else "●", color | base)
                put(y, 4, f"#{q['id']:<4}", C["faint"] | base if not selected else base)
                put(y, 10, f"{q['agent'][:11]:<11}", (C.get(q["agent"], C["other"]) | curses.A_BOLD) if not selected else base | curses.A_BOLD)
                put(y, 23, f"{age(q['created_at']):>4}", C["dim"] | base if not selected else base)
                put(y, 29, q["title"], base | (curses.A_BOLD if selected else 0), W - 30)
                st["hits"].append((y, 0, W, ("sel", idx)))
            top = first + list_h
            put(top, 0, "─" * W, C["faint"])
            above, below = st["scroll"], max(0, len(items) - st["scroll"] - list_h)
            if above:
                put(first - 1, W - 14, f" ▲ {above} more ", C["dim"])
            if below:
                put(top, W - 14, f" ▼ {below} more ", C["dim"])

            q = items[st["sel"]] if items else None
            body_h = max(0, h - top - 3)
            st["detail"] = (top + 1, top + 1 + body_h)
            if q:
                if st["dsel"] != q["id"]:
                    st["dsel"], st["dscroll"] = q["id"], 0
                lines = detail_lines(q, max(20, W - 4))
                st["dscroll"] = max(0, min(st["dscroll"], max(0, len(lines) - body_h)))
                for i, (ln, attr, act) in enumerate(lines[st["dscroll"]:st["dscroll"] + body_h]):
                    put(top + 1 + i, 2, ln, attr)
                    if act:
                        st["hits"].append((top + 1 + i, 2, W, act))
                if st["dscroll"] + body_h < len(lines):
                    put(h - 3, W - 21, " ▼ scroll for more ", C["dim"])

            put(h - 2, 0, " " * W, 0)
            if bar:
                bar(h)
            else:
                x = 1
                if q and q["status"] == "open":
                    x = button(h - 2, x, "answer", ("answer",), C["btnhot"])
                    x = button(h - 2, x, "answer in editor", ("editor",))
                    x = button(h - 2, x, "dismiss", ("dismiss",))
                x = button(h - 2, x, "▲ prev", ("move", -1))
                x = button(h - 2, x, "▼ next", ("move", 1))
                button(h - 2, max(x, W - 9), "quit", ("quit",))
                hint = "click or ↑↓ to browse · wheel scrolls · drag to copy · a answer · e editor · o option · d dismiss"
                put(h - 1, 1, st["flash"] or hint, C["teal3"] if st["flash"] else C["faint"], W - 2)
            for y, x0, x1 in selection_spans():
                try:
                    scr.chgat(y, x0, x1 - x0 + 1, C["selhl"])
                except curses.error:
                    pass
            scr.refresh()

        # ---- drag to copy (mouse reporting takes over the terminal's own selection)

        def selection_spans():
            if not st["selection"]:
                return []
            (ay, ax), (by, bx) = sorted(st["selection"])
            h, W = scr.getmaxyx()
            return [(y, ax if y == ay else 0, bx if y == by else W - 1)
                    for y in range(max(0, ay), min(h, by + 1))]

        def selected_text():
            lines = ["".join(st["grid"][y][x0:x1 + 1]).rstrip() for y, x0, x1 in selection_spans()
                     if y < len(st["grid"])]
            if len(lines) > 1:  # the first line starts where the drag did; drop the pane margin from the rest
                lines = [lines[0].lstrip()] + textwrap.dedent("\n".join(lines[1:])).split("\n")
            return "\n".join(lines).strip()

        def copy_text(text):
            for cmd in (["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"]):
                if shutil.which(cmd[0]):
                    subprocess.run(cmd, input=text.encode(), check=False)
                    return
            # No clipboard tool (e.g. over SSH): ask the terminal itself via OSC 52.
            sys.stdout.write("\x1b]52;c;" + base64.b64encode(text.encode()).decode() + "\x07")
            sys.stdout.flush()

        # ---- prompts

        def prompt_line(label, init=""):
            """One-line editor on the bottom row. Enter or [send] sends, Esc or [cancel] cancels."""
            buf = init
            curses.curs_set(1)
            try:
                while True:
                    def bar(h):
                        W = scr.getmaxyx()[1]
                        x = button(h - 2, 1, "send ⏎", ("send",), C["btnhot"])
                        button(h - 2, x, "cancel esc", ("cancel",))
                        shown = buf[-(W - len(label) - 4):]
                        put(h - 1, 1, label, C["purple3"] | curses.A_BOLD)
                        put(h - 1, 1 + len(label), shown)
                        scr.move(h - 1, min(W - 2, 1 + len(label) + len(shown)))
                    draw(bar)
                    ev = read_event(-1)
                    if ev is None:
                        continue
                    if ev[0] == "click":
                        act = hit_at(ev[1], ev[2])
                        if act == ("send",):
                            return buf.strip() or None
                        if act == ("cancel",):
                            return None
                        continue
                    if ev[0] != "key":
                        continue
                    k = ev[1]
                    if k == "enter":
                        return buf.strip() or None
                    if k == "esc":
                        return None
                    if k == "backspace":
                        buf = buf[:-1]
                    elif k == "\x15":  # Ctrl-U
                        buf = ""
                    elif len(k) == 1 and k.isprintable():
                        buf += k
            finally:
                curses.curs_set(0)

        def confirm(msg):
            while True:
                def bar(h):
                    put(h - 1, 1, msg, C["purple4"])
                    x = button(h - 2, 1, "yes", ("yes",), C["btnhot"])
                    button(h - 2, x, "no", ("no",))
                draw(bar)
                ev = read_event(-1)
                if ev and ev[0] == "click":
                    act = hit_at(ev[1], ev[2])
                    if act in (("yes",), ("no",)):
                        return act == ("yes",)
                elif ev and ev[0] == "key":
                    return ev[1] in ("y", "Y")

        def editor_answer(q, init=""):
            with tempfile.NamedTemporaryFile("w+", suffix=".md", delete=False) as f:
                f.write(f"{init}\n\n# Answer to #{q['id']}: {q['title']}\n"
                        "# Lines starting with '#' are ignored. Save empty to cancel.\n")
                path = f.name
            mouse(False)
            curses.def_prog_mode()
            curses.endwin()
            subprocess.call([*os.environ.get("EDITOR", "nano").split(), path])
            curses.reset_prog_mode()
            mouse(True)
            scr.refresh()
            text = "\n".join(l for l in Path(path).read_text().splitlines() if not l.startswith("#")).strip()
            os.unlink(path)
            return text or None

        def send(q, text):
            if not text:
                st["flash"] = "cancelled"
                return
            if get_question(conn, q["id"])["status"] == "cancelled":
                st["flash"] = f"#{q['id']} was withdrawn by the agent"
                return
            record_answer(conn, q["id"], text, deliver_now=False)
            st["flash"] = f"answered #{q['id']} · decyde is delivering it to the agent"

        # ---- actions

        def act(action):
            q = current()
            kind = action[0]
            if kind == "quit":
                return False
            if kind == "tab":
                st["view"], st["sel"], st["scroll"] = action[1], 0, 0
            elif kind == "sel":
                st["sel"] = action[1]
            elif kind == "move":
                st["sel"] = max(0, st["sel"] + action[1])
            elif q is None or q["status"] != "open":
                pass
            elif kind == "answer":
                send(q, prompt_line(f"#{q['id']} > "))
            elif kind == "editor":
                send(q, editor_answer(q))
            elif kind == "opt":
                send(q, prompt_line(f"#{q['id']} > ", q["options"][action[1]]))
            elif kind == "pickopt" and q["options"]:
                pick = prompt_line(f"option 1-{len(q['options'])} > ")
                if pick and pick.isdigit() and 1 <= int(pick) <= len(q["options"]):
                    act(("opt", int(pick) - 1))
                else:
                    st["flash"] = "no option picked"
            elif kind == "dismiss" and confirm(f"dismiss #{q['id']} without answering?"):
                dismiss_question(conn, q["id"])
                st["flash"] = f"dismissed #{q['id']}"
            return True

        KEYMAP = {"q": ("quit",), "Q": ("quit",), "up": ("move", -1), "k": ("move", -1),
                  "down": ("move", 1), "j": ("move", 1), "1": ("tab", 0), "2": ("tab", 1), "3": ("tab", 2),
                  "a": ("answer",), "enter": ("answer",), "e": ("editor",), "o": ("pickopt",), "d": ("dismiss",)}

        header = splash.Header()
        load()
        last_check = 0.0
        while True:
            if time.time() - last_check >= 1:
                last_check = time.time()
                m = change_marker(conn)
                if m != st["marker"]:
                    st["marker"] = m
                    load()
            draw()
            h, W = scr.getmaxyx()
            ev = read_event(120 if splash.big_header_fits(h, W) else 1000)
            if ev is None:
                continue
            if not (ev[0] == "key" and ev[1] == "resize"):
                st["flash"] = ""
            if ev[0] == "click":
                st["press"], st["selection"] = (ev[2], ev[1]), None
            elif ev[0] == "drag" and st["press"]:
                st["selection"] = (st["press"], (ev[2], ev[1]))
            elif ev[0] == "release":
                press, st["press"] = st["press"], None
                if st["selection"] and st["selection"][0] != st["selection"][1]:
                    text = selected_text()
                    if text:
                        copy_text(text)
                        st["flash"] = f"copied {len(text)} characters"
                elif press:
                    st["selection"] = None
                    action = hit_at(press[1], press[0])
                    if action and not act(action):
                        return
            elif ev[0] == "wheel":
                _, d, _, y = ev
                if st["list"][0] <= y < st["list"][1]:
                    st["sel"] = max(0, st["sel"] + d)
                else:
                    st["dscroll"] = max(0, st["dscroll"] + 2 * d)
            elif ev[1] == "tab":
                act(("tab", (st["view"] + 1) % 3))
            elif ev[1] == "backtab":
                act(("tab", (st["view"] - 1) % 3))
            elif ev[1] in ("pgdn", " "):
                st["dscroll"] += max(1, st["detail"][1] - st["detail"][0] - 2)
            elif ev[1] == "pgup":
                st["dscroll"] = max(0, st["dscroll"] - max(1, st["detail"][1] - st["detail"][0] - 2))
            elif ev[1] in KEYMAP and not act(KEYMAP[ev[1]]):
                return

    os.environ.setdefault("ESCDELAY", "25")
    mouse(True)
    try:
        curses.wrapper(run)
    finally:
        mouse(False)
