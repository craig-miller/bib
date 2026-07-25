#!/usr/bin/env python3
"""papis graph — a citation-graph discovery TUI (Textual).

A top metadata card over a single full-width table. The table starts as your library
(Papers); the columns are `· year author title Cited Infl` (Infl = S2 influential
citation count). Selecting a row and pressing ctrl-c/ctrl-r replaces the table with that
paper's citations/references (read from the papis `cited-by.yaml`/`citations.yaml` sidecars);
a browser-style history stack walks in and out.

Navigation:
  ctrl-c  citations of selected → table     ctrl-o / esc  back
  ctrl-r  references of selected → table     ctrl-i        forward
  ctrl-p  Papers (home)                      ctrl-/        search whole corpus (popup)
  type    fuzzy-filter · #kw keyword         ctrl-shift-/  help popup
  enter   open (in-library+PDF) · fetch PDF · add+fetch (grey)   ctrl-q  quit

Grey rows = known-but-not-in-library (rendered dim). Promoting into a grey row has no
sidecar to read, so its citations/references are **fetched live** from OpenAlex on a
background worker and streamed into the table behind a "fetching…" indicator — this is
what lets you walk the graph past the edge of your own library. Grey/in-library is a
*render* state, computed by matching each row's DOI/title against the library.

Runs in the papis venv (needs `textual`, `rapidfuzz`, `httpx`, and `papis`).
"""
from __future__ import annotations

import asyncio
import functools
import importlib.resources
import json
import os
import subprocess
import tempfile

# NOTE: `PAPIS_NP=0` (disables papis's multiprocessing, which crashes under Textual — see
# papers/__init__.py for the full reason) is set in the package __init__, which runs before
# this module imports papis. Don't import papis above that guard.

from dataclasses import dataclass, field, fields
from typing import Any

from rich.text import Text

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Input, Static

import papis.config
import papis.database
import papis.citations
import papis.crossref
import papis.commands.add
import papis.commands.addto
import papis.commands.rm
import papis.document
from papis.document import Document

from . import core as gc

# Leading-column icon by reference type. Glyphs are Font Awesome 4.7 (Nerd Font PUA,
# \uFxxx) so they render in JetBrains Mono Nerd Font. Keyed by a canonical type; the
# synonym map folds the three vocabularies papis sees — biblatex (info.yaml), OpenAlex
# work types, and S2 publicationTypes — onto those keys.
TYPE_GLYPHS = {
    "article":     "",   # file-text-o  — journal article / paper
    "conference":  "",   # users        — proceedings / conference
    "book":        "",   # book
    "chapter":     "",   # bookmark     — book chapter / section
    "thesis":      "",   # graduation-cap
    "report":      "",   # file         — report / techreport / standard
    "dataset":     "",   # database
    "software":    "",   # code
    "video":       "",   # film
    "audio":       "",   # music
    "online":      "",   # globe        — webpage / electronic
    "patent":      "",   # lightbulb-o
    "preprint":    "",   # file-text    — preprint / posted-content
    "review":      "",   # comments     — review / peer-review
    "reference":   "",   # book         — reference / encyclopedia entry
    "manual":      "",   # book
    "unpublished": "",   # file-o       — manuscript / unpublished
    "default":     "",   # file-o
}

_TYPE_SYNONYMS = {
    "journal-article": "article", "journalarticle": "article", "article-journal": "article",
    "paper": "article", "news-article": "article", "newspaper-article": "article",
    "letter": "article", "editorial": "article",
    "proceedings-article": "conference", "inproceedings": "conference",
    "conference-paper": "conference", "proceedings": "conference", "conference": "conference",
    "monograph": "book", "reference-book": "book", "edited-book": "book",
    "book-chapter": "chapter", "inbook": "chapter", "incollection": "chapter",
    "booksection": "chapter", "book-section": "chapter", "book-part": "chapter",
    "phdthesis": "thesis", "mastersthesis": "thesis", "masters-thesis": "thesis",
    "phd-thesis": "thesis", "dissertation": "thesis",
    "techreport": "report", "tech-report": "report", "standard": "report",
    "working-paper": "report", "grant": "report",
    "data": "dataset",
    "computer-program": "software",
    "movie": "video", "audiovisual": "video", "motion-picture": "video",
    "music": "audio", "sound-recording": "audio", "song": "audio",
    "webpage": "online", "electronic": "online", "website": "online", "www": "online",
    "web-resource": "online",
    "posted-content": "preprint",
    "peer-review": "review", "book-review": "review",
    "reference-entry": "reference", "entry": "reference",
    "manuscript": "unpublished",
}


def canonical_type(t: Any) -> str:
    key = str(t or "").strip().lower().replace("_", "-").replace(" ", "-")
    return _TYPE_SYNONYMS.get(key, key)


def type_glyph(t: Any) -> str:
    return TYPE_GLYPHS.get(canonical_type(t), TYPE_GLYPHS["default"])


def compose_venue(n: "Node") -> str:
    """The bibliographic 'published in' line. Container = journal (articles) or `In
    <booktitle/proceedings>` (chapter/conference) or `series` (a monograph in a series),
    with volume/issue/pages and an editor; then the thesis/report granting body; then
    publisher (+ edition for books, + address). Composed from whatever fields the node
    carries; blank if none."""
    ty = canonical_type(n.type)
    issue = n.number or n.issue
    seg: list[str] = []
    container = n.venue or (n.series if ty in ("book", "report", "chapter") else None)
    if container:
        s = ("In " if ty in ("chapter", "conference") else "") + str(container)
        if n.volume:
            s += f" {n.volume}"
        if issue:
            s += f"({issue})"
        if n.pages:
            s += f", {str(n.pages).replace('--', '–')}"
        if n.editor:
            s += f" (ed. {primary_family(str(n.editor))})"
        seg.append(s)
    org = n.institution or n.school
    if ty in ("thesis", "report") and org:
        seg.append(str(org))
    if n.publisher:
        pub = str(n.publisher)
        if n.edition and ty == "book":
            pub = f"{pub}, {n.edition} ed."
        if n.address:
            pub = f"{pub}, {n.address}"
        if not container or pub.split(",")[0].lower() not in str(container).lower():
            seg.append(pub)
    elif n.address and not container:
        seg.append(str(n.address))
    return "  ·  ".join(seg)


def primary_family(author: str) -> str:
    """Primary author's family name from a flat author string. Handles three shapes:
    'Family, Given …' (comma), 'FAMILY I. I.' (Crossref: family first + initials), and
    'Given … Family' (family last)."""
    if not author:
        return ""
    first = author.split(" and ")[0].strip()
    if "," in first:
        return first.split(",")[0].strip()
    parts = first.split()
    if len(parts) == 1:
        return parts[0]
    # trailing tokens all initials ("CHANG S. K." / "Egenhofer M.") → family is FIRST;
    # otherwise ("Given … Family") → family is last.
    is_initial = lambda p: len(p.replace(".", "")) <= 1
    return parts[0] if all(is_initial(p) for p in parts[1:]) else parts[-1]


def fmt_count(v: Any) -> str:
    """Thousands-separated count, blank for None / non-numeric."""
    try:
        return f"{int(v):,}"
    except (TypeError, ValueError):
        return ""


SEARCH_KINDS = ("doi", "author", "topic", "keyword", "title")


def parse_query(raw: str) -> tuple[str, str] | None:
    """(kind, query) from a command-line. Leading `doi `/`author `/`keyword `/`title `
    picks the kind (also `doi:` prefix); bare text = title. None for empty input."""
    raw = raw.strip()
    if not raw:
        return None
    low = raw.lower()
    for kind in SEARCH_KINDS:
        if low.startswith(kind + " "):
            return kind, raw[len(kind) + 1:].strip()
    if low.startswith("doi:"):
        return "doi", raw[4:].strip()
    return "title", raw


# --------------------------------------------------------------------------- #
# Enter (open / fetch / add) helpers                                           #
# --------------------------------------------------------------------------- #
def _first_pdf(doc: Document | None) -> str | None:
    """First attached PDF path on a library doc, or None."""
    if doc is None:
        return None
    for f in doc.get_files():
        if f.lower().endswith(".pdf"):
            return f
    return None


