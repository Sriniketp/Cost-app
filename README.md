# Documentation index

Start here. Each document has one job; where two disagree, the canonical
documents win.

## Canonical

| Document | Read it when you need | Audience |
|---|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | How the app is built and the cost methodology it implements — data flow, module responsibilities, investment cases, fuel cost attribution | Developers, analysts verifying method |
| [PIPELINE.md](PIPELINE.md) | A stage-by-stage walkthrough from SQL retrieval to rendered tab | Developers debugging a run |
| [DOMAIN_REFERENCE.md](DOMAIN_REFERENCE.md) | What a TIMES term, attribute, or cost parameter means, plus quick-reference tables | Everyone |
| [DEVELOPMENT_GUIDE.md](DEVELOPMENT_GUIDE.md) | The rules for changing this repository — boundaries, tolerances, testing, checklists, known debt | Anyone editing code |
| [VISUALIZATION_REFERENCE.md](VISUALIZATION_REFERENCE.md) | Chart conventions, colour usage, and figure patterns | Anyone touching the UI |
| [QA_PLAN.md](QA_PLAN.md) | How to QA a run before its results are shared — automated report, SQL cross-checks, hand traces, sign-off | Analysts, reviewers |
| [VD_SQL_EQUIVALENCE_CONTRACT.md](VD_SQL_EQUIVALENCE_CONTRACT.md) | The implemented UK-TIMES-WKG SQL source, mapping, identity, and failure contract | Database/model operations, developers |

A condensed, always-applied version of the development rules lives in
[.github/copilot-instructions.md](../.github/copilot-instructions.md).

## Supporting material

| Folder | What it is | Status |
|---|---|---|
| [domain-notes/](domain-notes/) | Working notes written while building the app — derivations, investigations, method scratchpads | **Not specifications.** Useful context; may be out of date |
| [external-reference/](external-reference/) | Third-party TIMES documentation and UKTM papers | **Read-only.** Never edit |

## Conventions

- Link to the canonical documents rather than restating them. Duplicated
  explanations drift apart and then contradict each other.
- Update the document in the same change as the behaviour it describes.
- Prefer deleting superseded documentation over keeping two versions; git
  history is the archive.
- `ARCHITECTURE.md` is asserted against by
  `tools/cost_allocation/tests/test_methodology_documentation.py`. Moving or
  rewording the methodology sections will fail that test on purpose.
