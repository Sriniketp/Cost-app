# QA Plan

How to show that a run's numbers are right. You need it before results are
shared, and again after any change to the calculations.

The unit tests prove the code does what it was designed to do, using small
made-up data. They cannot prove the design is right or that a real run comes
out right. This plan supplies that evidence.

Work through the steps in order. Each one produces a file or a signature for
the **evidence pack** (step 10).

| Step | Question it answers | Who | Pass criterion |
|---|---|---|---|
| 1 | Does the automated report pass? | Analyst | `OVERALL: PASS` |
| 2 | Does an independent query of the database give the same totals? | Analyst (not the developer, ideally) | Every row agrees |
| 3 | Is any cost lost inside the allocation? | Analyst | All Pass; in-app checks Pass |
| 4 | Do the Cost_Inv mismatches each have an explanation? | Analyst + modeller | No blank explanations |
| 5 | Can numbers be traced by hand? | Independent reviewer | 3 traces agree |
| 6 | Do the results look sensible? | Modeller | Deviations explained |
| 7 | Is the allocation method appropriate? | Domain lead | Signed off |
| 8 | Does the app work end to end? | Anyone | Checklist complete |
| 9 | Do the checks catch errors? | Developer | Demonstrated |
| 10 | Evidence pack | Analyst | Filed |

Run QA on **at least two runs**: the run the results will be published from,
and one run that differs from it in structure, such as a different scenario
family or a run with hydrogen or CCS active.

---

## Step 1 — Run the automated QA report

From the repository root, in the app's virtual environment (the one
`run_app.cmd` uses), on a machine with SQL and uktm-dds access:

```powershell
& "$env:LOCALAPPDATA\cost-allocation-app\.venv\Scripts\python.exe" tools\qa_report.py <RUN_ID> --out "$env:USERPROFILE\Documents\cost-allocation-qa"
```

Write the output outside the repository. The report loads the run exactly as
the dashboard does: SQL VD rows, DD and VDT files from the run's commit, and
live labels. It writes these files to `<out>\<RUN_ID>\`:

| File | Contents | Pass/Fail? |
|---|---|---|
| `00_qa_summary.txt` | Run identity, VD row count and hash, periods, native price year, missing DD files, and one line per check | — |
| `01_source_totals.csv` | Row count and total per cost attribute × period: raw VD rows (`vd_*`) against the app's cost table (`app_*`) | Yes: same rows, same total |
| `02_allocation_balance.csv` | Per view: source = assigned + unassigned, and assigned = allocated + unallocated. Per pool and period: in = booked + passed on + unallocated | Yes |
| `03_view_additivity.csv` | Per period × sector: Cost_Inv + Cost_Fom + Cost_Act + Cost_Flo views summed against the full supply-chain view | Yes |
| `04_allocation_scope.csv` | How much core cost enters the supply-chain allocation, and how much sits outside it (end-use technologies) | Information |
| `05_sector_totals.csv` | Full supply-chain cost by period × final-use sector | Information (used in step 6) |
| `05_allocation_diagnostics.csv` | Allocation diagnostics (ambiguous outputs, unmapped commodities, …) | Review |
| `06_reconciliation_summary.csv`, `06_reconciliation_counts.csv` | Cost_Inv rebuilt from DD inputs against VD, per process; MATCH/MISMATCH counts by investment case | Information |
| `07_reconciliation_mismatches.csv` | Every MISMATCH process with the app's diagnosed cause, plus blank `agreed_explanation` and `reviewer` columns | Step 4 |

Pass/Fail comparisons allow floating-point noise only: $|\Delta| \le \max(10^{-6}, 10^{-9}\,|x|)$ £m. They are not
model tolerances. The 5% Cost_Inv MATCH threshold is unchanged and lives in
`cost_backcalc.py`.

Use `--skip-reconciliation` for a quick re-run. The exit code is `0` when every
Pass/Fail check passes and `1` otherwise.

If `00_qa_summary.txt` lists **missing DD files** or `VDT: NOT FOUND`, stop
and resolve that first. Steps 3–4 are not meaningful without them.

---

## Step 2 — Independent database totals

The report's `vd_*` columns come from the app's own SQL reader. Re-derive them
in SQL Server Management Studio against `UK-TIMES-WKG`, so the comparison does
not depend on app code.

Use SQL Server Management Studio (SSMS), connected to server
`VMSPDB06.cbas.cloud`, database `UK-TIMES-WKG`, with Windows Authentication.

### 2a. Find the procedure's column order

In a **New Query** window, run (F5):

```sql
EXEC usr.Get_RawVD @RunList = N'<RUN_ID>';
```

Write down the ten column headers from left to right. Ignore the rows.

`INSERT ... EXEC` in 2b fills columns by **position**, not by name, so the
`CREATE TABLE` must list the columns in this order.
(`sp_describe_first_result_set` cannot be used: the procedure uses a temp
table and fails with error 11526.)

### 2b. Run the whole check as one script

The script is saved as
[tools/qa/step2_source_totals.sql](../tools/qa/step2_source_totals.sql). In
SSMS use **File → Open → File…**, change the run ID on the line marked
`CHANGE THIS`, and press F5. The copy below is for reference.

1. Reorder the ten lines inside `CREATE TABLE #vd ( ... )` to match 2a.
   Every line ends with a comma except the last one.
