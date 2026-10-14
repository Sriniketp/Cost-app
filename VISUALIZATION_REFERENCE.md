# Cost-Allocation App — Chart & Visualisation Guide

This document explains what each chart in the app shows, what types of technologies and costs it covers, and the logic used to classify and group over 2,000 UK TIMES model processes into meaningful visualisations.

---

## Colour policy

Interactive and generated Plotly charts use the DESNZ categorical palette in
the reviewed preference order: deep navy, sky blue, golden yellow, orange,
teal, red, muted purple, dark burgundy and green, followed by their lighter
variants. Categories are assigned deterministically, so the same displayed
label retains its colour when charts rerun or source-row order changes.

Charts retain distinct categorical hues for up to 18 visible additive
categories. When an additive chart contains more categories, it keeps the 17
largest by total absolute contribution and combines the rest into **Other**;
the signed total is unchanged. Runs, investment vintages, reconciliation
evidence and Sankey topology are never combined. Runs and vintages retain
their identity and use patterns or line styles after the palette is exhausted;
Sankey topology remains structurally intact. Semantic
colours remain meaningful: green indicates matched/pass status, golden yellow
indicates warnings, and red indicates errors or exports.

**Carbon credits and dummy imports.** In every stacked column chart on the
Overview tab (System, Direct energy supply and Direct end-use breakdowns, and
Fuels vs Capital) these categories are drawn last, so they sit on top of each
column. A negative value is drawn below zero instead. The labels matched are
*Carbon credits*, *Emission Credits*, *CO2 credits*, *Dummy imports* and
*_DUMMY IMPORTS*, ignoring case. They are never folded into **Other**, and a
one that is zero in every period and run is left out of the chart and legend.
The rule lives in `colours.pin_categories_last` and is applied by
`overview_current._render_multirun_stacked_columns`. Charts grouped by a
field where credits have no label of their own (for example GHGI or Process
set) cannot single them out.

---

## How We Classify Every Technology

The UK TIMES model contains thousands of processes — power plants, boilers, vehicles, refineries, pipelines, import terminals, and more. To present costs meaningfully, every process is classified from the authoritative SQL process labels (`usr.process_labels`, loaded by `labels.py`), which provide `process_sector`, `process_subsector`, `process_group`, `process_category`, `process_name` and related fields. Commodities are labelled the same way from `usr.commodity_labels`.

### Sectors (which part of the economy?)

| Sector | Covers |
|--------|--------|
| Agriculture | Farm equipment, agricultural heating, tractors |
| Electricity | Power generation, storage, interconnectors |
| Industry | Furnaces, kilns, motors, chemical processes |
| Residential | Domestic heating, cooking, lighting, insulation |
| Services | Commercial buildings, offices, retail heating/cooling |
| Transport | Cars, buses, HGVs, rail, aviation, shipping |
| Resources | Oil/gas extraction, coal mining, fuel imports |
| Processing | Refineries, hydrogen production, biomass conversion |
| Engineered Removals | DACCS, BECCS, engineered CO₂ removal |
| CO2 Credits | Carbon credit/offset mechanisms |
| Waste | Waste processing, landfill gas |
| Multiple | Processes serving more than one sector |

### Classification source

Classification comes only from the SQL process labels. There are no name-prefix heuristics, no `proc_merged.csv` seed and no user-override CSV. Process codes in the run that have no SQL label are reported explicitly in the dashboard rather than being guessed.

---

## Cost Components

Every technology can incur up to four types of annual cost:

| Component | What it represents |
|-----------|--------------------|
| **Cost_Inv** | Annualised capital investment (spread over the economic life of the asset using a capital recovery factor) |
| **Cost_Fom** | Fixed operating & maintenance costs (incurred regardless of output) |
| **Cost_Act** | Variable activity costs (proportional to how much the technology operates) |
| **Cost_Flo** | Fuel/commodity flow costs (cost of inputs consumed, e.g. gas price × gas consumed) |

