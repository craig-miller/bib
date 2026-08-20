#!/usr/bin/env python3
"""bib — a citation-graph discovery TUI (Textual).

A top metadata card over a single full-width table. The table starts as your library
(Papers); the columns are `· year author title Cited Infl` (Infl = S2 influential
citation count). Selecting a row and pressing `c`/`r` replaces the table with that
paper's citations/references (read from the papis `cited-by.yaml`/`citations.yaml`
sidecars); a browser-style history stack walks in and out.

Keys (single mode; press `?` for the live cheat sheet):
  j k · gg G · ^d ^u         cursor · top/bottom · half-page
  ^o · ^i · esc               back · forward · back-when-nothing-to-cancel
  / · f · ?                   filter (fzf · #tag prefix) · Find papers (OpenAlex) · help
  s S · z                     CSL toggle · pick style · zoom Details
  enter                       open (in-library+PDF) · fetch PDF · add+fetch (grey)
  n · e · t · c · r · dd      notes · edit · tags · citations · references · delete
  yc · yd · yu                yank citekey (@ref) · DOI · source URL (OSC 52)
  q                           quit

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
import re
import subprocess
import tempfile
from collections import Counter

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
from textual.widgets import DataTable, Input, OptionList, Static
from textual.widgets.option_list import Option

import papis.config
import papis.database
import papis.citations
import papis.crossref
import papis.commands.add
import papis.commands.addto
import papis.commands.rm
import papis.document
import papis.notes
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


def doc_tags(doc: Document | None) -> list[str]:
    """A library entry's user tags as a clean list. papis stores `tags` as a YAML list, but
    tolerate a legacy space/comma-separated string too."""
    if doc is None:
        return []
    raw = doc.get("tags")
    if not raw:
        return []
    if isinstance(raw, str):
        return [t for t in raw.replace(",", " ").split() if t]
    return [str(t) for t in raw]


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
    citation_count: Any = None              # times this paper is cited (incoming)
    reference_count: Any = None             # references this paper makes (outgoing)
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
        # references the paper makes: a stored count if we have one (free), else count the
        # references sidecar (small — a paper's bibliography), else None → shown as "-".
        reference_count=(doc.get("s2_reference_count") or doc.get("referenced_works_count")
                         or (len(papis.citations.get_citations(doc))
                             if papis.citations.has_citations(doc) else None)),
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
        reference_count=cit.get("reference_count"),
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
# help popup — floating shortcut list (?)                                       #
# --------------------------------------------------------------------------- #
# Grouped into named sections. A ("§", "Title") entry renders as a bold section
# heading (with a blank line above). A ("", "") entry renders as a bare blank
# line — used inside a section for a visual sub-break (e.g. yank commands
# separated from other Commands).
HELP_LINES = [
    ("§",       "Navigation"),
    ("j k",     "Cursor down · up"),
    ("gg G",    "Top · bottom"),
    ("^d ^u",   "Half-page down · up"),
    ("^o",      "Back"),
    ("^i",      "Forward"),                # actual key event is `tab` (ctrl-i = 0x09)
    ("esc",     "Cancel / Back"),

    ("§",       "Search"),
    ("/",       "Filter (fzf · #tag prefix)"),
    ("f",       "Find papers (OpenAlex)"),

    ("§",       "Summary"),
    ("s",       "CSL toggle"),
    ("S",       "Pick style"),
    ("z",       "Toggle detail view"),

    ("§",       "Commands"),
    ("enter",   "Open · fetch · add"),
    ("n",       "Notes"),
    ("e",       "Edit info.yaml"),
    ("t",       "Tags"),
    ("c",       "Citations"),
    ("r",       "References"),
    ("dd",      "Delete entry"),
    ("",        ""),
    ("yc",      "Yank citekey (@ref)"),
    ("yd",      "Yank DOI"),
    ("yu",      "Yank source URL"),

    ("§",       "Application"),
    ("?",       "Help"),
    ("q",       "Quit"),
]


class HelpScreen(ModalScreen):
    CSS = """
    HelpScreen { align: center middle; background: $background 55%; }
    #help-box { width: 60; height: auto; border: round $primary;
                background: $surface; padding: 1 2; }
    """
    # Dismiss on esc (universal cancel) or ? (toggle-back mnemonic). Deliberately no
    # `q` — that's the app's quit key everywhere else and shouldn't be repurposed to
    # close a dialog. Modals use esc; the app uses q.
    BINDINGS = [
        Binding("escape", "dismiss", "close"),
        Binding("question_mark", "dismiss", "close"),
        Binding("shift+slash", "dismiss", "close"),
    ]

    def compose(self) -> ComposeResult:
        # Content width = box width (60) - round border (2) - horizontal padding (4).
        # Section titles are centered within this; key/label pairs stay left-justified.
        CONTENT_W = 54
        body = Text()
        first_section = True
        for key, label in HELP_LINES:
            if key == "§":
                if not first_section:
                    body.append("\n")               # blank line before every section but the first
                pad = max(0, (CONTENT_W - len(label)) // 2)
                body.append(f"{' ' * pad}{label}\n", style="dim")
                first_section = False
                continue
            if not key and not label:
                body.append("\n")                    # blank line within a section
                continue
            body.append(f"{key:8}", style="yellow")
            body.append(f"{label}\n")
        body.append("\nesc · ? to close", style="dim")
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
        Binding("y", "confirm", "confirm"),           # vim-like affirmative
        Binding("escape", "cancel", "cancel"),
        Binding("n", "cancel", "cancel"),             # vim-like negative
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
            yield Static("\n[y/enter] Delete   ·   [n/esc] Cancel",
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
            yield Static("Find papers (OpenAlex)", id="search-title")
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
    BINDINGS = [
        Binding("escape", "cancel", "cancel"),
        # j/k vim-navigation over the source table. No filter Input here (focus is
        # on the DataTable), so screen bindings for j/k fire cleanly. StyleScreen
        # and TagScreen deliberately skip this — their filter Inputs would consume
        # printable chars before the screen binding could fire.
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
    ]

    def action_cursor_down(self) -> None:
        self.query_one("#fetch-table", DataTable).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#fetch-table", DataTable).action_cursor_up()

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
        base = "bright_black" if ok is False else None
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
    """Pick the CSL reference style. vi-mode: j/k navigate, `/` opens a filter
    over id + title, enter picks, esc cancels (or clears the filter if one is
    open). The DataTable owns focus so plain letters don't slip into a filter
    Input by accident."""
    CSS = """
    StyleScreen { align: center middle; background: $background 60%; }
    #style-box { width: 88%; height: 80%; border: round $primary;
                 background: ansi_default; padding: 0 1; }
    #style-hint  { color: $text-muted; }
    #style-prompt { }
    #style-prompt.hidden { display: none; }
    #style-table { height: 1fr;
        scrollbar-size-vertical: 1;
        scrollbar-color: ansi_bright_black;
        scrollbar-background: ansi_default;
    }
    #style-table > .datatable--cursor {
        background: ansi_bright_black; color: ansi_bright_white;
    }
    """
    BINDINGS = [
        Binding("escape", "close",        "close"),
        Binding("j",      "cursor_down",  show=False),
        Binding("k",      "cursor_up",    show=False),
        Binding("down",   "cursor_down",  show=False),
        Binding("up",     "cursor_up",    show=False),
        Binding("g",      "top",          show=False),
        Binding("G",      "bottom",       show=False),
        Binding("slash",  "start_filter", show=False),
    ]
    _HINT_DEFAULT = "j/k · / filter · enter select · esc close"

    def __init__(self, styles: list[tuple[str, str]], current: str | None) -> None:
        super().__init__()
        self._all = styles              # [(id, title)] sorted by id
        self._current = current
        self._filtered: list[str] = []  # ids currently shown, row-aligned
        self._mode = "browse"           # "browse" | "filter"
        self._filter = ""

    def compose(self) -> ComposeResult:
        with Vertical(id="style-box"):
            yield Input(id="style-prompt", classes="hidden")
            yield DataTable(id="style-table", cursor_type="row", zebra_stripes=True)
            yield Static(self._HINT_DEFAULT, id="style-hint")

    def on_mount(self) -> None:
        t = self.query_one("#style-table", DataTable)
        t.add_column("Style (id)", key="id", width=46)
        t.add_column("Title", key="title")
        self._refill()
        t.focus()

    def _refill(self) -> None:
        t = self.query_one("#style-table", DataTable)
        t.clear()
        self._filtered.clear()
        needle = self._filter.lower().strip()
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
                      Text(title, style="dim"), key=sid)
            self._filtered.append(sid)
        if self._filtered:
            t.move_cursor(row=cursor)

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if self._mode != "browse" and action in (
            "cursor_down", "cursor_up", "top", "bottom", "start_filter",
        ):
            return None
        return True

    def action_cursor_down(self) -> None:
        self.query_one("#style-table", DataTable).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#style-table", DataTable).action_cursor_up()

    def action_top(self) -> None:
        self.query_one("#style-table", DataTable).action_scroll_top()

    def action_bottom(self) -> None:
        self.query_one("#style-table", DataTable).action_scroll_bottom()

    def action_start_filter(self) -> None:
        self._mode = "filter"
        p = self.query_one("#style-prompt", Input)
        p.value = self._filter
        p.placeholder = "filter by id or title"
        p.remove_class("hidden")
        p.focus()

    def _close_prompt(self, clear: bool = False) -> None:
        self._mode = "browse"
        p = self.query_one("#style-prompt", Input)
        if clear:
            self._filter = ""
            self._refill()
        p.value = ""
        p.add_class("hidden")
        self.query_one("#style-table", DataTable).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "style-prompt" or self._mode != "filter":
            return
        self._filter = event.value
        self._refill()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # Enter in the filter prompt: hide the prompt, keep the filter applied,
        # return focus to the table so j/k works on the narrowed set.
        if event.input.id != "style-prompt":
            return
        event.stop()
        self._close_prompt(clear=False)

    def on_data_table_row_selected(self, event) -> None:
        # DataTable owns enter → RowSelected. Route to pick.
        if event.data_table.id == "style-table":
            self._pick()

    def _pick(self) -> None:
        t = self.query_one("#style-table", DataTable)
        i = t.cursor_row
        if i is not None and 0 <= i < len(self._filtered):
            self.dismiss(self._filtered[i])
        else:
            self.bell()

    def action_close(self) -> None:
        # esc in filter mode → clear + close prompt; esc in browse mode → cancel.
        if self._mode != "browse":
            self._close_prompt(clear=True)
            return
        self.dismiss(None)


# --------------------------------------------------------------------------- #
# tag editor — vocabulary-first: assign/unassign, add, rename, delete globally  #
# --------------------------------------------------------------------------- #
class TagPromptInput(Input):
    """The bottom prompt Input inside TagScreen. When `substitute_space` is on
    (add / rename modes), pressing spacebar inserts a `-` instead — papis' own
    tag helper (papis.web.tags.TAGS_SPLIT_RX = r'\\s*[,\\s]\\s*') treats
    whitespace as a tag separator, so tags with spaces would fragment when any
    non-bib papis tool loads the doc. Substituting at input time keeps the
    on-disk vocabulary papis-clean without a toast."""
    substitute_space = False

    def on_key(self, event) -> None:
        if self.substitute_space and event.key == "space":
            pos = self.cursor_position
            self.value = self.value[:pos] + "-" + self.value[pos:]
            self.cursor_position = pos + 1
            event.prevent_default()
            event.stop()


class TagScreen(ModalScreen["None"]):
    """Tag editor. The default view lists every tag in the vocabulary — the union of
    tags currently attached to any library paper and the user's own `custom_tags`
    (persisted in bib's state.json so orphan tags survive a restart). Each row shows
    an assignment marker (● = attached to the currently selected paper) and a paper
    count.

    j/k navigate. `/` opens a filter over the visible list. `space` toggles the
    highlighted tag on the current paper. `a` prompts for a name and adds a new
    tag to the vocabulary (not assigned to any paper). `r` renames the highlighted
    tag globally (across every paper that has it, plus the custom-tags list). `dd`
    (chord) then y/n confirm deletes the highlighted tag from the vocabulary AND
    from every paper that carries it. `esc` closes."""
    CSS = """
    TagScreen { align: center middle; background: $background 60%; }
    #tag-box { width: 60%; height: 70%; border: round $primary;
               background: ansi_default; padding: 0 1; }
    #tag-title { color: $accent; }
    #tag-hint  { color: $text-muted; }
    #tag-prompt { }
    #tag-prompt.hidden { display: none; }
    #tag-table { height: 1fr;
        scrollbar-size-vertical: 1;
        scrollbar-color: ansi_bright_black;
        scrollbar-background: ansi_default;
    }
    #tag-table > .datatable--cursor {
        background: ansi_bright_black; color: ansi_bright_white;
    }
    """
    BINDINGS = [
        Binding("escape", "close", "close"),
        Binding("j",      "cursor_down", show=False),
        Binding("k",      "cursor_up",   show=False),
        Binding("down",   "cursor_down", show=False),
        Binding("up",     "cursor_up",   show=False),
        Binding("g",      "top",         show=False),
        Binding("G",      "bottom",      show=False),
        Binding("slash",  "start_filter", show=False),
        Binding("a",      "start_add",    show=False),
        Binding("r",      "start_rename", show=False),
        Binding("d",      "delete_chord", show=False),
        Binding("space",  "toggle",       show=False),
    ]
    _CHORD_TIMEOUT = 0.3
    _HINT_DEFAULT = "j/k · / filter · a add · r rename · dd delete · space toggle · esc close"

    def __init__(self, app_ref, node) -> None:
        super().__init__()
        self._app = app_ref
        self._node = node
        self._mode = "browse"         # "browse" | "filter" | "add" | "rename"
        self._filter = ""
        self._rename_from = ""
        self._filtered: list[str] = []
        self._pending_d = False
        self._pending_timer = None
        self._hint_timer = None

    def compose(self) -> ComposeResult:
        with Vertical(id="tag-box"):
            yield TagPromptInput(id="tag-prompt", classes="hidden")
            yield DataTable(id="tag-table", cursor_type="row", zebra_stripes=True)
            yield Static(self._HINT_DEFAULT, id="tag-hint")

    def on_mount(self) -> None:
        t = self.query_one("#tag-table", DataTable)
        t.add_column("Tag", key="tag")
        t.add_column("Papers", key="count")
        self._refill()
        t.focus()                     # start on the table so j/k works immediately

    # ---- data helpers ------------------------------------------------------
    def _current_paper_tags(self) -> set[str]:
        return set(doc_tags(self._node.doc))

    def _refill(self) -> None:
        t = self.query_one("#tag-table", DataTable)
        t.clear()
        self._filtered.clear()
        counts = self._app._library_tag_counts()
        current = self._current_paper_tags()
        low = self._filter.strip().lower()
        for tag in self._app._all_vocab_tags():
            if low and low not in tag.lower():
                continue
            on = tag in current
            style = "bright_blue" if on else "dim"
            mark  = "● " if on else "  "
            t.add_row(Text(mark + tag, style=style),
                      Text(str(counts.get(tag, 0)), style=style),
                      key=tag)
            self._filtered.append(tag)
        if self._filtered:
            t.move_cursor(row=0)

    def _cursor_to(self, tag: str) -> None:
        for i, v in enumerate(self._filtered):
            if v == tag:
                self.query_one("#tag-table", DataTable).move_cursor(row=i)
                return

    def _selected_tag(self) -> str | None:
        t = self.query_one("#tag-table", DataTable)
        i = t.cursor_row
        if i is None or not (0 <= i < len(self._filtered)):
            return None
        return self._filtered[i]

    # ---- prompt (Input) mode helpers ---------------------------------------
    def _open_prompt(self, mode: str, placeholder: str, initial: str = "") -> None:
        self._mode = mode
        p = self.query_one("#tag-prompt", TagPromptInput)
        p.substitute_space = mode in ("add", "rename")   # papis: no spaces in tags
        p.value = initial
        p.placeholder = placeholder
        p.remove_class("hidden")
        p.focus()

    def _close_prompt(self, refill: bool = True) -> None:
        self._mode = "browse"
        p = self.query_one("#tag-prompt", TagPromptInput)
        p.substitute_space = False
        p.value = ""
        p.add_class("hidden")
        self._filter = ""
        if refill:
            self._refill()
        self.query_one("#tag-table", DataTable).focus()

    # ---- gating: browse-mode bindings are inert while a prompt is up -------
    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if self._mode != "browse" and action in (
            "cursor_down", "cursor_up", "top", "bottom",
            "start_filter", "start_add", "start_rename", "delete_chord",
            "toggle",
        ):
            return None
        return True

    # ---- nav ---------------------------------------------------------------
    def action_cursor_down(self) -> None:
        self.query_one("#tag-table", DataTable).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#tag-table", DataTable).action_cursor_up()

    def action_top(self) -> None:
        self.query_one("#tag-table", DataTable).action_scroll_top()

    def action_bottom(self) -> None:
        self.query_one("#tag-table", DataTable).action_scroll_bottom()

    # ---- filter / add / rename prompts -------------------------------------
    def action_start_filter(self) -> None:
        self._open_prompt("filter", "filter tags", self._filter)

    def action_start_add(self) -> None:
        self._open_prompt("add", "add tag: ")

    def action_start_rename(self) -> None:
        tag = self._selected_tag()
        if not tag:
            self.bell()
            return
        self._rename_from = tag
        self._open_prompt("rename", f"rename '{tag}' to: ", tag)

    # ---- destructive delete (dd chord + ConfirmScreen) ---------------------
    def action_delete_chord(self) -> None:
        tag = self._selected_tag()
        if not tag:
            self.bell()
            return
        if self._pending_d:
            self._clear_pending_d()
            self._confirm_delete(tag)
            return
        self._pending_d = True
        n = self._app._library_tag_counts().get(tag, 0)
        # timeout=0 pins the chord prompt — _clear_pending_d handles the reset
        # after 300ms so the "d…" message doesn't disappear mid-chord.
        self._hint(f"d… (press d again to delete '{tag}' from "
                   f"{n} paper{'' if n == 1 else 's'})", timeout=0)
        self._pending_timer = self.set_timer(self._CHORD_TIMEOUT, self._clear_pending_d)

    def _clear_pending_d(self) -> None:
        if self._pending_timer is not None:
            self._pending_timer.stop()
            self._pending_timer = None
        if self._pending_d:
            self._pending_d = False
            self._hint_default()

    def _confirm_delete(self, tag: str) -> None:
        n = self._app._library_tag_counts().get(tag, 0)
        detail = (f"Removes '{tag}' from the vocabulary and from "
                  f"{n} paper{'' if n == 1 else 's'}.\n"
                  "This cannot be undone.")
        self.app.push_screen(
            ConfirmScreen(f"Delete tag '{tag}' ?", detail),
            lambda ok: self._do_delete(tag) if ok else None,
        )

    def _do_delete(self, tag: str) -> None:
        try:
            self._app._delete_tag_globally(tag)
        except Exception as e:                    # noqa: BLE001
            self.notify(f"delete failed: {e}", severity="error")
            return
        self._refill()
        self._hint(f"deleted '{tag}'")

    # ---- toggle assignment on the current paper ----------------------------
    def action_toggle(self) -> None:
        tag = self._selected_tag()
        if not tag:
            self.bell()
            return
        current = self._current_paper_tags()
        if tag in current:
            current.discard(tag)
        else:
            current.add(tag)
        self._app._apply_tags(self._node, sorted(current))
        self._refill()
        self._cursor_to(tag)

    # ---- input events ------------------------------------------------------
    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "tag-prompt":
            return
        if self._mode == "filter":
            self._filter = event.value
            self._refill()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "tag-prompt":
            return
        event.stop()
        value = event.value.strip()
        mode = self._mode
        if mode == "filter":
            # Hide the prompt but keep the filter buffer applied.
            p = self.query_one("#tag-prompt", TagPromptInput)
            p.add_class("hidden")
            self._mode = "browse"
            self.query_one("#tag-table", DataTable).focus()
            return
        # add/rename: paste-safe — sanitize whitespace runs to a single `-`
        # (matches the live spacebar substitution). No-op if the field already
        # obeyed the rule via keystrokes.
        value = re.sub(r"\s+", "-", value)
        if mode == "add":
            self._close_prompt()
            if value:
                self._app._add_custom_tag(value)
                self._refill()
                self._cursor_to(value)
                self._hint(f"added '{value}'")
            return
        if mode == "rename":
            old = self._rename_from
            self._close_prompt()
            if value and value != old:
                self._app._rename_tag_globally(old, value)
                self._refill()
                self._cursor_to(value)
                self._hint(f"renamed '{old}' → '{value}'")
            return

    def _hint(self, msg: str, timeout: float = 1.5) -> None:
        """Show a transient status line under the tag table. After `timeout`
        seconds the row reverts to the keyboard-shortcut cheat sheet. Passing
        timeout=0 pins the message until the next _hint call."""
        self.query_one("#tag-hint", Static).update(msg)
        if self._hint_timer is not None:
            self._hint_timer.stop()
            self._hint_timer = None
        if timeout > 0:
            self._hint_timer = self.set_timer(timeout, self._hint_default)

    def _hint_default(self) -> None:
        self._hint_timer = None
        self.query_one("#tag-hint", Static).update(self._HINT_DEFAULT)

    def action_close(self) -> None:
        # esc in a prompt cancels the prompt; esc in browse mode dismisses the modal.
        if self._mode != "browse":
            self._close_prompt()
            return
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
# ctrl-y opens the picker. BIB_CSL_STYLE (or legacy PAPIS_GRAPH_CSL_STYLE) overrides.
_STYLE_DIR = os.fspath(importlib.resources.files(__package__) / "styles" / "csl")
_DEFAULT_STYLE_ID = "taylor-and-francis-harvard-x"

# Small persisted UI state (CSL mode + chosen style), so choices survive a restart. papers owns its
# own config dir; best-effort (never fatal). Migrates once from the old papis-config location.
_CONFIG_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "bib")
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
    env = os.environ.get("BIB_CSL_STYLE") or os.environ.get("PAPIS_GRAPH_CSL_STYLE")
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
        t.append(f"\n{label}: ", style="bright_black")
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
        t.append(badge, style="bright_black")
        t.append("\n")
        t.append(n.author or "—", style="italic")
        rows += 1
        venue = compose_venue(n)
        if venue:
            maxw = max(24, self.size.width - 2)
            if len(venue) > maxw:
                venue = venue[: maxw - 1] + "…"
            t.append("\n")
            t.append(venue, style="dim")
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
            t.append("  ○ not in library", style="bright_black")
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
        tags = doc_tags(n.doc)
        if tags or n.topics or n.keywords:
            t.append("\n")                        # blank line: separate head from Topics/Keywords
            rows += 1
        if tags:
            self._line(t, "Tags", tags, "bright_blue")   # your own tags — accent, not muted
            rows += 1
        if n.topics:
            self._line(t, "Topics", n.topics, "dim")   # muted, like the abstract (no highlight)
            rows += 1
        if n.keywords:
            self._line(t, "Keywords", n.keywords, "dim")
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
            t.append(abstract, style="italic")
        body.update(t)
        ref_left.update(f"@{n.ref}" if n.ref else "")
        # influential · cited-by, centered. Infl in the table's yellow (only when it has data;
        # the "-" placeholder stays muted like the rest). A real 0 shows "0".
        cited = fmt_count(n.citation_count) or "-"
        infl_raw = fmt_count(n.influential_count)
        mid = Text()
        mid.append(infl_raw or "-", style="bright_blue" if infl_raw else "")
        mid.append("  ·  ")
        mid.append(cited)
        ref_mid.update(mid)


# --------------------------------------------------------------------------- #
# filter prompt — a slim Input that pops in above the status row on `/`         #
# --------------------------------------------------------------------------- #
class TagPicker(OptionList):
    """Inline autocomplete popup that appears above the filter Input while the cursor
    sits inside a `#<partial>` token. Populated with library tags whose name contains
    the current prefix, ranked by descending count. Focus stays on the filter Input;
    this widget is display-only — nav (ctrl+j/k, arrows) and accept (enter/tab) come
    from FilterInput.on_key while the popup is visible."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.can_focus = False


