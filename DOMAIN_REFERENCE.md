# Glossary — Concepts, Definitions, and Units

A plain-language reference for all key terms used in the Cost Allocation App and the underlying UK TIMES model.

---

## 1. What Does the App Show?

| What the chart shows | Units | Meaning |
|---|---|---|
| Y-axis on all cost charts | **£B/yr** or **£M/yr** | An **annual rate** of spending, undiscounted, in constant 2010 prices |
| X-axis | Period (milestone year) | The representative year for each model period (e.g. 2025, 2030, 2035…) |
| "Total Undiscounted Spend" metric | **£bn** | Annual cost × period duration, summed over all periods — a cumulative total |
| "ObjZ" metric | **£bn** | Discounted present value of all system costs — what TIMES actually minimises |

**Key point:** The bars/lines on charts are *annual rates* (cost per year), not totals for the whole period. To get a period's total spend you would multiply by its duration D(t).

---

## 2. Time Concepts

### Period
A block of consecutive years (e.g. 2026–2030) that TIMES treats as one decision interval. The model solves for capacity and activity decisions once per period.

### Milestone Year — M(t)
The single year chosen to represent a period in results. Typically the middle year of the period. Example: for period 2026–2030, the milestone year is 2028.

### Period Duration — D(t)
The number of years in a period. Example: D = 5 for a 5-year period. Periods can have different durations (the early periods are often shorter).

### Period Start (OBLONG) — B(t)
Under OBLONG conventions: `B(t) = M(t) − ⌊D(t)/2⌋`. This is the first year of the period, used for timing investment payments.

### Reference Year — REFYR (G_DYEAR)
The year to which all costs are discounted when computing NPV. In this model: **2010**.

### Vintage
The year in which a unit of capacity was built (the investment decision year). A process can have multiple vintages coexisting — e.g. wind turbines built in 2020 and 2030 both contributing to 2035 output.

---

## 3. Cost Attributes (What TIMES Reports)

These are the result attributes read from VD files. **All are annual values (£/yr)**, undiscounted, reported at each period's milestone year.

### Cost_Inv — Annualised Investment Cost
The annual payment stream for capital investments. This is **not** the overnight capex — it is the capex annualised (spread) over the economic life using a CRF. It includes payments from all active vintages in that period.

### Cost_Fom — Fixed O&M
Annual cost per unit of installed capacity, incurred regardless of whether the process runs. Driven by `NCAP_FOM × VAR_Cap`.

### Cost_Act — Variable O&M (Activity Cost)
Annual cost proportional to how much the process produces. Driven by `ACT_COST × VAR_Act`.

### Cost_Flo — Fuel & Flow Costs
Costs attached to commodity flows into/out of a process — includes fuel purchase costs and import prices (`FLO_COST`, `IRE_PRICE`). Export revenues appear as negative values.

### Cost_Com — Commodity Costs
Costs on the total/net production of a commodity (`COM_CSTNET`, `COM_CSTPRD`).

### Cost_Salv — Salvage Value
A lump-sum credit (negative cost) for investments still active at the end of the model horizon. It has no period index — it is a single value at year EOH+1. Reported at the discount year.

### Cost_ire — Implied Trade Costs
Inter-regional trade valued at shadow prices. Sums to zero across all regions.

---

## 4. Aggregate/System Attributes

### ObjZ — Objective Function Value
The single scalar that TIMES minimises: the **total discounted present value** of all system costs across all regions and all years, discounted to REFYR. Units: £M (displayed as £bn in the app).

### Reg_ACost — Regional Annual Cost
The sum of all cost components for a region in a period. This is what you'd get if you added up all the Cost_* attributes for that period.

### Time_NPV — Period NPV Factor
The discount factor that converts an annual cost at period t into its contribution to ObjZ:

$$ObjZ \approx \sum_t Reg\_ACost(t) \times Time\_NPV(t)$$

This identity holds with high accuracy when `$SET ANNCOST LEV` + `$SET OBLONG YES` are active (as in this model).

---

## 5. The `$SET ANNCOST LEV` Switch — Why It Matters

| Without it (pre-v3.5) | With it (this model) |
|---|---|
| Cost_Inv for a period = snapshot at the milestone year only | Cost_Inv for a period = **levelised average** over the whole period |
| Cannot reconstruct ObjZ from annual costs | ObjZ ≈ Σ(Reg_ACost × Time_NPV) to high accuracy |
| Misses investments in non-milestone years of long periods | Captures all investments across the full period |