---

## Charts

### Batch Mode (Static Exports)

These charts are generated automatically for each scenario run and exported as HTML/PNG files.

---

### C1: Sector Cost Over Time (Stacked Area)

**What it shows:** The total annual energy system cost split by sector, from the base year through to 2050+. Each coloured band represents one sector's contribution to total cost in that year.

**Technologies covered:** All classified processes — every power plant, vehicle, boiler, refinery, pipeline, etc. is summed into its assigned sector. Only "Unallocated" processes (those that failed classification) are excluded.

**How to read it:** The height of the total stack shows total system cost in £bn/yr. Growing bands indicate sectors with increasing cost (e.g. Transport may grow as electrification requires new capital). Shrinking bands show sectors where costs decline.

---

### C2: Cost Breakdown by Component (Stacked Bars)

**What it shows:** For the most recent 3 time periods, a bar chart showing how each sector's costs decompose into Investment, Fixed O&M, Activity, and Fuel costs.

**Technologies covered:** Same as C1 — all classified processes, grouped by sector and then split by cost component.

**How to read it:** Tall "Investment" bars indicate capital-intensive sectors (e.g. Electricity with new wind/nuclear builds). Tall "Fuel" bars indicate fuel-dependent sectors (e.g. Transport with petrol/diesel, or Resources with gas imports). Comparing across periods reveals the transition from fuel-heavy to capital-heavy as decarbonisation progresses.

---

### C3: Supply-Side Cost Breakdown (Stacked Area)

**What it shows:** The cost of supplying fuels and energy carriers over time, broken down by sub-category (e.g. oil imports, gas imports, biomass processing, electricity generation fuel costs).

**Technologies covered:** Only **supply-side** processes — fuel import terminals, gas processing, oil refineries, coal mines, biomass conversion, hydrogen production. End-use technologies (boilers, cars) are excluded.

**How to read it:** Shows how the UK's fuel bill evolves. Declining oil/gas import costs reflect reduced fossil fuel dependence. Rising hydrogen or biomass costs show new supply chains emerging.

---

### C4: Scenario Comparison (Grouped Bars)

**What it shows:** The difference in costs between an alternative scenario and a baseline scenario, broken down by sector and time period. Only available when 2+ scenarios are loaded.

**Technologies covered:** All classified processes in both scenarios. The chart computes the delta (alternative minus baseline) for each sector in each period.

**How to read it:** Positive bars mean the alternative scenario is more expensive in that sector/period; negative bars mean it saves money. Useful for understanding the cost impact of policy choices (e.g. "what does banning gas boilers cost the Residential sector?").

---

## Interactive Dashboard (Streamlit)

The web-based dashboard offers deeper exploration with filters, toggles, and multi-run comparison. It is organised into tabs:

---

### Overview Tab — System & Sector Costs

**What it shows:**
- **Headline metrics** — Total discounted system cost (ObjZ), total undiscounted spend, peak annual cost
- **Annual system cost line** — Total cost trend across all periods (one line per scenario if comparing)
- **Sector cost stacked area** — Same concept as C1 but interactive, with toggles to include/exclude CO₂ credits

**Technologies covered:** All processes with the four core cost attributes. Excludes air quality damage costs and internal accounting ("underscore" sectors like dummy imports).

**Why some items are excluded:** CO₂ credit transactions are transfers (not real resource costs) and air quality externalities are damage valuations (not payments), so they're excluded from the headline system cost but can be toggled on.

---

### Overview Tab — Direct Energy Supply Costs

**What it shows:**
- Total direct energy supply cost trend
- Direct energy supply costs by current process sector
- Direct energy supply costs by technology group (e.g. "Electricity generation", "Oil refining", "Gas import")
- Direct energy supply costs by cost component
- Cumulative direct energy supply spend

