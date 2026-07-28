#!/usr/bin/env python3
"""papis graph — async fetch core (shared by the headless fetcher and the Textual TUI).

One rate-limited async HTTP layer, so rate discipline lives in exactly one place:

  * per-host **token-bucket** limiter — OpenAlex ~8 req/s, Semantic Scholar 1 req/s and
    never parallel (its account-wide ~1 rps cap 429s concurrent callers, even keyed).
  * exponential backoff honoring `Retry-After` on 429/5xx.

The fetchers are **async generators** — they `yield` each neighbor as its page arrives,
so the TUI can stream rows behind a "fetching…" indicator while the headless cron just
drains the same generator to a sidecar. Cursor-paged cited-by is complete (no cap);
OpenAlex is primary on that direction because S2 hard-walls at offset 10 000. S2 demotes
to an `isInfluential` enricher.

Attribution: data from Semantic Scholar (https://www.semanticscholar.org) and OpenAlex
(https://openalex.org); reference metadata may originate from Crossref.
"""
from __future__ import annotations

import asyncio
import html
import os
import re
import subprocess
import urllib.parse
from collections.abc import AsyncIterator
from typing import Any

import httpx
from rapidfuzz import fuzz

EMAIL = "craig@nextidea.io"
UA = f"papis-graph/1.0 (mailto:{EMAIL})"
# A browser-ish UA for fetching PDFs off arbitrary hosts (some publishers bot-block the
# polite API UA). Used only for verify/download, never for the JSON APIs.
BROWSER_UA = "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 papis-graph/1.0"

_TAG_RE = re.compile(r"<[^>]+>")


def strip_markup(s: Any) -> str:
    """Drop XML/HTML tags and unescape entities — publisher abstracts arrive wrapped in
    JATS (`<jats:p>…`, Crossref) and OpenAlex titles sometimes carry `<b>`/`<i>`; neither
    belongs on screen. Collapses the whitespace left behind."""
    if not s:
        return ""
    return " ".join(html.unescape(_TAG_RE.sub("", str(s))).split())

S2_BASE = "https://api.semanticscholar.org/graph/v1"
OA_BASE = "https://api.openalex.org"

S2_NEIGHBOR_FIELDS = "paperId,externalIds,title,year,authors,citationCount,venue,publicationTypes"
OA_WORK_SELECT = ("id,ids,doi,title,display_name,publication_year,type,"
                  "cited_by_count,referenced_works_count,authorships")

TITLE_MATCH_MIN = 90        # fuzz.ratio threshold for identity (same-paper) matching
MAX_RETRIES = 6


def _norm_title(t: str) -> str:
    """Lowercase, drop punctuation, collapse whitespace — for identity comparison."""
    return " ".join("".join(ch if ch.isalnum() else " " for ch in (t or "").lower()).split())


def same_paper(ta: str, tb: str, ya: Any = None, yb: Any = None,
               min_ratio: int = TITLE_MATCH_MIN) -> bool:
    """Are these the SAME paper? Symmetric Levenshtein ratio on normalized titles (NOT
    token_set_ratio, which scores 100 on token subsets — 'Geography' ⊂ 'Citizens as
    sensors: … geography'). A near-exact title (≥95) is identity outright; a borderline
    match (90–95) needs year corroboration (±2). The year is only *advisory* because
    OpenAlex frequently mis-dates works (Egenhofer1990 → indexed 1998), so it can't hard-veto
    a near-exact title."""
    na, nb = _norm_title(ta), _norm_title(tb)
    r = fuzz.ratio(na, nb)
    if r < min_ratio:
        return False
    # A long, distinctive title matched near-exactly is identity even if the years
    # disagree (OpenAlex misdates). But a SHORT/generic title ("Geographical
    # information science" — a whole field's name) is shared by many papers, so it
    # must still corroborate by year, or later namesakes get mis-linked.
    distinctive = len(na) >= 40 and len(na.split()) >= 6
    if distinctive and r >= 97:
        return True
    try:
        if ya is not None and yb is not None and abs(int(ya) - int(yb)) > 2:
            return False
    except (TypeError, ValueError):
        pass
    return True


# --------------------------------------------------------------------------- #
# per-host token-bucket rate limiter                                           #
# --------------------------------------------------------------------------- #
class HostLimiter:
    """Token bucket + concurrency cap for one host. `acquire()` blocks until a
    token is available, so any number of async callers self-throttle to `rate`."""

    def __init__(self, rate: float, burst: float, concurrency: int) -> None:
        self.rate = rate
        self.capacity = burst
        self.tokens = burst
        self.sem = asyncio.Semaphore(concurrency)
        self.lock = asyncio.Lock()
        self.updated = asyncio.get_event_loop().time()

    async def acquire(self) -> None:
        async with self.lock:
            now = asyncio.get_event_loop().time()
            self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
            self.updated = now
            if self.tokens < 1.0:
                await asyncio.sleep((1.0 - self.tokens) / self.rate)
                self.tokens = 0.0
                self.updated = asyncio.get_event_loop().time()
            else:
                self.tokens -= 1.0


