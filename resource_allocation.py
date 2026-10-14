"""Pure resource and fuel cost-pool allocation.

The workbook method is a top-down proportional allocation: a resource pool is
distributed to sectors using each sector's share of observed carrier use.  This
module deliberately does not import Streamlit or the legacy recursive
``AllocationMatrix``.  Selected-run VDT-matched annual flows provide quantities,
and each selected source cost row is assigned to one output-carrier pool, split
by observed output shares, or retained as explicitly unallocated.

The share of a pool used by a converter (a process producing another pool) is
passed on to the pools it produces and solved as a full chain in
``resource_cascade``; only final use books cost to a sector.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
import re
from typing import Iterable, Iterator, NamedTuple

import pandas as pd

from cost_allocation.constants import TWH_PER_PJ
from cost_allocation.helpers import compute_period_durations
from cost_allocation.labels import COMMODITY_LABEL_COLUMNS
from cost_allocation.resource_cascade import (
    CASCADE_ALLOCATION_COLUMNS,
    CASCADE_ROLE_FINAL,
    CASCADE_ROLE_PASSED_ON,
    CASCADE_ROLE_UNALLOCATED,
    PROCESS_ALLOCATION_COLUMNS,
    CascadeNetwork,
    apply_cascade,
    build_cascade_network,
    origin_sector_shares,
    unallocated_ledger_rows,
)
from cost_allocation.resource_rules import (
    DEFAULT_RESOURCE_POOLS_PATH,
    POOL_KIND_DEMAND,
    POOL_KIND_FUEL,
    POOL_KIND_SERVICE,
    POOL_KINDS,
    SPLIT_POOL_KINDS,
)
from cost_allocation.text_norm import normalise_code


RESOURCE_FLOW_VIEW = "Resource / flow cost"
UPSTREAM_SUPPLY_VIEW = "All upstream supply cost"
SEPARATE_COMMODITY_VIEW = "Commodity / policy cost (separate)"
# Sankey widths cannot be negative, so costs and credits are drawn apart.
SIGN_COSTS = "Costs"
SIGN_CREDITS = "Credits & revenues"
FINANCIAL_SIGNS = (SIGN_COSTS, SIGN_CREDITS)
# Costs and credits summed per displayed link; a net credit is drawn reversed.
SIGN_NET = "Net"
CONSUMER_USE_COLUMNS = (
    "period", "resource_pool", "region", "process", "sector", "role", "quantity_twh",
)
NET_DIRECTION_COST = "Net cost"
NET_DIRECTION_CREDIT = "Net credit (drawn reversed)"
CORE_COST_ATTRIBUTES = ("Cost_Inv", "Cost_Fom", "Cost_Act", "Cost_Flo")
# 3: costs cascade through conversion processes (full chain).
# 5: results carry ``consumer_use`` for final-user unit costs.
VINTAGE_ALLOCATION_SCHEMA_VERSION = 5
# Which year's sector use mix splits a cost row (see ``_choose_share_basis``).
SHARE_BASIS_BUILD_YEAR = "Build year"
SHARE_BASIS_REPORTING_YEAR = "Reporting year"
SHARE_BASIS_NEAREST_YEAR = "Nearest available year"
SHARE_BASIS_NO_USE = "No eligible year with use"
RESOURCE_ALLOCATION_VIEWS = (
    RESOURCE_FLOW_VIEW,
    UPSTREAM_SUPPLY_VIEW,
    SEPARATE_COMMODITY_VIEW,
)
_VIEW_COST_ATTRIBUTES = {
    RESOURCE_FLOW_VIEW: ("Cost_Flo",),
    UPSTREAM_SUPPLY_VIEW: CORE_COST_ATTRIBUTES,
    SEPARATE_COMMODITY_VIEW: ("Cost_Com",),
}
# A supply-chain view over a chosen subset of the core attributes.  It uses the
# same source-process gate and cascade as the fixed views, so {Cost_Flo} is the
# flow-cost view and all four attributes are the full supply-chain view.
ATTRIBUTE_VIEW_PREFIX = "Supply-chain cost: "


def attribute_view(attributes: Iterable[str]) -> str:
    """Return the cost view that allocates exactly ``attributes``."""
    chosen = set(attributes)
    unknown = chosen.difference(CORE_COST_ATTRIBUTES)
    if unknown or not chosen:
        raise ValueError(
            "Choose one or more of " + ", ".join(CORE_COST_ATTRIBUTES)
            + (f"; unsupported: {', '.join(sorted(unknown))}" if unknown else ".")
        )
    ordered = tuple(attribute for attribute in CORE_COST_ATTRIBUTES if attribute in chosen)
    for view in (RESOURCE_FLOW_VIEW, UPSTREAM_SUPPLY_VIEW):
        if ordered == _VIEW_COST_ATTRIBUTES[view]:
            return view
    return ATTRIBUTE_VIEW_PREFIX + " + ".join(ordered)


def view_cost_attributes(view: str) -> tuple[str, ...]:
    """Return the cost attributes a view allocates; raise for an unknown view."""
    if view in _VIEW_COST_ATTRIBUTES:
        return _VIEW_COST_ATTRIBUTES[view]
    if str(view).startswith(ATTRIBUTE_VIEW_PREFIX):
        attributes = tuple(str(view)[len(ATTRIBUTE_VIEW_PREFIX):].split(" + "))
        if set(attributes) <= set(CORE_COST_ATTRIBUTES) and attribute_view(attributes) == view:
            return attributes
    raise ValueError(f"Unsupported resource allocation view(s): {view}")
COMMODITY_COVERAGE_COUNTED = "Counted"
COMMODITY_COVERAGE_NOT_COUNTED = "Not counted"
COMMODITY_COVERAGE_ATTENTION = "Needs attention"
# Ordered most actionable first; a commodity code answered differently by
# different observed flows takes the earliest answer in this order.
COMMODITY_COVERAGE_STATUSES = (
    COMMODITY_COVERAGE_ATTENTION,
    COMMODITY_COVERAGE_COUNTED,
    COMMODITY_COVERAGE_NOT_COUNTED,
)
_ENERGY_UNIT_FACTORS = {
    "PJ": TWH_PER_PJ,
    "PJ/YR": TWH_PER_PJ,
    "PJ/YEAR": TWH_PER_PJ,
    "TWH": 1.0,
    "TWH/YR": 1.0,
    "TWH/YEAR": 1.0,
}
_PROCESS_TAXONOMY_FIELDS = (
    "process_group", "process_category", "process_subsector",
    "process_sector", "process_ghgi", "process_dukes",
    "process_useful_output", "process_set",
)
RESOURCE_PATHWAY_SANKEY_AGGREGATIONS = {
    "Individual processes": None,
    "Technology group": "process_group",
    "Process category": "process_category",
    "Subsector": "process_subsector",
    "Sector": "process_sector",
    "GHGI": "process_ghgi",
    "DUKES": "process_dukes",
    "Useful output": "process_useful_output",
    "Process set": "process_set",
}
RESOURCE_PATHWAY_SANKEY_COLOUR_DIMENSIONS = {
    "Node role": None,
    **{
        label: field
        for label, field in RESOURCE_PATHWAY_SANKEY_AGGREGATIONS.items()
        if field is not None
    },
}
DEFAULT_RESOURCE_PATHWAY_SUPPLY_AGGREGATION = "Technology group"
DEFAULT_RESOURCE_PATHWAY_DEMAND_AGGREGATION = "Sector"
DEFAULT_FUEL_PATHWAY_SUPPLY_AGGREGATION = "Subsector"
DEFAULT_FUEL_PATHWAY_DEMAND_AGGREGATION = "Subsector"
# Compatibility alias for integrations that configured both physical endpoints
# with one shared aggregation selection.
DEFAULT_FUEL_PATHWAY_PROCESS_AGGREGATION = DEFAULT_FUEL_PATHWAY_SUPPLY_AGGREGATION

_FUEL_CALCULATION_INCLUDED = "included"
_FUEL_VISIBILITY_DISPLAYED = "displayed"
_FUEL_VISIBILITY_EXTRACTION = "hidden_domestic_extraction"
_FUEL_VISIBILITY_DUMMY_EXPORT = "hidden_dummy_export"
_EXTRACTION_SUBSECTORS = frozenset({
    "coal extraction", "gas extraction", "oil extraction",
})
_RE_IMPORT_LOOP_SUBSECTOR = "fuel re-imports"


@dataclass(frozen=True)
class ResourceAllocationResult:
    """Auditable outputs from one selected run."""

    allocations: pd.DataFrame
    quantities: pd.DataFrame
    quantity_lineage: pd.DataFrame
    cost_assignments: pd.DataFrame
    pool_summary: pd.DataFrame
    view_summary: pd.DataFrame
    commodity_cost_summary: pd.DataFrame
    commodity_mapping: pd.DataFrame
    diagnostics: pd.DataFrame
    vintage_allocations: pd.DataFrame = field(default_factory=pd.DataFrame)
    # Per consumer process: cost booked as final use or passed on to another pool.
    process_allocations: pd.DataFrame = field(
        default_factory=lambda: pd.DataFrame(columns=[*PROCESS_ALLOCATION_COLUMNS, "pool_kind"])
    )
    # Final-use cost by the pool where it originated and the pool that delivered it.
    cascade_allocations: pd.DataFrame = field(
        default_factory=lambda: pd.DataFrame(columns=[
            *CASCADE_ALLOCATION_COLUMNS, "origin_pool_kind", "pool_kind",
        ])
    )
    # Each consumer's own-period use of a pool and its cascade role in that period.
    consumer_use: pd.DataFrame = field(
        default_factory=lambda: pd.DataFrame(columns=list(CONSUMER_USE_COLUMNS))
    )

    @cached_property
    def fuel_evidence(self) -> pd.DataFrame:
        """Normalised physical evidence, built once and shared by every reader.

        Normalising ``quantity_lineage`` is the dominant cost of opening the
        pathway tabs, and a single rerun asks for it repeatedly.  Callers must
        treat the frame as read-only and copy before mutating.
        """
        return _build_fuel_evidence(self)


@dataclass(frozen=True)
class ResourcePathwayResult:
    """Chart-independent Sankey links with reconciliation and diagnostics."""

    links: pd.DataFrame
    summary: pd.DataFrame
    diagnostics: pd.DataFrame


@dataclass(frozen=True)
class FuelPathwayResult:
    """Pooled individual-fuel physical links and their auditable evidence."""

    links: pd.DataFrame
    summary: pd.DataFrame
    source_detail: pd.DataFrame
    demand_detail: pd.DataFrame
    commodity_balance: pd.DataFrame
    unmatched_detail: pd.DataFrame
    hidden_detail: pd.DataFrame
    diagnostics: pd.DataFrame
    process_net_detail: pd.DataFrame = field(default_factory=pd.DataFrame)


_PATHWAY_LINK_COLUMNS = (
    "period", "view", "sign", "stage", "source_id", "source_label",
    "source_kind", "target_id", "target_label", "target_kind",
    "resource_pool", "signed_value", "display_value", "region",
    "process", "sector", "detail", "source_colour_field",
    "source_colour_category", "source_colour_contributors",
    "target_colour_field", "target_colour_category",
    "target_colour_contributors", "gross_cost_m", "gross_credit_m", "net_direction",
)
_PATHWAY_LINK_VALUE_COLUMNS = (
    "signed_value", "display_value", "detail", "gross_cost_m", "gross_credit_m",
    "net_direction",
)
_PATHWAY_DIAGNOSTIC_COLUMNS = (
    "diagnostic_type", "period", "view", "resource_pool", "detail", "value",
)
_FUEL_PATHWAY_LINK_COLUMNS = (
    "period", "view", "sign", "stage", "source_id", "source_label",
    "source_kind", "source_role", "target_id", "target_label", "target_kind",
    "target_role", "resource_pool", "commodity_name", "commodity",
    "exact_commodity_codes", "exact_commodity_count", "commodity_function",
    "function_tags", "commodity_component", "commodity_occurrence",
    "commodity_resolution_code", "commodity_resolution", "direction", "flow_role",
    "link_role", "depth", "region",
    "process", "process_name", "sector", "gross_output_twh", "gross_input_twh",
    "net_flow_twh",
    "signed_value", "display_value", "unit", "timeslice_mode", "vd_row_count",
    "display_mode", "stakeholder_visibility", "stakeholder_visibility_reason",
    "process_grouping_field", "process_grouping_label", "grouped_process_count",
    "grouped_processes", "source_colour_field", "source_colour_category",
    "source_colour_contributors", "target_colour_field", "target_colour_category",
    "target_colour_contributors", "detail",
)
_FUEL_DETAIL_COLUMNS = (
    "period", "region", "direction", "commodity", "commodity_name",
    "commodity_subgroup", "commodity_group", "resource_pool",
    "commodity_function", "function_tags", "process", "process_name",
    "process_group", "process_category", "process_subsector", "process_sector",
    "process_set", "connection_role", "connection_role_reason", "link_role",
    "depth", "flow_role", "reported_value", "unit", "quantity_twh", "active",
    "timeslice_mode", "vd_row_count", "calculation_status", "calculation_reason",
    "stakeholder_visibility", "stakeholder_visibility_reason",
    "included_in_graph", "exclusion_reason",
)
_FUEL_BALANCE_COLUMNS = (
    "period", "resource_pool", "commodity_name", "commodity",
    "commodity_function", "function_tags", "gross_output_twh", "gross_input_twh",
    "residual_twh", "residual_pct", "balance_status", "output_process_count",
    "input_process_count", "origin_import_twh", "final_consumption_twh",
    "exports_twh", "transformation_distribution_twh", "unclassified_input_twh",
)
_FUEL_SUMMARY_COLUMNS = (
    "period", "resource_pool", "commodity_name", "gross_output_twh",
    "gross_input_twh", "origin_import_twh", "final_consumption_twh",
    "exports_twh", "imports_twh", "pathway_entry_twh", "all_pathway_entry_twh",
    "displayed_output_twh", "displayed_input_twh", "visible_supply_twh",
    "visible_demand_twh", "all_supply_twh", "all_demand_twh", "supply_demand_gap_twh",
    "hidden_row_count",
    "hidden_twh", "omitted_row_count", "unmatched_row_count", "residual_twh",
    "commodity_count", "unresolved_count", "pass_through_process_count", "pathway_status",
)
_FUEL_PROCESS_NET_COLUMNS = (
    "period", "resource_pool", "commodity_name", "region", "process", "process_name",
    "process_group", "process_category", "process_subsector", "process_sector",
    "process_ghgi", "process_dukes", "process_useful_output", "process_set",
    "gross_output_twh", "gross_input_twh", "net_flow_twh",
    "endpoint_side", "pass_through_suppressed", "flow_role", "exact_commodity_codes",
    "exact_commodity_count", "projection", "display_mode", "stakeholder_visibility",
    "stakeholder_visibility_reason", "process_grouping_field", "process_grouping_label",
    "vd_row_count",
)
_FUEL_UNMATCHED_COLUMNS = (
    "period", "region", "direction", "commodity", "commodity_name",
    "commodity_function", "resource_pool", "process", "process_name", "value",
    "unit", "reason",
)
_FUEL_DIAGNOSTIC_COLUMNS = (
    "diagnostic_type", "period", "resource_pool", "commodity_name", "commodity",
    "process", "detail", "value",
)


def _empty_pathway(summary_columns: Iterable[str]) -> ResourcePathwayResult:
    return ResourcePathwayResult(
        links=pd.DataFrame(columns=list(_PATHWAY_LINK_COLUMNS)),
        summary=pd.DataFrame(columns=list(summary_columns)),
        diagnostics=pd.DataFrame(columns=list(_PATHWAY_DIAGNOSTIC_COLUMNS)),
    )


def _empty_fuel_pathway() -> FuelPathwayResult:
    """Return a stable physical-pathway result for unavailable selections."""
    return FuelPathwayResult(
        links=pd.DataFrame(columns=list(_FUEL_PATHWAY_LINK_COLUMNS)),
        summary=pd.DataFrame(columns=list(_FUEL_SUMMARY_COLUMNS)),
        source_detail=pd.DataFrame(columns=list(_FUEL_DETAIL_COLUMNS)),
        demand_detail=pd.DataFrame(columns=list(_FUEL_DETAIL_COLUMNS)),
        commodity_balance=pd.DataFrame(columns=list(_FUEL_BALANCE_COLUMNS)),
        unmatched_detail=pd.DataFrame(columns=list(_FUEL_UNMATCHED_COLUMNS)),
        hidden_detail=pd.DataFrame(columns=list(_FUEL_DETAIL_COLUMNS)),
        process_net_detail=pd.DataFrame(columns=list(_FUEL_PROCESS_NET_COLUMNS)),
        diagnostics=pd.DataFrame(columns=list(_FUEL_DIAGNOSTIC_COLUMNS)),
    )


def _node_text(value: object, fallback: str) -> str:
    text = str(value or "").strip()
    return text if text else fallback


def _process_node_id(stage: str, region: object, process: object) -> str:
    return f"{stage}|{_node_text(region, 'UNSPECIFIED').upper()}|{_node_text(process, 'UNSPECIFIED').upper()}"


def _cost_endpoint_node_id(field: str | None, label: str, row: pd.Series) -> str:
    """Return one node identity per displayed code in the cost Sankey.

    Own cost, cost passed in, final use and cost passed on at the same code
    share a node, so one process or taxonomy label is drawn once.
    """
    if field is None:
        return _process_node_id("cost_endpoint", row.get("region"), row.get("process"))
    return f"cost_endpoint|{_node_text(label, 'Unlabelled').upper()}"


def _pool_node_id(pathway: str, resource_pool: object) -> str:
    return f"{pathway}_pool|{_node_text(resource_pool, 'Unmapped').upper()}"


def resource_pathway_sankey_aggregation_field(selection: str) -> str | None:
    """Return the authoritative process-label field for a Sankey display level."""
    try:
        return RESOURCE_PATHWAY_SANKEY_AGGREGATIONS[selection]
    except KeyError as exc:
        options = ", ".join(RESOURCE_PATHWAY_SANKEY_AGGREGATIONS)
        raise ValueError(
            f"Unsupported resource pathway Sankey aggregation: {selection}. "
            f"Choose one of: {options}."
        ) from exc


def resource_pathway_sankey_colour_field(selection: str) -> str | None:
    """Return the independent taxonomy field used to colour pathway nodes."""
    try:
        return RESOURCE_PATHWAY_SANKEY_COLOUR_DIMENSIONS[selection]
    except KeyError as exc:
        options = ", ".join(RESOURCE_PATHWAY_SANKEY_COLOUR_DIMENSIONS)
        raise ValueError(
            f"Unsupported resource pathway Sankey colour dimension: {selection}. "
            f"Choose one of: {options}."
        ) from exc


def _validate_pathway_aggregation_field(field: str | None, *, endpoint: str) -> None:
    if field is not None and field not in _PROCESS_TAXONOMY_FIELDS:
        options = ", ".join(_PROCESS_TAXONOMY_FIELDS)
        raise ValueError(
            f"Unsupported {endpoint} aggregation field: {field}. "
            f"Choose one of: {options}, or None for individual processes."
        )


def _activity_display_labels(frame: pd.DataFrame, field: str | None) -> pd.Series:
    """Return a presentation label without changing detailed flow evidence."""
    if field is None:
        process = frame.get("process", pd.Series("", index=frame.index)).fillna("").astype(str).str.strip()
        name = frame.get("process_name", pd.Series("", index=frame.index)).fillna("").astype(str).str.strip()
        return pd.Series(
            [
                f"{process_name} ({process_code})"
                if process_name and process_name != process_code else process_code or "Unlabelled process"
                for process_code, process_name in zip(process, name)
            ],
            index=frame.index,
        )
    return frame.get(field, pd.Series("", index=frame.index)).fillna("").astype(str).str.strip().replace("", "Unlabelled")


def _activity_aggregation_node_id(stage: str, field: str, value: object) -> str:
    """Return a stage- and taxonomy-qualified aggregation node identity."""
    return f"{stage}|{field.upper()}|{_node_text(value, 'Unlabelled').upper()}"


def _activity_contributor_detail(rows: pd.DataFrame) -> str:
    """Summarise detailed process evidence behind a displayed aggregate node."""
    def contributor_text(value: object) -> str:
        return "" if pd.isna(value) else str(value).strip()

    contributors = dict.fromkeys(
        (contributor_text(process), contributor_text(name))
        for process, name in rows[["process", "process_name"]].itertuples(
            index=False, name=None
        )
    )
    labels = [
        f"{name} ({process})" if name and name != process else process or "Unlabelled process"
        for process, name in contributors
    ]
    preview = "; ".join(labels[:8])
    remainder = len(labels) - 8
    if remainder > 0:
        preview += f"; +{remainder} more"
    return f"Contributing processes ({len(labels)}): {preview or 'None'}"


def _activity_colour_metadata(
    rows: pd.DataFrame,
    field: str | None,
) -> dict[str, str]:
    """Resolve one colour category without changing the displayed grouping."""
    if field is None:
        return {"field": "", "category": "", "contributors": ""}
    values = sorted(set(
        rows.get(field, pd.Series("", index=rows.index))
        .fillna("").astype(str).str.strip().loc[lambda series: series.ne("")]
    ))
    category = "Unlabelled" if not values else values[0] if len(values) == 1 else "Mixed"
    return {
        "field": field,
        "category": category,
        "contributors": "; ".join(values) or "Unlabelled",
    }


PATHWAY_STAGE_SOURCE = "supply_to_resource"
PATHWAY_STAGE_PASSED_IN = "conversion_to_resource"
PATHWAY_STAGE_BETWEEN_POOLS = "resource_to_resource"
PATHWAY_STAGE_FINAL_USE = "resource_to_demand"
PATHWAY_STAGE_PASSED_ON = "resource_to_conversion"
PATHWAY_STAGE_UNALLOCATED = "resource_to_unallocated"
# Every cost endpoint is a Supply or a Demand node; whether a process is a
# converter is evidence in the ledger, not something the chart labels.
PATHWAY_NODE_SUPPLY = "Supply"
PATHWAY_NODE_DEMAND = "Demand"

# Readers see conversion between pools as supply and demand like any other;
# these names replace the internal cascade column names wherever a table is
# shown or exported.  Internal calculations keep the original names.
CONVERSION_COLUMN_NAMES = {
    "passed_in_cost_m": "conversion_supply_cost_m",
    "passed_on_cost_m": "conversion_demand_cost_m",
    "passed_to_pool": "output_pool",
}


def with_conversion_column_names(frame: pd.DataFrame) -> pd.DataFrame:
    """Return *frame* with cascade columns renamed for display and export."""
    return frame.rename(columns=CONVERSION_COLUMN_NAMES)
PATHWAY_NODE_UNALLOCATED = "Unallocated"
_COST_PATHWAY_SUMMARY_COLUMNS = (
    "period", "view", "sign", "source_cost_m", "passed_in_cost_m",
    "selected_allocated_cost_m", "passed_on_cost_m", "unallocated_cost_m",
    "display_value_m", "resource_pool_count", "demand_count",
    "reconciliation_difference_m", "pathway_status",
)
_NODE_END_FIELDS = ("id", "label", "kind", "colour_field", "colour_category", "colour_contributors")


def _orient_net_links(links: pd.DataFrame, tolerance: float) -> pd.DataFrame:
    """Draw each net credit in the reverse direction so Sankey widths stay positive.

    The signed value is kept; only the drawing direction changes, so every
    node still balances.  Node identities are kept, so a code is still drawn
    as one node whichever direction its links point.
    """
    links = links[links["signed_value"].abs().gt(tolerance)].copy()
    reverse = links["signed_value"].lt(0)
    links["net_direction"] = NET_DIRECTION_COST
    links.loc[reverse, "net_direction"] = NET_DIRECTION_CREDIT
    if reverse.any():
        flipped = links.loc[reverse].copy()
        for field in _NODE_END_FIELDS:
            links.loc[reverse, f"source_{field}"] = flipped[f"target_{field}"]
            links.loc[reverse, f"target_{field}"] = flipped[f"source_{field}"]
    links["display_value"] = links["signed_value"].abs()
    return links


_LEDGER_TAXONOMY_COLUMNS = ("process_name", *_PROCESS_TAXONOMY_FIELDS)


def _ledger_taxonomy(lineage: pd.DataFrame, period: int) -> pd.DataFrame:
    """Return one label row per ``(region, process)``, preferring ``period``'s evidence."""
    taxonomy_columns = list(_LEDGER_TAXONOMY_COLUMNS)
    if lineage.empty:
        return pd.DataFrame(columns=["region", "process", *taxonomy_columns])
    taxonomy = lineage.loc[:, [
        column for column in ("period", "region", "process", *taxonomy_columns)
        if column in lineage.columns
    ]].copy()
    for column in taxonomy_columns:
        if column not in taxonomy.columns:
            taxonomy[column] = ""
    for column in ("region", "process"):
        taxonomy[column] = taxonomy[column].fillna("").astype(str)
    taxonomy["_other_period"] = ~pd.to_numeric(
        taxonomy["period"], errors="coerce"
    ).eq(int(period))
    return taxonomy.sort_values("_other_period", kind="stable").drop_duplicates(
        ["region", "process"]
    ).loc[:, ["region", "process", *taxonomy_columns]]


