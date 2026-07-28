import asyncio, sys
from bib.app import BibApp
async def main():
    app = BibApp()
    async with app.run_test(size=(150, 46)) as pilot:
        await pilot.pause()
        target = sys.argv[1] if len(sys.argv) > 1 else "Anselin1995"
        for _ in range(len(app.library)):
            n = app._selected_node()
            if n and n.ref == target:
                break
            await pilot.press("down"); await pilot.pause()
        n = app._selected_node()
        print("on:", n.ref, "abs_len:", len(n.abstract or ""), file=sys.stderr)
        # let _load_about fetch topics/keywords (abstract already from info.yaml)
        for _ in range(20):
            await pilot.pause(0.25)
            if n.keywords is not None:
                break
        await pilot.pause(0.3)
        app.save_screenshot("/tmp/pg_card.svg")
        print("kw:", n.keywords, "topics:", n.topics, file=sys.stderr)
asyncio.run(main())
