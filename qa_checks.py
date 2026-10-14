"""Run-level QA checks over one prepared run — pure, no Streamlit, no I/O.

Each check returns a frame with a ``status`` column of ``Pass``, ``Fail`` or
``Information``. The checks only compare totals the app already produces; they
never change a cost, share, or tolerance used by the calculations themselves.
See ``Documentation/QA_PLAN.md`` for how they are used.
"""
from __future__ import annotations

import pandas as pd

from cost_allocation.helpers import ALL_COST_ATTRS
from cost_allocation.resource_allocation import (
    CORE_COST_ATTRIBUTES,
    UPSTREAM_SUPPLY_VIEW,
    attribute_view,
)

PASS = "Pass"
FAIL = "Fail"
INFO = "Information"

# QA comparison tolerance (£m). Totals are compared after summing in a
# different order, so allow floating-point noise and nothing more.
ABS_TOLERANCE_M = 1e-6
REL_TOLERANCE = 1e-9

SOURCE_TOTAL_ATTRIBUTES = (*ALL_COST_ATTRS, "ObjZ")
_NO_PERIOD = "(none)"


def _within(difference: float, scale: float) -> bool:
    return abs(difference) <= max(ABS_TOLERANCE_M, REL_TOLERANCE * abs(scale))


def single_attribute_views() -> tuple[str, ...]:
    """One allocation view per core cost attribute."""
    return tuple(attribute_view([attribute]) for attribute in CORE_COST_ATTRIBUTES)


def _attribute_period_totals(frame: pd.DataFrame, prefix: str) -> pd.DataFrame:
    rows = frame[frame["Attribute"].isin(SOURCE_TOTAL_ATTRIBUTES)].copy()
    period = pd.to_numeric(rows["Period"], errors="coerce").astype("Int64")
    rows["period"] = period.astype("string").fillna(_NO_PERIOD)
    rows["_value"] = pd.to_numeric(rows["Value"], errors="coerce")
    return rows.groupby(["Attribute", "period"]).agg(
        **{f"{prefix}_value_m": ("_value", "sum"), f"{prefix}_rows": ("_value", "size")}
    )


def source_totals_check(
    vd_df: pd.DataFrame, costs: pd.DataFrame, scenario: str,
) -> pd.DataFrame:
    """Compare raw VD cost totals with the app's cost table, by attribute and period.

    Every cost row read from the source must reach the cost table exactly
    once: equal row counts and equal totals.
    """
    vd = vd_df[vd_df["Scenario"].eq(scenario)]
    out = _attribute_period_totals(vd, "vd").join(
        _attribute_period_totals(costs, "app"), how="outer"
    ).reset_index().rename(columns={"Attribute": "attribute"})
    for column in ("vd_rows", "app_rows"):
        out[column] = out[column].fillna(0).astype(int)
    for column in ("vd_value_m", "app_value_m"):
        out[column] = out[column].fillna(0.0)
    out["difference_m"] = out["app_value_m"] - out["vd_value_m"]
    out["status"] = [
        PASS if rows_vd == rows_app and _within(diff, vd) else FAIL
        for rows_vd, rows_app, diff, vd in zip(
            out["vd_rows"], out["app_rows"], out["difference_m"], out["vd_value_m"]
        )
    ]
    return out.sort_values(["attribute", "period"]).reset_index(drop=True)