def _pass_token(env_var: str, pass_path: str) -> str | None:
    """A `pass`-stored API token: env override first, then `pass show`. Returns None on any
    failure (missing entry, or gpg-agent locked over a non-interactive session)."""
    key = os.environ.get(env_var)
    if key:
        return key.strip()
    try:
        out = subprocess.run(["pass", "show", pass_path],
                             capture_output=True, text=True, timeout=10)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    return None


def _s2_key() -> str | None:
    return _pass_token("BIB_S2_KEY", "api/semanticscholar/token")


def _kagi_token() -> str | None:
    return _pass_token("BIB_KAGI_KEY", "api/kagi/token")


# --------------------------------------------------------------------------- #
# the shared async client                                                      #
# --------------------------------------------------------------------------- #
class Client:
    """Rate-limited async JSON client. Create one per event loop; close when done
    (or use `async with`)."""

    def __init__(self) -> None:
        self.s2_key = _s2_key()
        self.kagi_token = _kagi_token()
        self.counts = {"openalex": 0, "s2": 0, "unpaywall": 0, "kagi": 0}   # every HTTP send
        self._http = httpx.AsyncClient(
            headers={"User-Agent": UA, "Accept": "application/json"},
            timeout=httpx.Timeout(40.0), follow_redirects=True)
        self.limiters = {
            # OpenAlex polite pool: 10/s ceiling -> stay at 8, allow some concurrency.
            "openalex": HostLimiter(rate=8.0, burst=8.0, concurrency=6),
            # S2: keyed limit is "1 req/s cumulative across all endpoints"; S2 asks
            # clients to stay UNDER it. Give real headroom -> one request every 2 s
            # (rate 0.5), burst 1, conc 1 never-parallel. S2 is only ~11 calls per full
            # library sync, so 2 s spacing costs ~22 s total — cheap insurance.
            "s2": HostLimiter(rate=0.5, burst=1.0, concurrency=1),
            # Unpaywall + Kagi: only ever one call each per PDF-discovery, so a gentle
            # limiter is plenty (Kagi is billed per query, Unpaywall asks callers to stay
            # polite). Not on any hot path.
            "unpaywall": HostLimiter(rate=5.0, burst=5.0, concurrency=2),
            "kagi": HostLimiter(rate=2.0, burst=2.0, concurrency=1),
        }

    async def __aenter__(self) -> Client:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, url: str, host: str,
                       json_body: dict | None = None) -> Any:
        lim = self.limiters[host]
        headers: dict[str, str] = {}
        if host == "s2" and self.s2_key:
            headers = {"x-api-key": self.s2_key}
        elif host == "kagi" and self.kagi_token:
            headers = {"Authorization": "Bearer " + self.kagi_token}
        last: Exception | None = None
        for attempt in range(MAX_RETRIES):
            async with lim.sem:
                await lim.acquire()
                self.counts[host] += 1
                try:
                    r = await self._http.request(method, url, json=json_body, headers=headers)
                except (httpx.TransportError, httpx.TimeoutException) as e:
                    last = e
                    await asyncio.sleep(min(60.0, 2 ** attempt) + 0.1 * (attempt + 1))
                    continue
            if r.status_code in (429, 500, 502, 503, 504):
                last = httpx.HTTPStatusError(f"{r.status_code}", request=r.request, response=r)
                # Always back off — honor Retry-After, else exponential. We stay on the
                # SAME lane (keyed if we have a key): backing off politely is the signal
                # S2 uses to raise a trial rate limit; jumping to another lane to dodge
                # the 429 would read as misbehaviour.
                ra = r.headers.get("Retry-After")
                wait = float(ra) if ra and ra.isdigit() else min(60.0, 2 ** attempt) + 0.1 * (attempt + 1)
                await asyncio.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError(f"giving up after {MAX_RETRIES} attempts: {url} ({last})")

    async def get(self, url: str, host: str) -> Any:
        return await self._request("GET", url, host)

    async def post(self, url: str, body: dict, host: str) -> Any:
        return await self._request("POST", url, host, json_body=body)

    # -- raw PDF fetches (arbitrary hosts, browser UA, no API limiter) ------------ #
    async def sniff(self, url: str, nbytes: int = 2048,
                    timeout: float = 12.0) -> tuple[int, str, bytes, int | None]:
        """Range-GET the first bytes of an arbitrary URL for %PDF sniffing. Returns
        (status, content_type, head_bytes, total_size|None). Does NOT raise on 4xx — the
        caller inspects status + body (a 403 often serves an HTML error page)."""
        headers = {"User-Agent": BROWSER_UA, "Accept": "*/*",
                   "Range": f"bytes=0-{nbytes - 1}"}
        async with self._http.stream("GET", url, headers=headers, timeout=timeout) as r:
            ct = r.headers.get("content-type", "").split(";")[0]
            cr = r.headers.get("content-range", "")   # "bytes 0-2047/115575"
            total = cr.rsplit("/", 1)[-1] if "/" in cr else r.headers.get("content-length")
            head = b""
            async for chunk in r.aiter_bytes():
                head += chunk
                if len(head) >= nbytes:
                    break
        size = int(total) if total and str(total).isdigit() else None
        return r.status_code, ct, head, size

    async def fetch_bytes(self, url: str, timeout: float = 60.0) -> bytes:
        """Full GET of an arbitrary URL (the actual PDF download). Raises on HTTP error."""
        r = await self._http.get(url, headers={"User-Agent": BROWSER_UA, "Accept": "*/*"},
                                  timeout=timeout)
        r.raise_for_status()
        return r.content