**Technologies covered:** Graph-derived supply processes that produce, convert or deliver energy to final-use technologies in the selected run. The boundary follows the selected run's VDT connections rather than a manually assigned sector, and costs remain on their reporting processes.

**Grouping logic:** Current process taxonomy labels provide readable sector and technology-group breakdowns for the graph-derived process boundary.

---

### Overview Tab — Direct End-use Technology Costs

**What it shows:** Costs for demand-side technologies only — the technologies that directly deliver energy services to consumers (heating, transport, industrial processes).

**Technologies covered:** Only processes in end-use sectors: Residential (heat pumps, boilers, insulation), Transport (EVs, ICE vehicles, rail), Industry (furnaces, motors, CHP), Services (commercial HVAC, lighting), Agriculture (farm equipment).

**Toggle:** Users can include or exclude capital investment costs. Excluding them shows only the ongoing fuel + O&M burden on consumers; including them shows the full cost including upfront technology purchase.

---

### Overview Tab — Direct Power-system Costs

**What it shows:**
- **Total annual power-system cost** for All or one selected role
- **Average power-system cost contribution** in £/MWh supplied
- A selectable cost breakdown using current process taxonomy
- An auditable electricity-balance denominator and process/exclusion tables

**Technologies covered:** Only the four validated roles **Generation**, **Storage**, **Network** and **Interconnector**. Current SQL `usr.process_labels` names and validates a candidate role; the VDT committed with the selected run must prove the relevant electricity connection. Power-labelled processes with incomplete or contradictory evidence are excluded from All and shown in the audit rather than forced into an Other category.

**Metric:** The selected role uses the same run-period denominator as All, making roles additive contributions rather than incomparable ratios:

$$
Supplied = Generation + Imports + Storage\ discharge - Storage\ charge - Exports - Explicit\ losses
$$

$$
Contribution_{role} = Cost_{role} / Supplied.
$$

The primary bus is the unique non-surplus electricity commodity structurally output by validated generators. Quantities come only from VDT-matched normalized annual VD flows in PJ, converted using $1\,TWh = 3.6\,PJ$. Surplus electricity is not counted positively. Final-consumption electricity is a reconciliation diagnostic, not the denominator. The result is an average whole-system contribution, not technology LCOE, a marginal price, or an allocation-derived consumer price. `Cost_Flo` is reported by standard VD only as a combined value and is not artificially split into delivery and trade components.

**Worked example:** The "How the power-system metric is calculated" expander ends with a worked example calculated live from the first loaded run that has one. It takes the highest-cost Generation technology in the latest period with an available denominator. It then shows the technology's role evidence, its four cost components, the full net-supplied balance in PJ, the conversion to TWh, its £/MWh contribution, and how that contribution fits into the Generation total. It also shows the technology's own output to explain why the metric is not an LCOE.

---

### Technology Drill-Down Tab

**What it shows:** Deep-dive into a single technology's cost reconciliation:
- Whether the model's reported annualised investment matches what we'd expect from the capital cost inputs, discount rate, and economic life
- Vintage breakdown (which build years contribute to cost in each period)
- Annual payment waterfall (capital recovery schedule)

**Technologies covered:** Any individual process the user selects. Most useful for large capital investments (offshore wind farms, nuclear plants, hydrogen electrolysers, CCS facilities) where understanding the annualisation arithmetic matters.

**Purpose:** Validates that the cost extraction pipeline correctly reconstructs TIMES solver outputs and flags any discrepancies.

---

### Fuel Cost Attribution and Resource & Fuel Pathways

**What it shows:** A top-down attribution of selected upstream fuel-type source costs to current process sectors. Each sector receives the same share of a fuel-cost pool as its share of observed fuel use. **Fuel Cost Attribution** is one page with a **Cost attributes** multiselect (all four = Full Supply-chain; `Cost_Flo` alone = Flow-cost; any subset allowed, shared with **Resource & Fuel Pathways**); **Resource & Fuel Pathways** is a separate lazy view. The page shows its sector chart, formula and downloads for the chosen attributes; categorical year spacing keeps milestone columns broad and readable.

