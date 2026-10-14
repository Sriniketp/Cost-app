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

The repository provides one user-facing workflow:

1. Streamlit dashboard (`tools/cost_allocation/app.py`)

The legacy CLI allocation engine (`__main__.py`, `classify.py`, `flow_matrix.py`, `allocate.py`, `validate.py`, `outputs.py`, `discount.py`) was removed in Phase 5. Classification comes only from the authoritative SQL process/commodity labels.

### 2.1 Core package structure

- `ingest.py`: Parses VD files and exposes common filters/accessors.
- `dd_inputs.py`: DD parsing, precedence, and TIMES I/E application.
- `cost_extraction.py`: Coefficient table extraction and interpolation behavior.
- `cost_backcalc.py`: `Cost_Inv` reconstruction and reconciliation diagnostics.
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

The removed legacy validator (`validate.py`, Phase 5) checked objective consistency in the form:

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

### 4.1 VD files (results)

Parsed from one or more `.vd` files. Required fields are fixed 9-column GDX2VEDA style rows.

Key ingest behavior:

1. Skip comment/header lines starting with `*`.
2. Parse numeric `Value`.
3. Normalize `Period` to integer nullable type.
4. Attach scenario name from header (`*Scenario`) or filename stem.

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

The legacy CLI allocation engine (including `classify.py` and `flow_matrix.py`) was removed in Phase 5. Classification comes only from the authoritative SQL process/commodity labels (`labels.py`).

---

## 7. Cost Allocation Engine (`allocate.py`)

The legacy CLI allocation engine (`allocate.py`) was removed in Phase 5. Classification comes only from the authoritative SQL process/commodity labels; dashboard fuel-cost allocation is in `energy_scope.py` and `resource_allocation.py`.

---
## 8. Investment Reconciliation Engine (`cost_backcalc.py`)

Supports beginning-of-year, end-of-year, and mid-year formulas.

Mid-year convention used by this model:

$$
CRF_{mid} = CRF_{beg} \cdot (1+r)^{0.5}

### 8.2 OBLONG helper formulas

Period start:

$$
B(t) = M(t) - \lfloor D(t)/2 \rfloor
$$

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

The legacy CLI validation gate (`validate.py`) was removed in Phase 5. Classification comes only from the authoritative SQL process/commodity labels.

---

## 10. Dashboard Behavior (`app.py`)

### 10.1 Supported run modes

1. Uploaded VD files.
App caches expensive operations:

- VD parsing,
- RunLog loading,
- proc/comm lookup tables,
- DD extraction and effective-table construction,
- reconciliation result generation.
---


### 11.1 Tables (`outputs.py`)

The legacy CLI output generator (`outputs.py`, CSV tables T1–T6) was removed in Phase 5. Classification comes only from the authoritative SQL process/commodity labels.

### 11.2 Charts

The legacy CLI chart writer (Plotly/Kaleido charts C1–C5) was removed with `outputs.py` in Phase 5.

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

The extractor itself does not back-fill before the first explicit year, but in the live DD pipeline `dd_inputs._apply_times_interpolation` runs first and its default TIMES IE option 0 extrapolates backward. A late-start technology's `NCAP_COST` therefore appears flat back to the first model year. This is intended: it is a model input, and investment cost is only incurred from `NCAP_START` (enforced in `reconcile_cost_inv`).

## 21.5 `tools/cost_allocation/classify.py`

Removed in Phase 5 with the legacy CLI allocation engine. Classification comes only from the authoritative SQL process/commodity labels (`labels.py`).

## 21.6 `tools/cost_allocation/flow_matrix.py`

Removed in Phase 5 with the legacy CLI allocation engine. Classification comes only from the authoritative SQL process/commodity labels.

## 21.7 `tools/cost_allocation/allocate.py`

Removed in Phase 5 with the legacy CLI allocation engine. Classification comes only from the authoritative SQL process/commodity labels.

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

Removed in Phase 5 with the legacy CLI allocation engine. Classification comes only from the authoritative SQL process/commodity labels.

## 21.10 `tools/cost_allocation/outputs.py`

Removed in Phase 5 with the legacy CLI allocation engine. Classification comes only from the authoritative SQL process/commodity labels.

## 21.11 `tools/cost_allocation/discount.py`

Removed in Phase 5 with the legacy CLI allocation engine. Classification comes only from the authoritative SQL process/commodity labels.

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

Removed in Phase 5 with the legacy CLI allocation engine it tested.