def prepare_supply_chain_cost_pathway_links(
    result: ResourceAllocationResult,
    *,
    period: int,
    resource_pools: Iterable[str] | None = None,
    view: str = UPSTREAM_SUPPLY_VIEW,
    supply_aggregation_field: str | None = None,
    demand_aggregation_field: str | None = None,
    supply_colour_field: str | None = None,
    demand_colour_field: str | None = None,
    sign: str = SIGN_COSTS,
    tolerance: float = 1e-9,
) -> ResourcePathwayResult:
    """Build cost links Supply → Pool → Final use for one cost view.

    Own source cost enters each selected pool from its cost-assignment rows.
    Final use, cost passed on by converters and unallocated cost come from the
    engine ledger (``process_allocations``), so the links equal the headline
    sector totals, build year included.  Cost passed between two selected
    pools is one pool-to-pool link, so a multi-pool Sankey shows the chain
    end to end.  Cost crossing the selection boundary enters from, or leaves
    to, the converter processes that carry it, labelled with the same process
    taxonomy as supply (entering) and demand (leaving) nodes.

    ``sign=SIGN_NET`` sums costs and credits on each displayed link, after
    grouping, and draws a link that nets to a credit in the reverse
    direction.  ``gross_cost_m`` and ``gross_credit_m`` keep the split.
    """
    summary_columns = _COST_PATHWAY_SUMMARY_COLUMNS
    if tolerance < 0:
        raise ValueError("tolerance must be non-negative.")
    if sign not in (*FINANCIAL_SIGNS, SIGN_NET):
        raise ValueError("sign must be Costs, Credits & revenues or Net.")
    if view == SEPARATE_COMMODITY_VIEW:
        raise ValueError(f"Unsupported cost pathway view: {view}.")
    view_cost_attributes(view)
    _validate_pathway_aggregation_field(supply_aggregation_field, endpoint="supply")
    _validate_pathway_aggregation_field(demand_aggregation_field, endpoint="demand")
    _validate_pathway_aggregation_field(supply_colour_field, endpoint="supply colour")
    _validate_pathway_aggregation_field(demand_colour_field, endpoint="demand colour")
    if result.cost_assignments.empty:
        return _empty_pathway(summary_columns)

    selected_pools = None if resource_pools is None else {
        str(pool).strip() for pool in resource_pools if str(pool).strip()
    }

    def selected(pools: pd.Series) -> pd.Series:
        return pools.ne("") & (
            pools.isin(selected_pools) if selected_pools is not None
            else pd.Series(True, index=pools.index)
        )

    assignments = result.cost_assignments[
        result.cost_assignments["period"].eq(int(period))
        & result.cost_assignments["view"].eq(view)
    ].copy()
    assignments["resource_pool"] = assignments["resource_pool"].fillna("")
    assignments = assignments[selected(assignments["resource_pool"])]
    assignments["assigned_cost_m"] = pd.to_numeric(
        assignments["assigned_cost_m"], errors="coerce"
    ).fillna(0.0)
    assignments = assignments[
        assignments["assigned_cost_m"].gt(tolerance) if sign == SIGN_COSTS
        else assignments["assigned_cost_m"].lt(-tolerance) if sign == SIGN_CREDITS
        else assignments["assigned_cost_m"].abs().gt(tolerance)
    ].copy()

    ledger = result.process_allocations
    ledger = ledger[
        pd.to_numeric(ledger["period"], errors="coerce").eq(int(period))
        & ledger["view"].eq(view)
        & (ledger["sign"].eq(sign) if sign != SIGN_NET else True)
        & pd.to_numeric(ledger["allocated_cost_m"], errors="coerce").abs().gt(tolerance)
    ].copy()
    for column in ("resource_pool", "passed_to_pool", "region", "process", "sector"):
        ledger[column] = ledger[column].fillna("").astype(str)
    ledger["allocated_cost_m"] = pd.to_numeric(ledger["allocated_cost_m"], errors="coerce")
    passed = ledger["role"].eq(CASCADE_ROLE_PASSED_ON)
    outflows = ledger[selected(ledger["resource_pool"])]
    inflows = ledger[
        passed & selected(ledger["passed_to_pool"]) & ~selected(ledger["resource_pool"])
    ]
    if assignments.empty and outflows.empty and inflows.empty:
        return _empty_pathway(summary_columns)

    # Ledger endpoints are labelled from the physical evidence, preferring the
    # period itself; a build-year user or converter may only appear earlier.
    taxonomy_columns = list(_LEDGER_TAXONOMY_COLUMNS)
    taxonomy = _ledger_taxonomy(result.quantity_lineage, int(period))

    def labelled(rows: pd.DataFrame) -> pd.DataFrame:
        rows = rows.drop(columns=[c for c in taxonomy_columns if c in rows.columns]).merge(
            taxonomy, on=["region", "process"], how="left"
        )
        for column in taxonomy_columns:
            rows[column] = rows[column].fillna("")
        return rows

    for column in ("region", "process", "process_name", "sector", *_PROCESS_TAXONOMY_FIELDS):
        if column not in assignments.columns:
            assignments[column] = ""

    links: list[dict] = []

    def link(**values) -> None:
        value = float(values.pop("value"))
        parts = pd.to_numeric(values.pop("parts"), errors="coerce").fillna(0.0)
        links.append({
            "period": int(period), "view": view, "sign": sign,
            "signed_value": value, "display_value": abs(value),
            "gross_cost_m": float(parts[parts.gt(0)].sum()),
            "gross_credit_m": float(parts[parts.lt(0)].sum()),
            **values,
        })

    def pool_id(pool: str) -> str:
        return _pool_node_id("supply_chain_cost", pool)

    for resource_pool, rows_by_pool in assignments.groupby("resource_pool", sort=True):
        rows_by_pool = rows_by_pool.copy()
        rows_by_pool["_display"] = _activity_display_labels(
            rows_by_pool, supply_aggregation_field
        )
        group_columns = ["_display"] if supply_aggregation_field else ["region", "process"]
        for _, rows in rows_by_pool.groupby(group_columns, sort=True, dropna=False):
            row = rows.iloc[0]
            label = str(row["_display"])
            value = float(rows["assigned_cost_m"].sum())
            colour = _activity_colour_metadata(rows, supply_colour_field)
            is_aggregate = supply_aggregation_field is not None
            attributes = ", ".join(sorted({
                str(attribute).strip()
                for attribute in rows.get("attribute", pd.Series(dtype=str))
                if str(attribute).strip()
            }))
            link(
                stage=PATHWAY_STAGE_SOURCE,
                source_id=_cost_endpoint_node_id(supply_aggregation_field, label, row),
                source_label=label, source_kind=PATHWAY_NODE_SUPPLY,
                target_id=pool_id(resource_pool), target_label=resource_pool,
                target_kind="Resource pool", resource_pool=resource_pool, value=value,
                parts=rows["assigned_cost_m"],
                region="" if is_aggregate else row.get("region", ""),
                process="" if is_aggregate else row.get("process", ""),
                sector="" if is_aggregate else row.get("sector", ""),
                source_colour_field=colour["field"],
                source_colour_category=colour["category"],
                source_colour_contributors=colour["contributors"],
                detail=(
                    f"Supply to {resource_pool}: £{value:,.3f}M/year; "
                    f"attributes: {attributes or 'not recorded'}; "
                    f"displayed by: {supply_aggregation_field or 'Individual processes'}; "
                    f"{_activity_contributor_detail(rows)}"
                ),
            )

    def converter_links(
        rows_by_pool: pd.DataFrame,
        *,
        entering: bool,
        resource_pool: str,
    ) -> None:
        """Draw cost crossing the selection boundary at its converter processes."""
        field = supply_aggregation_field if entering else demand_aggregation_field
        colour_field = supply_colour_field if entering else demand_colour_field
        rows_by_pool = rows_by_pool.copy()
        rows_by_pool["_display"] = _activity_display_labels(rows_by_pool, field)
        is_aggregate = field is not None
        group_columns = ["_display"] if is_aggregate else ["region", "process"]
        for _, rows in rows_by_pool.groupby(group_columns, sort=True, dropna=False):
            row = rows.iloc[0]
            label = str(row["_display"])
            value = float(rows["allocated_cost_m"].sum())
            colour = _activity_colour_metadata(rows, colour_field)
            node_id = _cost_endpoint_node_id(field, label, row)
            pool_node = {"id": pool_id(resource_pool), "label": resource_pool, "kind": "Resource pool"}
            converter_node = {
                "id": node_id, "label": label,
                "kind": PATHWAY_NODE_SUPPLY if entering else PATHWAY_NODE_DEMAND,
            }
            source, target = (converter_node, pool_node) if entering else (pool_node, converter_node)
            side = "source" if entering else "target"
            description = (
                f"Supply to {resource_pool}: £{value:,.3f}M/year" if entering
                else f"Demand on {resource_pool}: £{value:,.3f}M/year"
            )
            link(
                stage=PATHWAY_STAGE_PASSED_IN if entering else PATHWAY_STAGE_PASSED_ON,
                source_id=source["id"], source_label=source["label"], source_kind=source["kind"],
                target_id=target["id"], target_label=target["label"], target_kind=target["kind"],
                resource_pool=resource_pool, value=value, parts=rows["allocated_cost_m"],
                region="" if is_aggregate else row.get("region", ""),
                process="" if is_aggregate else row.get("process", ""),
                sector="" if is_aggregate else row.get("sector", ""),
                **{
                    f"{side}_colour_field": colour["field"],
                    f"{side}_colour_category": colour["category"],
                    f"{side}_colour_contributors": colour["contributors"],
                },
                detail=(
                    f"{description}; displayed by: {field or 'Individual processes'}; "
                    f"{_activity_contributor_detail(rows)}"
                ),
            )

    for resource_pool, rows in labelled(inflows).groupby("passed_to_pool", sort=True):
        converter_links(rows, entering=True, resource_pool=resource_pool)

    final_rows = labelled(outflows[outflows["role"].eq(CASCADE_ROLE_FINAL)])
    for resource_pool, rows_by_pool in final_rows.groupby("resource_pool", sort=True):
        rows_by_pool = rows_by_pool.copy()
        rows_by_pool["_display"] = _activity_display_labels(
            rows_by_pool, demand_aggregation_field
        )
        pool_total = float(rows_by_pool["allocated_cost_m"].sum())
        group_columns = ["_display"] if demand_aggregation_field else ["region", "process"]
        for _, rows in rows_by_pool.groupby(group_columns, sort=True, dropna=False):
            row = rows.iloc[0]
            label = str(row["_display"])
            value = float(rows["allocated_cost_m"].sum())
            colour = _activity_colour_metadata(rows, demand_colour_field)
            is_aggregate = demand_aggregation_field is not None
            link(
                stage=PATHWAY_STAGE_FINAL_USE,
                source_id=pool_id(resource_pool), source_label=resource_pool,
                source_kind="Resource pool",
                target_id=_cost_endpoint_node_id(demand_aggregation_field, label, row),
                target_label=label, target_kind=PATHWAY_NODE_DEMAND,
                resource_pool=resource_pool, value=value, parts=rows["allocated_cost_m"],
                region="" if is_aggregate else row.get("region", ""),
                process="" if is_aggregate else row.get("process", ""),
                sector="" if is_aggregate else row.get("sector", ""),
                target_colour_field=colour["field"],
                target_colour_category=colour["category"],
                target_colour_contributors=colour["contributors"],
                detail=(
                    f"Demand on {resource_pool}: £{value:,.3f}M/year"
                    + (f" ({value / pool_total:.2%} of its final use); " if pool_total else "; ")
                    + f"displayed by: {demand_aggregation_field or 'Individual processes'}; "
                    f"{_activity_contributor_detail(rows)}"
                ),
            )

    passed_rows = outflows[outflows["role"].eq(CASCADE_ROLE_PASSED_ON)]
    inside_selection = (
        pd.Series(True, index=passed_rows.index) if selected_pools is None
        else passed_rows["passed_to_pool"].isin(selected_pools)
    )
    for (resource_pool, target), rows in passed_rows[inside_selection].groupby(
        ["resource_pool", "passed_to_pool"], sort=True
    ):
        value = float(rows["allocated_cost_m"].sum())
        link(
            stage=PATHWAY_STAGE_BETWEEN_POOLS,
            source_id=pool_id(resource_pool), source_label=resource_pool,
            source_kind="Resource pool",
            target_id=pool_id(target), target_label=target, target_kind="Resource pool",
            resource_pool=resource_pool, value=value, parts=rows["allocated_cost_m"],
            detail=(
                f"{resource_pool} → {target}: "
                f"£{value:,.3f}M/year; processes: "
                + ", ".join(sorted(set(rows["process"])))
            ),
        )
    for resource_pool, rows in labelled(passed_rows[~inside_selection]).groupby(
        "resource_pool", sort=True
    ):
        converter_links(rows, entering=False, resource_pool=resource_pool)

    for resource_pool, rows in outflows[
        outflows["role"].eq(CASCADE_ROLE_UNALLOCATED)
    ].groupby("resource_pool", sort=True):
        value = float(rows["allocated_cost_m"].sum())
        link(
            stage=PATHWAY_STAGE_UNALLOCATED,
            source_id=pool_id(resource_pool), source_label=resource_pool,
            source_kind="Resource pool",
            target_id="cost_unallocated", target_label=PATHWAY_NODE_UNALLOCATED,
            target_kind=PATHWAY_NODE_UNALLOCATED, resource_pool=resource_pool, value=value,
            parts=rows["allocated_cost_m"],
            detail=(
                f"{resource_pool} cost with no route to final use in its share-basis "
                f"year: £{value:,.3f}M/year"
            ),
        )

    link_frame = pd.DataFrame(links, columns=list(_PATHWAY_LINK_COLUMNS))
    group_columns = [
        column for column in _PATHWAY_LINK_COLUMNS
        if column not in _PATHWAY_LINK_VALUE_COLUMNS
    ]
    link_frame = link_frame.fillna({
        column: "" for column in group_columns if column != "period"
    }).groupby(group_columns, as_index=False, dropna=False).agg(
        signed_value=("signed_value", "sum"),
        display_value=("display_value", "sum"),
        gross_cost_m=("gross_cost_m", "sum"),
        gross_credit_m=("gross_credit_m", "sum"),
        detail=("detail", lambda values: "; ".join(sorted(set(map(str, values))))),
    )
    link_frame["net_direction"] = ""
    link_frame = link_frame.loc[:, list(_PATHWAY_LINK_COLUMNS)]
    demand_count = int(link_frame.loc[
        link_frame["stage"].eq(PATHWAY_STAGE_FINAL_USE), "target_id"
    ].nunique())
    if sign == SIGN_NET:
        link_frame = _orient_net_links(link_frame, tolerance)

    def stage_total(stage: str, column: str = "signed_value") -> float:
        return float(link_frame.loc[link_frame["stage"].eq(stage), column].sum())

    source_total = float(assignments["assigned_cost_m"].sum())
    passed_in_total = stage_total(PATHWAY_STAGE_PASSED_IN)
    final_total = stage_total(PATHWAY_STAGE_FINAL_USE)
    passed_on_total = stage_total(PATHWAY_STAGE_PASSED_ON)
    unallocated_total = stage_total(PATHWAY_STAGE_UNALLOCATED)
    # Own cost (from cost assignments) plus cost entering from outside the
    # selection must equal what the ledger books, passes out or leaves unallocated.
    difference = (
        source_total + passed_in_total - final_total - passed_on_total - unallocated_total
    )
    summary = pd.DataFrame([{
        "period": int(period), "view": view, "sign": sign,
        "source_cost_m": source_total,
        "passed_in_cost_m": passed_in_total,
        "selected_allocated_cost_m": final_total,
        "passed_on_cost_m": passed_on_total,
        "unallocated_cost_m": unallocated_total,
        "display_value_m": stage_total(PATHWAY_STAGE_FINAL_USE, "display_value"),
        "resource_pool_count": int(link_frame["resource_pool"].nunique()),
        "demand_count": demand_count,
        "reconciliation_difference_m": difference,
        "pathway_status": "Available" if abs(difference) <= max(tolerance, 1e-9 * max(
            1.0, abs(source_total) + abs(passed_in_total)
        )) else "Check required",
    }], columns=list(summary_columns))
    return ResourcePathwayResult(
        links=link_frame.sort_values(
            ["resource_pool", "stage", "display_value"], ascending=[True, True, False]
        ).reset_index(drop=True),
        summary=summary,
        diagnostics=pd.DataFrame(columns=list(_PATHWAY_DIAGNOSTIC_COLUMNS)),
    )


