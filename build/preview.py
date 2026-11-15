#!/usr/bin/env python3
"""Render README.md the way GitHub does and write dist/index.html to look at it locally.

GitHub's own Markdown endpoint does the rendering, through the gh CLI, and the
asset links are pointed at the local dist/ folder instead of the output branch.
The page follows prefers-color-scheme, as a profile does, so both themes can be
checked by switching the browser's scheme. The snake only exists once the
workflow has run, and math shows as TeX because GitHub renders it client-side.
"""
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CFG = json.loads((ROOT / "build" / "profile.json").read_text(encoding="utf-8"))

PICTURE = re.compile(
    r'(<picture>(?:(?!</picture>).)*?)\s*(</picture></themed-picture>(?:</a>)?)\s*'
    r'<a target="_blank"[^>]*>(<img[^>]*>)</a>',
    re.S,
)
# An image with no link of its own stays inside the <picture>, but wrapped in one.
WRAPPED = re.compile(r'(<source[^>]*>)\s*<a target="_blank"[^>]*>(<img[^>]*>)</a>')

CSS = """
:root{color-scheme:light dark;--bg:#ffffff;--fg:#1f2328;--muted:#59636e;--border:#d1d9e0;--link:#0969da;--box:#ffffff}
@media (prefers-color-scheme:dark){:root{--bg:#0d1117;--fg:#f0f6fc;--muted:#9198a1;--border:#3d444d;--link:#4493f8;--box:#0d1117}}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans",Helvetica,Arial,sans-serif}
.wrap{max-width:896px;margin:24px auto;padding:0 16px}
.box{border:1px solid var(--border);border-radius:6px;padding:24px;background:var(--box)}
.cap{font:12px ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--muted);margin:0 0 8px}
a{color:var(--link);text-decoration:none}
h3{font-size:1.25em;font-weight:600;margin:24px 0 16px;line-height:1.25}
p,ul,details{margin:0 0 16px}
ul{padding-left:2em}
img{max-width:100%;box-sizing:content-box;vertical-align:middle}
sub{color:var(--muted)}
.markdown-heading .anchor{display:none}
"""


def main() -> int:
    md = (ROOT / "README.md").read_text(encoding="utf-8")
    body = json.dumps({"text": md, "mode": "markdown", "context": f'{CFG["login"]}/{CFG["login"]}'})
    out = subprocess.run(["gh", "api", "markdown", "--input", "-"], input=body, capture_output=True, text=True)
    if out.returncode:
        print(out.stderr.strip(), file=sys.stderr)
        return 1
    rendered = out.stdout.replace(CFG["assets"], "./")
    # The endpoint lifts each <img> out of its <picture> into a link of its own,
    # and github.com's themed-picture script puts it back on the page. Do that
    # here so the dark source is honoured the way it is on the real profile.
    rendered = PICTURE.sub(r"\1\3\2", rendered)
    rendered = WRAPPED.sub(r"\1\2", rendered)
    page = (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width,initial-scale=1"><title>Profile preview</title>'
        f"<style>{CSS}</style></head><body><div class=wrap>"
        f'<p class=cap>{CFG["login"]} / README.md</p><div class=box>{rendered}</div></div></body></html>'
    )
    (ROOT / "dist" / "index.html").write_text(page, encoding="utf-8")
    print("wrote dist/index.html")
    return 0


if __name__ == "__main__":
    sys.exit(main())
