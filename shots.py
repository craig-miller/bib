#!/usr/bin/env python3
"""Headless screenshot harness for papers — drives the app via the Textual
pilot and exports SVG snapshots at key UI states. No live terminal needed."""
import asyncio
import sys

from papers import GraphApp

OUT = "/tmp/pg"


async def main() -> None:
    app = GraphApp()
    async with app.run_test(size=(170, 46)) as pilot:
        await pilot.pause()

        # 1. home — library list, top card + columns for the highlighted paper
        app.save_screenshot(f"{OUT}_1_home.svg")

        # move the cursor down a few rows to a paper that HAS sidecars (Egenhofer)
        for _ in range(len(app.library)):
            n = app._selected_node()
            if n and n.ref == "Egenhofer1991":
                break
            await pilot.press("down")
            await pilot.pause()
        app.save_screenshot(f"{OUT}_2_egenhofer.svg")

        # 2b. fuzzy-filter by typing
        for ch in "topolog":
            await pilot.press(ch)
        await pilot.pause()
        app.save_screenshot(f"{OUT}_3_filter.svg")
        await pilot.press("escape")
        await pilot.pause()

        # re-land on Egenhofer, then promote cited-by → center (ctrl+l)
        for _ in range(len(app.library)):
            n = app._selected_node()
            if n and n.ref == "Egenhofer1991":
                break
            await pilot.press("down")
            await pilot.pause()
        await pilot.press("ctrl+l")
        await pilot.pause()
        app.save_screenshot(f"{OUT}_4_promote_citedby.svg")

        # back (ctrl+o) to home
        await pilot.press("ctrl+o")
        await pilot.pause()
        app.save_screenshot(f"{OUT}_5_back.svg")

    print("wrote SVGs to", OUT + "_*.svg", file=sys.stderr)


asyncio.run(main())