class FilterInput(Input):
    """The bottom-row filter prompt. Its own esc binding routes to the app so the app
    can hide the widget AND clear the filter atomically. Everything else (typing,
    backspace, enter/submit) is inherited from Input; the app owns the Changed +
    Submitted message handlers so filter state stays on the app, not this widget.

    While the TagPicker popup is visible (cursor inside a `#…` token), on_key steals
    ctrl+j/k + arrows for popup nav and enter/tab for accept; typing letters still
    reaches the Input and re-narrows both the library filter and the popup set."""

    BINDINGS = [Binding("escape", "app.close_filter_and_clear", show=False)]

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        # While the tag picker is up, esc dismisses the picker first (handled in
        # on_key). Suppress the app's close_filter_and_clear binding so it doesn't
        # race and close the filter along with the picker.
        if action == "app.close_filter_and_clear":
            picker = self.app.query_one("#tag-picker", TagPicker)
            if not picker.has_class("hidden"):
                return None
        return True

    def on_key(self, event) -> None:
        picker = self.app.query_one("#tag-picker", TagPicker)
        if picker.has_class("hidden"):
            return
        key = event.key
        if key == "escape":
            picker.add_class("hidden")
            event.prevent_default(); event.stop()
            return
        if key in ("enter", "tab"):
            if picker.option_count and picker.highlighted is not None:
                opt = picker.get_option_at_index(picker.highlighted)
                self.app._complete_tag_token(opt.id or "")
            event.prevent_default(); event.stop()
            return
        if key in ("ctrl+j", "down"):
            picker.action_cursor_down()
            event.prevent_default(); event.stop()
            return
        if key in ("ctrl+k", "up"):
            picker.action_cursor_up()
            event.prevent_default(); event.stop()
            return


