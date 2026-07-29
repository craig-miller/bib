# bib

A reference manager tui for [papis](https://github.com/papis/papis).

`bib` is a keyboard driven (vim motions inspired) terminal UI over your papis library that helps surface related seminal works. You browse your papers in a table, that shows citations, references, and influence. Automatically fetch related papers' metadata (year, author, doi, publisher, abstract, etc) from OpenAlex. If a paper isn't in your library, you can add / download it.

## Install

```sh
uv tool install /path/to/bib
```

This gives you two commands in an isolated environment (with its own papis):

- `bib` — the TUI
- `bib-fetch` — headless citation-graph + metadata refresh (cron-friendly): resolves each paper,
  writes its `citations.yaml` / `cited-by.yaml` sidecars, folds in Semantic Scholar counts, and cleans
  stored markup. Does not download PDFs (that's the TUI's interactive flow).
  `--ref <REF>` / `--all` / `--dry-run`, or `--cron` — the idempotent daily driver that skips
  unless the last success is >6 days old and notifies (Noctalia) + exits non-zero on failure.
  See `contrib/bib-fetch.crontab` for a laptop-safe every-3h schedule.

It reads your existing papis library and config (`~/.config/papis/`), so nothing else changes.
Your own `papis` command is untouched.

## Keys

| key | action |
|---|---|
| `↑`/`↓`, type | move · fuzzy-filter (`#kw` = keyword) |
| `ctrl+c` / `ctrl+r` | citations / references of the selected paper |
| `ctrl+o` / `esc`, `ctrl+i` | back / forward |
| `ctrl+p` | home (your library) |
| `ctrl+/` | search the whole corpus (OpenAlex) |
| `enter` | open (in-library + PDF) · fetch PDF · add + fetch (grey rows) |
| `ctrl+s` | toggle the card between the structured layout and a CSL reference |
| `ctrl+y` | pick the CSL reference style |
| `ctrl+d` | expand the detail card full-screen |
| `ctrl+e` / `ctrl+shift+d` | edit / delete the selected entry |
| `ctrl+q`, `ctrl+shift+/` | quit · help |

The card mode and chosen CSL style persist in `~/.config/bib/state.json`.

## CSL styles

`bib` renders the reference card in-process with [citeproc-py](https://github.com/citeproc-py/citeproc-py)
(CSL 1.0.1). It bundles the subset of the styles [Typst](https://typst.app) ships that citeproc-py can
render, named by their CSL id so they match Typst's `#bibliography(style: …)`. Styles that rely on
CSL 1.0.2 features (APA, MLA, Chicago author-date, …) are not renderable by citeproc-py and so aren't
offered. Override the active style file with `BIB_CSL_STYLE=/path/to/style.csl`.

The bundled `.csl` files are from the Citation Style Language project and are licensed CC-BY-SA 3.0 —
see `bib/styles/csl/ATTRIBUTION.md`.

## Acknowledgements

`bib` stands on the work of others, with thanks:

- **[papis](https://github.com/papis/papis)** — the reference manager it is built on. `bib`
  reads your papis library and config and writes citations and metadata back through papis.
- **[Semantic Scholar](https://www.semanticscholar.org/)** — citation counts, influential-citation
  counts, and reference lists, via the Academic Graph API.
- **[OpenAlex](https://openalex.org/)** — the complete cited-by graph and reference counts for
  DOI-less papers.
- **[Crossref](https://www.crossref.org/)** — reference lists and bibliographic metadata.
- **[citeproc-py](https://github.com/citeproc-py/citeproc-py)** and the
  **[Citation Style Language](https://citationstyles.org/)** project — in-process CSL rendering
  and the bundled styles.

## License

MIT (this project's own code). Bundled CSL styles: CC-BY-SA 3.0 (see attribution above).
