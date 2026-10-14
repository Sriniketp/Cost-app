# AGENTS.md

Guidance for AI coding agents working in this repository.

The rules live in **[.github/copilot-instructions.md](.github/copilot-instructions.md)**
(concise, always-on) and **[Documentation/DEVELOPMENT_GUIDE.md](Documentation/DEVELOPMENT_GUIDE.md)**
(full detail). Read both before making changes.

They are deliberately not repeated here — one copy, one source of truth.

Two things worth knowing before you touch anything:

1. This app produces cost numbers used for analysis. Never change a formula,
   tolerance, or classification rule that was not explicitly requested.
2. Never run a blanket `ruff check --fix`. Its unused-import fixer strips
   load-bearing re-exports from `tools/cost_allocation/ui_helpers.py` and breaks
   every dashboard tab.
