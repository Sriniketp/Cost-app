# UK TIMES Cost Allocation App — Complete Technical Documentation

## 1. Document Scope

This document is a full technical reference for:

1. UK TIMES cost concepts used in this codebase.
2. How the app reconstructs, allocates, and validates cost outputs.
3. Why exact process-by-process replication cannot always be achieved.
4. How analysts should interpret mismatches correctly.

The content below is implementation-aligned with the code under `tools/cost_allocation`.

---

## 2. System Context

The repository provides two user-facing workflows over the same core logic:

1. CLI (`tools/cost_allocation/__main__.py`)
2. Streamlit dashboard (`tools/cost_allocation/app.py`)

Both rely on shared modules for ingest, classification, flow tracing, allocation, reconciliation, validation, and outputs.

### 2.1 Core package structure

- `sql_connection.py`: Opens read-only UK-TIMES-WKG connections and executes
	parameterized result reads.
- `vd_sql.py`: Validates and maps `usr.Get_RawVD` rows to the canonical VD
	contract with immutable provenance.
- `ingest.py`: Owns canonical VD validation and offline text-parser fixtures.
- `classify.py`: Process category/sector assignment and enrichment.
- `flow_matrix.py`: Commodity-to-end-use share matrix (iterative tracing).
- `allocate.py`: Cost allocation engine from processes to sectors.
- `dd_inputs.py`: DD parsing, precedence, and TIMES I/E application.
- `cost_extraction.py`: Coefficient table extraction and interpolation behavior.
- `cost_backcalc.py`: `Cost_Inv` reconstruction and reconciliation diagnostics.
- `validate.py`: 5-check validation gate.
- `outputs.py`: CSV and chart generation.
- `discount.py`: discount reporting helper.
- `run_selector.py`: run log and historical DD extraction from git commits.

### 2.2 Supporting scripts

- `tools/tabulate_dd_inputs.py`: DD lineage audit tables.
- `tools/build_base_data.py`: Build merged long-form base data from DDs.

---

## 3. UK TIMES Cost Concepts Used by the App

### 3.1 Primary reported cost attributes

The app works with these TIMES attributes (from VD):

- `Cost_Inv`: annualized investment payments.
- `Cost_Fom`: fixed O&M.
- `Cost_Act`: variable O&M tied to activity.
- `Cost_Flo`: flow/fuel-linked process cost.
- `Cost_Com`: commodity cost component.
- `Cost_Salv`: salvage credit.

And global checks:

- `Reg_ACost`: regional annual cost aggregate.
- `ObjZ`: discounted objective value.
- `Time_NPV`: per-period NPV factor.

### 3.2 Objective identity used in validation

The validator checks objective consistency in the form:

$$
NPV_{allocated} = \sum_t \left(Time\_NPV_t \cdot \sum_s Cost_{s,t}\right) + Cost\_Salv
$$

and compares this with `ObjZ` within tolerance.

### 3.3 Why `Cost_Inv` is different from simple capex

`Cost_Inv` in VD is an annualized, timing-aware value, not a direct capex lump sum. It depends on:

1. build vintage (`VAR_Ncap`),
2. hurdle discount (`NCAP_DRATE`),
3. economic and technical life (`NCAP_ELIFE`, `NCAP_TLIFE`),
4. lead-time (`NCAP_ILED`),
5. social discount (`G_DRATE`),
6. period geometry (`OBLONG`, `D(t)`).

The app reconstructs this using vintage-level annuity logic and OBLONG payment fractions.

---

## 4. Data Inputs and Provenance

### 4.1 SQL VD results

Production rows are retrieved for one immutable run ID from
`usr.Get_RawVD @RunList=?`. The returned SQL schema and run identity are
validated before mapping to the canonical VD shape.

Key ingest behavior:

1. Validate the exact stored-procedure columns and requested `run_id`.
2. Map SQL dimension sentinels to canonical nulls.
3. Parse finite numeric `Value` values.
4. Normalize `Period` to integer nullable type.
5. Attach the immutable run ID as scenario and source version.
6. Record row count and duplicate-sensitive canonical multiset hash.

The text VD parser remains test-only for offline parity diagnostics.

### 4.2 DD files (inputs)

For reconciliation, the app reconstructs effective parameters from DDs:

1. Determine include order from run `.RUN` `$BATINCLUDE` sequence.
2. Parse parameter blocks for target attributes.
3. Apply merge precedence (later include wins per identical key).
4. Interpret year=0 rows as interpolation/extrapolation option codes.
5. Apply TIMES interpolation/extrapolation policy to generate effective values.

#### 4.2.1 Target DD attributes in the implementation

`dd_inputs.py` targets:

- `NCAP_START`, `NCAP_ELIFE`, `NCAP_TLIFE`, `NCAP_ILED`, `NCAP_DRATE`,
- `NCAP_COST`, `NCAP_FOM`, `ACT_COST`,
- `PRC_CAPACT`, `G_DRATE`, `G_DYEAR`, `G_RFRIR`.

#### 4.2.2 TIMES I/E options represented

Implementation supports non-negative interpolation codes and extrapolation modes according to parsed year=0 option values.

Default behavior for missing explicit IE code is option `0` (treated as full interpolation + forward/backward constant extrapolation).

---
## 5. Classification Model

`classify.py` assigns each process:

- category: `end_use | supply | infrastructure | other`
- sector: `Agriculture | CO2 Credits | Electricity | Engineered Removals | Industry | Multiple | Processing | Residential | Resources | Services | Transport | Waste | Other`


The app builds a nested allocation matrix:


2. Trace supply/infrastructure consumption through their output commodities using previous-iteration shares.

For each commodity, the app stores:

$$
gap = 1 - \sum_{sector} share
$$

This gap represents losses, exports, non-end-use leakage, and mapping incompleteness.

---

## 7. Cost Allocation Engine (`allocate.py`)

Allocation operates per scenario and period.


Resource-like attributes:

- `Cost_Inv`, `Cost_Fom`, `Cost_Act`, `Cost_Flo`, `Cost_Com`, `Cost_ire`

`Cost_Salv` is tracked separately as a non-timeseries credit.

### 7.2 Category-specific routing

