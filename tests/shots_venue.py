#!/usr/bin/env python3
"""Screenshot the card venue line for several reference types."""
import asyncio
import sys
from bib.app import BibApp, compose_venue

OUT = "/tmp/pgv"


async def land(pilot, app, ref):
    for _ in range(len(app.library) + 2):
        n = app._selected_node()
        if n and n.ref == ref:
            return n
        await pilot.press("j")
        await pilot.pause()
    return None


async def main() -> None:
    app = BibApp()
    async with app.run_test(size=(150, 46)) as pilot:
        await pilot.pause()
        for ref, tag in [("Guttman1984", "proc"), ("Anselin1995", "article"),
                         ("McHarg1969", "book"), ("Tomlinson1968", "symp")]:
            n = await land(pilot, app, ref)
            if n:
                print(f"{ref}: {compose_venue(n)!r}", file=sys.stderr)
            await asyncio.sleep(1.2)      # allow lazy about/abstract
            await pilot.pause()
            app.save_screenshot(f"{OUT}_{tag}.svg")
    print("wrote", OUT + "_*.svg", file=sys.stderr)


asyncio.run(main())
