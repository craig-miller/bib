# Bundled CSL styles — attribution

The `*.csl` files in this directory are Citation Style Language styles from the
**Citation Style Language project** (https://github.com/citation-style-language/styles),
obtained via the `citeproc-py-styles` package (which pins a citeproc-py-compatible snapshot
of that repository).

They are licensed under **Creative Commons Attribution-ShareAlike 3.0 Unported (CC BY-SA 3.0)**
— https://creativecommons.org/licenses/by-sa/3.0/ — and remain the work of their respective
authors (see the `<author>`/`<contributor>` and `rights` fields inside each file).

The bundled set is the subset of the styles Typst vendors that citeproc-py 0.10 (CSL 1.0.1)
can render, so the reference card's style ids match Typst's `#bibliography(style: …)` names.
`taylor-and-francis-harvard-x.csl` is the default.