1. `end_use`: direct assignment to classified sector.
2. `supply`/`infrastructure`: allocated via output commodity shares from the matrix.
3. missing shares or missing output mapping: routed to `Other` to conserve totals.
4. category `other`: kept as unallocated diagnostic bucket.
Supply costs are allocated through output commodity shares (not input commodity shares). This avoids duplicating upstream flows already embedded in downstream process costs.

---
## 8. Investment Reconciliation Engine (`cost_backcalc.py`)

Supports beginning-of-year, end-of-year, and mid-year formulas.

Mid-year convention used by this model:

$$
CRF_{mid} = CRF_{beg} \cdot (1+r)^{0.5}
$$

where the beginning-of-year form is:

$$
CRF_{beg}(r,L)=\frac{1-(1+r)^{-1}}{1-(1+r)^{-L}}.
$$

The mid-year multiplier is applied once; it is not the arithmetic mean of beginning- and end-of-year CRFs.

### 8.2 OBLONG helper formulas

For milestone $M(t)$ and integer duration $D(t)$, calculations use the half-open period interval:

$$
[B(t),E(t))=[M(t)-\lfloor D(t)/2\rfloor,\;B(t)+D(t)).
$$

The equivalent inclusive calendar-year display runs from $B(t)$ through $E(t)-1$. Odd-duration periods still start on integer years.

Discounted partial pay fraction:

$$
pay\_frac = \frac{AF(g,k)}{AF(g,D)} = \frac{1-(1+g)^{-k}}{1-(1+g)^{-D}}
$$

### 8.3 Investment case classification
Cases `1a`, `1b`, `2a`, `2b` are selected using:

1. `|ILED|` vs threshold `min(D/10, TLIFE/10)` (small vs large project),
2. coverage test `TLIFE + |ILED|` vs `D(t)` (single vs repeated pattern).
Case affects timing logic and IDC handling.
### 8.4 Full-vintage annuity formula

Per vintage, app uses:

$$
annuity = NCAP\_COST \cdot VAR\_Ncap \cdot CRF(drate, ELIFE) \cdot \frac{CRF(gdrate, TLIFE)}{CRF(gdrate, ELIFE)} \cdot IDC
$$


$$
IDC = (1+gdrate)^{|ILED|/2} \quad \text{for case 2}
$$

### 8.5 Case-specific NCAP_COST sampling nuances

Implementation includes specific treatment:

1. Case 1a with `D>1`: average NCAP_COST over spread window.
2. Case 2a/2b with `D>1`: sample/interpolate NCAP_COST at `B(t)` (not milestone year).

### 8.6 Legacy process handling

When processes have `Cost_Inv` but no new-build vintages (`VAR_Ncap` absent), app can use legacy proxy logic based on existing capacity and inferred CRF.

The proxy is explicitly disclosed and is less direct than a reported-vintage reconstruction. Other remaining differences can arise from incomplete source-DD lineage, parameters or defaults not observable in supplied artifacts, and model-specific timing details beyond the implemented reconstruction. Within-period Case-1a averaging and effective TLIFE interpolation are implemented behavior, not listed as unresolved gaps.

### 8.7 Output diagnostics

`reconcile_cost_inv` provides period-level rows with:

- actual vs expected `Cost_Inv`,
- gap percent,
- active/build vintage counts,
- implied NCAP_COST,
- investment case,
- discount factors and NPVized values,
- reason classification (`MATCH`/`MISMATCH`).

`compute_reconciliation_summary` aggregates to process-level KPIs.

---

## 9. Validation Framework (`validate.py`)

Five checks are applied:

1. Cost conservation by period against `Reg_ACost`.
2. Objective reconciliation against `ObjZ` using `Time_NPV` and salvage.
3. Share completeness warnings for high commodity gaps.
- share-gap warning: 5%

---

## 10. Dashboard Behavior (`app.py`)

### 10.1 Supported run mode

The dashboard loads RunLog and selected raw VD rows only from UK-TIMES-WKG
SQL. There is no file lookup, upload path, or automatic fallback. SQL failures
are visible and block analysis for the selected run.

The selector also loads the SQL `RunGroups` table in its explicit `SortOrder`,
matching Vistool's named-group choices. Run IDs and group codes beginning with
`#` are soft-deleted and excluded. A group's `Runs` value is comma-separated
and, as in Vistool's `ParseGroupRunsSpec`, each entry is either a bare run ID
or `'Label' = run_id` / `'Label' ~= run_id`; only the run ID is used. Members
not in the active RunLog are listed in a warning rather than added. In the
"Saved run groups" tab each group row lists its runs and has a tick box:
ticking a group adds all its runs to the selection and unticking removes them
(runs shared with another ticked group stay selected). Only groups the user
ticks show as ticked — a group is not ticked just because its runs are already
selected — and a tick clears if one of the group's runs is unselected. The highest numeric `CB7_dev_xxx` run is
pinned first, followed by already loaded runs; selected and loaded run lists
are displayed above the individual-run and group tables.

App caches expensive operations:

- SQL VD retrieval and canonicalization,
- SQL RunLog and RunGroups loading,
- proc/comm lookup tables,
- DD extraction and effective-table construction,
- reconciliation result generation.
---


### 11.1 Tables (`outputs.py`)

Per scenario, generator can write:

1. sector cost by period,
2. sector cost by component,
3. supply breakdown,
4. allocation share matrix,
5. validation summary,
6. discount summary.
### 11.2 Charts

When Plotly/Kaleido available, writes interactive/static charts for sector trends, component composition, supply breakdown, and comparison views.

---

## 12. Why Full Process-Level Match Is Not Always Possible

This section answers the core limitation question directly.

### 12.1 Incomplete observability in exported artifacts

VD is a reported projection, not a full internal solver trace. Many internal transitions (especially around partial-period timing and endogenous coupling) are not explicitly recoverable from a single process-period row.

### 12.2 Temporal resolution mismatch

TIMES economics can involve:

1. vintage-based windows,
2. construction lead/lag,
3. period geometry,
4. repeated cycles.


### 12.3 DD lineage recoverability constraints

Exact effective parameter depends on run commit and include order. If commit metadata or DD extraction is incomplete/unavailable, reconstructed coefficients can be close but not exact.

### 12.4 Interpolation/extrapolation path dependence