# S2 batch: details for up to 500 papers in ONE request. `fields` is a single
# query-string value (comma-joined); ids go in the JSON body. Collapses N per-paper
# calls into ceil(N/500) — the polite way to use S2, and it minimises exposure to the
# per-request 429s. Nested `references.*` returns each paper's bibliography inline.
S2_BATCH_FIELDS = ("externalIds,title,year,authors,citationCount,referenceCount,"
                   "influentialCitationCount,isOpenAccess,openAccessPdf,"
                   "references.externalIds,references.title,references.year,"
                   "references.authors,references.citationCount")


async def s2_batch(client: Client, ids: list[str],
                   fields: str = S2_BATCH_FIELDS) -> dict[str, dict]:
    """Return {input_id: paper_record} for the given S2 ids (DOI:/ARXIV:/SHA/…).
    Chunks at 500. Records are None for ids S2 doesn't know; those are dropped."""
    out: dict[str, dict] = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        url = f"{S2_BASE}/paper/batch?fields={urllib.parse.quote(fields, safe=',.')}"
        got = await client.post(url, {"ids": chunk}, host="s2")
        if isinstance(got, list):
            for input_id, rec in zip(chunk, got):
                if rec:
                    out[input_id] = rec
    return out


# --------------------------------------------------------------------------- #
# name splitting + mapping to papis citation dicts                             #
# --------------------------------------------------------------------------- #
def split_name(name: str) -> dict:
    name = " ".join((name or "").split())
    if not name:
        return {"given": "", "family": ""}
    if "," in name:
        fam, _, giv = name.partition(",")
        return {"given": giv.strip(), "family": fam.strip()}
    parts = name.split(" ")
    if len(parts) == 1:
        return {"given": "", "family": parts[0]}
    return {"given": " ".join(parts[:-1]), "family": parts[-1]}


def _flat_author(author_list: list[dict]) -> str:
    return " and ".join(
        ", ".join(p for p in (a.get("family", ""), a.get("given", "")) if p)
        for a in author_list)


def _clean(d: dict) -> dict:
    return {k: v for k, v in d.items() if v not in (None, "", [], {})}


def s2_to_citation(paper: dict, *, is_influential: bool | None = None) -> dict:
    ext = paper.get("externalIds") or {}
    authors = [split_name(a.get("name", "")) for a in (paper.get("authors") or [])]
    doi = ext.get("DOI")
    cit = {
        "title": paper.get("title"), "year": paper.get("year"),
        "author_list": authors, "author": _flat_author(authors),
        "doi": doi.lower() if doi else None,
        "type": (paper.get("publicationTypes") or [None])[0],
        "journal": paper.get("venue"),
        "citation_count": paper.get("citationCount"),
        "reference_count": paper.get("referenceCount"),
        "s2_id": paper.get("paperId"), "corpus_id": ext.get("CorpusId"),
        "arxiv": ext.get("ArXiv"), "pmid": ext.get("PubMed"),
        "url": f"https://doi.org/{doi}" if doi else paper.get("url"),
    }
    if is_influential is not None:
        cit["isInfluential"] = is_influential
    return _clean(cit)


def oa_to_citation(work: dict) -> dict:
    ids = work.get("ids") or {}
    authors = [split_name((a.get("author") or {}).get("display_name", ""))
               for a in (work.get("authorships") or [])]
    doi = work.get("doi")
    if doi:
        doi = doi.replace("https://doi.org/", "").lower()
    oa_id = (work.get("id") or "").replace("https://openalex.org/", "") or None
    return _clean({
        "title": work.get("title") or work.get("display_name"),
        "year": work.get("publication_year"),
        "author_list": authors, "author": _flat_author(authors),
        "doi": doi, "type": work.get("type"),
        "citation_count": work.get("cited_by_count"),
        "reference_count": work.get("referenced_works_count"),
        "openalex_id": oa_id,
        "pmid": (ids.get("pmid") or "").replace("https://pubmed.ncbi.nlm.nih.gov/", "") or None,
        "url": f"https://doi.org/{doi}" if doi else work.get("id"),
    })


