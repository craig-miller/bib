"""papers — a citation-graph reference-library TUI built on papis."""
import os

# Disable papis's multiprocessing BEFORE any submodule imports papis. papis parallelises document
# matching (filter_documents → parmap → multiprocessing.Pool); under Textual, sys.stdout/stderr are
# replaced with objects whose fileno() is -1, so the Pool worker spawn dies with "bad value(s) in
# fds_to_keep". PAPIS_NP=0 forces serial matching (a documented papis knob) — plenty for interactive
# single queries. Set here so it applies no matter which entry point or submodule loads papis first.
os.environ.setdefault("PAPIS_NP", "0")

__version__ = "0.1.0"