## 22.2 `test_cost_extraction.py`

Covers:

1. single-point forward persistence,
2. extractor in isolation does not back-fill a late-start series (end-to-end back-extrapolation is pinned in `test_dd_inputs_late_start.py`),
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
# UK TIMES Cost Documentation and App Replication Notes

## 1. Purpose

This document explains three things in one place:

1. How cost is represented in UK TIMES outputs.
2. How this Cost Allocation App reconstructs and allocates those costs.
3. Why exact process-by-process parity with UK TIMES is not always possible, even when total-system checks pass.

The aim is practical traceability for analysts using the app for diagnostics, reporting, and reconciliation.

---

## 2. Cost in UK TIMES: What the Model Reports

UK TIMES writes cost outputs in the `.vd` result file at different granularities. The key attributes used by this app are:

- `Cost_Inv`: Annualized investment cost.
- `Cost_Fom`: Fixed O&M.
- `Cost_Act`: Activity-dependent O&M.
- `Cost_Flo`: Flow/fuel-linked process cost.
- `Cost_Com`: Commodity-linked costs.
- `Cost_Salv`: End-of-horizon salvage credit (not a regular annual flow).
- `Cost_ire`: Implied endogenous trade costs (sums to zero across regions).
- `Reg_ACost`: Regional annual total cost.
- `ObjZ`: Objective value (NPV of costs under model settings).
- `Time_NPV`: Time discount factor used in objective aggregation.

At a high level, UK TIMES objective accounting is:

$$
ObjZ \approx \sum_t \left(Time\_NPV_t \cdot Reg\_ACost_t\right) + Cost\_Salv
$$

The app mirrors this identity in validation.

---

## 3. Investment Cost Logic in UK TIMES (Why `Cost_Inv` Is Subtle)

`Cost_Inv` is not simply `NCAP_COST * VAR_Ncap` in reporting years. It is annualized and can include timing effects from lead times and period geometry.

### 3.1 CRF and annuity structure

For one vintage, the app uses the TIMES-consistent form:

$$
annuity = NCAP\_COST \cdot VAR\_Ncap \cdot CRF(drate, ELIFE) \cdot relevel \cdot idc
$$

where:

- $CRF(drate, ELIFE)$ is the hurdle-rate capital recovery factor.
- $relevel = \frac{CRF(gdrate, TLIFE)}{CRF(gdrate, ELIFE)}$ when $ELIFE \neq TLIFE$.
- $idc = (1+gdrate)^{|ILED|/2}$ for case-2 style construction timing.

Under mid-year convention:

$$
CRF_{mid} = CRF_{beg} \cdot (1+r)^{0.5}
$$

### 3.2 OBLONG and payment fractions

With OBLONG period handling, process payments can partially overlap a period. The app applies discounted pay fractions:

$$
pay\_frac = \frac{AF(g,k)}{AF(g,D)} = \frac{1-(1+g)^{-k}}{1-(1+g)^{-D}}
$$

This is one of the major reasons process-level annual values can be unintuitive without vintage decomposition.

### 3.3 Investment case classification

The app classifies each process/period into case 1a/1b/2a/2b using ILED magnitude, TLIFE, and period duration. Case affects payment timing and sometimes IDC treatment, so two processes with similar capex can report different `Cost_Inv` profiles.

---

## 4. How the App Replicates UK TIMES Cost Handling

The app follows a deterministic staged pipeline.

## 4.1 Stage A: Ingest model outputs

- Reads one or more `.vd` files.
- Parses canonical columns: attribute, commodity, process, period, value, etc.
- Normalizes `Period` as integer.

## 4.2 Stage B: Build process classification

The legacy CLI allocation engine (including `classify.py` and its `Proc_merged` seed table and prefix heuristics) was removed in Phase 5. Classification comes only from the authoritative SQL process/commodity labels.

## 4.3 Stage C: Rebuild effective DD inputs

For reconciliation and profiles, the app:

- Resolves DD include order from RUN file commit lineage.
- Applies precedence (later include wins on same key).
- Applies TIMES interpolation/extrapolation behavior.
- Builds `capex`, `fom`, `act_cost`, and derived `totcapex` tables.

This step is critical: many reconciliation mismatches are data-lineage issues, not arithmetic issues.

## 4.4 Stage D: Build commodity allocation matrix

The legacy CLI allocation matrix (`flow_matrix.py`) was removed in Phase 5. Dashboard fuel-cost allocation uses `energy_scope.py` and `resource_allocation.py` (proportional allocation by observed carrier share).