**Two separate views:**

- **Flow-cost Attribution** allocates `Cost_Flo` source rows.
- **Full Supply-chain Attribution** allocates `Cost_Inv`, `Cost_Fom`, `Cost_Act` and `Cost_Flo` for graph-derived supply processes.

The attribution views overlap and are not additive. They are also not direct receiving-sector costs and must not be added to Overview's direct process-cost totals. `Cost_Com` is reported separately. Standard VD does not expose a reliable `FLO_DELIV`/`IRE_PRICE` split, so the tab retains combined `Cost_Flo`.

**Quantity and mapping logic:** Quantities come from selected-run VDT-matched normalized annual `VAR_FIn` rows. PJ is converted using $1\,TWh=3.6\,PJ$. A curated sheet (`tools/lookup_tables/resource_pools.csv`) assigns each exact commodity code to at most one carrier pool. Allocation aliases carry the canonical primary-carrier pool so same-pool process pass-through remains suppressible. CHP, heat and useful-energy carriers use distinct secondary pools; these expose different conversion stages and are not additive whole-system totals. Energy listed with no pool (for example CCS-retrofit proxies) is a reviewed *not pooled* answer: visible in coverage, not counted. Demand pools stay outside the fuel split. Genuine transformation inputs remain sector consumption. `ELCWSTHEAT` and `PRCWSTHEAT` are in the `Waste heat` pool; energy absent from the sheet and incompatible units remain in the audit.

**Cost ownership and reconciliation:** A source row follows its mapped output carrier. Safe multi-output rows are split by observed output shares; uncertain rows remain explicitly unallocated. Therefore a row contributes once to a corrected total. For each pool:

$$
AllocatedCost_{sector}=PoolCost\times\frac{SectorTWh}{TotalTWh}.
$$

Allocated plus denominator-unallocated cost reconciles to each mapped source pool, while source-row reconciliation separately reports costs that could not be mapped to any pool. Optional annual interpolation is presentation-only and leaves milestone values unchanged.

**Following cost through conversion:** a converter's share of its input pool follows the pool it produces rather than being kept, solved end-to-end, so gas burnt for power reaches electricity users. On screen a converter is just a Supply or Demand process. Each pool balances supply = demand + unallocated. Sector charts show each pool's full cost: final use plus the converter sectors' share (e.g. Power in the gas pool) plus an **Unallocated** segment, so a pool's bar equals its Sankey node total for that year. Bars for pools that feed each other overlap and must not be added across pools.

**Pool-level pathway Sankey (£M/year):** Supply → resource pool → Demand for the selected **Cost attributes**. Converted cost flows pool → pool when both pools are selected, otherwise via converter-process nodes drawn as ordinary Supply (left) or Demand (right) nodes, one node per code, labelled and coloured by the supply-side or demand-side taxonomy selectors; an **Unallocated** node shows cost with no route to final use. Costs and credits are netted per displayed link in one diagram; a link that nets to a credit is drawn green in the reverse direction, and hover shows the net amount with gross cost and gross credit.

**Pathways balance bar:** below each Pathways Sankey, two stacked horizontal bars (in vs out) for the selected pool. Equal lengths show nothing is missed; the title states *Balanced* or *Not balanced* with the gap. Cost: Supply vs Demand + Unallocated. Physical: Imports + Other supply vs Final consumption + Exports + Other use (TWh/year). Tables, explanations and display controls are in one **Details** expander per subtab.

**Cost-allocation Sankey (£M/year):** fuel type → sector, using the final `allocated_cost_m` values shown in the selected sector-cost chart. This is an incidence allocation, not a literal flow of money through the VDT. Positive Costs and negative Credits & revenues are separated, but the credit control appears only when negative allocated values exist. Credit widths use absolute magnitudes while downloads preserve their negative signs. Displayed links reconcile exactly to the sign-specific selected allocation.