# --------------------------------------------------------------------------- #
# id resolution — DOI, else arXiv, else title-match (for DOI-less works)       #
# --------------------------------------------------------------------------- #
class Resolved:
    def __init__(self, s2: str | None, oa: str | None, doi: str | None, how: str) -> None:
        self.s2, self.oa, self.doi, self.how = s2, oa, doi, how


async def resolve(client: Client, doc: Any) -> Resolved:
    """Resolve a papis doc to (S2 lookup id, OpenAlex work id, doi). Falls back to
    OpenAlex title search for DOI-less books/reports — the half of the library that
    was previously unfetchable."""
    doi = doc.get("doi")
    if doi:
        doi = str(doi).lower()
        # get the OpenAlex work id too (needed for cursor cited-by)
        oa = None
        try:
            w = await client.get(f"{OA_BASE}/works/doi:{doi}?mailto={EMAIL}&select=id", "openalex")
            oa = (w.get("id") or "").replace("https://openalex.org/", "") or None
        except Exception:
            pass
        return Resolved(f"DOI:{doi}", oa, doi, "doi")
    if (doc.get("eprinttype") or "").lower() == "arxiv" and doc.get("eprint"):
        return Resolved(f"ARXIV:{doc['eprint'].split('v')[0]}", None, None, "arxiv")
    title = str(doc.get("title", "")).strip()
    if not title:
        return Resolved(None, None, None, "unresolved")
    # strip punctuation before building the filter: OpenAlex treats a comma in a
    # filter value as the AND-separator, so "Regions, Lines, and Points" -> 400.
    q = urllib.parse.quote(_norm_title(title))
    year = doc.get("year")
    try:
        page = await client.get(
            f"{OA_BASE}/works?filter=title.search:{q}&per-page=3&mailto={EMAIL}"
            f"&select=id,doi,title,publication_year", "openalex")
    except Exception:
        return Resolved(None, None, None, "unresolved")
    for w in page.get("results") or []:
        if same_paper(title, str(w.get("title") or ""), year, w.get("publication_year")):
            oa = (w.get("id") or "").replace("https://openalex.org/", "") or None
            wdoi = w.get("doi")
            wdoi = wdoi.replace("https://doi.org/", "").lower() if wdoi else None
            return Resolved(f"DOI:{wdoi}" if wdoi else None, oa, wdoi, "title")
    return Resolved(None, None, None, "unresolved")


# --------------------------------------------------------------------------- #
# streaming cited-by — OpenAlex cursor, COMPLETE, no cap                        #
# --------------------------------------------------------------------------- #
async def stream_cited_by(client: Client, oa_id: str) -> AsyncIterator[dict]:
    """Yield every citer of the OpenAlex work `oa_id`, page by page (cursor paging —
    inherently sequential, but the whole set, no 10k wall)."""
    cursor = "*"
    while cursor:
        url = (f"{OA_BASE}/works?filter=cites:{oa_id}&per-page=200&cursor="
               f"{urllib.parse.quote(cursor)}&mailto={EMAIL}&select={OA_WORK_SELECT}")
        page = await client.get(url, "openalex")
        for w in page.get("results") or []:
            yield oa_to_citation(w)
        cursor = (page.get("meta") or {}).get("next_cursor")


