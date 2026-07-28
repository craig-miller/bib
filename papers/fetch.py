#!/usr/bin/env python3
"""papis graph fetch — populate citation sidecars (async, complete cited-by).

Headless backend for `papis graph`. Per library paper:
  * references  -> citations.yaml   (S2 → info.yaml/Crossref → OpenAlex, hydrated)
  * cited-by    -> cited-by.yaml     (OpenAlex cursor paging — COMPLETE, no cap)

OpenAlex is primary on cited-by because S2 hard-walls at offset 10 000; a top paper
(Anselin: 12 659 citers) can't be fully paged from S2. DOI-less papers are resolved by
title match, so the whole library is fetchable — not just the DOI-bearing half.

`save_cited_by()` in papis 0.15.0 is bugged (writes the citations file); we bypass it and
write cited-by directly via get_cited_by_file() + list_to_path().

    PY=~/.local/share/uv/tools/papis/bin/python
    $PY papis-graph-fetch.py --ref Anselin1995            # one paper
    $PY papis-graph-fetch.py --all --dry-run              # whole library, no writes
"""
from __future__ import annotations

import argparse
import asyncio
import sys

import papis.config
import papis.database
import papis.citations
from papis.yaml import list_to_path

from . import core as gc


async def process(client: gc.Client, doc, res, enrich: dict, *, dry_run: bool) -> None:
    ref = doc.get("ref") or doc.get("title", "?")
    print(f"\n=== {ref} ===", flush=True)
    if res.s2 is None and res.oa is None:
        print("  unresolved (no DOI/arXiv/title match) — skipping", flush=True)
        return
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
        return

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


async def resolve_and_enrich(client: gc.Client, targets: list):
    """Resolve every target, then batch-enrich all DOI-bearing subjects in ONE S2 call.
    Returns [(doc, Resolved, enrichment_dict)] preserving order."""
    resolved = [(d, await gc.resolve(client, d)) for d in targets]
    id_by_doi = {res.doi: doc for doc, res in resolved if res.doi}
    recs = await gc.s2_batch(client, [f"DOI:{doi}" for doi in id_by_doi]) if id_by_doi else {}
    enrich_by_ref = {}
    for iid, rec in recs.items():
        doc = id_by_doi.get(iid.replace("DOI:", ""))
        if doc is not None:
            enrich_by_ref[doc.get("ref")] = gc.s2_enrichment(rec)
    return [(doc, res, enrich_by_ref.get(doc.get("ref"), {})) for doc, res in resolved]


async def amain(refs: list[str] | None, do_all: bool, dry_run: bool) -> None:
    db = papis.database.get()
    docs = db.get_all_documents()
    if do_all:
        targets = docs
    else:
        targets = [d for d in docs if d.get("ref") in set(refs or [])]
        if not targets:
            sys.exit(f"no library paper with ref in {refs}")

    async with gc.Client() as client:
        print(f"S2 key: {'yes' if client.s2_key else 'keyless'}", file=sys.stderr)
        plan = await resolve_and_enrich(client, targets)   # ONE S2 batch call for all
        for doc, res, enrich in plan:
            try:
                await process(client, doc, res, enrich, dry_run=dry_run)
            except Exception as e:
                print(f"  ERROR on {doc.get('ref', '?')}: {e}", file=sys.stderr)
        print(f"\nrequest budget — OpenAlex: {client.counts['openalex']}  "
              f"S2: {client.counts['s2']}", file=sys.stderr)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", action="append", help="library paper ref (repeatable)")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not args.ref and not args.all:
        sys.exit("give --ref <REF> (repeatable) or --all")
    asyncio.run(amain(args.ref, args.all, args.dry_run))


if __name__ == "__main__":
    main()
