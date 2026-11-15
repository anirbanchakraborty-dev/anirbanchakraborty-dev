#!/usr/bin/env python3
"""Draw the profile README's cards as SVG and refresh its live sections.

Inputs are build/profile.json, the GitHub GraphQL API and the site's RSS feed.
Every card is written twice, dist/<name>-dark.svg and dist/<name>-light.svg,
in the website's night and paper palettes, and the marked blocks of README.md
are rewritten from the same data.

    GITHUB_TOKEN=... python build/build.py          build everything
    GITHUB_TOKEN=... python build/build.py --check  exit 1 if README.md would change

Text is set in IBM Plex, subset per card and embedded as WOFF2 under the
aliases PS and PM, since a subset is a modified font and OFL keeps the
reserved name "Plex" for the original.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import html
import io
import json
import math
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

from fontTools import subset
from fontTools.ttLib import TTFont

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
README = ROOT / "README.md"
CFG = json.loads((ROOT / "build" / "profile.json").read_text(encoding="utf-8"))

# Night and Paper, from the website's DESIGN.md. `mark` is the one chart hue:
# night uses the cyan accent itself; paper steps the cyan up to #007ea6 because
# the text token #005f7a falls under the chroma floor as a filled mark.
THEMES = {
    "dark": dict(
        bg="#000f28", bg2="#0d2a5c", bg3="#072658", border="#2a3f63", line="#233657",
        text="#ffffff", text2="#999fa9", text3="#8a93a4", cyan="#00c7fd", green="#00c48c",
        violet="#e2b4fc", bright="#2d76ff", orange="#ff9f43", red="#ff6b6b", mark="#00c7fd",
    ),
    "light": dict(
        bg="#fbf8f1", bg2="#f3eee3", bg3="#ebe5d7", border="#c8bea9", line="#e0d8c6",
        text="#1c1a16", text2="#3b372f", text3="#5a5448", cyan="#005f7a", green="#00614a",
        violet="#6b3fa0", bright="#1a44b8", orange="#8a4b00", red="#a1211f", mark="#007ea6",
    ),
}
MODES = ("dark", "light")

# --------------------------------------------------------------------------
# Fonts: measure with the real advance widths, embed a per-card subset.

FONT_FILES = {
    ("PS", 400): "IBMPlexSans-Regular.woff",
    ("PS", 600): "IBMPlexSans-SemiBold.woff",
    ("PM", 400): "ibm-plex-mono-latin-400-normal.woff2",
    ("PM", 500): "ibm-plex-mono-latin-500-normal.woff2",
    ("PM", 600): "ibm-plex-mono-latin-600-normal.woff2",
}
STACK = {
    "PS": "PS,-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif",
    "PM": "PM,ui-monospace,SFMono-Regular,Menlo,Consolas,monospace",
}


class Face:
    def __init__(self, key: tuple[str, int], file: str):
        self.key = key
        self.raw = (ROOT / "fonts" / file).read_bytes()
        tt = TTFont(io.BytesIO(self.raw))
        self.cmap = tt.getBestCmap()
        self.adv = {g: m[0] for g, m in tt["hmtx"].metrics.items()}
        self.upm = tt["head"].unitsPerEm
        self.missing = self.adv[self.cmap[ord("n")]]
        self._cache: dict[str, str] = {}

    def width(self, s: str, size: float, spacing: float = 0.0) -> float:
        units = sum(self.adv.get(self.cmap.get(ord(ch)), self.missing) for ch in s)
        return units * size / self.upm + spacing * len(s)

    def woff2(self, chars: set[str]) -> str:
        text = "".join(sorted(chars | {" "}))
        if text in self._cache:
            return self._cache[text]
        tt = TTFont(io.BytesIO(self.raw))
        opts = subset.Options()
        opts.flavor = "woff2"
        opts.layout_features = ["kern"]
        opts.name_IDs = [0, 1, 2, 3, 4, 5, 6]
        sub = subset.Subsetter(opts)
        sub.populate(text=text)
        sub.subset(tt)
        alias = f"{self.key[0]}{self.key[1]}"
        for rec in tt["name"].names:
            if rec.nameID in (1, 3, 4, 6, 16, 17):
                rec.string = alias
        tt.flavor = "woff2"
        buf = io.BytesIO()
        tt.save(buf)
        self._cache[text] = base64.b64encode(buf.getvalue()).decode("ascii")
        return self._cache[text]


FACES = {k: Face(k, f) for k, f in FONT_FILES.items()}


def esc(s) -> str:
    return html.escape(str(s), quote=True)


def width(s: str, size: float, fam: str = "PS", wt: int = 400, spacing: float = 0.0) -> float:
    return FACES[(fam, wt)].width(s, size, spacing)


def wrap(s: str, maxw: float, size: float, fam="PS", wt=400, maxlines: int | None = None) -> list[str]:
    lines, cur = [], ""
    for word in s.split():
        trial = f"{cur} {word}".strip()
        if not cur or width(trial, size, fam, wt) <= maxw:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    if maxlines and len(lines) > maxlines:
        lines = lines[:maxlines]
        last = lines[-1]
        while last and width(last + "…", size, fam, wt) > maxw:
            last = last.rsplit(" ", 1)[0] if " " in last else last[:-1]
        lines[-1] = last.rstrip(" ,;:-—") + "…"
    return lines


class Canvas:
    """One SVG document: body parts, defs, CSS and the characters each face draws."""

    def __init__(self, w: int, h: int, mode: str, title: str, desc: str):
        self.w, self.h, self.mode, self.c = w, h, mode, THEMES[mode]
        self.title, self.desc = title, desc
        self.body: list[str] = []
        self.defs: list[str] = []
        self.css: list[str] = []
        self.chars: dict[tuple[str, int], set[str]] = {}

    def col(self, name: str) -> str:
        return self.c.get(name, name)

    def add(self, raw: str) -> "Canvas":
        self.body.append(raw)
        return self

    def text(self, x, y, s, size=14, fam="PS", wt=400, fill="text", anchor="start",
             spacing=0.0, cls="", extra="") -> str:
        s = str(s)
        self.chars.setdefault((fam, wt), set()).update(s)
        attrs = f'x="{x:.1f}" y="{y:.1f}" class="{(fam.lower() + " " + cls).strip()}" font-size="{size}"'
        if wt != 400:
            attrs += f' font-weight="{wt}"'
        if anchor != "start":
            attrs += f' text-anchor="{anchor}"'
        if spacing:
            attrs += f' letter-spacing="{spacing}"'
        return f'<text {attrs} fill="{self.col(fill)}"{extra}>{esc(s)}</text>'

    def put(self, *a, **k) -> "Canvas":
        return self.add(self.text(*a, **k))

    def render(self) -> str:
        faces = "".join(
            "@font-face{font-family:%s;font-weight:%d;src:url(data:font/woff2;base64,%s) format('woff2')}"
            % (fam, wt, FACES[(fam, wt)].woff2(chars))
            for (fam, wt), chars in sorted(self.chars.items())
        )
        css = (
            faces
            + ".ps{font-family:%s}.pm{font-family:%s}" % (STACK["PS"], STACK["PM"])
            + "".join(self.css)
            + "@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}"
        )
        defs = f"<defs>{''.join(self.defs)}</defs>" if self.defs else ""
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w}" height="{self.h}" '
            f'viewBox="0 0 {self.w} {self.h}" role="img" aria-labelledby="t d">'
            f'<title id="t">{esc(self.title)}</title><desc id="d">{esc(self.desc)}</desc>'
            f"<style>{css}</style>{defs}{''.join(self.body)}</svg>"
        )


def card_frame(cv: Canvas, rx=12):
    cv.add(f'<rect x="0.5" y="0.5" width="{cv.w - 1}" height="{cv.h - 1}" rx="{rx}" '
           f'fill="{cv.col("bg")}" stroke="{cv.col("border")}"/>')


def bar_path(x, y, w, h, r=4.0) -> str:
    """A bar anchored on the left, rounded only at its data end."""
    w = max(w, 0.0)
    r = min(r, w, h / 2)
    if r <= 0:
        return f"M{x:.1f},{y:.1f}h0v{h:.1f}z"
    return (f"M{x:.1f},{y:.1f}h{w - r:.1f}a{r:.1f},{r:.1f} 0 0 1 {r:.1f},{r:.1f}"
            f"v{h - 2 * r:.1f}a{r:.1f},{r:.1f} 0 0 1 {-r:.1f},{r:.1f}h{-(w - r):.1f}z")


def fmt_int(n: int) -> str:
    return f"{n:,}"


def fmt_date(d: dt.date) -> str:
    return f"{d.day} {d:%b %Y}"


def fmt_day(d: dt.date) -> str:
    return f"{d.day} {d:%b}"


# --------------------------------------------------------------------------
# Data

GQL = """
query($login: String!) {
  user(login: $login) {
    login
    followers { totalCount }
    repositories(first: 100, ownerAffiliations: OWNER, privacy: PUBLIC, isFork: false,
                 orderBy: {field: PUSHED_AT, direction: DESC}) {
      totalCount
      nodes {
        name description url stargazerCount forkCount pushedAt isArchived
        primaryLanguage { name }
        languages(first: 20, orderBy: {field: SIZE, direction: DESC}) { edges { size node { name } } }
        repositoryTopics(first: 10) { nodes { topic { name } } }
      }
    }
    contributionsCollection {
      totalCommitContributions totalPullRequestContributions totalIssueContributions
      totalPullRequestReviewContributions
      contributionCalendar { totalContributions weeks { contributionDays { date contributionCount } } }
    }
    pullRequests(first: 50, states: MERGED, orderBy: {field: CREATED_AT, direction: DESC}) {
      nodes { title url number mergedAt repository { nameWithOwner isPrivate owner { login } } }
    }
  }
}
"""


def fetch_github(token: str) -> dict:
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=json.dumps({"query": GQL, "variables": {"login": CFG["login"]}}).encode(),
        headers={"Authorization": f"bearer {token}", "Content-Type": "application/json",
                 "User-Agent": "profile-readme-build"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        payload = json.load(r)
    if payload.get("errors"):
        raise SystemExit("GraphQL errors: " + "; ".join(e.get("message", "?") for e in payload["errors"]))
    return payload["data"]["user"]


def fetch_feed() -> list[dict]:
    req = urllib.request.Request(CFG["feed"], headers={"User-Agent": "profile-readme-build"})
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read()
    # RSS needs no DTD; refusing one closes off entity expansion and external entities.
    if re.search(rb"<!(DOCTYPE|ENTITY)", raw, re.I):
        raise SystemExit("feed carries a DTD or entity declaration; refusing to parse it")
    root = ET.fromstring(raw)
    items = []
    for it in root.iter("item"):
        cats = [c.text or "" for c in it.findall("category")]
        pub = it.findtext("pubDate") or ""
        try:
            when = dt.datetime.strptime(pub[:25].strip(), "%a, %d %b %Y %H:%M:%S")
        except ValueError:
            when = dt.datetime(1970, 1, 1)
        items.append({"title": (it.findtext("title") or "").strip(), "link": (it.findtext("link") or "").strip(),
                      "category": cats[0] if cats else "", "when": when})
    return items


def analyse(user: dict, feed: list[dict]) -> dict:
    login = CFG["login"]
    repos = [r for r in user["repositories"]["nodes"] if not r["isArchived"]]
    by_name = {r["name"]: r for r in repos}

    langs: dict[str, int] = {}
    for r in repos:
        for e in r["languages"]["edges"]:
            langs[e["node"]["name"]] = langs.get(e["node"]["name"], 0) + e["size"]
    total_bytes = sum(langs.values()) or 1
    ranked = sorted(langs.items(), key=lambda kv: -kv[1])
    top = ranked[:6]
    other = sum(v for _, v in ranked[6:])
    shares = [(k, v / total_bytes) for k, v in top]
    if other:
        shares.append(("Other", other / total_bytes))

    cc = user["contributionsCollection"]
    cal = cc["contributionCalendar"]
    days = [(dt.date.fromisoformat(d["date"]), d["contributionCount"])
            for w in cal["weeks"] for d in w["contributionDays"]]
    days.sort()
    # Each week keeps its day count. The calendar's first and last weeks are
    # usually partial, and the chart draws those apart from the full ones.
    weeks = [(dt.date.fromisoformat(w["contributionDays"][0]["date"]),
              sum(d["contributionCount"] for d in w["contributionDays"]),
              len(w["contributionDays"])) for w in cal["weeks"] if w["contributionDays"]]

    longest = run = 0
    best_end = None
    for d, n in days:
        run = run + 1 if n else 0
        if run > longest:
            longest, best_end = run, d
    current = 0
    tail = days[:-1] if days and days[-1][1] == 0 else days
    for _, n in reversed(tail):
        if not n:
            break
        current += 1
    busiest = max(days, key=lambda dn: dn[1]) if days else (dt.date.today(), 0)

    counts: dict[str, int] = {}
    for it in feed:
        counts[it["category"]] = counts.get(it["category"], 0) + 1

    prs = [p for p in user["pullRequests"]["nodes"]
           if p["repository"]["owner"]["login"].lower() != login.lower() and not p["repository"]["isPrivate"]]

    return {
        "repos": repos, "by_name": by_name, "shares": shares, "lang_repos": sum(1 for r in repos if r["languages"]["edges"]),
        "stars": sum(r["stargazerCount"] for r in repos), "public_repos": user["repositories"]["totalCount"],
        "total": cal["totalContributions"], "commits": cc["totalCommitContributions"],
        "pulls": cc["totalPullRequestContributions"], "issues": cc["totalIssueContributions"],
        "reviews": cc["totalPullRequestReviewContributions"],
        "days": days, "weeks": weeks, "longest": longest, "longest_end": best_end, "current": current,
        "busiest": busiest, "active": sum(1 for _, n in days if n), "counts": counts, "feed": feed, "prs": prs,
        "today": days[-1][0] if days else dt.date.today(),
    }


# --------------------------------------------------------------------------
# Hero banner

# Assembled with `llvm-mc -triple=riscv32 -mattr=-c -filetype=obj` and read back with
# llvm-objdump -d: one step of a Galois LFSR per loop trip, a0 the state, a1 the taps.
LISTING = [
    ("lfsr:", None, None),
    ("00", "00157293", "andi  t0, a0, 1"),
    ("04", "00155513", "srli  a0, a0, 1"),
    ("08", "00028463", "beqz  t0, skip"),
    ("0c", "00b54533", "xor   a0, a0, a1"),
    ("skip:", None, None),
    ("10", "fff60613", "addi  a2, a2, -1"),
    ("14", "fe0616e3", "bnez  a2, lfsr"),
    ("18", "00008067", "ret"),
]

TRACES = [
    "M0,282 H170 L186,266 H330 L342,278 H470",
    "M860,16 H716 L704,28 H596 L584,16 H500",
    "M846,52 V128 L834,140 V262",
    "M14,0 V30 L26,42 V118",
    "M560,286 H700 L712,274 H818",
]


def hero(mode: str) -> Canvas:
    h = CFG["hero"]
    cv = Canvas(860, 300, mode, h["name"], f'{h["eyebrow"].title()}. ' + ". ".join(h["lines"]) + ".")
    c = cv.c
    cv.defs.append(
        f'<linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="{c["bg"]}"/>'
        f'<stop offset="1" stop-color="{c["bg3"]}"/></linearGradient>'
        '<clipPath id="k"><rect x="0.5" y="0.5" width="859" height="299" rx="16"/></clipPath>'
    )
    cv.add('<g clip-path="url(#k)">')
    cv.add('<rect width="860" height="300" fill="url(#g)"/>')

    # Circuit traces with a pulse running along each.
    for i, d in enumerate(TRACES):
        cv.add(f'<path d="{d}" fill="none" stroke="{c["border"]}" stroke-width="1.5"/>')
        cv.add(f'<path class="p p{i}" d="{d}" pathLength="100" fill="none" stroke="{c["cyan"]}" '
               f'stroke-width="2" stroke-linecap="round" stroke-dasharray="7 93"/>')
        for x, y in (_trace_end(d, 0), _trace_end(d, -1)):
            cv.add(f'<circle cx="{x}" cy="{y}" r="3" fill="{c["bg"]}" stroke="{c["border"]}" stroke-width="1.5"/>')
    cv.css.append("@keyframes run{from{stroke-dashoffset:100}to{stroke-dashoffset:0}}"
                  ".p{stroke-dashoffset:100;animation:run 5s linear infinite}")
    for i in range(len(TRACES)):
        cv.css.append(f".p{i}{{animation-duration:{4 + (i * 1.3) % 3:.1f}s;animation-delay:{-i * 0.9:.1f}s}}")

    # Left: who.
    cv.put(48, 74, h["eyebrow"], 12, "PM", 500, "cyan", spacing=2)
    size = 38
    while width(h["name"], size, "PS", 600) > 450:
        size -= 1
    cv.put(46, 122, h["name"], size, "PS", 600, "text", spacing=-0.4)
    cv.put(48, 160, h["lines"][0], 17, "PS", 400, "text")
    cv.put(48, 186, h["lines"][1], 14, "PS", 400, "text2")

    # Pipeline strip: a highlight sweeps IF to WB.
    stages = ["IF", "ID", "EX", "MEM", "WB"]
    for i, s in enumerate(stages):
        x = 48 + i * 70
        cv.add(f'<rect x="{x}" y="222" width="60" height="32" rx="6" fill="{c["bg2"]}" fill-opacity="0.55" '
               f'stroke="{c["border"]}"/>')
        cv.add(f'<rect class="st s{i}" x="{x}" y="222" width="60" height="32" rx="6" fill="{c["cyan"]}" '
               f'fill-opacity="0" stroke="{c["cyan"]}" stroke-opacity="0"/>')
        cv.put(x + 30, 242.5, s, 12, "PM", 600, "text", "middle", spacing=1)
    cv.css.append("@keyframes stage{0%,100%{fill-opacity:0;stroke-opacity:0}8%{fill-opacity:.28;stroke-opacity:1}"
                  "28%{fill-opacity:0;stroke-opacity:0}}.st{animation:stage 3s ease-out infinite}")
    for i in range(5):
        cv.css.append(f".s{i}{{animation-delay:{i * 0.42:.2f}s}}")

    # Right: the listing, with the executing line stepping down.
    px, py, pw, ph = 528, 40, 296, 224
    cv.add(f'<rect x="{px}" y="{py}" width="{pw}" height="{ph}" rx="12" fill="{c["bg"]}" fill-opacity="0.72" '
           f'stroke="{c["border"]}"/>')
    cv.put(px + 18, py + 26, "lfsr.s", 11, "PM", 600, "text2")
    cv.put(px + pw - 18, py + 26, "RV32I", 11, "PM", 500, "text3", "end", spacing=1)
    cv.add(f'<line x1="{px}" y1="{py + 38}" x2="{px + pw}" y2="{py + 38}" stroke="{c["border"]}"/>')
    row0, lh = py + 62, 18.5
    inst_rows = [i for i, r in enumerate(LISTING) if r[1]]
    cv.add(f'<rect class="hl" x="{px + 8}" y="{row0 + inst_rows[0] * lh - 13:.1f}" width="{pw - 16}" '
           f'height="18" rx="4" fill="{c["cyan"]}" fill-opacity="0.14"/>')
    frames = []
    for k, r in enumerate(inst_rows):
        dy = (r - inst_rows[0]) * lh
        frames.append(f"{k * 100 / len(inst_rows):.2f}%{{transform:translateY({dy:.1f}px)}}")
    frames.append(f"100%{{transform:translateY(0px)}}")
    cv.css.append("@keyframes step{" + "".join(frames) + "}"
                  f".hl{{animation:step {len(inst_rows) * 0.9:.1f}s steps(1,end) infinite}}")
    for i, (addr, enc, asm) in enumerate(LISTING):
        y = row0 + i * lh
        if enc is None:
            cv.put(px + 18, y, addr, 12, "PM", 500, "violet")
            continue
        cv.put(px + 18, y, addr, 12, "PM", 400, "text3")
        cv.put(px + 46, y, enc, 12, "PM", 400, "cyan")
        cv.put(px + 130, y, asm, 12, "PM", 400, "text2")
    cv.add("</g>")
    cv.add(f'<rect x="0.5" y="0.5" width="859" height="299" rx="16" fill="none" stroke="{c["border"]}"/>')
    return cv


def _trace_end(d: str, idx: int) -> tuple[float, float]:
    """First or last point of an M/H/V/L path."""
    x = y = 0.0
    pts = []
    for cmd, args in re.findall(r"([MHVL])\s*([-\d.,\s]+)", d):
        nums = [float(n) for n in re.findall(r"-?[\d.]+", args)]
        if cmd in "ML":
            x, y = nums[0], nums[1]
        elif cmd == "H":
            x = nums[0]
        elif cmd == "V":
            y = nums[0]
        pts.append((x, y))
    return pts[idx]


# --------------------------------------------------------------------------
# "Now" pill


def fill_counts(s: str, counts: dict) -> str:
    return re.sub(r"\{count:([^}]+)\}", lambda m: str(counts.get(m.group(1), 0)), s)


def now_pill(mode: str, entries: list[str]) -> Canvas:
    cv = Canvas(860, 52, mode, "Currently", " / ".join(entries))
    c = cv.c
    lw = width("NOW", 12, "PM", 600, 2)
    tw = max(width(e, 15) for e in entries)
    pw = 22 + 8 + 12 + lw + 14 + 14 + tw + 24
    x0 = (860 - pw) / 2
    cv.add(f'<rect x="{x0:.1f}" y="4.5" width="{pw:.1f}" height="43" rx="21.5" fill="{c["bg"]}" stroke="{c["border"]}"/>')
    dx = x0 + 26
    cv.add(f'<circle class="ring" cx="{dx:.1f}" cy="26" r="4" fill="none" stroke="{c["green"]}" stroke-width="1.5"/>')
    cv.add(f'<circle cx="{dx:.1f}" cy="26" r="4" fill="{c["green"]}"/>')
    cv.css.append("@keyframes ring{0%{r:4px;stroke-opacity:.9}100%{r:11px;stroke-opacity:0}}"
                  ".ring{animation:ring 2s ease-out infinite}")
    lx = dx + 14
    cv.put(lx, 30.5, "NOW", 12, "PM", 600, "cyan", spacing=2)
    sx = lx + lw + 12
    cv.add(f'<line x1="{sx:.1f}" y1="15" x2="{sx:.1f}" y2="37" stroke="{c["border"]}"/>')
    tx = sx + 14
    n, slot_s = len(entries), 4
    slot = 100 / n
    cv.css.append(
        "@keyframes nf{0%%{opacity:0;transform:translateY(6px)}%.2f%%{opacity:1;transform:none}"
        "%.2f%%{opacity:1;transform:none}%.2f%%{opacity:0;transform:translateY(-6px)}100%%{opacity:0}}"
        % (slot * 0.1, slot * 0.88, slot)
        + f".n{{opacity:0;animation:nf {n * slot_s}s ease-in-out infinite both}}.n0{{opacity:1}}"
    )
    for i, e in enumerate(entries):
        # Start half a second into the cycle, so a renderer that shows only the
        # first frame shows the first entry rather than an empty pill.
        cv.css.append(f".n{i}{{animation-delay:{i * slot_s - 0.5:g}s}}")
        cv.add(f'<g class="n n{i}">' + cv.text(tx, 31.5, e, 15, "PS", 400, "text") + "</g>")
    return cv


# --------------------------------------------------------------------------
# Link pills

ICONS = {
    "globe": '<circle cx="8" cy="8" r="6.5"/><ellipse cx="8" cy="8" rx="2.8" ry="6.5"/><path d="M1.5 8h13"/>',
    "briefcase": '<rect x="1.5" y="4.5" width="13" height="9" rx="1.5"/><path d="M5.5 4.5V3a1 1 0 0 1 1-1h3a1 1 0 0 1 1 1v1.5M1.5 8.5h13"/>',
    "cap": '<path d="M8 2.5 1 6l7 3.5L15 6z"/><path d="M4 7.6v3.1c0 1 1.8 2.3 4 2.3s4-1.3 4-2.3V7.6M15 6v4"/>',
    "paper": '<path d="M3.5 1.5h6l3 3v10h-9z"/><path d="M9.5 1.5v3h3M5.5 8h5M5.5 10.5h5M5.5 13h3"/>',
    "rss": '<circle cx="3.5" cy="12.5" r="1.4" fill="currentColor" stroke="none"/><path d="M2.5 7a6.5 6.5 0 0 1 6.5 6.5M2.5 2.5a11 11 0 0 1 11 11"/>',
    "mail": '<rect x="1.5" y="3.5" width="13" height="9" rx="1.5"/><path d="m2 4.5 6 4.5 6-4.5"/>',
}


def link_pill(mode: str, link: dict) -> Canvas:
    tw = width(link["label"], 13, "PS", 600)
    w = math.ceil(14 + 16 + 8 + tw + 16)
    cv = Canvas(w, 36, mode, link["label"], f'{link["label"]}: {link["url"]}')
    c = cv.c
    cv.add(f'<rect x="0.5" y="0.5" width="{w - 1}" height="35" rx="17.5" fill="{c["bg"]}" stroke="{c["border"]}"/>')
    cv.add(f'<g transform="translate(14 10)" fill="none" stroke="{c["cyan"]}" color="{c["cyan"]}" '
           f'stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">{ICONS[link["icon"]]}</g>')
    cv.put(38, 22.5, link["label"], 13, "PS", 600, "text")
    return cv


# --------------------------------------------------------------------------
# Project cards


def motif(name: str, col: str, faint: str) -> str:
    def st(w=1.6):
        return f'fill="none" stroke="{col}" stroke-width="{w}" stroke-linecap="round" stroke-linejoin="round"'
    s = st()
    if name == "staff":
        lines = "".join(f'<path d="M7 {y}h26"/>' for y in (12, 16, 20, 24, 28))
        return (f'<g {st(1)} stroke-opacity="0.55">{lines}</g>'
                f'<g {s}><path d="M23.4 25V9.5"/><path d="M23.4 9.5c2 1.4 4.6 2.2 5.4 4.6"/></g>'
                f'<ellipse cx="20" cy="25.6" rx="3.8" ry="2.7" transform="rotate(-22 20 25.6)" fill="{col}"/>')
    if name == "parity":
        out = []
        bits = [[1, 0, 1], [0, 1, 1], [1, 1, 0]]
        for r in range(4):
            for q in range(4):
                if r < 3 and q < 3:
                    on = bits[r][q]
                    par = False
                elif r < 3:
                    on, par = sum(bits[r]) % 2, True
                elif q < 3:
                    on, par = sum(bits[k][q] for k in range(3)) % 2, True
                else:
                    on, par = sum(map(sum, bits)) % 2, True
                fill = col if on else "none"
                op = "1" if par else "0.55"
                out.append(f'<rect x="{6 + q * 7.5}" y="{6 + r * 7.5}" width="5.5" height="5.5" rx="1.2" '
                           f'fill="{fill}" fill-opacity="{op}" stroke="{col}" stroke-width="1" stroke-opacity="{op}"/>')
        return "".join(out)
    if name == "prompt":
        return f'<g {st(2)}><path d="M10 13l7 7-7 7"/><path d="M21 28h10"/></g>'
    if name == "chip":
        pins = "".join(f'<path d="M{p} 8v3M{p} 29v3M8 {p}h3M29 {p}h3"/>' for p in (15, 20, 25))
        return (f'<g {s}><rect x="11" y="11" width="18" height="18" rx="2.5"/>{pins}</g>'
                f'<rect x="16" y="16" width="8" height="8" rx="1" fill="{col}" fill-opacity="0.55"/>')
    if name == "tree":
        nodes = [(20, 9), (12, 20), (28, 20), (7, 31), (17, 31), (28, 31)]
        edges = [(0, 1), (0, 2), (1, 3), (1, 4), (2, 5)]
        e = "".join(f'<path d="M{nodes[a][0]} {nodes[a][1]}L{nodes[b][0]} {nodes[b][1]}"/>' for a, b in edges)
        n = "".join(f'<circle cx="{x}" cy="{y}" r="2.6" fill="{col}"/>' for x, y in nodes)
        return f'<g {s} stroke-opacity="0.7">{e}</g>{n}'
    if name == "layers":
        return (f'<g {s}><path d="M20 9l12 6-12 6-12-6z" fill="{col}" fill-opacity="0.25"/>'
                f'<path d="M8 20.5l12 6 12-6"/><path d="M8 26l12 6 12-6"/></g>')
    return ""


STAR = "M6 0.8l1.6 3.3 3.6.5-2.6 2.5.6 3.6L6 9l-3.2 1.7.6-3.6L.8 4.6l3.6-.5z"
FORK = ('<circle cx="3" cy="2.5" r="1.6"/><circle cx="9" cy="2.5" r="1.6"/><circle cx="6" cy="10" r="1.6"/>'
        '<path d="M3 4.1v1.2c0 1 .8 1.7 1.7 1.7h2.6c.9 0 1.7-.7 1.7-1.7V4.1M6 7v1.4"/>')


def project_desc(p: dict, repo: dict | None) -> str:
    return (repo or {}).get("description") or p.get("description", "")


DESC_W, DESC_PT, DESC_LH = 380, 13, 19


def project_card(mode: str, p: dict, repo: dict | None, rows: int = 3) -> Canvas:
    """Draw one project card.

    `rows` is the line count of the longest description among all the cards,
    so every card is the same height and none of them cuts its text short.
    """
    name = p["repo"]
    desc = project_desc(p, repo)
    topics = [t["topic"]["name"] for t in (repo or {}).get("repositoryTopics", {}).get("nodes", [])]
    lang = p.get("language") or ((repo or {}).get("primaryLanguage") or {}).get("name")
    extra = (rows - 3) * DESC_LH
    cv = Canvas(420, 184 + extra, mode, name, f"{name}: {desc}")
    c = cv.c
    acc = c[p["accent"]]
    card_frame(cv)
    cv.add(f'<rect x="20.5" y="20.5" width="40" height="40" rx="10" fill="{acc}" fill-opacity="0.12" '
           f'stroke="{acc}" stroke-opacity="0.45"/>')
    cv.add(f'<g transform="translate(20.5 20.5)">{motif(p["motif"], acc, c["text3"])}</g>')
    size = 17
    while width(name, size, "PS", 600) > 320:
        size -= 1
    cv.put(76, 38, name, size, "PS", 600, "text")
    sub = p.get("tag") or " · ".join(topics[:3])
    cv.put(76, 57, sub, 11, "PM", 400, "text3")
    for i, line in enumerate(wrap(desc, DESC_W, DESC_PT, maxlines=rows)):
        cv.put(20, 92 + i * DESC_LH, line, DESC_PT, "PS", 400, "text2")
    cv.add(f'<line x1="20" y1="{142.5 + extra:g}" x2="400" y2="{142.5 + extra:g}" stroke="{c["line"]}"/>')
    x, y = 20, 166 + extra
    if lang:
        known = lang in CFG["lang_accent"]
        dot = c[CFG["lang_accent"][lang]] if known else c["text3"]
        if known:
            cv.add(f'<circle cx="{x + 5}" cy="{y - 4}" r="5" fill="{dot}"/>')
            x += 16
        cv.put(x, y, lang, 12, "PS", 400, "text2")
        x += width(lang, 12) + 18
    if repo:
        cv.add(f'<path transform="translate({x} {y - 10})" d="{STAR}" fill="none" stroke="{c["text3"]}" '
               f'stroke-width="1.2" stroke-linejoin="round"/>')
        cv.put(x + 17, y, fmt_int(repo["stargazerCount"]), 12, "PS", 400, "text2")
        x += 17 + width(fmt_int(repo["stargazerCount"]), 12) + 16
        cv.add(f'<g transform="translate({x} {y - 11})" fill="none" stroke="{c["text3"]}" stroke-width="1.2">{FORK}</g>')
        cv.put(x + 16, y, fmt_int(repo["forkCount"]), 12, "PS", 400, "text2")
        pushed = dt.datetime.fromisoformat(repo["pushedAt"].replace("Z", "+00:00")).date()
        cv.put(400, y, f"Updated {fmt_date(pushed)}", 12, "PS", 400, "text3", "end")
    return cv


# --------------------------------------------------------------------------
# Book covers


def cover_motif(name: str, col: str, faint: str, w=148, h=110) -> str:
    def st(w=1.4):
        return f'fill="none" stroke="{col}" stroke-width="{w}" stroke-linejoin="round" stroke-linecap="round"'
    s = st()
    if name == "pipeline":
        out = []
        for r in range(4):
            for k in range(5):
                x, y = r * 11.5 + k * 21, 14 + r * 22
                out.append(f'<rect x="{x:.1f}" y="{y}" width="17" height="15" rx="3" fill="{col}" '
                           f'fill-opacity="{0.9 if k == 2 else 0.12}" stroke="{col}" stroke-opacity="{0.9 if k == 2 else 0.5}"/>')
        return "".join(out)
    if name == "timing":
        clk = "M0 34" + "".join(f"h12V14h12V34" for _ in range(6))
        data = ("M0 72h14l6-10h40l6 10h-6l-6 10h-40l-6-10" + "M66 72l6-10h46l6 10l-6 10h-46l-6-10"
                "M124 72l6-10h18M124 72l6 10h18")
        return (f'<path d="{clk}" {s}/>'
                f'<path d="M0 72h14l6-10h40l6 10M0 72h14l6 10h40l6-10M66 72l6-10h46l6 10M66 72l6 10h46l6-10'
                f'M124 72l6-10h18M124 72l6 10h18" fill="none" stroke="{col}" stroke-width="1.4" stroke-opacity="0.65" stroke-linejoin="round"/>'
                f'<path d="M48 4V100" stroke="{col}" stroke-width="1.2" stroke-dasharray="3 4"/>'
                f'<circle cx="48" cy="14" r="3" fill="{col}"/>')
    if name == "lattice":
        # A skewed two-dimensional lattice: every point is a whole-number mix
        # of the two basis vectors drawn from the origin, and the shaded cell is
        # the parallelogram they span. The ring off the grid is a lattice point
        # pushed by a small error, the picture behind learning with errors, and
        # the dashed segment leads back to the point it came from.
        b1, b2 = (22.0, 5.0), (8.0, 19.0)
        o = (46.0, 64.0)

        def at(i, j):
            return o[0] + i * b1[0] + j * b2[0], o[1] + i * b1[1] + j * b2[1]

        pts = []
        for i in range(-6, 8):
            for j in range(-5, 6):
                x, y = at(i, j)
                if 4 <= x <= w - 4 and 4 <= y <= h - 4:
                    pts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.2" fill="{col}" fill-opacity="0.72"/>')
        cell = (f'<path d="M{o[0]} {o[1]}l{b1[0]} {b1[1]}l{b2[0]} {b2[1]}l{-b1[0]} {-b1[1]}z" fill="{col}" '
                f'fill-opacity="0.28" stroke="{col}" stroke-opacity="0.5" stroke-width="0.8"/>')

        def arrow(b):
            tx, ty = o[0] + b[0], o[1] + b[1]
            n = math.hypot(*b)
            ux, uy = b[0] / n, b[1] / n
            head = (f"M{tx:.1f} {ty:.1f}L{tx - 6 * ux - 3.2 * uy:.1f} {ty - 6 * uy + 3.2 * ux:.1f}"
                    f"L{tx - 6 * ux + 3.2 * uy:.1f} {ty - 6 * uy - 3.2 * ux:.1f}Z")
            return (f'<path d="M{o[0]} {o[1]}L{tx - 4 * ux:.1f} {ty - 4 * uy:.1f}" {st(2)}/>'
                    f'<path d="{head}" fill="{col}"/>')

        lx, ly = at(3, -1)
        tx, ty = lx + 6.0, ly - 7.0
        noise = (f'<path d="M{tx:.1f} {ty:.1f}L{lx:.1f} {ly:.1f}" fill="none" stroke="{faint}" stroke-width="1.2" '
                 f'stroke-dasharray="2 2.5"/>'
                 f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="3" fill="{col}"/>'
                 f'<circle cx="{tx:.1f}" cy="{ty:.1f}" r="3.6" fill="none" stroke="{col}" stroke-width="1.6"/>')
        return (cell + "".join(pts) + arrow(b1) + arrow(b2) + noise
                + f'<circle cx="{o[0]}" cy="{o[1]}" r="3.2" fill="{col}"/>')
    if name == "parity":
        bits = [[1, 0, 1, 1, 0], [0, 1, 1, 0, 1], [1, 1, 0, 0, 0]]
        out = []
        sz, gap = 15, 4
        ox = (w - (6 * sz + 5 * gap)) / 2
        oy = (h - (4 * sz + 3 * gap)) / 2
        for r in range(4):
            for q in range(6):
                if r < 3 and q < 5:
                    on, par = bits[r][q], False
                elif r < 3:
                    on, par = sum(bits[r]) % 2, True
                elif q < 5:
                    on, par = sum(bits[k][q] for k in range(3)) % 2, True
                else:
                    on, par = sum(map(sum, bits)) % 2, True
                x, y = ox + q * (sz + gap), oy + r * (sz + gap)
                out.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{sz}" height="{sz}" rx="3" fill="{col}" '
                           f'fill-opacity="{(0.85 if par else 0.4) if on else 0}" stroke="{col}" '
                           f'stroke-opacity="{0.95 if par else 0.45}" stroke-width="{1.6 if par else 1.1}"/>')
        return "".join(out)
    if name == "aperture":
        cx, cy, R, r0 = w / 2, h / 2, 50.0, 17.0
        verts = [(cx + r0 * math.cos(math.radians(60 * k + 15)), cy + r0 * math.sin(math.radians(60 * k + 15)))
                 for k in range(6)]
        blades = []
        for k in range(6):
            (x1, y1), (x2, y2) = verts[k], verts[(k + 1) % 6]
            dx, dy = x2 - x1, y2 - y1
            n = math.hypot(dx, dy)
            dx, dy = dx / n, dy / n
            fx, fy = x2 - cx, y2 - cy
            b = fx * dx + fy * dy
            t = -b + math.sqrt(b * b - (fx * fx + fy * fy - R * R))
            blades.append(f"M{x1:.1f} {y1:.1f}L{x2 + dx * t:.1f} {y2 + dy * t:.1f}")
        hexagon = "M" + "L".join(f"{x:.1f} {y:.1f}" for x, y in verts) + "z"
        return (f'<circle cx="{cx}" cy="{cy}" r="{R}" {st(1.8)}/>'
                f'<path d="{hexagon}" fill="{col}" fill-opacity="0.18" stroke="none"/>'
                f'<path d="{"".join(blades)}" {s} stroke-opacity="0.8"/>')
    return ""


def book_cover(mode: str, b: dict, count: int) -> Canvas:
    cv = Canvas(200, 280, mode, b["title"], f'{b["title"]}: {count} {b["unit"]} published.')
    c = cv.c
    acc = c[b["accent"]]
    cv.defs.append('<clipPath id="k"><rect x="0.5" y="0.5" width="199" height="279" rx="10"/></clipPath>')
    cv.add(f'<g clip-path="url(#k)"><rect width="200" height="280" fill="{c["bg"]}"/>'
           f'<rect width="8" height="280" fill="{acc}"/></g>')
    cv.add(f'<rect x="0.5" y="0.5" width="199" height="279" rx="10" fill="none" stroke="{c["border"]}"/>')
    cv.put(26, 32, b["kind"], 10, "PM", 600, "text3", spacing=1.6)
    cv.add(f'<g transform="translate(26 46)">{cover_motif(b["motif"], acc, c["text3"])}</g>')
    lines = wrap(b["title"], 156, 17, "PS", 600, maxlines=3)
    y0 = 196 if len(lines) < 3 else 186
    for i, line in enumerate(lines):
        cv.put(26, y0 + i * 21, line, 17, "PS", 600, "text")
    cv.add(f'<line x1="26" y1="243.5" x2="176" y2="243.5" stroke="{c["line"]}"/>')
    cv.put(26, 263, f"{count} {b['unit']}", 11, "PM", 500, "text2")
    return cv


# --------------------------------------------------------------------------
# Stats, languages, activity


SINGULAR = {"contributions": "contribution", "public commits": "public commit", "pull requests": "pull request",
            "issues": "issue", "public repositories": "public repository", "stars earned": "star earned"}


def count_phrase(v: str, label: str) -> str:
    """'1 issue' rather than '1 issues' in the sentence a screen reader hears."""
    k = label.lower()
    return f"{v} {SINGULAR.get(k, k) if v == '1' else k}"


def stats_card(mode: str, a: dict) -> Canvas:
    tiles = [
        (fmt_int(a["total"]), "Contributions"),
        (fmt_int(a["commits"]), "Public commits"),
        (fmt_int(a["pulls"]), "Pull requests"),
        (fmt_int(a["issues"]), "Issues"),
        (fmt_int(a["public_repos"]), "Public repositories"),
        (fmt_int(a["stars"]), "Stars earned"),
    ]
    cv = Canvas(420, 214, mode, "GitHub at a glance",
                "Last 12 months: " + ", ".join(count_phrase(v, k) for v, k in tiles) + ".")
    c = cv.c
    card_frame(cv)
    cv.put(20, 34, "GitHub at a glance", 15, "PS", 600, "text")
    cv.put(20, 54, f"Last 12 months · as of {fmt_date(a['today'])}", 11, "PM", 400, "text3")
    for i, (v, k) in enumerate(tiles):
        col, row = i % 3, i // 3
        x, y = 20 + col * 130, 78 + row * 66
        cv.add(f'<rect x="{x}.5" y="{y}.5" width="120" height="56" rx="8" fill="{c["bg2"]}" fill-opacity="0.45" '
               f'stroke="{c["line"]}"/>')
        cv.put(x + 12, y + 27, v, 22, "PS", 600, "text")
        cv.put(x + 12, y + 45, k, 11.5, "PS", 400, "text3")
    return cv


def langs_card(mode: str, a: dict) -> Canvas:
    shares = a["shares"]
    cv = Canvas(420, 214, mode, "Languages",
                "Share of code by language: " + ", ".join(f"{k} {v * 100:.0f}%" for k, v in shares) + ".")
    c = cv.c
    card_frame(cv)
    cv.put(20, 34, "Languages", 15, "PS", 600, "text")
    cv.put(20, 54, f"Share of bytes across {a['lang_repos']} public repositories", 11, "PM", 400, "text3")
    top = max(v for _, v in shares) if shares else 1
    x0, x1 = 118, 344
    bars = []
    for i, (k, v) in enumerate(shares):
        y = 82 + i * 18.5
        cv.put(20, y + 4, k, 12, "PS", 400, "text2")
        cv.add(f'<rect x="{x0}" y="{y - 4}" width="{x1 - x0}" height="8" rx="4" fill="{c["line"]}"/>')
        d = bar_path(x0, y - 4, (x1 - x0) * v / top, 8)
        bars.append(d)
        cv.add(f'<path d="{d}" fill="{c["mark"]}"/>')
        cv.put(400, y + 4, f"{v * 100:.1f}%", 12, "PM", 400, "text", "end")
    # The bars are drawn at full length from the first frame, so a renderer
    # that never runs the animation still shows the data. The motion is a sheen
    # that crosses the bars and starts off to their left, clipped to them.
    if bars:
        y_top, y_bot = 78, 82 + (len(bars) - 1) * 18.5 + 4
        cv.defs.append('<linearGradient id="sh" x1="0" y1="0" x2="1" y2="0">'
                       '<stop offset="0" stop-color="#fff" stop-opacity="0"/>'
                       '<stop offset="0.5" stop-color="#fff" stop-opacity="0.5"/>'
                       '<stop offset="1" stop-color="#fff" stop-opacity="0"/></linearGradient>')
        cv.defs.append('<clipPath id="lb">' + "".join(f'<path d="{d}"/>' for d in bars) + "</clipPath>")
        cv.add(f'<g clip-path="url(#lb)"><rect class="sh" x="{x0 - 70}" y="{y_top}" width="70" '
               f'height="{y_bot - y_top:g}" fill="url(#sh)"/></g>')
        cv.css.append(f"@keyframes sh{{0%{{transform:translateX(0)}}35%,100%{{transform:translateX({x1 - x0 + 70}px)}}}}"
                      ".sh{animation:sh 7s cubic-bezier(.45,0,.25,1) 1s infinite}")
    return cv


def nice_step(top: float, ticks: int = 3) -> float:
    raw = max(top, 1) / ticks
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        if m * mag >= raw:
            return m * mag
    return 10 * mag


def activity_card(mode: str, a: dict) -> Canvas:
    weeks = a["weeks"]
    days = a["days"]
    first_day, last_day = (days[0][0], days[-1][0]) if days else (a["today"], a["today"])
    cv = Canvas(860, 316, mode, "Weekly contributions",
                f"Contributions per week from {fmt_date(first_day)} to {fmt_date(last_day)}, "
                f"{fmt_int(a['total'])} in all.")
    c = cv.c
    card_frame(cv)
    cv.put(20, 34, "Weekly contributions", 15, "PS", 600, "text")
    cv.put(20, 54, f"{fmt_date(first_day)} to {fmt_date(last_day)} · {fmt_int(a['total'])} contributions",
           11, "PM", 400, "text3")
    X0, X1, Y0, Y1 = 64, 836, 84, 210
    vals = [v for _, v, _ in weeks]
    step = nice_step(max(vals) if vals else 1)
    ymax = step * math.ceil(max(max(vals, default=1), 1) / step)
    for k in range(int(round(ymax / step)) + 1):
        v = k * step
        y = Y1 - (Y1 - Y0) * v / ymax
        dash = "" if k == 0 else ' stroke-dasharray="2 4"'
        cv.add(f'<line x1="{X0}" y1="{y:.1f}" x2="{X1}" y2="{y:.1f}" stroke="{c["line"]}"{dash}/>')
        cv.put(X0 - 10, y + 3.5, fmt_int(int(v)), 10, "PM", 400, "text3", "end")
    n = len(vals)
    xs = [X0 + (X1 - X0) * i / max(n - 1, 1) for i in range(n)]
    ys = [Y1 - (Y1 - Y0) * v / ymax for v in vals]
    # A week the calendar holds only part of is drawn apart from the full ones:
    # a dashed run out to a hollow point, so a week that has barely begun does
    # not read as a week in which activity collapsed. Only the two ends of the
    # calendar can be partial.
    full = [i for i in range(n) if weeks[i][2] == 7]
    lo, hi = (full[0], full[-1]) if full else (0, n - 1)
    partial = [i for i in range(n) if not lo <= i <= hi]
    pts = lambda idx: "L".join(f"{xs[i]:.1f},{ys[i]:.1f}" for i in idx)
    line = "M" + pts(range(lo, hi + 1))
    area = line + f"L{xs[hi]:.1f},{Y1}L{xs[lo]:.1f},{Y1}Z"
    cv.defs.append(f'<linearGradient id="a" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{c["mark"]}" '
                   f'stop-opacity="0.32"/><stop offset="1" stop-color="{c["mark"]}" stop-opacity="0"/></linearGradient>')
    cv.add(f'<path d="{area}" fill="url(#a)"/>')
    for i in partial:
        j = lo if i < lo else hi
        cv.add(f'<path d="M{pts([j, i])}" fill="none" stroke="{c["mark"]}" stroke-width="2" '
               f'stroke-dasharray="3 4" stroke-linecap="round"/>')
    cv.add(f'<path d="{line}" fill="none" stroke="{c["mark"]}" stroke-width="2" '
           f'stroke-linejoin="round" stroke-linecap="round"/>')
    for i in partial:
        cv.add(f'<circle cx="{xs[i]:.1f}" cy="{ys[i]:.1f}" r="4" fill="{c["bg"]}" stroke="{c["mark"]}" '
               f'stroke-width="2"/>')
    # A short bright dash runs along the line and back out of sight. At the
    # first frame it sits before the start of the path, so nothing is hidden
    # and nothing extra is drawn when a renderer shows that frame alone.
    cv.add(f'<path class="pl" d="{line}" pathLength="100" fill="none" stroke="{c["text"]}" stroke-opacity="0.85" '
           f'stroke-width="3" stroke-linejoin="round" stroke-linecap="round" stroke-dasharray="3 300" '
           f'stroke-dashoffset="3"/>')
    cv.css.append("@keyframes pl{0%{stroke-dashoffset:3}55%,100%{stroke-dashoffset:-100}}"
                  ".pl{animation:pl 9s cubic-bezier(.4,0,.3,1) 0.6s infinite}")
    # Month ticks.
    last_x = -99
    for i, (d, _, _) in enumerate(weeks):
        mid = d + dt.timedelta(days=3)
        prev = weeks[i - 1][0] + dt.timedelta(days=3) if i else None
        if prev is None or mid.month != prev.month:
            if xs[i] - last_x >= 40 and xs[i] <= X1 - 12:
                cv.put(xs[i], Y1 + 20, f"{mid:%b}", 10, "PM", 400, "text3", "middle")
                last_x = xs[i]
    # Peak week, labelled directly.
    if partial:
        key = "Partial week"
        kw = width(key, 10, "PM", 400)
        cv.add(f'<circle cx="{X1 - kw - 10:.1f}" cy="50.5" r="3.5" fill="{c["bg"]}" stroke="{c["mark"]}" '
               f'stroke-width="1.75"/>')
        cv.put(X1, 54, key, 10, "PM", 400, "text3", "end")
    if vals:
        pk = max(range(lo, hi + 1), key=lambda i: vals[i])
        px, py = xs[pk], ys[pk]
        # A ring that swells out of the peak marker. It is transparent until its
        # first run, so the marker reads the same in a still frame.
        cv.add(f'<circle class="rg" cx="{px:.1f}" cy="{py:.1f}" r="5" fill="none" stroke="{c["mark"]}" '
               f'stroke-width="1.5" opacity="0"/>')
        cv.css.append("@keyframes rg{0%{transform:scale(1);opacity:.8}100%{transform:scale(2.4);opacity:0}}"
                      ".rg{transform-box:fill-box;transform-origin:center;animation:rg 2.6s ease-out 2s infinite}")
        cv.add(f'<circle cx="{px:.1f}" cy="{py:.1f}" r="5" fill="{c["mark"]}" stroke="{c["bg"]}" stroke-width="2"/>')
        label = f"Peak week: {fmt_int(vals[pk])}, from {fmt_day(weeks[pk][0])}"
        lw = width(label, 11, "PS", 600)
        anchor, lx = ("end", px - 15) if px + 15 + lw > X1 else ("start", px + 15)
        cv.put(lx, max(py - 2, Y0 - 4), label, 11, "PS", 600, "text", anchor)
    # Streak tiles.
    le = a["longest_end"]
    tiles = [
        (f"{a['current']} days", "Current streak"),
        (f"{a['longest']} days", f"Longest streak, to {fmt_day(le)}" if le else "Longest streak"),
        (fmt_int(a["busiest"][1]), f"Busiest day, {fmt_day(a['busiest'][0])}"),
        (f"{a['active']} of {len(a['days'])}", "Days with a contribution"),
    ]
    tw = (860 - 40 - 3 * 12) / 4
    for i, (v, k) in enumerate(tiles):
        x = 20 + i * (tw + 12)
        cv.add(f'<rect x="{x:.1f}" y="246.5" width="{tw:.1f}" height="50" rx="8" fill="{c["bg2"]}" '
               f'fill-opacity="0.45" stroke="{c["line"]}"/>')
        cv.put(x + 14, 268, v, 17, "PS", 600, "text")
        cv.put(x + 14, 286, k, 11.5, "PS", 400, "text3")
    return cv


def toolbox_card(mode: str) -> Canvas:
    groups = CFG["toolbox"]
    X0, X1, chip_h, gap, row_gap = 196, 840, 26, 6, 8
    layout, y = [], 80
    for g in groups:
        x, rows = X0, [[]]
        for item in g["items"]:
            cw = width(item, 12, "PM", 400) + 20
            if x + cw > X1 and rows[-1]:
                rows.append([])
                x = X0
            rows[-1].append((x, item, cw))
            x += cw + gap
        layout.append((g["group"], y, rows))
        y += len(rows) * (chip_h + row_gap) + 10
    cv = Canvas(860, int(y + 8), mode, "Toolbox",
                "; ".join(f'{g["group"]}: {", ".join(g["items"])}' for g in groups) + ".")
    c = cv.c
    card_frame(cv)
    cv.put(20, 34, "From the gate level up", 15, "PS", 600, "text")
    n = sum(len(g["items"]) for g in groups)
    cv.put(20, 54, f"{n} languages, tools and domains in {len(groups)} groups", 11, "PM", 400, "text3")
    for gi, (label, gy, rows) in enumerate(layout):
        if gi:
            cv.add(f'<line x1="20" y1="{gy - 9.5}" x2="840" y2="{gy - 9.5}" stroke="{c["line"]}"/>')
        cv.put(20, gy + 17, label.upper(), 10, "PM", 600, "text3", spacing=1.3)
        for r, row in enumerate(rows):
            ry = gy + r * (chip_h + row_gap)
            for x, item, cw in row:
                cv.add(f'<rect x="{x:.1f}" y="{ry + 0.5}" width="{cw:.1f}" height="{chip_h}" rx="6" '
                       f'fill="{c["bg2"]}" fill-opacity="0.5" stroke="{c["line"]}"/>')
                cv.put(x + 10, ry + 17.5, item, 12, "PM", 400, "text2")
    return cv


# --------------------------------------------------------------------------
# README blocks


def picture(name: str, alt: str, width_attr: str = "100%", height: str | None = None) -> str:
    base = CFG["assets"]
    size = f' height="{height}"' if height else f' width="{width_attr}"'
    return (f'<picture><source media="(prefers-color-scheme: dark)" srcset="{base}{name}-dark.svg">'
            f'<img alt="{esc(alt)}" src="{base}{name}-light.svg"{size}></picture>')


def md_text(s: str) -> str:
    return s.replace("<", "&lt;").replace(">", "&gt;").replace("[", "\\[").replace("]", "\\]")


def blocks(a: dict) -> dict[str, str]:
    out = {}

    cards = []
    for p in CFG["projects"]:
        r = a["by_name"].get(p["repo"])
        url = (r or {}).get("url") or f'https://github.com/{CFG["login"]}/{p["repo"]}'
        desc = (r or {}).get("description") or ""
        alt = f'{p["repo"]}: {desc}'
        cards.append(f'<a href="{url}">{picture("project-" + p["repo"].lower(), alt, "49%")}</a>')
    out["projects"] = "\n".join(
        '<p align="center">\n' + "\n".join(cards[i:i + 2]) + "\n</p>" for i in range(0, len(cards), 2))

    covers = []
    for b in CFG["books"]:
        n = a["counts"].get(b["category"], 0)
        alt = f'{b["title"]}, {n} {b["unit"]}'
        covers.append(f'<a href="{b["url"]}">{picture("book-" + b["motif"], alt, "19%")}</a>')
    out["books"] = '<p align="center">\n' + "\n".join(covers) + "\n</p>"

    # The newest item from each book, newest first: one busy day on one book
    # would otherwise fill the whole list.
    newest: dict[str, dict] = {}
    for it in sorted(a["feed"], key=lambda it: it["when"], reverse=True):
        newest.setdefault(it["category"], it)
    latest = sorted(newest.values(), key=lambda it: it["when"], reverse=True)[:6]
    out["feed"] = "\n".join(
        f'- [{md_text(it["title"])}]({it["link"]}) · {md_text("Blog" if it["category"] == "blog" else it["category"])}'
        f' · {fmt_date(it["when"].date())}' for it in latest)

    langs_alt = "Share of code by language across public repositories: " + ", ".join(
        f"{k} {v * 100:.0f}%" for k, v in a["shares"]) + "."
    out["activity"] = (
        '<p align="center">\n'
        f'{picture("stats", "GitHub activity over the last 12 months: contributions, commits, pull requests, issues, repositories and stars.", "49%")}\n'
        f'{picture("languages", langs_alt, "49%")}\n'
        "</p>\n"
        '<p align="center">\n'
        f'{picture("activity", "Area chart of contributions per week over the last year, with the peak week labelled and streak figures beneath.")}\n'
        "</p>\n"
        '<p align="center">\n'
        f'{picture("snake", "The contribution calendar, eaten by a snake.")}\n'
        "</p>")

    # Written from profile.json, so the text a screen reader hears always
    # holds every group and item the card draws.
    out["toolbox"] = picture("toolbox", "Toolbox. " + " ".join(
        f'{g["group"]}: {", ".join(g["items"])}.' for g in CFG["toolbox"]), "100%")

    out["prs"] = "\n".join(
        f'- [{p["repository"]["nameWithOwner"]}#{p["number"]}]({p["url"]}): {md_text(p["title"])}'
        f' · merged {fmt_date(dt.date.fromisoformat(p["mergedAt"][:10]))}' for p in a["prs"][:8]
    ) or "- None yet."
    return out


def apply_blocks(text: str, new: dict[str, str]) -> str:
    for key, body in new.items():
        pat = re.compile(rf"(<!-- {key}:start -->\n)(?:.*?\n)?(<!-- {key}:end -->)", re.S)
        if not pat.search(text):
            raise SystemExit(f"README.md has no {key} markers")
        text = pat.sub(lambda m: m.group(1) + body + "\n" + m.group(2), text)
    return text


# --------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true", help="exit 1 if README.md would change")
    args = ap.parse_args()
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("GITHUB_TOKEN is not set", file=sys.stderr)
        return 2

    a = analyse(fetch_github(token), fetch_feed())
    entries = [fill_counts(e, a["counts"]) for e in CFG["now"]]

    cards: dict[str, callable] = {
        "hero": lambda m: hero(m),
        "now": lambda m: now_pill(m, entries),
        "stats": lambda m: stats_card(m, a),
        "languages": lambda m: langs_card(m, a),
        "activity": lambda m: activity_card(m, a),
        "toolbox": lambda m: toolbox_card(m),
    }
    for link in CFG["links"]:
        cards[f"link-{link['id']}"] = lambda m, l=link: link_pill(m, l)
    rows = max([3] + [len(wrap(project_desc(p, a["by_name"].get(p["repo"])), DESC_W, DESC_PT))
                      for p in CFG["projects"]])
    for p in CFG["projects"]:
        cards[f"project-{p['repo'].lower()}"] = lambda m, p=p: project_card(m, p, a["by_name"].get(p["repo"]), rows)
    for b in CFG["books"]:
        cards[f"book-{b['motif']}"] = lambda m, b=b: book_cover(m, b, a["counts"].get(b["category"], 0))

    DIST.mkdir(exist_ok=True)
    total = 0
    for name, make in cards.items():
        for mode in MODES:
            svg = make(mode).render()
            (DIST / f"{name}-{mode}.svg").write_text(svg, encoding="utf-8")
            total += len(svg)
    print(f"wrote {len(cards) * 2} SVGs to dist/, {total / 1024:.0f} KiB in all")

    old = README.read_text(encoding="utf-8")
    new = apply_blocks(old, blocks(a))
    if new != old:
        if args.check:
            print("README.md is out of date")
            return 1
        README.write_text(new, encoding="utf-8")
        print("README.md updated")
    else:
        print("README.md unchanged")
    return 0


if __name__ == "__main__":
    sys.exit(main())
