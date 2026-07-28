#!/usr/bin/env python3
"""Screenshot harness for topics+keywords in the card and the `topic` search mode."""
import asyncio
import sys

from bib.app import BibApp

OUT = "/tmp/pga"


async def main() -> None:
    app = BibApp()
    async with app.run_test(size=(170, 46)) as pilot:
        await pilot.pause()
        # land on Egenhofer1991 (library, has DOI) and wait for lazy topics/keywords
        for _ in range(len(app.library) + 2):
            n = app._selected_node()
            if n and n.ref == "Egenhofer1991":
                break
            await pilot.press("down")
            await pilot.pause()
        await asyncio.sleep(1.5)          # let _load_about fetch + re-render the card
        await pilot.pause()
        n = app._selected_node()
        print(f"card node: {n.ref} topics={n.topics} keywords={n.keywords}", file=sys.stderr)
        app.save_screenshot(f"{OUT}_1_card.svg")

        # topic search
        await pilot.press("ctrl+underscore")
        await pilot.pause()
        for ch in "topic geographic information systems":
            await pilot.press(ch if ch != " " else "space")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        await asyncio.sleep(3.0)
        await pilot.pause()
        app.save_screenshot(f"{OUT}_2_topic_search.svg")
        f = app._current()
        print(f"topic search -> {f.title!r} nodes={len(f.nodes)} total={f.total}",
              file=sys.stderr)

    print("wrote", OUT + "_*.svg", file=sys.stderr)


asyncio.run(main())
