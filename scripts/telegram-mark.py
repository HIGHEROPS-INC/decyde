"""Draws the Telegram bot photo: the "d" from the ASCII logo, purple with a narrow teal and white glint.

Usage: python3 scripts/telegram-mark.py assets/decyde-mark.svg, then screenshot it at 640x640 for the PNG.
import sys
D = ["██████╗ ", "██╔══██╗", "██║  ██║", "██║  ██║", "██████╔╝", "╚═════╝ "]
S = 640
CW, CH = 41, 77                  # terminal cells are roughly 1:2 (wider here so the mark reads at small sizes)
W, H = CW * 8, CH * 6
OX, OY = (S - W) / 2 + CW * 0.35, (S - H) / 2   # nudge right: the last column is mostly empty
G, T = 7, 6                      # double-line gap from cell centre, stroke width

def cell(ch, x, y):
    cx, cy, r, b = x + CW / 2, y + CH / 2, x + CW, y + CH
    if ch == "█":
        return [f'<rect fill="#fff" stroke="none" x="{x:.1f}" y="{y:.1f}" width="{CW + .6:.1f}" height="{CH + .6:.1f}"/>']
    lines = {
        "═": [[(x, cy - G), (r, cy - G)], [(x, cy + G), (r, cy + G)]],
        "║": [[(cx - G, y), (cx - G, b)], [(cx + G, y), (cx + G, b)]],
        "╗": [[(x, cy - G), (cx + G, cy - G), (cx + G, b)], [(x, cy + G), (cx - G, cy + G), (cx - G, b)]],
        "╔": [[(r, cy - G), (cx - G, cy - G), (cx - G, b)], [(r, cy + G), (cx + G, cy + G), (cx + G, b)]],
        "╝": [[(x, cy + G), (cx + G, cy + G), (cx + G, y)], [(x, cy - G), (cx - G, cy - G), (cx - G, y)]],
        "╚": [[(r, cy + G), (cx - G, cy + G), (cx - G, y)], [(r, cy - G), (cx + G, cy - G), (cx + G, y)]],
    }.get(ch, [])
    return [f'<polyline points="{" ".join(f"{px:.1f},{py:.1f}" for px, py in l)}"/>' for l in lines]

shapes = []
for r, row in enumerate(D):
    for c, ch in enumerate(row):
        shapes += cell(ch, OX + c * CW, OY + r * CH)

svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="{S}" height="{S}" viewBox="0 0 {S} {S}">
  <defs>
    <radialGradient id="bg" cx="50%" cy="42%" r="70%">
      <stop offset="0" stop-color="#1a1530"/><stop offset="1" stop-color="#0c0b12"/>
    </radialGradient>
    <linearGradient id="purple" x1="{OX}" y1="0" x2="{OX + W}" y2="0" gradientUnits="userSpaceOnUse">
      <stop offset="0" stop-color="#7c5cf0"/><stop offset=".35" stop-color="#9b7bf7"/>
      <stop offset=".65" stop-color="#a78bfa"/><stop offset="1" stop-color="#d8b4fe"/>
    </linearGradient>
    <linearGradient id="glint" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0" stop-color="#5eead4" stop-opacity="0"/>
      <stop offset=".3" stop-color="#5eead4" stop-opacity=".9"/>
      <stop offset=".5" stop-color="#ffffff"/>
      <stop offset=".7" stop-color="#5eead4" stop-opacity=".9"/>
      <stop offset="1" stop-color="#5eead4" stop-opacity="0"/>
    </linearGradient>
    <mask id="d" maskUnits="userSpaceOnUse">
      <g fill="none" stroke="#fff" stroke-width="{T}" stroke-linejoin="miter" stroke-linecap="square">{"".join(shapes)}</g>
    </mask>
    <filter id="glow" x="-20%" y="-20%" width="140%" height="140%"><feGaussianBlur stdDeviation="14"/></filter>
  </defs>
  <rect width="{S}" height="{S}" fill="url(#bg)"/>
  <rect width="{S}" height="{S}" fill="#7c5cf0" opacity=".35" mask="url(#d)" filter="url(#glow)"/>
  <rect width="{S}" height="{S}" fill="url(#purple)" mask="url(#d)"/>
  <g mask="url(#d)">
    <rect x="{OX + W * 0.52}" y="-200" width="44" height="{S + 400}" fill="url(#glint)" transform="rotate(24 {S/2} {S/2})"/>
  </g>
</svg>'''
open(sys.argv[1], "w").write(svg)