# --------------------------------------------------------------------------- #
# whole-corpus search — the "find NEW papers" entry point (OpenAlex)           #
# --------------------------------------------------------------------------- #
async def search(client: Client, kind: str, query: str,
                 *, limit: int = 50) -> tuple[list[dict], int]:
    """Discover papers across all of OpenAlex (NOT the local library). Returns
    (citation_dicts, total_match_count) — the caller shows the first `limit` and can note
    the rest as truncated. `kind`:

      doi      exact work lookup by DOI (1 result).
      author   resolve the name → OpenAlex author id, then their works, most-cited first
               (a person's body of work by impact).
      keyword  OR of `|`-separated terms over title+abstract, most-cited first (topical
               union by impact).
      title    full-text relevance search (default for bare text) — best known-item ranking.

    Everything is normalized through `_norm_title` before it hits a filter value, because a
    raw comma is OpenAlex's filter AND-separator (→ 400).
    """
    def works(res: dict) -> tuple[list[dict], int]:
        rows = [oa_to_citation(w) for w in (res.get("results") or [])]
        return rows, (res.get("meta") or {}).get("count", len(rows))

    if kind == "doi":
        doi = query.replace("https://doi.org/", "").strip().lower()
        try:
            w = await client.get(
                f"{OA_BASE}/works/doi:{doi}?mailto={EMAIL}&select={OA_WORK_SELECT}", "openalex")
        except Exception:
            return [], 0
        return [oa_to_citation(w)], 1

    if kind in ("author", "topic"):
        # both resolve a name → an OpenAlex entity id, then that entity's works by impact.
        entity, filt = (("authors", "authorships.author.id") if kind == "author"
                        else ("topics", "topics.id"))
        q = urllib.parse.quote(_norm_title(query))
        page = await client.get(
            f"{OA_BASE}/{entity}?search={q}&per-page=1&mailto={EMAIL}"
            f"&select=id,display_name", "openalex")
        hits = page.get("results") or []
        if not hits:
            return [], 0
        eid = (hits[0].get("id") or "").replace("https://openalex.org/", "")
        page = await client.get(
            f"{OA_BASE}/works?filter={filt}:{eid}&sort=cited_by_count:desc"
            f"&per-page={limit}&mailto={EMAIL}&select={OA_WORK_SELECT}", "openalex")
        return works(page)

    if kind == "keyword":
        terms = "|".join(urllib.parse.quote(_norm_title(t))
                         for t in query.split("|") if t.strip())
        if not terms:
            return [], 0
        page = await client.get(
            f"{OA_BASE}/works?filter=title_and_abstract.search:{terms}"
            f"&sort=cited_by_count:desc&per-page={limit}&mailto={EMAIL}"
            f"&select={OA_WORK_SELECT}", "openalex")
        return works(page)

    # title / bare — relevance-ranked full-text search
    q = urllib.parse.quote(_norm_title(query))
    page = await client.get(
        f"{OA_BASE}/works?search={q}&per-page={limit}&mailto={EMAIL}"
        f"&select={OA_WORK_SELECT}", "openalex")
    return works(page)


# --------------------------------------------------------------------------- #
# per-work keywords — what a paper is "about" (topic vocabulary for the card)   #
# --------------------------------------------------------------------------- #
# OpenAlex `keywords`/`concepts` carry generic umbrella terms and parenthetical
# disambiguations; drop the umbrellas so what's left is the searchable specifics.
_GENERIC_KW = {
    "mathematics", "computer science", "artificial intelligence", "geometry",
    "combinatorics", "pure mathematics", "physics", "engineering", "biology",
    "chemistry", "economics", "statistics", "psychology", "geography", "philosophy",
    "science", "political science", "sociology", "medicine", "business",
    "materials science", "environmental science", "geology", "history", "linguistics",
}


def _clean_terms(items: list[dict], limit: int, *, drop_generic: bool) -> list[str]:
    """Deduped display names, parenthetical disambiguations stripped, capped at `limit`.
    `drop_generic` removes umbrella terms — on for keywords, off for curated topics."""
    out: list[str] = []
    seen: set[str] = set()
    for it in items:
        raw = it.get("display_name") or ""
        name = raw.split(" (")[0].strip()
        low = name.lower()
        if not name or low in seen:
            continue
        if drop_generic:
            if low in _GENERIC_KW:
                continue
            # A disambiguated single word ("Point (geometry)", "Space (punctuation)") loses
            # its meaning once the paren is stripped — too ambiguous to seed a search. Drop.
            if "(" in raw and len(name.split()) < 2:
                continue
        seen.add(low)
        out.append(name)
        if len(out) >= limit:
            break
    return out


def reconstruct_abstract(inv: dict | None) -> str:
    """OpenAlex stores abstracts as an inverted index {word: [positions]}; rebuild the
    plain text. Strips a leading 'Abstract' token some records carry. '' if absent."""
    if not inv:
        return ""
    n = max((p for ps in inv.values() for p in ps), default=-1) + 1
    if n <= 0:
        return ""
    words: list[str | None] = [None] * n
    for word, positions in inv.items():
        for p in positions:
            if 0 <= p < n:
                words[p] = word
    text = " ".join(w for w in words if w)
    if text[:9].lower() == "abstract ":
        text = text[9:]
    return strip_markup(text)


class About:
    """The lazily-fetched 'what is this paper' bundle for the top card."""
    def __init__(self, topics: list[str] | None = None, keywords: list[str] | None = None,
                 abstract: str = "", venue: str | None = None, publisher: str | None = None,
                 volume: str | None = None, number: str | None = None,
                 pages: str | None = None) -> None:
        self.topics = topics or []
        self.keywords = keywords or []
        self.abstract = abstract
        self.venue, self.publisher = venue, publisher
        self.volume, self.number, self.pages = volume, number, pages