**Bottom line:** Because this model uses `$SET ANNCOST LEV`, the values in charts are genuine period-average annual costs — they represent the entire period, not just a single year's snapshot.

---

## 6. Input Parameters (What Drives the Costs)

### NCAP_COST — Unit Investment Cost
The capital cost to build one unit of new capacity (£M/unit). This is the "overnight cost" for Case 2 processes. The model converts it to annual payments via CRF.

### NCAP_FOM — Fixed O&M Cost
Annual cost per unit of installed capacity (£M/unit/yr).

### ACT_COST — Variable/Activity Cost
Cost per unit of activity (£M/PJ or £M/unit-output).

### NCAP_DRATE — Hurdle Rate (Technology Discount Rate)
The technology-specific discount rate used **only** for annualising that process's investment. Reflects the investor's cost of capital or risk premium. Example: 10% for a household heat pump vs 3.5% for a utility-scale wind farm. **Not** the rate used for time-discounting in the objective function.

### G_DRATE — General (Social) Discount Rate
The economy-wide rate used for discounting all cost streams over time in the objective function. In UK TIMES: typically 3.5% declining.

### NCAP_ELIFE — Economic Life
The number of years over which the investment cost is amortised (spread into annual payments). Can differ from technical life.

### NCAP_TLIFE — Technical Life
How many years the process actually operates. Determines when capacity retires.

### NCAP_ILED — Investment Lead Time
Construction duration (years). For large projects (Case 2), interest accumulates during construction (IDC). A negative ILED means construction starts before the decision year.

### PRC_CAPACT — Capacity-to-Activity Ratio
Converts between capacity units and activity units. Example: 1 GW × 31.536 PJ/GW = activity in PJ.

---

## 7. The Annuity Formula

How Cost_Inv is computed from inputs. For each vintage:

$$Cost\_Inv_v = NCAP\_COST_v \times VAR\_Ncap_v \times CRF(drate, ELIFE) \times relevel \times IDC$$

### CRF — Capital Recovery Factor
Converts a lump-sum investment into an equivalent annual payment:

$$CRF_{mid}(r, L) = \frac{1 - (1+r)^{-1}}{1 - (1+r)^{-L}} \times (1+r)^{0.5}$$

where r = discount rate, L = economic life. The $(1+r)^{0.5}$ factor is the mid-year correction (this model uses `$SET MIDYEAR YES`).

### Re-levelisation (relevel)
When ELIFE ≠ TLIFE, adjusts the annuity from economic-life basis to technical-life basis at the social rate:

$$relevel = \frac{CRF(g, TLIFE)}{CRF(g, ELIFE)}$$

If ELIFE = TLIFE, relevel = 1 (no adjustment needed).

### IDC — Interest During Construction
For Case 2 processes (large projects with lead time), construction-phase interest is added:

$$IDC = (1 + g)^{|ILED|/2}$$

For Case 1 processes (small/progressive investments), IDC = 1 (already included in NCAP_COST).

---

## 8. OBLONG Period Handling

With `$SET OBLONG YES` (active in this model), TIMES synchronises capacity costs with process activity to avoid accounting distortions.

### Pay Fraction
When a process's payment window doesn't fill an entire period, the payment is prorated:

$$pay\_frac = \frac{1 - (1+g)^{-k}}{1 - (1+g)^{-D}}$$

where k = overlap years between the payment window and the period, D = period duration. Equals 1.0 when k ≥ D (payment covers the full period).

---

## 9. Investment Cases

TIMES classifies each investment into one of four cases based on project size and period length:

| Case | When | Construction pattern | IDC |
|---|---|---|---|
| **1a** | Small project, TLIFE ≥ D | Progressive build spread over period years | Already in NCAP_COST |
| **1b** | Small project, TLIFE < D | Repeated progressive builds | Already in NCAP_COST |
| **2a** | Large project, TLIFE ≥ D | Single lump build at B(t) | Computed at social rate |
| **2b** | Large project, TLIFE < D | Repeated lump builds | Computed at social rate |

"Large" means `|ILED| > min(D/10, TLIFE/10)`.

---

## 10. Classification Concepts (What the App Adds)

### Category
How the app groups processes by their role in the energy system:

| Category | Meaning | Examples |
|---|---|---|
| **end_use** | Consumes energy to deliver a final service | Heat pumps, cars, boilers, industrial furnaces |
| **supply** | Produces or transforms energy carriers | Power plants, refineries, gas import terminals |
| **infrastructure** | Transports/distributes energy between supply and demand | Electricity grid, gas network, H₂ pipelines |
| **other** | Unclassified or auxiliary | Dummy processes, allocation nodes |

### Sector
The demand sector that ultimately benefits from the cost:

Agriculture · CO2 Credits · Electricity · Engineered Removals · Industry · Multiple · Processing · Residential · Resources · Services · Transport · Waste · Other

### Cost Allocation
The app's central task: attributing supply and infrastructure source costs to the sectors that use their commodities, based on observed commodity-flow shares. Attribution does not create a direct cost on the receiving sector.

### Direct Process Cost
A cost displayed on the UK TIMES process where the VD result reports it. In Overview, Direct Energy Supply Costs, Direct End-use Technology Costs and Direct Power-system Costs retain this process-owned boundary.

### Cost Attribution
The proportional redistribution of eligible upstream source-process costs to fuel-using sectors. Attribution uses observed fuel-use shares and is an incidence view, not a new model total, a fuel price or a direct receiving-sector process cost.

### Flow-cost Attribution
The Fuel Cost Attribution view that redistributes eligible graph-derived supply-process `Cost_Flo` source rows to sectors using observed fuel shares.

### Full Supply-chain Attribution
The Fuel Cost Attribution view that redistributes eligible graph-derived supply-process `Cost_Inv`, `Cost_Fom`, `Cost_Act` and `Cost_Flo` source rows to sectors using observed fuel shares. It overlaps Flow-cost Attribution because it includes `Cost_Flo`; do not add their totals.

### Unit Cost by Final User
On Fuel Cost Attribution, one pool's net attributed final-use cost for a group of final users divided by that group's own use of the pool in the same year (£M/TWh = £/MWh), as a ratio of sums. It includes cost passed in from the pools used to make the chosen pool. Converters pass their share on and are excluded from both cost and use. Investment cost split by build-year users can reach a user with no use this year; that cost is shown with no unit cost (`Cost without reporting-year use`). It differs from the Direct Power-system £/MWh, which divides direct power-process cost by net electricity supplied. It is an average attributed cost, not a tariff, marginal price or LCOE.

---

## 11. Discounting vs. Undiscounted — Quick Reference