## 4.5 Stage E: Allocate costs to sectors

The legacy CLI allocation engine (`allocate.py`) was removed in Phase 5. Classification comes only from the authoritative SQL process/commodity labels.

## 4.6 Stage F: Validate conservation and objective consistency

The legacy CLI validation gate (`validate.py`) was removed in Phase 5.

---

## 5. Why the App Cannot Match All Processes Fully

Exact process-level parity is limited by model structure and observable data boundaries. The main reasons are below.

## 5.1 Incomplete observability from result files

The `.vd` output is a reporting projection of model internals, not a full execution trace. Some process-level accounting decisions are only implicit in derived attributes or in DD lineage, not directly present as a single explainable row.

## 5.2 Temporal aggregation mismatch

The app allocates mostly on annualized/summed values, while TIMES decision logic may operate across vintages, construction windows, and period boundary conventions. Small process-level differences can appear while totals still reconcile.

## 5.3 Case-dependent investment timing

For processes with non-trivial ILED/TLIFE interactions, payment timing can differ strongly by investment case. If a process has sparse or overridden input history, its reconstructed annuity path may be close but not identical.

## 5.4 DD precedence and interpolation ambiguities

Effective values depend on:

- exact DD include order at run commit,
- year-0 interpolation/extrapolation option codes,
- scenario naming and override consistency.

If any of these cannot be perfectly reconstructed from available artifacts, process-level divergence is expected.

## 5.5 Classification uncertainty

Some processes cannot be sectorized perfectly with static rules:

- multi-service or cross-sector processes,
- infrastructure that serves heterogeneous final demand,
- sparse naming conventions or legacy process labels.

The app intentionally preserves conservation and transparency, but semantic misclassification at process level can remain.

## 5.6 Commodity-flow residuals are real

Losses, exports, balancing artifacts, and partial commodity mappings create share gaps. Routing residuals to `Other` maintains accounting integrity but prevents exact attribution for every process component.

## 5.7 Policy and accounting attributes are not always one-to-one

Attributes like taxes/subsidies and certain balancing costs can be represented in ways that are internally consistent in TIMES but not uniquely mappable to one end-use sector without extra assumptions.

## 5.8 Data quality and availability constraints

Missing or stale lookup tables, unavailable historical commits, or partial DD extraction naturally reduce process-level match quality.

---

## 6. What “Good Match” Means in Practice

A robust run should prioritize these success criteria (in order):

1. System-level conservation by period (`Reg_ACost` alignment).
2. Objective-level NPV consistency (`ObjZ` alignment including salvage).
3. High classification coverage with low unallocated share.
4. Process-level mismatch concentrated in explainable edge cases.

Trying to force perfect process-by-process equality can break higher-level consistency if underlying observability is incomplete.

---

## 7. Analyst Guidance for Interpreting Mismatches

When reviewing mismatches, use this sequence:

1. Confirm DD provenance and include order for the run.
2. Verify period durations and OBLONG behavior assumptions.
3. Check process profile completeness (`NCAP_DRATE`, `NCAP_ELIFE`, `NCAP_TLIFE`, `NCAP_ILED`, `NCAP_START`).
4. Inspect whether the process is multi-vintage in the target period.

This typically separates true model-data issues from expected structural limits.

---

## 8. Scope Boundaries of This App

The app is designed for transparent reconstruction and sector allocation, not for replacing full TIMES internals. It intentionally emphasizes:

- reproducible ETL from available files,
- explicit reconciliation math,
- diagnostics for uncertainty,
- defensible system-level accounting.

Accordingly, it can deliver high-confidence totals and useful process diagnostics without guaranteeing exact equality for every process-period row.

---

## 9. Summary

UK TIMES cost handling combines annuitization, discounting, vintage timing, and precedence-driven parameter resolution. This app reproduces those mechanics as closely as possible with available artifacts and validates against both annual and objective identities.

Full process-level matching is limited mainly by observability, temporal structure, and classification ambiguity, not by a single formula error. The correct acceptance target is strong system-level consistency plus transparent, explainable residual mismatches.

---

## 10. Fuel Cost Attribution and Resource & Fuel Pathways

**Fuel Cost Attribution** implements the proportional method used by the exported resource/fuel workbooks with live selected-run evidence. It attributes upstream source-process costs to the sectors that use each fuel; it does not create direct costs on those receiving sectors. The five exported scenario CSVs are reference/test material only and are never read by the production tab.