2. Replace `<RUN_ID>` in **both** places.
3. Press F5 with nothing highlighted. Always run the whole script: `#vd` only
   exists inside this window's connection.

```sql
SET NOCOUNT ON;
DROP TABLE IF EXISTS #vd;
CREATE TABLE #vd (               -- reorder these 10 lines to match 2a
    run_id          nvarchar(4000),
    [year]          nvarchar(4000),
    attribute       nvarchar(4000),
    commodity       nvarchar(4000),
    process         nvarchar(4000),
    pv              float,       -- keep as float: text would lose digits
    timeslice       nvarchar(4000),
    region          nvarchar(4000),
    vintage         nvarchar(4000),
    user_constraint nvarchar(4000)
);
INSERT INTO #vd EXEC usr.Get_RawVD @RunList = N'<RUN_ID>';

-- Grid 1: column-order check. All three "ok_" columns must say YES.
SELECT COUNT(*) AS total_rows,
       CASE WHEN SUM(CASE WHEN run_id = N'<RUN_ID>' THEN 1 ELSE 0 END) = COUNT(*)
            THEN 'YES' ELSE 'NO' END AS ok_run_id,
       CASE WHEN SUM(CASE WHEN attribute = 'Cost_Inv' THEN 1 ELSE 0 END) > 0
            THEN 'YES' ELSE 'NO' END AS ok_attribute,
       CASE WHEN SUM(CASE WHEN TRY_CAST([year] AS int) BETWEEN 2000 AND 2300 THEN 1 ELSE 0 END)
                 > COUNT(*) / 2
            THEN 'YES' ELSE 'NO' END AS ok_year
FROM #vd;

-- Grid 2: a few rows, to eyeball that each value sits under the right header.
SELECT TOP 5 * FROM #vd WHERE attribute = 'Cost_Inv';

-- Grid 3: totals by attribute and period. Same layout as 01_source_totals.csv.
WITH c AS (
    SELECT LTRIM(RTRIM(attribute)) AS attribute,
           CASE WHEN [year] IS NULL
                  OR LTRIM(RTRIM([year])) IN ('', '-')
                  OR LOWER(LTRIM(RTRIM([year]))) IN ('none', 'all_years')
                THEN '(none)' ELSE LTRIM(RTRIM([year])) END AS period,
           pv
    FROM #vd
    WHERE LTRIM(RTRIM(attribute)) IN ('Cost_Inv','Cost_Fom','Cost_Act','Cost_Flo',
                                      'Cost_Com','Cost_Salv','Cost_ire','ObjZ')
)
SELECT attribute, period, SUM(pv) AS vd_value_m, COUNT(*) AS vd_rows
FROM c
GROUP BY attribute, period
ORDER BY attribute COLLATE Latin1_General_BIN, period COLLATE Latin1_General_BIN;

-- Grid 4: grand totals per attribute (for the evidence pack).
SELECT attribute, COUNT(*) AS vd_rows, SUM(pv) AS total_m
FROM #vd
WHERE attribute LIKE 'Cost[_]%' OR attribute = 'ObjZ'
GROUP BY attribute
ORDER BY attribute;

-- Grid 5: exact duplicate rows. Information only; the app keeps duplicates.
SELECT attribute, process, commodity, [year], region, vintage, timeslice,
       user_constraint, pv, COUNT(*) AS copies
FROM #vd
GROUP BY attribute, process, commodity, [year], region, vintage, timeslice,
         user_constraint, pv
HAVING COUNT(*) > 1
ORDER BY copies DESC;
```

