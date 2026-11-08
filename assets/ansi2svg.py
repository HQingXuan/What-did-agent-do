#!/usr/bin/env python3
"""Render terminal captures (`tmux capture-pane -e -p`) as SVG terminal windows.
Convert to PNG for the README: `convert -density 144 out.svg out.png`.

Usage: ansi2svg.py out.svg "Title" capture.ansi [--lines A:B] [-- "Title 2" capture2.ansi [--lines A:B]]
Two captures are drawn side by side. Standard library only.
"""
import re
import sys
from html import escape

CW, LH, PAD, BAR = 8.4, 18, 16, 30  # char width and line height at 14px monospace
BASE16 = ["#2e3440", "#e06c75", "#98c379", "#e5c07b", "#61afef", "#c678dd", "#56b6c2", "#c8ccd4",
          "#5c6370", "#ff7b86", "#b5e890", "#ffd580", "#7cc4ff", "#de9cff", "#7fdbe6", "#ffffff"]
FG, BG = "#d7dae0", "#1e2127"


def xterm256(n):
    if n < 16:
        return BASE16[n]
    if n < 232:
        n -= 16
        v = [0, 95, 135, 175, 215, 255]
        return "#%02x%02x%02x" % (v[n // 36], v[n // 6 % 6], v[n % 6])
    g = 8 + (n - 232) * 10
    return "#%02x%02x%02x" % (g, g, g)


def parse(line, st):
    """Split one line into (text, style) runs; st carries style across lines."""
    runs, pos = [], 0
    for m in re.finditer(r"\x1b\[([0-9;]*)m", line):
        if m.start() > pos:
            runs.append((line[pos:m.start()], dict(st)))
        codes = [int(c) for c in m.group(1).split(";") if c] or [0]
        i = 0
        while i < len(codes):
            c = codes[i]
            if c == 0:
                st.clear()
            elif c == 1:
                st["bold"] = True
            elif c == 2:
                st["dim"] = True
            elif c == 7:
                st["inv"] = True
            elif c in (22,):
                st.pop("bold", None); st.pop("dim", None)
            elif 30 <= c <= 37:
                st["fg"] = BASE16[c - 30]
            elif 90 <= c <= 97:
                st["fg"] = BASE16[c - 82]
            elif 40 <= c <= 47:
                st["bg"] = BASE16[c - 40]
            elif c == 39:
                st.pop("fg", None)
            elif c == 49:
                st.pop("bg", None)
            elif c in (38, 48) and i + 2 < len(codes) and codes[i + 1] == 5:
                st["fg" if c == 38 else "bg"] = xterm256(codes[i + 2]); i += 2
            elif c in (38, 48) and i + 4 < len(codes) and codes[i + 1] == 2:
                st["fg" if c == 38 else "bg"] = "#%02x%02x%02x" % tuple(codes[i + 2:i + 5]); i += 4
            i += 1
        pos = m.end()
    if pos < len(line):
        runs.append((line[pos:], dict(st)))
    return runs


def load(path, span):
    lines = open(path, encoding="utf-8").read().rstrip("\n").split("\n")
    if span:
        a, b = (int(x) if x else None for x in span.split(":"))
        lines = lines[a:b]
    while lines and not re.sub(r"\x1b\[[0-9;]*m", "", lines[-1]).strip():
        lines.pop()
    return lines


def panel(x0, title, lines, cols, height):
    w = PAD * 2 + cols * CW
    out = [f'<rect x="{x0}" y="0" width="{w}" height="{height}" rx="10" fill="{BG}"/>',
           f'<rect x="{x0}" y="0" width="{w}" height="{BAR}" rx="10" fill="#2c313a"/>',
           f'<rect x="{x0}" y="{BAR - 10}" width="{w}" height="10" fill="#2c313a"/>']
    for i, col in enumerate(["#ff5f56", "#ffbd2e", "#27c93f"]):
        out.append(f'<circle cx="{x0 + 18 + i * 18}" cy="15" r="5.5" fill="{col}"/>')
    out.append(f'<text x="{x0 + w / 2}" y="20" fill="#abb2bf" font-size="13" text-anchor="middle" '
               f'font-family="-apple-system,Segoe UI,Helvetica,sans-serif">{escape(title)}</text>')
    st = {}
    for row, line in enumerate(lines):
        y = BAR + PAD + row * LH
        col = 0
        for text, s in parse(line, st):
            fg, bg = s.get("fg", FG), s.get("bg")
            if s.get("inv"):
                fg, bg = bg or BG, fg
            x = x0 + PAD + col * CW
            if bg:
                out.append(f'<rect x="{x:.1f}" y="{y - 13}" width="{len(text) * CW:.1f}" height="{LH}" fill="{bg}"/>')
            if text.strip():
                attrs = f'fill="{fg}"' + (' font-weight="bold"' if s.get("bold") else "") + (' opacity="0.6"' if s.get("dim") else "")
                out.append(f'<text x="{x:.1f}" y="{y}" {attrs} xml:space="preserve">{escape(text)}</text>')
            col += len(text)
    return out, w


def main(argv):
    out_path, groups, cur = argv[0], [], []
    for a in argv[1:]:
        if a == "--":
            groups.append(cur); cur = []
        else:
            cur.append(a)
    groups.append(cur)
    specs = []
    for g in groups:
        span = g[g.index("--lines") + 1] if "--lines" in g else None
        lines = load(g[1], span)
        cols = max(len(re.sub(r"\x1b\[[0-9;]*m", "", l)) for l in lines)
        specs.append((g[0], lines, cols))
    height = BAR + PAD * 2 + max(len(s[1]) for s in specs) * LH
    parts, x = [], 0
    for title, lines, cols in specs:
        p, w = panel(x, title, lines, cols, height)
        parts += p; x += w + 20
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{x - 20:.0f}" height="{height}" '
           f'font-family="DejaVu Sans Mono,Menlo,monospace" font-size="14">' + "".join(parts) + "</svg>\n")
    open(out_path, "w", encoding="utf-8").write(svg)


if __name__ == "__main__":
    if len(sys.argv) < 4:
        sys.exit(__doc__)
    main(sys.argv[1:])