### 10.1 Direct costs versus attribution

| Dashboard location | Accounting boundary | Cost attributes | Interpretation |
|---|---|---|---|
| Overview — **Direct Energy Supply Costs** | Graph-derived supply processes | Core process costs | Costs remain on their reporting supply processes. |
| Overview — **Direct End-use Technology Costs** | Graph-derived final-use technologies | Core process costs, with optional investment exclusion | Costs remain on the technologies delivering final energy services. |
| Overview — **Direct Power-system Costs** | Validated Power-role processes | Core process costs | Whole-system Power-role costs, not consumer allocations. |
| Fuel Cost Attribution — **Flow-cost Attribution** | Graph-derived supply source rows | `Cost_Flo` | Source costs redistributed by observed fuel-use shares. |
| Fuel Cost Attribution — **Full Supply-chain Attribution** | Connected graph-derived supply source rows | `Cost_Inv`, `Cost_Fom`, `Cost_Act`, `Cost_Flo` | Source costs redistributed by observed fuel-use shares. |

The two attribution views overlap because Full Supply-chain Attribution includes `Cost_Flo`; neither can be added to the other or to the direct-cost Overview totals. **Resource & Fuel Pathways** explains the attributed monetary incidence and physical energy evidence. It is not an additional cost total or a literal solver cost trace.

### 10.2 Inputs

1. Current authoritative process and commodity labels, read from SQL (`usr.process_labels`, `usr.commodity_labels`; maintained in the `uktm-lookup-tables` repository).
2. The VDT committed with the selected run.
3. Normalized annual, VDT-matched `VAR_FIn` and `VAR_FOut` rows.
4. The per-row VD cost table, preserving `Process`, `Commodity`, `Attribute`, `Period` and `Value`.
5. The curated resource-pool sheet `tools/lookup_tables/resource_pools.csv` (`commodity,resource_pool`, one row per exact commodity code). A blank pool is a reviewed *not pooled* decision; residential, services, industry and agriculture demand pools are reference-only and never enter a fuel split; codes absent from the sheet are unmapped and need attention.

### 10.3 Sector quantities

Compatible `IN` flows are converted to TWh and grouped by model period, resource pool and current `process_sector`. A process input is suppressed when that process also outputs the same pool in the same period, preventing a distribution chain from repeatedly appearing as demand. Inputs to genuine transformations remain visible under the transforming sector. Signed quantities are retained and a non-positive aggregate denominator is not used for allocation.

### 10.3.1 Label taxonomy and multi-role commodities

The live SQL label fields are reporting dimensions, not a replacement for the TIMES topology. `process_sector` is the NZS / Costs Sector receiving-cost axis; `process_group` is the normal technology reporting level; `process_category` is an optional archetype/geography dimension; and `process_subsector` identifies energy-system areas such as Hydrogen Generation and Hydrogen Network. GHGI, DUKES and Useful Output are separate reporting dimensions.

A commodity code is never split merely because it plays several roles. The same carrier can be an input to a final-use technology, a transformation input, a network throughput flow, and an output from a producer. The app records a **connection role** for each VDT process–commodity link. Final-use membership is an energy `IN` link to a process with authoritative non-blank `process_useful_output`; `process_set = DMD` and a `DEM` output are retained as validation evidence. Commodity function remains descriptive metadata and is not used as a universal end-use rule.

### 10.4 Source-cost ownership

Each eligible source row has one auditable outcome:

1. assigned to one mapped observed output pool;
2. safely split between mapped outputs in proportion to absolute output TWh, with shares summing to one; or
3. left unallocated because output, mapping or unit evidence is incomplete.

**Flow-cost Attribution** selects `Cost_Flo`. **Full Supply-chain Attribution** selects `Cost_Inv`, `Cost_Fom`, `Cost_Act` and `Cost_Flo` on graph-derived supply processes. These are overlapping attribution views and must not be summed. `Cost_Com` is disclosed separately.

### 10.5 Proportional allocation

For sector $s$, resource pool $r$ and model period $t$:

$$
AllocatedCost_{s,r,t}=PoolCost_{r,t}\times
\frac{SectorTWh_{s,r,t}}{\sum_j SectorTWh_{j,r,t}}.
$$

Every mapped pool reports source cost, denominator, allocated cost, denominator-unallocated cost and status. A separate source-row reconciliation proves that assigned plus unassigned source cost equals the selected source rows without duplication. Reconciliation is intentionally to these selected rows rather than `Reg_ACost`.

