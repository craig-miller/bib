#!/usr/bin/env python3
"""Screenshot the 'S' style picker + a non-default (IEEE) CSL card."""
import asyncio
import sys
from bib.app import BibApp

OUT = "/tmp/pgstyle"


async def land(pilot, app, ref):
    for _ in range(len(app.library) + 1):
        n = app._selected_node()
        if n and n.ref == ref:
            return
        await pilot.press("j")
        await pilot.pause()


async def main() -> None:
    app = BibApp()
    async with app.run_test(size=(170, 46)) as pilot:
        await pilot.pause()
        await land(pilot, app, "Anselin1995")

        await pilot.press("S")                       # open picker
        await pilot.pause()
        app.save_screenshot(f"{OUT}_1_picker.svg")

        for ch in "ieee":                           # filter
            await pilot.press(ch)
        await pilot.pause()
        app.save_screenshot(f"{OUT}_2_filtered.svg")

        await pilot.press("enter")                  # select IEEE
        await pilot.pause()
        app.save_screenshot(f"{OUT}_3_ieee_card.svg")

    print("wrote", OUT + "_*.svg", file=sys.stderr)


asyncio.run(main())