### 12.5 Classification ambiguity

Sector attribution for multi-service/infrastructure processes may not be uniquely identifiable from naming and flow data alone.

### 12.6 Commodity residual reality

Losses, exports, balancing artifacts, and missing end-use links create genuine residual shares. Routing to `Other` preserves accounting but prevents complete deterministic process attribution.


Tax/subsidy and transfer-like attributes can be objective-consistent while not uniquely attributable to a single end-use destination without additional assumptions.

### 12.8 Legacy stock behavior

Processes with historical stock and no current build can still emit `Cost_Inv`-like behavior that needs proxy logic; this is inherently less exact than direct vintage reconstruction.
---

## 13. What “Correct” Should Mean for This App

A high-quality run should be judged in this order:

1. system-level conservation (`Reg_ACost`) by period,
3. high coverage and low unallocated share,
4. explainable residual mismatches at process level.

Forcing perfect process equality can degrade global consistency if done by overfitting assumptions.

---

## 14. Recommended Mismatch Investigation Workflow

When a mismatch appears:

1. Confirm run ID, commit, and DD include list used.
2. Export DD lineage using `tabulate_dd_inputs.py`.
3. Validate process profile availability (`DRATE`, `ELIFE`, `TLIFE`, `ILED`, `NCAP_START`).
4. Check case classification and period durations.
5. Inspect vintage table and active payment windows.
6. Compare expected vs actual at period level and NPV level.
7. Check whether mismatch is concentrated in `Other`, legacy segment, or high-gap commodities.

---

## 15. Known Practical Limits and Non-Goals

The app is designed for transparent reproducible diagnostics and defensible allocation, not as a byte-for-byte replacement of full TIMES internals.

Non-goals:

1. reconstructing every internal solver state transition,
2. guaranteeing exact equality for every process-period under incomplete artifacts,
3. replacing official TIMES execution logic.

---

## 16. Testing Coverage in Repository

Key tests in `tools/cost_allocation/tests` validate:

1. conservation and allocation pipeline behavior,
2. coefficient interpolation/forward-fill semantics,
3. reconciliation logic including multi-vintage behavior,
4. investment case classification assumptions.

This strengthens confidence in implementation consistency and regression safety.

---

## 17. Bottom Line

The app reproduces UK TIMES cost mechanics to a high degree using:

1. explicit DD precedence and I/E treatment,
2. vintage-aware annuity and OBLONG timing reconstruction,
3. iterative commodity share tracing,
4. strict system/objective validation.

It can reliably match system totals and provide deep diagnostics, while process-level perfect matching remains bounded by data observability, temporal structure, and attribution ambiguity inherent in available exports.

---

## 18. Deep Technical Annex - UK TIMES Cost Mechanics and App Replication

This annex is intentionally exhaustive and should be read as the detailed companion to Sections 1-17.

## 18.1 UK TIMES cost mechanics in practical terms

At reporting level, UK TIMES gives a period-indexed set of cost attributes, but internally the economics are vintage and timing driven.

The practical implication is:

1. `Cost_Inv` at period `t` is usually the sum of payments from several vintages.
2. Payment eligibility depends on period boundaries under OBLONG, not just milestone equality.
3. Input parameters (`NCAP_COST`, `NCAP_DRATE`, etc.) can change by year and by scenario precedence.

Therefore, exact reverse engineering is a reconstruction problem, not a direct lookup.

## 18.2 Core equations used by the app

### 18.2.1 CRF

Beginning-of-year form:

$$
CRF_{beg}(r,L) = \frac{1-(1+r)^{-1}}{1-(1+r)^{-L}}
$$

Mid-year form used by model convention:

$$
CRF_{mid}(r,L) = CRF_{beg}(r,L)\cdot(1+r)^{0.5}
$$

### 18.2.2 Re-levelization

$$
relevel = \frac{CRF(gdrate,TLIFE)}{CRF(gdrate,ELIFE)}
$$

### 18.2.3 IDC factor for case 2

$$
IDC = (1+gdrate)^{|ILED|/2}
$$

### 18.2.4 Full-vintage annuity used by reconciliation

$$
annuity_v = NCAP\_COST_v\cdot VAR\_Ncap_v\cdot CRF(drate_v,ELIFE_v)\cdot relevel_v\cdot IDC_v
$$

### 18.2.5 OBLONG boundaries and period overlap

Period start:

$$
B(t)=M(t)-\lfloor D(t)/2 \rfloor
$$

Pay fraction for overlap `k` years in period duration `D`:

$$
pay\_frac = \frac{1-(1+g)^{-k}}{1-(1+g)^{-D}}
$$

Expected period contribution from one vintage:

$$
contrib_{v,t}=annuity_v\cdot pay\_frac_{v,t}
$$

Expected `Cost_Inv` at period `t`:

$$
Expected\_Cost\_Inv_t = \sum_v contrib_{v,t}
$$

---

## 19. Worked numerical examples

## 19.1 Example 1: Case 1a process, one vintage, no IDC

Given:

- `NCAP_COST = 100`
- `VAR_Ncap = 3`
- `drate = 0.09`
- `ELIFE = 20`
- `TLIFE = 20`
- `gdrate = 0.035`

Then:

$$
annuity = 100\cdot3\cdot CRF(0.09,20)
$$

If full-period overlap gives `pay_frac=1`, expected period `Cost_Inv = annuity`.

If VD has `Cost_Inv=annuity\cdot1.03`, gap is +3%, so classification is `MATCH` (<= 5%).

## 19.2 Example 2: Case 2 with IDC

Given same numbers but `ILED=-4` and case 2 conditions, IDC applies:

$$
IDC=(1+0.035)^{2}=1.071225
$$

So annuity increases by about 7.12% versus no-IDC case.

This is a common source of apparent mismatch if IDC assumptions are omitted.

## 19.3 Example 3: Multi-vintage aggregation

Suppose for period 2035:

- vintage 2020 contribution: 40
- vintage 2025 contribution: 55
- vintage 2030 contribution: 65

Expected:

$$
40+55+65=160
$$

If VD reports 148:

$$
gap\%=\frac{148-160}{160}\cdot100=-7.5\%
$$

This is `MISMATCH` (> 5% absolute gap).

## 19.4 Example 4: Commodity allocation to sectors

