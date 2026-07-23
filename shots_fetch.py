#!/usr/bin/env python3
"""Screenshot the PDF picker (FetchScreen): discovery + live %PDF verify."""
import asyncio, sys
from papers import GraphApp, FetchScreen, Node

OUT = "/tmp/pg_fetch"

CASES = [
    ("biorxiv", Node(title="A SARS-CoV-2 protein interaction map reveals targets for drug repurposing",
                     year=2020, author="Gordon, David E", doi="10.1101/2020.03.22.002386")),
    ("closed",  Node(title="Local indicators of spatial association LISA",
                     year=1995, author="Anselin, Luc", doi="10.1111/j.1538-4632.1995.tb00338.x")),
]

async def settle(app, pilot, timeout=80):
    for _ in range(timeout):
        await pilot.pause(0.25)
        scr = app.screen
        cands = getattr(scr, "_cands", None)
        if cands and all(c.get("ok") is not None for c in cands):
            await pilot.pause(0.2)
            return True
    return False

async def main():
    app = GraphApp()
    async with app.run_test(size=(150, 30)) as pilot:
        await pilot.pause()
        for name, node in CASES:
            await app.push_screen(FetchScreen(node))
            ok = await settle(app, pilot)
            app.save_screenshot(f"{OUT}_{name}.svg")
            print(f"{name}: settled={ok} cands={len(getattr(app.screen,'_cands',[]))}", file=sys.stderr)
            app.pop_screen()
            await pilot.pause()
    print("done", file=sys.stderr)

asyncio.run(main())