def clean_about(work: dict) -> About:
    """topics/keywords/abstract plus venue (journal or book/proceedings name), publisher,
    and volume/issue/pages for one OpenAlex work — the top card's bibliographic line."""
    src = (work.get("primary_location") or {}).get("source") or {}
    biblio = work.get("biblio") or {}
    fp, lp = biblio.get("first_page"), biblio.get("last_page")
    pages = f"{fp}–{lp}" if fp and lp and fp != lp else (fp or lp or None)
    return About(
        topics=_clean_terms(work.get("topics") or [], 3, drop_generic=False),
        keywords=_clean_terms(work.get("keywords") or [], 8, drop_generic=True),
        abstract=reconstruct_abstract(work.get("abstract_inverted_index")),
        venue=src.get("display_name"),
        publisher=src.get("host_organization_name"),
        volume=biblio.get("volume"), number=biblio.get("issue"), pages=pages,
    )


async def fetch_about(client: Client, oa_id: str | None = None,
                      doi: str | None = None, title: str | None = None,
                      year: Any = None) -> About:
    """topics/keywords/abstract for one work, resolved cheapest-first: OpenAlex id → DOI →
    title match. Empty About when the work can't be located."""
    sel = "keywords,topics,abstract_inverted_index,primary_location,biblio"
    if oa_id:
        w = await client.get(f"{OA_BASE}/works/{oa_id}?mailto={EMAIL}&select={sel}", "openalex")
    elif doi:
        w = await client.get(
            f"{OA_BASE}/works/doi:{str(doi).lower()}?mailto={EMAIL}&select={sel}", "openalex")
    elif title:
        res = await resolve(client, {"title": title, "year": year})
        if not res.oa:
            return About()
        w = await client.get(f"{OA_BASE}/works/{res.oa}?mailto={EMAIL}&select={sel}", "openalex")
    else:
        return About()
    return clean_about(w)


# --------------------------------------------------------------------------- #
# S2 batch enrichment                                                          #
# --------------------------------------------------------------------------- #
def s2_enrichment(rec: dict) -> dict:
    """Pull the fields worth keeping from an S2 batch record → custom keys for the
    subject paper's info.yaml (top-card metadata + a PDF hint for the Enter action)."""
    oa = rec.get("openAccessPdf") or {}
    return _clean({
        "s2_citation_count": rec.get("citationCount"),
        "s2_influential_citation_count": rec.get("influentialCitationCount"),
        "s2_reference_count": rec.get("referenceCount"),
        "s2_id": rec.get("paperId"),
        "openaccess_pdf": oa.get("url"),
    })


# --------------------------------------------------------------------------- #
# references — info.yaml/Crossref → OpenAlex, then DOI-hydrate bare stubs       #
# --------------------------------------------------------------------------- #
def _infoyaml_references(doc: Any) -> list[dict] | None:
    out = []
    for c in (doc.get("citations") or []):
        if not isinstance(c, dict):
            continue
        doi = c.get("doi")
        e = _clean({
            "title": c.get("title") or c.get("volume-title") or c.get("journal-title"),
            "year": c.get("year"),
            "doi": str(doi).lower() if doi else None,
            "author": c.get("author"),
        })
        if e.get("doi") or e.get("title"):
            out.append(e)
    return out or None


async def _openalex_references(client: Client, oa_id: str) -> list[dict] | None:
    work = await client.get(
        f"{OA_BASE}/works/{oa_id}?mailto={EMAIL}&select=referenced_works", "openalex")
    refs = work.get("referenced_works") or []
    if not refs:
        return None
    out: list[dict] = []
    for i in range(0, len(refs), 50):
        pipe = "|".join(w.replace("https://openalex.org/", "") for w in refs[i:i + 50])
        page = await client.get(
            f"{OA_BASE}/works?filter=ids.openalex:{pipe}&per-page=50&mailto={EMAIL}"
            f"&select={OA_WORK_SELECT}", "openalex")
        out.extend(oa_to_citation(w) for w in (page.get("results") or []))
    return out or None


async def _hydrate_by_doi(client: Client, rows: list[dict]) -> None:
    """Fill title/authors/year on bare `{doi: …}` stubs via OpenAlex DOI filter."""
    by_doi = {r["doi"]: r for r in rows if r.get("doi") and not r.get("title")}
    if not by_doi:
        return
    dois = list(by_doi)
    for i in range(0, len(dois), 50):
        pipe = "|".join(dois[i:i + 50])
        try:
            page = await client.get(
                f"{OA_BASE}/works?filter=doi:{pipe}&per-page=50&mailto={EMAIL}"
                f"&select={OA_WORK_SELECT}", "openalex")
        except Exception:
            continue
        for w in page.get("results") or []:
            full = oa_to_citation(w)
            row = by_doi.get((full.get("doi") or "").lower())
            if row:
                for k, v in full.items():
                    row.setdefault(k, v)