**Build-year split for investment.** Each source cost row is split by the sector use mix of its *share-basis year* $b$, so the general form is $AllocatedCost_{s,r,t}=\sum_{rows}Cost\times SectorTWh_{s,r,b}/\sum_j SectorTWh_{j,r,b}$. `Cost_Inv` with a reported vintage uses the model milestone whose OBLONG period contains the build year; when that year has no positive use of the pool it uses the nearest later year with use, capped at the reporting period, and the row is flagged `Nearest available year`. `Cost_Fom`, `Cost_Act`, `Cost_Flo` and `Cost_Inv` without a vintage use the reporting year. Flow-cost Attribution contains only `Cost_Flo`, so it (and the reference workbook built from it) is unchanged. Rows record `share_basis_year` and `share_basis`; `allocation_share` remains the reporting-year use share and `cost_share` is the sector's actual share of pool cost. The vintage detail sums to the headline allocation by construction.

### 10.5.1 Passing cost on through conversion

A process that uses one pool's energy to make another pool's energy is a *converter*: a gas power station, an electrolyser, a hydrogen turbine, a heat-service boiler. Its share of the input pool is not booked to its own sector; it is **passed on** to the pools it produces, split by observed output TWh. Only final use keeps cost. Loops (electricity → hydrogen → electricity) are solved exactly rather than iterated. For each sign and share-basis year, with $D$ the vector of each pool's own source cost and $T_{k,r}$ the share of pool $k$'s use taken by converters producing pool $r$:

$$
C=(I-T^{\top})^{-1}D,\qquad AllocatedCost_{s}=\sum_{r}C_r\times FinalShare_{s,r}.
$$