Supply process cost total = 250.

Output commodity shares to sectors:

- Residential: 0.45
- Industry: 0.35
- Services: 0.10
- Gap: 0.10

Allocated amounts:

- Residential 112.5
- Industry 87.5
- Services 25.0
- Other 25.0

Total remains 250 (conserved).

---

## 20. Example chart narratives (what people should infer)

## 20.1 C1-style stacked area (sector totals over time)

If the stack rises from 2025 to 2035, then at least one of the following happened:

1. Direct End-use Technology Costs rose.
2. Upstream supply source costs rose and were attributed through commodity shares.
3. Policy transfer channels increased.

Checklist when interpreting:

1. confirm whether growth is broad-based or one-sector dominated,
2. verify if rise appears in `Cost_Inv` or operating channels,
3. compare with supply breakdown chart to identify upstream driver.

## 20.2 C2-style component bars

Use this to distinguish structural changes:

1. `Direct Resource` increase -> sector internal technology/fuel behavior.
2. `Upstream Resource` increase -> supply-chain pass-through.
3. `Policy Transfers` increase -> tax/subsidy mechanism change.

## 20.3 C3 supply-side stack

Useful for policy discussion:

1. rising `Fuel imports` can explain cross-sector pressure,
2. rising `Grid & networks` often transmits broadly,
3. shifts between `Power generation` and `Hydrogen production` indicate system redesign.

## 20.4 Sankey view caveat

The Resource & Fuel Costs page isolates Sankeys in a dedicated **Pathways** subtab. The monetary Sankey is a proportional incidence explanation built directly from the final allocated fuel-type and sector values; it is not a literal solver path tracer and the VDT does not contain cost values. The activity Sankey uses VDT-matched annual VD quantities and is physical rather than monetary. Their £M/year and TWh/year widths must never be compared or added. Producer/consumer TWh gaps remain explicit rather than being force-balanced. Raw lineage and reconciliation evidence is in the full run workbook.

---

## 21. Detailed file-level internals (how each file works)

This section expands Section 10 with more implementation detail.

## 21.1 `tools/cost_allocation/app.py`

Responsibilities:

1. Orchestration of run loading (`RunBundle`).
2. Streamlit UI and tab rendering.
3. Comparison dashboards and diagnostic controls.
4. Caching wrappers around expensive steps.

Important implementation patterns:

1. `@st.cache_data` is used for parsing VD, loading DD, and reconciliation tables.
2. Run selector state is persisted in `st.session_state` (`loaded_run_ids`, run bundle cache).
3. Reconciliation view is behind explicit toggles to avoid heavy default reruns.

## 21.2 `tools/cost_allocation/run_selector.py`

Responsibilities:

1. Parse RunLog CSV into tidy rows.
2. Find git commit for run ID using message search.
3. Resolve `.RUN` file name case-safely at that commit.
4. Parse `$BATINCLUDE` DD order.
5. Extract only required DD files via `git archive` with cache fallback.

Why it matters:

Run-specific DD lineage is essential for faithful reconciliation.

## 21.3 `tools/cost_allocation/dd_inputs.py`

Responsibilities:

1. Parse DD parameter blocks and normalize token structure.
2. Keep only target attributes.
3. Apply include-order precedence.
4. Split and process IE option rows (year=0).
5. Apply TIMES interpolation/extrapolation behavior.

Important detail:

Interpolated rows are appended without overriding explicit winner rows.

## 21.4 `tools/cost_allocation/cost_extraction.py`

Responsibilities:

1. Convert long-form DD/base data to wide process-year tables.
2. Apply scenario precedence.
3. Perform interpolation and forward persistence behavior consistent with model data interpretation.

Notable behavior:

No backward fill before first explicit year, preserving late-start technology behavior.

## 21.5 `tools/cost_allocation/classify.py`

Responsibilities:

1. Build process metadata cache.
2. Merge from user and lookup sources.
3. Apply regex-based fallback rules.

Potential pitfall:

Classification uncertainty can dominate sector-level interpretation for ambiguous process names.

## 21.6 `tools/cost_allocation/flow_matrix.py`

Responsibilities:

1. Build converged commodity-to-sector shares by period.
2. Keep residual gap diagnostics.

Convergence design:

Stops at tolerance (`1e-4`) or max 30 iterations with warning if non-converged.

## 21.7 `tools/cost_allocation/allocate.py`

Responsibilities:

1. Build process-period attribute cost pivots.
2. Allocate by category and commodity shares.
3. Track component groups and per-process detail.

Conservation design:

Residual and missing-share values are routed to `Other` to avoid silent loss.

## 21.8 `tools/cost_allocation/cost_backcalc.py`

Responsibilities:

1. CRF utilities.
2. OBLONG boundary and pay fraction math.
3. Investment case classification.
4. Vintage annuity computation with re-levelization and IDC.
5. Period-by-period expected vs actual reconciliation.
6. Process-level summary generation.
7. Vintage detail table for drill-down.

Why it is large:

Most model-specific economic timing complexity lives here.

## 21.9 `tools/cost_allocation/validate.py`

Responsibilities:

1. Run five consistency checks.
2. Return machine-readable report rows plus status.
3. Provide textual summary for CLI/dashboard.

## 21.10 `tools/cost_allocation/outputs.py`

Responsibilities:

1. Build T1-T6 style tables.
2. Build chart set including sector trend, component mix, supply breakdown, Sankey, scenario delta.
3. Export HTML and optional PNG.

## 21.11 `tools/cost_allocation/discount.py`

Responsibilities:

1. Load optional hurdle-rate map.
2. Build sector discount summary and implied premium diagnostics.

## 21.12 `tools/cost_allocation/ingest.py`

Responsibilities:

1. Robustly parse VD structure.
2. Provide attribute getters and summary helpers used across modules.

## 21.13 `tools/cost_allocation/colours.py`

Responsibilities:

1. Shared palettes for consistent semantics in UI and export charts.

## 21.14 `tools/cost_allocation/run_config.py`

Current status:

1. Empty placeholder in this repository snapshot.

---

## 22. Extended test coverage map

## 22.1 `test_conservation.py`

Covers:

1. minimal synthetic pipeline behavior,
2. share sums,
3. sector allocation sanity,
4. conservation and no-negative checks.

