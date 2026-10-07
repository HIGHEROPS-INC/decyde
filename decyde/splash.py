"""Launch animation: the logo glints in a field of twinkling ✓ ✗ ? stars, then settles into the header."""
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

FPS = 30
INTRO, SETTLE = 1.7, 0.55  # seconds


def play(scr, colors: dict) -> None:
    """Draw the intro. `colors` maps purple1..4, teal1..3, grey1..4 and white to curses attrs."""
    h, w = scr.getmaxyx()
    if w < LOGO_W + 4 or h < LOGO_H + 6:
        return  # too small for the big logo; go straight to the app
    rng = random.Random()
    top0, left = (h - LOGO_H) // 2 - 1, (w - LOGO_W) // 2
    stars = []
    while len(stars) < max(18, w * h // 70):
        y, x = rng.randrange(0, h - 1), rng.randrange(1, w - 2)
        if top0 - 1 <= y <= top0 + LOGO_H + 2 and left - 2 <= x <= left + LOGO_W + 2:
            continue
        stars.append((y, x, rng.choice(STAR_GLYPHS), rng.uniform(0, math.tau), rng.uniform(2.0, 4.5)))

    purples = [colors["purple1"], colors["purple2"], colors["purple3"], colors["purple4"]]
    twinkle = [colors["grey1"], colors["grey2"], colors["teal1"], colors["teal2"], colors["white"]]
    scr.nodelay(True)
    start = time.time()
    try:
        while True:
            t = time.time() - start
            if t > INTRO + SETTLE or scr.getch() != -1:
                break
            scr.erase()
            settle = max(0.0, (t - INTRO) / SETTLE)  # 0 → 1 while the logo moves to the top
            ease = 1 - (1 - settle) ** 3

            # stars: twinkle, then fade out as the logo settles
            for y, x, g, phase, speed in stars:
                b = (math.sin(t * speed + phase) + 1) / 2 * (1 - settle)
                if b < 0.15:
                    continue
                idx = min(len(twinkle) - 1, int(b * len(twinkle)))
                char = g if b > 0.35 else "·"
                put(scr, y, x, char, twinkle[idx] | (curses.A_BOLD if idx == len(twinkle) - 1 else 0))

            # logo: reveal left to right, then a glint sweeps across it
            top = round(top0 * (1 - ease))
            reveal = min(1.0, t / 0.55)
            glint = (t - 0.55) * 1.6 % 1.6 * (LOGO_W + 20) / 1.6 - 10
            for r, line in enumerate(LOGO):
                if settle > 0 and r >= LOGO_H * (1 - settle * 0.999):
                    continue  # collapse from the bottom while settling
                for c, ch in enumerate(line):
                    if ch == " " or c > reveal * LOGO_W:
                        continue
                    attr = purples[min(3, c * 4 // LOGO_W)]
                    d = abs(c - r * 0.6 - glint)
                    if t > 0.55 and d < 1.5:
                        attr = colors["white"] | curses.A_BOLD
                    elif t > 0.55 and d < 4:
                        attr = colors["teal3"] | curses.A_BOLD
                    elif ch == "█":
                        attr |= curses.A_BOLD
                    put(scr, top + r, left + c, ch, attr)
            if settle == 0 and t > 0.7:
                put(scr, top0 + LOGO_H + 1, (w - len(TAGLINE)) // 2, TAGLINE, colors["teal2"])
            scr.refresh()
            time.sleep(1 / FPS)
    finally:
        scr.nodelay(False)
        scr.erase()


def put(scr, y, x, text, attr):
    try:
        scr.addstr(y, x, text, attr)
    except curses.error:
        pass
