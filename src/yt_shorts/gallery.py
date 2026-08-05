"""Builds an HTML page on which all drafts are visible side by side."""

from __future__ import annotations

from html import escape

HEAD = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>{title}</title>
<style>
 body{{background:#0d1f1a;color:#fff;font:16px/1.4 system-ui,sans-serif;margin:24px}}
 h1{{font-size:20px;margin:0 0 20px}}
 .grid{{display:flex;flex-wrap:wrap;gap:20px}}
 figure{{margin:0;width:270px}}
 video{{width:270px;height:480px;background:#000;border-radius:8px}}
 figcaption{{margin-top:8px;font-size:14px;color:#B8F5CA;word-break:break-word}}
</style></head><body>
<h1>{title} &mdash; {count} drafts</h1>
<div class="grid">
"""

FOOT = """</div></body></html>
"""


def build_page(entries: list[dict], title: str) -> str:
    parts = [HEAD.format(title=escape(title), count=len(entries))]
    for e in entries:
        file = escape(e["file"])
        parts.append(
            f'<figure><video src="{file}" controls preload="metadata"></video>'
            f'<figcaption>{escape(e.get("hook", ""))}</figcaption></figure>\n'
        )
    parts.append(FOOT)
    return "".join(parts)
