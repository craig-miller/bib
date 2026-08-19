#!/usr/bin/env python3
"""Screenshot harness for the Find-papers popup. Opens `f`, types a query, submits,
and lets the background worker query OpenAlex (whole corpus) → grey results Frame."""
import asyncio
import sys

from bib.app import BibApp

OUT = "/tmp/pgs"


async def do_search(pilot, app, text, tag, settle=3.0):
    await pilot.press("f")                    # opens the Find-papers popup
    await pilot.pause()
    for ch in text:
        await pilot.press(ch if ch != " " else "space")
    await pilot.pause()
    app.save_screenshot(f"{OUT}_{tag}_typed.svg")
    await pilot.press("enter")
    await pilot.pause()
    await asyncio.sleep(settle)
    await pilot.pause()
    app.save_screenshot(f"{OUT}_{tag}_results.svg")
    f = app._current()
    print(f"[{tag}] {text!r} -> title={f.title!r} nodes={len(f.nodes)} "
          f"total={f.total} loading={f.loading}", file=sys.stderr)
    await pilot.press("ctrl+o")              # back to library between searches
    await pilot.pause()


async def main() -> None:
    app = BibApp()
    async with app.run_test(size=(170, 46)) as pilot:
        await pilot.pause()
        await do_search(pilot, app, "author max egenhofer", "author")
        await do_search(pilot, app, "topological relations between regions", "title")
        await do_search(pilot, app, "keyword quadtree | raster", "keyword")
    print("wrote", OUT + "_*.svg", file=sys.stderr)


asyncio.run(main())
