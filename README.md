# Cost Allocation App

A Streamlit dashboard that reconstructs, allocates, and reconciles cost
outputs from the UK TIMES (UKTM) energy system model.

It reads model results from UK-TIMES-WKG SQL, model inputs (GAMS `.DD` files),
and label lookup tables, attributes costs to sectors, and reports any divergence
from the model's own reported totals.

---

## Quickstart

Requires **Python 3.12**. The app uses Streamlit 1.61 features for dynamic and
lazy tabs.

**First-time users: double-click `run_app.cmd`.** On first launch it:

1. Finds Python 3.12 (`py -3.12`, `python`, or the default per-user install).
   If none is found and `winget` is available, it offers to install Python 3.12
   for your user account.
2. Creates a virtual environment in `%LOCALAPPDATA%\cost-allocation-app\.venv`.
3. Installs `requirements.txt` and runs `pip check`.
4. Starts the app in your browser.

Later launches skip straight to step 4. If `requirements.txt` changes (for
example after a `git pull`), the next launch reinstalls automatically. A failed
install is retried on the next launch. The ODBC driver for SQL Server is a
system component and is not installed by the script.

Manual setup, if you prefer:

```powershell
py -3.12 -m venv "$env:LOCALAPPDATA\cost-allocation-app\.venv"
& "$env:LOCALAPPDATA\cost-allocation-app\.venv\Scripts\python.exe" -m pip install -r requirements.txt
& "$env:LOCALAPPDATA\cost-allocation-app\.venv\Scripts\python.exe" -m pip check
.\run_app.cmd
```

`pip check` must report no conflicts. In particular Streamlit 1.61 requires
PyArrow 7 through 24.

`run_app.cmd` picks an interpreter in this order:

1. `COST_ALLOCATION_PYTHON` (used as-is; never modified)
2. `%LOCALAPPDATA%\cost-allocation-app\.venv` (managed: created and kept in
   sync with `requirements.txt`)
3. `.venv\Scripts\python.exe` (used as-is; never modified)
4. None found: create the managed environment (first-run setup above)

The local-appdata location matters when the repository lives on a network
drive, where a virtual environment cannot be created in place.

Direct launch, if you prefer:

```powershell
python -m streamlit run tools/cost_allocation/app.py
```

### How the app loads

- On launch only the **Run Selector** renders. Nothing is read until you select
  runs.
- The RunLog and RunGroups are read from UK-TIMES-WKG SQL and cached for five
  minutes. Run IDs and group codes beginning with `#` are soft-deleted and are
  not shown.
- The latest numeric `CB7_dev_xxx` run is pinned first. Already loaded runs are
  listed next, and the selected/loaded summaries stay above the individual-run
  and run-group tables.
- **Load selected runs** performs the one-time preparation stage per run, then
  the analysis tabs appear.
- Only the open tab renders. The first visit to a tab may do extra work;
  revisits reuse the prepared run.
- Raw VD rows are retrieved only from `usr.Get_RawVD`; SQL failures block the
  selected run rather than falling back to a file.

---

## Configuration

The former `python -m cost_allocation` command line was removed in Phase 5;
the dashboard is the only entry point.

The default database is `UK-TIMES-WKG` on `VMSPDB06.cbas.cloud`, using
`ODBC Driver 17 for SQL Server` and Windows trusted authentication. Override
`DB_DRIVER`, `DB_SERVER`, or `DB_DATABASE` when required. Set `DB_DEPLOY=true`
for Active Directory managed-identity authentication.

**DD/VDT source:** each run's `.RUN` file (named after the run ID), its DD
files and its VDT are read from the run's commit in
[uk-times-off-sen/uktm-dds](https://gitlab2.cbas.cloud/uk-times-off-sen/uktm-dds).
The app keeps its own bare mirror under `~/.cache/cost_allocation/uktm-dds.git`,
cloned on first use with your normal git credentials and fetched when a new run
ID is not found. No local path needs configuring.

Optional overrides: `UKTM_DDS_DIR` reuses an existing local clone instead of
the mirror; `UKTM_DDS_REPO_URL` points at a different https remote.

---

## Repository layout

| Path | Contents |
|---|---|
| `tools/cost_allocation/` | The application package: pipeline, domain maths, I/O, and `tabs/` for the dashboard UI |
| `tools/cost_allocation/tests/` | pytest suite |
| `tools/build_base_data.py` | Standalone script — see below |
| `tools/tabulate_dd_inputs.py` | Standalone script — see below |
| `tools/lookup_tables/` | Tracked reference data: label maps, cost nature rules, resource pools |
| `Documentation/` | All documentation — start at [Documentation/README.md](Documentation/README.md) |
| `requirements.txt` | Runtime dependencies |
| `requirements-dev.txt` | Test and lint tooling |
| `pyproject.toml` | ruff and pytest configuration |

---

## Standalone scripts

Both live in `tools/` and run independently of the dashboard.

### `tools/build_base_data.py`

Builds a single effective-value CSV from GAMS DD input files: extracts
cost-related `PARAMETER` blocks, applies scenario precedence from the `.RUN`
file's `$BATINCLUDE` order (later file wins), and writes the result for the
reconciliation app to consume.

```powershell
python tools/build_base_data.py [DD_DIR] [RUN_FILE] [OUTPUT]
```

`DD_DIR` (a folder of `.dd` files plus the `.RUN` file) defaults to
`$UKTM_DDS_DIR` and is required if that is unset; the others default to `Ref_Outputsplit_test02.RUN` and
`tools/lookup_tables/base_data.csv`.

### `tools/tabulate_dd_inputs.py`

Exports a DD audit trail with precedence and TIMES
interpolation/extrapolation applied — used to explain where an input value came
from.

```powershell
python tools/tabulate_dd_inputs.py --run-id Ref_prax_closure01
python tools/tabulate_dd_inputs.py --run-id Ref_prax_closure01 --excel
python tools/tabulate_dd_inputs.py --dd-dir <folder with .dd files> --run-file Ref_Outputsplit_test02.RUN
```

Outputs land under `tools/outputs/dd_tabulation/`:

| File | Contents |
|---|---|
| `dd_include_order.csv` | Ordered DD files from `$BATINCLUDE` |
| `dd_lineage_all_rows.csv` | Every parsed DD row with winner flags |
| `dd_effective_after_precedence_and_ie.csv` | Final effective values |
| `dd_tabulation.xlsx` | All of the above as sheets, with `--excel` |

---

## Documentation

Start at **[Documentation/README.md](Documentation/README.md)**, which indexes
everything. The two you are most likely to want:

- [Documentation/ARCHITECTURE.md](Documentation/ARCHITECTURE.md) — how the app
  works and the cost methodology it implements
- [Documentation/DOMAIN_REFERENCE.md](Documentation/DOMAIN_REFERENCE.md) — TIMES
  terms, cost parameters, and quick reference tables

---

## Contributing

Install the tooling and run both checks before proposing a change:

```powershell
pip install -r requirements-dev.txt
pytest
ruff check .
```

Read **[Documentation/DEVELOPMENT_GUIDE.md](Documentation/DEVELOPMENT_GUIDE.md)**
first. It covers module boundaries, numerical-integrity rules, the tolerances
you must not quietly change, and the before-merge checklist.