def allocation_balance_check(
    view_summary: pd.DataFrame, pool_summary: pd.DataFrame,
) -> pd.DataFrame:
    """Check that no cost is lost or created inside the allocation.

    Per view: source = assigned + unassigned, and assigned = allocated to
    final use + unallocated. Per view, period and pool: what enters the pool
    equals what is booked, passed on, or left unallocated.
    """
    rows: list[dict] = []

    def add(level: str, view: str, period, pool: str, check: str, actual: float, expected: float):
        difference = actual - expected
        rows.append({
            "level": level, "view": view, "period": period, "resource_pool": pool,
            "check": check, "actual_m": actual, "expected_m": expected,
            "difference_m": difference,
            "status": PASS if _within(difference, expected) else FAIL,
        })

    for record in view_summary.to_dict("records"):
        add(
            "view", record["view"], None, "", "Source = assigned + unassigned",
            float(record["assigned_cost_m"]) + float(record["unassigned_source_cost_m"]),
            float(record["source_cost_m"]),
        )
        allocated = pd.to_numeric(pd.Series([record.get("allocated_cost_m")]), errors="coerce").iloc[0]
        if pd.notna(allocated):
            add(
                "view", record["view"], None, "",
                "Assigned = allocated + unallocated",
                float(allocated)
                + float(record.get("denominator_unallocated_cost_m") or 0.0)
                + float(record.get("cascade_unallocated_cost_m") or 0.0),
                float(record["assigned_cost_m"]),
            )
    for record in pool_summary.to_dict("records"):
        add(
            "pool", record["view"], int(record["period"]), record["resource_pool"],
            "Pool in = booked + passed on + unallocated",
            float(record["allocated_cost_m"]) + float(record["passed_on_cost_m"])
            + float(record["denominator_unallocated_cost_m"])
            + float(record["cascade_unallocated_cost_m"]),
            float(record["source_cost_m"]) + float(record["passed_in_cost_m"]),
        )
    return pd.DataFrame(rows, columns=[
        "level", "view", "period", "resource_pool", "check",
        "actual_m", "expected_m", "difference_m", "status",
    ])


def view_additivity_check(allocations: pd.DataFrame) -> pd.DataFrame:
    """Check the single-attribute views add up to the full supply-chain view.

    Compared per period and final-use sector, so a cost that lands in the
    wrong sector for one attribute is caught even if the grand total agrees.
    """
    singles = single_attribute_views()
    columns = ["period", "sector", "full_view_m", "sum_of_attributes_m", "difference_m", "status"]
    if allocations.empty:
        return pd.DataFrame(columns=columns)
    keys = ["period", "sector"]
    full = allocations[allocations["view"].eq(UPSTREAM_SUPPLY_VIEW)].groupby(keys)[
        "allocated_cost_m"
    ].sum().rename("full_view_m")
    parts = allocations[allocations["view"].isin(singles)].groupby(keys)[
        "allocated_cost_m"
    ].sum().rename("sum_of_attributes_m")
    out = pd.concat([full, parts], axis=1).fillna(0.0).reset_index()
    out["difference_m"] = out["sum_of_attributes_m"] - out["full_view_m"]
    out["status"] = [
        PASS if _within(diff, full) else FAIL
        for diff, full in zip(out["difference_m"], out["full_view_m"])
    ]
    return out.loc[:, columns]


def allocation_scope_summary(
    costs: pd.DataFrame, view_summary: pd.DataFrame, periods,
) -> pd.DataFrame:
    """Report how much core cost enters the full supply-chain allocation.

    Costs of processes outside the VDT supply scope (for example end-use
    technologies) are not allocated through fuel pools; this states the size
    of that remainder so it can be reviewed. Informational, never a failure.
    """
    selected = {int(period) for period in periods}
    rows = costs[costs["Attribute"].isin(CORE_COST_ATTRIBUTES)].copy()
    period = pd.to_numeric(rows["Period"], errors="coerce")
    rows = rows[period.isin(selected)]
    total = float(pd.to_numeric(rows["Value"], errors="coerce").sum())
    full = view_summary[view_summary["view"].eq(UPSTREAM_SUPPLY_VIEW)]
    in_scope = float(full["source_cost_m"].sum()) if not full.empty else 0.0
    return pd.DataFrame([{
        "core_attributes": ", ".join(CORE_COST_ATTRIBUTES),
        "cost_table_total_m": total,
        "in_supply_chain_allocation_m": in_scope,
        "outside_allocation_m": total - in_scope,
        "outside_share_pct": (total - in_scope) / total * 100.0 if total else 0.0,
        "status": INFO,
    }])


def reconciliation_status_counts(summary_df: pd.DataFrame) -> pd.DataFrame:
    """Count Cost_Inv reconciliation MATCH / MISMATCH processes by investment case."""
    columns = ["investment_case", "reason", "processes"]
    if summary_df.empty:
        return pd.DataFrame(columns=columns)
    return summary_df.groupby(["investment_case", "reason"]).size().rename(
        "processes"
    ).reset_index().loc[:, columns]
