#!/usr/bin/env python3
"""Screenshot the ctrl-s CSL toggle: same paper, plain head vs CSL reference head."""
import asyncio
import sys

from bib.app import BibApp

OUT = "/tmp/pgcsl"


async def land(pilot, app, ref):
    for _ in range(len(app.library) + 1):
        n = app._selected_node()
        if n and n.ref == ref:
            return
        await pilot.press("down")
        await pilot.pause()


async def main() -> None:
    app = BibApp()
    async with app.run_test(size=(170, 46)) as pilot:
        await pilot.pause()
        await land(pilot, app, "Anselin1995")
        app.save_screenshot(f"{OUT}_1_plain.svg")

        await pilot.press("ctrl+s")             # → CSL mode
        await pilot.pause()
        app.save_screenshot(f"{OUT}_2_csl.svg")

        # a book, in CSL mode, to see a different type
        await land(pilot, app, "Tomlin1990")
        app.save_screenshot(f"{OUT}_3_csl_book.svg")

        await pilot.press("ctrl+s")             # back to plain (verify toggle-off)
        await pilot.pause()
        app.save_screenshot(f"{OUT}_4_plain_again.svg")

    print("wrote", OUT + "_*.svg", file=sys.stderr)


asyncio.run(main())