async def fetch_references(client: Client, doc: Any, res: Resolved) -> tuple[list[dict], str]:
    """(references, tier). info.yaml/Crossref → OpenAlex; then hydrate stubs.

    NOTE: the S2 `/references` tier was removed — it's publisher-elided for essentially
    every real-world paper (returns null, even via batch), so it was a pure-waste call
    that also 429'd on the flaky key. References now come from the paper's own Crossref
    metadata or OpenAlex `referenced_works`. S2 contributes only bulk enrichment
    (see `s2_batch`), not references."""
    refs, tier = _infoyaml_references(doc), "info.yaml/crossref"
    if not refs and res.oa:
        refs, tier = await _openalex_references(client, res.oa), "openalex"
    if not refs:
        return [], "none"
    await _hydrate_by_doi(client, refs)
    return refs, tier


# --------------------------------------------------------------------------- #
# dual matching — resolve a neighbor to a library entry by DOI OR fuzzy title   #
# --------------------------------------------------------------------------- #
def build_library_index(docs: list[Any]) -> tuple[dict, list[tuple]]:
    """(doi_index, title_index). title_index = [(ref, title, year, doc)]."""
    doi_index = {str(d["doi"]).lower(): d for d in docs if d.get("doi")}
    title_index = [(d.get("ref"), str(d.get("title", "")), d.get("year"), d)
                   for d in docs if d.get("title")]
    return doi_index, title_index


def match_to_library(cit: dict, doi_index: dict, title_index: list[tuple]) -> Any | None:
    """Return the library doc this citation IS, matched by DOI (reliable) or by
    same_paper() title+year identity (guards against token-subset false positives)."""
    doi = (str(cit["doi"]).lower() if cit.get("doi") else None)
    if doi and doi in doi_index:
        return doi_index[doi]
    t = str(cit.get("title") or "")
    if len(t) >= 8:
        yr = cit.get("year")
        for _ref, lt, lyr, doc in title_index:
            if same_paper(t, lt, yr, lyr):
                return doc
    return None


# --------------------------------------------------------------------------- #
# PDF discovery — find a downloadable open-access PDF for one paper            #
#                                                                             #
# Rolled in from the standalone `papis fetch` research so the graph app is     #
# self-contained (core papis only, no dotfiles). Several free sources are      #
# queried in parallel; every url is %PDF-verified before it's offered (OA urls #
# frequently 403 / serve HTML), and candidates are ranked so the most-likely-  #
# real copy sits at the top of the picker.                                     #
# --------------------------------------------------------------------------- #

# OpenAlex tags each location with a version-of-record. Prefer the published PDF, then the
# peer-reviewed accepted manuscript, then the raw preprint. A preprint isn't "incomplete"
# — it's pre-review — so it's a valid last resort, not junk.
_VERSION_RANK = {"publishedVersion": 0, "acceptedVersion": 1, "submittedVersion": 2}
# Open repositories / preprint servers: a paper can be publisher-paywalled yet have a
# working green copy here, so try these before a publisher's own pdf_url.
_REPO_HINTS = ("ncbi.nlm.nih.gov", "europepmc.org", "arxiv.org", "biorxiv", "medrxiv",
               "ssrn", "escholarship", "repository", "repec", "hal.", "zenodo",
               "osf.io", "semanticscholar.org", ".edu")


def _pdf_host(url: str) -> str:
    return urllib.parse.urlparse(url).netloc.replace("www.", "")


def _is_repo(url: str, loc: dict) -> bool:
    if (loc.get("source") or {}).get("type") == "repository":
        return True
    low = url.lower()
    return any(h in low for h in _REPO_HINTS)


async def _empty() -> list[dict]:
    return []


def _arxiv_pdf_cands(arxiv_id: str | None) -> list[dict]:
    """arXiv gives a stable direct PDF url for an eprint id — the single most reliable
    source for the preprints that dominate live graph discovery."""
    if not arxiv_id:
        return []
    eid = str(arxiv_id).split("v")[0].strip()
    return [{"source": "arxiv", "url": f"https://arxiv.org/pdf/{eid}.pdf", "label": eid}] if eid else []


