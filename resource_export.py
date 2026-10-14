"""Prepare and serialize the reference-style Resource & Fuel Costs workbook.

The workbook is generated only from the selected run's prepared allocation
result and cost rows.  It never reads the reference CSV or XLSX files at
runtime.  Annual values are formed by interpolating primitive TWh and cost
series first, then recalculating every allocation share and derived total.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

import pandas as pd

from cost_allocation.resource_allocation import (
    CORE_COST_ATTRIBUTES,
    RESOURCE_FLOW_VIEW,
    ResourceAllocationResult,
    fuel_pathway_combinations as _fuel_pathway_combinations,
    prepare_fuel_pathways_batch,
    prepare_supply_chain_cost_pathway_links,
)
from cost_allocation.resource_rules import DEMAND_SERVICE_POOLS


EXPORT_YEARS = tuple(range(2025, 2051))
EXPORT_SECTORS = (
    "Agriculture",
    "Buildings",
    "CCS Storage",
    "DAS",
    "Removals",
    "Fuel Supply",
    "Hydrogen Production",
    "IAS",
    "Industry",
    "Power",
    "Surface Transport",
)
TWH_FUELS = (
    "Synthetic fuel",
    "Biomass & biofuels",
    "Biomethane",
    "Hydrogen",
    "Electricity",
    "Natural gas",
    "Oil",
)
SOURCE_COST_FUELS = (
    "Ammonia & synthetics",
    "Biomass & biofuels",
    "Coal",
    "Electricity",
    "Hydrogen",
    "Natural gas",
    "Oil",
    "Renewable resource",
    "Uranium",
)
RESOURCE_COST_ROWS = (
    "Synthetic fuel",
    "Biomass & biofuels",
    "Electricity (distributed)",
    "Hydrogen",
    "Natural gas",
    "Oil",
)
FUELEX_ROWS = (
    "Fuelex - alt",
    "Fuelex - alt + elec",
    "Fuelex - f/fuels",
    "Net Fuel Costs",
    "Net Fuel Costs + elec",
)

_POOL_TO_SOURCE_FUEL = {
    "Synthetic fuels": "Ammonia & synthetics",
    "Manufactured fuels": "Ammonia & synthetics",
    "Bioenergy and waste": "Biomass & biofuels",
    "Coal": "Coal",
    "Electricity": "Electricity",
    "Hydrogen": "Hydrogen",
    "Natural gas": "Natural gas",
    "Primary oil": "Oil",
    "Petroleum products": "Oil",
    "Oil and petroleum products": "Oil",  # Backward-compatible cached-result export.
    "Renewable resources": "Renewable resource",
    "Nuclear fuels": "Uranium",
}

REVIEWED_NON_REFERENCE_POOLS = frozenset({
    "CHP",
    "Cooling",
    "Cooling conservation",
    "District heating",
    "Drying",
    "High temperature heat",
    "Industry byproducts",
    "Industry other",
    "Low temperature heat",
    "Motor drive",
    "Non-heat appliance demand conservation",
    "Refrigeration",
    "Space heating",
    "Space heating and hot water",
    "Space heating and hot water conservation",
    "Steam",
    "Waste heat",
})


@dataclass(frozen=True)
class ResourceFuelExport:
    """Prepared tables for a single reference-layout workbook.

    The wide tables carry the finished figures.  The raw and milestone frames
    carry the primitives those figures are built from, so the workbook writer
    can lay the calculation out as live formulas instead of pasted values.
    """

    run_id: str
    years: tuple[int, ...]
    twh: pd.DataFrame
    source_costs_m: pd.DataFrame
    electricity_costs_bn: pd.DataFrame
    resource_costs_m: pd.DataFrame
    fuelex_bn: pd.DataFrame
    checks: pd.DataFrame
    raw_fuel_use: pd.DataFrame = field(default_factory=pd.DataFrame)
    raw_source_costs: pd.DataFrame = field(default_factory=pd.DataFrame)
    raw_power_costs: pd.DataFrame = field(default_factory=pd.DataFrame)
    milestone_fuel_use: pd.DataFrame = field(default_factory=pd.DataFrame)
    milestone_source_costs: pd.DataFrame = field(default_factory=pd.DataFrame)
    milestone_power_costs: pd.DataFrame = field(default_factory=pd.DataFrame)
    milestone_years: tuple[int, ...] = ()
    electricity_base_years: tuple[int, ...] = ()
    price_year: int | None = None


def _empty_table(index_columns: list[str], years: Iterable[int]) -> pd.DataFrame:
    return pd.DataFrame(columns=[*index_columns, *years])


def _normalise_text(values: pd.Series) -> pd.Series:
    return values.fillna("").astype(str).str.strip()


def _reference_fuel(row: pd.Series) -> str:
    """Map current label metadata to the narrowly defined reference categories."""
    pool = str(row.get("resource_pool", "") or "").strip()
    name = " ".join(str(row.get(column, "") or "") for column in (
        "commodity_name", "commodity_subgroup", "commodity_group",
    )).casefold()
    if pool == "Bioenergy and waste":
        return "Biomethane" if "biomethane" in name else "Biomass & biofuels"
    return {
        "Synthetic fuels": "Synthetic fuel",
        "Manufactured fuels": "Synthetic fuel",
        "Hydrogen": "Hydrogen",
        "Electricity": "Electricity",
        "Natural gas": "Natural gas",
        "Primary oil": "Oil",
        "Petroleum products": "Oil",
        "Oil and petroleum products": "Oil",  # Backward-compatible cached-result export.
    }.get(pool, "")


def _annualise_primitives(
    rows: pd.DataFrame,
    group_columns: list[str],
    value_column: str,
    years: tuple[int, ...],
) -> pd.DataFrame:
    """Interpolate each primitive series without extrapolating beyond evidence."""
    columns = [*group_columns, "year", value_column]
    if rows.empty:
        return pd.DataFrame(columns=columns)
    primitives = rows.loc[:, [*group_columns, "period", value_column]].copy()
    primitives["period"] = pd.to_numeric(primitives["period"], errors="coerce")
    primitives[value_column] = pd.to_numeric(primitives[value_column], errors="coerce")
    primitives = primitives.dropna(subset=["period", value_column])
    primitives["period"] = primitives["period"].astype(int)
    primitives = primitives.groupby([*group_columns, "period"], as_index=False)[value_column].sum()
    parts: list[pd.DataFrame] = []
    for key, group in primitives.groupby(group_columns, dropna=False, sort=False):
        group = group.sort_values("period").drop_duplicates("period", keep="last")
        series = group.set_index("period")[value_column]
        covered = [year for year in years if series.index.min() <= year <= series.index.max()]
        if not covered:
            continue
        annual = series.reindex(sorted(set(series.index).union(covered))).interpolate(
            method="linear", limit_area="inside"
        ).reindex(covered)
        part = pd.DataFrame({"year": annual.index, value_column: annual.values})
        values = key if isinstance(key, tuple) else (key,)
        for column, value in zip(group_columns, values):
            part[column] = value
        parts.append(part.loc[:, columns])
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=columns)


def _wide(rows: pd.DataFrame, index_columns: list[str], value_column: str, years: tuple[int, ...]) -> pd.DataFrame:
    if rows.empty:
        return _empty_table(index_columns, years)
    result = rows.pivot_table(
        index=index_columns, columns="year", values=value_column, aggfunc="sum"
    ).reindex(columns=years)
    return result.reset_index()


def _power_cost_rows(costs: pd.DataFrame, power_processes: Iterable[str]) -> pd.DataFrame:
    """Return the individual core cost rows on validated Power processes."""
    columns = ["period", "process", "attribute", "power_core_cost_m"]
    required = {"Attribute", "Period", "Process", "Value"}
    if costs is None or not required.issubset(costs.columns):
        return pd.DataFrame(columns=columns)
    processes = {str(value).strip().upper() for value in power_processes if str(value).strip()}
    if not processes:
        return pd.DataFrame(columns=columns)
    rows = costs[costs["Attribute"].isin(CORE_COST_ATTRIBUTES)].copy()
    codes = _normalise_text(rows["Process"]).str.upper()
    rows = rows[codes.isin(processes)]
    rows["period"] = pd.to_numeric(rows["Period"], errors="coerce")
    rows["power_core_cost_m"] = pd.to_numeric(rows["Value"], errors="coerce")
    rows["process"] = codes.loc[rows.index]
    rows["attribute"] = _normalise_text(rows["Attribute"])
    return rows.dropna(subset=["period", "power_core_cost_m"]).loc[:, columns]


def _cost_processes(costs: pd.DataFrame, power_processes: Iterable[str]) -> pd.DataFrame:
    """Return core costs for validated Power processes, by model period."""
    rows = _power_cost_rows(costs, power_processes)
    if rows.empty:
        return pd.DataFrame(columns=["period", "power_core_cost_m"])
    return rows.groupby("period", as_index=False)["power_core_cost_m"].sum()


def _check_row(check: str, detail: str, value: object = "") -> dict[str, object]:
    return {"Check": check, "Detail": detail, "Value": value}


def prepare_resource_fuel_export(
    result: ResourceAllocationResult,
    *,
    run_id: str,
    costs: pd.DataFrame,
    power_processes: Iterable[str] = (),
    years: Iterable[int] = EXPORT_YEARS,
    price_year: int | None = None,
) -> ResourceFuelExport:
    """Prepare all reference-layout tables from selected-run primitives.

    The reference table deliberately allocates only synthetic, bio, hydrogen,
    natural gas, oil, and distributed electricity to sectors.  Coal, renewable
    resources, uranium, and direct electricity source costs remain transparent
    source-cost rows rather than being silently folded into Fuelex.
    """
    selected_years = tuple(sorted({int(year) for year in years}))
    checks: list[dict[str, object]] = []

    lineage = result.quantity_lineage.copy()
    raw_fuel_use = pd.DataFrame(columns=["period", "sector", "resource_pool", "reference_fuel", "quantity_twh", "included"])
    quantity_columns = {"period", "sector", "quantity_twh", "included", "resource_pool"}
    if not quantity_columns.issubset(lineage.columns) or lineage.empty:
        lineage = pd.DataFrame(columns=list(quantity_columns))
        checks.append(_check_row("Quantity evidence", "No compatible selected-run quantity lineage is available."))
    else:
        # Classify every reported row first so the workbook can show the rows
        # that were excluded alongside the rows that were kept.
        lineage["sector"] = _normalise_text(lineage["sector"]).replace("", "Unlabelled")
        lineage["reference_fuel"] = lineage.apply(_reference_fuel, axis=1)
        lineage["quantity_twh"] = pd.to_numeric(lineage["quantity_twh"], errors="coerce")
        lineage["included"] = lineage["included"].fillna(False).astype(bool)
        raw_fuel_use = lineage.copy()
        lineage = lineage[lineage["included"]].copy()
        unmapped = lineage[lineage["reference_fuel"].eq("") & lineage["quantity_twh"].notna()]
        for _, row in unmapped.groupby("resource_pool", dropna=False)["quantity_twh"].sum().reset_index().iterrows():
            pool = str(row["resource_pool"] or "").strip()
            if pool in REVIEWED_NON_REFERENCE_POOLS:
                checks.append(_check_row(
                    "Reviewed non-reference fuel type",
                    f"{pool} is intentionally excluded from the fixed reference TWh layout.",
                    float(row["quantity_twh"]),
                ))
                continue
            checks.append(_check_row(
                "Unmapped export fuel type",
                f"{pool or 'Unspecified'} is not part of the fixed reference TWh layout.",
                float(row["quantity_twh"]),
            ))
        unexpected_sectors = sorted(set(lineage["sector"]).difference(EXPORT_SECTORS))
        for sector in unexpected_sectors:
            checks.append(_check_row("Unexpected sector", "Excluded from the fixed reference layout.", sector))
        lineage = lineage[
            lineage["reference_fuel"].isin(TWH_FUELS)
            & lineage["sector"].isin(EXPORT_SECTORS)
        ]

    milestone_twh = lineage.groupby(
        ["period", "sector", "reference_fuel"], as_index=False
    )["quantity_twh"].sum()
    annual_twh = _annualise_primitives(
        milestone_twh, ["sector", "reference_fuel"], "quantity_twh", selected_years
    )
    twh = _wide(annual_twh, ["sector", "reference_fuel"], "quantity_twh", selected_years)
    if not twh.empty:
        twh = twh.rename(columns={"sector": "Sector", "reference_fuel": "Fuel type"})
    full_twh_index = pd.MultiIndex.from_product(
        [EXPORT_SECTORS, TWH_FUELS], names=["Sector", "Fuel type"]
    ).to_frame(index=False)
    twh = full_twh_index.merge(twh, on=["Sector", "Fuel type"], how="left")

    source_summary = result.pool_summary.copy()
    raw_source_costs = pd.DataFrame(
        columns=["period", "view", "resource_pool", "source_fuel", "source_cost_m"]
    )
    expected_summary = {"period", "view", "resource_pool", "source_cost_m"}
    if not expected_summary.issubset(source_summary.columns) or source_summary.empty:
        source_summary = pd.DataFrame(columns=list(expected_summary))
        checks.append(_check_row("Source cost evidence", "No compatible selected-run source cost summary is available."))
    else:
        # Every view is retained in the raw sheet so a reader can see which
        # rows the flow-cost attribution selects and which it leaves aside.
        source_summary["source_fuel"] = source_summary["resource_pool"].map(_POOL_TO_SOURCE_FUEL).fillna("")
        raw_source_costs = source_summary.loc[:, [
            "period", "view", "resource_pool", "source_fuel", "source_cost_m",
        ]].copy()
        source_summary = source_summary[source_summary["view"].eq(RESOURCE_FLOW_VIEW)].copy()
        unmatched_sources = source_summary[source_summary["source_fuel"].eq("")]
        for _, row in unmatched_sources.groupby("resource_pool", dropna=False)["source_cost_m"].sum().reset_index().iterrows():
            pool = str(row["resource_pool"] or "").strip()
            if pool in REVIEWED_NON_REFERENCE_POOLS:
                checks.append(_check_row(
                    "Reviewed non-reference source cost",
                    "Intentionally excluded from the fixed reference source-cost layout.",
                    pool,
                ))
                continue
            checks.append(_check_row(
                "Unmapped source cost", "Excluded from the fixed reference source-cost layout.", pool
            ))
        source_summary = source_summary[source_summary["source_fuel"].isin(SOURCE_COST_FUELS)]
    milestone_source_costs = (
        source_summary.groupby(["period", "source_fuel"], as_index=False)["source_cost_m"].sum()
        if {"period", "source_fuel", "source_cost_m"}.issubset(source_summary.columns)
        and not source_summary.empty
        else pd.DataFrame(columns=["period", "source_fuel", "source_cost_m"])
    )
    annual_source = _annualise_primitives(
        source_summary.rename(columns={"source_cost_m": "cost_m"}),
        ["source_fuel"], "cost_m", selected_years,
    )
    source_costs_m = _wide(annual_source, ["source_fuel"], "cost_m", selected_years).rename(
        columns={"source_fuel": "Fuel type"}
    )
    source_costs_m = pd.DataFrame({"Fuel type": SOURCE_COST_FUELS}).merge(
        source_costs_m, on="Fuel type", how="left"
    )
    source_total = source_costs_m.loc[:, list(selected_years)].sum(min_count=1)
    source_costs_m = pd.concat([
        source_costs_m,
        pd.DataFrame([{ "Fuel type": "Total", **source_total.to_dict() }]),
    ], ignore_index=True)

    # Recalculate annual direct fuel allocations from annual primitive quantities.
    source_lookup = annual_source.rename(columns={"source_fuel": "Fuel type", "cost_m": "source_cost_m"})
    # Reference fuels feeding each exported allocation row. Biomethane shares
    # the combined bio source cost with solid biomass.
    allocation_reference_fuels = {
        "Synthetic fuel": ("Synthetic fuel",),
        "Hydrogen": ("Hydrogen",),
        "Natural gas": ("Natural gas",),
        "Oil": ("Oil",),
        "Biomass & biofuels": ("Biomass & biofuels", "Biomethane"),
    }
    allocation_source_fuels = {
        "Synthetic fuel": "Ammonia & synthetics",
        "Hydrogen": "Hydrogen",
        "Natural gas": "Natural gas",
        "Oil": "Oil",
        "Biomass & biofuels": "Biomass & biofuels",
    }
    allocation_fuels = list(allocation_reference_fuels)
    fuel_of_reference = {
        reference: fuel
        for fuel, references in allocation_reference_fuels.items()
        for reference in references
    }
    # Index the quantities and source costs once instead of re-scanning the
    # frames for every year, sector and fuel combination.
    priced_twh = annual_twh[annual_twh["reference_fuel"].isin(fuel_of_reference)].copy()
    priced_twh["Fuel type"] = priced_twh["reference_fuel"].map(fuel_of_reference)
    denominator_by_fuel = priced_twh.groupby(["year", "Fuel type"])["quantity_twh"].sum()
    use_by_sector = priced_twh.groupby(["year", "sector", "Fuel type"])["quantity_twh"].sum()
    source_cost_by_fuel = (
        source_lookup.drop_duplicates(subset=["year", "Fuel type"], keep="first")
        .set_index(["year", "Fuel type"])["source_cost_m"]
    )

    def _fuel_terms(year: int, fuel: str) -> tuple[float, float]:
        """Return the source cost and allocation denominator for one fuel year."""
        cost = source_cost_by_fuel.get((year, allocation_source_fuels[fuel]))
        return (
            float(cost) if pd.notna(cost) else float("nan"),
            float(denominator_by_fuel.get((year, fuel), 0.0)),
        )

    fuel_terms = {
        (year, fuel): _fuel_terms(year, fuel)
        for year in selected_years
        for fuel in allocation_fuels
    }
    for (year, fuel), (cost, denominator) in fuel_terms.items():
        if pd.notna(cost) and denominator <= 0 and abs(cost) > 1e-9:
            subject = (
                f"Combined bio fuel in {year}" if fuel == "Biomass & biofuels"
                else f"{fuel} in {year}"
            )
            checks.append(_check_row(
                "Non-positive fuel denominator", f"{subject} remains unallocated.", cost
            ))
    allocation_rows: list[dict[str, object]] = []
    for year in selected_years:
        for sector in EXPORT_SECTORS:
            for fuel in allocation_fuels:
                cost, denominator = fuel_terms[(year, fuel)]
                use = float(use_by_sector.get((year, sector, fuel), 0.0))
                allocation_rows.append({
                    "year": year, "Sector": sector, "Fuel type": fuel,
                    "cost_m": (
                        cost * use / denominator
                        if denominator > 0 and pd.notna(cost) else pd.NA
                    ),
                })
    direct_costs = pd.DataFrame(allocation_rows)

    # Electricity uses an independently-derived Power-system base cost.  The
    # Power sector is excluded from distribution to prevent circular allocation.
    raw_power_costs = _power_cost_rows(costs, power_processes)
    power_core = _cost_processes(costs, power_processes)
    direct_power = direct_costs[(direct_costs["Sector"].eq("Power")) & direct_costs["Fuel type"].isin(RESOURCE_COST_ROWS)].groupby(
        "year", as_index=False
    )["cost_m"].sum(min_count=1).rename(columns={"year": "period", "cost_m": "power_direct_fuel_m"})
    electricity_base = power_core.merge(direct_power, on="period", how="outer")
    electricity_base["electricity_base_m"] = (
        electricity_base["power_core_cost_m"] - electricity_base["power_direct_fuel_m"]
    )
    # A period with Power fuel cost but no core Power cost subtracts into NaN and
    # would otherwise disappear from the electricity rows without explanation.
    missing_power_core = electricity_base[
        electricity_base["power_core_cost_m"].isna()
        & electricity_base["power_direct_fuel_m"].notna()
    ]
    for _, row in missing_power_core.iterrows():
        checks.append(_check_row(
            "Missing Power core cost",
            f"{int(row['period'])} has Power fuel cost but no core Power cost, so its "
            "electricity cost is left blank rather than assumed.",
            float(row["power_direct_fuel_m"]),
        ))
    # _annualise_primitives expects grouping columns; a single synthetic group
    # keeps its behaviour explicit and prevents extrapolation.
    if not electricity_base.empty:
        electricity_base["series"] = "Electricity costs"
        annual_electricity_base = _annualise_primitives(
            electricity_base, ["series"], "electricity_base_m", selected_years
        )
    else:
        annual_electricity_base = pd.DataFrame(columns=["series", "year", "electricity_base_m"])
    electricity_base_years = tuple(sorted({
        int(period)
        for period in electricity_base.dropna(
            subset=["period", "electricity_base_m"]
        )["period"]
    })) if not electricity_base.empty else ()

    electricity_rows: list[dict[str, object]] = []
    distributed_rows: list[dict[str, object]] = []
    for year in selected_years:
        base = annual_electricity_base.loc[annual_electricity_base["year"].eq(year), "electricity_base_m"]
        base_value = base.iloc[0] if not base.empty else pd.NA
        power_fuel = direct_costs[(direct_costs["year"].eq(year)) & (direct_costs["Sector"].eq("Power"))]["cost_m"].sum(min_count=1)
        distributed_total = base_value + power_fuel if pd.notna(base_value) and pd.notna(power_fuel) else pd.NA
        electricity_rows.append({"Row": "Electricity Costs", "year": year, "cost_bn": base_value / 1000 if pd.notna(base_value) else pd.NA})
        electricity_rows.append({"Row": "Electricity Costs + Power fuel costs", "year": year, "cost_bn": distributed_total / 1000 if pd.notna(distributed_total) else pd.NA})
        year_twh = annual_twh[annual_twh["year"].eq(year)]
        non_power = year_twh[~year_twh["sector"].eq("Power")]
        denominator = float(non_power.loc[non_power["reference_fuel"].eq("Electricity"), "quantity_twh"].sum())
        for sector in EXPORT_SECTORS:
            use = float(year_twh.loc[
                year_twh["sector"].eq(sector) & year_twh["reference_fuel"].eq("Electricity"), "quantity_twh"
            ].sum())
            value = 0.0 if sector == "Power" else (
                distributed_total * use / denominator if denominator > 0 and pd.notna(distributed_total) else pd.NA
            )
            distributed_rows.append({"year": year, "Sector": sector, "Fuel type": "Electricity (distributed)", "cost_m": value})
        if pd.notna(distributed_total) and denominator <= 0 and abs(float(distributed_total)) > 1e-9:
            checks.append(_check_row("Non-positive electricity denominator", f"Non-Power electricity use in {year} is not positive.", float(distributed_total)))

    electricity_costs_bn = _wide(pd.DataFrame(electricity_rows), ["Row"], "cost_bn", selected_years)
    electricity_costs_bn = pd.DataFrame({"Row": ["Electricity Costs", "Electricity Costs + Power fuel costs"]}).merge(
        electricity_costs_bn, on="Row", how="left"
    )
    resource_long = pd.concat([direct_costs, pd.DataFrame(distributed_rows)], ignore_index=True)
    resource_costs_m = _wide(resource_long, ["Sector", "Fuel type"], "cost_m", selected_years)
    full_resource_index = pd.MultiIndex.from_product(
        [EXPORT_SECTORS, RESOURCE_COST_ROWS], names=["Sector", "Fuel type"]
    ).to_frame(index=False)
    resource_costs_m = full_resource_index.merge(resource_costs_m, on=["Sector", "Fuel type"], how="left")

    fuelex_rows: list[dict[str, object]] = []
    for year in selected_years:
        values = resource_long[resource_long["year"].eq(year)].pivot_table(
            index="Sector", columns="Fuel type", values="cost_m", aggfunc="sum"
        )
        for sector in EXPORT_SECTORS:
            row = values.loc[sector] if sector in values.index else pd.Series(dtype=float)
            alt = sum(pd.to_numeric(row.get(name, 0.0), errors="coerce") for name in ("Synthetic fuel", "Biomass & biofuels", "Hydrogen"))
            fossil = sum(pd.to_numeric(row.get(name, 0.0), errors="coerce") for name in ("Natural gas", "Oil"))
            electricity = pd.to_numeric(row.get("Electricity (distributed)", 0.0), errors="coerce")
            metrics = {
                "Fuelex - alt": alt,
                "Fuelex - alt + elec": alt + electricity,
                "Fuelex - f/fuels": fossil,
                "Net Fuel Costs": alt + fossil,
                "Net Fuel Costs + elec": alt + fossil + electricity,
            }
            for label, value in metrics.items():
                fuelex_rows.append({"year": year, "Sector": sector, "Measure": label, "cost_bn": value / 1000})
    fuelex_bn = _wide(pd.DataFrame(fuelex_rows), ["Sector", "Measure"], "cost_bn", selected_years)
    full_fuelex_index = pd.MultiIndex.from_product(
        [EXPORT_SECTORS, FUELEX_ROWS], names=["Sector", "Measure"]
    ).to_frame(index=False)
    fuelex_bn = full_fuelex_index.merge(fuelex_bn, on=["Sector", "Measure"], how="left")

    checks.extend([
        _check_row("Workbook method", "Annual TWh and source costs are interpolated before allocation; no reference file is read at runtime."),
        _check_row("Electricity distribution", "Distributed only to non-Power electricity users; Power receives zero distributed electricity."),
        _check_row("Commodity costs", "Cost_Com is excluded from all workbook formulae."),
        _check_row(
            "Not additive",
            "Source-fuel rows and Electricity (distributed) rows must not be summed "
            "across sectors: distributed electricity already contains the Power "
            "sector's own fuel cost, which is also reported on the Power sector's "
            "source-fuel rows.",
        ),
    ])
    diagnostics = result.diagnostics
    if not diagnostics.empty and {"diagnostic_type", "period", "value_m"}.issubset(diagnostics.columns):
        nearest = diagnostics[
            diagnostics["diagnostic_type"].eq("share_basis_nearest_available_year")
        ]
        for period, value in nearest.groupby("period")["value_m"].sum().items():
            checks.append(_check_row(
                "Investment split by nearest available year",
                f"{int(period)}: investment cost whose build year had no recorded use of its "
                "pool, or predates the first model year, was split by the nearest available "
                "year's sector mix instead.",
                float(value),
            ))
    milestone_years = tuple(sorted({
        int(period)
        for frame, column in (
            (milestone_twh, "period"),
            (milestone_source_costs, "period"),
            (power_core, "period"),
        )
        for period in pd.to_numeric(frame.get(column, pd.Series(dtype=float)), errors="coerce").dropna()
    }))
    return ResourceFuelExport(
        run_id=run_id,
        years=selected_years,
        twh=twh,
        source_costs_m=source_costs_m,
        electricity_costs_bn=electricity_costs_bn,
        resource_costs_m=resource_costs_m,
        fuelex_bn=fuelex_bn,
        checks=pd.DataFrame(checks, columns=["Check", "Detail", "Value"]),
        raw_fuel_use=raw_fuel_use,
        raw_source_costs=raw_source_costs,
        raw_power_costs=raw_power_costs,
        milestone_fuel_use=milestone_twh,
        milestone_source_costs=milestone_source_costs,
        milestone_power_costs=power_core,
        milestone_years=milestone_years,
        electricity_base_years=electricity_base_years,
        price_year=price_year,
    )


# ---------------------------------------------------------------------------
# Provenance evidence
# ---------------------------------------------------------------------------

_FLOW_PROVENANCE_COLUMNS = (
    "period", "region", "process", "process_name", "commodity", "commodity_name",
    "direction", "reported_value", "unit", "vd_row_count", "timeslice_mode",
    "vdt_match", "provenance_note",
)
_UNIT_CONVERSION_COLUMNS = (
    "period", "commodity", "commodity_name", "unit", "unit_factor",
    "conversion_status",
)
_POOLING_TABLE_COLUMNS = ("commodity", "resource_pool", "pooling_status")

_TIMESLICE_EXPLANATIONS = {
    "explicit_annual": "The run reported an annual figure, so it was used as-is.",
    "summed_timeslices": "The run reported separate time slices, which were added together.",
}


def prepare_flow_provenance(annual_flows) -> pd.DataFrame:
    """Show how each reported connection was formed from the run's result rows.

    This is the step before every other sheet: it names the connection, the
    value as reported, how many result rows were combined to reach it, and
    whether the connection exists in the run's own model connections. Flows
    with no matching connection are retained here and marked, never dropped
    silently.
    """
    empty = pd.DataFrame(columns=list(_FLOW_PROVENANCE_COLUMNS))
    if annual_flows is None:
        return empty
    matched = getattr(annual_flows, "flows", None)
    unmatched = getattr(annual_flows, "unmatched_edges", None)
    parts: list[pd.DataFrame] = []
    for frame, in_connections in ((matched, "Yes"), (unmatched, "No")):
        if frame is None or frame.empty:
            continue
        rows = frame.copy()
        rows["vdt_match"] = in_connections
        if "reason" in rows.columns:
            rows["provenance_note"] = _normalise_text(rows["reason"]).replace(
                "not_in_vdt",
                "This flow is not one of the run's model connections, so it is not used.",
            )
        else:
            rows["provenance_note"] = ""
        parts.append(rows)
    if not parts:
        return empty
    provenance = pd.concat(parts, ignore_index=True)
    provenance = provenance.rename(columns={"value": "reported_value"})
    for column in _FLOW_PROVENANCE_COLUMNS:
        if column not in provenance.columns:
            provenance[column] = ""
    provenance["timeslice_mode"] = _normalise_text(provenance["timeslice_mode"]).map(
        lambda mode: _TIMESLICE_EXPLANATIONS.get(mode, mode)
    )
    provenance["period"] = pd.to_numeric(provenance["period"], errors="coerce")
    provenance["reported_value"] = pd.to_numeric(provenance["reported_value"], errors="coerce")
    provenance["vd_row_count"] = pd.to_numeric(
        provenance["vd_row_count"], errors="coerce"
    ).fillna(0).astype(int)
    return provenance.loc[:, list(_FLOW_PROVENANCE_COLUMNS)].sort_values(
        ["period", "commodity", "process", "direction"], kind="stable"
    ).reset_index(drop=True)


def prepare_unit_conversion(quantity_lineage: pd.DataFrame) -> pd.DataFrame:
    """List each model year, commodity unit and factor that reaches TWh.

    The factor is reproduced from the same lineage the allocation used, so the
    workbook can perform the conversion itself rather than assert its result.
    """
    empty = pd.DataFrame(columns=list(_UNIT_CONVERSION_COLUMNS))
    if quantity_lineage is None or quantity_lineage.empty:
        return empty
    if not {"period", "commodity"}.issubset(quantity_lineage.columns):
        return empty
    rows = quantity_lineage.copy()
    rows["period"] = pd.to_numeric(rows["period"], errors="coerce")
    for column in ("commodity", "commodity_name", "unit"):
        rows[column] = _normalise_text(rows.get(column, pd.Series("", index=rows.index)))
    rows["unit_factor"] = pd.to_numeric(
        rows.get("unit_factor", pd.Series(pd.NA, index=rows.index)), errors="coerce"
    )
    rows = rows[rows["period"].notna() & rows["commodity"].ne("")].copy()
    if rows.empty:
        return empty
    rows["period"] = rows["period"].astype(int)
    conversion = rows.drop_duplicates(
        ["period", "commodity", "unit"], keep="first"
    ).copy()
    conversion["conversion_status"] = [
        "Cannot be converted to TWh" if pd.isna(factor)
        else "Already in TWh" if abs(float(factor) - 1.0) < 1e-12
        else "Converted to TWh"
        for factor in conversion["unit_factor"]
    ]
    return conversion.loc[:, list(_UNIT_CONVERSION_COLUMNS)].sort_values(
        ["period", "commodity_name", "commodity"], kind="stable"
    ).reset_index(drop=True)


def prepare_pooling_table(pools: pd.DataFrame) -> pd.DataFrame:
    """Restate the curated pool sheet, one row per commodity code."""
    empty = pd.DataFrame(columns=list(_POOLING_TABLE_COLUMNS))
    if pools is None or pools.empty:
        return empty
    prepared = pools.copy()
    for column in ("commodity", "resource_pool"):
        if column not in prepared.columns:
            prepared[column] = ""
        prepared[column] = _normalise_text(prepared[column])
    prepared["pooling_status"] = [
        "Not pooled" if not pool
        else "Demand pool (outside the fuel split)" if pool in DEMAND_SERVICE_POOLS
        else "Fuel pool"
        for pool in prepared["resource_pool"]
    ]
    return prepared.loc[:, list(_POOLING_TABLE_COLUMNS)].sort_values(
        ["resource_pool", "commodity"], kind="stable"
    ).reset_index(drop=True)


@dataclass(frozen=True)
class PathwayExport:
    """Physical and monetary pathway evidence for every year, pool and fuel."""

    links: pd.DataFrame = field(default_factory=pd.DataFrame)
    process_net: pd.DataFrame = field(default_factory=pd.DataFrame)
    source_detail: pd.DataFrame = field(default_factory=pd.DataFrame)
    demand_detail: pd.DataFrame = field(default_factory=pd.DataFrame)
    commodity_balance: pd.DataFrame = field(default_factory=pd.DataFrame)
    unmatched: pd.DataFrame = field(default_factory=pd.DataFrame)
    hidden: pd.DataFrame = field(default_factory=pd.DataFrame)
    diagnostics: pd.DataFrame = field(default_factory=pd.DataFrame)
    supply_chain_links: pd.DataFrame = field(default_factory=pd.DataFrame)
    combinations: tuple[tuple[int, str, str], ...] = ()


PATHWAY_KEY_COLUMNS = ("Model year", "Resource pool", "Individual fuel")
_PATHWAY_SIGNS = ("Costs", "Credits & revenues")


def _tagged(frame: pd.DataFrame | None, keys: dict[str, object]) -> pd.DataFrame | None:
    """Return a copy of one pathway frame with its identifying keys in front."""
    if frame is None or frame.empty:
        return None
    tagged = frame.copy()
    for position, (name, value) in enumerate(keys.items()):
        if name in tagged.columns:
            tagged = tagged.drop(columns=[name])
        tagged.insert(position, name, value)
    return tagged


def _stack(parts: list[pd.DataFrame]) -> pd.DataFrame:
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def _stack_pathway_frames(
    parts: list[tuple[pd.DataFrame, dict[str, object]]],
) -> pd.DataFrame:
    """Add repeated workbook pathway keys once after stacking compatible frames."""
    if not parts:
        return pd.DataFrame()
    source_columns = ("period", "resource_pool", "commodity_name")
    if all(set(source_columns).issubset(frame.columns) for frame, _keys in parts):
        stacked = pd.concat([frame for frame, _keys in parts], ignore_index=True)
        for position, (name, source) in enumerate(zip(PATHWAY_KEY_COLUMNS, source_columns)):
            stacked.insert(position, name, stacked[source])
        return stacked
    return _stack([
        tagged for frame, keys in parts if (tagged := _tagged(frame, keys)) is not None
    ])


def fuel_pathway_combinations(
    result: ResourceAllocationResult,
) -> tuple[tuple[int, str, str], ...]:
    """Return every model year, resource pool and individual fuel with evidence."""
    return _fuel_pathway_combinations(result)


def prepare_all_fuel_pathways(
    result: ResourceAllocationResult,
    *,
    unmatched_flows: pd.DataFrame | None = None,
    annual_diagnostics: pd.DataFrame | None = None,
    progress: object = None,
) -> PathwayExport:
    """Build pathway evidence for every model year, resource pool and fuel.

    The workbook exports the whole run rather than the single combination on
    screen, so ``progress`` is called as ``progress(done, total)`` to let a
    caller report how far the export has advanced.
    """
    combinations = _fuel_pathway_combinations(result)
    periods = sorted({period for period, _, _ in combinations})
    total = len(combinations) + len(periods) * len(_PATHWAY_SIGNS)
    done = 0

    def advance() -> None:
        nonlocal done
        done += 1
        if callable(progress):
            progress(done, total)

    collected: dict[str, list[tuple[pd.DataFrame, dict[str, object]]]] = {
        name: [] for name in (
            "links", "process_net", "source_detail", "demand_detail",
            "commodity_balance", "unmatched", "hidden", "diagnostics",
        )
    }
    for key, pathway in prepare_fuel_pathways_batch(
        result,
        combinations,
        unmatched_flows=unmatched_flows,
        annual_diagnostics=annual_diagnostics,
    ):
        keys = dict(zip(PATHWAY_KEY_COLUMNS, key))
        for name, frame in (
            ("links", pathway.links),
            ("process_net", pathway.process_net_detail),
            ("source_detail", pathway.source_detail),
            ("demand_detail", pathway.demand_detail),
            ("commodity_balance", pathway.commodity_balance),
            ("unmatched", pathway.unmatched_detail),
            ("hidden", pathway.hidden_detail),
            ("diagnostics", pathway.diagnostics),
        ):
            if frame is not None and not frame.empty:
                collected[name].append((frame, keys))
        advance()

    supply_chain: list[pd.DataFrame] = []
    for period in periods:
        for sign in _PATHWAY_SIGNS:
            pathway = prepare_supply_chain_cost_pathway_links(
                result, period=period, sign=sign
            )
            tagged = _tagged(
                pathway.links, {"Model year": period, "Cost or credit": sign}
            )
            if tagged is not None:
                supply_chain.append(tagged)
            advance()

    return PathwayExport(
        links=_stack_pathway_frames(collected["links"]),
        process_net=_stack_pathway_frames(collected["process_net"]),
        source_detail=_stack_pathway_frames(collected["source_detail"]),
        demand_detail=_stack_pathway_frames(collected["demand_detail"]),
        commodity_balance=_stack_pathway_frames(collected["commodity_balance"]),
        unmatched=_stack_pathway_frames(collected["unmatched"]),
        hidden=_stack_pathway_frames(collected["hidden"]),
        diagnostics=_stack_pathway_frames(collected["diagnostics"]),
        supply_chain_links=_stack(supply_chain),
        combinations=combinations,
    )


def resource_fuel_workbook_bytes(
    export: ResourceFuelExport,
    *,
    pathways: PathwayExport | None = None,
    evidence: object = None,
) -> bytes:
    """Serialize the export as one traceable, formula-driven XLSX workbook."""
    from cost_allocation.workbook_writer import build_workbook_bytes

    return build_workbook_bytes(export, pathways=pathways, evidence=evidence)


def resource_fuel_filename(run_id: str) -> str:
    """Return a safe user-facing filename while retaining the run identity."""
    invalid = '<>:"/\\|?*'
    safe = "".join("_" if character in invalid else character for character in str(run_id)).strip()
    return f"{safe or 'selected_run'}_resource_and_fuel_full_workbook.xlsx"