If the **Messages** tab shows `Error converting data type nvarchar to float`,
or Grid 1 shows a `NO`, the column order is wrong. Go back to 2a.

### 2c. Compare with the app

- Grid 1 `total_rows` must equal "VD rows read" in `00_qa_summary.txt`.
- Grid 3 must match `01_source_totals.csv` row for row:
  1. Open `01_source_totals.csv` in Excel. Its columns are A `attribute`,
     B `period`, C `vd_value_m`, D `vd_rows`, …, H `status`.
  2. In SSMS, right-click Grid 3 → **Select All**, then right-click →
     **Copy with Headers**. Click cell **J1** in Excel and paste.
  3. In **N2**, enter the formula below and fill it down to the last row:
     `=IF(AND(A2=J2, B2&""=K2&"", D2=M2, ABS(C2-L2)<0.000001), "OK", "CHECK")`
  4. Every row must say `OK`, and both tables must have the same number of
     rows.

A `CHECK` row means the app's reader and the database disagree. Raise it under
the adjudication rules in
[VD_SQL_EQUIVALENCE_CONTRACT.md](VD_SQL_EQUIVALENCE_CONTRACT.md) §8. Save the
Excel file in the evidence pack.

---

## Step 3 — Conservation inside the allocation

1. `02_allocation_balance.csv` and `03_view_additivity.csv` must contain only
   `Pass`. Any `Fail` is a bug. Record the view, period and pool, and do not
   publish.
2. Open `04_allocation_scope.csv`. Record the share of core cost that sits
   **outside** the supply-chain allocation. A reviewer must agree that this
   cost (end-use technology costs) belongs outside, as part of step 7.
3. Review `05_allocation_diagnostics.csv`. Every diagnostic type should be
   understood. Unmapped commodities and ambiguous outputs are the usual ones.
4. In the app, load the run and check these panels:
   - **Fuel Cost Attribution**: the *checks and workbook* expander for the
     run.
   - **Methodology**: *Worked proof — Hydrogen*, then the *QA checks* table,
     for at least two periods.

   Every check should read `Pass`.
5. Export the workbook and confirm that sheet **90 Checks** has no failures.
6. Sidebar **Diagnostics → Reconciliation**: under *System Cost
   Reconciliation (ObjZ)*, record how far the rebuilt total is from ObjZ.
   This confirms the VD cost rows are complete relative to the objective.

---

## Step 4 — Explain every Cost_Inv mismatch

`07_reconciliation_mismatches.csv` lists every process whose Cost_Inv, rebuilt
from DD inputs, differs from TIMES by more than 5%. Its `dominant_cause` and
`detail` columns hold the app's own diagnosis.

For each row:

1. Open sidebar **Diagnostics → Reconciliation** and **Mismatch Diagnosis**
   for that process. Compare the DD inputs (NCAP_COST, NCAP_DRATE, ELIFE,
   TLIFE, ILED) with `python tools/tabulate_dd_inputs.py --run-id <RUN_ID>`.