## 22.2 `test_cost_extraction.py`

Covers:

1. single-point forward persistence,
2. late-start no-backfill behavior,
3. interior interpolation + tail flattening.

## 22.3 `test_cost_reconciliation.py`

Covers:

1. multi-vintage reconciliation consistency,
2. summary weighted/implied metrics,
3. investment case classification edge conditions,
4. OBLONG-related spread assumptions.

---

## 23. Common questions answered directly

## 23.1 Why can total checks pass while some processes mismatch?

Because system totals are aggregate identities. Process-level rows depend on additional assumptions (lineage, interpolation, timing windows, classification) that can have local uncertainty while global sums still hold.

## 23.2 Why is `Other` sometimes large?

Usually one or more of:

1. high commodity share gaps,
2. missing/ambiguous process classification,
3. missing output commodity mapping,
4. true non-end-use or export/loss behavior.

## 23.3 Should we force all process gaps to zero?

No. Doing so with ad-hoc adjustments can violate conservation and objective consistency, reducing model fidelity.

---

## 24. Documentation use recommendations

For stakeholders, read Sections 3, 5, 6, 7, 8.

For analysts, add Sections 9, 19, 20, 23.

For developers, add Sections 21 and 22.

For QA/release review, focus on Sections 8, 9, 22.

---

## 25. Fuel Cost Attribution and Resource & Fuel Pathways

### Colour policy

The dashboard and generated Plotly HTML/PNG charts use the reviewed 18-colour
DESNZ categorical palette: nine primary hues followed by nine lighter
overflow hues. Colour assignment is deterministic for a displayed category
set. Additive charts retain their 17 largest categories by absolute
contribution and combine any further categories into **Other**, without
changing signed totals. Scenario runs, build vintages and Sankey topology are
not combined; after 18 identities they use deterministic line or marker styles
where the trace type supports them, while Sankey topology remains intact.
Named semantic colours preserve the meaning
of matched, warning, error, import, export, storage and transformation states.

**Fuel Cost Attribution** implements the proportional method used by the exported resource/fuel workbooks with live selected-run evidence. It attributes upstream source-process costs to the sectors that use each fuel; it does not create direct costs on those receiving sectors. Historical scenario CSVs are reference/test material only and are never read by the production tab.

### 25.1 Direct costs versus attribution

| Dashboard location | Accounting boundary | Cost attributes | Interpretation |
|---|---|---|---|
| Overview — **Direct Energy Supply Costs** | Graph-derived supply processes | Core process costs | Costs remain on their reporting supply processes. |
| Overview — **Direct End-use Technology Costs** | Graph-derived final-use technologies | Core process costs, with optional investment exclusion | Costs remain on the technologies delivering final energy services. |
| Overview — **Direct Power-system Costs** | Validated Power-role processes | Core process costs | Whole-system Power-role costs, not consumer allocations. |
| Fuel Cost Attribution — **Flow-cost Attribution** | Graph-derived supply source rows | `Cost_Flo` | Source costs redistributed by observed fuel-use shares. |
| Fuel Cost Attribution — **Full Supply-chain Attribution** | Connected graph-derived supply source rows | `Cost_Inv`, `Cost_Fom`, `Cost_Act`, `Cost_Flo` | Source costs redistributed by observed fuel-use shares. |

The two attribution views overlap because Full Supply-chain Attribution includes `Cost_Flo`; neither can be added to the other or to the direct-cost Overview totals. **Resource & Fuel Pathways** explains the attributed monetary incidence and physical energy evidence. It is not an additional cost total or a literal solver cost trace.

### 25.2 Inputs

1. Current authoritative process and commodity Relabelling metadata.
2. The VDT committed with the selected run.
3. Normalized annual, VDT-matched `VAR_FIn` and `VAR_FOut` rows.
4. The per-row VD cost table, preserving `Process`, `Commodity`, `Attribute`, `Period` and `Value`.
5. Reviewed exact-match resource rules in `tools/lookup_tables/resource_fuel_allocation_rules.csv`.

### 25.3 Sector quantities

Compatible `IN` flows are converted to TWh and grouped by model period, resource pool and current `process_sector`. A process input is suppressed when that process also outputs the same pool in the same period, preventing a distribution chain from repeatedly appearing as demand. Inputs to genuine transformations remain visible under the transforming sector. Signed quantities are retained and a non-positive aggregate denominator is not used for allocation.

The reviewed taxonomy distinguishes **canonical primary-carrier aliases** from **secondary/useful-energy carriers**. Electricity, gas, heavy-fuel-oil, coke, coke-oven-gas and blast-furnace-gas allocation aliases retain their underlying primary pool; low-temperature-heat allocation aliases retain the `Low temperature heat` secondary pool. This preserves same-pool suppression across routing steps. Distinct secondary pools cover CHP, district heating, ventilation, drying, high- and low-temperature heat, motor drive, industry other, piped or standalone heat and hot water, refrigeration, steam, wet piped heating, industry byproducts, space heating, and conservation services. They represent different conversion stages and are not additive with their primary inputs as a whole-system energy total. Reviewed CCS-retrofit proxy commodities are explicitly excluded as internal accounting links. `ELCWSTHEAT` and `PRCWSTHEAT` are mapped to a reviewed `Waste heat` pool, so a plant that co-produces waste heat alongside its primary output splits its source costs by observed output share instead of being dropped as ambiguous output.

### 25.3.1 Label taxonomy and multi-role commodities

The live Relabelling fields are reporting dimensions, not a replacement for the TIMES topology. `process_sector` is the NZS / Costs Sector receiving-cost axis; `process_group` is the normal technology reporting level; `process_category` is an optional archetype/geography dimension; and `process_subsector` identifies energy-system areas such as Hydrogen Generation and Hydrogen Network. GHGI, DUKES and Useful Output are separate reporting dimensions.

A commodity code is never split merely because it plays several roles. The same carrier can be an input to a final-use technology, a transformation input, a network throughput flow, and an output from a producer. The app records a **connection role** for each VDT process–commodity link. Final-use membership is an energy `IN` link to a process with authoritative non-blank `process_useful_output`; `process_set = DMD` and a `DEM` output are retained as validation evidence. Commodity function remains descriptive metadata and is not used as a universal end-use rule.

