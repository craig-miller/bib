#!/usr/bin/env python3
"""Screenshot harness for Phase-1 live grey-node fetch. Walks into a library paper's
citations (grey rows read from the sidecar), then promotes into a GREY child — which has
no sidecar — so the background worker resolves it on OpenAlex and streams neighbors in.
Real network from zentoo; we sleep to let pages arrive."""
import asyncio
import sys

from bib.app import BibApp

OUT = "/tmp/pgl"


async def land_on(pilot, app, ref):
    for _ in range(len(app.library) + 2):
        n = app._selected_node()
        if n and n.ref == ref:
            return True
        await pilot.press("down")
        await pilot.pause()
    return False


async def main() -> None:
    app = BibApp()
    async with app.run_test(size=(170, 46)) as pilot:
        await pilot.pause()

        # pick any library paper that actually has a cited-by sidecar
        subject = next((n.ref for n in app.library
                        if n.doc is not None and papis_has_citedby(n)), None)
        if subject is None:
            print("no library paper has a cited-by sidecar", file=sys.stderr)
            return
        await land_on(pilot, app, subject)
        await pilot.press("ctrl+c")          # citations of subject → grey rows
        await pilot.pause()
        app.save_screenshot(f"{OUT}_1_citations.svg")

        # find the first grey child that carries an OpenAlex id (fetchable), select it
        rows = app._rows
        gi = next((i for i, n in enumerate(rows)
                   if not n.in_library and n.ids.get("openalex_id")), None)
        if gi is None:
            print("no fetchable grey child", file=sys.stderr)
            return
        for _ in range(gi):
            await pilot.press("down")
        await pilot.pause()
        print(f"grey child: {rows[gi].label!r} cited_by={rows[gi].citation_count} "
              f"oa={rows[gi].ids.get('openalex_id')}", file=sys.stderr)

        # promote into the GREY child's citations → live fetch
        await pilot.press("ctrl+c")
        await pilot.pause()
        app.save_screenshot(f"{OUT}_2_fetch_start.svg")
        for i in range(6):                    # let pages stream in
            await asyncio.sleep(1.0)
            await pilot.pause()
        app.save_screenshot(f"{OUT}_3_fetch_streamed.svg")
        f = app._current()
        print(f"after stream: title={f.title!r} nodes={len(f.nodes)} "
              f"loading={f.loading}", file=sys.stderr)

        # back out to the subject's citations
        await pilot.press("ctrl+o")
        await pilot.pause()
        app.save_screenshot(f"{OUT}_4_back.svg")

    print("wrote", OUT + "_*.svg", file=sys.stderr)


def papis_has_citedby(n) -> bool:
    import papis.citations
    try:
        return papis.citations.has_cited_by(n.doc)
    except Exception:
        return False


asyncio.run(main())
