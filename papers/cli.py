"""Console entry points — wired up in pyproject's [project.scripts].

Imports are deferred into the functions so `--help`/version and shell completion stay fast and
don't drag in Textual/papis until a command actually runs.
"""
from __future__ import annotations


def main() -> None:
    """`papers` — launch the citation-graph TUI."""
    from .app import PapersApp
    PapersApp().run()


def fetch_main() -> None:
    """`papers-fetch` — headless PDF fetch + metadata enrich for the library (cron-friendly)."""
    from .fetch import main as _fetch
    _fetch()
