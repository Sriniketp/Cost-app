# UK TIMES Cost Allocation App — Full Processing Flow

## Overview

The app has a single entry point:

| Entry point | Module | Purpose |
|-------------|--------|---------|
| Dashboard | `app.py` | Interactive Streamlit app (`streamlit run tools/cost_allocation/app.py` or `run_app.cmd`) with tabs for exploration and reconciliation |

The legacy CLI allocation engine (`__main__.py`, `classify.py`, `flow_matrix.py`,
`allocate.py`, `validate.py`, `discount.py`, `outputs.py`) was removed in Phase 5.
Process classification comes only from the authoritative SQL process/commodity
labels (`labels.py`); dashboard fuel-cost allocation lives in `energy_scope.py`
and `resource_allocation.py`.

Data travels through the sequential stages below. Each stage transforms a well-defined input into a well-defined output.

---

## Stage 1 — Retrieve and Canonicalise VD Rows (`sql_connection.py`, `vd_sql.py`)

**Input:** One or more immutable SQL RunLog IDs

**Processing:**
- `usr.Get_RawVD @RunList=?` retrieves one raw run through a parameterized query.
- The adapter verifies the exact stored-procedure schema and that every
   returned `run_id` equals the requested immutable run ID.
- `vd_sql.py` maps the SQL columns and sentinels to the established canonical
   VD fields.
- `canonicalise_vd_frame()` validates nullable whole periods, finite values,
   and preserves exact duplicates.
- The prepared frame and immutable SQL provenance are injected into the core
   pipeline. The core pipeline does not open files or database connections.

The dashboard has no runtime file or manual-upload fallback. The text
parser in `ingest.py` is retained only for offline fixtures and parity tests.

**Output DataFrame schema:**

| Column | Type | Notes |
|--------|------|-------|
| Scenario | str | e.g. `CB7_dev_155` |
| Attribute | str | e.g. `Cost_Inv`, `VAR_Ncap`, `VAR_FIn` |
| Commodity | str | e.g. `ELCC`, `NGASC` |
| Process | str | e.g. `RESDHP01` |
| Period | int | Milestone year (2010–2060) |
| Region | str | e.g. `UK` |
| Vintage | str/int | Build year (for VAR_Ncap / Cost_Inv) |
| TimeSlice | str | e.g. `ANNUAL`, `SD`, `WN` |
| UserConstraint | str | Usually empty |
| Value | float | Reported value in model units |

**Key constants:** `VD_COLUMNS` (9 fields), `ESSENTIAL_ATTRIBUTES` (warn if missing), `COST_ATTRIBUTES` (14 cost types).

---

> **Process classification:** the legacy CLI engine's `classify.py` was removed
> in Phase 5. Processes and commodities are classified only from the
> authoritative SQL process/commodity labels loaded by `labels.py`.

---

## Stage 2 — Cost Input Extraction (`cost_extraction.py` / `dd_inputs.py`)

**Input:** GAMS DD files at the run's uktm-dds commit; VD DataFrame; DD
precedence from the run's `.RUN` file (`$BATINCLUDE` order)

### Option B — DD files (`load_effective_dd_inputs`)

- The `.RUN` file at the run's commit gives the ordered DD list
  (`run_selector.extract_dd_list_from_run_file`)
- `_extract_rows_from_dd(*.dd)` parses PARAMETER blocks, normalises tokens by attribute type (region, process, year, currency)
- Collapses by include order: a later DD file overwrites an earlier one for the same key
- DD files not listed in the `.RUN` file are dropped
- If a run's commit, `.RUN` file or DD files cannot be resolved, loading the
  run fails with `RunDdUnavailableError` rather than continuing with empty
  input costs. There is no other source of scenario precedence (the old
  `dev_model_files.cpk` lookup was removed).
- A DD file listed in the `.RUN` file but absent at the run's commit is not
  fatal: the run loads, the missing file names are logged and recorded on
  `RunResult.missing_dd_files`, and the dashboard shows a warning naming them.

**Output dict:**
```python
{
    "capex":    DataFrame (index=Process, columns=year strings)  # NCAP_COST
    "fom":      DataFrame  # NCAP_FOM
    "act_cost": DataFrame  # ACT_COST
    "totcapex": DataFrame  # VAR_Ncap × NCAP_COST per process/year
}
```

---

## Stage 3 — Cost Back-Calculation & Reconciliation (`cost_backcalc.py`)

**Input:** VD DataFrame (`VAR_Ncap`, `Cost_Inv`); `input_costs` dict; `ReferenceData`

**Purpose:** Verify that `Cost_Inv` reported in VD matches the theoretical annuity computed from NCAP_COST × VAR_Ncap × CRF × re-levelization factor.

### Key formulas

**Capital Recovery Factor (mid-year convention):**
```
CRF(r, L) = (1 - (1+r)^(-1)) / (1 - (1+r)^(-L)) × (1+r)^0.5
```

**Levelized Cost_Inv annuity:**
```
annuity = NCAP_COST × VAR_Ncap × CRF(drate, elife)
                    × CRF(gdrate, tlife) / CRF(gdrate, elife)   # re-levelization
                    × (1 + gdrate)^(|iled| / 2)                 # IDC for Case 2 only
```