| Term | What it means | Where you see it |
|---|---|---|
| **Undiscounted** | Real cost in constant 2010 prices, no time-preference weighting | All charts and tables (£B/yr) |
| **Discounted** / **NPV** | Cost weighted by time preference — £1 today worth more than £1 in 2050 | ObjZ, Time_NPV, Reg_obj |
| **Present Value** | Same as discounted — the value today of a future cost stream | ObjZ metric |
| **Annualised** | A lump sum converted to an equivalent annual stream (via CRF) | Cost_Inv (always annualised) |
| **Levelised** | Period-average annual value (not a single year's snapshot) | All costs under ANNCOST LEV |

---

## 12. Units Used in the App

| Quantity | Units | Notes |
|---|---|---|
| Costs in charts | 2010 £B/yr or £M/yr | Annual rate, undiscounted, constant prices |
| ObjZ | £M (shown as £bn) | Discounted present value |
| Capacity (VAR_Cap, VAR_Ncap) | GW, Mt/yr, or model units | Depends on process |
| Activity (VAR_Act) | PJ/yr typically | Energy throughput |
| Discount rates | Decimal (0.035 = 3.5%) | Per year |
| Lives (ELIFE, TLIFE) | Years | Integer |
| Period duration D(t) | Years | Integer (typically 5 or 10) |

---

## 13. Summary: How to Read a Chart

When you see a bar for "Cost_Inv" at period 2035 showing £15 B/yr:

1. It is the **annual investment payment rate** for that period — not the overnight capex and not a period total.
2. It is **undiscounted** — real 2010 pounds, no time-preference weighting applied.
3. It is a **period-average** — thanks to ANNCOST LEV, it reflects all investments across the period, not just those at the milestone year.
4. It includes **all active vintages** — every past and new investment still making payments in that period contributes.
5. To get the period's total investment spend: multiply by D(t) (period duration).
6. To get this period's contribution to ObjZ: multiply by Time_NPV(t).

---
---

# Part II — Quick Reference Card

Condensed lookup tables for reviewing code and model output. Definitions for
every term used here are in Part I above; this part deliberately does not
redefine them.

---

## 14. Cost Elements in the Objective Function

| # | Component | Input Param | Result Attr | Calculation | Typical Uses |
|---|-----------|-------------|-------------|---|---|
| 1 | **Capital** | NCAP_COST | Cost_Inv | CRF × VAR_Ncap | Power plants, vehicles, buildings |
| 2 | **Fixed O&M** | NCAP_FOM | Cost_Fom | × VAR_CAP | Maintenance per capacity |
| 3 | **Variable O&M** | ACT_COST | Cost_Act | × VAR_ACT | Fuel handling, labor |
| 4 | **Flow/Commodity** | FLO_COST, COM_CSTNET, COM_CSTPRD | Cost_Flo, Cost_Com | × VAR_FLO | Import/export revenues, delivery costs |
| 5 | **Import/Export** | IRE_PRICE | (in Cost_Flo) | × trade flows | Trade prices (exogenous) |
| 6 | **Salvage Value** | Residual value formula | Cost_Salv | Lump-sum credit | Capacity beyond model horizon |

---

## 15. Critical Parameters for Cost Calculation

### Always needed

```
NCAP_COST      — Unit investment cost
NCAP_ELIFE     — Years to annualize investment over (default = TLIFE)
NCAP_TLIFE     — How long process operates (default = 10 years, G_TLIFE)
NCAP_ILED      — Construction lead-time (default = 0)
NCAP_DRATE     — Discount rate for CRF (default = G_DRATE)
NCAP_FOM       — Fixed O&M cost per unit capacity
ACT_COST       — Variable cost per unit activity
PRC_CAPACT     — Capacity-to-activity conversion
G_DRATE        — General (social) discount rate
```

### Sometimes needed

```
FLO_COST       — If process-specific flow costs not implicit in results
NCAP_START     — If restricting investment start years
IRE_PRICE      — If modeling exogenous trade
```

### Expected in VD results

```
Cost_Inv       — Annualized investment (£ per year)
Cost_Fom       — Fixed O&M (£ per year)
Cost_Act       — Variable activity (£ per year)
Cost_Flo       — Flow/commodity (£ per year)
Cost_Com       — Commodity-level (£ per year)
Cost_Salv      — Residual value (£ lump sum)
Reg_ACost      — Regional total (£ per year) — sum check
```

---

## 16. Investment Case Classification Grid

Determines how investment is spread across years. See Part I section 9 for the
narrative explanation.

|  | TLIFE + ILED ≥ D(t) (single investment) | TLIFE + ILED < D(t) (repeated investment) |
|---|---|---|
| **ILED ≤ min(D/10, TLIFE/10)** (small project) | Case 1.a (~85%) | Case 1.b (~8%) |
| **ILED > min(D/10, TLIFE/10)** (large project) | Case 2.a (~7%) | Case 2.b (<1%) |

Default: `ILED = 0` → always Case 1 (small project).
Typical: `TLIFE ≥ D(t)` → always sub-case (a) for 5-year periods.

| Case | Meaning | Example |
|------|---------|---------|
| **1.a** | Small, single | Cars (ILED=0, TLIFE=10, D=5) |
| **1.b** | Small, repeated | Electronics (ILED=0, TLIFE=2, D=5) |
| **2.a** | Large, single | Nuclear (ILED=3, TLIFE=40, D=5) |
| **2.b** | Large, repeated | Rare |

---

## 17. CRF Formulas by Method

- **Beginning-of-year:** `CRF = (1 - (1+r)^(-1)) / (1 - (1+r)^(-L))`
- **End-of-year:** `CRF = r / (1 - (1+r)^(-L))`
- **Mid-year (TIMES standard):** `CRF_mid = CRF_beg × (1+r)^0.5`

Example — 8% discount, 25-year economic life, mid-year:

```
CRF ≈ 0.0650 → Annualized payment = 0.0650 × NCAP_COST × VAR_Ncap
```

---

## 18. Reconciliation Identities

Sum of components equals the regional total, before discounting:

```
Cost_Inv + Cost_Fom + Cost_Act + Cost_Flo + Cost_Com + Cost_Salv
≈ Reg_ACost
```

Discounted form:

```
Discounted_NPV ≈ Σ(periods) Reg_ACost(period) × Time_NPV(period)
```

where `Time_NPV = (1 + G_DRATE)^(REFYR - period_year)`.

---

## 19. Common Mistakes to Avoid

| Mistake | Impact | Fix |
|---------|--------|-----|
| Confusing ELIFE with TLIFE | Wrong cost amortization | ELIFE = payment period, TLIFE = operational life |
| Using NCAP_DRATE as general discount | Inconsistent NPV | Reserve NCAP_DRATE for CRF only |
| Missing Time_NPV in discounting | Wrong objective value | Check the VD file has the Time_NPV attribute |
| Not accounting for investment case | Wrong cost timing | Classify correctly (usually Case 1.a) |
| Forgetting Reg_ACost is a sum check | Missed errors | Reconcile components regularly |
| Ignoring tax parameters | Incomplete policy costs | Check whether taxes/subsidies are defined in the model |
| Using the wrong CRF formula | 10–15% cost error | Use mid-year when `$SET MIDYEAR YES` |

---

## 20. Audit Checklist

Before trusting a cost allocation run:

- [ ] All six core parameters loaded: NCAP_COST, NCAP_ELIFE, NCAP_TLIFE, NCAP_ILED, NCAP_DRATE, G_DRATE
- [ ] All expected cost attributes present in the VD file
- [ ] `Time_NPV` present (needed for discounting)
- [ ] `Reg_ACost` present (needed for reconciliation)
- [ ] Investment case classification looks sane (~85% Case 1.a)
- [ ] CRF matches the TIMES formula (verify one example by hand)
- [ ] Reconciliation passes (`Cost_*` sum ≈ `Reg_ACost`)
- [ ] Discount rates documented (G_DRATE = ?, NCAP_DRATE = ?)

Reporting switches to check:

- `$SET MIDYEAR YES` → use mid-year CRF
- `$SET ANNCOST LEV` → levelized costs enabled
- `$SET OBLONG YES` → OBLONG treatment for periods
- `RPT_OPT('OBJ','1') <> 0` → extra cost reporting
- `$SET DAMAGE LP/NLP/NO` → damage cost handling

Questions worth asking when auditing:

1. **Which cost attributes are in my VD file?** Run `df['Attribute'].unique()` and filter for `Cost_*`.
2. **What parameters does the model actually use?** Search the DD files for NCAP_COST, NCAP_FOM, and friends.
3. **Are investment cases classified correctly?** Most should be Case 1.a; only large infrastructure such as nuclear should be 2.a.
4. **Do costs reconcile?** Sum the individual `Cost_*` attributes against `Reg_ACost` (tolerance ~1%).
5. **Is discounting applied correctly?** Verify `NPV = Σ(Reg_ACost × Time_NPV) ≈ ObjZ`.

---

## 21. One-Page Calculation Flow

```
INPUT PARAMETERS                   CALCULATION                 RESULT ATTRIBUTES
─────────────────                  ───────────                 ──────────────────

NCAP_COST ────┐
NCAP_ELIFE ──┼─→ CRF = (1-(1+r)^(-1)) / (1-(1+r)^(-L)) × MIDYEAR → Cost_Inv
NCAP_DRATE ──┘

NCAP_FOM × VAR_CAP ──────────────────────────────────────────────→ Cost_Fom

ACT_COST × VAR_ACT ──────────────────────────────────────────────→ Cost_Act

FLO_COST × VAR_FLO + COM_CSTNET/CSTPRD ──────────────────────────→ Cost_Flo, Cost_Com

Residual value of capacity beyond horizon ──────────────────────→ Cost_Salv


                    ↓ SUM ALL COMPONENTS ↓

                    Reg_ACost (per region/period)

                    ↓ APPLY DISCOUNTING ↓

G_DRATE × Time_NPV (Reg_ACost) ─────────────────────────────→ ObjZ (Net Present Value)
```

---

## 22. Where to Read Further

**Understanding the parameters**

- [Complete Guide to Cost Handling](domain-notes/Complete%20Guide%20to%20Cost%20Handling.txt) (sections 1–3)
- [Investment case classification](domain-notes/Investment%20case%20classification.txt)

**Implementation**

- `tools/cost_allocation/cost_backcalc.py` — CRF, investment cases, payment timing
- `tools/cost_allocation/dd_inputs.py` — parameter loading
- `tools/cost_allocation/ingest.py` — VD attribute handling