# --------------------------------------------------------------------------- #
# app                                                                          #
# --------------------------------------------------------------------------- #
class BibApp(App):
    TITLE = "bib"
    CSS = """
    Screen { layout: vertical; }
    #card { height: 19; padding: 0 1; }                    /* borderless — no box-drawing lines */
    #card.expanded { height: 1fr; }          /* z: Details panel fills the screen */
    #card-body { height: 1fr; }              /* metadata + abstract */
    #card-ref { height: 1; }                 /* docked bottom row */
    #card-ref-left  { width: 1fr; color: $text-muted; }   /* @ref citation key (bottom-left) */
    #card-ref-mid   { width: auto; color: $text-muted; }  /* Cited-by / Infl (centered) */
    #card-ref-right { width: 1fr; }          /* spacer, balances the left so mid is centered */
    /* bordered table (dim grey = topics-value shade); the card above stays borderless. */
    #center { height: 1fr; border: round ansi_default 50%; scrollbar-size-vertical: 1;
              scrollbar-color: ansi_bright_black; scrollbar-background: ansi_default; }
    #center.hidden { display: none; }        /* z: table hidden while Details is expanded */
    /* filter: hidden by default, revealed on '/' and focused. A 3-row bordered Input to
       match the modal aesthetics; live-narrows the visible rows as the user types. */
    #filter { height: 3; border: round $primary; }
    #filter.hidden { display: none; }
    /* tag picker: pops in above the filter while cursor sits in a `#…` token */
    #tag-picker { height: auto; max-height: 8; border: round $primary; }
    #tag-picker.hidden { display: none; }
    /* Focus stays on the filter Input, so the OptionList never gets the "focused"
       highlight — force the highlighted row to render like it is anyway, else the
       cursor is invisible and the user can't tell what enter would accept. */
    #tag-picker > .option-list--option-highlighted {
        background: ansi_bright_black;
        color: ansi_bright_white;
        text-style: bold;
    }
    #status { height: 1; color: $text-muted; padding: 0 1; }
    #hint   { height: 1; color: $warning;    padding: 0 1; }   /* ephemeral messages */
    DataTable > .datatable--cursor { background: ansi_bright_black; color: ansi_bright_white; }
    /* ANSI theme paints the header on ansi_bright_blue; force dark text so it stays legible
       (terminal default fg is light in dark mode → too low contrast on the bright header). */
    DataTable > .datatable--header { color: ansi_black; }
    """

    ENABLE_COMMAND_PALETTE = False   # unused; we roll our own key surface

    # Vim-like single-mode keymap. Typing letters DOES NOT filter — press '/' to open the
    # filter prompt. Multi-char sequences (gg, gp, dd, yc, yd, yu) are handled in `on_key`
    # via a 300ms pending-key state, not by Binding (Textual has no chord support).
    BINDINGS = [
        # navigation (DataTable owns up/down/enter/pageup/pagedown natively; j/k added here)
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
        Binding("G", "bottom", show=False),
        Binding("ctrl+d", "half_page_down", show=False),
        Binding("ctrl+u", "half_page_up", show=False),
        # frame stack (jump-list metaphor). Bind `tab`, not `ctrl+i`: terminals send byte
        # 0x09 for both and Textual normalizes to `tab`, so a ctrl+i binding never matches.
        # priority=True beats the default Screen `tab → focus_next` binding.
        Binding("ctrl+o", "back", "Back", priority=True),
        Binding("tab", "forward", "Forward", priority=True),
        # filter + whole-corpus search + help
        Binding("slash", "open_filter", "Filter"),
        Binding("f", "search", "Search"),
        Binding("question_mark", "help", "Help"),
        Binding("shift+slash", "help", show=False),   # alt encoding of '?'
        # display
        Binding("s", "toggle_csl", "CSL"),
        Binding("S", "pick_style", "Style"),
        Binding("z", "toggle_details", "Zoom"),
        # paper actions (c retains priority=True so ctrl+c-style muscle memory isn't
        # ambiguous with any downstream widget that also reacts to plain 'c')
        Binding("n", "notes", "Notes"),
        Binding("e", "edit", "Edit"),
        Binding("t", "tags", "Tags"),
        Binding("c", "promote_right", "Cites", priority=True),
        Binding("r", "promote_left", "Refs"),
        # quit
        Binding("q", "quit", "Quit"),
    ]

    # Multi-char sequences (first-key → set of valid second keys → action name). Consulted
    # by on_key when a pending-key is active. Any second key not listed here cancels the
    # pending state and dispatches normally through Bindings.
    _CHORDS: "dict[tuple[str, str], str]" = {
        ("g", "g"): "top",
        ("d", "d"): "delete",
        ("y", "c"): "yank_citekey",
        ("y", "d"): "yank_doi",
        ("y", "u"): "yank_url",
    }
    _CHORD_STARTERS = frozenset({"g", "d", "y"})
    _CHORD_TIMEOUT = 0.3   # seconds — matches Craig's spec for double-tap window

    def __init__(self) -> None:
        super().__init__()
        # Use Textual's ANSI theme so the app follows the terminal's own colour scheme (foot's
        # wallpaper-driven palette) instead of a fixed design-system palette: fg/bg become the
        # terminal's, and accents map to the ANSI palette (blue/green/red/yellow). Named colours
        # in Rich styles (cyan/yellow/green/red) then pass through as ANSI too.
        self.theme = "ansi-dark"
        self.doi_index: dict[str, Document] = {}
        self.library: list[Node] = []
        self.stack: list[Frame] = []
        self.sp = 0                     # stack pointer (for back/forward)
        self.filter_buf = ""
        self._details_expanded = False          # z: details panel full-screen (table hidden)
        self._client: gc.Client | None = None   # lazy shared async client for live fetch
        self._hint_timer = None                 # transient bottom-row message → auto-clear
        self._pending: str | None = None        # first key of a pending chord (g/d/y)
        self._pending_timer = None              # cancels the pending chord after 300ms

    # ---- layout ----
    def compose(self) -> ComposeResult:
        yield TopCard(id="card")
        yield DataTable(id="center", cursor_type="row", zebra_stripes=True)
        yield TagPicker(id="tag-picker", classes="hidden")
        yield FilterInput(placeholder="filter · #tag prefix",
                          id="filter", classes="hidden")
        yield Static(id="status")
        yield Static(id="hint")

    def on_mount(self) -> None:
        db = papis.database.get()
        docs = db.get_all_documents()
        self.doi_index = {str(d["doi"]).lower(): d for d in docs if d.get("doi")}
        self.library = sorted((node_from_doc(d) for d in docs),
                              key=lambda n: str(n.year or ""))
        table = self.query_one("#center", DataTable)
        table.add_columns(" ", "Year", "Author", "Title", "Infl", "Citations", "References")
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
            # tag filter: `#a #b` -> entries whose tags contain a prefix-match for EVERY token
            wanted = [w for w in (tok.lstrip("#").lower() for tok in buf.split()) if w]
            if not wanted:
                return nodes
            def _match(n: Node) -> bool:
                tags = [t.lower() for t in doc_tags(n.doc)]
                return all(any(t.startswith(w) for t in tags) for w in wanted)
            return [n for n in nodes if _match(n)]
        from rapidfuzz import fuzz
        scored = [(fuzz.partial_ratio(buf.lower(), f"{n.title} {n.author}".lower()), n)
                  for n in nodes]
        return [n for s, n in sorted(scored, key=lambda x: -x[0]) if s > 55]

    def _row_cells(self, n: Node) -> tuple:
        """The six DataTable cells for one node — shared by full render and live append."""
        style = None if n.in_library else "bright_black"
        t = n.title or "(untitled)"
        if len(t) > 72:
            t = t[:71] + "…"
        cited = fmt_count(n.citation_count)
        infl = fmt_count(n.influential_count)
        refs = fmt_count(n.reference_count)
        # Leading glyph colour = download status (independent of the row's in-library grey):
        # grey when the content file isn't on disk, else its normal colour (yellow for an
        # influential citation edge, default otherwise).
        if not n.downloaded:
            glyph_style = "bright_black"
        elif n.is_influential:
            glyph_style = "yellow"
        else:
            glyph_style = ""
        return (
            Text(type_glyph(n.type), style=glyph_style),
            Text(str(n.year or ""), style=style),
            Text(n.first_author, style=style),
            Text(t, style=style),
            Text(infl, style="bright_blue" if infl else (style or "bright_black"), justify="right"),
            Text(cited, style=style or "bright_black", justify="right"),
            Text(refs, style=style or "bright_black", justify="right"),
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

    def _hint(self, msg: str, timeout: float = 1.2) -> None:
        """Show a transient message in the bottom hint row. Replaces the bell — a
        muted-yellow line that says why an action was a no-op, then self-clears so it
        never lingers in the user's field of view. Passing timeout=0 pins the message
        until _hint_clear() is called (used for pending-key indicators like 'd…')."""
        if self._hint_timer is not None:
            self._hint_timer.stop()
            self._hint_timer = None
        self.query_one("#hint", Static).update(msg)
        if timeout > 0:
            self._hint_timer = self.set_timer(timeout, self._hint_clear)

    def _hint_clear(self) -> None:
        if self._hint_timer is not None:
            self._hint_timer.stop()
            self._hint_timer = None
        self.query_one("#hint", Static).update("")

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
    # ---- input dispatch ----
    def check_action(self, action: str, parameters: tuple) -> "bool | None":
        """Gate app-level bindings for two reasons:

        1. While a modal is up, a stray 'n'/'s'/etc. typed into a picker must NOT
           fire the app-level action. Modals own the screen; their own BINDINGS
           still work because they belong to the modal, not the app. `quit` is
           whitelisted — q means quit-the-app everywhere (except inside a text
           input, which consumes the char as text before bindings dispatch).
        2. While a chord is pending (user pressed 'y'/'d'/'g' and we're waiting for
           the second key), Textual would otherwise dispatch the second key's
           binding (e.g. 'c' → action_promote_right) BEFORE our on_key gets a
           chance to complete the chord (e.g. 'yc' → action_yank_citekey). Gate
           all app bindings so on_key runs the chord machinery instead. If the
           second key isn't a valid continuation, on_key re-dispatches it manually.
        """
        if len(self.screen_stack) > 1:
            if action == "quit":
                return True                          # q quits from within modals too
            return None
        if self._pending is not None:
            return None
        # Tab is bound app-level (forward through the frame history, priority=True)
        # so it beats FilterInput.on_key. When the tag picker is up (user is in
        # `/`, cursor inside a `#…` token), tab must complete the highlighted tag
        # instead of firing forward-history. Gate the binding so FilterInput.on_key
        # gets the key and handles completion.
        if action == "forward" and isinstance(self.focused, FilterInput):
            try:
                picker = self.query_one("#tag-picker", TagPicker)
            except Exception:                        # noqa: BLE001
                picker = None
            if picker is not None and not picker.has_class("hidden"):
                return None
        return True

    def on_key(self, event) -> None:
        """Single-mode key dispatch. Order:
          1. modal owns screen → bail (Bindings gated by check_action, on_key silent)
          2. filter Input has focus → bail (Input consumes typing)
          3. escape → cancel current input op, or fall through to Back
          4. chord continuation? complete the chord, or re-dispatch second key
          5. chord starter (g/d/y)? arm pending + 300ms timer
        Everything else is a Binding.
        """
        if len(self.screen_stack) > 1:
            return
        if isinstance(self.focused, FilterInput):
            return

        key = event.key

        # esc order: filter open/applied → clear · pending → clear · else → Back
        # (Craig-approved reversal on top of the earlier "esc never navigates" rule:
        # a plain esc with nothing to cancel now feels like ctrl+o, which matches user
        # expectation when drilled into a citations frame.)
        if key == "escape":
            filt = self.query_one("#filter", FilterInput)
            if (not filt.has_class("hidden")) or self.filter_buf:
                self._close_filter_and_clear()
            elif self._pending is not None:
                self._clear_pending()
            elif self.sp > 0:
                self.action_back()
            else:
                self._hint("nothing to cancel")
            event.prevent_default(); event.stop()
            return

        # chord continuation? Bindings are gated by check_action while _pending, so
        # this handler owns the second-key dispatch.
        if self._pending is not None:
            prev = self._pending
            self._clear_pending()
            action = self._CHORDS.get((prev, key))
            if action is not None:
                getattr(self, f"action_{action}")()
                event.prevent_default(); event.stop()
                return
            # Non-completion: re-dispatch the second key to its would-be Binding
            # manually (since check_action gated it while pending). If the key
            # isn't bound, silently no-op.
            for b in self.BINDINGS:
                if b.key == key and hasattr(self, f"action_{b.action}"):
                    getattr(self, f"action_{b.action}")()
                    event.prevent_default(); event.stop()
                    return
            return

        # chord starter? swallow the key and arm the pending state + 300ms clear timer.
        if key in self._CHORD_STARTERS:
            self._pending = key
            self._hint(f"{key}…", timeout=0)             # pinned until chord completes/cancels
            self._pending_timer = self.set_timer(self._CHORD_TIMEOUT, self._clear_pending)
            event.prevent_default(); event.stop()
            return

    def _clear_pending(self) -> None:
        if self._pending_timer is not None:
            self._pending_timer.stop()
            self._pending_timer = None
        if self._pending is not None:
            self._pending = None
            self._hint_clear()

    # ---- filter prompt ----
    def action_open_filter(self) -> None:
        """'/' → reveal the filter Input, seed with current filter_buf, take focus.
        Re-opening while a filter is applied edits-in-place (buf preserved)."""
        filt = self.query_one("#filter", FilterInput)
        filt.value = self.filter_buf                    # seed for edit-in-place
        filt.remove_class("hidden")
        filt.focus()

    def action_close_filter_and_clear(self) -> None:
        """esc from within the filter Input (or from the main view when a filter is
        open/applied). Hides the prompt, clears the buffer, restores focus to the
        table, and re-renders so the full list is visible."""
        filt = self.query_one("#filter", FilterInput)
        filt.value = ""
        filt.add_class("hidden")
        self.filter_buf = ""
        self._render_center()
        self.query_one("#center", DataTable).focus()

    def _close_filter_and_clear(self) -> None:
        # Non-action alias — used from on_key so we don't have to hop through the
        # action machinery. Same body.
        self.action_close_filter_and_clear()

    def on_input_changed(self, event: Input.Changed) -> None:
        """Live-filter as the user types into the '/' prompt. Ignore Inputs from
        modals (their own screens receive their own on_input_changed)."""
        if event.input.id == "filter":
            self.filter_buf = event.value
            self._render_center()
            self._update_tag_picker()

    # ---- tag picker (inline autocomplete when cursor is in a `#…` token) ----
    def _tag_token_at_cursor(self) -> tuple[str, int, int] | None:
        """(prefix-without-hash, token_start, token_end) if the space-delimited token
        under the filter's cursor starts with '#'; None otherwise."""
        filt = self.query_one("#filter", FilterInput)
        buf, cur = filt.value, filt.cursor_position
        start = buf.rfind(" ", 0, cur) + 1
        end = buf.find(" ", cur)
        if end == -1:
            end = len(buf)
        tok = buf[start:end]
        if not tok.startswith("#"):
            return None
        return tok[1:], start, end

    def _update_tag_picker(self) -> None:
        """Reveal + populate the picker when in a `#…` token; hide otherwise. Ranks
        matches by descending library count, then alphabetical."""
        picker = self.query_one("#tag-picker", TagPicker)
        got = self._tag_token_at_cursor()
        if got is None:
            picker.add_class("hidden")
            return
        prefix = got[0].lower()
        # Recompute per keystroke — cheap over `self.library`, and picks up any
        # tags added/renamed via TagScreen since the app started.
        counts = Counter(t for n in self.library for t in doc_tags(n.doc))
        matches = sorted(
            ((tag, cnt) for tag, cnt in counts.items()
             if prefix in tag.lower()),
            key=lambda x: (-x[1], x[0]),
        )[:20]
        if not matches:
            picker.add_class("hidden")
            return
        picker.clear_options()
        for tag, cnt in matches:
            picker.add_option(Option(f"{tag}  ({cnt})", id=tag))
        picker.highlighted = 0
        picker.remove_class("hidden")

    def _complete_tag_token(self, tag: str) -> None:
        """Enter/tab from the picker: replace the `#<partial>` at cursor with
        `#<full-tag> ` (trailing space so more filter terms can follow)."""
        got = self._tag_token_at_cursor()
        if got is None or not tag:
            return
        _, start, end = got
        filt = self.query_one("#filter", FilterInput)
        buf = filt.value
        filt.value = f"{buf[:start]}#{tag} {buf[end:]}"
        filt.cursor_position = start + len(tag) + 2
        # setting .value fires on_input_changed → _update_tag_picker hides the popup
        # (cursor is now past a space, so no `#` token at cursor).

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter in the filter prompt: hide the widget but KEEP the filter applied,
        return focus to the table so j/k works on the filtered set. esc-instead would
        both hide and clear (action_close_filter_and_clear)."""
        if event.input.id == "filter":
            self.query_one("#filter", FilterInput).add_class("hidden")
            self.query_one("#center", DataTable).focus()

    # ---- navigation helpers ----
    def action_cursor_down(self) -> None:
        self.query_one("#center", DataTable).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#center", DataTable).action_cursor_up()

    def action_top(self) -> None:
        t = self.query_one("#center", DataTable)
        if t.row_count:
            t.move_cursor(row=0)

    def action_bottom(self) -> None:
        t = self.query_one("#center", DataTable)
        if t.row_count:
            t.move_cursor(row=t.row_count - 1)

    def action_half_page_down(self) -> None:
        t = self.query_one("#center", DataTable)
        step = max(1, t.scrollable_content_region.height // 2)
        for _ in range(step):
            t.action_cursor_down()

    def action_half_page_up(self) -> None:
        t = self.query_one("#center", DataTable)
        step = max(1, t.scrollable_content_region.height // 2)
        for _ in range(step):
            t.action_cursor_up()

    # ---- yank ----
    # Uses Textual's App.copy_to_clipboard(), which drives OSC 52 through the same
    # terminal driver that owns the alternate screen. A hand-rolled sys.stdout write
    # goes nowhere while Textual is running — the driver holds the tty.
    def action_yank_citekey(self) -> None:
        """Yank the citekey in @citation form, ready to paste into a Typst/pandoc
        manuscript without further shaping."""
        n = self._selected_library_node()
        if n is None:
            return
        cite = f"@{n.ref}" if n.ref else ""
        self.copy_to_clipboard(cite)
        self._hint(f"yanked citekey: {cite}")

    def action_yank_doi(self) -> None:
        n = self._selected_node()
        if n is None:
            self._hint("no row selected"); return
        if not n.doi:
            self._hint("no DOI on selected row"); return
        self.copy_to_clipboard(n.doi)
        self._hint(f"yanked doi: {n.doi}")

    def action_yank_url(self) -> None:
        """Yank the primary source URL for the selected row. Prefers the DOI URL when
        present (canonical for a paper); falls back to OpenAlex id URL for grey nodes
        that lack a DOI (books/reports OpenAlex indexes shallowly)."""
        n = self._selected_node()
        if n is None:
            self._hint("no row selected"); return
        url = None
        if n.doi:
            url = f"https://doi.org/{n.doi}"
        elif n.ids.get("openalex_id"):
            url = f"https://openalex.org/{n.ids['openalex_id']}"
        if url is None:
            self._hint("no URL on selected row"); return
        self.copy_to_clipboard(url)
        self._hint(f"yanked url: {url}")

    # ---- actions ----
    def _promote(self, nodes: list[Node], title: str) -> None:
        if not nodes:
            # title looks like "Citations of <label>" / "References of <label>"; keep the
            # first word so the hint reads "no citations" / "no references".
            self._hint(f"no {title.split()[0].lower()}")
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
        label = "Citations" if direction == "cited" else "References"
        if n.doc is not None:
            cites, cited = self._neighbors(n)
            self._promote(cited if direction == "cited" else cites, f"{label} of {n.label}")
            return
        # grey neighbor — nothing on disk; walk the graph live
        if not (n.ids.get("openalex_id") or n.doi or n.title):
            self._hint("no source to walk from (no doi / openalex id / title)")
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
            self._hint("already at oldest frame")

    def action_forward(self) -> None:
        if self.sp < len(self.stack) - 1:
            self.sp += 1
            self.filter_buf = ""
            self._render_center()
        else:
            self._hint("already at newest frame")

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

    def _selected_library_node(self) -> Node | None:
        """Return the selected node iff it's a library entry with a papis doc. Otherwise
        emit the precise reason to the hint row and return None. Used by every action
        that only makes sense on a library row (edit / notes / tags / delete)."""
        n = self._selected_node()
        if n is None:
            self._hint("no row selected")
            return None
        if not n.in_library or n.doc is None:
            self._hint("not in library — press enter to add")
            return None
        return n

    def action_edit(self) -> None:
        """Open the selected library entry's info.yaml in $EDITOR (or vi), then reload
        the (possibly changed) metadata. Only for in-library papers; grey nodes have no file."""
        n = self._selected_library_node()
        if n is None:
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

    def action_notes(self) -> None:
        """Open the selected library entry's notes file in $EDITOR, creating it from the
        papis notes-template if it doesn't exist yet. Only for in-library papers; grey nodes
        have no folder to hold a note."""
        n = self._selected_library_node()
        if n is None:
            return
        self._notes_worker(n)

    @work(group="notes")
    async def _notes_worker(self, n: Node) -> None:
        # Refresh n.doc from info.yaml before asking papis for the notes path:
        # papis.notes.notes_path reads doc["notes"], and n.doc is a snapshot from
        # library-load time. Without this, an externally-edited notes filename
        # (or one written back by a previous notes_path_ensured call) is missed.
        folder = n.doc.get_main_folder()
        try:
            if folder:
                n.doc = papis.document.from_folder(folder)
            notepath = papis.notes.notes_path_ensured(n.doc)   # renders template if absent
        except Exception as e:                       # noqa: BLE001
            self.notify(f"notes failed: {e}", severity="error")
            return
        # notes_path may have set doc["notes"] and written info.yaml; keep the
        # SQLite cache in step so external queries see the same filename.
        if folder:
            papis.database.get().update(n.doc)
        editor = os.environ.get("EDITOR") or "vi"
        try:
            with self.suspend():                     # hand the terminal to the editor
                subprocess.run([editor, notepath])
        except Exception as e:                       # noqa: BLE001
            self.notify(f"notes failed: {e}", severity="error")
            return
        self.notify(f"notes @{n.ref}")

    def action_tags(self) -> None:
        """Open the tag editor for the selected library entry. The modal is
        vocabulary-first: enter toggles assignment on this paper, but a/r/dd
        also add / rename / delete tags across the whole library. Grey nodes
        have no info.yaml so we skip them."""
        n = self._selected_library_node()
        if n is None:
            return
        self.push_screen(
            TagScreen(self, n),
            lambda _r: self._render_center())     # one refresh once the editor closes

    def _library_tags(self) -> list[str]:
        """Every distinct tag currently attached to some library paper."""
        seen: set[str] = set()
        for x in self.library:
            seen.update(doc_tags(x.doc))
        return sorted(seen)

    def _library_tag_counts(self) -> dict[str, int]:
        """{tag: number of papers that carry it} across the whole library."""
        counts: dict[str, int] = {}
        for x in self.library:
            for t in doc_tags(x.doc):
                counts[t] = counts.get(t, 0) + 1
        return counts

    def _custom_tags(self) -> list[str]:
        """User-added tags that don't (yet) appear on any paper. Persisted in
        state.json under `custom_tags` so orphan tags survive restarts."""
        return list(_load_state().get("custom_tags", []))

    def _all_vocab_tags(self) -> list[str]:
        """Union of library-attached tags and the user's custom tags."""
        return sorted(set(self._library_tags()) | set(self._custom_tags()))

    def _add_custom_tag(self, name: str) -> None:
        """Add `name` to the persisted custom-tags list (idempotent)."""
        name = name.strip()
        if not name:
            return
        state = _load_state()
        ct = set(state.get("custom_tags", []))
        ct.add(name)
        _save_state(custom_tags=sorted(ct))

    def _delete_tag_globally(self, name: str) -> None:
        """Remove `name` from every library paper that has it AND from the
        custom-tags list. Writes each affected info.yaml + refreshes the papis
        cache. No-op for tags that don't exist."""
        for x in self.library:
            tags = doc_tags(x.doc)
            if name in tags:
                self._apply_tags(x, [t for t in tags if t != name])
        state = _load_state()
        ct = [t for t in state.get("custom_tags", []) if t != name]
        _save_state(custom_tags=ct)

    def _rename_tag_globally(self, old: str, new: str) -> None:
        """Rename `old` to `new` across every paper that carries it plus the
        custom-tags list. If `new` already exists on a paper, the two collapse
        (no duplicate). No-op if old == new."""
        if not new or old == new:
            return
        for x in self.library:
            tags = doc_tags(x.doc)
            if old in tags:
                self._apply_tags(x, sorted({(new if t == old else t) for t in tags}))
        state = _load_state()
        ct = {(new if t == old else t) for t in state.get("custom_tags", [])}
        _save_state(custom_tags=sorted(ct))

    def _apply_tags(self, n: Node, tags: list[str]) -> None:
        """Live-write `tags` onto n's entry (one toggle = one save). The in-
        library nodes for a paper share a single papis doc object, so mutating
        it in place propagates to every view; we persist to info.yaml and
        refresh the search index. The visible table/card re-render once, when
        the editor closes (see action_tags callback)."""
        new = sorted(set(tags))
        if new == sorted(doc_tags(n.doc)):
            return                                # no change — skip the write
        if new:
            n.doc["tags"] = new
        elif "tags" in n.doc:
            del n.doc["tags"]
        try:
            n.doc.save()
        except Exception as e:                    # noqa: BLE001
            self.notify(f"tag save failed: {e}", severity="error")
            return
        papis.database.get().update(n.doc)

    def action_delete(self) -> None:
        """Delete the selected library entry — its folder, PDF, notes, everything — behind a
        y/n confirm. Grey nodes have nothing to delete."""
        n = self._selected_library_node()
        if n is None:
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
    BibApp().run()