### 25.3.2 Commodity coverage

Resource pooling must not be opaque: no energy commodity that carries energy in the selected run may be left unexplained. `summarise_commodity_coverage` restates the calculation status already recorded for every observed flow as one row per exact commodity code, because `commodity_name` is a non-unique roll-up. The relevant universe is deliberately narrow — commodities that are energy by `commodity_set = NRG`, by `commodity_type`, or by having a mapped pool, and that carry a material quantity in the period. Emissions and demand services are outside a fuel-cost split by construction.

Each code receives exactly one of three answers. **Counted** means its sector use enters the allocation denominator. **Not counted** covers a same-pool pass-through, which would otherwise double-count, and a commodity that is only produced in the period and never consumed. **Needs attention** covers an unmapped energy commodity and an energy unit that cannot be converted to TWh; where a code has rows in more than one state, the most actionable answer wins. The **Which fuels are counted** section on Fuel Cost Attribution states the count of unexplained fuels and lists them, per model year, with their reason. This is presentation evidence derived from existing statuses; it changes no denominator, share, pool cost or allocated value. Observed flows that do not match the selected run's VDT are removed before allocation and remain disclosed as unmatched edges.

### 25.4 Source-cost ownership

Each eligible source row has one auditable outcome:

1. assigned to one mapped observed output pool;
2. safely split between mapped outputs in proportion to absolute output TWh, with shares summing to one; or
3. left unallocated because output, mapping or unit evidence is incomplete.

**Flow-cost Attribution** selects `Cost_Flo`. **Full Supply-chain Attribution** selects `Cost_Inv`, `Cost_Fom`, `Cost_Act` and `Cost_Flo` on graph-derived supply processes. These are overlapping attribution views and must not be summed. `Cost_Com` is disclosed separately.

### 25.5 Proportional allocation

For sector $s$, resource pool $r$ and model period $t$:

$$
AllocatedCost_{s,r,t}=PoolCost_{r,t}\times
\frac{SectorTWh_{s,r,t}}{\sum_j SectorTWh_{j,r,t}}.
$$

Every mapped pool reports source cost, denominator, allocated cost, denominator-unallocated cost and status. A separate source-row reconciliation proves that assigned plus unassigned source cost equals the selected source rows without duplication. Reconciliation is intentionally to these selected rows rather than `Reg_ACost`.

**Cascade through converters** (`resource_cascade.py`, pure). A converter's share of its input pool is passed on to the pools it produces, split by output TWh; only final use keeps cost. `apply_cascade` solves $C=(I-T^{\top})^{-1}D$ per (period, view, sign, share-basis year) and writes the `process_allocations` ledger (`sign`, `role`, `passed_to_pool`). Every pool balances own + passed in = final use + passed on + unallocated. These are internal names: displayed and exported tables use `with_conversion_column_names` (`conversion_supply_cost_m`, `conversion_demand_cost_m`, `output_pool`), the ledger role is `Conversion`, and the UI says Supply / Demand. See APP_DOCUMENTATION §10.5.1.

### 25.6 Time resolution and limitations

Milestone calculations are authoritative. Optional annual output linearly interpolates displayed allocations between milestones and labels the inserted years; it does not alter milestone values or create model observations. Negative source costs are retained. The tab does not use the legacy recursive `AllocationMatrix`, does not classify by process-name prefixes, and does not invent separate `FLO_DELIV` and `IRE_PRICE` values from combined `Cost_Flo`.

### 25.7 Cost and physical pathway Sankeys

The **Resource & Fuel Pathways** view places **Pool-level allocated costs** first and **Individual fuel pathway** second. Primary model-year, resource-pool and individual-fuel selectors sit above these subtabs; each subtab shows its Sankey and a balance bar (`_cost_balance_rows` / `_physical_balance_rows` → `_balance_figure` in `tabs/resource_allocation.py`), with display controls and evidence tables in one **Details** expander.

The monetary Sankey is **Supply → resource pool → Demand**, built from the cascade ledger for the selected **Cost attributes**. Cost converted between two selected pools flows pool → pool; otherwise it enters from, or leaves to, converter-process nodes labelled with the supply-side or demand-side taxonomy selection (kinds `Supply` / `Demand`, one node per code), plus an **Unallocated** node. Supply links are built from uniquely owned source-cost rows, and demand links are final-use ledger rows. Independent supply-side and demand-side selectors control grouping and colour without changing source ownership, allocation shares or balances. Positive **Costs** and negative **Credits & revenues** are summed per displayed link into one net Sankey (`SIGN_NET`). A net-credit link keeps its negative signed value and is drawn reversed by `_orient_net_links`, keeping both node identities (one node per code, `_cost_endpoint_node_id`, whatever the role); `gross_cost_m`, `gross_credit_m` and `net_direction` record the split and orientation. The per-sign modes remain for evidence and tests.

The physical Sankey is selected hierarchically by model year, reviewed resource pool, and authoritative `commodity_name` (for example, **Petroleum products → Petrol**). It is displayed as **Supply → Individual fuel → Demand**. All positive compatible matched exact commodity codes under the selected fuel are pooled at each process. Compatible PJ is converted using $1\,TWh=3.6\,PJ$. A process contributes its selected-fuel output less its selected-fuel input: a positive net is Supply, a negative net is Demand, and a balanced same-fuel pass-through is suppressed. This means balanced terminals, networks and storage cycles do not appear as new production, while net storage discharge is Supply and net charging is Demand. Exact commodity codes remain in audit tables and downloads; no other fuel is recursively added.

Secondary carriers use the same pathway builders and therefore appear automatically in both Sankey selectors when the selected run contains evidence. The physical graph still stays within one authoritative `commodity_name`; it does not join a primary input to a converted secondary output into a recursive transformation graph.

In stakeholder mode, authoritative Coal/Gas/Oil Extraction processes and export-side process codes ending `-E` are hidden to remove domestic extraction and UK TIMES dummy-export legs. Genuine imports, exports and import-side re-import processes remain visible. Technical mode restores hidden valid edges. Independent physical supply and demand selectors control grouping and colour, defaulting to Subsector grouping; presentation grouping occurs after process-level netting and cannot change balances. Both Sankeys use model milestones only. Hover text is intentionally concise, while process codes, contributors, signed values, quantities and visibility reasons remain in the one-table-at-a-time evidence views and selected-run XLSX workbooks. Supply-demand gaps, exact-code residuals and VD rows unmatched to VDT are disclosed separately; no balancing link is fabricated. The monetary Sankey remains resource-pool based and is unaffected by individual-fuel visibility or grouping.