_FUNCTION_TAG_ALIASES = {
    "final consumption": "final consumption",
    "throughput": "throughput",
    "transformed": "transformed",
    "transformation": "transformed",
}


def parse_commodity_function_tags(value: object) -> tuple[str, ...]:
    """Normalise compound commodity-function labels without discarding evidence."""
    parts = re.split(r"\s*(?:/|;|\||,)\s*", str(value or "").strip())
    tags = []
    for part in parts:
        tag = " ".join(part.casefold().split())
        if tag:
            tags.append(_FUNCTION_TAG_ALIASES.get(tag, tag))
    return tuple(dict.fromkeys(tags))


def _fuel_evidence(result: ResourceAllocationResult) -> pd.DataFrame:
    """Return physical evidence with stable selection and calculation fields.

    The frame is cached on ``result``; treat it as read-only.
    """
    return result.fuel_evidence


def _build_fuel_evidence(result: ResourceAllocationResult) -> pd.DataFrame:
    """Normalise ``quantity_lineage`` into the shared physical evidence frame."""
    lineage = result.quantity_lineage.copy()
    defaults = {
        "period": pd.NA, "region": "", "process": "", "process_name": "",
        "commodity": "", "commodity_name": "", "commodity_subgroup": "",
        "commodity_group": "", "commodity_function": "", "resource_pool": "",
        "direction": "", "value": pd.NA, "unit": "", "unit_factor": pd.NA,
        "quantity_twh": pd.NA, "active": False, "timeslice_mode": "",
        "vd_row_count": 0, "connection_role": "", "connection_role_reason": "",
        "link_role": "outside_selected_boundary", "depth": pd.NA,
    }
    defaults.update(dict.fromkeys(_PROCESS_TAXONOMY_FIELDS, ""))
    for column, default in defaults.items():
        if column not in lineage.columns:
            lineage[column] = default
    if lineage.empty:
        return lineage
    lineage["period"] = pd.to_numeric(lineage["period"], errors="coerce").astype("Int64")
    lineage["quantity_twh"] = pd.to_numeric(lineage["quantity_twh"], errors="coerce")
    lineage["value"] = pd.to_numeric(lineage["value"], errors="coerce")
    lineage["depth"] = pd.to_numeric(lineage["depth"], errors="coerce").astype("Int64")
    for column in (
        "region", "process", "process_name", "commodity", "commodity_name",
        "commodity_subgroup", "commodity_group", "commodity_function", "resource_pool",
        "direction", "unit", "timeslice_mode", "connection_role",
        "connection_role_reason", "link_role", *_PROCESS_TAXONOMY_FIELDS,
    ):
        lineage[column] = lineage[column].fillna("").astype(str).str.strip()
    # One regex pass per distinct declaration rather than one per lineage row.
    tag_lookup = {
        value: "; ".join(parse_commodity_function_tags(value))
        for value in lineage["commodity_function"].unique()
    }
    lineage["function_tags"] = lineage["commodity_function"].map(tag_lookup)
    return lineage


def fuel_pathway_periods(result: ResourceAllocationResult, tolerance: float = 1e-9) -> list[int]:
    """Return model periods containing positive, mapped, compatible fuel evidence."""
    evidence = _fuel_evidence(result)
    if evidence.empty:
        return []
    eligible = evidence[
        evidence["resource_pool"].ne("")
        & evidence["commodity_name"].ne("")
        & evidence["unit_factor"].notna()
        & evidence["quantity_twh"].gt(tolerance)
    ]
    return sorted(eligible["period"].dropna().astype(int).unique().tolist())


def fuel_pathway_resource_pools(
    result: ResourceAllocationResult,
    period: int,
    tolerance: float = 1e-9,
) -> list[str]:
    """Return reviewed resource pools with active physical evidence in one period."""
    evidence = _fuel_evidence(result)
    eligible = evidence[
        evidence["period"].eq(int(period))
        & evidence["resource_pool"].ne("")
        & evidence["commodity_name"].ne("")
        & evidence["unit_factor"].notna()
        & evidence["quantity_twh"].gt(tolerance)
    ]
    return sorted(eligible["resource_pool"].unique().tolist(), key=str.casefold)


def fuel_pathway_fuels(
    result: ResourceAllocationResult,
    period: int,
    resource_pool: str,
    tolerance: float = 1e-9,
) -> list[str]:
    """Return individual authoritative fuel names available within one pool."""
    evidence = _fuel_evidence(result)
    eligible = evidence[
        evidence["period"].eq(int(period))
        & evidence["resource_pool"].eq(str(resource_pool).strip())
        & evidence["commodity_name"].ne("")
        & evidence["unit_factor"].notna()
        & evidence["quantity_twh"].gt(tolerance)
    ]
    return sorted(eligible["commodity_name"].unique().tolist(), key=str.casefold)


def cost_pathway_resource_pools(
    result: ResourceAllocationResult,
    period: int,
    *,
    view: str = UPSTREAM_SUPPLY_VIEW,
    tolerance: float = 1e-9,
) -> list[str]:
    """Return pools whose cost Sankey has something to draw in one period.

    A pool qualifies when cost enters it: its own source cost or cost passed
    in by converters (``pool_cost_m``).  Cost that cannot reach final use is
    drawn as unallocated, so it is not a reason to hide the pool.  Pools with
    physical flow but no cost belong only to the physical view.
    """
    summary = result.pool_summary
    if summary.empty:
        return []
    rows = summary[
        summary["view"].eq(view)
        & pd.to_numeric(summary["period"], errors="coerce").eq(int(period))
        & (
            pd.to_numeric(summary["source_cost_m"], errors="coerce").abs().gt(tolerance)
            | pd.to_numeric(
                summary.get("passed_in_cost_m", pd.Series(0.0, index=summary.index)),
                errors="coerce",
            ).abs().gt(tolerance)
        )
    ]
    return sorted(rows["resource_pool"].unique().tolist(), key=str.casefold)


def resource_pools_of_kind(result: ResourceAllocationResult, kind: str) -> set[str]:
    """Return every pool the curated sheet declares as ``kind`` for this result."""
    mapping = result.commodity_mapping
    if mapping.empty or "pool_kind" not in mapping.columns:
        return set()
    return set(mapping.loc[mapping["pool_kind"].eq(kind), "resource_pool"]) - {""}


_FUEL_SUMMARY_TEXT_COLUMNS = ("period", "resource_pool", "commodity_name", "pathway_status")


def prepare_pool_fuel_pathway_links(
    result: ResourceAllocationResult,
    *,
    period: int,
    resource_pool: str,
    commodity_names: list[str] | None = None,
    all_label: str = "All fuels",
    **options,
) -> FuelPathwayResult:
    """Build the physical pathway for several fuels in one pool at once.

    ``commodity_names=None`` means every fuel with evidence in the pool.  Each
    fuel is netted on its own first, so same-fuel pass-through is suppressed
    per fuel before anything is summed; each fuel keeps its own middle node.
    """
    names = (
        fuel_pathway_fuels(result, period, resource_pool)
        if commodity_names is None else list(dict.fromkeys(commodity_names))
    )
    if len(names) == 1:
        return prepare_fuel_pathway_links(
            result, period=period, resource_pool=resource_pool,
            commodity_name=names[0], **options,
        )
    if not names:
        return _empty_fuel_pathway()
    parts = [
        prepare_fuel_pathway_links(
            result, period=period, resource_pool=resource_pool,
            commodity_name=name, **options,
        )
        for name in names
    ]

    def combined(attribute: str) -> pd.DataFrame:
        frames = [getattr(part, attribute) for part in parts]
        frames = [frame for frame in frames if not frame.empty]
        if not frames:
            return getattr(parts[0], attribute)
        return pd.concat(frames, ignore_index=True)

    links = combined("links")
    if not links.empty:
        links = links.sort_values(
            ["stage", "display_value", "process"], ascending=[True, False, True]
        ).reset_index(drop=True)
    summaries = combined("summary")
    if summaries.empty:
        summary = summaries
    else:
        numeric = [
            column for column in _FUEL_SUMMARY_COLUMNS
            if column not in _FUEL_SUMMARY_TEXT_COLUMNS
        ]
        totals = summaries[numeric].apply(pd.to_numeric, errors="coerce").sum()
        record = {column: totals[column] for column in numeric}
        record.update({
            "period": int(period),
            "resource_pool": str(resource_pool).strip(),
            "commodity_name": all_label if commodity_names is None else "; ".join(names),
            "pathway_status": (
                "Available" if not links.empty else "No material net selected-fuel flow"
            ),
        })
        summary = pd.DataFrame([record], columns=list(_FUEL_SUMMARY_COLUMNS))
    return FuelPathwayResult(
        links=links,
        summary=summary,
        source_detail=combined("source_detail"),
        demand_detail=combined("demand_detail"),
        commodity_balance=combined("commodity_balance"),
        unmatched_detail=combined("unmatched_detail"),
        hidden_detail=combined("hidden_detail"),
        diagnostics=combined("diagnostics"),
        process_net_detail=combined("process_net_detail"),
    )


def _fuel_flow_role(row: pd.Series) -> str:
    """Classify an observed edge from authoritative labels and scope metadata."""
    direction = str(row.get("direction", "")).upper()
    connection_role = str(row.get("connection_role", "")).casefold()
    link_role = str(row.get("link_role", "")).casefold()
    tags = set(parse_commodity_function_tags(row.get("commodity_function", "")))
    taxonomy = " ".join(
        str(row.get(column, "") or "")
        for column in (
            "process_name", "process_group", "process_category", "process_subsector",
            "process_sector", "process_useful_output",
        )
    ).casefold()
    if direction == "IN" and (
        connection_role == "final energy use" or link_role == "end_use_input"
    ):
        return "Final consumption"
    if "export" in taxonomy:
        return "Export"
    if "import" in taxonomy or "re-import" in taxonomy:
        return "Import"
    if "storage" in taxonomy:
        return "Storage"
    if any(term in taxonomy for term in ("distribution", "network", "terminal", "refuelling")):
        return "Distribution / throughput"
    if connection_role == "throughput / network":
        return "Distribution / throughput"
    if direction == "IN" and connection_role == "transformation input":
        return "Transformation"
    if any(term in taxonomy for term in ("refinery", "conversion", "transformation", "processing")):
        return "Transformation"
    if direction == "IN" and "final consumption" in tags:
        return "Final consumption"
    if direction == "OUT" and connection_role == "production output":
        return "Origin supply"
    if "throughput" in tags:
        return "Distribution / throughput"
    return "Unclassified"


def _fuel_stakeholder_visibility(row: pd.Series) -> tuple[str, str]:
    """Return the reviewed stakeholder-display decision for one valid edge.

    Extraction is identified from exact authoritative subsector values. The
    UK TIMES re-import loop is labelled with process subsector
    ``Fuel Re-imports``; its export-side leg is hidden as a dummy export and
    the paired import-side re-import leg remains visible.
    """
    subsector = str(row.get("process_subsector", "") or "").strip().casefold()
    flow_role = str(row.get("flow_role", "") or "").strip()
    if subsector in _EXTRACTION_SUBSECTORS:
        return (
            _FUEL_VISIBILITY_EXTRACTION,
            "Authoritative process subsector identifies domestic fuel extraction.",
        )
    if subsector == _RE_IMPORT_LOOP_SUBSECTOR and flow_role == "Export":
        return (
            _FUEL_VISIBILITY_DUMMY_EXPORT,
            "Export leg of the re-import loop (process subsector Fuel Re-imports).",
        )
    return _FUEL_VISIBILITY_DISPLAYED, "Included in the stakeholder physical pathway."


def _annotate_fuel_evidence(rows: pd.DataFrame, tolerance: float) -> pd.DataFrame:
    """Attach calculation and stakeholder-display status to selected evidence."""
    annotated = rows.copy()
    annotated["reported_value"] = annotated["value"]
    annotated["flow_role"] = annotated.apply(_fuel_flow_role, axis=1)
    compatible = annotated["unit_factor"].notna()
    positive = annotated["quantity_twh"].gt(tolerance)
    annotated["calculation_status"] = _FUEL_CALCULATION_INCLUDED
    annotated["calculation_reason"] = "Positive compatible VDT-matched energy evidence."
    annotated.loc[~compatible, "calculation_status"] = "omitted_incompatible_unit"
    annotated.loc[~compatible, "calculation_reason"] = "Incompatible or missing energy unit"
    annotated.loc[compatible & ~positive, "calculation_status"] = "omitted_non_positive"
    annotated.loc[compatible & ~positive, "calculation_reason"] = (
        "Zero or negative observed quantity"
    )
    decisions = annotated.apply(_fuel_stakeholder_visibility, axis=1)
    annotated["stakeholder_visibility"] = [decision[0] for decision in decisions]
    annotated["stakeholder_visibility_reason"] = [decision[1] for decision in decisions]
    return annotated


def _process_pathway_label(row: pd.Series, *, technical: bool) -> str:
    code = _node_text(row.get("process"), "Unlabelled process")
    name = _node_text(row.get("process_name"), code)
    return f"{name} ({code})" if technical and name != code else name


def _detail_frame(
    rows: pd.DataFrame,
    tolerance: float,
    *,
    technical: bool = False,
) -> pd.DataFrame:
    if rows.empty:
        return pd.DataFrame(columns=list(_FUEL_DETAIL_COLUMNS))
    detail = rows.copy()
    if "calculation_status" not in detail.columns:
        detail = _annotate_fuel_evidence(detail, tolerance)
    detail["included_in_graph"] = detail["calculation_status"].eq(
        _FUEL_CALCULATION_INCLUDED
    ) & (
        technical
        | detail["stakeholder_visibility"].eq(_FUEL_VISIBILITY_DISPLAYED)
    )
    stakeholder_exclusion = (
        pd.Series("", index=detail.index)
        if technical else
        detail["stakeholder_visibility_reason"].where(
            ~detail["stakeholder_visibility"].eq(_FUEL_VISIBILITY_DISPLAYED), ""
        )
    )
    detail["exclusion_reason"] = detail["calculation_reason"].where(
        ~detail["calculation_status"].eq(_FUEL_CALCULATION_INCLUDED),
        stakeholder_exclusion,
    )
    for column in _FUEL_DETAIL_COLUMNS:
        if column not in detail.columns:
            detail[column] = ""
    return detail.loc[:, list(_FUEL_DETAIL_COLUMNS)].sort_values(
        ["commodity", "region", "process"]
    ).reset_index(drop=True)


def _selected_unmatched_detail(
    result: ResourceAllocationResult,
    unmatched_flows: pd.DataFrame | None,
    *,
    period: int,
    resource_pool: str,
    commodity_name: str,
) -> pd.DataFrame:
    unmatched = _prepare_unmatched_fuel_evidence(result, unmatched_flows)
    if unmatched.empty:
        return unmatched
    unmatched = unmatched[
        pd.to_numeric(unmatched["period"], errors="coerce").eq(int(period))
        & unmatched["resource_pool"].fillna("").eq(resource_pool)
        & unmatched["commodity_name"].fillna("").eq(commodity_name)
    ].copy()
    return unmatched.reset_index(drop=True)


def _prepare_unmatched_fuel_evidence(
    result: ResourceAllocationResult,
    unmatched_flows: pd.DataFrame | None,
) -> pd.DataFrame:
    """Attach reviewed fuel metadata to unmatched rows once for reuse."""
    if unmatched_flows is None or unmatched_flows.empty:
        return pd.DataFrame(columns=list(_FUEL_UNMATCHED_COLUMNS))
    unmatched = unmatched_flows.copy()
    mapping_columns = [
        "commodity", "resource_pool", "commodity_name", "commodity_function", "unit",
    ]
    mapping = result.commodity_mapping.copy()
    for column in mapping_columns:
        if column not in mapping.columns:
            mapping[column] = ""
    for column in mapping_columns[1:]:
        if column in unmatched.columns:
            unmatched = unmatched.drop(columns=column)
    unmatched["commodity"] = _normalise_code(unmatched.get(
        "commodity", pd.Series("", index=unmatched.index)
    ))
    unmatched = unmatched.merge(mapping[mapping_columns], on="commodity", how="left")
    for column in _FUEL_UNMATCHED_COLUMNS:
        if column not in unmatched.columns:
            unmatched[column] = ""
    return unmatched.loc[:, list(_FUEL_UNMATCHED_COLUMNS)].reset_index(drop=True)


def _fuel_node_id(resource_pool: object, commodity_name: object) -> str:
    """Return the stable middle-node identity for one pooled individual fuel."""
    return (
        f"fuel|{_node_text(resource_pool, 'Unmapped').upper()}|"
        f"{_node_text(commodity_name, 'Unlabelled fuel').upper()}"
    )


def _fuel_process_display(
    row: pd.Series,
    field: str | None,
    *,
    technical: bool,
    stage: str = "fuel_process",
) -> tuple[str, str]:
    """Return one process-side node id and label for the requested projection."""
    if field is None:
        return (
            _process_node_id(stage, row.get("region"), row.get("process")),
            _process_pathway_label(row, technical=technical),
        )
    label = _node_text(row.get(field), "Unlabelled")
    node_id = (
        f"{stage}_group|{_node_text(row.get('region'), 'UNSPECIFIED').upper()}|"
        f"{field.upper()}|{label.upper()}"
    )
    return node_id, label


def _join_fuel_values(values: pd.Series) -> str:
    """Join unique non-empty evidence values deterministically."""
    return "; ".join(sorted({
        str(value).strip() for value in values if str(value).strip()
    }, key=str.casefold))


def _aggregate_pooled_fuel_links(links: pd.DataFrame) -> pd.DataFrame:
    """Aggregate already-netted endpoints without changing their fuel boundary."""
    if links.empty:
        return pd.DataFrame(columns=list(_FUEL_PATHWAY_LINK_COLUMNS))
    group_columns = [
        "period", "view", "sign", "stage", "source_id", "source_label",
        "source_kind", "target_id", "target_label", "target_kind",
        "resource_pool", "commodity_name", "direction", "region", "unit",
        "display_mode", "process_grouping_field", "process_grouping_label",
    ]
    records: list[dict] = []
    for keys, rows in links.groupby(group_columns, dropna=False, sort=False):
        record = dict(zip(group_columns, keys))
        processes = rows[["process", "process_name"]].drop_duplicates()
        contributor_labels = _activity_display_labels(processes, None).tolist()
        exact_codes = sorted({
            code.strip()
            for value in rows["exact_commodity_codes"]
            for code in str(value).split(";")
            if code.strip()
        }, key=str.casefold)
        depths = pd.to_numeric(rows["depth"], errors="coerce").dropna()
        value = float(rows["display_value"].sum())

        def colour_metadata(endpoint: str) -> dict[str, str]:
            field = _join_fuel_values(rows[f"{endpoint}_colour_field"])
            contributors = sorted({
                value.strip()
                for values in rows[f"{endpoint}_colour_contributors"]
                for value in str(values).split(";")
                if value.strip()
            })
            categories = sorted({
                str(value).strip()
                for value in rows[f"{endpoint}_colour_category"]
                if str(value).strip()
            })
            category = (
                "Mixed" if "Mixed" in categories or len(categories) > 1
                else categories[0] if categories else ""
            )
            return {
                f"{endpoint}_colour_field": field,
                f"{endpoint}_colour_category": category,
                f"{endpoint}_colour_contributors": "; ".join(contributors),
            }

        record.update({
            "source_role": _join_fuel_values(rows["source_role"]),
            "target_role": _join_fuel_values(rows["target_role"]),
            "commodity": "; ".join(exact_codes),
            "exact_commodity_codes": "; ".join(exact_codes),
            "exact_commodity_count": len(exact_codes),
            "commodity_function": _join_fuel_values(rows["commodity_function"]),
            "function_tags": _join_fuel_values(rows["function_tags"]),
            "flow_role": _join_fuel_values(rows["flow_role"]),
            "link_role": _join_fuel_values(rows["link_role"]),
            "depth": int(depths.min()) if not depths.empty else pd.NA,
            "process": _join_fuel_values(rows["process"]),
            "process_name": _join_fuel_values(rows["process_name"]),
            "sector": _join_fuel_values(rows["sector"]),
            "gross_output_twh": float(rows["gross_output_twh"].sum()),
            "gross_input_twh": float(rows["gross_input_twh"].sum()),
            "net_flow_twh": float(rows["net_flow_twh"].sum()),
            "signed_value": float(rows["signed_value"].sum()),
            "display_value": value,
            "timeslice_mode": _join_fuel_values(rows["timeslice_mode"]),
            "vd_row_count": int(pd.to_numeric(
                rows["vd_row_count"], errors="coerce"
            ).fillna(0).sum()),
            "stakeholder_visibility": _join_fuel_values(
                rows["stakeholder_visibility"]
            ),
            "stakeholder_visibility_reason": _join_fuel_values(
                rows["stakeholder_visibility_reason"]
            ),
            "grouped_process_count": int(len(processes)),
            "grouped_processes": "; ".join(contributor_labels),
            **colour_metadata("source"),
            **colour_metadata("target"),
            "detail": (
                f"Net selected-fuel flow: {value:,.6f} TWh/year; gross output "
                f"{float(rows['gross_output_twh'].sum()):,.6f}; gross input "
                f"{float(rows['gross_input_twh'].sum()):,.6f}; contributors: "
                f"{'; '.join(contributor_labels)}"
            ),
        })
        records.append(record)
    return pd.DataFrame(records, columns=list(_FUEL_PATHWAY_LINK_COLUMNS))