Every pool balances: own source cost + cost passed in = final use + cost passed on + unallocated. Cost that enters a pool with no route to final use is reported as **Unallocated** rather than dropped. Sector totals count each cost once. Both Flow-cost and Full Supply-chain Attribution cascade. `process_allocations` is a ledger with a `sign`, `role` (Final use, Conversion, Unallocated) and `passed_to_pool` per row; pool summaries add `passed_in_cost_m` and `passed_on_cost_m`. Fuel Cost Attribution charts show each pool's full cost by sector (`pool_sector_cost_rows`): a sector's final use plus the share it converts into another pool (power stations' gas appears under Power in the gas pool), and an **Unallocated** segment. Each pool's bar for a year equals that pool's node total in the Resource & Fuel Pathways cost Sankey. Pools that feed each other both include the conversion cost, so bars must not be added across pools as a system total; there is no headline cost metric for that reason.

**Display vocabulary.** "Passed in" and "passed on" are internal names only. Everything a reader sees calls a converter feeding a pool *Supply* and a converter drawing on a pool *Demand*, like any other process. Every displayed or exported table (workbook raw sheets, Hydrogen evidence workbook) renames the columns through `with_conversion_column_names`: `passed_in_cost_m` → `conversion_supply_cost_m`, `passed_on_cost_m` → `conversion_demand_cost_m`, `passed_to_pool` → `output_pool`. Pathway stages are `conversion_to_resource` / `resource_to_conversion`, cost-Sankey node kinds are `Supply` / `Demand`, and Hydrogen proof stages are *Conversion supply* / *Conversion demand*. This is a breaking change to the exported column names and stage values; the workbook cache key is `traceable-workbook-v4` and `VINTAGE_ALLOCATION_SCHEMA_VERSION` is 4.

### 10.6 Time resolution and limitations

Milestone calculations are authoritative. Optional annual output linearly interpolates displayed allocations between milestones and labels the inserted years; it does not alter milestone values or create model observations. Negative source costs are retained. The tab does not use the removed legacy recursive `AllocationMatrix`, does not classify by process-name prefixes, and does not invent separate `FLO_DELIV` and `IRE_PRICE` values from combined `Cost_Flo`.

### 10.7 Cost and physical pathway Sankeys

The normal cost-allocation Sankey is **fuel type → sector**. Each link is read directly from final `allocated_cost_m`, so the displayed monetary total is identical to the corresponding selected cost chart. Positive **Costs** and negative **Credits & revenues** are selected independently. The credit option is shown only when negative allocated values exist for the selected cost basis, year and fuel types. Signed values remain unchanged in the download; because Plotly requires non-negative widths, credit links use absolute display widths while retaining negative signed values.

The pool-level pathway Sankey uses the page's **Cost attributes** selection and is built from the cascade ledger. When both ends of a conversion are selected pools, cost flows pool → pool; otherwise it enters from, or leaves to, the **converter processes** that carry it, so a converted cost appears in both pools' diagrams. Converter nodes are ordinary **Supply** (left) or **Demand** (right) nodes using the same process-label taxonomy and grouping/colour selectors as other endpoints. Each displayed code (process, or taxonomy label when grouped) is drawn as **one node** whatever its role (`_cost_endpoint_node_id`), and each node meets a pool through **one flow**: final use and conversion demand by the same code (or own cost and conversion supply) are summed into one link when drawn, and in the Net view a cost and a credit on the same pair net to one link (`_merge_parallel_cost_links`). Stage totals and the balance bar use the unmerged links. When a code both supplies and draws on the pool, node hover shows the supply and demand amounts. Link detail shows only the pool and amount. An **Unallocated** node shows cost with no route to final use. Pool hover gives the pathway total.

The pool-level pathway is one **net** Sankey (`sign=SIGN_NET`). Costs and credits are summed on each displayed link after grouping. A link that nets to a credit or revenue keeps its negative `signed_value` but is drawn in green in the reverse direction (`net_direction` = *Net credit (drawn reversed)*), because Plotly widths cannot be negative; its non-pool end keeps the same node. Every node still balances in signed terms. Link hover shows the net amount and the gross cost (`gross_cost_m`) and gross credit (`gross_credit_m`). The separate **Costs** and **Credits & revenues** modes remain available in `prepare_supply_chain_cost_pathway_links` and are unchanged.

The physical Sankey is selected hierarchically by model year, reviewed resource pool, and authoritative `commodity_name` (for example, **Petroleum products → Petrol**). The fuel selector sits inside the physical subtab and defaults to **All fuels**: each fuel is netted separately at each process and the per-fuel results are summed, so pass-through of one fuel never cancels another. Fuel and service pools (pool kind from `resource_pools.csv`) are listed together in every pool selector, coverage table and evidence table; demand pools are not listed. One list is sound because the cascade books each cost to final use once. It is displayed as **Supply → Individual fuel → Demand**. All positive compatible matched exact commodity codes under the selected fuel are pooled at each process. Compatible PJ is converted using $1\,TWh=3.6\,PJ$. A process contributes its selected-fuel output less its selected-fuel input: a positive net is Supply, a negative net is Demand, and a balanced same-fuel pass-through is suppressed. This means balanced terminals, networks and storage cycles do not appear as new production, while net storage discharge is Supply and net charging is Demand. Exact commodity codes remain in audit tables and downloads; no other fuel is recursively added.

In stakeholder mode, authoritative Coal/Gas/Oil Extraction processes and export-direction legs of processes labelled with subsector "Fuel Re-imports" are hidden to remove domestic extraction and UK TIMES dummy-export legs. Genuine imports, exports and import-side re-import processes remain visible. Technical mode restores the hidden valid edges, while audit tables and downloads always retain exact process codes, quantities and visibility reasons. One Advanced-display control groups both physical endpoint sides by the same authoritative taxonomy dimension, defaulting to **Subsector**; grouping occurs after process-level netting and cannot change balances. Both Sankeys use model milestones only. The source/output, demand/input, process-net, hidden-evidence, and exact-code balance tables retain auditable evidence. Supply-demand gaps, exact-code residuals and VD rows unmatched to VDT are disclosed separately; no balancing link is fabricated. The monetary Sankey remains resource-pool based and is unaffected by individual-fuel visibility or grouping.

**Pathways layout.** Each Pathways subtab shows a one-line explainer, the Sankey, then a **balance bar** for the selected pool instead of metric tiles. The cost bar shows *Supply* (enters) against *Demand* and *Unallocated* (leaves); its title states *Balanced* or *Not balanced* from the pathway's own `pathway_status`, and enters minus leaves equals `reconciliation_difference_m`. The physical bar stacks *Imports* and *Other supply* against *Final consumption*, *Exports* and *Other use*; its title names any non-zero `supply_demand_gap_twh`. All other material (the balance table, explanatory text, grouping, colour and node-detail controls, and one selectable evidence table) sits in a single **Details** expander per subtab. Only presentation changed; calculations, tolerances and the workbook are unchanged.

### 10.8 Reference-format workbook export

The primary download is a formatted, selected-run XLSX workbook with a fixed 2025–2050 layout: TWh, source **Resources Costs by Fuel**, Electricity Costs, Resource Costs, and Fuelex by DESNZ Sector. It is generated in memory from the selected run and never reads the scenario CSVs or reference workbook at runtime.

For each annual year, the workbook first linearly interpolates eligible primitive fuel-use and source-cost series between model milestones, without extrapolation. It then recalculates shares and allocations; it never interpolates already allocated endpoint costs. Biomass and biomethane remain separate TWh rows, but their source cost uses their combined consumption share. The workbook distributes electricity only across non-Power electricity users. Its Electricity Costs row two equals row one plus the Power sector's direct net fuel cost, while Power receives no distributed-electricity row. `Cost_Com` is not added to these calculations. The workbook does not yet follow cost through conversion (§10.5.1); its formula sheets show the one-step split, so its totals can differ from the dashboard.

**Fuel Cost Attribution** is one lazy page with a **Cost attributes** pill selector (`Cost_Inv`, `Cost_Fom`, `Cost_Act`, `Cost_Flo`; default all four). All four is the Full Supply-chain view and `Cost_Flo` alone is the Flow-cost view; any other subset is a supply-chain view over just those attributes (`attribute_view` in `resource_allocation.py`), using the same source-process gate, share basis and cascade, so subsets add up exactly to the full view. The selection is shared with **Resource & Fuel Pathways** (session key `resource_cost_attributes_v1`). **Resource & Fuel Pathways** is a separate lazy top-level view. Their milestone years use categorical chart spacing so stacked columns have the same readable width as other dashboard charts. Fuel Cost Attribution is deliberately short: a one-line caption pointing to **Methodology → Attribution methodology**, one row with the resource-pool multiselect and the **Annual estimates** toggle, the sector chart, **Unit cost by final user** (pool and breakdown selectors, chart, and one collapsed **Unit-cost data and reconciliation** expander), then **Checks and download**: **Which fuels are counted**, a collapsed **Allocation balance check (all pools)** and the workbook download (with several runs: the comparison chart and one collapsed checks-and-workbook panel per run). Explanatory notes are plain captions; coloured alerts are kept for real problems such as fuels not yet accounted for. The method, the worked OILPET example and the notes on reading the page live on **Methodology → Attribution methodology**. Raw evidence tables are not shown on screen; they are all in the full workbook. The **step-by-step walkthrough** lives on its own **Methodology → Step-by-step walkthrough** tab (see below).

**Unit cost by final user** follows the sector chart (one shared section with several runs, one chart per run). Pick one pool (default Electricity) and a breakdown: any process label from the pathway selectors, **Region**, or **Individual processes** (`FINAL_USER_UNIT_COST_BREAKDOWNS`). `final_user_unit_costs` in `resource_allocation.py` divides the net (costs plus credits) `Final use` rows of `process_allocations` for that pool by the same consumers' own-period `Final use` TWh in `consumer_use`, summed per group (a ratio of sums; £M/TWh = £/MWh). `consumer_use` records each consumer's included use and cascade role in its own period's network. With all four cost attributes this is the full supply chain, including cost passed in from other pools; other subsets show a warning. Converters are excluded from cost and use. Investment cost reaches users by build-year shares, so users can differ; a group with cost but no use this year has status `Cost without reporting-year use` and no unit cost. A collapsed reconciliation table shows own source cost + passed in = final use + passed on + unallocated per year, and the all-final-users average (dashed in the chart). No interpolation, and no workbook export.

### 10.9 Methodology — Hydrogen attribution proof

The **Methodology** screen has four lazy tabs: **Reconciliation methodology**, **Attribution methodology** (the Hydrogen proof), **Step-by-step walkthrough** and **Unit cost walkthrough**, each with its own run picker. **Unit cost walkthrough** (`tabs/unit_cost_walkthrough.py`) derives the Fuel Cost Attribution **Unit cost by final user** for one pool, model year and grouping, using the shared cost-attribute selection: 1 pool cost (own source cost plus cost passed in), 2 where it goes (final use, passed on, unallocated, with a balance check), 3 final use versus converters (converter TWh and passed-on cost are excluded), 4 each final-user process's cost, use and £/MWh (with a check that they add up to the final-use cost), 5 the chosen grouping with each group's difference from the all-users average, 6 the unit cost as final-use cost ÷ final-use TWh, and 7 the chart across years. Every table restates `final_user_unit_costs`, `consumer_use` and `process_allocations`; nothing is recalculated. **Step-by-step walkthrough** follows one resource pool in one model year, using the shared cost-attribute selection: 1 resource pools used, 2 supply into the pool (own supply cost by technology group plus energy from other pools, totalling the pool cost), 3 demand from the pool (sector TWh and share of use), 4 split of the pool cost into kept as final use, passed on via conversion and not allocated, with a check that it adds up to the pool cost, 5 where passed-on cost goes and where the pool's own supply cost finally lands, and 6 the Fuel Cost Attribution chart for that pool. Every step restates `pool_summary`, `cost_assignments`, `allocations`, `process_allocations` and `cascade_allocations`; nothing is recalculated. The **Attribution methodology** tab opens with two collapsed sections, **How Fuel Cost Attribution is calculated** (the four steps, formulas and a live OILPET `Cost_Flo` example, prepared only when opened) and **Reading the Fuel Cost Attribution page**, then holds the **Hydrogen proof**, a selected-run, selected-period Hydrogen worked example that reuses exactly the same cached Full Supply-chain allocation result as the Fuel Cost Attribution screen. Neither recreates or varies the allocation calculation.

The proof starts with the whole selected-period Hydrogen system: all compatible observed Hydrogen producers, all included Hydrogen-input users, the sector-use denominator, reported Cost_Inv and Cost_Fom vintages, and explicit excluded input rows such as same-pool pass-through. A `structural_end_use` flag distinguishes the VDT-derived end-use boundary from the broader set of processes that contribute to the sector-use denominator.

**Source-cost boundary** is not a native TIMES set and is explained in the interface. It is a run-specific, VDT-derived upstream process set used only to determine which source-cost rows can enter the Full Supply-chain pool. It begins with same-region VDT producers of energy consumed by a process with non-blank Useful Output, then follows eligible energy inputs recursively to their producers. It excludes final-demand/end-use processes, UK TIMES allocation processes, and non-energy, emissions, demand or proxy commodities (by `commodity_type`). Dummy/accounting processes are not inferred from names or descriptions; until they carry a dedicated label they remain in scope. A compatible Hydrogen output outside this boundary is still shown in the physical evidence table and Sankey, but its costs do not enter the Hydrogen cost pool. Separately, an input is included in the use denominator only if it is non-zero, mapped to a compatible energy unit, and is not a same-process/same-pool input-output pass-through; excluded inputs retain their exact reason.

It presents two deliberately separate flow diagrams. The **physical activity** diagram uses observed annual TWh and follows producer → Hydrogen pool → user process → sector. Link hover detail gives the model process code, quantity, sector, structural-end-use flag and source-cost-boundary status. The **financial attribution** diagram uses £M/year and follows one total per eligible source process → Hydrogen cost pool → sector; link hover detail breaks that total into Cost_Inv, Cost_Fom, Cost_Act and Cost_Flo, and includes reported vintages where available. Costs and credits are netted on each displayed link (`net_financial_links`); net credits are drawn reversed, and hover shows the gross cost and credit. The evidence workbook and balance checks keep costs and credits as separate signed rows. They are linked as an explanation but their widths must not be compared: the former is observed energy activity and the latter is proportional cost incidence. Producer output and user input are independent observations; any gap is displayed rather than converted into a fabricated direct route or loss.

The financial diagram is read from the same cascade ledger as the headline (§10.5.1). Converters such as electrolysers appear as Hydrogen **supply** (stage *Conversion supply*), sharing a node with the same label's own source cost. Cost leaves as DUKES final use, as **demand** by converters such as hydrogen turbines (stage *Conversion demand*), or as **Unallocated**. The DUKES table shows `conversion_demand_cost_m`; `cost_share` is a class's share of all demand on the pool. Methodology metrics show Supply, Demand and Unallocated. Balance checks prove supply = demand + unallocated, overall and per sign.

The optional source-to-sector zoom-in replays the first step only: one source's own cost split by Hydrogen use in each row's share-basis year. It excludes the cost of energy converters use to make Hydrogen, and a share used to make another pool's energy follows that pool, so it can differ from the DUKES table. The proof does not book a direct cost on recipient technologies, claim physical ownership of costs, add `Cost_Com`, synthesize vintages, or make an additional total that can be added to direct-cost or Flow-cost views. Its on-demand evidence workbook includes all Hydrogen sources, all users, excluded flows, physical and financial graph links, source rows, sector shares, reported vintages, diagnostics, and balance checks.
