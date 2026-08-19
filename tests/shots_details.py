import asyncio, sys
from bib.app import BibApp
async def main():
    app = BibApp()
    async with app.run_test(size=(150, 46)) as pilot:
        await pilot.pause()
        await pilot.press("z"); await pilot.pause(0.4)
        print("z expanded:", app._details_expanded,
              "center hidden:", app.query_one("#center").has_class("hidden"), file=sys.stderr)
        await pilot.press("z"); await pilot.pause(0.4)
        print("toggle back:", app._details_expanded, file=sys.stderr)
        # help popup shows the new label
        await pilot.press("question_mark"); await pilot.pause(0.3)
        app.save_screenshot("/tmp/pg_help.svg")
asyncio.run(main())