def _prepare_fuel_process_nets(
    rows: pd.DataFrame,
    *,
    projection: str,
    display_mode: str,
    technical: bool = False,
    supply_aggregation_field: str | None = None,
    demand_aggregation_field: str | None = None,
    tolerance: float = 1e-9,
) -> pd.DataFrame:
    """Build process nets for one or many fuel pathway selections at once."""
    if rows.empty:
        return pd.DataFrame(columns=list(_FUEL_PROCESS_NET_COLUMNS))
    prepared = rows.copy()
    keys = [
        "period", "resource_pool", "commodity_name", "region", "process",
    ]
    metadata_columns = [
        "process_name", *_PROCESS_TAXONOMY_FIELDS,
        "stakeholder_visibility", "stakeholder_visibility_reason",
    ]
    for column in [*keys, *metadata_columns]:
        if column not in prepared.columns:
            prepared[column] = ""
    prepared["_gross_output_twh"] = prepared["quantity_twh"].where(
        prepared["direction"].eq("OUT"), 0.0
    )
    prepared["_gross_input_twh"] = prepared["quantity_twh"].abs().where(
        prepared["direction"].eq("IN"), 0.0
    )
    aggregations: dict[str, tuple[str, object]] = {
        "gross_output_twh": ("_gross_output_twh", "sum"),
        "gross_input_twh": ("_gross_input_twh", "sum"),
        "flow_role": ("flow_role", _join_fuel_values),
        "exact_commodity_codes": ("commodity", _join_fuel_values),
        "vd_row_count": ("vd_row_count", "sum"),
    }
    aggregations.update({column: (column, "first") for column in metadata_columns})
    nets = prepared.groupby(keys, as_index=False, dropna=False, sort=False).agg(
        **aggregations
    )
    nets["net_flow_twh"] = nets["gross_output_twh"] - nets["gross_input_twh"]
    nets["endpoint_side"] = "Pass-through"
    nets.loc[nets["net_flow_twh"].gt(tolerance), "endpoint_side"] = "Supply"
    nets.loc[nets["net_flow_twh"].lt(-tolerance), "endpoint_side"] = "Demand"
    nets["pass_through_suppressed"] = nets["endpoint_side"].eq("Pass-through")
    nets["exact_commodity_count"] = nets["exact_commodity_codes"].map(
        lambda value: len([code for code in str(value).split(";") if code.strip()])
    )
    nets["projection"] = projection
    nets["display_mode"] = display_mode
    nets["stakeholder_visibility"] = nets["stakeholder_visibility"].fillna(
        _FUEL_VISIBILITY_DISPLAYED
    )
    nets["stakeholder_visibility_reason"] = nets[
        "stakeholder_visibility_reason"
    ].fillna("")
    nets["process_grouping_field"] = ""
    nets.loc[nets["endpoint_side"].eq("Supply"), "process_grouping_field"] = (
        supply_aggregation_field or ""
    )
    nets.loc[nets["endpoint_side"].eq("Demand"), "process_grouping_field"] = (
        demand_aggregation_field or ""
    )
    nets["process_grouping_label"] = _activity_display_labels(nets, None)
    for endpoint, taxonomy_field in (
        ("Supply", supply_aggregation_field), ("Demand", demand_aggregation_field),
    ):
        if taxonomy_field:
            mask = nets["endpoint_side"].eq(endpoint)
            nets.loc[mask, "process_grouping_label"] = _activity_display_labels(
                nets.loc[mask], taxonomy_field
            )
    for column in _FUEL_PROCESS_NET_COLUMNS:
        if column not in nets.columns:
            nets[column] = ""
    return nets.loc[:, list(_FUEL_PROCESS_NET_COLUMNS)]


def _prepare_fuel_commodity_balances(
    graph_rows: pd.DataFrame,
    *,
    tolerance: float = 1e-9,
) -> pd.DataFrame:
    """Build exact-code balances for one or many fuel selections at once."""
    if graph_rows.empty:
        return pd.DataFrame(columns=list(_FUEL_BALANCE_COLUMNS))
    rows = graph_rows.copy()
    keys = ["period", "resource_pool", "commodity_name", "commodity"]
    output = rows["direction"].eq("OUT")
    inputs = rows["direction"].eq("IN")
    process_token = rows["region"].astype(str) + "\x00" + rows["process"].astype(str)
    rows["_gross_output_twh"] = rows["quantity_twh"].where(output, 0.0)
    rows["_gross_input_twh"] = rows["quantity_twh"].where(inputs, 0.0)
    rows["_output_process"] = process_token.where(output)
    rows["_input_process"] = process_token.where(inputs)
    rows["_origin_import_twh"] = rows["quantity_twh"].where(
        output & rows["flow_role"].eq("Import"), 0.0
    )
    rows["_final_consumption_twh"] = rows["quantity_twh"].where(
        inputs & rows["flow_role"].eq("Final consumption"), 0.0
    )
    rows["_exports_twh"] = rows["quantity_twh"].where(
        inputs & rows["flow_role"].eq("Export"), 0.0
    )
    rows["_transformation_distribution_twh"] = rows["quantity_twh"].where(
        inputs & rows["flow_role"].isin(
            ["Transformation", "Distribution / throughput", "Storage"]
        ),
        0.0,
    )
    rows["_unclassified_input_twh"] = rows["quantity_twh"].where(
        inputs & rows["flow_role"].eq("Unclassified"), 0.0
    )
    balance = rows.groupby(keys, as_index=False, dropna=False, sort=True).agg(
        commodity_function=("commodity_function", "first"),
        function_tags=("function_tags", "first"),
        gross_output_twh=("_gross_output_twh", "sum"),
        gross_input_twh=("_gross_input_twh", "sum"),
        output_process_count=("_output_process", "nunique"),
        input_process_count=("_input_process", "nunique"),
        origin_import_twh=("_origin_import_twh", "sum"),
        final_consumption_twh=("_final_consumption_twh", "sum"),
        exports_twh=("_exports_twh", "sum"),
        transformation_distribution_twh=("_transformation_distribution_twh", "sum"),
        unclassified_input_twh=("_unclassified_input_twh", "sum"),
    )
    balance["residual_twh"] = (
        balance["gross_output_twh"] - balance["gross_input_twh"]
    )
    scale = balance[["gross_output_twh", "gross_input_twh"]].max(axis=1)
    balance["residual_pct"] = balance["residual_twh"].div(scale).where(
        scale.gt(tolerance), 0.0
    )
    significant = balance["residual_twh"].abs().gt(
        pd.concat([pd.Series(tolerance, index=scale.index), scale * 0.01], axis=1).max(axis=1)
    )
    balance["balance_status"] = "Within tolerance"
    balance.loc[significant, "balance_status"] = "Unresolved"
    return balance.loc[:, list(_FUEL_BALANCE_COLUMNS)]


def prepare_fuel_pathway_links(
    result: ResourceAllocationResult,
    *,
    period: int,
    resource_pool: str,
    commodity_name: str,
    technical: bool = False,
    process_aggregation_field: str | None = None,
    supply_aggregation_field: str | None = None,
    demand_aggregation_field: str | None = None,
    supply_colour_field: str | None = None,
    demand_colour_field: str | None = None,
    unmatched_flows: pd.DataFrame | None = None,
    annual_diagnostics: pd.DataFrame | None = None,
    tolerance: float = 1e-9,
    _prepared_evidence: pd.DataFrame | None = None,
    _prepared_unmatched: pd.DataFrame | None = None,
    _prepared_source_detail: pd.DataFrame | None = None,
    _prepared_demand_detail: pd.DataFrame | None = None,
    _prepared_process_net_detail: pd.DataFrame | None = None,
    _prepared_balance: pd.DataFrame | None = None,
    _prepared_annual_diagnostics: tuple[tuple[str, str], ...] | None = None,
    _omit_summary: bool = False,
) -> FuelPathwayResult:
    """Build a net Supply → Individual fuel → Demand graph for one fuel.

    All exact commodity codes belonging to the selected authoritative
    ``commodity_name`` are pooled at each process. A process appears on at
    most one endpoint: positive selected-fuel net flow is supply, negative
    flow is demand, and balanced same-fuel pass-through is suppressed. Exact
    code evidence and balances remain available independently of the chart.
    """
    if tolerance < 0:
        raise ValueError("tolerance must be non-negative.")
    if process_aggregation_field is not None and (
        supply_aggregation_field is not None or demand_aggregation_field is not None
    ):
        raise ValueError(
            "Specify either process_aggregation_field or independent physical "
            "supply_aggregation_field and demand_aggregation_field, not both."
        )
    if process_aggregation_field is not None:
        supply_aggregation_field = process_aggregation_field
        demand_aggregation_field = process_aggregation_field
    _validate_pathway_aggregation_field(
        supply_aggregation_field, endpoint="physical supply"
    )
    _validate_pathway_aggregation_field(
        demand_aggregation_field, endpoint="physical demand"
    )
    _validate_pathway_aggregation_field(
        supply_colour_field, endpoint="physical supply colour"
    )
    _validate_pathway_aggregation_field(
        demand_colour_field, endpoint="physical demand colour"
    )
    selected_pool = str(resource_pool).strip()
    selected_fuel = str(commodity_name).strip()
    if not selected_pool or not selected_fuel:
        return _empty_fuel_pathway()
    if _prepared_evidence is None:
        evidence = _fuel_evidence(result)
    else:
        # Full-workbook export has already normalized and partitioned the
        # evidence once. Repeating that work for every pool/fuel combination
        # makes large selected-run exports appear to hang.
        evidence = _prepared_evidence
    selected_fuel_available = bool((
        evidence["period"].eq(int(period))
        & evidence["resource_pool"].eq(selected_pool)
        & evidence["commodity_name"].eq(selected_fuel)
        & evidence["unit_factor"].notna()
        & evidence["quantity_twh"].gt(tolerance)
    ).any())
    if not selected_fuel_available:
        empty = _empty_fuel_pathway()
        diagnostics = pd.DataFrame([{
            "diagnostic_type": "unavailable_fuel_selection", "period": int(period),
            "resource_pool": selected_pool, "commodity_name": selected_fuel,
            "commodity": "", "process": "",
            "detail": "The selected individual fuel has no positive compatible evidence in this pool and period.",
            "value": pd.NA,
        }], columns=list(_FUEL_DIAGNOSTIC_COLUMNS))
        return FuelPathwayResult(
            empty.links, empty.summary, empty.source_detail, empty.demand_detail,
            empty.commodity_balance, empty.unmatched_detail, empty.hidden_detail,
            diagnostics, empty.process_net_detail,
        )

    selected = evidence[
        evidence["period"].eq(int(period))
        & evidence["resource_pool"].eq(selected_pool)
        & evidence["commodity_name"].eq(selected_fuel)
        & evidence["direction"].isin(("IN", "OUT"))
    ].copy()
    if "calculation_status" not in selected.columns:
        selected = _annotate_fuel_evidence(selected, tolerance)
    source_detail = (
        _prepared_source_detail.copy().reset_index(drop=True)
        if _prepared_source_detail is not None else
        _detail_frame(
            selected[selected["direction"].eq("OUT")], tolerance, technical=technical
        )
    )
    demand_detail = (
        _prepared_demand_detail.copy().reset_index(drop=True)
        if _prepared_demand_detail is not None else
        _detail_frame(
            selected[selected["direction"].eq("IN")], tolerance, technical=technical
        )
    )
    graph_rows = selected[
        selected["calculation_status"].eq(_FUEL_CALCULATION_INCLUDED)
    ].copy()
    display_rows = graph_rows[
        technical
        | graph_rows["stakeholder_visibility"].eq(_FUEL_VISIBILITY_DISPLAYED)
    ].copy()
    display_mode = "Technical" if technical else "Stakeholder"

    if _prepared_process_net_detail is None:
        all_process_nets = _prepare_fuel_process_nets(
            graph_rows, projection="All evidence", display_mode=display_mode,
            technical=technical, supply_aggregation_field=supply_aggregation_field,
            demand_aggregation_field=demand_aggregation_field, tolerance=tolerance,
        )
        visible_process_nets = _prepare_fuel_process_nets(
            display_rows, projection="Displayed projection", display_mode=display_mode,
            technical=technical, supply_aggregation_field=supply_aggregation_field,
            demand_aggregation_field=demand_aggregation_field, tolerance=tolerance,
        )
        process_net_detail = pd.concat(
            [all_process_nets, visible_process_nets], ignore_index=True
        )
    else:
        process_net_detail = _prepared_process_net_detail.copy()
        all_process_nets = process_net_detail[
            process_net_detail["projection"].eq("All evidence")
        ]
        visible_process_nets = process_net_detail[
            process_net_detail["projection"].eq("Displayed projection")
        ]
    process_net_detail = process_net_detail.sort_values(
        ["projection", "endpoint_side", "region", "process"]
    ).reset_index(drop=True)

    links: list[dict] = []
    fuel_id = _fuel_node_id(selected_pool, selected_fuel)
    for _, row in visible_process_nets[
        ~visible_process_nets["pass_through_suppressed"]
    ].iterrows():
        stage = "supply_to_fuel" if row["endpoint_side"] == "Supply" else "fuel_to_demand"
        process_aggregation_field = row["process_grouping_field"] or None
        process_id, process_label = _fuel_process_display(
            row, process_aggregation_field, technical=technical,
            stage="fuel_supply" if stage == "supply_to_fuel" else "fuel_demand",
        )
        value = abs(float(row["net_flow_twh"]))
        endpoint_kind = "Supply" if stage == "supply_to_fuel" else "Demand"
        source_id, source_label, source_kind, source_role = (
            (process_id, process_label, endpoint_kind, row["flow_role"])
            if stage == "supply_to_fuel" else
            (fuel_id, selected_fuel, "Individual fuel", "")
        )
        target_id, target_label, target_kind, target_role = (
            (fuel_id, selected_fuel, "Individual fuel", "")
            if stage == "supply_to_fuel" else
            (process_id, process_label, endpoint_kind, row["flow_role"])
        )
        colour_field = (
            supply_colour_field if stage == "supply_to_fuel" else demand_colour_field
        )
        colour_metadata = (
            _activity_colour_metadata(pd.DataFrame([row]), colour_field)
            if colour_field else
            {"field": "", "category": "", "contributors": ""}
        )
        links.append({
            "period": int(period), "view": "Individual fuel physical pathway", "sign": "",
            "stage": stage, "source_id": source_id, "source_label": source_label,
            "source_kind": source_kind, "source_role": source_role,
            "target_id": target_id, "target_label": target_label,
            "target_kind": target_kind, "target_role": target_role,
            "resource_pool": selected_pool, "commodity_name": selected_fuel,
            "commodity": row["exact_commodity_codes"],
            "exact_commodity_codes": row["exact_commodity_codes"],
            "exact_commodity_count": int(row["exact_commodity_count"]),
            "commodity_function": "", "function_tags": "", "direction": "NET",
            "flow_role": row["flow_role"], "link_role": "", "depth": pd.NA,
            "region": row["region"], "process": row["process"],
            "process_name": row["process_name"], "sector": row["process_sector"],
            "gross_output_twh": float(row["gross_output_twh"]),
            "gross_input_twh": float(row["gross_input_twh"]),
            "net_flow_twh": float(row["net_flow_twh"]),
            "signed_value": value, "display_value": value, "unit": "TWh/yr",
            "timeslice_mode": "", "vd_row_count": int(row["vd_row_count"]),
            "display_mode": display_mode,
            "stakeholder_visibility": row["stakeholder_visibility"],
            "stakeholder_visibility_reason": row["stakeholder_visibility_reason"],
            "process_grouping_field": process_aggregation_field or "",
            "process_grouping_label": row["process_grouping_label"],
            "grouped_process_count": 1,
            "grouped_processes": _process_pathway_label(row, technical=True),
            "source_colour_field": colour_metadata["field"] if stage == "supply_to_fuel" else "",
            "source_colour_category": colour_metadata["category"] if stage == "supply_to_fuel" else "",
            "source_colour_contributors": colour_metadata["contributors"] if stage == "supply_to_fuel" else "",
            "target_colour_field": colour_metadata["field"] if stage == "fuel_to_demand" else "",
            "target_colour_category": colour_metadata["category"] if stage == "fuel_to_demand" else "",
            "target_colour_contributors": colour_metadata["contributors"] if stage == "fuel_to_demand" else "",
            "detail": (
                f"Net selected-fuel flow: {value:,.6f} TWh/year; gross output "
                f"{float(row['gross_output_twh']):,.6f}; gross input "
                f"{float(row['gross_input_twh']):,.6f}; contributors: "
                f"{_process_pathway_label(row, technical=True)}"
            ),
        })
    link_frame = pd.DataFrame(links, columns=list(_FUEL_PATHWAY_LINK_COLUMNS))
    if any((
        supply_aggregation_field, demand_aggregation_field,
        supply_colour_field, demand_colour_field,
    )):
        link_frame = _aggregate_pooled_fuel_links(link_frame)
    if not link_frame.empty:
        link_frame = link_frame.sort_values(
            ["stage", "display_value", "process"], ascending=[True, False, True]
        ).reset_index(drop=True)

    balance = (
        _prepared_balance.copy().reset_index(drop=True)
        if _prepared_balance is not None else
        _prepare_fuel_commodity_balances(graph_rows, tolerance=tolerance)
    )
    hidden_rows = graph_rows[
        ~graph_rows["stakeholder_visibility"].eq(_FUEL_VISIBILITY_DISPLAYED)
    ]
    hidden_detail = pd.concat([
        source_detail[~source_detail["stakeholder_visibility"].eq(_FUEL_VISIBILITY_DISPLAYED)],
        demand_detail[~demand_detail["stakeholder_visibility"].eq(_FUEL_VISIBILITY_DISPLAYED)],
    ], ignore_index=True)
    omitted_row_count = int((
        ~selected["calculation_status"].eq(_FUEL_CALCULATION_INCLUDED)
    ).sum())
    unmatched_detail = (
        _prepared_unmatched.copy().reset_index(drop=True)
        if _prepared_unmatched is not None
        else _selected_unmatched_detail(
            result, unmatched_flows, period=int(period), resource_pool=selected_pool,
            commodity_name=selected_fuel,
        )
    )
    visible_supply = float(visible_process_nets.loc[
        visible_process_nets["endpoint_side"].eq("Supply"), "net_flow_twh"
    ].sum())
    visible_demand = float(-visible_process_nets.loc[
        visible_process_nets["endpoint_side"].eq("Demand"), "net_flow_twh"
    ].sum())
    if _omit_summary:
        summary = pd.DataFrame(columns=list(_FUEL_SUMMARY_COLUMNS))
    else:
        all_supply = float(all_process_nets.loc[
            all_process_nets["endpoint_side"].eq("Supply"), "net_flow_twh"
        ].sum())
        all_demand = float(-all_process_nets.loc[
            all_process_nets["endpoint_side"].eq("Demand"), "net_flow_twh"
        ].sum())
        visible_supply_roles = visible_process_nets[
            visible_process_nets["endpoint_side"].eq("Supply")
        ]
        visible_demand_roles = visible_process_nets[
            visible_process_nets["endpoint_side"].eq("Demand")
        ]
        summary = pd.DataFrame([{
            "period": int(period), "resource_pool": selected_pool,
            "commodity_name": selected_fuel,
            "gross_output_twh": float(balance["gross_output_twh"].sum()),
            "gross_input_twh": float(balance["gross_input_twh"].sum()),
            "origin_import_twh": float(all_process_nets.loc[
                all_process_nets["flow_role"].str.contains("Import", na=False)
                & all_process_nets["endpoint_side"].eq("Supply"), "net_flow_twh"
            ].sum()),
            "final_consumption_twh": float(-visible_demand_roles.loc[
                visible_demand_roles["flow_role"].str.contains("Final consumption", na=False), "net_flow_twh"
            ].sum()),
            "exports_twh": float(-visible_demand_roles.loc[
                visible_demand_roles["flow_role"].str.contains("Export", na=False), "net_flow_twh"
            ].sum()),
            "imports_twh": float(visible_supply_roles.loc[
                visible_supply_roles["flow_role"].str.contains("Import", na=False), "net_flow_twh"
            ].sum()),
            "pathway_entry_twh": visible_supply, "all_pathway_entry_twh": all_supply,
            "displayed_output_twh": float(display_rows.loc[display_rows["direction"].eq("OUT"), "quantity_twh"].sum()),
            "displayed_input_twh": float(display_rows.loc[display_rows["direction"].eq("IN"), "quantity_twh"].sum()),
            "visible_supply_twh": visible_supply, "visible_demand_twh": visible_demand,
            "all_supply_twh": all_supply, "all_demand_twh": all_demand,
            "supply_demand_gap_twh": visible_supply - visible_demand,
            "hidden_row_count": int(len(hidden_rows)),
            "hidden_twh": float(hidden_rows["quantity_twh"].sum()),
            "omitted_row_count": omitted_row_count, "unmatched_row_count": int(len(unmatched_detail)),
            "residual_twh": float(balance["residual_twh"].sum()),
            "commodity_count": int(balance["commodity"].nunique()),
            "unresolved_count": int(balance["balance_status"].eq("Unresolved").sum()),
            "pass_through_process_count": int(visible_process_nets["pass_through_suppressed"].sum()),
            "pathway_status": "Available" if not link_frame.empty else "No material net selected-fuel flow",
        }], columns=list(_FUEL_SUMMARY_COLUMNS))

    diagnostics: list[dict] = []
    pass_through = visible_process_nets[visible_process_nets["pass_through_suppressed"]]
    if not pass_through.empty:
        diagnostics.append({
            "diagnostic_type": "same_fuel_pass_through_suppressed", "period": int(period),
            "resource_pool": selected_pool, "commodity_name": selected_fuel,
            "commodity": "", "process": "",
            "detail": "Processes with balanced selected-fuel annual inputs and outputs are omitted from Sankey endpoints.",
            "value": len(pass_through),
        })
    gap = visible_supply - visible_demand
    if abs(gap) > max(tolerance, max(visible_supply, visible_demand) * 0.01):
        diagnostics.append({
            "diagnostic_type": "pooled_supply_demand_gap", "period": int(period),
            "resource_pool": selected_pool, "commodity_name": selected_fuel,
            "commodity": "", "process": "",
            "detail": "Net supply and net demand differ at the selected individual-fuel boundary; no balancing link was fabricated.",
            "value": gap,
        })
    excluded = pd.concat([source_detail, demand_detail], ignore_index=True)
    for _, row in excluded[~excluded["calculation_status"].eq(_FUEL_CALCULATION_INCLUDED)].iterrows():
        diagnostics.append({
            "diagnostic_type": "physical_flow_omitted", "period": int(period),
            "resource_pool": selected_pool, "commodity_name": selected_fuel,
            "commodity": row["commodity"], "process": row["process"],
            "detail": row["exclusion_reason"], "value": row["reported_value"],
        })
    for visibility, rows in hidden_rows.groupby("stakeholder_visibility", sort=True):
        diagnostics.append({
            "diagnostic_type": visibility, "period": int(period),
            "resource_pool": selected_pool, "commodity_name": selected_fuel,
            "commodity": "", "process": "",
            "detail": f"{len(rows)} valid physical edge(s) are hidden from the stakeholder Sankey and retained in Technical mode and audit evidence.",
            "value": float(rows["quantity_twh"].sum()),
        })
    for _, row in balance[balance["balance_status"].eq("Unresolved")].iterrows():
        diagnostics.append({
            "diagnostic_type": "exact_commodity_residual", "period": int(period),
            "resource_pool": selected_pool, "commodity_name": selected_fuel,
            "commodity": row["commodity"], "process": "",
            "detail": "Gross matched output and input differ; no balancing link was fabricated.",
            "value": row["residual_twh"],
        })
    annual_records = _prepared_annual_diagnostics
    if annual_records is None and annual_diagnostics is not None and not annual_diagnostics.empty:
        retained_types = {
            "exact_duplicate_rows_removed", "detailed_timeslice_rows_ignored",
            "matched_annual_edges", "unmatched_annual_edges",
        }
        annual_records = tuple(
            (str(row.diagnostic_type), str(row.detail))
            for row in annual_diagnostics.itertuples(index=False)
            if str(row.diagnostic_type) in retained_types
        )
    for diagnostic_type, detail in annual_records or ():
        diagnostics.append({
            "diagnostic_type": diagnostic_type, "period": int(period),
            "resource_pool": selected_pool, "commodity_name": selected_fuel,
            "commodity": "", "process": "", "detail": detail,
            "value": pd.NA,
        })
    if not unmatched_detail.empty:
        diagnostics.append({
            "diagnostic_type": "unmatched_selected_fuel_edges", "period": int(period),
            "resource_pool": selected_pool, "commodity_name": selected_fuel,
            "commodity": "", "process": "",
            "detail": "Selected-fuel VD rows not present in VDT are excluded from the graph.",
            "value": len(unmatched_detail),
        })
    return FuelPathwayResult(
        links=link_frame, summary=summary, source_detail=source_detail,
        demand_detail=demand_detail, commodity_balance=balance,
        unmatched_detail=unmatched_detail, hidden_detail=hidden_detail,
        diagnostics=pd.DataFrame(diagnostics, columns=list(_FUEL_DIAGNOSTIC_COLUMNS)),
        process_net_detail=process_net_detail,
    )