2. Fill `agreed_explanation` with one of the following, plus a sentence:
   - `Data gap`: an input is missing in DD.
   - `Known model gap`: for example IDC or existing capacity, named
     explicitly.
   - `TIMES behaviour`: evidence required, such as a TIMES documentation
     reference.
   - `Unexplained`: leave it open and say so.
3. Fill `reviewer`.

Mismatches do **not** change the allocated numbers. Allocation uses the VD
Cost_Inv as reported by TIMES. They measure how well the app *explains* the
investment cost. Report the MATCH percentage and the count of
`Unexplained`. Never loosen the 5% threshold to improve the figure.

---

## Step 5 — Trace three numbers by hand

Ideally someone who did not build the app does this. Set the sidebar
**Costs displayed in (year) prices** to the run's native price year from
`00_qa_summary.txt` (normally 2010), so screens match SQL.

Pick the processes with this query. It lists the largest cost rows in one
period.

```sql
SELECT TOP 20 attribute, process, commodity, vintage, SUM(pv) AS total_m
FROM #vd
WHERE attribute IN ('Cost_Inv','Cost_Fom','Cost_Act','Cost_Flo')
  AND [year] = '2030'
GROUP BY attribute, process, commodity, vintage
ORDER BY ABS(SUM(pv)) DESC;
```

### Trace A — one process's costs (VD → app)

```sql
SELECT attribute, commodity, vintage, timeslice, SUM(pv) AS total_m
FROM #vd
WHERE process = N'<PROCESS>' AND [year] = '2030'
  AND attribute LIKE 'Cost[_]%'
GROUP BY attribute, commodity, vintage, timeslice
ORDER BY attribute, vintage;
```

Compare the result with **Technology Drill-Down** for that process and period.
Each attribute total must agree.

### Trace B — Cost_Inv from DD inputs

For one MATCH process with a single vintage, compute it by hand:

$$
\text{Cost\_Inv} = \text{NCAP\_COST} \times \text{VAR\_Ncap} \times
\text{CRF}(r_{\text{hurdle}}, \text{ELIFE}) \times
\frac{\text{CRF}(g, \text{TLIFE})}{\text{CRF}(g, \text{ELIFE})}
\times \text{pay fraction}
$$

Take the inputs from `tools/tabulate_dd_inputs.py --run-id <RUN_ID>`. Take
VAR_Ncap from this query:

```sql
SELECT attribute, vintage, [year], SUM(pv) AS value
FROM #vd
WHERE process = N'<PROCESS>' AND attribute IN ('VAR_Ncap','VAR_Cap','Cost_Inv')
GROUP BY attribute, vintage, [year]
ORDER BY attribute, [year], vintage;
```

The hand figure, the Diagnostics → Reconciliation figure and the VD Cost_Inv should
agree within the stated divergence. See
[ARCHITECTURE.md](ARCHITECTURE.md) for the full formula.

### Trace C — one fuel cost to final-use sectors

Pick a fuel pool such as Natural gas, and a period. List who produces and who
consumes the fuel:

```sql
SELECT attribute, process, SUM(pv) AS quantity
FROM #vd
WHERE commodity = N'<COMMODITY_CODE>' AND [year] = '2030'
  AND attribute IN ('VAR_FOut','VAR_FIn')
GROUP BY attribute, process
ORDER BY attribute, quantity DESC;
```

1. Sum the supply-process Cost_Flo + Cost_Act + Cost_Inv + Cost_Fom for the
   producers (query from Trace A). This is the pool's source cost.
2. Each consumer's share is its `VAR_FIn` divided by the total `VAR_FIn` for
   the commodity, over the processes the app counts. Compare with workbook
   sheet **10 Raw fuel use**.
3. Source cost × share gives the cost booked to the consumer's sector, before
   any cascade (for example gas burnt in power passes on to electricity
   users). Compare with workbook sheets **11 Raw source costs** and
   **12 Raw pool summary**, and with the **Resource & Fuel Pathways** Sankey.

Record each trace as figures, screenshots and the agreed difference.

---

## Step 6 — Do the results look sensible?