**Investment Case Classification** (`$SET OBLONG YES`):

| Case | Condition | Behaviour |
|------|-----------|-----------|
| 1a | `|ILED| ≤ ILEDMin` AND `TLIFE + |ILED| ≥ D(t)` | Single investment, fills period |
| 1b | `|ILED| ≤ ILEDMin` AND `TLIFE + |ILED| < D(t)` | Repeated investment, C cycles |
| 2a | `|ILED| > ILEDMin` AND `TLIFE + |ILED| ≥ D(t)` | Large project, single cycle |
| 2b | `|ILED| > ILEDMin` AND `TLIFE + |ILED| < D(t)` | Large project, repeated |

where `ILEDMin = min(D(t)/10, TLIFE/10)`.

**OBLONG Pay Fraction** (partial-period overlap):
```
pay_frac(k, D, g) = AF(g, k) / AF(g, D)
                  = (1 - (1+g)^(-k)) / (1 - (1+g)^(-D))
```

where `k` = overlap years between vintage payment window and period, `D` = period duration.

**Forward-vintage guard:** `pay_frac = 0` when `period < vintage` (prevents future build costs bleeding into earlier periods under OBLONG period-start shifts).

**Output DataFrame** from `reconcile_cost_inv()`:

| Column | Description |
|--------|-------------|
| process | Process name |
| period | Milestone year |
| cost_inv_vd | Actual `Cost_Inv` from VD |
| expected_cost_inv | Theoretical annuity sum across active vintages |
| cost_inv_gap_pct | `(actual - expected) / expected × 100` |
| reason | `MATCH` (≤5%) or `MISMATCH` (>5%) |
| investment_case | `case_1a` / `case_1b` / `case_2a` / `case_2b` |
| has_nonzero_iled | Whether any vintage has ILED ≠ 0 |
| n_build_vintages | Number of active vintages contributing |

**`ReferenceData` class:**
- Built in memory from the run's effective DD table with
  `ReferenceData.from_frame(effective_dd_inputs)` — once per run at load in the
  dashboard, and by `run_reconciliation` when no reference data is passed.
  There is no temporary CSV; the path constructor remains for CSV exports only.
- Loads DD export into nested dict `{attribute → {(scenario, process, year) → value}}`
- `get(attribute, process, year)`: direct lookup of DD-derived values, with exact-year fallback across scenarios only
- `get_profile_info(process, year)`: returns `{drate_pct, elife_years, tlife_years, iled_years, gdrate}` with fallback cascade; results cached per `(process, year)`
- `get_gdrate(year)`: time-varying social discount rate with SysSettings → BASE → any fallback

**Period Durations:**
```python
{2010:1, 2011:1, 2012:1, 2015:5, 2020:5, 2025:5, 2030:5, 2035:5,
 2040:5, 2045:5, 2050:5, 2055:5, 2060:5}
```

---

> **Removed:** the legacy CLI stages for commodity-flow matrix tracing
> (`flow_matrix.py`), sector cost allocation (`allocate.py`), the validation gate
> (`validate.py`), discount reporting (`discount.py`) and CSV/chart output
> (`outputs.py`) were removed in Phase 5. Dashboard fuel-cost allocation is in
> `energy_scope.py` and `resource_allocation.py` (see Stage 4).

---

## Stage 4 — Streamlit Dashboard (`app.py`)

### Resource & Fuel pathways

The Resource & Fuel Pathways page keeps physical and monetary semantics separate:

- **Reviewed pools:** routing aliases keep the corresponding canonical primary-carrier pool so same-pool pass-through remains suppressible. Distinct CHP, heat and useful-energy carriers retain separate reviewed pools for visibility, but stages are not additive whole-system energy totals. Reviewed exact-code pools cover industry byproducts, space heating, conservation services and low-temperature waste-heat delivery. CCS-retrofit proxies are excluded as internal accounting links. `ELCWSTHEAT` and `PRCWSTHEAT` map to a reviewed `Waste heat` generation pool, so co-producing plants split their source costs by observed output share.
- **Physical evidence:** model year → resource pool → authoritative individual fuel (`commodity_name`, or **All fuels**, which applies the per-fuel netting below to each fuel and sums the results). Positive compatible VDT-matched annual rows are pooled across exact commodity codes sharing the selected fuel, at each `(region, process)` boundary. Output minus selected-fuel input determines one endpoint: positive net is Supply, negative net is Demand, and balanced same-fuel pass-through is suppressed. The visible graph is Supply → Individual fuel → Demand; exact codes remain auditable evidence rather than intermediate nodes.
- **Stakeholder policy:** hide processes authoritatively classified in Coal/Gas/Oil Extraction and the export-direction legs of processes labelled subsector "Fuel Re-imports". Retain genuine imports, genuine exports, and import-side re-import processes. Technical mode restores all valid hidden edges. Never filter by a broad `dummy` or `domestic` substring.
- **Grouping:** one presentation-only process grouping applies to both physical endpoint sides and defaults to Subsector. Process-level netting and all-evidence balances are calculated before grouping.
- **Evidence:** process-net, source/output, demand/input, hidden-evidence, exact-code balance, unmatched-edge, and diagnostic CSVs retain exact codes, visibility reasons, annualization mode, row counts and connection roles. Residuals and supply-demand gaps are reported without fabricated balancing links.
- **Boundary:** the graph stays within the selected `commodity_name`; upstream commodities with another name or pool are not recursively added.
- **Monetary:** cost attribution remains at reviewed resource-pool level. Cost used by a converter (power station, electrolyser, hydrogen turbine, boiler) follows the pool it produces, solved end-to-end so only final use keeps cost; the pathway Sankey shows converters as ordinary supply and demand processes, plus unallocated cost. Individual-fuel selection does not create unsupported fuel-specific cost estimates.
- **Workbook boundary:** reviewed secondary pools remain in dynamic evidence and pathway sheets but are intentionally excluded from fixed primary-fuel TWh, source-cost, Resource Cost and Fuelex tables. The Checks sheet states that source-fuel and Electricity (distributed) rows are not additive across sectors, and names any model year with Power fuel cost but no core Power cost. A test asserts that every `resource_pool` in `tools/lookup_tables/resource_pools.csv` is either mapped to a source fuel, listed as a reviewed non-reference pool, or a demand pool, so a newly added pool cannot be dropped from the fixed layout unnoticed. Another asserts that every NRG commodity in `commodity_labels.csv` appears in the sheet exactly once.

Exact-code movements through terminals, transformation, distribution, or refuelling are netted only at the selected-fuel process boundary. The chart shows neither gross pass-through nor synthetic production.

**6 Tabs** (see `_MAIN_ANALYSIS_TAB_LABELS` in `app.py`, which is the authoritative list):

| Tab | Content |
|-----|---------|
| Run Selector | Choose runs to load; nothing else is read until runs are selected |
| Overview | Headline metrics, cost chart, sector breakdown, investment profile summary |
| Technology Drill-Down | Per-process CRF calc, vintage detail, annuity chart, payment stream |
| Fuel Cost Attribution | Proportional attribution of upstream fuel costs to consuming sectors |
| Resource & Fuel Pathways | Physical and monetary Sankey pathways, workbook export |
| Methodology | Definitions, formulas, and the Hydrogen attribution proof |

**Key shared helpers:**
- `build_cost_table()` — joins VD to the process label taxonomy for sector assignment
- `compute_period_durations()` — derives `D(t)` from milestone year list

---

## Data Flow Diagram

```
 SQL run ID(s)
    │
    ▼  sql_connection.py → vd_sql.py → ingest.py canonicalization
 Combined DataFrame (Scenario, Attribute, Commodity, Process, Period, ...)
    │
    ├─────────────────────────────────────────────────────────────┐
    │                                                             │
    ▼  labels.py (SQL process/commodity labels)                  ▼  cost_extraction.py / dd_inputs.py
 Labelled cost table                                         input_costs {capex, fom, act_cost, totcapex}
    │                                                             │
    ├──────────────────────────────────┐                          │
    │                                 │                          │
    ▼  energy_scope.py                ▼  cost_backcalc.py ◄──────┘
    │  resource_allocation.py      ReferenceData
 Fuel Cost Attribution /              │
 Resource & Fuel Pathways             ▼
  (app.py tabs)                reconcile_cost_inv()
                               compute_reconciliation_summary()
                                      │
                                Reconciliation Tab (app.py)
                                Drill-Down Tab (app.py)
```

---

## Module Dependency Graph

```
app.py      ──► pipeline, run_config, cost_backcalc, labels, run_selector, data_cache, tabs/*
pipeline.py ──► dd_inputs, labels, energy_scope, vdt, run_selector, run_config, cost_backcalc (lazy)
energy_scope.py ──► constants, labels, vdt
resource_allocation.py ──► constants, helpers, labels, resource_rules, text_norm
labels.py   ──► sql_connection
cost_backcalc.py ──► (no internal deps)
cost_extraction.py ──► (no internal deps)
dd_inputs.py ──► cost_extraction
colours.py  ──► (no internal deps)
```

No circular imports — all import chains are acyclic.

---

## TIMES Model Settings (UK TIMES CB7)

| Setting | Value | Effect |
|---------|-------|--------|
| `$SET MIDYEAR YES` | On | CRF uses mid-year convention: `CRF_mid = CRF_beg × (1+r)^0.5` |
| `$SET OBLONG YES` | On | Period boundaries: `B(t) = M(t) - ⌊D(t)/2⌋`, `E(t) = B(t) + D(t)` |
| `OBJ=AUTO` → RPT_OPT('OBJ','2')>0 | Levelized | `Cost_Inv` levelized over TLIFE at G_DRATE |
| `VDA=YES` | On | Vintage-dependent attributes enabled |
| G_DRATE (SysSettings) | 0.035 (2015–2050), 0.030 (2055–2060) | Social discount rate for re-levelization |