def fuel_pathway_combinations(
    result: ResourceAllocationResult,
    tolerance: float = 1e-9,
) -> tuple[tuple[int, str, str], ...]:
    """Return every model year, resource pool and individual fuel with evidence."""
    evidence = _fuel_evidence(result)
    if evidence.empty:
        return ()
    eligible = evidence[
        evidence["resource_pool"].ne("")
        & evidence["commodity_name"].ne("")
        & evidence["unit_factor"].notna()
        & evidence["quantity_twh"].gt(tolerance)
    ]
    combinations = {
        (int(period), str(resource_pool), str(fuel))
        for period, resource_pool, fuel in eligible[
            ["period", "resource_pool", "commodity_name"]
        ].itertuples(index=False, name=None)
        if pd.notna(period)
    }
    return tuple(sorted(
        combinations,
        key=lambda item: (item[0], item[1].casefold(), item[2].casefold()),
    ))


def _partition_by_pathway(frame: pd.DataFrame) -> dict[tuple[int, str, str], pd.DataFrame]:
    """Split one prepared frame into its model year, pool and fuel groups."""
    keys = ["period", "resource_pool", "commodity_name"]
    if frame.empty or not set(keys).issubset(frame.columns):
        return {}
    return {
        (int(period), str(resource_pool), str(fuel)): rows
        for (period, resource_pool, fuel), rows in frame.groupby(
            keys, dropna=False, sort=False,
        )
        if pd.notna(period) and str(resource_pool) and str(fuel)
    }


def prepare_fuel_pathways_batch(
    result: ResourceAllocationResult,
    combinations: Iterable[tuple[int, str, str]],
    *,
    unmatched_flows: pd.DataFrame | None = None,
    annual_diagnostics: pd.DataFrame | None = None,
    tolerance: float = 1e-9,
) -> Iterator[tuple[tuple[int, str, str], FuelPathwayResult]]:
    """Yield one prepared pathway per requested year, resource pool and fuel.

    Normalising, annotating and partitioning the physical evidence is done once
    for the whole run rather than once per combination; repeating it per
    combination is what made whole-run exports appear to hang. Each yielded
    result carries the same frames as ``prepare_fuel_pathway_links`` except the
    single-selection summary, which is meaningless for a batch.
    """
    requested = [
        (int(period), str(resource_pool), str(fuel))
        for period, resource_pool, fuel in combinations
    ]
    evidence = _annotate_fuel_evidence(_fuel_evidence(result), tolerance)
    evidence_groups = _partition_by_pathway(evidence)
    unmatched = _prepare_unmatched_fuel_evidence(result, unmatched_flows)
    unmatched_groups = _partition_by_pathway(unmatched)
    empty_unmatched = pd.DataFrame(columns=unmatched.columns)

    source_detail_groups: dict[tuple[int, str, str], pd.DataFrame] = {}
    demand_detail_groups: dict[tuple[int, str, str], pd.DataFrame] = {}
    process_net_groups: dict[tuple[int, str, str], pd.DataFrame] = {}
    balance_groups: dict[tuple[int, str, str], pd.DataFrame] = {}
    batch_columns = {
        "direction", "calculation_status", "stakeholder_visibility",
        "quantity_twh", "flow_role", "commodity", "vd_row_count",
    }
    if batch_columns.issubset(evidence.columns):
        source_detail_groups = _partition_by_pathway(_detail_frame(
            evidence[evidence["direction"].eq("OUT")], tolerance
        ))
        demand_detail_groups = _partition_by_pathway(_detail_frame(
            evidence[evidence["direction"].eq("IN")], tolerance
        ))
        graph_rows = evidence[
            evidence["calculation_status"].eq(_FUEL_CALCULATION_INCLUDED)
            & evidence["direction"].isin(("IN", "OUT"))
        ]
        display_rows = graph_rows[
            graph_rows["stakeholder_visibility"].eq(_FUEL_VISIBILITY_DISPLAYED)
        ]
        process_net = pd.concat([
            _prepare_fuel_process_nets(
                graph_rows, projection="All evidence", display_mode="Stakeholder"
            ),
            _prepare_fuel_process_nets(
                display_rows,
                projection="Displayed projection",
                display_mode="Stakeholder",
            ),
        ], ignore_index=True)
        process_net_groups = _partition_by_pathway(process_net)
        balance_groups = _partition_by_pathway(
            _prepare_fuel_commodity_balances(graph_rows)
        )

    retained_diagnostic_types = {
        "exact_duplicate_rows_removed", "detailed_timeslice_rows_ignored",
        "matched_annual_edges", "unmatched_annual_edges",
    }
    prepared_annual_diagnostics = tuple(
        (str(row.diagnostic_type), str(row.detail))
        for row in annual_diagnostics.itertuples(index=False)
        if str(row.diagnostic_type) in retained_diagnostic_types
    ) if annual_diagnostics is not None and not annual_diagnostics.empty else ()

    for key in requested:
        yield key, prepare_fuel_pathway_links(
            result,
            period=key[0],
            resource_pool=key[1],
            commodity_name=key[2],
            tolerance=tolerance,
            unmatched_flows=unmatched_flows,
            annual_diagnostics=annual_diagnostics,
            _prepared_evidence=evidence_groups[key],
            _prepared_unmatched=unmatched_groups.get(key, empty_unmatched),
            _prepared_source_detail=source_detail_groups.get(key),
            _prepared_demand_detail=demand_detail_groups.get(key),
            _prepared_process_net_detail=process_net_groups.get(key),
            _prepared_balance=balance_groups.get(key),
            _prepared_annual_diagnostics=prepared_annual_diagnostics,
            _omit_summary=True,
        )


def load_resource_pools(path: Path | str = DEFAULT_RESOURCE_POOLS_PATH) -> pd.DataFrame:
    """Load and validate the curated one-row-per-commodity pool sheet.

    A blank ``resource_pool`` is a reviewed decision that the commodity is not
    pooled; it is kept, not dropped.  Every pool declares exactly one
    ``pool_kind`` (Fuel, Service or Demand); an unpooled row has no kind.
    """
    pools = pd.read_csv(path, dtype=str, keep_default_na=False)
    required = {"commodity", "resource_pool", "pool_kind"}
    missing = required.difference(pools.columns)
    if missing:
        raise ValueError(
            "Resource pools are missing required columns: "
            + ", ".join(sorted(missing))
        )
    pools = pools.loc[:, ["commodity", "resource_pool", "pool_kind"]].copy()
    pools["commodity"] = _normalise_code(pools["commodity"])
    pools["resource_pool"] = _clean_text(pools["resource_pool"])
    pools["pool_kind"] = _clean_text(pools["pool_kind"])
    if pools["commodity"].eq("").any():
        raise ValueError("Resource pools cannot contain blank commodity codes.")
    duplicated = sorted(set(pools.loc[pools["commodity"].duplicated(), "commodity"]))
    if duplicated:
        raise ValueError(
            "Resource pools list a commodity more than once: " + ", ".join(duplicated)
        )
    unknown = sorted(set(pools["pool_kind"]) - set(POOL_KINDS) - {""})
    if unknown:
        raise ValueError("Resource pools use an unknown pool_kind: " + ", ".join(unknown))
    mismatched = pools["resource_pool"].eq("") != pools["pool_kind"].eq("")
    if mismatched.any():
        raise ValueError(
            "Resource pools need a pool_kind exactly when a pool is set: "
            + ", ".join(sorted(pools.loc[mismatched, "commodity"]))
        )
    kinds_per_pool = pools[pools["resource_pool"].ne("")].groupby("resource_pool")[
        "pool_kind"
    ].nunique()
    mixed = sorted(kinds_per_pool[kinds_per_pool.gt(1)].index)
    if mixed:
        raise ValueError("Resource pools declare more than one pool_kind for: " + ", ".join(mixed))
    return pools.reset_index(drop=True)


def resource_pool_kinds(pools: pd.DataFrame) -> dict[str, str]:
    """Return ``{resource_pool: pool_kind}`` for every pool in a loaded sheet."""
    named = pools[pools["resource_pool"].ne("")]
    return dict(zip(named["resource_pool"], named["pool_kind"]))


def _attach_pool_kind(frame: pd.DataFrame, kinds: dict[str, str]) -> pd.DataFrame:
    """Place ``pool_kind`` directly after ``resource_pool``; unpooled rows stay blank."""
    result = frame.drop(columns="pool_kind", errors="ignore").copy()
    kind = result["resource_pool"].map(kinds).fillna("") if len(result) else pd.Series(dtype=str)
    result.insert(result.columns.get_loc("resource_pool") + 1, "pool_kind", kind)
    return result


def pool_kind_reconciliation(result: ResourceAllocationResult) -> pd.DataFrame:
    """Check, per view and model year, that Fuel + Service + Unallocated = source cost.

    Fuel and service totals are summed here only to prove nothing is lost;
    they measure different things and are never compared or charted together.
    """
    columns = [
        "view", "period", "fuel_cost_m", "service_cost_m", "unallocated_cost_m",
        "source_cost_m", "difference_m",
    ]
    assignments = result.cost_assignments
    if assignments is None or assignments.empty:
        return pd.DataFrame(columns=columns)
    allocatable = assignments[
        assignments["view"].isin((RESOURCE_FLOW_VIEW, UPSTREAM_SUPPLY_VIEW))
    ]
    source_rows = allocatable.drop_duplicates(["view", "source_row_id"])
    keys = ["view", "period"]
    source = source_rows.groupby(keys)["cost_value_m"].sum().rename("source_cost_m")
    unallocated = source_rows[
        source_rows["assignment_status"].eq("Unallocated")
    ].groupby(keys)["cost_value_m"].sum().rename("unallocated_cost_m")
    pools = result.pool_summary
    by_kind = pools.groupby([*keys, "pool_kind"])["source_cost_m"].sum().unstack(
        "pool_kind", fill_value=0.0
    ) if not pools.empty else pd.DataFrame()
    table = pd.concat([source, unallocated], axis=1)
    for kind, column in ((POOL_KIND_FUEL, "fuel_cost_m"), (POOL_KIND_SERVICE, "service_cost_m")):
        table[column] = by_kind[kind] if kind in by_kind.columns else 0.0
    table = table.fillna(0.0)
    table["difference_m"] = (
        table["source_cost_m"] - table["fuel_cost_m"]
        - table["service_cost_m"] - table["unallocated_cost_m"]
    )
    return table.reset_index().loc[:, columns]


_COMMODITY_COVERAGE_COLUMNS = (
    "period", "commodity", "commodity_name", "resource_pool", "pool_kind",
    "counted_twh", "excluded_twh", "coverage_status", "coverage_reason",
)
# Coverage groups: a pool's declared kind, then the reviewed "not pooled"
# answers, then energy the sheet does not answer at all.
COVERAGE_GROUP_NOT_POOLED = "Not pooled"
COVERAGE_GROUP_NO_POOL = "No pool"
COVERAGE_GROUPS = (*SPLIT_POOL_KINDS, COVERAGE_GROUP_NOT_POOLED, COVERAGE_GROUP_NO_POOL)
_COMMODITY_COVERAGE_ANSWERS: dict[str, tuple[str, str]] = {
    "Included sector consumption": (
        COMMODITY_COVERAGE_COUNTED,
        "Its use by sectors counts towards the split.",
    ),
    "Suppressed same-pool pass-through": (
        COMMODITY_COVERAGE_NOT_COUNTED,
        "Passed through a process that also produces this pool, so counting it "
        "would count the same energy twice.",
    ),
    "Not an input flow": (
        COMMODITY_COVERAGE_NOT_COUNTED,
        "Only produced in this model year and never used, so there is no use to split.",
    ),
    "Unmapped energy commodity": (
        COMMODITY_COVERAGE_ATTENTION,
        "This fuel is in no resource pool, so its use is not counted.",
    ),
    "Incompatible energy unit": (
        COMMODITY_COVERAGE_ATTENTION,
        "Reported in a unit that cannot be converted to TWh, so its use is not counted.",
    ),
    "Reviewed not pooled": (
        COMMODITY_COVERAGE_NOT_COUNTED,
        "Reviewed in the curated pooling sheet and deliberately left out of every "
        "resource pool, so its use is not counted.",
    ),
}


def summarise_commodity_coverage(
    quantity_lineage: pd.DataFrame,
    *,
    tolerance: float = 1e-9,
) -> pd.DataFrame:
    """Answer, per model year and exact commodity code, whether it is counted.

    This restates the ``quantity_status`` already recorded for every observed
    flow during allocation.  It reads the lineage and computes nothing new: no
    denominator, share, pool cost or allocated value depends on it.

    Rows are keyed on the exact ``commodity`` code rather than
    ``commodity_name``, which is a non-unique roll-up.  The universe is the
    commodities that carry energy — energy by ``commodity_set``,
    ``commodity_type`` or by having a mapped pool — and that move a material
    quantity in the period.  Emissions and demand services are outside a
    fuel-cost split by construction and are not listed.

    Where one code is answered differently by different flows, the most
    actionable answer wins, in ``COMMODITY_COVERAGE_STATUSES`` order.  An
    unrecognised status is surfaced as *needs attention* rather than hidden.
    """
    empty = pd.DataFrame(columns=list(_COMMODITY_COVERAGE_COLUMNS))
    if quantity_lineage is None or quantity_lineage.empty:
        return empty
    if not {"period", "commodity"}.issubset(quantity_lineage.columns):
        return empty

    rows = quantity_lineage.copy()
    rows["period"] = pd.to_numeric(rows["period"], errors="coerce")
    rows["commodity"] = _normalise_code(rows["commodity"])
    for column in ("commodity_name", "resource_pool", "pool_kind", "mapping_status"):
        rows[column] = _clean_text(rows.get(column, pd.Series("", index=rows.index)))
    rows["quantity_twh"] = pd.to_numeric(
        rows.get("quantity_twh", pd.Series(pd.NA, index=rows.index)), errors="coerce"
    )
    reported = pd.to_numeric(
        rows.get("value", pd.Series(pd.NA, index=rows.index)), errors="coerce"
    )
    if "is_energy" in rows.columns:
        is_energy = rows["is_energy"].fillna(False).astype(bool)
    else:
        is_energy = _is_energy_row(rows)
    magnitude = reported.abs().fillna(rows["quantity_twh"].abs())
    rows = rows[
        rows["period"].notna()
        & rows["commodity"].ne("")
        & is_energy
        & magnitude.gt(tolerance)
    ].copy()
    if rows.empty:
        return empty
    rows["period"] = rows["period"].astype(int)

    status = _clean_text(rows.get("quantity_status", pd.Series("", index=rows.index)))
    answers = [
        _COMMODITY_COVERAGE_ANSWERS.get(
            value,
            (
                COMMODITY_COVERAGE_ATTENTION,
                f"Unrecognised calculation status: {value or 'blank'}.",
            ),
        )
        for value in status
    ]
    rows["coverage_status"] = [answer[0] for answer in answers]
    rows["coverage_reason"] = [answer[1] for answer in answers]
    rows["_rank"] = [
        COMMODITY_COVERAGE_STATUSES.index(answer[0]) for answer in answers
    ]

    counted = rows["coverage_status"].eq(COMMODITY_COVERAGE_COUNTED)
    rows["counted_twh"] = rows["quantity_twh"].where(counted, 0.0).fillna(0.0)
    rows["excluded_twh"] = rows["quantity_twh"].where(~counted, 0.0).fillna(0.0)

    def first_labelled(values: pd.Series) -> str:
        return next((value for value in values if value), "")

    coverage = rows.groupby(["period", "commodity"], as_index=False, dropna=False).agg(
        commodity_name=("commodity_name", first_labelled),
        resource_pool=("resource_pool", first_labelled),
        pool_kind=("pool_kind", first_labelled),
        mapping_status=("mapping_status", first_labelled),
        counted_twh=("counted_twh", "sum"),
        excluded_twh=("excluded_twh", "sum"),
        _rank=("_rank", "min"),
    )
    reasons = rows.groupby(
        ["period", "commodity", "_rank"], as_index=False, dropna=False
    )["coverage_reason"].agg(
        lambda values: "; ".join(dict.fromkeys(value for value in values if value))
    )
    coverage = coverage.merge(reasons, on=["period", "commodity", "_rank"], how="left")
    coverage["coverage_status"] = [
        COMMODITY_COVERAGE_STATUSES[int(rank)] for rank in coverage["_rank"]
    ]
    coverage["coverage_reason"] = coverage["coverage_reason"].fillna("")
    reviewed_unpooled = coverage["mapping_status"].eq("Not pooled")
    coverage["pool_kind"] = coverage["pool_kind"].where(
        coverage["pool_kind"].ne(""),
        reviewed_unpooled.map({True: COVERAGE_GROUP_NOT_POOLED, False: COVERAGE_GROUP_NO_POOL}),
    )
    coverage["_group"] = coverage["pool_kind"].map(
        {group: rank for rank, group in enumerate(COVERAGE_GROUPS)}
    ).fillna(len(COVERAGE_GROUPS))
    return coverage.sort_values(
        ["period", "_group", "commodity_name", "commodity"], kind="stable"
    ).loc[:, list(_COMMODITY_COVERAGE_COLUMNS)].reset_index(drop=True)


