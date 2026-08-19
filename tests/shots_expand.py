import asyncio, sys
from bib.app import BibApp
async def main():
    app = BibApp()
    async with app.run_test(size=(150, 46)) as pilot:
        await pilot.pause()
        for _ in range(len(app.library)):
            n = app._selected_node()
            if n and n.ref == "Anselin1995":
                break
            await pilot.press("j"); await pilot.pause()
        for _ in range(20):
            await pilot.pause(0.25)
            if app._selected_node().keywords is not None:
                break
        await pilot.press("z")
        await pilot.pause(0.4)
        print("expanded:", app._details_expanded,
              "center hidden:", app.query_one("#center").has_class("hidden"), file=sys.stderr)
        app.save_screenshot("/tmp/pg_expand.svg")
        await pilot.press("z")
        await pilot.pause(0.4)
        print("after toggle back, expanded:", app._details_expanded, file=sys.stderr)
        app.save_screenshot("/tmp/pg_contract.svg")
asyncio.run(main())
