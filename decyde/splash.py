"""The logo: a launch animation, then the same logo as the TUI's permanent header.

The intro shows the logo glinting in a field of twinkling ✓ ✗ ? stars, then glides
it into the top-left corner where `draw_header` keeps it, with a few stars still twinkling.
"""
from __future__ import annotations

import curses
import math
import random
import time

GLYPHS = {
    "d": ["██████╗ ", "██╔══██╗", "██║  ██║", "██║  ██║", "██████╔╝", "╚═════╝ "],
    "e": ["███████╗", "██╔════╝", "█████╗  ", "██╔══╝  ", "███████╗", "╚══════╝"],
    "c": [" ██████╗", "██╔════╝", "██║     ", "██║     ", "╚██████╗", " ╚═════╝"],
    "y": ["██╗   ██╗", "╚██╗ ██╔╝", " ╚████╔╝ ", "  ╚██╔╝  ", "   ██║   ", "   ╚═╝   "],
}
LOGO = ["".join(GLYPHS[ch][row] for ch in "decyde") for row in range(6)]
LOGO_W, LOGO_H = len(LOGO[0]), len(LOGO)
TAGLINE = "your agents ask. you decide."
STAR_GLYPHS = "✓✗?"

HEADER_TOP, HEADER_LEFT = 1, 2
HEADER_ROWS = LOGO_H + 2      # rows the big header occupies (logo plus a blank row above and below)
FPS = 30
INTRO, SETTLE = 1.7, 0.6      # seconds
GLINT_EVERY = 7.0             # the header logo glints again every few seconds


def big_header_fits(h: int, w: int) -> bool:
    return w >= LOGO_W + 6 and h >= 26


def put(scr, y, x, text, attr):
    try:
        scr.addstr(y, x, text, attr)
    except curses.error:
        pass


def draw_logo(scr, top, left, colors, glint=None, reveal=1.0):
    """Purple gradient logo; `glint` is the column of a highlight sweeping across it."""
    purples = [colors["purple1"], colors["purple2"], colors["purple3"], colors["purple4"]]
    for r, line in enumerate(LOGO):
        for c, ch in enumerate(line):
            if ch == " " or c > reveal * LOGO_W:
                continue
            attr = purples[min(3, c * 4 // LOGO_W)]
            d = abs(c - r * 0.6 - glint) if glint is not None else 99
            if d < 1.5:
                attr = colors["white"] | curses.A_BOLD
            elif d < 4:
                attr = colors["teal3"] | curses.A_BOLD
            elif ch == "█":
                attr |= curses.A_BOLD
            put(scr, top + r, left + c, ch, attr)


def twinkle(scr, stars, t, colors, fade=0.0):
    shades = [colors["grey1"], colors["grey2"], colors["teal1"], colors["teal2"], colors["white"]]
    for y, x, g, phase, speed in stars:
        b = (math.sin(t * speed + phase) + 1) / 2 * (1 - fade)
        if b < 0.15:
            continue
        idx = min(len(shades) - 1, int(b * len(shades)))
        put(scr, y, x, g if b > 0.35 else "·", shades[idx] | (curses.A_BOLD if idx == len(shades) - 1 else 0))


def scatter(rng, n, rows, cols, avoid):
    """`n` stars in the given row/col ranges, outside the `avoid` (y0, y1, x0, x1) box."""
    stars = []
    tries = 0
    while len(stars) < n and tries < n * 50:
        tries += 1
        y, x = rng.randrange(*rows), rng.randrange(*cols)
        y0, y1, x0, x1 = avoid
        if y0 <= y <= y1 and x0 <= x <= x1:
            continue
        stars.append((y, x, rng.choice(STAR_GLYPHS), rng.uniform(0, math.tau), rng.uniform(0.8, 2.2)))
    return stars


# ---------------------------------------------------------------- intro

def play(scr, colors: dict) -> None:
    h, w = scr.getmaxyx()
    if w < LOGO_W + 4 or h < LOGO_H + 6:
        return
    rng = random.Random()
    top0, left0 = (h - LOGO_H) // 2 - 1, (w - LOGO_W) // 2
    end_top, end_left = (HEADER_TOP, HEADER_LEFT) if big_header_fits(h, w) else (-LOGO_H, left0)
    stars = [(y, x, g, p, s * 2) for y, x, g, p, s in
             scatter(rng, max(18, w * h // 70), (0, h - 1), (1, w - 2),
                     (top0 - 1, top0 + LOGO_H + 2, left0 - 2, left0 + LOGO_W + 2))]
    start = time.time()
    try:
        while True:
            t = time.time() - start
            if t > INTRO + SETTLE:
                break
            scr.erase()
            settle = max(0.0, (t - INTRO) / SETTLE)
            ease = 1 - (1 - settle) ** 3
            twinkle(scr, stars, t, colors, fade=settle)
            top = round(top0 + (end_top - top0) * ease)
            left = round(left0 + (end_left - left0) * ease)
            glint = (t - 0.55) * 1.6 % 1.6 * (LOGO_W + 20) / 1.6 - 10 if t > 0.55 and settle == 0 else None
            draw_logo(scr, top, left, colors, glint=glint, reveal=min(1.0, t / 0.55))
            if settle == 0 and t > 0.7:
                put(scr, top0 + LOGO_H + 1, (w - len(TAGLINE)) // 2, TAGLINE, colors["teal2"])
            scr.refresh()
            time.sleep(1 / FPS)
    finally:
        curses.flushinp()  # the intro always runs to the end; drop anything typed during it
        scr.erase()


# ---------------------------------------------------------------- header

class Header:
    """The settled logo with a little life in it: stars twinkle, and the logo glints now and then."""

    def __init__(self):
        self.start = time.time()
        self.stars = []
        self.size = None

    def draw(self, scr, colors, info: list[tuple[str, int]]) -> None:
        h, w = scr.getmaxyx()
        t = time.time() - self.start
        info_x = HEADER_LEFT + LOGO_W + 4
        info_w = max((len(s) for s, _ in info), default=0)
        if self.size != (h, w):
            self.size = (h, w)
            self.stars = scatter(random.Random(w * 1000 + h), max(6, (w - info_x - info_w) // 9),
                                 (0, HEADER_ROWS), (info_x + info_w + 3, max(info_x + info_w + 4, w - 2)),
                                 (-1, -1, -1, -1))
        twinkle(scr, self.stars, t, colors)
        phase = t % GLINT_EVERY
        glint = phase * (LOGO_W + 20) / 1.4 - 10 if phase < 1.4 else None
        draw_logo(scr, HEADER_TOP, HEADER_LEFT, colors, glint=glint)
        for i, (text, attr) in enumerate(info):
            put(scr, HEADER_TOP + i, info_x, text[:max(0, w - info_x - 1)], attr)