def _normalise_code(values: pd.Series) -> pd.Series:
    return normalise_code(values)


def _clean_text(values: pd.Series, default: str = "") -> pd.Series:
    return values.fillna("").astype(str).str.strip().replace("", default)


# A pool balances: source + passed in = allocated + passed on + both unallocated.
_POOL_SUMMARY_COLUMNS = [
    "period", "view", "resource_pool", "pool_kind", "source_cost_m", "passed_in_cost_m",
    "pool_cost_m", "pool_twh", "allocated_cost_m", "passed_on_cost_m",
    "denominator_unallocated_cost_m", "cascade_unallocated_cost_m", "allocation_status",
]


def _empty_result() -> ResourceAllocationResult:
    return ResourceAllocationResult(
        allocations=pd.DataFrame(columns=[
            "period", "view", "resource_pool", "pool_kind", "sector", "sector_twh",
            "pool_twh", "allocation_share", "source_cost_m", "passed_in_cost_m",
            "pool_cost_m", "allocated_cost_m", "passed_on_cost_m",
            "cost_share", "allocation_status",
        ]),
        quantities=pd.DataFrame(columns=["period", "resource_pool", "sector", "sector_twh"]),
        quantity_lineage=pd.DataFrame(),
        cost_assignments=pd.DataFrame(),
        pool_summary=pd.DataFrame(columns=_POOL_SUMMARY_COLUMNS),
        view_summary=pd.DataFrame(columns=[
            "view", "source_cost_m", "assigned_cost_m", "unassigned_source_cost_m",
            "reconciliation_difference_m", "source_row_count", "allocated_cost_m",
            "denominator_unallocated_cost_m", "cascade_unallocated_cost_m",
            "allocation_difference_m",
        ]),
        commodity_cost_summary=pd.DataFrame(),
        commodity_mapping=pd.DataFrame(),
        diagnostics=pd.DataFrame(columns=[
            "diagnostic_type", "period", "process", "commodity", "detail", "value_m",
        ]),
        vintage_allocations=pd.DataFrame(columns=[
            "source_row_id", "period", "view", "resource_pool", "pool_kind", "process", "process_name",
            "attribute", "vintage", "vintage_basis", "share_basis_year", "share_basis",
            "sector", "allocation_share", "assigned_cost_m", "allocated_cost_m",
        ]),
    )


def normalise_resource_views(views: Iterable[str] | None) -> tuple[str, ...]:
    """Return a deterministic, validated set of allocation cost views.

    ``None`` preserves the legacy all-view preparation, including the separate
    ``Cost_Com`` disclosure. Explicit requests may omit views to avoid work for
    inactive Resource & Fuel pages.
    """
    if views is None:
        return RESOURCE_ALLOCATION_VIEWS
    requested = {str(view) for view in views}
    custom = sorted(view for view in requested if view not in RESOURCE_ALLOCATION_VIEWS)
    for view in custom:
        view_cost_attributes(view)
    return (
        *(view for view in RESOURCE_ALLOCATION_VIEWS if view in requested),
        *custom,
    )


def _commodity_catalog(
    flows: pd.DataFrame,
    commodity_labels: pd.DataFrame | None,
) -> pd.DataFrame:
    columns = list(COMMODITY_LABEL_COLUMNS)
    frames: list[pd.DataFrame] = []
    if commodity_labels is not None and not commodity_labels.empty:
        labels = commodity_labels.copy()
        for column in columns:
            if column not in labels.columns:
                labels[column] = ""
        frames.append(labels.loc[:, columns])
    if not flows.empty and "commodity" in flows.columns:
        attached = flows.copy()
        for column in columns:
            if column not in attached.columns:
                attached[column] = ""
        frames.append(attached.loc[:, columns])
    if not frames:
        return pd.DataFrame(columns=columns)
    catalog = pd.concat(frames, ignore_index=True)
    for column in columns:
        catalog[column] = _clean_text(catalog[column])
    catalog["commodity"] = _normalise_code(catalog["commodity"])
    catalog = catalog[catalog["commodity"].ne("")]
    return catalog.drop_duplicates("commodity", keep="first").reset_index(drop=True)


def map_resource_pools(catalog: pd.DataFrame, pools: pd.DataFrame) -> pd.DataFrame:
    """Look each commodity up by exact code in the curated pool sheet.

    ``mapping_status`` records the answer: ``Mapped`` (a fuel pool),
    ``Not pooled`` (listed with a blank pool), ``Demand service`` (a reference
    demand pool that never enters a fuel split) or ``Unmapped`` (absent from
    the sheet).  Only ``Mapped`` rows carry an effective ``resource_pool`` and
    its ``pool_kind`` (Fuel or Service).
    """
    result = catalog.copy()
    for column in COMMODITY_LABEL_COLUMNS:
        if column not in result.columns:
            result[column] = ""
    sheet = pools.drop_duplicates("commodity").set_index("commodity")
    codes = _normalise_code(result["commodity"])
    sheet_pool = codes.map(sheet["resource_pool"])
    listed = sheet_pool.notna()
    sheet_pool = sheet_pool.fillna("").astype(str).str.strip()
    sheet_kind = codes.map(sheet["pool_kind"]).fillna("").astype(str).str.strip()
    demand = listed & sheet_kind.eq(POOL_KIND_DEMAND)
    not_pooled = listed & sheet_pool.eq("")
    mapped = listed & ~demand & ~not_pooled

    result["resource_pool"] = sheet_pool.where(mapped, "")
    result["pool_kind"] = sheet_kind.where(mapped, "")
    result["mapping_status"] = "Unmapped"
    result.loc[mapped, "mapping_status"] = "Mapped"
    result.loc[not_pooled, "mapping_status"] = "Not pooled"
    result.loc[demand, "mapping_status"] = "Demand service"
    result["mapping_rule"] = ""
    result.loc[mapped, "mapping_rule"] = "Curated pooling sheet"
    result.loc[not_pooled, "mapping_rule"] = "Curated pooling sheet: not pooled"
    result.loc[demand, "mapping_rule"] = (
        "Curated pooling sheet: demand pool " + sheet_pool[demand]
        + " (outside the fuel split)"
    )
    return result


def _unit_factor(unit: object) -> float | None:
    normalised = str(unit or "").strip().upper().replace(" ", "")
    return _ENERGY_UNIT_FACTORS.get(normalised)


def _is_energy_row(frame: pd.DataFrame) -> pd.Series:
    commodity_set = _clean_text(frame.get("commodity_set", pd.Series("", index=frame.index))).str.upper()
    commodity_type = _clean_text(frame.get("commodity_type", pd.Series("", index=frame.index))).str.casefold()
    pool_kind = _clean_text(frame.get("pool_kind", pd.Series("", index=frame.index)))
    return commodity_set.eq("NRG") | commodity_type.isin({"fuel", "energy"}) | pool_kind.isin(SPLIT_POOL_KINDS)


def _prepare_quantities(
    flows: pd.DataFrame,
    mapping: pd.DataFrame,
    periods: set[int] | None,
    tolerance: float,
) -> tuple[pd.DataFrame, pd.DataFrame, list[dict]]:
    diagnostics: list[dict] = []
    required = {"region", "process", "commodity", "direction", "period", "value"}
    if flows.empty or not required.issubset(flows.columns):
        diagnostics.append({
            "diagnostic_type": "missing_annual_flows", "period": pd.NA,
            "process": "", "commodity": "", "detail": "No usable normalized annual flows.",
            "value_m": pd.NA,
        })
        return pd.DataFrame(columns=["period", "resource_pool", "sector", "sector_twh"]), pd.DataFrame(), diagnostics

    prepared = flows.copy()
    prepared["process"] = _normalise_code(prepared["process"])
    prepared["commodity"] = _normalise_code(prepared["commodity"])
    prepared["direction"] = _normalise_code(prepared["direction"])
    prepared["period"] = pd.to_numeric(prepared["period"], errors="coerce")
    prepared["value"] = pd.to_numeric(prepared["value"], errors="coerce")
    prepared = prepared[prepared["period"].notna() & prepared["value"].notna()].copy()
    prepared["period"] = prepared["period"].astype(int)
    if periods is not None:
        prepared = prepared[prepared["period"].isin(periods)].copy()

    metadata = mapping.drop_duplicates("commodity").copy()
    attached_columns = [
        "commodity", "resource_pool", "pool_kind", "mapping_rule", "mapping_status", "commodity_name",
        "commodity_group", "commodity_subgroup", "commodity_function", "commodity_type",
        "commodity_set", "unit",
    ]
    for column in attached_columns:
        if column not in metadata.columns:
            metadata[column] = ""
    existing_metadata = [column for column in attached_columns if column != "commodity" and column in prepared.columns]
    prepared = prepared.drop(columns=existing_metadata, errors="ignore").merge(
        metadata.loc[:, attached_columns], on="commodity", how="left"
    )
    prepared["resource_pool"] = _clean_text(prepared["resource_pool"])
    prepared["pool_kind"] = _clean_text(prepared["pool_kind"])
    prepared["unit_factor"] = prepared["unit"].map(_unit_factor)
    prepared["quantity_twh"] = prepared["value"] * prepared["unit_factor"]
    prepared["sector"] = _clean_text(
        prepared.get("process_sector", pd.Series("", index=prepared.index)), "Unlabelled"
    )
    prepared["is_energy"] = _is_energy_row(prepared)
    prepared["included"] = False
    prepared["quantity_status"] = "Not an input flow"

    inputs = prepared["direction"].eq("IN") & prepared["value"].abs().gt(tolerance)
    not_pooled = prepared["is_energy"] & _clean_text(prepared["mapping_status"]).eq("Not pooled")
    unmapped = (
        inputs & prepared["is_energy"] & prepared["resource_pool"].eq("")
        & ~not_pooled
    )
    incompatible = inputs & prepared["resource_pool"].ne("") & prepared["unit_factor"].isna()
    candidates = inputs & prepared["resource_pool"].ne("") & prepared["unit_factor"].notna()

    output_keys = prepared[
        prepared["direction"].eq("OUT")
        & prepared["value"].abs().gt(tolerance)
        & prepared["resource_pool"].ne("")
        & prepared["unit_factor"].notna()
    ][["region", "process", "period", "resource_pool"]].drop_duplicates()
    candidate_rows = prepared.loc[candidates, ["region", "process", "period", "resource_pool"]].copy()
    candidate_rows["_candidate_index"] = candidate_rows.index
    if not candidate_rows.empty and not output_keys.empty:
        pass_through_indices = candidate_rows.merge(
            output_keys.assign(_pass_through=True),
            on=["region", "process", "period", "resource_pool"],
            how="left",
        ).loc[lambda frame: frame["_pass_through"].eq(True), "_candidate_index"]
    else:
        pass_through_indices = pd.Series(dtype=int)

    prepared.loc[not_pooled, "quantity_status"] = "Reviewed not pooled"
    prepared.loc[unmapped, "quantity_status"] = "Unmapped energy commodity"
    prepared.loc[incompatible, "quantity_status"] = "Incompatible energy unit"
    prepared.loc[candidates, "quantity_status"] = "Included sector consumption"
    prepared.loc[pass_through_indices, "quantity_status"] = "Suppressed same-pool pass-through"
    prepared.loc[candidates & ~prepared.index.isin(pass_through_indices), "included"] = True

    for status, diagnostic_type in (
        ("Unmapped energy commodity", "unmapped_consumption_commodity"),
        ("Incompatible energy unit", "incompatible_consumption_unit"),
        ("Suppressed same-pool pass-through", "suppressed_pass_through"),
        ("Reviewed not pooled", "excluded_not_pooled_commodity"),
    ):
        rows = prepared[prepared["quantity_status"].eq(status)]
        for _, row in rows.iterrows():
            diagnostics.append({
                "diagnostic_type": diagnostic_type,
                "period": row["period"], "process": row["process"],
                "commodity": row["commodity"], "detail": status,
                "value_m": pd.NA,
            })

    negative = prepared[prepared["included"] & prepared["quantity_twh"].lt(0)]
    for _, row in negative.iterrows():
        diagnostics.append({
            "diagnostic_type": "negative_consumption_quantity",
            "period": row["period"], "process": row["process"],
            "commodity": row["commodity"],
            "detail": "Signed consumption is retained in the allocation denominator.",
            "value_m": pd.NA,
        })

    included = prepared[prepared["included"]]
    quantities = included.groupby(
        ["period", "resource_pool", "sector"], as_index=False, dropna=False
    )["quantity_twh"].sum().rename(columns={"quantity_twh": "sector_twh"})
    return quantities, prepared, diagnostics


def _build_output_pool_lookup(
    output_flows: pd.DataFrame,
    tolerance: float,
) -> dict[tuple[int, str, str], tuple[dict[str, float], bool]]:
    """Return ``{(period, region, process): (pool weights, ambiguous)}``.

    Observed output-carrier shares do not depend on the cost view, so this is
    built once per call to :func:`prepare_resource_allocation` and shared by
    every view.
    """
    output_lookup: dict[tuple[int, str, str], tuple[dict[str, float], bool]] = {}
    if output_flows.empty:
        return output_lookup
    outputs = output_flows[
        output_flows["direction"].eq("OUT") & output_flows["value"].abs().gt(tolerance)
    ]
    not_pooled = _clean_text(
        outputs.get("mapping_status", pd.Series("", index=outputs.index))
    ).eq("Not pooled")
    energy = outputs[outputs["is_energy"] & ~not_pooled]
    if energy.empty:
        # Processes without eligible energy output behave as ({}, False), which
        # is also the lookup default.
        return output_lookup

    # Two whole-frame passes instead of a pandas call chain per process group.
    keys = ["period", "region", "process"]
    pools = energy["resource_pool"]
    unmapped_or_incompatible = pools.eq("") | (pools.ne("") & energy["unit_factor"].isna())
    ambiguous_by_key = (
        energy.assign(_ambiguous=unmapped_or_incompatible)
        .groupby(keys, sort=False)["_ambiguous"].any()
    )
    usable = energy[pools.ne("") & energy["unit_factor"].notna()]
    weights_by_key: dict[tuple[int, str, str], dict[str, float]] = {}
    if not usable.empty:
        weights = (
            usable.assign(_weight=usable["quantity_twh"].abs())
            .groupby(keys + ["resource_pool"], sort=True)["_weight"].sum()
        )
        for (period, region, process, resource_pool), weight in weights.items():
            weights_by_key.setdefault(
                (int(period), str(region), str(process)), {}
            )[resource_pool] = float(weight)
    for (period, region, process), ambiguous in ambiguous_by_key.items():
        key = (int(period), str(region), str(process))
        output_lookup[key] = (weights_by_key.get(key, {}), bool(ambiguous))
    return output_lookup


_TAXONOMY_COLUMNS = (
    "process_name", "process_group", "process_category", "process_subsector",
    "process_sector", "process_ghgi", "process_dukes", "process_useful_output",
    "process_set",
)


def _assign_cost_rows(
    costs: pd.DataFrame,
    mapping: pd.DataFrame,
    output_lookup: dict[tuple[int, str, str], tuple[dict[str, float], bool]],
    *,
    view: str,
    attributes: tuple[str, ...],
    supply_processes: set[str] | None,
    periods: set[int] | None,
    tolerance: float,
) -> tuple[pd.DataFrame, list[dict]]:
    diagnostics: list[dict] = []
    required = {"Attribute", "Period", "Process", "Value"}
    if costs.empty or not required.issubset(costs.columns):
        return pd.DataFrame(), diagnostics
    selected = costs[costs["Attribute"].isin(attributes)].copy()
    selected["process"] = _normalise_code(selected["Process"])
    selected["region"] = _normalise_code(
        selected.get("Region", pd.Series("", index=selected.index))
    )
    selected["period"] = pd.to_numeric(selected["Period"], errors="coerce")
    selected["cost_value_m"] = pd.to_numeric(selected["Value"], errors="coerce")
    selected = selected[selected["period"].notna() & selected["cost_value_m"].notna()].copy()
    selected["period"] = selected["period"].astype(int)
    if periods is not None:
        selected = selected[selected["period"].isin(periods)]
    if supply_processes is not None:
        direct_commodity_cost = (
            selected["Attribute"].eq("Cost_Com")
            & _clean_text(selected.get("Commodity", pd.Series("", index=selected.index))).ne("")
        )
        selected = selected[
            selected["process"].isin(supply_processes) | direct_commodity_cost
        ]
    selected = selected.reset_index(names="_original_index")
    selected["source_row_id"] = [f"{index}:{position}" for position, index in enumerate(selected["_original_index"])]
    if selected.empty:
        return pd.DataFrame(), diagnostics

    catalog_pool = mapping.set_index("commodity")["resource_pool"].to_dict()

    # Plain dict records are much cheaper than ``iterrows`` here: the frame
    # carries the full label taxonomy, which would be boxed into a Series per row.
    for column in (*_TAXONOMY_COLUMNS, "Commodity"):
        if column not in selected.columns:
            selected[column] = ""
    if "Vintage" not in selected.columns:
        selected["Vintage"] = None
    record_columns = [
        "source_row_id", "period", "region", "process", "Attribute", "Commodity",
        "cost_value_m", "Vintage", *_TAXONOMY_COLUMNS,
    ]

    assignments: list[dict] = []
    for row in selected.loc[:, record_columns].to_dict("records"):
        key = (int(row["period"]), str(row["region"]), str(row["process"]))
        output_weights, ambiguous = output_lookup.get(key, ({}, False))
        base = {
            "source_row_id": row["source_row_id"], "period": int(row["period"]),
            "view": view, "region": row["region"], "process": row["process"],
            **{column: row[column] for column in _TAXONOMY_COLUMNS},
            "attribute": row["Attribute"], "cost_commodity": row["Commodity"],
            "cost_value_m": float(row["cost_value_m"]),
        }
        raw_vintage = pd.to_numeric(row["Vintage"], errors="coerce")
        vintage = int(raw_vintage) if pd.notna(raw_vintage) else pd.NA
        if row["Attribute"] == "Cost_Inv":
            vintage_basis = (
                "Reported Cost_Inv vintage" if pd.notna(vintage)
                else "Cost_Inv without reported vintage"
            )
        elif row["Attribute"] == "Cost_Fom":
            vintage_basis = (
                "Reported Cost_Fom vintage" if pd.notna(vintage)
                else "Annual O&M (no reported vintage)"
            )
        else:
            vintage_basis = "Not vintage-attributed"
        base["vintage"] = vintage
        base["vintage_basis"] = vintage_basis
        if ambiguous:
            reason = "Observed output includes unmapped or incompatible energy."
            assignments.append({
                **base, "resource_pool": "", "assignment_share": 0.0,
                "assigned_cost_m": 0.0, "assignment_status": "Unallocated",
                "assignment_reason": reason,
            })
        elif output_weights and sum(output_weights.values()) > tolerance:
            total_weight = sum(output_weights.values())
            status = "Assigned by observed output" if len(output_weights) == 1 else "Split by observed output share"
            for resource_pool, weight in sorted(output_weights.items()):
                share = weight / total_weight
                assignments.append({
                    **base, "resource_pool": resource_pool, "assignment_share": share,
                    "assigned_cost_m": float(row["cost_value_m"]) * share,
                    "assignment_status": status,
                    "assignment_reason": "Compatible observed output-carrier share.",
                })
        else:
            cost_commodity = str(row["Commodity"] or "").strip().upper()
            direct_pool = catalog_pool.get(cost_commodity, "")
            if direct_pool:
                assignments.append({
                    **base, "resource_pool": direct_pool, "assignment_share": 1.0,
                    "assigned_cost_m": float(row["cost_value_m"]),
                    "assignment_status": "Assigned from cost commodity",
                    "assignment_reason": "No observed mapped output; used the row's mapped Commodity.",
                })
            else:
                assignments.append({
                    **base, "resource_pool": "", "assignment_share": 0.0,
                    "assigned_cost_m": 0.0, "assignment_status": "Unallocated",
                    "assignment_reason": "No compatible mapped output or cost commodity.",
                })

    frame = pd.DataFrame(assignments)
    unallocated = frame[frame["assignment_status"].eq("Unallocated")]
    for _, row in unallocated.iterrows():
        diagnostics.append({
            "diagnostic_type": "unallocated_source_cost", "period": row["period"],
            "process": row["process"], "commodity": row["cost_commodity"],
            "detail": row["assignment_reason"], "value_m": row["cost_value_m"],
        })
    return frame, diagnostics


