#!/usr/bin/env python3
"""Draw the banners and charts that the other repositories' READMEs show.

build/repos.json names the repositories. One GraphQL query reads each one's
metadata and, at the head of its default branch, the files its pictures are
drawn from. Every picture is written twice beside the profile's own cards,
dist/repo-<name>-<card>-dark.svg and -light.svg, and the workflow publishes
them all together on the output branch.

A chart is drawn from the table its README already carries, so the picture
and the table it sits beside cannot disagree for longer than a day. A table
that no longer parses, or a count that no longer matches its cross-check,
stops the run before anything is published, and the previous pictures stay
up. The failure names the file that moved.

    GITHUB_TOKEN=... python build/repos.py
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import urllib.request

import build as B

CFG = json.loads((B.ROOT / "build" / "repos.json").read_text(encoding="utf-8"))

W, H = 860, 250                      # banner
PX, PY, PW, PH = 470, 22, 360, 206   # the motif panel on its right
TEXT_W = 404                         # the text column on its left


class Drift(Exception):
    """A source file no longer has the shape its picture is drawn from."""


def need(cond, msg: str):
    if not cond:
        raise Drift(msg)


# --------------------------------------------------------------------------
# Data

META = ("name description url homepageUrl stargazerCount forkCount licenseInfo { spdxId } "
        "primaryLanguage { name } latestRelease { tagName publishedAt } "
        "defaultBranchRef { target { ... on Commit { history { totalCount } } } }")


def query() -> str:
    parts = []
    for i, r in enumerate(CFG["repos"]):
        files = " ".join(
            f'f{j}: object(expression: {json.dumps("HEAD:" + p)}) {{ ... on Blob {{ text isTruncated }} }}'
            for j, p in enumerate(r["files"]))
        parts.append(f'r{i}: repository(owner: {json.dumps(CFG["owner"])}, name: {json.dumps(r["name"])}) '
                     f'{{ {META} {files} }}')
    return "query {\n" + "\n".join(parts) + "\n}"


def fetch(token: str) -> dict:
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=json.dumps({"query": query()}).encode(),
        headers={"Authorization": f"bearer {token}", "Content-Type": "application/json",
                 "User-Agent": "profile-readme-build"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        payload = json.load(r)
    if payload.get("errors"):
        raise SystemExit("GraphQL errors: " + "; ".join(e.get("message", "?") for e in payload["errors"]))
    out = {}
    for i, spec in enumerate(CFG["repos"]):
        node = payload["data"].get(f"r{i}")
        need(node, f'{spec["name"]}: repository not found')
        files = {}
        for j, path in enumerate(spec["files"]):
            blob = node.get(f"f{j}")
            need(blob and blob.get("text") is not None, f'{spec["name"]}: {path} is missing or binary')
            need(not blob["isTruncated"], f'{spec["name"]}: {path} came back truncated')
            files[path] = blob["text"]
        out[spec["name"]] = (node, files)
    return out


def split_row(line: str) -> list[str] | None:
    s = line.strip()
    if not s.startswith("|"):
        return None
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", s.strip("|"))]


def md_table(text: str, header: list[str], where: str, after: str | None = None) -> list[list[str]]:
    """Body rows of the first table whose header starts with `header`, below heading `after`."""
    lines = text.splitlines()
    start = 0
    if after:
        start = next((i for i, l in enumerate(lines) if re.match(after, l)), -1)
        need(start >= 0, f"{where}: no heading matching {after!r}")
    for i in range(start, len(lines)):
        cells = split_row(lines[i])
        if cells and cells[:len(header)] == header:
            rows = []
            for l in lines[i + 2:]:
                row = split_row(l)
                if row is None:
                    break
                need(len(row) >= len(header), f"{where}: short row {l!r}")
                rows.append(row)
            need(rows, f"{where}: table {header} is empty")
            return rows
    raise Drift(f"{where}: no table headed {' | '.join(header)}")


def plain(s: str) -> str:
    """A table cell as it reads: backticks, bold and links dropped."""
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)
    return s.replace("`", "").replace("**", "")


NUM = r"(\d[\d,]*(?:\.\d+)?)"


def number(s: str, where: str) -> float:
    m = re.search(NUM, s)
    need(m, f"{where}: no number in {s!r}")
    return float(m.group(1).replace(",", ""))


# --------------------------------------------------------------------------
# Shared pieces

TRACES = ["M0,242 H150 L160,234 H300 L308,242 H446", "M318,0 V6 L330,18 H452"]


def frame(cv: B.Canvas, w: int, h: int, rx: int = 16):
    c = cv.c
    cv.defs.append(
        f'<linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="{c["bg"]}"/>'
        f'<stop offset="1" stop-color="{c["bg3"]}"/></linearGradient>'
        f'<clipPath id="k"><rect x="0.5" y="0.5" width="{w - 1}" height="{h - 1}" rx="{rx}"/></clipPath>'
    )
    cv.add('<g clip-path="url(#k)">')
    cv.add(f'<rect width="{w}" height="{h}" fill="url(#g)"/>')


def close(cv: B.Canvas, w: int, h: int, rx: int = 16):
    cv.add("</g>")
    cv.add(f'<rect x="0.5" y="0.5" width="{w - 1}" height="{h - 1}" rx="{rx}" fill="none" stroke="{cv.col("border")}"/>')


def traces(cv: B.Canvas):
    c = cv.c
    for i, d in enumerate(TRACES):
        cv.add(f'<path d="{d}" fill="none" stroke="{c["border"]}" stroke-width="1.5"/>')
        cv.add(f'<path class="p p{i}" d="{d}" pathLength="100" fill="none" stroke="{c["cyan"]}" '
               f'stroke-width="2" stroke-linecap="round" stroke-dasharray="7 93"/>')
    cv.css.append("@keyframes run{from{stroke-dashoffset:100}to{stroke-dashoffset:0}}"
                  ".p{stroke-dashoffset:100;animation:run 5s linear infinite}.p1{animation-duration:6.3s;animation-delay:-2s}")


def panel(cv: B.Canvas, left: str, right: str):
    """The rounded window the motif sits in, with a title row."""
    c = cv.c
    cv.defs.append(f'<clipPath id="pc"><rect x="{PX}" y="{PY}" width="{PW}" height="{PH}" rx="12"/></clipPath>')
    cv.add(f'<rect x="{PX}" y="{PY}" width="{PW}" height="{PH}" rx="12" fill="{c["bg"]}" fill-opacity="0.72" '
           f'stroke="{c["border"]}"/>')
    for k, dot in enumerate(("line", "line", "line")):
        cv.add(f'<circle cx="{PX + 16 + k * 12}" cy="{PY + 17}" r="3.5" fill="{c[dot]}"/>')
    cv.put(PX + 54, PY + 21, left, 11, "PM", 600, "text2")
    cv.put(PX + PW - 16, PY + 21, right, 11, "PM", 500, "text3", "end", spacing=0.5)
    cv.add(f'<line x1="{PX}" y1="{PY + 32}" x2="{PX + PW}" y2="{PY + 32}" stroke="{c["border"]}"/>')


def stepper(cv: B.Canvas, cls: str, offsets: list[tuple[float, float]], secs: float):
    """CSS that jumps an element through `offsets`, one stop per beat."""
    n = len(offsets)
    frames = "".join(f"{k * 100 / n:.2f}%{{transform:translate({dx:.1f}px,{dy:.1f}px)}}" for k, (dx, dy) in enumerate(offsets))
    cv.css.append(f"@keyframes {cls}{{{frames}100%{{transform:translate(0px,0px)}}}}"
                  f".{cls}{{animation:{cls} {secs:.1f}s steps(1,end) infinite}}")


def tokens_line(cv: B.Canvas, x: float, y: float, toks: list[tuple[str, str]], size: float = 12, wt: int = 400) -> float:
    # SVG collapses leading white space, so each run starts after its own.
    for s, col in toks:
        body = s.lstrip()
        if body:
            cv.put(x + B.width(s[:len(s) - len(body)], size, "PM", wt), y, body.rstrip(), size, "PM", wt, col)
        x += B.width(s, size, "PM", wt)
    return x


STAR = ("M6 .6l1.7 3.5 3.8.5-2.8 2.7.7 3.8L6 9.3 2.6 11.1l.7-3.8L.5 4.6l3.8-.5z")


def chips(cv: B.Canvas, x: float, y: float, items: list[tuple[str | None, str]], maxx: float) -> list[str]:
    """Pills of key and value; returns the ones that fit, for the alt text."""
    c = cv.c
    shown = []
    for key, val in items:
        if key == "star":
            w = 10 + 12 + 5 + B.width(val, 10.5, "PM", 600) + 10
        else:
            kw = B.width(key, 10.5, "PM", 400) + 6 if key else 0
            w = 10 + kw + B.width(val, 10.5, "PM", 600) + 10
        if x + w > maxx:
            break
        cv.add(f'<rect x="{x:.1f}" y="{y}" width="{w:.1f}" height="24" rx="12" fill="{c["bg2"]}" '
               f'fill-opacity="0.6" stroke="{c["border"]}"/>')
        if key == "star":
            cv.add(f'<path d="{STAR}" transform="translate({x + 10:.1f},{y + 6})" fill="{c["orange"]}"/>')
            cv.put(x + 27, y + 16, val, 10.5, "PM", 600, "text")
            shown.append(f"{val} star" + ("" if val == "1" else "s"))
        else:
            tx = x + 10
            if key:
                cv.put(tx, y + 16, key, 10.5, "PM", 400, "text3")
                tx += B.width(key, 10.5, "PM", 400) + 6
            cv.put(tx, y + 16, val, 10.5, "PM", 600, "text")
            shown.append(f"{key} {val}" if key else val)
        x += w + 7
    return shown


# --------------------------------------------------------------------------
# Motifs: each draws inside the panel and returns a sentence for the alt text.

SOURCE = "\\relative c' { c4 d e f | g1 }"
SOURCE_TOKS = [("\\relative", "violet"), (" ", "text"), ("c'", "cyan"), (" ", "text"), ("{", "text3"),
               (" c4 d e f ", "text"), ("|", "text3"), (" g1 ", "text"), ("}", "text3")]


def motif_staff(cv: B.Canvas, spec, files) -> str:
    """The source line and the bar it engraves to, as LilyPond sets it: treble, 4/4."""
    c = cv.c
    panel(cv, "lesson.ly", "LilyPond")
    tokens_line(cv, PX + 18, PY + 58, SOURCE_TOKS)
    xs, xe = PX + 22, PX + PW - 22
    top = PY + 104
    bottom = top + 40          # E4 on the bottom line; each diatonic step is 5 px
    for i in range(5):
        cv.add(f'<line x1="{xs}" y1="{top + i * 10}" x2="{xe}" y2="{top + i * 10}" stroke="{c["text3"]}" stroke-width="1"/>')
    cv.add(f'<line x1="{xs}" y1="{top}" x2="{xs}" y2="{bottom}" stroke="{c["text3"]}" stroke-width="1"/>')
    for k, ty in enumerate((top + 18.5, top + 38.5)):
        cv.put(xs + 20, ty, "4", 21, "PS", 600, "text", "middle")
    steps = {"c": -2, "d": -1, "e": 0, "f": 1, "g": 2}
    notes = [("c", 4, xs + 72), ("d", 4, xs + 114), ("e", 4, xs + 156), ("f", 4, xs + 198), ("g", 1, xs + 262)]
    for name, dur, x in notes:
        y = bottom - steps[name] * 5
        if name == "c":
            cv.add(f'<line x1="{x - 11}" y1="{y}" x2="{x + 11}" y2="{y}" stroke="{c["text3"]}" stroke-width="1.2"/>')
        if dur == 4:
            cv.add(f'<ellipse cx="{x}" cy="{y}" rx="6.1" ry="4.3" transform="rotate(-20 {x} {y})" fill="{c["text"]}"/>')
            cv.add(f'<line x1="{x + 5.6}" y1="{y - 1.2}" x2="{x + 5.6}" y2="{y - 35}" stroke="{c["text"]}" stroke-width="1.3"/>')
        else:
            cv.add(f'<ellipse cx="{x}" cy="{y}" rx="7.2" ry="4.6" fill="none" stroke="{c["text"]}" stroke-width="2.2"/>')
    bar = xs + 226
    cv.add(f'<line x1="{bar}" y1="{top}" x2="{bar}" y2="{bottom}" stroke="{c["text3"]}" stroke-width="1.2"/>')
    cv.add(f'<line x1="{xe - 6}" y1="{top}" x2="{xe - 6}" y2="{bottom}" stroke="{c["text3"]}" stroke-width="1.2"/>')
    cv.add(f'<rect x="{xe - 3}" y="{top}" width="3" height="40" fill="{c["text3"]}"/>')
    # Playback: a playhead stepping from note to note.
    x0 = notes[0][2]
    cv.add(f'<g class="ph"><line x1="{x0}" y1="{top - 14}" x2="{x0}" y2="{bottom + 18}" stroke="{c["cyan"]}" '
           f'stroke-width="2" stroke-opacity="0.85"/><circle cx="{x0}" cy="{top - 14}" r="3" fill="{c["cyan"]}"/></g>')
    stepper(cv, "ph", [(x - x0, 0) for *_, x in notes], 4.5)
    cv.put(PX + 18, PY + PH - 14, "c d e f in quarter notes, then g held for the whole bar", 10, "PS", 400, "text3")
    return f"The panel shows the LilyPond line {SOURCE} above the bar it engraves, with a playhead stepping across the notes."


# From `tree-sitter parse` (CLI 0.27.2) on SOURCE with the grammar at commit
# 53293e9. The full tree also holds pitch, octave and duration nodes under each
# note; they are folded into the leaf text here.
TREE = [("command", "\\relative"), ("note", "c'"),
        ("sequential_music", [("note", "c4"), ("note", "d"), ("note", "e"), ("note", "f"),
                              ("bar_check", "|"), ("note", "g1")])]


def motif_tree(cv: B.Canvas, spec, files) -> str:
    c = cv.c
    panel(cv, "lesson.ly", "parse tree")
    sx = PX + 18
    tokens_line(cv, sx, PY + 56, SOURCE_TOKS)

    def node(x, y, label, col="text2", ring="border"):
        w = B.width(label, 8.5, "PM", 500) + 8
        cv.add(f'<rect x="{x - w / 2:.1f}" y="{y - 10.5}" width="{w:.1f}" height="15" rx="4.5" fill="{c["bg2"]}" '
               f'fill-opacity="0.8" stroke="{c[ring]}"/>')
        cv.put(x, y + 0.5, label, 8.5, "PM", 500, col, "middle")

    def edge(x1, y1, x2, y2):
        cv.add(f'<path d="M{x1:.1f},{y1:.1f} V{(y1 + y2) / 2:.1f} H{x2:.1f} V{y2:.1f}" fill="none" '
               f'stroke="{c["border"]}" stroke-width="1.2"/>')

    ry, y1, y2 = PY + 84, PY + 124, PY + 168
    root_x = PX + PW / 2
    # The six children of sequential_music take the right of the panel, so
    # their edges never cross the leaves of command and note on the left.
    l1 = [PX + 44, PX + 98, PX + 232]
    kids = TREE[2][1]
    l2 = [PX + 128 + k * (PW - 128 - 20) / (len(kids) - 1) for k in range(len(kids))]
    gaps = [(l2[k + 1] - l2[k]) - (B.width(kids[k][0], 8.5, "PM", 500) + B.width(kids[k + 1][0], 8.5, "PM", 500)) / 2 - 8
            for k in range(len(kids) - 1)]
    need(min(gaps) > 1, "tree motif: node labels overlap")
    for x in l1:
        edge(root_x, ry + 5, x, y1 - 11)
    for x in l2:
        edge(l1[2], y1 + 5, x, y2 - 11)
    node(root_x, ry, "source_file", "text", "violet")
    for x, (kind, leaf) in zip(l1, TREE):
        node(x, y1, kind)
        if isinstance(leaf, str):
            cv.put(x, y1 + 22, leaf, 10.5, "PM", 500, "cyan", "middle")
    for x, (kind, leaf) in zip(l2, kids):
        node(x, y2, kind)
        cv.put(x, y2 + 22, leaf, 10.5, "PM", 500, "cyan", "middle")

    # A marker steps through the six children of sequential_music and the
    # source token each one spans, in step.
    cw = B.width("c", 12, "PM", 400)
    pos, idx = [], 0
    for _, leaf in kids:
        idx = SOURCE.index(leaf, idx)
        pos.append(sx + idx * cw + len(leaf) * cw / 2)
        idx += len(leaf)
    cv.add(f'<rect class="mk" x="{pos[0] - 9:.1f}" y="{PY + 62}" width="18" height="2.5" rx="1.2" fill="{c["cyan"]}"/>')
    stepper(cv, "mk", [(p - pos[0], 0) for p in pos], 5.4)
    cv.add(f'<rect class="nk" x="{l2[0] - 4:.1f}" y="{y2 + 28}" width="8" height="2.5" rx="1.2" fill="{c["cyan"]}"/>')
    stepper(cv, "nk", [(x - l2[0], 0) for x in l2], 5.4)
    return (f"The panel shows {SOURCE} and its parse tree: source_file holds a command, a note and "
            "sequential_music, whose six children are five notes and a bar check.")


LUA_KW = {"local", "if", "then", "end", "not", "function", "return", "else", "elseif", "for", "in", "do", "and", "or", "nil", "true", "false"}
LUA_TOK = re.compile(r'(--.*$)|("(?:[^"\\]|\\.)*")|(\b\d+\b)|([A-Za-z_]\w*)|(\s+)|(.)')


def lua_tokens(line: str) -> list[tuple[str, str]]:
    out = []
    for m in LUA_TOK.finditer(line):
        com, st, num, ident, ws, other = m.groups()
        if com:
            out.append((com, "text3"))
        elif st:
            out.append((st, "green"))
        elif num:
            out.append((num, "orange"))
        elif ident:
            nxt = line[m.end():m.end() + 1]
            out.append((ident, "violet" if ident in LUA_KW else "cyan" if nxt == "(" else "text"))
        elif ws:
            out.append((ws, "text"))
        else:
            out.append((other, "text3"))
    return out


def motif_editor(cv: B.Canvas, spec, files) -> str:
    c = cv.c
    lines = files["init.lua"].splitlines()
    start = next((i for i, l in enumerate(lines) if l.strip() == "vim.loader.enable()"), -1)
    need(start >= 0, "ACH-NEOVIM: init.lua no longer calls vim.loader.enable()")
    shown = lines[start:start + 8]
    plugins = len(json.loads(files["lazy-lock.json"]))
    mason = len(json.loads(files["mason-lock.json"]))
    need(plugins > 0 and mason > 0, "ACH-NEOVIM: an empty lock file")
    panel(cv, "init.lua", "Lua")
    cv.add('<g clip-path="url(#pc)">')
    lh, y0 = 16, PY + 54
    for k, line in enumerate(shown):
        y = y0 + k * lh
        cv.put(PX + 34, y, str(start + 1 + k), 10.5, "PM", 400, "text3", "end")
        tokens_line(cv, PX + 44, y, lua_tokens(line), 10.5)
    # Cursor on the first line, after its closing parenthesis.
    cx = PX + 44 + B.width(shown[0], 10.5, "PM", 400)
    cv.add(f'<rect class="cur" x="{cx + 1:.1f}" y="{y0 - 10}" width="6.3" height="13" fill="{c["cyan"]}" fill-opacity="0.8"/>')
    cv.css.append("@keyframes blink{0%,55%{opacity:1}56%,100%{opacity:0}}.cur{animation:blink 1.1s steps(1,end) infinite}")
    sy = PY + PH - 26
    cv.add(f'<rect x="{PX}" y="{sy}" width="{PW}" height="26" fill="{c["bg2"]}"/>')
    cv.add(f'<rect x="{PX + 10}" y="{sy + 5}" width="58" height="16" rx="4" fill="{c["mark"]}"/>')
    cv.put(PX + 39, sy + 16.5, "NORMAL", 9.5, "PM", 600, "bg", "middle", spacing=0.5)
    cv.put(PX + 78, sy + 17, f"{plugins} plugins · {mason} Mason packages", 10.5, "PM", 500, "text2")
    cv.add("</g>")
    return (f"The panel shows lines {start + 1} to {start + len(shown)} of init.lua, and a status line counting "
            f"{plugins} plugins and {mason} Mason packages, from lazy-lock.json and mason-lock.json.")


TRIPLES = {"aarch64-apple-darwin": "macOS arm64", "x86_64-apple-darwin": "macOS x86_64",
           "aarch64-unknown-linux-gnu": "Linux arm64", "x86_64-unknown-linux-gnu": "Linux x86_64"}


def motif_brew(cv: B.Canvas, spec, files) -> str:
    c = cv.c
    rb = files["Formula/swara.rb"]
    urls = re.findall(r'url "([^"]+)"', rb)
    need(urls, "homebrew-tap: Formula/swara.rb has no url")
    vers = {m.group(1) for u in urls if (m := re.search(r"/download/v([\d.]+)/", u))}
    need(len(vers) == 1, f"homebrew-tap: urls name versions {sorted(vers)}")
    version = vers.pop()
    plats = []
    for u in urls:
        t = next((v for k, v in TRIPLES.items() if k in u), None)
        need(t, f"homebrew-tap: unknown target in {u}")
        plats.append(t)
    desc = re.search(r'desc "([^"]+)"', rb)
    need(desc, "homebrew-tap: Formula/swara.rb has no desc")
    panel(cv, "Terminal", "Homebrew")
    cv.add('<g clip-path="url(#pc)">')
    y = PY + 54
    cv.put(PX + 18, y, "$", 11, "PM", 600, "green")
    cv.put(PX + 32, y, "brew install anirbanchakraborty-dev/tap/swara", 11, "PM", 400, "text")
    y += 26
    cv.put(PX + 18, y, "swara", 13, "PS", 600, "text")
    cv.put(PX + 18 + B.width("swara ", 13, "PS", 600), y, version, 13, "PM", 500, "cyan")
    for k, line in enumerate(B.wrap(desc.group(1), PW - 36, 11.5, maxlines=2)):
        cv.put(PX + 18, y + 20 + k * 16, line, 11.5, "PS", 400, "text2")
    gy = y + 24 + 16 * min(2, len(B.wrap(desc.group(1), PW - 36, 11.5))) + 4
    cw = (PW - 36 - 8) / 2
    for k, p in enumerate(plats):
        gx, gyy = PX + 18 + (k % 2) * (cw + 8), gy + (k // 2) * 26
        cv.add(f'<rect x="{gx:.1f}" y="{gyy}" width="{cw:.1f}" height="22" rx="6" fill="{c["bg2"]}" fill-opacity="0.7" '
               f'stroke="{c["border"]}"/>')
        cv.add(f'<circle cx="{gx + 12:.1f}" cy="{gyy + 11}" r="3" fill="{c["green"]}"/>')
        cv.put(gx + 22, gyy + 15, p, 10.5, "PM", 500, "text")
    checks = [re.sub(r"#\{bin\}/", "", t) for t in re.findall(r'shell_output\("([^"]+)"\)', rb)]
    need(checks, "homebrew-tap: Formula/swara.rb has no test")
    ty = gy + 2 * 26 + 14
    need(ty + 8 < PY + PH, "homebrew-tap: the test line falls below the panel")
    cv.put(PX + 18, ty, "$", 11, "PM", 600, "green")
    cv.put(PX + 32, ty, "brew test swara", 11, "PM", 400, "text")
    cv.put(PX + 32 + B.width("brew test swara  ", 11, "PM", 400), ty, "# " + ", ".join(checks), 11, "PM", 400, "text3")
    cv.add("</g>")
    return (f"The panel shows brew install anirbanchakraborty-dev/tap/swara, the formula's version {version}, "
            f"its {len(plats)} prebuilt targets, {', '.join(plats)}, and the commands brew test runs: {', '.join(checks)}.")


def motif_parity(cv: B.Canvas, spec, files) -> str:
    c = cv.c
    sv = files["hdl/parity/parity_gen.sv"]
    m = re.search(r"parameter\s+int\s+K\s*=\s*(\d+)", sv)
    need(m, "ecc-book-lab: parity_gen.sv no longer sets K")
    k = int(m.group(1))
    data = spec["data_bits"]
    need(len(data) == k, f"ecc-book-lab: repos.json has {len(data)} data bits, parity_gen.sv has K = {k}")
    parity = str(data.count("1") % 2)
    sent = parity + data                       # {parity, data}: parity in bit K, the top
    flip = spec["flip"]                        # bit index, 0 = least significant
    pos = len(sent) - 1 - flip
    got = sent[:pos] + ("0" if sent[pos] == "1" else "1") + sent[pos + 1:]
    syndrome = got.count("1") % 2
    need(syndrome == 1, "ecc-book-lab: one flipped bit must leave odd weight")
    panel(cv, "parity_gen.sv", f"K = {k}")
    cell, gap = 27, 4
    x0 = PX + PW - 18 - (len(sent) * cell + (len(sent) - 1) * gap)
    cv.put(x0 + cell / 2, PY + 52, "parity", 9.5, "PM", 500, "cyan", "middle")
    cv.put(x0 + cell + gap + (k * (cell + gap) - gap) / 2, PY + 52, f"{k} data bits", 9.5, "PM", 500, "text3", "middle")

    def row(y, bits, label, bad=None):
        cv.put(PX + 18, y + 18, label, 11, "PM", 500, "text2")
        for i, b in enumerate(bits):
            x = x0 + i * (cell + gap)
            stroke = c["red"] if i == bad else c["cyan"] if i == 0 else c["border"]
            fill = c["red"] if i == bad else c["bg2"]
            cls = ' class="flip"' if i == bad else ""
            cv.add(f'<rect{cls} x="{x}" y="{y}" width="{cell}" height="{cell}" rx="5" fill="{fill}" '
                   f'fill-opacity="{0.22 if i == bad else 0.7}" stroke="{stroke}" stroke-width="{1.6 if i in (0, bad) else 1}"/>')
            cv.put(x + cell / 2, y + 18.5, b, 13, "PM", 600, "text", "middle")

    row(PY + 60, sent, "sent")
    ax = x0 + pos * (cell + gap) + cell / 2
    cv.add(f'<path d="M{ax},{PY + 93} V{PY + 110}" stroke="{c["red"]}" stroke-width="1.5" stroke-dasharray="3 3"/>'
           f'<path d="M{ax - 4},{PY + 106} L{ax},{PY + 111} L{ax + 4},{PY + 106}" fill="none" stroke="{c["red"]}" stroke-width="1.5"/>')
    cv.put(ax - 10, PY + 105, "one bit flips", 9.5, "PM", 400, "text3", "end")
    row(PY + 116, got, "received", bad=pos)
    cv.css.append("@keyframes fl{0%,100%{fill-opacity:.22}50%{fill-opacity:.5}}.flip{animation:fl 1.6s ease-in-out infinite}")
    y = PY + 172
    cv.put(PX + 18, y, f"XOR of all {len(got)} bits = {syndrome}", 11.5, "PM", 500, "text")
    bx = PX + PW - 18 - B.width("error detected", 11, "PS", 600) - 22
    cv.add(f'<circle cx="{bx + 7}" cy="{y - 4}" r="7" fill="{c["red"]}"/>'
           f'<path d="M{bx + 7},{y - 8} V{y - 3.5}" stroke="{c["bg"]}" stroke-width="1.8" stroke-linecap="round"/>'
           f'<circle cx="{bx + 7}" cy="{y - 0.6}" r="1.1" fill="{c["bg"]}"/>')
    cv.put(bx + 20, y, "error detected", 11, "PS", 600, "text")
    return (f"The panel shows the single parity check code from parity_gen.sv with K = {k}: data {data} sent with "
            f"parity bit {parity} as {sent}, one bit flipped to give {got}, and the XOR of all bits equal to 1, "
            "so the error is detected.")


def short_section(s: str) -> str:
    s = re.sub(r"^(Post-Install:|Installing|Updating)\s+", "", s)
    return re.sub(r"\s*\([^)]*\)", "", s).strip()


def motif_setup(cv: B.Canvas, spec, files) -> str:
    c = cv.c
    # A section can be named in both arms of an if, as when a step is skipped,
    # so each name counts once, where it first appears.
    names = list(dict.fromkeys(re.findall(r'^\s*section "([^"]+)"', files["mac-setup.sh"], re.M)))
    need(len(names) >= 4, "Post-Mac-Setup: mac-setup.sh has too few section calls")
    short = [short_section(n) for n in names]
    panel(cv, "mac-setup.sh", f"{len(names)} sections")
    rows = (len(short) + 1) // 2
    lh = min(14.0, (PH - 50) / rows)
    colw = (PW - 36 - 10) / 2
    size = 9.5
    while size > 8 and max(B.width(s, size, "PM", 400) for s in short) > colw - 16:
        size -= 0.5
    slots = []
    for i, s in enumerate(short):
        col, r = divmod(i, rows)
        x, y = PX + 18 + col * (colw + 10), PY + 50 + r * lh
        slots.append((x, y))
        cv.add(f'<path d="M{x:.1f},{y - 3.5:.1f} l2.6,2.8 l5.2,-6" fill="none" stroke="{c["green"]}" stroke-width="1.6" '
               f'stroke-linecap="round" stroke-linejoin="round"/>')
        text = s
        while B.width(text, size, "PM", 400) > colw - 16:
            text = text[:-2] + "…"
        cv.put(x + 14, y, text, size, "PM", 400, "text2")
    x0, y0 = slots[0]
    cv.add(f'<rect class="sk" x="{x0 - 5:.1f}" y="{y0 - lh + 3.5:.1f}" width="{colw:.1f}" height="{lh:.1f}" rx="4" '
           f'fill="{c["cyan"]}" fill-opacity="0.14"/>')
    stepper(cv, "sk", [(x - x0, y - y0) for x, y in slots], len(slots) * 0.6)
    return f"The panel lists the {len(names)} sections mac-setup.sh runs, from {names[0]} to {names[-1]}."


MOTIFS = {"staff": motif_staff, "tree": motif_tree, "editor": motif_editor, "brew": motif_brew,
          "parity": motif_parity, "setup": motif_setup}


def banner(mode: str, spec: dict, node: dict, files: dict) -> B.Canvas:
    desc = (node.get("description") or "").strip()
    need(desc, f'{spec["name"]}: the repository has no description')
    cv = B.Canvas(W, H, mode, spec["display"], "")
    frame(cv, W, H)
    traces(cv)
    cv.put(44, 58, spec["eyebrow"], 12, "PM", 500, "cyan", spacing=2)
    size = 34
    while B.width(spec["display"], size, "PS", 600) > TEXT_W:
        size -= 1
    cv.put(42, 100, spec["display"], size, "PS", 600, "text", spacing=-0.4)
    for dsize in (16, 15, 14, 13):
        lines = B.wrap(desc, TEXT_W, dsize)
        if len(lines) <= 3:
            break
    lines = B.wrap(desc, TEXT_W, dsize, maxlines=3)
    for k, line in enumerate(lines):
        cv.put(44, 132 + k * dsize * 1.45, line, dsize, "PS", 400, "text2")

    items: list[tuple[str | None, str]] = []
    rel = node.get("latestRelease")
    if rel:
        items.append(("release", rel["tagName"]))
    if node.get("licenseInfo") and node["licenseInfo"].get("spdxId") not in (None, "NOASSERTION"):
        items.append((None, node["licenseInfo"]["spdxId"]))
    if node.get("primaryLanguage"):
        items.append((None, node["primaryLanguage"]["name"]))
    commits = node["defaultBranchRef"]["target"]["history"]["totalCount"]
    items.append((None, f"{B.fmt_int(commits)} commit" + ("" if commits == 1 else "s")))
    if node["stargazerCount"] > 0:
        items.append(("star", B.fmt_int(node["stargazerCount"])))
    shown = chips(cv, 44, 198, items, TEXT_W + 44)

    said = MOTIFS[spec["motif"]](cv, spec, files)
    close(cv, W, H)
    cv.desc = f'{spec["display"]}: {desc} {", ".join(shown)}. {said}'
    return cv


# --------------------------------------------------------------------------
# Charts

def chart_head(cv: B.Canvas, title: str, sub: str, w: int):
    cv.put(28, 40, title, 17, "PS", 600, "text")
    for k, line in enumerate(B.wrap(sub, w - 56, 12.5)):
        cv.put(28, 62 + k * 18, line, 12.5, "PS", 400, "text2")


def chart_speed(mode: str, spec, node, files) -> B.Canvas:
    rows = md_table(files["README.md"], ["Measurement", "Median", "Target"], "swara README.md")
    data = []
    for m, med, tgt in (r[:3] for r in rows):
        a, b = number(med, "swara Speed"), number(tgt, "swara Speed")
        need(b > 0, f"swara Speed: zero target in {tgt!r}")
        data.append((plain(m), plain(med), plain(tgt), a / b))
    lw = max(B.width(d[0], 12.5) for d in data)
    x0 = 28 + lw + 18
    vx = 860 - 28 - max(B.width(f"{d[1]}  /  {d[2]}", 11, "PM", 500) for d in data)
    bw = vx - 16 - x0
    need(bw > 160, "swara Speed: labels leave no room for the bars")
    top = max(1.0, max(d[3] for d in data))
    rh, y0 = 27, 104
    h = y0 + len(data) * rh + 44
    cv = B.Canvas(860, h, mode, "Swara speed against each target",
                  "Median of each measurement as a share of its target: " +
                  "; ".join(f"{d[0]}, {d[1]} against {d[2]}" for d in data) + ".")
    c = cv.c
    B.card_frame(cv)
    chart_head(cv, "Speed against each target",
               "Each bar is a measurement's median as a share of its target, so a shorter bar has more room to spare. "
               "The figures are the Speed table in this README.", 860)
    # Grid at quarters of the target, the target itself drawn firmer.
    for q in (0, 0.25, 0.5, 0.75, 1.0):
        gx = x0 + bw * q / top
        firm = q == 1.0
        dash = ' stroke-dasharray="4 3"' if firm else ""
        cv.add(f'<line x1="{gx:.1f}" y1="{y0 - 6}" x2="{gx:.1f}" y2="{y0 + len(data) * rh - 6}" '
               f'stroke="{c["text3"] if firm else c["line"]}" stroke-width="1"{dash}/>')
        cv.put(gx, y0 + len(data) * rh + 12, "target" if firm else f"{q * 100:.0f}%", 10.5, "PM", 400,
               "text2" if firm else "text3", "middle")
    for i, (label, med, tgt, share) in enumerate(data):
        y = y0 + i * rh
        cv.put(28, y + 4, label, 12.5, "PS", 400, "text")
        over = share > 1
        cv.add(f'<path d="{B.bar_path(x0, y - 5, max(bw * share / top, 2), 11)}" fill="{c["red"] if over else c["mark"]}"/>')
        cv.put(vx, y + 4, med, 11, "PM", 600, "text")
        cv.put(vx + B.width(med + "  ", 11, "PM", 600), y + 4, f"/  {tgt}", 11, "PM", 400, "text3")
    cv.put(28, h - 14, "Every median sits inside its target." if top <= 1 else "A red bar is over its target.",
           11, "PS", 400, "text3")
    return cv


def chart_compare(mode: str, spec, node, files) -> B.Canvas:
    rows = md_table(files["README.md"], ["Criterion", "This grammar", "nwhetsell", "Result"], "tree-sitter-lilypond README.md")
    data = []
    for crit, mine, theirs, res in (r[:4] for r in rows):
        res = plain(res).strip().lower()
        need(res in ("passes", "fails"), f"tree-sitter-lilypond: result {res!r} is neither passes nor fails")
        a, b = number(mine, "compare"), number(theirs, "compare")
        tot = re.search(r"of\s+" + NUM, mine)
        scale = float(tot.group(1).replace(",", "")) if tot else max(a, b)
        data.append((plain(crit), plain(mine), plain(theirs), a, b, scale, res))
    rh, y0 = 74, 120
    h = y0 + len(data) * rh + 8
    cv = B.Canvas(860, h, mode, "tree-sitter-lilypond against nwhetsell's grammar",
                  "Three criteria, each with its own scale: " +
                  "; ".join(f"{d[0]}: this grammar {d[1]}, nwhetsell {d[2]}, {d[6]}" for d in data) + ".")
    c = cv.c
    B.card_frame(cv)
    chart_head(cv, "Against the other LilyPond grammar",
               "This grammar and nwhetsell's tree-sitter-lilypond on the same corpus. Each row has its own scale. "
               "The figures are the comparison table in this README.", 860)
    # Legend: two series, so it is always shown.
    lx = 28
    for name, col in (("This grammar", "mark"), ("nwhetsell", "text3")):
        cv.add(f'<rect x="{lx}" y="{y0 - 26}" width="12" height="12" rx="3" fill="{c[col]}"/>')
        cv.put(lx + 18, y0 - 16, name, 12, "PS", 400, "text")
        lx += 18 + B.width(name, 12) + 22
    # The status column is pinned to the right edge, and the bars get what is
    # left once the longest value label has room after the longest bar.
    sx = 860 - 28 - max(B.width(d[6], 12.5, "PS", 600) for d in data) - 15 - 8
    lab = max(B.width(t, 11, "PM", 500) for d in data for t in (d[1], d[2]))
    bx = 388
    bmax = sx - 8 - 24 - lab - 8 - bx
    need(bmax > 140, "tree-sitter-lilypond compare: value labels leave no room for the bars")
    for i, (crit, mine, theirs, a, b, scale, res) in enumerate(data):
        y = y0 + i * rh
        cv.add(f'<line x1="28" y1="{y - 2}" x2="832" y2="{y - 2}" stroke="{c["line"]}"/>')
        for k, line in enumerate(B.wrap(crit, bx - 28 - 24, 12.5, maxlines=3)):
            cv.put(28, y + 22 + k * 17, line, 12.5, "PS", 400, "text")
        for j, (val, txt, col) in enumerate(((a, mine, "mark"), (b, theirs, "text3"))):
            by = y + 13 + j * 22
            wv = max(bmax * val / scale, 2)
            cv.add(f'<path d="{B.bar_path(bx, by, wv, 12)}" fill="{c[col]}"/>')
            cv.put(bx + wv + 8, by + 10.5, txt, 11, "PM", 500, "text")
        good = res == "passes"
        sy = y + 30
        col = c["green"] if good else c["red"]
        cv.add(f'<circle cx="{sx}" cy="{sy}" r="8" fill="{col}"/>')
        if good:
            cv.add(f'<path d="M{sx - 3.6},{sy} l2.6,2.8 l4.8,-5.4" fill="none" stroke="{c["bg"]}" stroke-width="1.8" '
                   f'stroke-linecap="round" stroke-linejoin="round"/>')
        else:
            cv.add(f'<path d="M{sx - 3},{sy - 3} l6,6 M{sx + 3},{sy - 3} l-6,6" stroke="{c["bg"]}" stroke-width="1.8" '
                   f'stroke-linecap="round"/>')
        cv.put(sx + 15, sy + 4.5, res, 12.5, "PS", 600, "text")
    return cv


COVER_KINDS = [("set up", "mark"), ("waiting", "orange"), ("not working", "red"), ("check failed", "violet"), ("none", None)]


def cover_kind(cell: str) -> str:
    s = plain(cell).strip().lower()
    if s == "none":
        return "none"
    for k in ("waiting", "not working", "check failed"):
        if s.startswith(k):
            return k
    return "set up"


def chart_coverage(mode: str, spec, node, files) -> B.Canvas:
    doc = files["docs/languages.md"]
    layers = ["Tree-sitter", "Server", "Formatter", "Linter", "Grammar"]
    rows = md_table(doc, ["Language"] + layers, "ACH-NEOVIM docs/languages.md")
    m = re.search(r"Measured on (\d{4}-\d\d-\d\d) with Neovim (\d+(?:\.\d+)+)", doc)
    need(m, "ACH-NEOVIM: docs/languages.md no longer says when it was measured")
    when, nvim = dt.date.fromisoformat(m.group(1)), m.group(2)
    n = len(rows)
    tally = {L: {k: 0 for k, _ in COVER_KINDS} for L in layers}
    for r in rows:
        for L, cell in zip(layers, r[1:6]):
            tally[L][cover_kind(cell)] += 1
    present = [(k, col) for k, col in COVER_KINDS if any(tally[L][k] for L in layers)]
    rh, y0 = 40, 128
    h = y0 + len(layers) * rh + 18
    cv = B.Canvas(860, h, mode, "ACH-NEOVIM language coverage by layer",
                  f"For each of {n} languages, measured {B.fmt_date(when)} with Neovim {nvim}: " +
                  "; ".join(f"{L}: " + ", ".join(f"{tally[L][k]} {k}" for k, _ in present if tally[L][k]) for L in layers) + ".")
    c = cv.c
    # The profile's dark mark and orange sit above the lightness band a chart
    # fill should keep to on a dark surface (OKLCH L 0.48 to 0.67), so this chart
    # steps both down. Checked with the dataviz palette validator on 2026-11-14:
    # every check passes, CVD separation 23 and the count text on each fill
    # above 5:1. Red and violet appear only when a layer is broken, carry a
    # legend label and the in-bar count, and keep the theme tokens.
    fills = dict(c, mark="#0093c2", orange="#cf7a1a") if mode == "dark" else c
    B.card_frame(cv)
    chart_head(cv, "Language coverage by layer",
               f"What each layer has working for the {n} languages in docs/languages.md, measured "
               f"{B.fmt_date(when)} with Neovim {nvim}. The table there names every tool.", 860)
    lx = 28
    for k, col in present:
        if col:
            cv.add(f'<rect x="{lx}" y="{y0 - 30}" width="12" height="12" rx="3" fill="{fills[col]}"/>')
        else:
            cv.add(f'<rect x="{lx + 0.5}" y="{y0 - 29.5}" width="11" height="11" rx="3" fill="{c["bg3"]}" stroke="{c["border"]}"/>')
        cv.put(lx + 18, y0 - 20, k, 12, "PS", 400, "text")
        lx += 18 + B.width(k, 12) + 22
    bx, bw = 132, 860 - 28 - 132
    for i, L in enumerate(layers):
        y = y0 + i * rh
        cv.put(28, y + 14, L, 12.5, "PS", 400, "text")
        x = bx
        segs = [(k, col, tally[L][k]) for k, col in present if tally[L][k]]
        for j, (k, col, cnt) in enumerate(segs):
            w = bw * cnt / n - (2 if j < len(segs) - 1 else 0)
            fill = fills[col] if col else c["bg3"]
            ring = "" if col else f' stroke="{c["border"]}"'
            cv.add(f'<rect x="{x:.1f}" y="{y}" width="{max(w, 1):.1f}" height="20" rx="{3 if w > 6 else 1}" '
                   f'fill="{fill}"{ring}/>')
            if w >= 16:
                cv.put(x + w / 2, y + 14.5, str(cnt), 11, "PM", 600, "bg" if col else "text2", "middle")
            x += w + 2
    return cv


def chart_packages(mode: str, spec, node, files) -> B.Canvas:
    readme, brewfile = files["README.md"], files["Brewfile"]
    groups = []
    for head, col, what, kind in ((r"^### CLI tools \((\d+) formulae\)", "Packages", "formulae", "brew"),
                                  (r"^### GUI apps and fonts \((\d+) casks\)", "Casks", "casks", "cask")):
        hm = re.search(head, readme, re.M)
        need(hm, f"Post-Mac-Setup README.md: no heading matching {head!r}")
        rows = md_table(readme, ["Category", col], "Post-Mac-Setup README.md", head)
        cats = [(plain(r[0]), len(re.findall(r"`[^`]+`", r[1]))) for r in rows]
        total = sum(n for _, n in cats)
        brew = len(re.findall(rf'^\s*{kind}\s+"', brewfile, re.M))
        need(total == int(hm.group(1)) == brew,
             f"Post-Mac-Setup: {what} table sums to {total}, heading says {hm.group(1)}, Brewfile has {brew}")
        groups.append((what, total, cats))
    im = re.search(r"(\d+) command-line tools, (\d+) GUI apps and (\d+) fonts", readme)
    need(im, "Post-Mac-Setup README.md: the intro no longer gives the three totals")
    fonts = sum(n for name, n in groups[1][2] if name.lower().startswith("fonts"))
    need((int(im.group(1)), int(im.group(2)), int(im.group(3))) == (groups[0][1], groups[1][1] - fonts, fonts),
         "Post-Mac-Setup README.md: the intro's totals disagree with the tables")
    top = max(n for _, _, cats in groups for _, n in cats)
    rows_n = max(len(cats) for _, _, cats in groups)
    rh, y0 = 21, 120
    h = y0 + rows_n * rh + 12
    cv = B.Canvas(860, h, mode, "What Post-Mac-Setup installs",
                  f"{groups[0][1]} Homebrew formulae and {groups[1][1]} casks by category. " +
                  " ".join(f"{what.capitalize()}: " + ", ".join(f"{name} {n}" for name, n in cats) + "."
                           for what, _, cats in groups))
    c = cv.c
    B.card_frame(cv)
    chart_head(cv, "What the script installs",
               f"{groups[0][1]} Homebrew formulae and {groups[1][1]} casks, by category, counted from the tables in "
               "this README and checked against the Brewfile. One scale for both columns.", 860)
    colw = (860 - 56 - 28) / 2
    for g, (what, total, cats) in enumerate(groups):
        gx = 28 + g * (colw + 28)
        cv.put(gx, y0 - 14, f"{total} {what}", 13.5, "PS", 600, "text")
        lw = max(B.width(name, 12) for name, _ in cats) + 12
        bmax = colw - lw - 34
        for i, (name, n) in enumerate(sorted(cats, key=lambda kv: -kv[1])):
            y = y0 + i * rh
            cv.put(gx, y + 9, name, 12, "PS", 400, "text")
            wv = bmax * n / top
            cv.add(f'<path d="{B.bar_path(gx + lw, y - 1, wv, 12)}" fill="{c["mark"]}"/>')
            cv.put(gx + lw + wv + 6, y + 9.5, str(n), 11, "PM", 600, "text2")
    return cv


CHARTS = {"speed": chart_speed, "compare": chart_compare, "coverage": chart_coverage, "packages": chart_packages}


# --------------------------------------------------------------------------


def main() -> int:
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("GITHUB_TOKEN is not set", file=sys.stderr)
        return 2
    try:
        got = fetch(token)
        made = {}
        for spec in CFG["repos"]:
            node, files = got[spec["name"]]
            base = f'repo-{spec["name"].lower()}'
            for mode in B.MODES:
                made[f"{base}-banner-{mode}.svg"] = banner(mode, spec, node, files).render()
                for ch in spec["charts"]:
                    made[f"{base}-{ch}-{mode}.svg"] = CHARTS[ch](mode, spec, node, files).render()
    except Drift as e:
        print(f"repos.py: {e}", file=sys.stderr)
        return 1
    B.DIST.mkdir(exist_ok=True)
    for name, svg in made.items():
        (B.DIST / name).write_text(svg, encoding="utf-8")
    print(f"wrote {len(made)} SVGs to dist/, {sum(map(len, made.values())) / 1024:.0f} KiB in all")
    return 0


if __name__ == "__main__":
    sys.exit(main())
