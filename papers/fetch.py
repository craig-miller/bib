#!/usr/bin/env python3
"""papis graph fetch — populate citation sidecars (async, complete cited-by).

Headless backend for `papers`. Per library paper:
  * references  -> citations.yaml   (S2 → info.yaml/Crossref → OpenAlex, hydrated)
  * cited-by    -> cited-by.yaml     (OpenAlex cursor paging — COMPLETE, no cap)

OpenAlex is primary on cited-by because S2 hard-walls at offset 10 000; a top paper
(Anselin: 12 659 citers) can't be fully paged from S2. DOI-less papers are resolved by
title match, so the whole library is fetchable — not just the DOI-bearing half.

`save_cited_by()` in papis 0.15.0 is bugged (writes the citations file); we bypass it and
write cited-by directly via get_cited_by_file() + list_to_path().

    papers-fetch --ref Anselin1995        # one paper
    papers-fetch --all --dry-run          # whole library, no writes
    papers-fetch --cron                   # idempotent daily driver (see cron_main)

Exit code is meaningful: 0 iff every resolvable paper was written AND S2 enrichment
succeeded; 1 if any paper errored or the S2 batch failed (an unresolved paper — no
DOI/title match — is a benign skip, not a failure).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import papis.config
import papis.database
import papis.citations
from papis.yaml import list_to_path

from . import core as gc

# --cron state: skip a run if the last SUCCESS is younger than this. Cron ticks far more
# often than this (every few hours) so a laptop that misses its slot catches up on the
# next power-on, and a failed run — which does NOT stamp — retries at the next tick.
_CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "papers"
_STAMP = _CONFIG_DIR / "last-run.json"
_MIN_INTERVAL = timedelta(days=6)


async def process(client: gc.Client, doc, res, enrich: dict, *, dry_run: bool) -> str:
    """Write one paper's sidecars. Returns "ok", or "unresolved" for a paper with no
    DOI/arXiv/title match (a benign skip). Raises on a real fetch/write error."""
    ref = doc.get("ref") or doc.get("title", "?")
    print(f"\n=== {ref} ===", flush=True)
    if res.s2 is None and res.oa is None:
        print("  unresolved (no DOI/arXiv/title match) — skipping", flush=True)
        return "unresolved"
    print(f"  resolved via {res.how}: doi={res.doi} oa={res.oa}", flush=True)

    if enrich:
        print(f"  S2 enrichment: cites={enrich.get('s2_citation_count')} "
              f"influential={enrich.get('s2_influential_citation_count')} "
              f"oaPdf={bool(enrich.get('openaccess_pdf'))}", flush=True)

    references, tier = await gc.fetch_references(client, doc, res)
    print(f"  references: {len(references)} via [{tier}]", flush=True)

    cited_by: list[dict] = []
    if res.oa:
        async for cit in gc.stream_cited_by(client, res.oa):
            cited_by.append(cit)
            if len(cited_by) % 500 == 0:
                print(f"    …cited-by streaming: {len(cited_by)}", flush=True)
    print(f"  cited-by:   {len(cited_by)} (complete)", flush=True)

    if dry_run:
        print("  --dry-run: not writing", flush=True)
        return "ok"

    allow_unicode = papis.config.getboolean("info-allow-unicode")
    if references:
        papis.citations.save_citations(doc, references)
    cb_file = papis.citations.get_cited_by_file(doc)
    if cited_by and cb_file:
        list_to_path(cited_by, cb_file, allow_unicode=allow_unicode)

    # info.yaml write, done once: sanitize stored markup (papis keeps Crossref abstracts as
    # raw <jats:p> XML) AND fold the S2 enrichment. Save if either touched anything.
    dirty = False
    cleaned = []
    for field in ("title", "abstract"):
        v = doc.get(field)
        if v:
            clean = gc.strip_markup(v)
            if clean and clean != str(v):
                doc[field] = clean
                cleaned.append(field)
                dirty = True
    if enrich:
        for k, v in enrich.items():
            doc[k] = v
        dirty = True
    # DOI-less papers get no S2 batch record, so no citation count. Fold in the exact
    # OpenAlex cited-by total we just streamed (node_from_doc reads `cited_by_count`).
    if not enrich and cited_by:
        doc["cited_by_count"] = len(cited_by)
        dirty = True
    if dirty:
        doc.save()
        # refresh the papis cache so a separate reader (the TUI) sees the new keys —
        # writing info.yaml alone does NOT invalidate the `papis` backend's pickle cache.
        papis.database.get().update(doc)

    extras = "".join(f"  + info.yaml {x}" for x in (
        (["enrichment"] if enrich else [])
        + (["oa-cites"] if (not enrich and cited_by) else [])
        + ([f"clean({','.join(cleaned)})"] if cleaned else [])))
    print(f"  wrote citations.yaml ({len(references)}) + cited-by.yaml ({len(cited_by)}){extras}",
          flush=True)
    return "ok"


async def resolve_and_enrich(client: gc.Client, targets: list):
    """Resolve every target, then batch-enrich all DOI-bearing subjects in ONE S2 call.

    Each stage is isolated so one bad paper (or an S2 outage) degrades rather than aborts
    the whole run: a resolve that raises yields (doc, None) and is counted; an S2 batch
    that raises leaves every paper un-enriched (they still get their OpenAlex sidecars).
    Returns (plan, resolve_failures, s2_ok) where plan is [(doc, Resolved|None, enrich)]."""
    resolved = []
    resolve_failures = 0
    for d in targets:
        try:
            resolved.append((d, await gc.resolve(client, d)))
        except Exception as e:
            resolve_failures += 1
            print(f"  ERROR resolving {d.get('ref', '?')}: {e}", file=sys.stderr)
            resolved.append((d, None))

    id_by_doi = {res.doi: doc for doc, res in resolved if res is not None and res.doi}
    s2_ok = True
    recs: dict = {}
    if id_by_doi:
        try:
            recs = await gc.s2_batch(client, [f"DOI:{doi}" for doi in id_by_doi])
        except Exception as e:
            s2_ok = False
            print(f"  ERROR S2 batch (enrichment skipped this run): {e}", file=sys.stderr)

    enrich_by_ref = {}
    for iid, rec in recs.items():
        doc = id_by_doi.get(iid.replace("DOI:", ""))
        if doc is not None:
            enrich_by_ref[doc.get("ref")] = gc.s2_enrichment(rec)
    plan = [(doc, res, enrich_by_ref.get(doc.get("ref"), {})) for doc, res in resolved]
    return plan, resolve_failures, s2_ok


async def amain(refs: list[str] | None, do_all: bool, dry_run: bool) -> dict:
    """Run the fetch and return a summary dict:
    {processed, unresolved, failed, s2_ok, openalex, s2}."""
    db = papis.database.get()
    docs = db.get_all_documents()
    if do_all:
        targets = docs
    else:
        targets = [d for d in docs if d.get("ref") in set(refs or [])]
        if not targets:
            sys.exit(f"no library paper with ref in {refs}")

    processed = unresolved = failed = 0
    async with gc.Client() as client:
        print(f"S2 key: {'yes' if client.s2_key else 'keyless'}", file=sys.stderr)
        plan, resolve_failures, s2_ok = await resolve_and_enrich(client, targets)
        failed += resolve_failures
        for doc, res, enrich in plan:
            if res is None:          # resolve raised — already counted
                continue
            try:
                status = await process(client, doc, res, enrich, dry_run=dry_run)
                if status == "unresolved":
                    unresolved += 1
                else:
                    processed += 1
            except Exception as e:
                failed += 1
                print(f"  ERROR on {doc.get('ref', '?')}: {e}", file=sys.stderr)
        summary = {
            "processed": processed, "unresolved": unresolved, "failed": failed,
            "s2_ok": s2_ok,
            "openalex": client.counts["openalex"], "s2": client.counts["s2"],
        }

    print(f"\nrequest budget — OpenAlex: {summary['openalex']}  S2: {summary['s2']}",
          file=sys.stderr)
    print(f"summary: {processed} written, {unresolved} unresolved, {failed} failed, "
          f"S2 {'ok' if s2_ok else 'FAILED'}", file=sys.stderr)
    return summary


def _run_failed(summary: dict) -> bool:
    """A run is a failure iff a paper errored or the S2 batch failed. Unresolved papers
    (permanent — no DOI/title match) are benign and must NOT count, or --cron would never
    stamp and would re-run forever."""
    return bool(summary["failed"] or not summary["s2_ok"])


def _notify(title: str, body: str) -> None:
    """Best-effort desktop notification (routes to the Noctalia daemon). Silent if
    notify-send is missing or no session bus is reachable (e.g. a headless run)."""
    try:
        subprocess.run(["notify-send", "-u", "critical", "-a", "papers", title, body],
                       timeout=10, check=False)
    except Exception:
        pass


def cron_main() -> int:
    """Idempotent daily driver. Skip if a run succeeded within _MIN_INTERVAL; otherwise
    run --all. On success, stamp last-run.json (so the next few days of ticks are cheap
    no-ops). On failure, notify + return 1 WITHOUT stamping, so the next cron tick retries."""
    now = datetime.now(timezone.utc)
    try:
        stamp = json.loads(_STAMP.read_text())
        last = datetime.fromisoformat(stamp["last_success"])
        age = now - last
        if age < _MIN_INTERVAL:
            print(f"papers-fetch --cron: last success {age.days}d ago "
                  f"(< {_MIN_INTERVAL.days}d) — skipping", flush=True)
            return 0
    except (FileNotFoundError, KeyError, ValueError) as e:
        print(f"papers-fetch --cron: no valid stamp ({e}) — running", flush=True)

    summary = asyncio.run(amain(None, do_all=True, dry_run=False))

    if _run_failed(summary):
        body = (f"{summary['failed']} paper(s) failed"
                + ("" if summary["s2_ok"] else "; S2 enrichment failed")
                + " — will retry next tick")
        _notify("papers-fetch failed", body)
        print(f"papers-fetch --cron: FAILURE ({body}); not stamped", file=sys.stderr)
        return 1

    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _STAMP.write_text(json.dumps({
        "last_success": now.isoformat(),
        "processed": summary["processed"], "unresolved": summary["unresolved"],
        "openalex": summary["openalex"], "s2": summary["s2"],
    }, indent=2))
    print(f"papers-fetch --cron: success — {summary['processed']} written, stamped", flush=True)
    return 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", action="append", help="library paper ref (repeatable)")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--cron", action="store_true",
                    help=f"idempotent driver: skip if a run succeeded in the last "
                         f"{_MIN_INTERVAL.days}d, else --all; notify + exit 1 on failure")
    args = ap.parse_args()
    if args.cron:
        sys.exit(cron_main())
    if not args.ref and not args.all:
        sys.exit("give --ref <REF> (repeatable) or --all, or --cron")
    summary = asyncio.run(amain(args.ref, args.all, args.dry_run))
    sys.exit(1 if _run_failed(summary) else 0)


if __name__ == "__main__":
    main()