### 25.8 Reference-format workbook export

The primary download is a formatted, selected-run XLSX workbook with a fixed 2025–2050 layout: TWh, source **Resources Costs by Fuel**, Electricity Costs, Resource Costs, and Fuelex by DESNZ Sector. It is generated in memory from the selected run and never reads the scenario CSVs or reference workbook at runtime.

For each annual year, the workbook first linearly interpolates eligible primitive fuel-use and source-cost series between model milestones, without extrapolation. It then recalculates shares and allocations; it never interpolates already allocated endpoint costs. Biomass and biomethane remain separate TWh rows, but their source cost uses their combined consumption share. The workbook distributes electricity only across non-Power electricity users. Its Electricity Costs row two equals row one plus the Power sector's direct net fuel cost, while Power receives no distributed-electricity row. `Cost_Com` is not added to these calculations. The workbook does not yet follow cost through conversion; it shows the one-step split.

The fixed tables remain a primary-fuel reporting contract. Reviewed secondary pools are retained in raw mapping, commodity coverage, pool summary and pathway evidence, but are intentionally excluded from the fixed TWh, source-cost, Resource Cost and Fuelex layouts. The Checks sheet distinguishes these reviewed exclusions from genuinely unknown mappings.

The Checks sheet also states explicitly that source-fuel rows and Electricity (distributed) rows are **not additive** across sectors: distributed electricity already contains the Power sector's own fuel cost, which is also reported on the Power sector's source-fuel rows, so summing the whole Resource Cost block double counts it. A model year that has Power fuel cost but no core Power cost is named rather than left as a blank electricity row, and a fuel whose allocation denominator is not positive is reported once for that year rather than once per sector.

#### 25.8.1 Provenance sheets

Sheets are numbered in calculation order and every derived cell is a live formula pointing at the sheet before it, so the whole chain can be walked in Excel. Six sheets carry the evidence that precedes the fuel-use table, so the chain begins at the run's own reported rows rather than at an already aggregated total.

**02 How pooling works** states the reviewed pooling rules in the order they are tried, together with the four properties that make them defensible: first match wins, matching looks only at the commodity label, a commodity belongs to exactly one pool, and anything unmatched is named rather than dropped. **03 Worked example** follows the largest observed fuel, sector and model year from reported rows through to the allocated cost, entirely in live formulas, and re-multiplies share by source cost to prove the final figure. **04 VD to annual flows** holds one row per connection per model year with the value as reported, the number of result rows combined, whether an explicit annual figure or summed time slices were used, and whether the connection exists in the run's model connections; unmatched flows are retained and marked. **05 Unit conversion** performs the conversion to TWh separately for each model year, commodity and reported unit rather than combining annual rates from different years. **09 Source cost assignment** retains every reported cost row, its assignment share, assigned resource pool and reason. **11 Raw source costs** then totals those process rows with live `SUMIFS` formulae, while **12 Raw pool summary** remains the application's independently prepared cross-check. **19 Which fuels are counted** carries the commodity coverage ledger of §25.3.2.

A full dump of result rows is deliberately not attempted, because a single Excel sheet cannot hold one. Sheet 04 records the row count and aggregation method behind every connection instead, which proves the same aggregation at a size the format supports. Oversized tables are truncated only with a recorded warning that reaches the Checks sheet.

