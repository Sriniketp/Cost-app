# Development Guide

Working rules for changing this repository. The short, always-on summary lives
in [.github/copilot-instructions.md](../.github/copilot-instructions.md); this
document is the detail behind it.

Read this before your first change. After that, the
[before-merge checklist](#15-before-merge-checklist) is usually enough.

---

## 1. What this application is

A Streamlit dashboard that reconstructs, allocates, and reconciles cost
outputs from the UK TIMES energy system model. It reads model results from
UK-TIMES-WKG SQL, model input files (DD), and label lookup tables, then
attributes costs to sectors and explains any divergence from the model's own
reported totals.

The output is used for analysis and reporting. **Silently wrong numbers are far
worse than a visible error.** Most rules below follow from that.

---

## 2. Layout and module boundaries

```
tools/cost_allocation/      the application package
  app.py                    Streamlit entry point (the only entry point)
  tabs/                     one module per dashboard tab (UI only)
  tests/                    pytest suite
tools/build_base_data.py    standalone script: build merged base data from DDs
tools/tabulate_dd_inputs.py standalone script: DD lineage audit tables
tools/lookup_tables/        tracked reference data (do not edit casually)
Documentation/              see Documentation/README.md
```

Layers, strictest first:

| Layer | Modules | Rule |
|---|---|---|
| Domain / maths | `cost_backcalc`, `resource_allocation`, `energy_scope`, `attribution_proof` | **Must not import Streamlit.** Pure functions over DataFrames. |
| I/O and parsing | `sql_connection`, `vd_sql`, `ingest`, `vdt`, `dd_inputs`, `cost_extraction`, `labels`, `run_selector`, `workbook_*`, `resource_export` | No Streamlit except where explicitly noted. Owns database and file access. |
| UI | `tabs/*`, `ui_helpers`, `app` | Streamlit lives here. Should contain presentation logic, not domain maths. |
| Shared | `helpers`, `text_norm`, `colours`, `price_year`, `run_bundle`, `data_cache`, `resource_rules` | Importable from anywhere. Keep dependency-light. |

Specific constraints that already exist and must be preserved:

- **`energy_scope.py` is documented as pure** — it must not import Streamlit or
  the classification engine. This is why `text_norm.py` exists as a
  dependency-free home for shared normalisation.
- **`ui_helpers.py` is a facade.** Tab modules import `APP_BUILD`, `RunBundle`,
  `money_unit`, `go`, `px` and others *through* it. Those imports look unused
  locally but are load-bearing re-exports. The file is `F401`-exempt in
  `pyproject.toml` — never "clean up" its imports.
- **The `sys.path` bootstrap block at the top of `app.py` is deliberate.**
  The entry point must repair `sys.path` before any `cost_allocation` import,
  so the logic cannot live in an importable module. It is `E402`-exempt. Do
  not "simplify" the network-drive handling — it exists because `.resolve()`
  turns a mapped drive letter into a UNC path and breaks imports.

---

## 3. Numerical integrity

This is the rule that matters most.

**Never change a formula, constant, or tolerance in the domain layer unless the
change was explicitly requested, and never without a test that pins the new
behaviour.** That includes:

- CRF, annuity, re-levelisation, and IDC calculations
- OBLONG period boundaries and pay-fraction proration
- Investment case classification (1a / 1b / 2a / 2b)
- Discounting, `Time_NPV`, and objective identities
- Allocation shares and residual routing

Current tolerances, and where they live:

| Constant | Value | Location | Meaning |
|---|---|---|---|
| MATCH threshold | 5.0 % | `cost_backcalc.py` | Reconciliation MATCH vs MISMATCH |

The legacy CLI validation tolerances (`CONSERVATION_TOL`, `OBJZ_TOL`,
`SHARE_GAP_WARN` in `validate.py`) were removed with the CLI in Phase 5.

Loosening a tolerance to make a check pass is never an acceptable fix. If a
number does not reconcile, find out why; the divergence is usually the finding.

Related standing constraint (inherited from the performance work): **UI
execution order may be changed freely, but TIMES formulas, DD precedence,
classification, and reconciliation semantics must stay identical.**

---

## 4. pandas conventions

- Work on copies. `df.copy()` before mutating a frame you did not create.
- No chained assignment (`df[a][b] = ...`). Use `.loc`.
- Normalise identifier codes with `text_norm.normalise_code()` rather than
  writing `.fillna("").astype(str).str.strip().str.upper()` again. It returns
  `""` for missing values, not `NaN`, because downstream filters use `.eq("")`
  and `.ne("")` which silently drop `NaN`.
- Handle the empty frame. Every function that filters or groups should behave
  sensibly when the result is empty — the dashboard regularly has periods or
  sectors with no rows.
- Be explicit about dtypes on `Period` (nullable integer) and `Value` (float).
- Prefer named columns over positional access; VD column order is a parsing
  detail, not an API.

---

## 5. Workbook and artifact compatibility

`workbook_layout.py` and `workbook_writer.py` define the exported XLSX
contract: sheet names, sheet order, column headings, and the glossary. Analysts
and downstream spreadsheets depend on that shape.

Treat any change to sheet names, column headings, column order, or units as a
**breaking change**: call it out explicitly and update
`tests/test_workbook_layout.py` and `tests/test_resource_export.py` in the same
change. `test_resource_export.py` already asserts that every retained model term
appears in the workbook glossary.

---

## 6. Streamlit conventions

- Tab modules are imported lazily by `app.py`. Keep import-time work minimal;
  no file reads or computation at module scope.
- Session state keys are declared in `_SESSION_DEFAULTS` in `app.py`. Add new
  keys there rather than inventing them inline, and prefix internal keys with
  `_`.
- The authoritative tab list is `_MAIN_ANALYSIS_TAB_LABELS` in `app.py`. Renames
  need an entry in `_MAIN_ANALYSIS_TAB_RENAMES` so existing sessions survive.
- Nothing is loaded until runs are selected. Preserve that: the Run Selector is
  Stage 1, everything else is Stage 2.
- Only the open tab renders. Do not add eager work that defeats this.
- Gate every chart on `HAS_PLOTLY` before touching `go` or `px`; they are `None`
  when Plotly is missing.
- Use `width="stretch"` / `width="content"`, not the removed
  `use_container_width`.

---

## 7. Performance

Caching layers, in order of cost:

1. `data_cache.py` — `@st.cache_data` wrappers over SQL VD/RunLog and label
  loading, and the reconciliation result cache.
2. `run_bundle.py` — the prepared per-run object shared by all tabs. It holds
  the run's DD reference data (`ReferenceData.from_frame`), built once in
  memory at load; nothing is written to a temporary file.
3. `run_selector.py` — per-run DD/VDT extraction cache under
  `~/.cache/cost_allocation`. A DD cache entry is reused only when its
  `.complete` marker exists.

Rules:

- VD caches must key on immutable run ID and safe logical SQL source identity;
  label caches key on the SQL label-table identity; caches over in-memory frames
  key on content fingerprints (`helpers.frame_fingerprint`). A missing source
  must raise, never produce a usable cache key. Never put credentials or full
  connection strings in cache keys.
- Do not add parallel DD parsing without timing evidence. This was evaluated
  and deliberately deferred.
- Measure before optimising; stage-level pipeline timings are already available
  in the sidebar.

---

## 8. Testing

- Suite lives in `tools/cost_allocation/tests/`. Run it with `pytest`.
- A change to domain maths needs a test that would fail without the change.
- A bug fix needs a regression test that reproduces the bug.
- Never weaken, skip, or `xfail` an existing test to get a green run. If a test
  is genuinely wrong, say so explicitly and explain why in the change
  description.
- `test_methodology_documentation.py` asserts that specific formulas and phrases
  are present in `Documentation/ARCHITECTURE.md`. It will fail if you move or
  reword those sections — that is intentional.

Modules with little or no direct unit coverage, so extra care is needed when
changing them: `run_selector.py`.

---

## 9. Dependencies

- Runtime dependencies go in `requirements.txt`; test and lint tooling goes in
  `requirements-dev.txt`.
- Anything imported must be declared. Do not rely on a transitive dependency.
- `kaleido` and `pyarrow` are declared but never imported directly — Streamlit
  serialises DataFrames through Arrow. `kaleido` backed `fig.write_image()` in
  the removed CLI `outputs.py`; whether to drop it is a separate, owner-agreed
  decision. **Do not remove either based on an unused-import scan.** Both
  carry explanatory comments.
- Keep the upper bounds. Streamlit 1.61 requires `pyarrow>=7,<25`; the app uses
  Streamlit 1.61 tab and width APIs.

---

## 10. Linting and formatting

`ruff` is the arbiter. Configuration is in `pyproject.toml`, which contains tool
configuration only — deliberately no `[build-system]` or `[project]` table,
because the entry point bootstraps `sys.path` itself and making the project
installable would change how imports resolve.

- Run `ruff check .` before proposing a change. It should stay clean.
- **Never run a blanket `ruff check --fix` on this repository.** Its
  unused-import fixer strips the load-bearing re-exports from `ui_helpers.py`
  and breaks every tab. Fix findings rule by rule, and run the tests
  immediately afterwards.
- Do not run `ruff format` across the repository. Blanket reformatting destroys
  reviewability.
- The `ignore` list in `pyproject.toml` is a **burn-down list**, not an
  endorsement. Each entry records the rule, the reason, and the approximate
  count. Fix them opportunistically when you are already touching a file, and
  delete the line once the count reaches zero. Re-check any rule with
  `ruff check . --select <CODE> --statistics`.

---

## 11. File placement

- Application code → `tools/cost_allocation/`.
- Standalone operational scripts → `tools/`, with a docstring explaining
  purpose, inputs, outputs, and how to run them.
- Tests → `tools/cost_allocation/tests/`, named `test_*.py`.
- Documentation → `Documentation/`, indexed in `Documentation/README.md`.
- **Nothing scratch at the repository root.** Ad-hoc inspection scripts and
  their output dumps do not belong in version control; `.gitignore` blocks the
  `_inspect_*` pattern because a batch of them accumulated there previously.
- Generated workbooks, exports, caches, and virtual environments are ignored.
  If you add a new generated output location, add it to `.gitignore` in the
  same change.

---

## 12. Documentation policy

- `Documentation/ARCHITECTURE.md` and `Documentation/DOMAIN_REFERENCE.md` are
  canonical. Link to them; do not restate their content elsewhere.
- `Documentation/external-reference/` is read-only third-party material. Never
  edit it.
- `Documentation/domain-notes/` holds working notes, not specifications. Where
  a note and the canonical documents disagree, the canonical documents win.
- When you change behaviour that a document describes, update the document in
  the same change.
- Prefer deleting superseded documentation over leaving two versions. Git
  history is the archive.

---

## 13. Public interfaces

Treat these as contracts requiring explicit agreement to change:

- The exported workbook shape (section 5)
- Tab names and session-state keys that persist across reruns
- Function names re-exported through `ui_helpers.py`
- The tracked lookup-table schemas in `tools/lookup_tables/`

---

## 14. When to stop and ask

Stop and raise it rather than guessing when a change would:

- alter a domain formula, tolerance, or classification rule;
- change the workbook output shape;
- delete a file whose consumers you cannot prove;
- require editing `tools/lookup_tables/` data;
- depend on an assumption about TIMES behaviour you cannot verify from the
  documentation in this repository;
- touch the `sys.path` bootstrap or network-drive handling.

---

## 15. Before-merge checklist

- [ ] `pytest` passes, and the count is what you expect
- [ ] `ruff check .` is clean
- [ ] The app launches (`run_app.cmd`) and the affected tabs render
- [ ] No new files at the repository root
- [ ] No tolerance, formula, or classification change that was not asked for
- [ ] Documentation updated if behaviour changed
- [ ] New dependencies declared in the right requirements file
- [ ] The change is small enough to review, and revertible on its own

---

## 16. Known debt

Carried forward deliberately. None of it is blocking; all of it is worth fixing
when you are next in the area.

**Oversized modules** (line counts approximate):

| Module | Lines | Note |
|---|---:|---|
| `resource_allocation.py` | ~3220 | Well covered by tests; splittable |
| `cost_backcalc.py` | ~2405 | Highest risk. Do not split without a characterisation harness that snapshots `reconcile_cost_inv()` and `build_vintage_table()` on a real run |
| `tabs/resource_allocation.py` | ~1885 | UI and data prep are mixed |
| `tabs/drilldown.py` | ~1295 | |
| `workbook_writer.py` | ~1232 | Sheet builders and styling are mixed; add a golden-output test first |
| `tabs/methodology.py` | ~1180 | Mostly static prose; low risk |
| `energy_scope.py` | ~1180 | |
| `tabs/overview_current.py` | ~1058 | Chart builders could be extracted |

Rule for any such split: **move code, do not rewrite it.** Re-export from the
original module path so importers are untouched, then update importers in a
separate follow-up change.

**Duplication left in place, with reasons:**

- `_format_money` exists in both `tabs/methodology.py` (£ millions) and
  `tabs/resource_allocation.py` (£ billions). Same name, different units. Do not
  merge without first agreeing one display convention.
- Session-state cache scaffolding appears in `app.py`,
  `allocation_service.py`, and `tabs/resource_allocation.py` at different
  scopes. Consolidating needs care.
- Period-extraction idioms differ across `ingest.py` and
  `resource_allocation.py` in integer casting and NaN
  handling. They are not equivalent; do not force a shared helper.

**Open performance items:**

- Cache or reuse the derived power-generation summaries used by the Overview
  power view.
- Evaluate a versioned prepared-run artifact cache once timing data exists. It
  must invalidate on SQL run identity/DD source changes and preserve
  `RunResult` semantics.
- Run an interactive validation with real SQL RunLog/VD and DD data: load one
  and multiple runs, visit each tab, use diagnostics, and clear runs.
- Record load timings for representative run sizes before deciding whether
  parallel DD parsing is warranted.

**Lint burn-down:** see the annotated `ignore` list in `pyproject.toml`.