Using `05_sector_totals.csv` and the Overview tab:

- Divide sector fuel cost by the sector's TWh to get £/MWh by sector and fuel.
  Compare with published retail and wholesale prices for the price year.
  Order of magnitude and ranking should be plausible, for example industry
  below domestic.
- Compare the final-use shares of each fuel with DUKES for the base years.
- Compare the trend across periods with the scenario narrative. Large step
  changes should line up with known policy or technology switches.

Record each deviation with an explanation or an open question.

---

## Step 7 — Method sign-off

A domain lead reads the method in [ARCHITECTURE.md](ARCHITECTURE.md) (fuel
cost attribution) and signs off on these points:

- The supply scope (VDT-derived), and that costs outside it are excluded from
  pools (step 3.2).
- That allocation follows physical flows: consumer TWh shares, with cascades
  through conversion such as gas to power to electricity users.
- How credits and negative costs are treated (signed, netted).
- How "Unallocated" cost is treated and reported.
- That Cost_Com and policy cost are shown separately.
- The [resource pool definitions](../tools/lookup_tables/resource_pools.csv).

This is a judgement call, so it needs a named person and date.

---

## Step 8 — End-to-end walkthrough

On the published run:

- [ ] Load one run, then add a second run, from **Run Selector**.
- [ ] Visit every tab and both sidebar Diagnostics pages. Note errors,
      warnings and empty charts.
- [ ] Change the period range and the price base year. Totals rescale; nothing
      errors.
- [ ] Export the workbook and open every sheet.
- [ ] Clear the runs and reload. The results are the same as the first load.

---

## Step 9 — Show that the checks catch errors

A check that never fails proves nothing. Show reviewers the negative tests in
`tools/cost_allocation/tests/test_qa_checks.py`. They duplicate, drop and alter
a cost row, lose cost in a pool, and move an attribute's cost to the wrong
sector, and each check fails as expected:

```powershell
& "$env:LOCALAPPDATA\cost-allocation-app\.venv\Scripts\python.exe" -m pytest tools/cost_allocation/tests/test_qa_checks.py -v
```

Also record that the full suite passes (`pytest`) and the number of tests, with
the git commit of the app.

---

## Step 10 — Evidence pack

One folder per QA'd run, containing the `qa_report` output, the step 2 query
results, the filled mismatch CSV, the trace notes and this summary:

| Claim | Evidence | Result | Checked by | Date |
|---|---|---|---|---|
| App reads every VD row once | `01_source_totals.csv` + step 2 SQL | | | |
| No cost lost or created in allocation | `02`, `03`, in-app checks, workbook *90 Checks* | | | |
| Share of cost outside the allocation is understood | `04_allocation_scope.csv` | __ % | | |
| Cost_Inv is explained from DD inputs | `06`/`07`: __/__ MATCH, __ unexplained | | | |
| Numbers trace by hand | Traces A, B, C | | | |
| Results are plausible | Step 6 notes | | | |
| Method is appropriate | Step 7 sign-off | | | |
| App works end to end | Step 8 checklist | | | |
| Checks detect errors | Step 9 test output | | | |
| Known limitations | Listed, with likely size | | | |

Record the app git commit, the run ID, the VD row hash from
`00_qa_summary.txt` and the resource-pool file version. The pack is then tied
to exactly what was checked.

### After a code change

Re-run step 1 on the same run(s) into a new folder and compare it with the
signed-off output:

```powershell
$old = "$env:USERPROFILE\Documents\cost-allocation-qa-signed\<RUN_ID>"
$new = "$env:USERPROFILE\Documents\cost-allocation-qa\<RUN_ID>"
Get-ChildItem $old -Filter *.csv | ForEach-Object {
    $diff = Compare-Object (Get-Content $_.FullName) (Get-Content (Join-Path $new $_.Name))
    "{0}: {1} differing lines" -f $_.Name, ($diff | Measure-Object).Count
}
```

Any difference in `01`–`05` must be explained by the change being made.