**Fuel Cost Attribution** is one lazy page with a **Cost attributes** multiselect shared with **Resource & Fuel Pathways**. `attribute_view(attributes)` maps the choice to a cost view: `{Cost_Flo}` is `RESOURCE_FLOW_VIEW`, all four is `UPSTREAM_SUPPLY_VIEW`, any other subset is `"Supply-chain cost: " + " + ".join(attributes)`; `view_cost_attributes` reverses it and `normalise_resource_views` accepts it. Each view runs the unchanged source gate, share basis and cascade, so attribute subsets are additive. Fuel and service pools are listed together (no pool-kind switch). **Resource & Fuel Pathways** is a separate lazy top-level view. Their milestone years use categorical chart spacing so stacked columns have the same readable width as other dashboard charts. Fuel Cost Attribution shows only the pool multiselect, sector chart, the **Unit cost by final user** section (`final_user_unit_costs`: net final-use ledger cost of one pool ÷ the same consumers' own-period final-use TWh from `ResourceAllocationResult.consumer_use`, grouped by any `FINAL_USER_UNIT_COST_BREAKDOWNS` label as a ratio of sums), fuel coverage, a collapsed balance check and the workbook; raw evidence tables are not rendered on screen. The six-step walkthrough of one pool and one model year (pools used → supply into the pool → demand from the pool → split into final use, passed on and unallocated → conversion destinations and final users by origin pool → sector chart) is on **Methodology → Step-by-step walkthrough**, implemented in `tabs/attribution_walkthrough.py`. Its tables (`_pools_used_table`, `_pool_supply_table`, `_pool_use_table`, `_pool_conversion_table`, `_pool_final_destination_table`) are pure restatements of `pool_summary`, `cost_assignments`, `allocations`, `process_allocations` and `cascade_allocations`; step 2 totals and step 4 totals plus unallocated equal `pool_cost_m`, pinned by `tests/test_attribution_walkthrough.py`. **Methodology → Unit cost walkthrough** (`tabs/unit_cost_walkthrough.py`) derives the final-user £/MWh in six steps (pool cost, where it goes, final users versus converters, per-process cost and use, grouped result against the average, the division) from `final_user_unit_costs`; its balances are pinned by `tests/test_unit_cost_walkthrough.py`.

### 25.9 Methodology — Hydrogen attribution proof

The **Methodology** screen has three lazy tabs: **Reconciliation methodology**, **Attribution methodology** (how Fuel Cost Attribution is calculated, with a live OILPET example, notes on reading the page, and the selected-period **Hydrogen proof** worked example) and **Step-by-step walkthrough** (any pool, using the shared cost-attribute selection). Each attribution tab has its own run picker. Both reuse exactly the same cached allocation result as the Fuel Cost Attribution screen. They therefore do not recreate or vary the allocation calculation.

The proof starts with the whole selected-period Hydrogen system: net-positive observed Hydrogen sources, all included material Hydrogen-input users, the DUKES-use denominator, reported Cost_Inv and Cost_Fom vintages, and explicit excluded input rows. Principal exclusion statuses include zero or non-material input, missing mapping, incompatible energy units and same-pool pass-through; every excluded row retains its exact reason. A `structural_end_use` flag distinguishes the narrower VDT-derived end-use boundary from the broader set of processes that can contribute to the use denominator.

**Source-cost boundary** is not a native TIMES set. It is the selected run's VDT-derived upstream process set used to determine which source-cost rows can enter the Full Supply-chain pool. It starts from evidenced energy delivery and follows eligible energy inputs to same-region producers while excluding final-demand/end-use, allocation, dummy/accounting and non-energy routes. Physical source evidence and financial source rows are both restricted to this active supply boundary. Separately, an input enters the use denominator only when it is material, mapped to a compatible energy unit and not a same-process/same-pool pass-through; excluded inputs retain their exact reason.

It presents deliberately separate flow evidence. The **physical activity** Sankey uses observed annual TWh and shows grouped net sources → Hydrogen pool → grouped included uses. Source output and user input are independent observations; any gap is displayed rather than converted into a fabricated direct route or loss.

The **financial attribution** section uses £M/year and renders one **net** Sankey from the cascade ledger: `net_financial_links` sums the signed **Costs** and **Credits & revenues** links per displayed link and draws net credits reversed (non-Hydrogen-pool end suffixed `|net_credit`). It follows grouped eligible supply-chain rows and converter supply (*Conversion supply*, sharing a node per label) → Hydrogen cost pool → grouped final uses, converter demand (*Conversion demand*) and **Unallocated**. `financial_links` and the per-sign balance checks stay sign-specific. Hover shows the net amount and gross cost and credit. The physical and financial widths must never be compared because one is observed energy activity and the other is proportional cost incidence.

The optional source-to-sector zoom-in replays the first step only (one source's own cost split by Hydrogen use), so it excludes converter supply and can differ from the DUKES table. The proof does not book a direct cost on recipient technologies, claim physical ownership of costs, add `Cost_Com`, synthesize vintages, or make an additional total that can be added to direct-cost or Flow-cost views. Its on-demand evidence workbook includes all Hydrogen sources, all users, excluded flows, physical and financial graph links, signed financial categories and values, source rows, sector shares, reported vintages, diagnostics, and net plus sign-specific balance checks.

---

## 26. Fuels vs Capital (Overview — System)

The **System & Sector Costs** tab ends with a **Fuels vs Capital** section. It relabels the same rows the System view already sums (`Cost_Inv`, `Cost_Fom`, `Cost_Act`, `Cost_Flo`, excluding special sectors), plus any dummy import rows (`_DUMMY IMPORTS`), which the System view leaves out. It does not allocate, share, re-price or re-discount anything. Scenarios such as fossil price shocks are represented by the model runs themselves and are compared by loading those runs side by side. The chart draws negative values (exports) below zero, and Carbon credits and Dummy imports sit on top of each column (see the colour policy in VISUALIZATION_REFERENCE.md).

### 26.1 Classification

`cost_nature.py` assigns each row exactly one bucket using the reviewed rules in `tools/lookup_tables/cost_nature_rules.csv`. Rules are tried in ascending `priority` and the first match wins. A rule matches on `Attribute` plus optional `process` (glob patterns such as `IMP*-E`), `process_subsector`, `process_group` and `energy_role` (`*` = any, `|` separates alternatives, comparison is case-insensitive). A rule's `action` is `include` or `exclude`; excluded rows leave every chart and total but stay in the evidence and reconciliation. `energy_role` is `end_use` or `supply` from the selected run's VDT-derived energy scope (end-use wins), otherwise `other`. A row matched by no rule is kept as `Unclassified`, never dropped.

Headlines: **Fuel imports** (flow and activity costs on import processes and electricity interconnector imports), **Domestic fossil production** (the `MIN*` processes in Gas, Oil and Coal Extraction, the only place domestic production is costed), **Exports** (export revenues), **Carbon credits** (flow and activity costs on the Emission Credits processes `IMPCO2CRD`, `EXPCO2CRD` and `CO2BKSTP`, netted together), **Capital & infrastructure** (`Cost_Inv`, split into supply and networks, end-use equipment and other), **Dummy imports** (flow and activity costs on the Dummy Imports processes, which normally appear only when a run does not solve cleanly), and **Operating & other** (fixed O&M and remaining activity and flow costs). The re-import loop is excluded for every attribute: the model exports extracted fuel through dummy `EXP*-E` processes and brings it back through dummy `IMP*-E` processes, so both legs only route domestic production. Any other Fuel Re-imports process falls to a safety-net bucket that the app flags for review. Both `-E` rules are restricted to the Fuel Re-imports subsector, so a genuine import whose code ends in `-E` (for example `IMPBIOOILCRP-E`) is still counted as a fuel import.

### 26.2 Checks

For every run and period, `reconcile_cost_nature` proves the shown and excluded buckets partition the source rows: equal row counts, no unclassified rows, and shown plus excluded totals equal to the source total within 1e-6 £M. The undiscounted-spend table uses the same annual-cost-times-period-duration convention as the System summary. Its model total plus the re-import loop memo line equals that table's **Undiscounted spend**. Dummy imports are listed after the totals as **Dummy imports (outside System total)**, so the totals still match the System view. `excluded_cost_check` lists any excluded row with a non-zero cost; the loop is expected to carry none, so a non-empty result is reported, with per-run amounts, inside the *How fuel and capital are separated* dropdown and under *Reconciliation and evidence*, and is a question for the model team.

### 26.3 Limitations

`Cost_Inv` is annualised investment including hurdle rates, not overnight capex. `Cost_Com` and `Cost_Salv` are outside the System view and are excluded. `Cost_Flo` on an import process is treated as the import price; combined `Cost_Flo` values are not split further.