def _grid_periods(annual_flows: pd.DataFrame, assignments: pd.DataFrame) -> set[int]:
    """The run's milestone periods: every period with flows or cost rows."""
    periods = set(assignments["period"].astype(int))
    if "period" in annual_flows.columns:
        flow_periods = pd.to_numeric(annual_flows["period"], errors="coerce").dropna()
        periods.update(flow_periods.astype(int))
    return periods


def _milestone_spans(grid_periods: Iterable[int]) -> list[tuple[int, int, int]]:
    """Return ``(milestone, first_year, last_year)`` for each OBLONG period."""
    durations = compute_period_durations(sorted({int(period) for period in grid_periods}))
    spans = []
    for milestone in sorted(durations):
        duration = int(durations[milestone])
        start = milestone - duration // 2
        spans.append((milestone, start, start + duration - 1))
    return spans


def _containing_milestone(year: int, spans: list[tuple[int, int, int]]) -> float:
    """Milestone whose period contains ``year``; -inf before the grid, +inf after it."""
    for milestone, start, end in spans:
        if milestone == year:
            return milestone
    for milestone, start, end in spans:
        if start <= year <= end:
            return milestone
    if not spans or year < spans[0][1]:
        return float("-inf")
    return float("inf")


def _choose_share_basis(
    assigned: pd.DataFrame,
    available: dict[str, list[int]],
    spans: list[tuple[int, int, int]],
) -> tuple[list[object], list[str]]:
    """Pick, per assigned cost row, the year whose use mix splits it.

    ``Cost_Inv`` with a reported vintage uses the period it was built in; if
    that period has no use of the pool, the nearest later period with use, up
    to the reporting period.  A vintage before the first model period uses the
    earliest period with use.  Every other row — including ``Cost_Fom``, paid
    in the year it is reported, and ``Cost_Inv`` without a vintage — uses the
    reporting period.  A row with no eligible year stays unallocated.
    """
    years: list[object] = []
    labels: list[str] = []
    for period, pool, attribute, vintage in zip(
        assigned["period"], assigned["resource_pool"], assigned["attribute"], assigned["vintage"]
    ):
        period = int(period)
        eligible = [year for year in available.get(pool, ()) if year <= period]
        choice: int | None
        if attribute == "Cost_Inv" and pd.notna(vintage):
            target = _containing_milestone(int(vintage), spans)
            if target > period:
                choice = period if period in eligible else None
            else:
                choice = next((year for year in eligible if year >= target), None)
            label = SHARE_BASIS_BUILD_YEAR if choice == target else SHARE_BASIS_NEAREST_YEAR
        else:
            choice = period if period in eligible else None
            label = SHARE_BASIS_REPORTING_YEAR
        years.append(pd.NA if choice is None else choice)
        labels.append(SHARE_BASIS_NO_USE if choice is None else label)
    return years, labels


class _PoolAllocation(NamedTuple):
    allocations: pd.DataFrame
    summary: pd.DataFrame
    diagnostics: list[dict]
    row_basis: pd.DataFrame
    origin_shares: pd.DataFrame
    process_allocations: pd.DataFrame
    cascade_allocations: pd.DataFrame
    consumer_use: pd.DataFrame


def _consumer_use(
    consumers: pd.DataFrame, networks: dict[int, CascadeNetwork],
) -> pd.DataFrame:
    """Return each consumer's own-period use with its role in that period's network.

    Pools outside a solved network (no positive use, or no route to final use)
    are omitted, as they are from the cost ledger.
    """
    keys = ["resource_pool", "region", "process", "sector"]
    parts: list[pd.DataFrame] = []
    for year, network in networks.items():
        roles = network.consumer_shares.loc[:, [*keys, "role"]].drop_duplicates()
        if roles.empty:
            continue
        use = consumers[consumers["period"].eq(year)]
        parts.append(roles.merge(use, on=keys, how="inner"))
    if not parts:
        return pd.DataFrame(columns=list(CONSUMER_USE_COLUMNS))
    frame = pd.concat(parts, ignore_index=True).loc[:, list(CONSUMER_USE_COLUMNS)]
    frame["period"] = frame["period"].astype(int)
    return frame.sort_values(list(CONSUMER_USE_COLUMNS[:5])).reset_index(drop=True)


def _allocate_pools(
    quantities: pd.DataFrame,
    assignments: pd.DataFrame,
    tolerance: float,
    *,
    views: Iterable[str],
    grid_periods: Iterable[int],
    quantity_lineage: pd.DataFrame,
    output_lookup: dict[tuple[int, str, str], tuple[dict[str, float], bool]],
) -> _PoolAllocation:
    """Cascade each assigned cost row through its share-basis year's network.

    A row's share-basis year is chosen exactly as before (the build year for
    investment).  Its cost then enters its own pool and is split by that
    year's use mix; the share used by converters is passed on to the pools
    they produce, solved as a full chain.  Without converters this is exactly
    ``row cost x basis-year use share``, the one-step method.
    """
    diagnostics: list[dict] = []
    assigned = assignments[assignments["resource_pool"].ne("")].copy()
    costs = assigned.groupby(
        ["period", "view", "resource_pool"], as_index=False
    )["assigned_cost_m"].sum().rename(columns={"assigned_cost_m": "source_cost_m"})
    denominators = quantities.groupby(
        ["period", "resource_pool"], as_index=False
    )["sector_twh"].sum().rename(columns={"sector_twh": "pool_twh"})
    quantity_keys = quantities[["period", "resource_pool"]].drop_duplicates()
    keys = pd.concat([
        costs[["period", "view", "resource_pool"]],
        *[
            quantity_keys.assign(view=view)[["period", "view", "resource_pool"]]
            for view in views
        ],
    ], ignore_index=True).drop_duplicates()
    summary = keys.merge(costs, on=["period", "view", "resource_pool"], how="left").merge(
        denominators, on=["period", "resource_pool"], how="left"
    )
    summary["source_cost_m"] = summary["source_cost_m"].fillna(0.0)
    summary["pool_twh"] = summary["pool_twh"].fillna(0.0)

    use = quantities.merge(denominators, on=["period", "resource_pool"], how="inner")
    use = use[use["pool_twh"].gt(tolerance)].copy()
    use["allocation_share"] = use["sector_twh"] / use["pool_twh"]
    available = {
        str(pool): sorted({int(period) for period in group["period"]})
        for pool, group in use.groupby("resource_pool")
    }
    basis_years, basis_labels = _choose_share_basis(
        assigned, available, _milestone_spans(grid_periods)
    )
    assigned["share_basis_year"] = pd.array(basis_years, dtype="Int64")
    assigned["share_basis"] = basis_labels
    assigned["sign"] = assigned["assigned_cost_m"].lt(0).map(
        {False: SIGN_COSTS, True: SIGN_CREDITS}
    )

    split_rows = assigned[assigned["share_basis_year"].notna()].copy()
    split_rows["share_basis_year"] = split_rows["share_basis_year"].astype("int64")
    pool_keys = ["period", "view", "resource_pool"]

    consumers = _cascade_consumers(quantity_lineage)
    demand_keys = _demand_output_keys(quantity_lineage, tolerance)
    networks: dict[int, CascadeNetwork] = {}
    for year in sorted({int(year) for year in split_rows["share_basis_year"]}):
        network, network_diagnostics = build_cascade_network(
            year, consumers[consumers["period"].eq(year)], output_lookup,
            demand_output_keys=demand_keys, tolerance=tolerance,
        )
        networks[year] = network
        diagnostics.extend(network_diagnostics)
    # Unit costs need each reporting period's own roles, including periods that
    # are no row's share-basis year; their diagnostics are not cost findings.
    use_networks = dict(networks)
    for year in sorted({int(year) for year in consumers["period"]} - set(networks)):
        use_networks[year], _ = build_cascade_network(
            year, consumers[consumers["period"].eq(year)], output_lookup,
            demand_output_keys=demand_keys, tolerance=tolerance,
        )
    injections = split_rows.groupby(
        [*pool_keys, "sign", "share_basis_year"], as_index=False
    )["assigned_cost_m"].sum().rename(columns={"assigned_cost_m": "cost_m"})
    outcome = apply_cascade(injections, networks)
    no_basis = assigned[assigned["share_basis_year"].isna()].groupby(
        [*pool_keys, "sign"], as_index=False
    )["assigned_cost_m"].sum().rename(columns={"assigned_cost_m": "allocated_cost_m"})
    no_basis = no_basis[no_basis["allocated_cost_m"].ne(0)]
    process = outcome.process_allocations
    if not no_basis.empty:
        process = pd.concat(
            [process, unallocated_ledger_rows(no_basis)], ignore_index=True
        ).groupby(
            list(PROCESS_ALLOCATION_COLUMNS[:-1]), as_index=False, dropna=False, sort=True,
        )["allocated_cost_m"].sum()
    final = process[process["role"].eq(CASCADE_ROLE_FINAL)]
    passed = process[process["role"].eq(CASCADE_ROLE_PASSED_ON)]
    sector_costs = final.groupby(
        [*pool_keys, "sector"], as_index=False, sort=False
    )["allocated_cost_m"].sum()
    sector_passed_on = passed.groupby(
        [*pool_keys, "sector"], as_index=False, sort=False
    )["allocated_cost_m"].sum().rename(columns={"allocated_cost_m": "passed_on_cost_m"})
    passed_in = passed.groupby(
        ["period", "view", "passed_to_pool"], as_index=False
    )["allocated_cost_m"].sum().rename(columns={
        "passed_to_pool": "resource_pool", "allocated_cost_m": "passed_in_cost_m",
    })
    cascade_unallocated = outcome.cascade_unallocated

    summary = pd.concat([
        summary,
        passed_in[pool_keys].merge(summary[pool_keys], how="left", indicator=True)
        .loc[lambda frame: frame["_merge"].eq("left_only"), pool_keys],
        cascade_unallocated[pool_keys].merge(summary[pool_keys], how="left", indicator=True)
        .loc[lambda frame: frame["_merge"].eq("left_only"), pool_keys],
    ], ignore_index=True).drop_duplicates(pool_keys)
    summary = summary.drop(columns="pool_twh").merge(
        denominators, on=["period", "resource_pool"], how="left"
    )
    allocated = sector_costs.groupby(pool_keys)["allocated_cost_m"].sum()
    passed_on = sector_passed_on.groupby(pool_keys)["passed_on_cost_m"].sum()
    unallocated = assigned[assigned["share_basis_year"].isna()].groupby(pool_keys)[
        "assigned_cost_m"
    ].sum().rename("denominator_unallocated_cost_m")
    for frame in (
        allocated.reset_index(), passed_on.reset_index(), unallocated.reset_index(),
        passed_in, cascade_unallocated,
    ):
        summary = summary.merge(frame, on=pool_keys, how="left")
    for column in (
        "source_cost_m", "pool_twh", "passed_in_cost_m", "allocated_cost_m",
        "passed_on_cost_m", "denominator_unallocated_cost_m", "cascade_unallocated_cost_m",
    ):
        summary[column] = pd.to_numeric(summary[column], errors="coerce").fillna(0.0)
    summary["period"] = summary["period"].astype(int)
    summary["pool_cost_m"] = summary["source_cost_m"] + summary["passed_in_cost_m"]
    has_use = (
        summary["pool_twh"].gt(tolerance)
        | summary["allocated_cost_m"].abs().gt(tolerance)
        | summary["passed_on_cost_m"].abs().gt(tolerance)
    )
    summary["allocation_status"] = "Available"
    summary.loc[~has_use, "allocation_status"] = "Non-positive denominator"
    summary = summary.loc[:, [
        column for column in _POOL_SUMMARY_COLUMNS if column != "pool_kind"
    ]]

    reporting_sectors = summary.loc[
        summary["pool_twh"].gt(tolerance), pool_keys
    ].merge(use[["period", "resource_pool", "sector"]], on=["period", "resource_pool"])
    allocation_keys = pd.concat([
        reporting_sectors,
        sector_costs[[*pool_keys, "sector"]],
        sector_passed_on[[*pool_keys, "sector"]],
    ], ignore_index=True).drop_duplicates()
    allocations = allocation_keys.merge(
        quantities[["period", "resource_pool", "sector", "sector_twh"]],
        on=["period", "resource_pool", "sector"], how="left",
    ).merge(
        use[["period", "resource_pool", "sector", "allocation_share"]],
        on=["period", "resource_pool", "sector"], how="left",
    ).merge(
        summary[[*pool_keys, "pool_twh", "source_cost_m", "passed_in_cost_m", "pool_cost_m"]],
        on=pool_keys, how="left",
    ).merge(sector_costs, on=[*pool_keys, "sector"], how="left").merge(
        sector_passed_on, on=[*pool_keys, "sector"], how="left",
    )
    for column in ("sector_twh", "allocation_share", "allocated_cost_m", "passed_on_cost_m"):
        allocations[column] = pd.to_numeric(allocations[column], errors="coerce").fillna(0.0)
    allocations["period"] = allocations["period"].astype(int)
    material = allocations["pool_cost_m"].abs().gt(tolerance)
    # The sector's share of everything entering the pool, kept or passed on.
    allocations["cost_share"] = (
        (allocations["allocated_cost_m"] + allocations["passed_on_cost_m"])
        / allocations["pool_cost_m"].where(material)
    )
    allocations["allocation_status"] = "Available"
    # pool_kind is attached once by the caller from the curated sheet.
    allocations = allocations.loc[:, [
        column for column in _empty_result().allocations.columns if column != "pool_kind"
    ]]

    for _, row in summary[summary["denominator_unallocated_cost_m"].abs().gt(tolerance)].iterrows():
        diagnostics.append({
            "diagnostic_type": "non_positive_allocation_denominator",
            "period": row["period"], "process": "", "commodity": row["resource_pool"],
            "detail": "Source cost remains unallocated because total signed TWh is non-positive in every eligible year.",
            "value_m": row["denominator_unallocated_cost_m"],
        })
    for _, row in summary[summary["cascade_unallocated_cost_m"].abs().gt(tolerance)].iterrows():
        diagnostics.append({
            "diagnostic_type": "cascade_unallocated_cost",
            "period": row["period"], "process": "", "commodity": row["resource_pool"],
            "detail": "Cost in this pool stays unallocated because, in "
            "its share-basis year, the pool has no positive use or never reaches final use.",
            "value_m": row["cascade_unallocated_cost_m"],
        })
    nearest = assigned[assigned["share_basis"].eq(SHARE_BASIS_NEAREST_YEAR)].groupby(
        pool_keys, as_index=False
    )["assigned_cost_m"].sum()
    for _, row in nearest[nearest["assigned_cost_m"].abs().gt(tolerance)].iterrows():
        diagnostics.append({
            "diagnostic_type": "share_basis_nearest_available_year",
            "period": row["period"], "process": "", "commodity": row["resource_pool"],
            "detail": "Investment cost split by the nearest available year's use mix "
            "because its build year had no recorded use.",
            "value_m": row["assigned_cost_m"],
        })
    row_basis = assigned.loc[:, [
        "view", "source_row_id", "resource_pool", "share_basis_year", "share_basis",
    ]]
    return _PoolAllocation(
        allocations=allocations,
        summary=summary,
        diagnostics=diagnostics,
        row_basis=row_basis,
        origin_shares=origin_sector_shares(networks),
        process_allocations=process,
        cascade_allocations=outcome.cascade_allocations,
        consumer_use=_consumer_use(consumers, use_networks),
    )


def _cascade_consumers(quantity_lineage: pd.DataFrame) -> pd.DataFrame:
    """Included signed use per consumer process, the denominator of every pool."""
    columns = ["period", "resource_pool", "region", "process", "sector", "quantity_twh"]
    if quantity_lineage.empty or "included" not in quantity_lineage.columns:
        return pd.DataFrame(columns=columns)
    included = quantity_lineage[quantity_lineage["included"].fillna(False).astype(bool)]
    return included.groupby(
        columns[:-1], as_index=False, dropna=False
    )["quantity_twh"].sum()


def _demand_output_keys(
    quantity_lineage: pd.DataFrame, tolerance: float,
) -> set[tuple[int, str, str]]:
    """Return ``(period, region, process)`` keys that deliver a demand service."""
    if quantity_lineage.empty or "direction" not in quantity_lineage.columns:
        return set()
    lineage = quantity_lineage

    def text(column: str) -> pd.Series:
        return _clean_text(lineage.get(column, pd.Series("", index=lineage.index)))

    demand = (
        lineage["direction"].eq("OUT")
        & pd.to_numeric(lineage["value"], errors="coerce").abs().gt(tolerance)
        & (
            text("commodity_set").str.upper().eq("DEM")
            | text("commodity_type").str.casefold().eq("demand")
            | text("mapping_status").eq("Demand service")
        )
    )
    rows = lineage.loc[demand, ["period", "region", "process"]].drop_duplicates()
    return {
        (int(period), str(region), str(process))
        for period, region, process in rows.itertuples(index=False)
    }