**Individual-fuel physical Sankey (TWh/year):** select model year → resource pool → authoritative individual fuel (default **All fuels**: per-fuel netting, then summed; the pool list combines fuel and service pools). VDT provides allowed topology and normalized annual VD provides observed quantities. The visible graph is Supply → Individual fuel → Demand: all positive compatible exact codes under the selected `commodity_name` are pooled per process, then selected-fuel input is subtracted from selected-fuel output. Positive process net flow is Supply, negative net flow is Demand, and balanced annual pass-through is suppressed. Exact codes remain in the process-net, source/output, demand/input and balance audits; they are not visible intermediate nodes. Stakeholder mode hides authoritative Coal/Gas/Oil Extraction processes and the export-direction legs of processes labelled subsector "Fuel Re-imports"; genuine imports, exports, and import-side re-imports remain visible. Technical mode restores hidden valid edges. One physical grouping control applies the same taxonomy dimension to both endpoint sides and defaults to **Subsector**; it cannot change process-level net totals. Supply-demand gaps, exact-code residuals, incompatible rows and unmatched VD/VDT rows are disclosed without a fabricated balancing link. The selected graph never recursively adds another fuel.

**Workbook boundary:** Reviewed secondary pools remain available in mapping, coverage, pool-summary and pathway evidence. The fixed reference TWh, source-cost, Resource Cost and Fuelex tables intentionally retain their primary-fuel categories; secondary-pool exclusions are labelled as reviewed rather than reported as unknown mappings. The Checks sheet states that source-fuel rows and Electricity (distributed) rows must not be summed across sectors, because distributed electricity already contains the Power sector's own fuel cost, and it names any model year that has Power fuel cost but no core Power cost.

Both Sankeys use authoritative milestone periods and live only in the dedicated Pathways subtab. Physical link hover is deliberately concise — fuel, endpoint, role, net selected-fuel flow and displayed width — while exact-code contributors, netting basis, visibility and grouping evidence remain in the audit tables and downloads. £M/year and TWh/year are different measures: their widths must not be compared or added. Fuel Cost Attribution shows the sector chart for the chosen pools, the unit-cost chart, a **Which fuels are counted** expander and a collapsed **Allocation balance check (all pools)**; explanatory notes are captions, and the method, worked example and reading notes are on **Methodology → Attribution methodology**. The step-by-step walkthrough is on the Methodology tab and raw evidence tables are only in the full workbook.

**Workbook:** The primary XLSX uses the fixed 2025–2050 reference layout and derives annual values by interpolating TWh/source-cost primitives before recalculating shares. Unit conversion is shown separately for each model year, and pool source costs are live formula totals over retained process-level assignment rows. Biomass and biomethane are separate TWh rows but have one combined bio cost denominator. Distributed electricity is allocated only to non-Power sectors. The workbook does not yet follow cost through conversion; it shows the one-step split. The workbook prepares all attribution views only when requested so `Cost_Com` can be disclosed separately without entering any allocation formula, and it is generated entirely from selected-run data—reference CSVs and the uploaded workbook are design/test evidence only.

---

### Multi-Run Comparison

When multiple scenario runs are loaded simultaneously, the dashboard provides:
- **Overlay lines** — same metric plotted for each run on one chart
- **Faceted bars** — side-by-side bars comparing runs within each time period
- **Delta tables** — numerical difference between runs, highlighting which technologies drive cost differences
- **Top cost movers** — ranks the 30 processes with the largest cost change between scenarios

---

## Summary of Coverage Logic

| Layer | What it catches |
|-------|----------------|
| SQL process labels (`usr.process_labels`) | Every labelled process code; sector, subsector, group and category come from these labels |
| Unlabelled processes | Process codes with no SQL label are listed explicitly in the dashboard; they are not classified by name prefix |

There is no `proc_merged.csv` lookup, name-prefix rule set or user-override layer.
