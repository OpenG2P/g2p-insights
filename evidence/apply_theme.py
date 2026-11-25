#!/usr/bin/env python3
"""Inject the platform's maps theme into every page, replacing any inline copy.

WHY
---
The two-column layout, the brand bar and the type are how an OpenG2P map page
LOOKS. That is a platform decision — every registry's map should look the same,
and a registry author should not have to know the CSS exists.

It used to live as a <style> block at the top of each registry's index.md,
copied by hand from whichever page was copied first. That failed exactly the way
copied things fail: the Farmer Registry's page used `class="split"` in all three
places but carried no stylesheet, so the two-column layout it was written for
silently fell back to the stacked one it was meant to replace. Nothing errored.
Nobody could have spotted it except by looking at the rendered page.

So the theme ships with the toolchain and is spliced in at build time, after the
registry's own content has been copied into place. A page that already carries an
inline block has it REPLACED, so an old page picks up the current theme on its
next build rather than pinning whatever was current when it was written.

Run by build-into, after the content overlay and before the Evidence build.
"""

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
THEME = os.path.join(HERE, "maps-theme.css")
PAGES = os.path.join(HERE, "pages")

START = "<!-- OPENG2P-MAPS-THEME:START — injected by apply_theme.py, do not edit -->"
END = "<!-- OPENG2P-MAPS-THEME:END -->"

# A page's own <style> block, from when the theme was copied per registry.
INLINE = re.compile(r"\n?<style>\n.*?\n</style>\n?", re.S)
BLOCK = re.compile(re.escape(START) + r".*?" + re.escape(END) + r"\n?", re.S)
# YAML frontmatter, which must stay first: the theme goes after it.
FRONTMATTER = re.compile(r"\A(---\n.*?\n---\n)", re.S)


def themed(css: str) -> str:
    return f"{START}\n<style>\n{css}\n</style>\n{END}\n"


def apply_to(path: str, css: str) -> str:
    page = open(path).read()

    # Drop any previous injection AND any hand-written block. Replacing rather
    # than skipping is the point: a page written against an older theme should
    # follow the current one, not freeze at the version it was authored with.
    page, n_prev = BLOCK.subn("", page)
    page, n_inline = INLINE.subn("\n", page)

    m = FRONTMATTER.match(page)
    if m:
        page = m.group(1) + "\n" + themed(css) + page[m.end():].lstrip("\n")
    else:
        page = themed(css) + page

    with open(path, "w") as fh:
        fh.write(page)
    return ("refreshed" if n_prev else
            "replaced an inline copy" if n_inline else
            "added")


def main() -> int:
    if not os.path.isfile(THEME):
        print(f"[theme] no {THEME} — leaving pages alone", file=sys.stderr)
        return 0
    css = open(THEME).read().rstrip("\n")

    pages = [os.path.join(PAGES, f) for f in sorted(os.listdir(PAGES))
             if f.endswith(".md")] if os.path.isdir(PAGES) else []
    if not pages:
        print("[theme] no pages to theme", file=sys.stderr)
        return 0

    for p in pages:
        print(f"[theme] {os.path.basename(p)}: {apply_to(p, css)}")

    # A page that positions with .split and never got the stylesheet is the exact
    # failure this script exists to prevent, so prove it did not happen rather
    # than assume it.
    for p in pages:
        body = open(p).read()
        if 'class="split"' in body and START not in body:
            print(f"[theme] ERROR: {os.path.basename(p)} uses .split but has no "
                  "theme block", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