async def _oa_pdf_cands(client: Client, oa_id: str | None, doi: str | None) -> list[dict]:
    """OpenAlex `locations[].pdf_url` for one work, version- then repo-ranked. Uses ONLY
    pdf_url (never `oa_url`, which is a landing page). Empty on miss."""
    sel = "locations,best_oa_location"
    try:
        if oa_id:
            w = await client.get(f"{OA_BASE}/works/{oa_id}?mailto={EMAIL}&select={sel}", "openalex")
        elif doi:
            w = await client.get(
                f"{OA_BASE}/works/doi:{str(doi).lower()}?mailto={EMAIL}&select={sel}", "openalex")
        else:
            return []
    except Exception:
        return []
    locs = list(w.get("locations") or [])
    if w.get("best_oa_location"):
        locs.append(w["best_oa_location"])
    ranked, seen = [], set()
    for loc in locs:
        url = (loc or {}).get("pdf_url")
        if not url or url.split("?")[0] in seen:
            continue
        seen.add(url.split("?")[0])
        version = loc.get("version")
        ranked.append((_VERSION_RANK.get(version, 3), not _is_repo(url, loc),
                       {"source": "openalex", "url": url, "label": version or "oa"}))
    ranked.sort(key=lambda t: (t[0], t[1]))
    return [c for _, _, c in ranked]


async def _unpaywall_pdf_cands(client: Client, doi: str) -> list[dict]:
    try:
        m = await client.get(f"https://api.unpaywall.org/v2/{doi}?email={EMAIL}", "unpaywall")
    except Exception:
        return []
    out = []
    for loc in (m.get("oa_locations") or []):
        u = loc.get("url_for_pdf")
        if u:
            out.append({"source": "unpaywall", "url": u, "label": loc.get("version") or ""})
    return out


async def _s2_pdf_cands(client: Client, doi: str) -> list[dict]:
    try:
        m = await client.get(f"{S2_BASE}/paper/DOI:{doi}?fields=openAccessPdf", "s2")
    except Exception:
        return []
    oa = m.get("openAccessPdf") or {}
    return ([{"source": "s2", "url": oa["url"], "label": oa.get("status") or ""}]
            if oa.get("url") else [])


async def _kagi_pdf_cands(client: Client, title: str | None, family: str | None) -> list[dict]:
    """Kagi web search for `"title" author filetype:pdf` — the fallback that finds
    author-hosted / course-page PDFs the DOI databases miss. Needs the Kagi token
    (billed ~2.5¢/query); silently skipped when absent. Response results are at
    `data.search[]`."""
    if not client.kagi_token or not title:
        return []
    query = f'"{title}" {family or ""} filetype:pdf'.strip()
    try:
        m = await client.post("https://kagi.com/api/v1/search", {"query": query}, "kagi")
    except Exception:
        return []
    out = []
    for it in (m.get("data") or {}).get("search", []):
        if isinstance(it, dict) and it.get("url"):
            out.append({"source": "kagi", "url": it["url"],
                        "label": strip_markup(it.get("title", ""))})
    return out


async def discover_pdfs(client: Client, *, doi: str | None = None, arxiv_id: str | None = None,
                        oa_id: str | None = None, title: str | None = None,
                        author: str | None = None) -> list[dict]:
    """All candidate PDF sources for one paper, most-promising first (arXiv + version-ranked
    OpenAlex, then Unpaywall/S2, then the Kagi web-search fallback), deduped by url. Each
    candidate is `{source, url, label, host}` — NOT yet verified (see `verify_pdf`). The
    structured tiers run concurrently; only Kagi costs money and only when a token exists."""
    oa, up, s2, kg = await asyncio.gather(
        _oa_pdf_cands(client, oa_id, doi),
        _unpaywall_pdf_cands(client, doi) if doi else _empty(),
        _s2_pdf_cands(client, doi) if doi else _empty(),
        _kagi_pdf_cands(client, title, author),
    )
    ordered = _arxiv_pdf_cands(arxiv_id) + oa + up + s2 + kg
    seen, out = set(), []
    for c in ordered:
        key = c["url"].split("?")[0]
        if key in seen:
            continue
        seen.add(key)
        c["host"] = _pdf_host(c["url"])
        out.append(c)
    return out


async def verify_pdf(client: Client, url: str) -> tuple[bool, str, str]:
    """(ok, status, size). Range-fetch the first bytes and check for the %PDF magic — a
    paywall/landing page (HTML, or a 403 error page) never passes. `size` is set for real
    PDFs only (its own column); blank otherwise."""
    try:
        status, ct, head, total = await client.sniff(url)
    except Exception:
        return False, "timeout", ""
    if head[:4] == b"%PDF":
        return True, "PDF", (f"{total // 1024}KB" if total else "?")
    if b"<html" in head[:512].lower() or "html" in ct:
        return False, "html", ""
    if status >= 400:
        return False, f"http{status}", ""
    return False, ((ct.split("/")[-1] if ct else "?") or "?")[:7], ""


async def download_pdf(client: Client, url: str, dest: str) -> int:
    """Download `url` to `dest`, refusing anything that isn't a real PDF. Returns bytes."""
    data = await client.fetch_bytes(url)
    if data[:4] != b"%PDF":
        raise ValueError("not a PDF (got HTML/other)")
    with open(dest, "wb") as f:
        f.write(data)
    return len(data)