def prepare_resource_allocation(
    costs: pd.DataFrame,
    annual_flows: pd.DataFrame,
    *,
    commodity_labels: pd.DataFrame | None = None,
    pools: pd.DataFrame | None = None,
    supply_processes: Iterable[str] | None = None,
    periods: Iterable[int] | None = None,
    tolerance: float = 1e-9,
    views: Iterable[str] | None = None,
) -> ResourceAllocationResult:
    """Prepare requested non-additive allocation views for one selected run.

    When ``views`` is omitted, this retains the legacy all-view result:
    fuel/flow allocation, upstream supply-chain allocation, and the separate
    commodity/policy disclosure.
    """
    if tolerance < 0:
        raise ValueError("tolerance must be non-negative.")
    selected_views = normalise_resource_views(views)
    if annual_flows is None:
        annual_flows = pd.DataFrame()
    if costs is None:
        costs = pd.DataFrame()
    if costs.empty and annual_flows.empty:
        return _empty_result()
    selected_periods = None if periods is None else {int(period) for period in periods}
    selected_supply = (
        None if supply_processes is None
        else {str(process).strip().upper() for process in supply_processes if str(process).strip()}
    )
    selected_pools = load_resource_pools() if pools is None else pools.copy()
    catalog = _commodity_catalog(annual_flows, commodity_labels)
    mapping = map_resource_pools(catalog, selected_pools)
    quantities, quantity_lineage, diagnostics = _prepare_quantities(
        annual_flows, mapping, selected_periods, tolerance
    )

    assignment_frames: list[pd.DataFrame] = []
    output_lookup = _build_output_pool_lookup(quantity_lineage, tolerance)
    for view in selected_views:
        assigned, assignment_diagnostics = _assign_cost_rows(
            costs, mapping, output_lookup,
            view=view, attributes=view_cost_attributes(view), supply_processes=selected_supply,
            periods=selected_periods, tolerance=tolerance,
        )
        if not assigned.empty:
            assignment_frames.append(assigned)
        diagnostics.extend(assignment_diagnostics)
    cost_assignments = (
        pd.concat(assignment_frames, ignore_index=True)
        if assignment_frames else pd.DataFrame()
    )
    if cost_assignments.empty:
        view_summary = _empty_result().view_summary
    else:
        source_rows = cost_assignments.drop_duplicates(["view", "source_row_id"])
        source_total = source_rows.groupby("view").agg(
            source_cost_m=("cost_value_m", "sum"),
            source_row_count=("source_row_id", "nunique"),
        )
        assigned_total = cost_assignments.groupby("view")["assigned_cost_m"].sum().rename(
            "assigned_cost_m"
        )
        unassigned_total = source_rows[
            source_rows["assignment_status"].eq("Unallocated")
        ].groupby("view")["cost_value_m"].sum().rename("unassigned_source_cost_m")
        view_summary = source_total.join(assigned_total, how="left").join(
            unassigned_total, how="left"
        ).fillna(0.0).reset_index()
        view_summary["reconciliation_difference_m"] = (
            view_summary["source_cost_m"]
            - view_summary["assigned_cost_m"]
            - view_summary["unassigned_source_cost_m"]
        )
    allocatable_views = tuple(
        view for view in selected_views if view != SEPARATE_COMMODITY_VIEW
    )
    allocatable_assignments = cost_assignments[
        cost_assignments.get("view", pd.Series(dtype=str)).isin(allocatable_views)
    ].copy() if not cost_assignments.empty else pd.DataFrame()
    if allocatable_assignments.empty:
        allocations = _empty_result().allocations
        summary = _empty_result().pool_summary
        row_basis = pd.DataFrame(columns=[
            "view", "source_row_id", "resource_pool", "share_basis_year", "share_basis",
        ])
        origin_shares = pd.DataFrame(
            columns=["share_basis_year", "resource_pool", "sector", "cascade_share"]
        )
        process_allocations = _empty_result().process_allocations
        cascade_allocations = _empty_result().cascade_allocations
        consumer_use = _empty_result().consumer_use
    else:
        pooled = _allocate_pools(
            quantities, allocatable_assignments, tolerance, views=allocatable_views,
            grid_periods=_grid_periods(annual_flows, allocatable_assignments),
            quantity_lineage=quantity_lineage, output_lookup=output_lookup,
        )
        allocations, summary, row_basis = pooled.allocations, pooled.summary, pooled.row_basis
        origin_shares = pooled.origin_shares
        process_allocations = pooled.process_allocations
        cascade_allocations = pooled.cascade_allocations
        consumer_use = pooled.consumer_use
        diagnostics.extend(pooled.diagnostics)
    if not view_summary.empty:
        allocation_totals = summary.groupby("view")[[
            "allocated_cost_m", "denominator_unallocated_cost_m", "cascade_unallocated_cost_m",
        ]].sum()
        view_summary = view_summary.merge(
            allocation_totals.reset_index(), on="view", how="left"
        )
        allocatable = view_summary["view"].isin(allocatable_views)
        for column in allocation_totals.columns:
            view_summary[column] = view_summary[column].where(
                ~allocatable, view_summary[column].fillna(0.0)
            )
        # Assigned cost must end as final use or be reported as unallocated.
        view_summary["allocation_difference_m"] = (
            view_summary["assigned_cost_m"]
            - view_summary["allocated_cost_m"]
            - view_summary["denominator_unallocated_cost_m"]
            - view_summary["cascade_unallocated_cost_m"]
        )
        view_summary = view_summary.loc[:, list(_empty_result().view_summary.columns)]
    if not cost_assignments.empty:
        cost_assignments = cost_assignments.merge(
            row_basis, on=["view", "source_row_id", "resource_pool"], how="left"
        )
        cost_assignments["share_basis_year"] = cost_assignments["share_basis_year"].astype("Int64")
        cost_assignments["share_basis"] = cost_assignments["share_basis"].fillna("")

    commodity_rows = cost_assignments[
        cost_assignments.get("view", pd.Series(dtype=str)).eq(SEPARATE_COMMODITY_VIEW)
    ].copy() if not cost_assignments.empty else pd.DataFrame()
    if commodity_rows.empty:
        commodity_summary = pd.DataFrame(columns=[
            "period", "resource_pool", "assigned_cost_m", "unallocated_cost_m"
        ])
    else:
        commodity_rows["assigned_component_m"] = commodity_rows["assigned_cost_m"]
        commodity_rows["unallocated_component_m"] = commodity_rows["cost_value_m"].where(
            commodity_rows["assignment_status"].eq("Unallocated"), 0.0
        )
        commodity_summary = commodity_rows.groupby(
            ["period", "resource_pool"], as_index=False, dropna=False
        ).agg(
            assigned_cost_m=("assigned_component_m", "sum"),
            unallocated_cost_m=("unallocated_component_m", "sum"),
        )

    diagnostics_frame = pd.DataFrame(diagnostics, columns=[
        "diagnostic_type", "period", "process", "commodity", "detail", "value_m"
    ]).drop_duplicates()
    vintage_columns = [
        "source_row_id", "period", "view", "resource_pool", "process", "process_name",
        "attribute", "vintage", "vintage_basis", "share_basis_year", "share_basis",
        "sector", "allocation_share", "assigned_cost_m", "allocated_cost_m",
    ]
    vintage_rows = cost_assignments[
        cost_assignments.get("attribute", pd.Series(dtype=str)).isin(("Cost_Inv", "Cost_Fom"))
        & cost_assignments.get("resource_pool", pd.Series(dtype=str)).ne("")
        & cost_assignments.get("view", pd.Series(dtype=str)).isin(allocatable_views)
    ].copy() if not cost_assignments.empty else pd.DataFrame(columns=vintage_columns)
    if vintage_rows.empty:
        vintage_allocations = pd.DataFrame(columns=vintage_columns)
    else:
        # Each row follows the conversion network of its own share-basis year
        # from its pool to final use, so the detail sums to the headline
        # (by origin pool) by construction.  ``allocation_share`` is the share
        # of this row's cost reaching the sector.
        basis_shares = origin_shares.rename(columns={"cascade_share": "allocation_share"})
        basis_shares["share_basis_year"] = basis_shares["share_basis_year"].astype("Int64")
        vintage_allocations = vintage_rows.merge(
            basis_shares, on=["share_basis_year", "resource_pool"], how="left"
        )
        vintage_allocations["allocation_share"] = pd.to_numeric(
            vintage_allocations["allocation_share"], errors="coerce"
        ).fillna(0.0)
        vintage_allocations["sector"] = _clean_text(
            vintage_allocations["sector"], "Unallocated"
        )
        vintage_allocations["allocated_cost_m"] = (
            vintage_allocations["assigned_cost_m"]
            * vintage_allocations["allocation_share"]
        )
        vintage_allocations = vintage_allocations.groupby(
            vintage_columns[:12], as_index=False, dropna=False
        ).agg(
            allocation_share=("allocation_share", "sum"),
            assigned_cost_m=("assigned_cost_m", "first"),
            allocated_cost_m=("allocated_cost_m", "sum"),
        ).sort_values(
            ["view", "period", "attribute", "vintage", "resource_pool", "process", "sector"],
            na_position="last",
        ).reset_index(drop=True)
    kinds = resource_pool_kinds(selected_pools)
    allocations = _attach_pool_kind(allocations, kinds)
    summary = _attach_pool_kind(summary, kinds)
    vintage_allocations = _attach_pool_kind(vintage_allocations, kinds)
    process_allocations = _attach_pool_kind(process_allocations, kinds)
    cascade_allocations = _attach_pool_kind(
        cascade_allocations.drop(columns="origin_pool_kind", errors="ignore"), kinds
    )
    cascade_allocations.insert(
        cascade_allocations.columns.get_loc("origin_pool") + 1, "origin_pool_kind",
        cascade_allocations["origin_pool"].map(kinds).fillna("")
        if len(cascade_allocations) else pd.Series(dtype=str),
    )
    return ResourceAllocationResult(
        allocations=allocations.sort_values(
            ["view", "period", "resource_pool", "sector"]
        ).reset_index(drop=True),
        quantities=quantities.sort_values(
            ["period", "resource_pool", "sector"]
        ).reset_index(drop=True),
        quantity_lineage=quantity_lineage,
        cost_assignments=cost_assignments,
        pool_summary=summary.sort_values(
            ["view", "period", "resource_pool"]
        ).reset_index(drop=True),
        view_summary=view_summary,
        commodity_cost_summary=commodity_summary,
        commodity_mapping=mapping,
        diagnostics=diagnostics_frame,
        vintage_allocations=vintage_allocations,
        process_allocations=process_allocations.sort_values(
            ["view", "period", "resource_pool", "role", "sector", "process"]
        ).reset_index(drop=True),
        cascade_allocations=cascade_allocations.sort_values(
            ["view", "period", "origin_pool", "resource_pool", "sector"]
        ).reset_index(drop=True),
        consumer_use=consumer_use,
    )


POOL_UNALLOCATED_SECTOR = "Unallocated"


def pool_sector_cost_rows(result: ResourceAllocationResult, view: str) -> pd.DataFrame:
    """Return each pool's full cost by sector, matching the pool's Sankey node.

    A sector's cost in a pool is its final use plus the share it uses to make
    another pool (a power station's gas), so a pool's rows add up to its own
    source cost plus cost entering from other pools.  Cost with no route to
    final use is a separate ``Unallocated`` sector.  Pools that feed each
    other therefore both show the conversion cost: rows are pool totals and
    must not be added across pools as a system total.
    """
    allocations = result.allocations[result.allocations["view"].eq(view)].copy()
    allocations["allocated_cost_m"] = (
        pd.to_numeric(allocations["allocated_cost_m"], errors="coerce").fillna(0.0)
        + pd.to_numeric(allocations["passed_on_cost_m"], errors="coerce").fillna(0.0)
    )
    allocations["passed_on_cost_m"] = 0.0

    summary = result.pool_summary[result.pool_summary["view"].eq(view)].copy()
    unallocated_columns = [
        column for column in ("denominator_unallocated_cost_m", "cascade_unallocated_cost_m")
        if column in summary.columns
    ]
    summary["allocated_cost_m"] = sum(
        (pd.to_numeric(summary[column], errors="coerce").fillna(0.0)
         for column in unallocated_columns),
        start=pd.Series(0.0, index=summary.index),
    )
    unallocated = summary.loc[
        summary["allocated_cost_m"].ne(0),
        [column for column in (
            "period", "view", "resource_pool", "pool_kind", "pool_twh",
            "source_cost_m", "passed_in_cost_m", "pool_cost_m", "allocated_cost_m",
        ) if column in summary.columns],
    ].assign(sector=POOL_UNALLOCATED_SECTOR, passed_on_cost_m=0.0)
    if unallocated.empty:
        return allocations.reset_index(drop=True)
    unallocated = unallocated.reindex(columns=allocations.columns)
    if allocations.empty:
        return unallocated.reset_index(drop=True)
    return pd.concat([allocations, unallocated], ignore_index=True)


# Grouping options for final-user unit costs: every process label, plus region.
UNIT_COST_PROCESS_BREAKDOWN = "Individual processes"
FINAL_USER_UNIT_COST_BREAKDOWNS = {
    **RESOURCE_PATHWAY_SANKEY_AGGREGATIONS,
    "Region": "region",
}
UNIT_COST_UNLABELLED = "Unlabelled"
UNIT_COST_STATUS_AVAILABLE = "Available"
UNIT_COST_STATUS_NO_USE = "Cost without reporting-year use"
UNIT_COST_STATUS_NON_POSITIVE = "Non-positive use"
UNIT_COST_ROW_COLUMNS = (
    "period", "group", "allocated_cost_m", "final_use_twh", "unit_cost", "status",
)
UNIT_COST_TOTAL_COLUMNS = (
    "period", "source_cost_m", "passed_in_cost_m", "pool_cost_m", "passed_on_cost_m",
    "unallocated_cost_m", "allocated_cost_m", "final_use_twh", "unit_cost", "status",
)


@dataclass(frozen=True)
class UnitCostResult:
    """Final-user unit costs of one pool and their per-period reconciliation.

    ``unit_cost`` is £M/yr per TWh/yr, numerically equal to £/MWh.
    """

    rows: pd.DataFrame
    totals: pd.DataFrame


def _unit_cost_status(cost: pd.Series, twh: pd.Series, tolerance: float) -> tuple[pd.Series, pd.Series]:
    available = twh.gt(tolerance)
    unit_cost = (cost / twh.where(available)).astype(float)
    status = pd.Series(UNIT_COST_STATUS_AVAILABLE, index=twh.index)
    status[twh.abs().le(tolerance)] = UNIT_COST_STATUS_NO_USE
    status[twh.lt(-tolerance)] = UNIT_COST_STATUS_NON_POSITIVE
    return unit_cost, status


def final_user_unit_costs(
    result: ResourceAllocationResult,
    *,
    resource_pool: str,
    view: str = UPSTREAM_SUPPLY_VIEW,
    breakdown: str = "Sector",
    tolerance: float = 1e-9,
) -> UnitCostResult:
    """Return each final user's attributed cost per unit of its use of one pool.

    The cost is the net (costs plus credits) final-use cost of ``resource_pool``
    in the cascade ledger, so it includes cost passed in from the pools used
    to make it.  The use is each final user's own use of the pool in the
    reporting period.  Converters pass their share on and are excluded from
    both.  Each group's value is a ratio of sums.  Investment cost split by
    its build year's users may reach a user with no use this period; that
    cost is kept, with no unit cost, as ``UNIT_COST_STATUS_NO_USE``.
    """
    if breakdown not in FINAL_USER_UNIT_COST_BREAKDOWNS:
        raise ValueError(f"Unsupported unit-cost breakdown: {breakdown}.")
    if view == SEPARATE_COMMODITY_VIEW:
        raise ValueError(f"Unsupported unit-cost view: {view}.")
    view_cost_attributes(view)
    pool = str(resource_pool).strip()
    keys = ["period", "region", "process"]

    ledger = result.process_allocations
    ledger = ledger[
        ledger["view"].eq(view) & ledger["resource_pool"].eq(pool)
        & ledger["role"].eq(CASCADE_ROLE_FINAL)
    ]
    cost = ledger.loc[:, keys].assign(
        allocated_cost_m=pd.to_numeric(ledger["allocated_cost_m"], errors="coerce").fillna(0.0),
        final_use_twh=0.0,
    )
    use = result.consumer_use
    use = use[use["resource_pool"].eq(pool) & use["role"].eq(CASCADE_ROLE_FINAL)]
    use = use.loc[:, keys].assign(
        allocated_cost_m=0.0,
        final_use_twh=pd.to_numeric(use["quantity_twh"], errors="coerce").fillna(0.0),
    )
    frame = pd.concat([cost, use], ignore_index=True)
    frame["period"] = pd.to_numeric(frame["period"], errors="coerce")
    frame = frame.dropna(subset=["period"])
    frame["period"] = frame["period"].astype(int)
    for column in ("region", "process"):
        frame[column] = frame[column].fillna("").astype(str)

    field = FINAL_USER_UNIT_COST_BREAKDOWNS[breakdown]
    labelled: list[pd.DataFrame] = []
    for period, rows in frame.groupby("period", sort=True):
        if field == "region":
            labels = rows["region"]
        else:
            taxonomy = _ledger_taxonomy(result.quantity_lineage, int(period))
            rows = rows.merge(taxonomy, on=["region", "process"], how="left")
            if field is None:
                names = _clean_text(rows["process_name"].fillna(""))
                labels = rows["process"].where(
                    names.eq("") | names.eq(rows["process"]),
                    names + " (" + rows["process"] + ")",
                )
            else:
                labels = rows[field]
        rows = rows.assign(group=_clean_text(labels.fillna(""), UNIT_COST_UNLABELLED))
        labelled.append(rows.loc[:, ["period", "group", "allocated_cost_m", "final_use_twh"]])
    if labelled:
        grouped = pd.concat(labelled, ignore_index=True).groupby(
            ["period", "group"], as_index=False, sort=True
        )[["allocated_cost_m", "final_use_twh"]].sum()
        grouped = grouped[
            grouped["allocated_cost_m"].abs().gt(tolerance)
            | grouped["final_use_twh"].abs().gt(tolerance)
        ].reset_index(drop=True)
    else:
        grouped = pd.DataFrame(columns=list(UNIT_COST_ROW_COLUMNS[:4])).astype({
            "period": int, "group": str, "allocated_cost_m": float, "final_use_twh": float,
        })
    grouped["unit_cost"], grouped["status"] = _unit_cost_status(
        grouped["allocated_cost_m"], grouped["final_use_twh"], tolerance
    )
    rows_out = grouped.loc[:, list(UNIT_COST_ROW_COLUMNS)]

    summary = result.pool_summary
    summary = summary[summary["view"].eq(view) & summary["resource_pool"].eq(pool)].copy()
    summary["period"] = pd.to_numeric(summary["period"], errors="coerce").astype(int)
    for column in (
        "source_cost_m", "passed_in_cost_m", "pool_cost_m", "passed_on_cost_m",
        "denominator_unallocated_cost_m", "cascade_unallocated_cost_m",
    ):
        summary[column] = pd.to_numeric(summary[column], errors="coerce").fillna(0.0)
    summary["unallocated_cost_m"] = (
        summary["denominator_unallocated_cost_m"] + summary["cascade_unallocated_cost_m"]
    )
    final = rows_out.groupby("period", as_index=False)[
        ["allocated_cost_m", "final_use_twh"]
    ].sum()
    totals = summary.loc[:, [
        "period", "source_cost_m", "passed_in_cost_m", "pool_cost_m",
        "passed_on_cost_m", "unallocated_cost_m",
    ]].merge(final, on="period", how="outer")
    for column in UNIT_COST_TOTAL_COLUMNS[1:8]:
        totals[column] = pd.to_numeric(totals[column], errors="coerce").fillna(0.0)
    totals["unit_cost"], totals["status"] = _unit_cost_status(
        totals["allocated_cost_m"], totals["final_use_twh"], tolerance
    )
    totals = totals[
        totals[list(UNIT_COST_TOTAL_COLUMNS[1:8])].abs().gt(tolerance).any(axis=1)
    ]
    return UnitCostResult(
        rows=rows_out.reset_index(drop=True),
        totals=totals.loc[:, list(UNIT_COST_TOTAL_COLUMNS)].sort_values("period")
        .reset_index(drop=True),
    )


def interpolate_milestone_allocations(allocations: pd.DataFrame) -> pd.DataFrame:
    """Linearly interpolate display years while preserving milestone rows exactly."""
    if allocations.empty:
        result = allocations.copy()
        result["year_type"] = pd.Series(dtype=str)
        return result
    required = {"period", "view", "resource_pool", "sector"}
    if not required.issubset(allocations.columns):
        raise ValueError("Allocations are missing period, view, resource_pool, or sector.")
    group_columns = ["view", "resource_pool", "sector"]
    numeric_columns = [
        column for column in (
            "sector_twh", "pool_twh", "allocation_share", "source_cost_m",
            "passed_in_cost_m", "pool_cost_m", "allocated_cost_m", "passed_on_cost_m",
        ) if column in allocations.columns
    ]
    parts: list[pd.DataFrame] = []
    for keys, group in allocations.groupby(group_columns, dropna=False, sort=False):
        group = group.sort_values("period").drop_duplicates("period", keep="first")
        years = range(int(group["period"].min()), int(group["period"].max()) + 1)
        expanded = group.set_index("period").reindex(years)
        for column, value in zip(group_columns, keys):
            expanded[column] = value
        expanded[numeric_columns] = expanded[numeric_columns].interpolate(method="linear")
        if {"cost_share", "allocated_cost_m", "passed_on_cost_m", "pool_cost_m"}.issubset(
            expanded.columns
        ):
            # Same definition as the engine: the sector's share of all cost
            # entering the pool, whether booked to it or passed on.
            pool_cost = expanded["pool_cost_m"]
            expanded["cost_share"] = (
                expanded["allocated_cost_m"] + expanded["passed_on_cost_m"]
            ) / pool_cost.where(pool_cost.abs().gt(1e-12))
        elif {"cost_share", "allocated_cost_m", "source_cost_m"}.issubset(expanded.columns):
            source = expanded["source_cost_m"]
            expanded["cost_share"] = expanded["allocated_cost_m"] / source.where(source.abs().gt(1e-12))
        milestone_years = set(group["period"].astype(int))
        expanded["year_type"] = [
            "Milestone" if int(year) in milestone_years else "Interpolated"
            for year in expanded.index
        ]
        if "allocation_status" in expanded.columns:
            expanded["allocation_status"] = expanded["allocation_status"].fillna("Interpolated")
        parts.append(expanded.reset_index(names="period"))
    return pd.concat(parts, ignore_index=True).sort_values(
        ["view", "period", "resource_pool", "sector"]
    ).reset_index(drop=True)
