import asyncio, sys
from papers.app import PapersApp
async def main():
    app = PapersApp()
    async with app.run_test(size=(150, 30)) as pilot:
        await pilot.pause()
        n = app._selected_node()
        print("row0:", n.ref, "in_lib:", n.in_library, file=sys.stderr)
        await pilot.press("enter")
        # wait for the modal to appear + discovery to settle
        for _ in range(60):
            await pilot.pause(0.25)
            if len(app.screen_stack) > 1:
                scr = app.screen
                cands = getattr(scr, "_cands", None)
                if cands is not None and (not cands or all(c.get("ok") is not None for c in cands)):
                    break
        print("screen_stack:", len(app.screen_stack), "top:", type(app.screen).__name__,
              "cands:", len(getattr(app.screen,'_cands',[])), file=sys.stderr)
        app.save_screenshot("/tmp/pg_enter.svg")
        await pilot.press("escape")
        await pilot.pause()
        print("after esc, stack:", len(app.screen_stack), file=sys.stderr)
asyncio.run(main())