def _is_downloaded(doc: Document | None) -> bool:
    """Is the paper's content actually on disk? True when the entry has at least one attached
    file (PDF/video/…) that exists — the signal for the leading-glyph colour. Distinct from
    `in_library` (that's 'is it a papis entry at all'); an entry can exist with no file yet."""
    return bool(doc is not None and any(os.path.exists(f) for f in doc.get_files()))


def _open_file(path: str) -> None:
    """Open a file with the papis-configured opener (zathura for PDFs via xdg-open), detached
    so the GUI viewer lives independently of the TUI — no terminal handoff needed."""
    opener = papis.config.get("opentool") or "xdg-open"
    subprocess.Popen([opener, path], start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# info.yaml keys that map straight through from a Node (DOI-less add fallback).
_NODE_DATA_KEYS = ("doi", "publisher", "volume", "number", "pages", "address",
                   "series", "editor", "institution", "school", "edition", "isbn")


def _node_to_data(n: Node) -> dict:
    """Build a papis entry dict from a grey Node's cached metadata — the DOI-less add path
    (books/reports OpenAlex/Crossref can't resolve). Venue lands as booktitle for book-like
    types, else journal."""
    data: dict[str, Any] = {"title": n.title, "type": n.type or "article"}
    if n.year:
        data["year"] = str(n.year)
    if n.author:
        data["author"] = n.author
        # papis's ref-format ({doc[author_list][0][family]}{doc[year]}) needs a STRUCTURED
        # author_list — with only the `author` string it can't build the key and falls back
        # to a title-based ref (e.g. "M_tree_AnEfficient…" instead of "Ciaccia1997"). Parse it.
        author_list = papis.document.split_authors_name(n.author)
        if author_list:
            data["author_list"] = author_list
    for k in _NODE_DATA_KEYS:
        v = getattr(n, k, None)
        if v:
            data[k] = v
    if n.venue:
        book_like = canonical_type(n.type) in ("book", "chapter")
        data["booktitle" if book_like else "journal"] = n.venue
    return {k: v for k, v in data.items() if v}


# --------------------------------------------------------------------------- #
# node model — one uniform shape for library papers AND grey neighbors         #
# --------------------------------------------------------------------------- #
@dataclass
class Node:
    title: str = ""
    year: Any = None
    author: str = ""
    doi: str | None = None
    citation_count: Any = None
    influential_count: Any = None           # S2 influentialCitationCount (subject papers)
    is_influential: bool | None = None      # only meaningful for a cited-by edge
    type: str | None = None                  # biblatex / OpenAlex / S2 reference type
    ids: dict = field(default_factory=dict)  # s2_id / openalex_id / corpus_id …
    url: str | None = None
    # bibliographic venue (from info.yaml for library nodes, from OpenAlex for grey):
    venue: str | None = None                 # journal, or book/proceedings title
    publisher: str | None = None
    volume: str | None = None
    number: str | None = None                # journal issue (biblatex `number` …)
    issue: str | None = None                 # … or `issue` (some sources use this key)
    pages: str | None = None
    address: str | None = None
    series: str | None = None
    editor: str | None = None
    institution: str | None = None           # thesis / techreport granting body
    school: str | None = None
    edition: str | None = None
    isbn: str | None = None                   # shown in place of DOI for books
    # lazily fetched on highlight (None = not fetched yet, [] / "" = fetched/none):
    topics: list[str] | None = None
    keywords: list[str] | None = None
    abstract: str | None = None
    # resolved-against-library (a *render* state, not stored):
    in_library: bool = False
    downloaded: bool = False                 # content file(s) present on disk → coloured glyph
    ref: str | None = None
    doc: Document | None = None

    @property
    def label(self) -> str:
        return self.ref or self.title or self.doi or "?"

    @property
    def first_author(self) -> str:
        return primary_family(self.author)


def node_from_doc(doc: Document) -> Node:
    return Node(
        title=gc.strip_markup(doc.get("title")),
        year=doc.get("year"),
        author=str(doc.get("author", "")),
        doi=(str(doc["doi"]).lower() if doc.get("doi") else None),
        citation_count=(doc.get("s2_citation_count") or doc.get("cited_by_count")
                        or doc.get("citation_count")),
        influential_count=doc.get("s2_influential_citation_count"),
        type=doc.get("type"),
        ids={k: doc[k] for k in ("s2_id", "openalex_id", "corpus_id") if doc.get(k)},
        url=doc.get("url"),
        venue=(doc.get("journal") or doc.get("booktitle") or doc.get("venue")),
        publisher=doc.get("publisher"),
        volume=doc.get("volume"),
        number=doc.get("number"),
        issue=doc.get("issue"),
        pages=doc.get("pages"),
        address=doc.get("address"),
        series=doc.get("series"),
        editor=doc.get("editor"),
        institution=doc.get("institution"),
        school=doc.get("school"),
        edition=doc.get("edition"),
        isbn=doc.get("isbn"),
        # prefer the local abstract (Crossref JATS stripped); OA fills it if absent:
        abstract=(gc.strip_markup(doc.get("abstract")) or None),
        in_library=True,
        downloaded=_is_downloaded(doc),
        ref=doc.get("ref"),
        doc=doc,
    )


def node_from_citation(cit: dict, doi_index: dict[str, Document]) -> Node:
    doi = (str(cit["doi"]).lower() if cit.get("doi") else None)
    lib = doi_index.get(doi) if doi else None
    n = Node(
        title=gc.strip_markup(cit.get("title")),
        year=cit.get("year"),
        author=str(cit.get("author", "")),
        doi=doi,
        citation_count=cit.get("citation_count"),
        is_influential=cit.get("isInfluential"),
        type=cit.get("type"),
        ids={k: cit[k] for k in ("s2_id", "openalex_id", "corpus_id") if cit.get(k)},
        url=cit.get("url"),
    )
    if lib is not None:                       # this grey neighbor IS in the library
        n.in_library = True
        n.downloaded = _is_downloaded(lib)
        n.ref = lib.get("ref")
        n.doc = lib
    return n


# --------------------------------------------------------------------------- #
# a place in the browse history                                                #
# --------------------------------------------------------------------------- #
@dataclass(eq=False)                          # identity, not value, equality (stack `is`)
class Frame:
    title: str            # what this center list represents ("Papers", "citations …")
    nodes: list[Node]
    cursor: int = 0
    loading: bool = False  # a live fetch is still streaming rows into this frame
    total: int | None = None  # search: total corpus matches (we show the top slice)


# --------------------------------------------------------------------------- #
# help popup — floating shortcut list (ctrl-shift-/)                            #
# --------------------------------------------------------------------------- #
HELP_LINES = [
    ("^c", "Citations"),
    ("^r", "References"),
    ("^o, esc", "Back"),
    ("^i", "Forward"),
    ("^/", "Search"),
    ("^p", "Papers"),
    ("^d", "Expand Details"),
    ("^s", "CSL / plain toggle"),
    ("^y", "Pick CSL style"),
    ("^e", "Edit entry (info.yaml)"),
    ("⇧^d", "Delete entry"),
    ("enter", "open · fetch PDF · add"),
    ("^q", "Quit"),
]


class HelpScreen(ModalScreen):
    CSS = """
    HelpScreen { align: center middle; background: $background 55%; }
    #help-box { width: 34; height: auto; border: round $primary;
                background: $surface; padding: 1 2; }
    """
    BINDINGS = [
        Binding("escape", "dismiss", "close"),
        Binding("ctrl+question_mark", "dismiss", "close"),
        Binding("ctrl+shift+slash", "dismiss", "close"),
    ]

    def compose(self) -> ComposeResult:
        body = Text()
        body.append("Keyboard shortcuts\n\n", style="bold")
        for key, label in HELP_LINES:
            body.append(f"{key:8}", style="yellow")
            body.append(f"{label}\n")
        body.append("\nesc to close", style="dim")
        yield Static(body, id="help-box")


# --------------------------------------------------------------------------- #
# confirm dialog — a deliberate y/n gate for destructive actions               #
# --------------------------------------------------------------------------- #
class ConfirmScreen(ModalScreen[bool]):
    """A confirmation dialog: Enter confirms, Esc cancels. Returns True on confirm."""
    CSS = """
    ConfirmScreen { align: center middle; background: $background 60%; }
    #confirm-box { width: 74; height: auto; border: round $error;
                   background: $surface; padding: 1 2; }
    #confirm-title { text-style: bold; color: $error; }
    #confirm-detail { color: $text; }
    #confirm-hint { color: $text-muted; }
    """
    BINDINGS = [
        Binding("enter", "confirm", "confirm"),
        Binding("escape", "cancel", "cancel"),
    ]

    def __init__(self, title: str, detail: str = "") -> None:
        super().__init__()
        self._title = title
        self._detail = detail

    def compose(self) -> ComposeResult:
        # markup=False: titles can contain [brackets] and the hint literally shows "[y]" —
        # Rich would otherwise parse those as (broken) style tags and drop them.
        with Vertical(id="confirm-box"):
            yield Static(self._title, id="confirm-title", markup=False)
            if self._detail:
                yield Static(self._detail, id="confirm-detail", markup=False)
            yield Static("\n[enter] Delete   ·   [esc] Cancel",
                         id="confirm-hint", markup=False)

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


# --------------------------------------------------------------------------- #
# search popup — whole-corpus discovery command line (ctrl-/)                   #
# --------------------------------------------------------------------------- #
class SearchScreen(ModalScreen[str]):
    """A one-line command input; dismisses with the raw string (the app parses + fetches).
    Grammar hint sits under the box: doi / author / keyword a|b / bare = title."""
    CSS = """
    SearchScreen { align: center middle; background: $background 55%; }
    #search-box { width: 76; height: 7; border: round $primary;
                  background: $surface; padding: 0 1; }
    #search-title { color: $accent; }
    #search-hint  { color: $text-muted; }
    """
    BINDINGS = [Binding("escape", "dismiss", "close")]

    def compose(self) -> ComposeResult:
        with Vertical(id="search-box"):
            yield Static("Search — whole corpus (OpenAlex)", id="search-title")
            yield Input(placeholder="doi <doi> · author <name> · topic <name> · "
                        "keyword a | b · <title>", id="q")
            yield Static("enter to search · esc to cancel", id="search-hint")

    def on_mount(self) -> None:
        self.query_one("#q", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)


# --------------------------------------------------------------------------- #
# PDF picker — discover + %PDF-verify candidate downloads, native to the app   #
# --------------------------------------------------------------------------- #
# (key, header, width). Explicit widths: a DataTable column's width locks to its content at
# add-time, and update_cell() (used for live verify) never re-expands it — so a column that
# starts with a "⋯"/"" placeholder would clip the later "✓ http403"/"13121KB". Reserve room.
_FETCH_COLS = [("mark", "  ", 10), ("source", "Source", 9), ("host", "Host", 26),
               ("detail", "Detail", 28), ("size", "Size", 9)]


class FetchScreen(ModalScreen["str | None"]):
    """Pick a PDF to download for a node. Discovers candidate sources, streams them into a
    table, and %PDF-verifies each live (rows flip ✓/✗ in place — a paywall/landing page can
    never be picked). Enter on a verified row returns its url; esc cancels. All native to the
    app — no drop to an external picker."""
    CSS = """
    FetchScreen { align: center middle; background: $background 60%; }
    #fetch-box { width: 80%; height: 80%; border: round $primary;
                 background: $surface; padding: 0 1; }
    #fetch-title { color: $accent; }
    #fetch-hint  { color: $text-muted; }
    #fetch-table { height: 1fr; }
    #fetch-table > .datatable--cursor { background: $accent; }
    """
    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def __init__(self, node: "Node") -> None:
        super().__init__()
        self.node = node
        self._cands: list[dict] = []       # row index → candidate dict (gains ok/status/size)

    def compose(self) -> ComposeResult:
        with Vertical(id="fetch-box"):
            title = (self.node.title or self.node.label)[:90]
            yield Static(f"Download PDF — {title}", id="fetch-title")
            yield DataTable(id="fetch-table", cursor_type="row", zebra_stripes=True)
            yield Static("⋯ searching sources…", id="fetch-hint")

    def on_mount(self) -> None:
        t = self.query_one("#fetch-table", DataTable)
        for key, label, width in _FETCH_COLS:
            t.add_column(label, key=key, width=width)
        t.focus()
        self._discover()
        self.call_after_refresh(self._fit_detail)

    def _fit_detail(self) -> None:
        """Grow the Detail column to fill the leftover width so Size sits flush at the right edge.
        DataTable has no flex column, so compute it: table width − the other (fixed) render widths.
        `get_render_width` = 2·cell_padding + width, so account for padding on every column."""
        t = self.query_one("#fetch-table", DataTable)
        region = t.scrollable_content_region                 # excludes border/padding/scrollbar
        total = region.width or t.size.width
        if total <= 0:
            return
        pad = 2 * t.cell_padding
        fixed = sum(pad + w for k, _, w in _FETCH_COLS if k != "detail")
        detail_w = max(20, total - fixed - pad)
        for key, col in t.columns.items():
            if key.value == "detail":
                col.width, col.auto_width = detail_w, False
        t._require_update_dimensions = True
        t.refresh()

    def on_resize(self) -> None:
        self.call_after_refresh(self._fit_detail)

    def _cells(self, c: dict) -> tuple:
        ok = c.get("ok")
        if ok is True:
            mark = Text("✓ " + c.get("status", "PDF"), style="green")
        elif ok is False:
            mark = Text("✗ " + c.get("status", ""), style="red")
        else:
            mark = Text("⋯", style="dim")
        base = "grey50" if ok is False else None
        return (
            mark,
            Text(c["source"], style=base),
            Text((c.get("host") or "")[:26], style=base),
            Text((c.get("label") or "")[:200], style=base),   # DataTable clips to the column width
            Text(str(c.get("size") or ""), style=base or "dim", justify="right"),
        )

    @work
    async def _discover(self) -> None:
        node = self.node
        hint = self.query_one("#fetch-hint", Static)
        try:
            client = await self.app._get_client()
            arxiv_id = None
            if node.doc is not None and str(node.doc.get("eprinttype", "")).lower() == "arxiv":
                arxiv_id = node.doc.get("eprint")
            cands = await gc.discover_pdfs(
                client, doi=node.doi, arxiv_id=arxiv_id,
                oa_id=node.ids.get("openalex_id"),
                title=node.title, author=node.first_author)
        except Exception as e:                       # noqa: BLE001 — surface, never crash
            hint.update(f"discovery failed: {e} · esc to cancel")
            return
        if not cands:
            hint.update("no candidate sources found · esc to cancel")
            return
        table = self.query_one("#fetch-table", DataTable)
        for i, c in enumerate(cands):
            self._cands.append(c)
            table.add_row(*self._cells(c), key=str(i))
        hint.update(f"{len(cands)} source(s) · verifying… · enter=download · esc=cancel")
        self.call_after_refresh(self._fit_detail)   # re-fit once rows exist (scrollbar now present)
        await asyncio.gather(*(self._verify_row(i) for i in range(len(cands))))
        # Favor real PDFs (like the original `papis fetch`): re-sort so ✓-verified rows come first
        # — keeping the version rank among them — then any pending, then rejected ✗ last. The sort
        # is stable, so discovery order is preserved within each group. Rebuild the table from the
        # reordered candidates (row index still maps to self._cands) and land the cursor on the
        # best verified PDF.
        self._cands.sort(key=lambda c: (c.get("ok") is not True, c.get("ok") is False))
        table.clear()
        for i, c in enumerate(self._cands):
            table.add_row(*self._cells(c), key=str(i))
        n_ok = sum(1 for c in self._cands if c.get("ok"))
        if n_ok:
            table.move_cursor(row=0)
        hint.update(f"{n_ok} verified PDF(s) of {len(self._cands)} · "
                    "enter=download · esc=cancel")
        self.call_after_refresh(self._fit_detail)

    async def _verify_row(self, i: int) -> None:
        c = self._cands[i]
        try:
            client = await self.app._get_client()
            c["ok"], c["status"], c["size"] = await gc.verify_pdf(client, c["url"])
        except Exception:                            # noqa: BLE001 — a miss just stays unverified
            c["ok"], c["status"], c["size"] = False, "error", ""
        table = self.query_one("#fetch-table", DataTable)
        for (key, _, _), cell in zip(_FETCH_COLS, self._cells(c)):
            try:
                table.update_cell(str(i), key, cell)
            except Exception:
                pass

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()                     # don't let it bubble to the app's center handler
        i = event.cursor_row
        if i is None or i >= len(self._cands):
            return
        c = self._cands[i]
        if not c.get("ok"):
            self.bell()
            self.notify("that source isn't a verified PDF", severity="warning")
            return
        self.dismiss(c["url"])

    def action_cancel(self) -> None:
        self.dismiss(None)


class StyleScreen(ModalScreen["str | None"]):
    """Pick the CSL reference style. Filter-as-you-type over the bundled styles (id + title);
    ↑/↓ move, enter selects (its id becomes the sticky default), esc cancels. Focus stays on the
    input for typing; ↑/↓/enter are handled at screen level so they drive the list underneath."""
    CSS = """
    StyleScreen { align: center middle; background: $background 60%; }
    #style-box { width: 74%; height: 80%; border: round $primary;
                 background: $surface; padding: 0 1; }
    #style-title { color: $accent; }
    #style-hint  { color: $text-muted; }
    #style-table { height: 1fr; }
    #style-table > .datatable--cursor { background: $accent; }
    """
    BINDINGS = [
        Binding("escape", "cancel", "cancel"),
        Binding("down", "cursor_down", show=False),
        Binding("up", "cursor_up", show=False),
        Binding("enter", "choose", show=False),
    ]

    def __init__(self, styles: list[tuple[str, str]], current: str | None) -> None:
        super().__init__()
        self._all = styles              # [(id, title)] sorted by id
        self._current = current
        self._filtered: list[str] = []  # ids currently shown, row-aligned

    def compose(self) -> ComposeResult:
        with Vertical(id="style-box"):
            yield Static("Reference style — CSL (id matches Typst)", id="style-title")
            yield Input(placeholder="filter by id or title…", id="style-q")
            yield DataTable(id="style-table", cursor_type="row", zebra_stripes=True)
            yield Static("↑/↓ move · enter select · esc cancel", id="style-hint")

    def on_mount(self) -> None:
        t = self.query_one("#style-table", DataTable)
        t.add_column("Style (id)", key="id", width=46)
        t.add_column("Title", key="title")
        self._fill("")
        self.query_one("#style-q", Input).focus()

    def _fill(self, needle: str) -> None:
        t = self.query_one("#style-table", DataTable)
        t.clear()
        self._filtered.clear()
        needle = needle.lower().strip()
        cursor = 0
        for sid, title in self._all:
            if needle and needle not in sid.lower() and needle not in title.lower():
                continue
            here = sid == self._current
            mark = "● " if here else "  "
            style = "yellow" if here else None
            if here:
                cursor = len(self._filtered)
            t.add_row(Text(mark + sid, style=style),
                      Text(title, style="grey62"), key=sid)
            self._filtered.append(sid)
        if self._filtered:
            t.move_cursor(row=cursor)

    def on_input_changed(self, event: Input.Changed) -> None:
        self._fill(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # Focus is on the filter Input, so Enter arrives as a submit (the screen-level `enter`
        # binding never fires) — route it to the same selection action.
        event.stop()
        self.action_choose()

    def action_cursor_down(self) -> None:
        self.query_one("#style-table", DataTable).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#style-table", DataTable).action_cursor_up()

    def action_choose(self) -> None:
        t = self.query_one("#style-table", DataTable)
        i = t.cursor_row
        if i is not None and 0 <= i < len(self._filtered):
            self.dismiss(self._filtered[i])
        else:
            self.bell()

    def action_cancel(self) -> None:
        self.dismiss(None)


# --------------------------------------------------------------------------- #
# top card — metadata for the highlighted node                                 #
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# CSL reference rendering (ctrl-s toggle)                                       #
# --------------------------------------------------------------------------- #
# The card's head can render as a proper CSL reference (author-date, Harvard) instead of our
# invented layout — the familiar bibliography form a researcher already reads fluently.
# citeproc-py renders in-process (pure Python, ~1 ms/entry); the parsed style is cached.
# Library nodes render from their papis Document; grey nodes from a doc-shaped dict we build.
# Styles ship as package data (papers/styles/csl/*.csl) so the tool stays self-contained — located
# via importlib.resources so it works whether run from source or an installed wheel. The set is the
# subset of Typst's bundled CSL styles that citeproc-py 0.10 (CSL 1.0.1) can render — sourced via
# citeproc-py-styles, validated at build time. Named by their CSL id so they match Typst's
# `#bibliography(style: …)` names. The active style + the ctrl-s mode persist in the papers config;
# ctrl-y opens the picker. PAPERS_CSL_STYLE (or legacy PAPIS_GRAPH_CSL_STYLE) overrides.
_STYLE_DIR = os.fspath(importlib.resources.files(__package__) / "styles" / "csl")
_DEFAULT_STYLE_ID = "taylor-and-francis-harvard-x"

# Small persisted UI state (CSL mode + chosen style), so choices survive a restart. papers owns its
# own config dir; best-effort (never fatal). Migrates once from the old papis-config location.
_CONFIG_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "papers")
_STATE_PATH = os.path.join(_CONFIG_DIR, "state.json")
_OLD_STATE_PATH = os.path.join(papis.config.get_config_folder(), "graph-state.json")


def _load_state() -> dict:
    for path in (_STATE_PATH, _OLD_STATE_PATH):          # new location, then one-time migration read
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            continue
    return {}


def _save_state(**kw: Any) -> None:
    state = _load_state()
    state.update(kw)
    try:
        os.makedirs(_CONFIG_DIR, exist_ok=True)
        with open(_STATE_PATH, "w") as f:
            json.dump(state, f)
    except Exception:
        pass


def _style_path() -> str:
    """Absolute path to the active CSL style: env override → chosen id in the bundle → default."""
    env = os.environ.get("PAPERS_CSL_STYLE") or os.environ.get("PAPIS_GRAPH_CSL_STYLE")
    if env:
        return env
    sid = _load_state().get("csl_style") or _DEFAULT_STYLE_ID
    p = os.path.join(_STYLE_DIR, sid + ".csl")
    if os.path.exists(p):
        return p
    return os.path.join(_STYLE_DIR, _DEFAULT_STYLE_ID + ".csl")


@functools.lru_cache(maxsize=1)
def _available_styles() -> list[tuple[str, str]]:
    """[(id, human title)] for every bundled style, sorted by id. Title read from each .csl's
    <title>; falls back to the id. Cached — the bundle is fixed at runtime."""
    import xml.etree.ElementTree as ET
    out = []
    for fn in sorted(os.listdir(_STYLE_DIR)) if os.path.isdir(_STYLE_DIR) else []:
        if not fn.endswith(".csl"):
            continue
        sid, title = fn[:-4], fn[:-4]
        try:
            for _, el in ET.iterparse(os.path.join(_STYLE_DIR, fn)):
                if el.tag.split("}")[-1] == "title":
                    title = (el.text or sid).strip()
                    break
        except Exception:
            pass
        out.append((sid, title))
    return out


@functools.lru_cache(maxsize=16)
def _csl_style(path: str):
    from citeproc import CitationStylesStyle
    return CitationStylesStyle(path, validate=False)


def _render_csl(doc_like: Any, style_path: str) -> str:
    """Render a papis-doc-shaped mapping as a one-paragraph CSL reference string."""
    import papis.exporters.csl as pcsl
    from citeproc import (Citation, CitationItem, CitationStylesBibliography,
                          formatter)
    from citeproc.source import BibliographySource
    src = BibliographySource(doc_like)
    src.add(pcsl.to_csl(doc_like))
    bib = CitationStylesBibliography(_csl_style(style_path), src, formatter.plain)
    cit = Citation([CitationItem(str(doc_like["ref"]).lower())])
    bib.register(cit)
    bib.cite(cit, callback=lambda i: None)
    for item in bib.bibliography():
        return str(item).replace("..", ".").strip()
    return ""


def node_csl_reference(n: Node) -> str:
    """The selected node as a CSL reference, or "" if it can't render (citeproc missing, bad
    style, or too little metadata) — in which case the caller falls back to our layout."""
    try:
        if n.in_library and n.doc is not None:
            doc_like: Any = n.doc
        else:
            doc_like = _node_to_data(n)
            doc_like["ref"] = n.ref or n.label or "ref"
            doc_like["type"] = n.type or "article"
            # to_csl reads doc["author_list"] whenever an "author" key exists; drop an
            # unpaired author string so it doesn't KeyError on a name it couldn't split.
            if "author" in doc_like and "author_list" not in doc_like:
                doc_like.pop("author")
        return _render_csl(doc_like, _style_path())
    except Exception:
        return ""


class TopCard(Vertical):
    """The bibliographic-reference card. Metadata + abstract fill the top; the citation key
    (@ref) is docked at the bottom-left — the exact form you cite with in Typst / Pandoc /
    markdown, so it reads like a caption on the reference. ctrl-s flips the head between our
    layout and a CSL reference."""

    csl_mode = False        # ctrl-s: render the head as a CSL reference instead of our layout

    def compose(self) -> ComposeResult:
        yield Static("", id="card-body")
        with Horizontal(id="card-ref"):
            yield Static("", id="card-ref-left", markup=False)     # @ref
            yield Static("", id="card-ref-mid", markup=False)      # Cited-by · Infl
            yield Static("", id="card-ref-right")                  # spacer

    def _line(self, t: Text, label: str, terms: list[str], style: str) -> None:
        """A single non-wrapping `label: a · b · c` line, truncated to the card width."""
        joined = "  ·  ".join(terms)
        maxw = max(24, self.size.width - 2 - len(label) - 2)
        if len(joined) > maxw:
            joined = joined[: maxw - 1] + "…"
        t.append(f"\n{label}: ", style="grey50")
        t.append(joined, style=style)

    def _normal_head(self, t: Text, n: Node) -> int:
        """Our invented layout — year+title, author, venue, DOI/ISBN. Returns rows used (for
        the abstract budget). The title is the only line that realistically wraps."""
        badge = "" if n.in_library else "  ○ not in library"
        title = n.title or "(untitled)"
        head = f"{n.year or '—'}  {title}{badge}"
        rows = max(1, -(-len(head) // max(1, self.size.width)))    # ceil-divide → wrapped rows
        t.append(f"{n.year or '—'}  ", style="cyan")
        t.append(title, style="bold")
        t.append(badge, style="grey50")
        t.append("\n")
        t.append(n.author or "—", style="italic")
        rows += 1
        venue = compose_venue(n)
        if venue:
            maxw = max(24, self.size.width - 2)
            if len(venue) > maxw:
                venue = venue[: maxw - 1] + "…"
            t.append("\n")
            t.append(venue, style="grey70")
            rows += 1
        # cited-by/influential live in the centered bottom row; this line is IDs only —
        # show DOI and ISBN both when both exist (a book/chapter can carry each).
        meta = []
        if n.doi:
            meta.append(n.doi)
        if n.isbn:
            meta.append(f"ISBN {n.isbn}")
        if meta:
            t.append("\n" + "   ".join(meta), style="dim")
            rows += 1
        return rows

    def _csl_head(self, t: Text, n: Node, body: Static) -> int | None:
        """ctrl-s mode — the head as a CSL reference (tandf-harvard). Returns rows used, or
        None if it couldn't render (caller then falls back to the normal layout)."""
        ref_str = node_csl_reference(n)
        if not ref_str:
            return None
        inner_w = max(24, (body.size.width or self.size.width) - 2)
        rows = max(1, -(-len(ref_str) // inner_w))
        # Bold the title span in place (citeproc's plain formatter has no emphasis) so the card
        # keeps a visual anchor like our layout. Locate n.title verbatim in the rendered string;
        # if the style transformed it (case/punctuation) and it isn't found, just leave it plain.
        seg = Text(ref_str)
        title = (n.title or "").strip().rstrip(".")
        if title:
            i = ref_str.find(title)
            if i >= 0:
                seg.stylize("bold", i, i + len(title))
        t.append(seg)
        if not n.in_library:
            t.append("  ○ not in library", style="grey50")
        # An author-date journal style (tandf-harvard) omits the publisher and DOI/ISBN, so the
        # CSL paragraph loses what our plain card shows. Restore them below — publisher on its own
        # line, DOI/ISBN on the next — but only fields the style didn't already print (a DOI-
        # carrying style won't double up), so this stays correct across styles.
        if n.publisher and str(n.publisher) not in ref_str:
            t.append("\n" + str(n.publisher), style="dim")
            rows += 1
        ids = []
        if n.doi and str(n.doi) not in ref_str:
            ids.append(str(n.doi))
        if n.isbn and str(n.isbn) not in ref_str:
            ids.append(f"ISBN {n.isbn}")
        if ids:
            t.append("\n" + "   ".join(ids), style="dim")
            rows += 1
        return rows

    def show(self, n: Node | None) -> None:
        body = self.query_one("#card-body", Static)
        ref_left = self.query_one("#card-ref-left", Static)
        ref_mid = self.query_one("#card-ref-mid", Static)
        if n is None:
            body.update("")
            ref_left.update("")
            ref_mid.update("")
            return
        t = Text()
        rows = self._csl_head(t, n, body) if self.csl_mode else None
        if rows is None:                          # normal mode, or CSL render unavailable
            t = Text()
            rows = self._normal_head(t, n)
        if n.topics or n.keywords:
            t.append("\n")                        # blank line: separate head from Topics/Keywords
            rows += 1
        if n.topics:
            self._line(t, "Topics", n.topics, "grey62")   # muted, like the abstract (no highlight)
            rows += 1
        if n.keywords:
            self._line(t, "Keywords", n.keywords, "grey62")
            rows += 1
        if n.abstract:
            # Fill the BODY region's ACTUAL remaining space (the @ref line is a separate
            # docked widget, so it's already excluded): budget = free rows × inner width.
            # Card is a fixed height, so this stays reflow-free.
            inner_w = max(24, (body.size.width or self.size.width) - 2)
            avail = max(2, (body.size.height or self.size.height) - rows - 1)
            budget = avail * inner_w
            abstract = n.abstract.strip()
            if len(abstract) > budget:
                abstract = abstract[:budget - 1].rsplit(" ", 1)[0] + "…"
            t.append("\n\n")
            t.append(abstract, style="italic grey62")
        body.update(t)
        ref_left.update(f"@{n.ref}" if n.ref else "")
        # influential · cited-by, centered. Infl in the table's yellow (only when it has data;
        # the "-" placeholder stays muted like the rest). A real 0 shows "0".
        cited = fmt_count(n.citation_count) or "-"
        infl_raw = fmt_count(n.influential_count)
        mid = Text()
        mid.append(infl_raw or "-", style="yellow" if infl_raw else "")
        mid.append("  ·  ")
        mid.append(cited)
        ref_mid.update(mid)


# --------------------------------------------------------------------------- #
# app                                                                          #
# --------------------------------------------------------------------------- #
class PapersApp(App):
    CSS = """
    Screen { layout: vertical; }
    #card { height: 19; border: round $primary; padding: 0 1; }
    #card.expanded { height: 1fr; }          /* ctrl-d: Details panel fills the screen */
    #card-body { height: 1fr; }              /* metadata + abstract */
    #card-ref { height: 1; }                 /* docked bottom row */
    #card-ref-left  { width: 1fr; color: $text-muted; }   /* @ref citation key (bottom-left) */
    #card-ref-mid   { width: auto; color: $text-muted; }  /* Cited-by / Infl (centered) */
    #card-ref-right { width: 1fr; }          /* spacer, balances the left so mid is centered */
    #center { height: 1fr; border: round $accent; }
    #center.hidden { display: none; }        /* ctrl-d: table hidden while Details is expanded */
    #status { height: 1; color: $text-muted; padding: 0 1; }
    DataTable > .datatable--cursor { background: $accent; }
    """

    ENABLE_COMMAND_PALETTE = False   # free ctrl+p for "home"

    BINDINGS = [
        Binding("ctrl+c", "promote_right", "Citations", priority=True),
        Binding("ctrl+r", "promote_left", "References"),
        Binding("ctrl+o", "back", "Back"),
        Binding("escape", "back", "Back", show=False),   # esc = back (when no filter active)
        Binding("ctrl+i", "forward", "Forward"),
        Binding("ctrl+slash", "search", "Search"),
        Binding("ctrl+underscore", "search", "Search", show=False),   # legacy ctrl+/ (0x1f)
        Binding("ctrl+p", "home", "Papers"),
        Binding("ctrl+d", "toggle_details", "Expand Details"),
        Binding("ctrl+s", "toggle_csl", "CSL ref"),
        Binding("ctrl+y", "pick_style", "Style"),
        Binding("ctrl+e", "edit", "Edit entry"),
        Binding("ctrl+shift+d", "delete", "Delete entry"),
        Binding("ctrl+q", "quit", "Quit"),           # plain 'q' is a filter character (on_key)
        Binding("ctrl+question_mark", "help", "Help"),
        Binding("ctrl+shift+slash", "help", "Help", show=False),      # alt encoding of ctrl-shift-/
        Binding("enter", "enter", "open/get"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.doi_index: dict[str, Document] = {}
        self.library: list[Node] = []
        self.stack: list[Frame] = []
        self.sp = 0                     # stack pointer (for back/forward)
        self.filter_buf = ""
        self._details_expanded = False          # ctrl-d: details panel full-screen (table hidden)
        self._client: gc.Client | None = None   # lazy shared async client for live fetch

    # ---- layout ----
    def compose(self) -> ComposeResult:
        yield TopCard(id="card")
        yield DataTable(id="center", cursor_type="row", zebra_stripes=True)
        yield Static(id="status")

    def on_mount(self) -> None:
        db = papis.database.get()
        docs = db.get_all_documents()
        self.doi_index = {str(d["doi"]).lower(): d for d in docs if d.get("doi")}
        self.library = sorted((node_from_doc(d) for d in docs),
                              key=lambda n: str(n.year or ""))
        table = self.query_one("#center", DataTable)
        table.add_columns(" ", "Year", "Author", "Title", "Infl", "Citations")
        self.query_one(TopCard).csl_mode = bool(_load_state().get("csl_mode", False))
        self._push(Frame("Papers", self.library))
        table.focus()

    async def on_unmount(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    # ---- frame / center rendering ----
    def _current(self) -> Frame:
        return self.stack[self.sp]

    def _push(self, frame: Frame) -> None:
        """Set the initial (root) frame."""
        self.stack = [frame]
        self.sp = 0
        self.filter_buf = ""
        self._render_center()

    def _push_frame(self, frame: Frame) -> None:
        """Append a frame after the current one, truncating any forward history."""
        self.stack = self.stack[: self.sp + 1]
        self.stack.append(frame)
        self.sp = len(self.stack) - 1
        self.filter_buf = ""
        self._render_center()

    def _visible_nodes(self) -> list[Node]:
        nodes = self._current().nodes
        buf = self.filter_buf.strip()
        if not buf:
            return nodes
        if buf.startswith("#"):
            kw = buf[1:].lower()
            return [n for n in nodes
                    if kw in (str(n.doc.get("tags", "")).lower() if n.doc else "")
                    or kw in n.title.lower()]
        from rapidfuzz import fuzz
        scored = [(fuzz.partial_ratio(buf.lower(), f"{n.title} {n.author}".lower()), n)
                  for n in nodes]
        return [n for s, n in sorted(scored, key=lambda x: -x[0]) if s > 55]

    def _row_cells(self, n: Node) -> tuple:
        """The six DataTable cells for one node — shared by full render and live append."""
        style = None if n.in_library else "grey50"
        t = n.title or "(untitled)"
        if len(t) > 72:
            t = t[:71] + "…"
        cited = fmt_count(n.citation_count)
        infl = fmt_count(n.influential_count)
        # Leading glyph colour = download status (independent of the row's in-library grey):
        # grey when the content file isn't on disk, else its normal colour (yellow for an
        # influential citation edge, default otherwise).
        if not n.downloaded:
            glyph_style = "grey50"
        elif n.is_influential:
            glyph_style = "yellow"
        else:
            glyph_style = ""
        return (
            Text(type_glyph(n.type), style=glyph_style),
            Text(str(n.year or ""), style=style),
            Text(n.first_author, style=style),
            Text(t, style=style),
            Text(infl, style="yellow" if infl else (style or "dim"), justify="right"),
            Text(cited, style=style or "dim", justify="right"),
        )

    def _render_center(self) -> None:
        table = self.query_one("#center", DataTable)
        table.clear()
        self._rows = self._visible_nodes()
        for n in self._rows:
            table.add_row(*self._row_cells(n))
        cur = min(self._current().cursor, max(0, len(self._rows) - 1))
        if self._rows:
            table.move_cursor(row=cur)
        self._update_card()
        self._render_status()

    def _render_status(self) -> None:
        f = self._current()
        n = len(self._rows)
        total = len(f.nodes)
        shown = f"{n}" if n == total else f"{n}/{total}"
        filt = f"   filter: {self.filter_buf}" if self.filter_buf else ""
        load = f"   ⋯ fetching… ({total})" if f.loading else ""
        trunc = (f"   (top {total} of {f.total:,} matches)"
                 if f.total and f.total > total else "")
        hist = f"   [{self.sp + 1}/{len(self.stack)}]"
        self.query_one("#status", Static).update(
            f"{f.title}   ({shown} papers){filt}{load}{trunc}{hist}")

    # ---- context columns + card for the highlighted row ----
    def _selected_node(self) -> Node | None:
        if not getattr(self, "_rows", None):
            return None
        table = self.query_one("#center", DataTable)
        idx = table.cursor_row
        if idx is None or idx >= len(self._rows):
            return None
        return self._rows[idx]

    def _neighbors(self, n: Node | None) -> tuple[list[Node], list[Node]]:
        """(cites, cited_by) for a node — from its sidecars if it's a library doc,
        else empty (grey node: neighbors are fetched live, see _fetch_neighbors)."""
        if n is None or n.doc is None:
            return [], []
        cites = [node_from_citation(c, self.doi_index)
                 for c in papis.citations.get_citations(n.doc)]
        cited = [node_from_citation(c, self.doi_index)
                 for c in papis.citations.get_cited_by(n.doc)]
        return cites, cited

    def _update_card(self) -> None:
        # Cheap: just the top card for the highlighted row. Neighbors are NOT read here
        # anymore — only on an explicit ctrl-r/ctrl-c promote — so scrolling stays fast
        # even over a paper with a 12k-row citations sidecar.
        self.query_one(TopCard).show(self._selected_node())

    def on_data_table_row_highlighted(self, _: DataTable.RowHighlighted) -> None:
        self._current().cursor = self.query_one("#center", DataTable).cursor_row or 0
        self._update_card()
        n = self._selected_node()
        if n is not None and n.keywords is None:   # not fetched yet → lazy load
            self._load_about(n)

    # ---- typed filter (printable keys; ctrl-combos stay bindings) ----
    def on_key(self, event) -> None:
        if len(self.screen_stack) > 1:      # a modal (search/help) is up — it owns keys
            return
        if event.character and event.character.isprintable() and len(event.character) == 1:
            self.filter_buf += event.character
            self._render_center()
            event.prevent_default()
        elif event.key == "backspace" and self.filter_buf:
            self.filter_buf = self.filter_buf[:-1]
            self._render_center()
            event.prevent_default()
        elif event.key == "escape" and self.filter_buf:
            self.filter_buf = ""
            self._render_center()
            event.prevent_default()

    # ---- actions ----
    def _promote(self, nodes: list[Node], title: str) -> None:
        if not nodes:
            self.bell()
            return
        self._push_frame(Frame(title, nodes))

    def action_promote_left(self) -> None:      # ctrl-r → references of selected
        self._promote_neighbors("cites")

    def action_promote_right(self) -> None:     # ctrl-c → citations of selected
        self._promote_neighbors("cited")

    def _promote_neighbors(self, direction: str) -> None:
        """'cited' = citations (cited-by), 'cites' = references. An in-library node reads
        its sidecars instantly; a grey node has none on disk, so we open an empty frame
        and stream its neighbors in live from OpenAlex (walk past the library edge)."""
        n = self._selected_node()
        if not n:
            return
        label = "citations" if direction == "cited" else "references"
        if n.doc is not None:
            cites, cited = self._neighbors(n)
            self._promote(cited if direction == "cited" else cites, f"{label} of {n.label}")
            return
        # grey neighbor — nothing on disk; walk the graph live
        if not (n.ids.get("openalex_id") or n.doi or n.title):
            self.bell()
            return
        frame = Frame(f"{label} of {n.label}", [], loading=True)
        self._push_frame(frame)
        self._fetch_neighbors(n, direction, frame)

    def action_back(self) -> None:
        if self.sp > 0:
            self.sp -= 1
            self.filter_buf = ""
            self._render_center()
        else:
            self.bell()

    def action_forward(self) -> None:
        if self.sp < len(self.stack) - 1:
            self.sp += 1
            self.filter_buf = ""
            self._render_center()
        else:
            self.bell()

    def action_home(self) -> None:
        self._push_frame(Frame("Papers", self.library))

    def action_toggle_details(self) -> None:
        """ctrl-d: expand the Details panel to fill the screen (hiding the table) and back.
        The abstract budget tracks the panel size, so expanding shows the whole abstract."""
        self._details_expanded = not self._details_expanded
        self.query_one("#card").set_class(self._details_expanded, "expanded")
        self.query_one("#center").set_class(self._details_expanded, "hidden")
        # re-render the Details panel AFTER the relayout so the abstract re-fills the new height
        self.call_after_refresh(self._update_card)
        if not self._details_expanded:
            self.query_one("#center", DataTable).focus()

    def action_toggle_csl(self) -> None:
        """ctrl-s: flip the Details head between our layout and a CSL reference (tandf-harvard).
        Re-renders the selected row in the other form; the choice persists across restarts."""
        card = self.query_one(TopCard)
        card.csl_mode = not card.csl_mode
        _save_state(csl_mode=card.csl_mode)
        self._update_card()

    def action_pick_style(self) -> None:
        """ctrl-y: choose the CSL reference style (persisted, and switches into CSL mode)."""
        self._pick_style_worker()

    @work
    async def _pick_style_worker(self) -> None:
        cur = _load_state().get("csl_style") or _DEFAULT_STYLE_ID
        chosen = await self.push_screen_wait(StyleScreen(_available_styles(), cur))
        if not chosen:
            return
        card = self.query_one(TopCard)
        card.csl_mode = True
        _save_state(csl_style=chosen, csl_mode=True)
        self._update_card()
        self.notify(f"reference style → {chosen}")

    def action_edit(self) -> None:
        """ctrl-e: open the selected library entry's info.yaml in $EDITOR (or vi), then reload
        the (possibly changed) metadata. Only for in-library papers; grey nodes have no file."""
        n = self._selected_node()
        if n is None or not n.in_library or n.doc is None:
            self.bell()
            return
        self._edit_worker(n)

    @work(group="edit")
    async def _edit_worker(self, n: Node) -> None:
        folder = n.doc.get_main_folder()
        info = n.doc.get_info_file()
        editor = os.environ.get("EDITOR") or "vi"
        try:
            with self.suspend():                     # hand the terminal to the editor
                subprocess.run([editor, info])
        except Exception as e:                       # noqa: BLE001
            self.notify(f"edit failed: {e}", severity="error")
            return
        # reload from disk — title/author/ref/year/… may all have changed — and copy the
        # fresh fields onto EVERY node that points at this paper (Home list + all frames).
        fresh = papis.document.from_folder(folder)
        papis.database.get().update(fresh)
        updated = node_from_doc(fresh)
        for lst in [self.library, *(fr.nodes for fr in self.stack)]:
            for x in lst:
                if x.doc is not None and x.doc.get_main_folder() == folder:
                    for f in fields(Node):
                        setattr(x, f.name, getattr(updated, f.name))
        if n.doi:
            self.doi_index[n.doi] = fresh
        self._render_center()
        self.notify(f"updated @{n.ref}")

    def action_delete(self) -> None:
        """⇧ctrl-d: delete the selected library entry — its folder, PDF, notes, everything —
        behind a y/n confirm. Grey nodes have nothing to delete."""
        n = self._selected_node()
        if n is None or not n.in_library or n.doc is None:
            self.bell()
            return
        self.push_screen(
            ConfirmScreen(
                f"Delete  @{n.ref} ?",
                f"{n.title}\n\nPermanently removes the entry, its PDF, notes, and all\n"
                "associated files from your library. This cannot be undone."),
            lambda ok: self._delete_worker(n) if ok else None)

    @work(group="delete")
    async def _delete_worker(self, n: Node) -> None:
        ref = n.ref
        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, lambda: papis.commands.rm.run(n.doc))
        except Exception as e:                       # noqa: BLE001
            self.notify(f"delete failed: {e}", severity="error")
            return
        # Prune from the Home list (its nodes list is shared by the Papers frame, so mutate in
        # place). In OTHER frames the paper survives as an edge/result → revert it to grey.
        self.library[:] = [x for x in self.library if x.ref != ref]
        if n.doi:
            self.doi_index.pop(n.doi, None)
        for fr in self.stack:
            for x in fr.nodes:
                if x.ref == ref:
                    x.in_library, x.doc, x.ref, x.downloaded = False, None, None, False
        self._render_center()
        self.notify(f"deleted @{ref}")

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        # Enter on a center-table row. DataTable owns the `enter` key (→ select_cursor →
        # this RowSelected), so the App's `enter` binding never fires — this handler is the
        # real Enter trigger. Ignore the picker modal's own table (id="fetch-table").
        if event.data_table.id == "center":
            self.action_enter()

    def action_enter(self) -> None:
        """Enter is tri-state on the highlighted node:
          in-library + has PDF  → open it
          in-library + no PDF   → fetch a PDF (picker) → open
          grey (not in library) → add → fetch → open, and flip the row live.
        Reversible (`papis rm`), so there's no confirm — just do it. All IO on a worker."""
        n = self._selected_node()
        if n is not None:
            self._enter_worker(n)

    @work(group="enter")
    async def _enter_worker(self, n: Node) -> None:
        try:
            if n.in_library and n.doc is not None:
                pdf = _first_pdf(n.doc)
                if pdf:
                    _open_file(pdf)
                    self.notify(f"opening {n.label}")
                else:
                    await self._get_and_open(n)
            else:
                await self._add_then_get(n)
        except Exception as e:                       # noqa: BLE001 — never crash the app
            self.notify(f"enter failed: {e}", severity="error")

    async def _get_and_open(self, n: Node) -> None:
        """Push the PDF picker, download+attach the chosen url in-process, then open."""
        url = await self.push_screen_wait(FetchScreen(n))
        if not url:
            return
        ref = n.ref or (n.doc.get("ref") if n.doc else None) or "document"
        try:
            client = await self._get_client()
            tmp = os.path.join(tempfile.mkdtemp(), f"{ref}.pdf")
            self.notify(f"downloading {n.label}…")
            size = await gc.download_pdf(client, url, tmp)
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None, lambda: papis.commands.addto.run(n.doc, [tmp], file_name=ref))
            papis.database.get().update(n.doc)
        except Exception as e:                       # noqa: BLE001
            self.notify(f"download failed: {e}", severity="error")
            return
        fresh = papis.document.from_folder(n.doc.get_main_folder())
        n.doc = fresh
        n.downloaded = _is_downloaded(fresh)         # flip the leading glyph grey→coloured
        self.notify(f"downloaded PDF ({size // 1024}KB) → {n.label}")
        _open_file(_first_pdf(fresh) or tmp)
        self._render_center()                        # re-render so the glyph colour updates

    async def _add_then_get(self, n: Node) -> None:
        """Grey node → add to the library in-process (keeps the pickle db coherent), flip the
        row to in-library live, then fetch+open like an in-library paper with no PDF."""
        loop = asyncio.get_event_loop()
        try:
            doc = await loop.run_in_executor(None, self._add_node_blocking, n)
        except Exception as e:                       # noqa: BLE001
            self.notify(f"add failed: {e}", severity="error")
            return
        if doc is None:
            self.notify("added, but couldn't locate the new entry", severity="warning")
            return
        n.in_library, n.doc, n.ref = True, doc, doc.get("ref")
        if n.doi:
            self.doi_index[n.doi] = doc
        self._render_center()                        # restyle the row (no longer grey)
        self.notify(f"Created bibliographic entry @{n.ref}  {n.title}")
        await self._get_and_open(n)                  # attach refreshes n.doc + n.downloaded
        if not n.downloaded:                         # picker cancelled / download failed
            self.notify(f"{n.label} is in your library — press Enter to download its PDF")
        # Add to Home last, so its glyph reflects the final on-disk state (post-attach).
        self.library.append(node_from_doc(n.doc))

    def _add_node_blocking(self, n: Node) -> Document | None:
        """Create a papis entry for a grey node. DOI → canonical Crossref metadata; DOI-less
        (books/reports OpenAlex doesn't index well) → build info.yaml from the cached Node.
        Uses the in-process papis API so `papis.database` stays coherent, then queries the
        new doc back by DOI (else title)."""
        data = None
        if n.doi:
            try:
                recs = papis.crossref.get_data(dois=[n.doi])
                data = recs[0] if recs else None
            except Exception:
                data = None
        if not data:
            data = _node_to_data(n)
        if n.doi:
            data.setdefault("doi", n.doi)
        papis.commands.add.run([], data=data, batch=True)
        db = papis.database.get()
        if n.doi:
            hits = db.query_dict({"doi": n.doi})
            if hits:
                return hits[0]
        for d in db.get_all_documents():
            if d.get("title") and gc.same_paper(str(d.get("title")), n.title,
                                                d.get("year"), n.year):
                return d
        return None

    def action_search(self) -> None:
        # ^/ — command popup for whole-corpus discovery (doi · author · keyword · title)
        self.push_screen(SearchScreen(), self._run_search)

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def _run_search(self, raw: str | None) -> None:
        parsed = parse_query(raw or "")
        if parsed is None:                  # empty / cancelled
            return
        kind, query = parsed
        frame = Frame(f"search {kind}: {query}", [], loading=True)
        self._push_frame(frame)
        self._search_worker(kind, query, frame)

    @work(group="search")
    async def _search_worker(self, kind: str, query: str, frame: Frame) -> None:
        """Whole-corpus discovery on a background worker → grey Frame (never persisted).
        Same identity/never-crash discipline as the neighbor fetch."""
        try:
            client = await self._get_client()
            results, total = await gc.search(client, kind, query)
            if not any(fr is frame for fr in self.stack):
                return
            frame.nodes = [node_from_citation(c, self.doi_index) for c in results]
            frame.total = total
            frame.loading = False
            self._refresh_if_current(frame)
            if not results:
                self.notify(f"no matches for “{query}”", severity="warning")
        except Exception as e:              # noqa: BLE001 — surface, never crash
            frame.loading = False
            self._refresh_if_current(frame)
            self.notify(f"search failed: {e}", severity="error")

    # ---- live graph walk (grey neighbors) ---------------------------------- #
    async def _get_client(self) -> gc.Client:
        if self._client is None:
            self._client = gc.Client()
        return self._client

    async def _grey_oa_id(self, client: gc.Client, node: Node) -> str | None:
        """The OpenAlex work id for a grey node: carried on the edge if we have it,
        else resolved by DOI (fast) or title.search (DOI-less)."""
        oa = node.ids.get("openalex_id")
        if oa:
            return oa
        res = await gc.resolve(client, {"doi": node.doi, "title": node.title,
                                        "year": node.year})
        return res.oa

    @work(group="fetch")
    async def _fetch_neighbors(self, node: Node, direction: str, frame: Frame) -> None:
        """Background worker: resolve the grey node and stream its neighbors into `frame`.
        Guards every UI touch with `is`-identity so a fetch the user has navigated away
        from stops (citations) or is discarded (references) instead of clobbering the view.
        Never raises — failures surface as a toast so the app can't be killed by a worker."""
        try:
            client = await self._get_client()
            oa_id = await self._grey_oa_id(client, node)
            if not oa_id:
                frame.loading = False
                self._refresh_if_current(frame)
                self.notify(f"{node.label}: not found on OpenAlex", severity="warning")
                return
            if direction == "cited":                      # citations — stream (unbounded)
                async for cit in gc.stream_cited_by(client, oa_id):
                    if not any(fr is frame for fr in self.stack):   # navigated past it
                        return
                    frame.nodes.append(node_from_citation(cit, self.doi_index))
                    if len(frame.nodes) % 50 == 0:
                        self._live_tick(frame)
            else:                                          # references — one bounded page-set
                refs = await gc._openalex_references(client, oa_id) or []
                if not any(fr is frame for fr in self.stack):
                    return
                frame.nodes = [node_from_citation(c, self.doi_index) for c in refs]
            frame.loading = False
            self._refresh_if_current(frame)
        except Exception as e:                             # noqa: BLE001 — surface, never crash
            frame.loading = False
            self._refresh_if_current(frame)
            self.notify(f"fetch failed: {e}", severity="error")

    def _live_tick(self, frame: Frame) -> None:
        """Append only the newly-arrived rows to the live table — no full rebuild, so
        streaming a many-thousand-citer paper stays O(n) rather than O(n²)."""
        if self._current() is not frame:
            return
        if self.filter_buf:                # filter active → full (filtered) render instead
            self._render_center()
            return
        table = self.query_one("#center", DataTable)
        self._rows = frame.nodes
        for n in frame.nodes[table.row_count:]:
            table.add_row(*self._row_cells(n))
        self._render_status()

    def _refresh_if_current(self, frame: Frame) -> None:
        if self._current() is frame:
            self._render_center()

    @work(exclusive=True, group="about")
    async def _load_about(self, node: Node) -> None:
        """Lazily fetch topics+keywords for the highlighted node and cache them on it.
        `exclusive` + a short debounce means fast scrolling cancels in-flight fetches, so
        we only hit OpenAlex for a row the cursor actually rests on. Card re-renders only
        if that row is still highlighted."""
        await asyncio.sleep(0.25)               # debounce; a newer highlight cancels this
        if node.keywords is not None:
            return
        try:
            client = await self._get_client()
            about = await gc.fetch_about(
                client, node.ids.get("openalex_id"), node.doi, node.title, node.year)
        except Exception:                        # display-only; a miss just leaves it blank
            about = gc.About([], [], "")
        node.topics, node.keywords = about.topics, about.keywords
        node.abstract = node.abstract or about.abstract   # keep a local (info.yaml) abstract
        if node.doc is None:                     # grey node — no info.yaml; take venue from OA
            node.venue, node.publisher = about.venue, about.publisher
            node.volume, node.number, node.pages = about.volume, about.number, about.pages
        if self._selected_node() is node:
            self._update_card()


if __name__ == "__main__":
    PapersApp().run()
