"""Layout, vocabulary and cell-reference helpers for the traceable workbook.

The Resource & Fuel workbook is written as numbered sheets in calculation
order so a reader can follow raw model evidence through to the pooled totals.
Sheets numbered 10-19 hold raw values, 20-31 hold the milestone and annual
primitives, 40-44 hold the allocation itself, 50-58 hold pathway evidence and
90-91 hold audit material.  Every derived cell is written as a live Excel
formula that points at the sheet before it, so the whole chain can be walked
in Excel with ``Ctrl+[``.

This module owns three concerns that the writer must agree on:

* the ordered sheet registry and its names,
* the plain-English column vocabulary and the glossary that explains it,
* absolute A1 reference construction.

It deliberately has no ``openpyxl`` dependency so the layout contract can be
imported and tested without the writer.
"""
from __future__ import annotations

from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Sheet registry
# ---------------------------------------------------------------------------

READ_ME_SHEET = "00 Read me"
GLOSSARY_SHEET = "01 Glossary"
POOLING_RULES_SHEET = "02 How pooling works"
WORKED_EXAMPLE_SHEET = "03 Worked example"

FLOW_PROVENANCE_SHEET = "04 VD to annual flows"
UNIT_CONVERSION_SHEET = "05 Unit conversion"
SOURCE_COST_ASSIGNMENT_SHEET = "09 Source cost assignment"

RAW_FUEL_USE_SHEET = "10 Raw fuel use"
RAW_SOURCE_COST_SHEET = "11 Raw source costs"
RAW_POOL_SUMMARY_SHEET = "12 Raw pool summary"
FUEL_TYPE_MAPPING_SHEET = "13 Fuel type mapping"
MODEL_CONNECTIONS_SHEET = "14 Model connections"
RAW_POWER_COST_SHEET = "15 Raw power costs"
COMMODITY_COST_SHEET = "16 Commodity costs separate"
VINTAGE_DETAIL_SHEET = "17 Vintage detail"
RAW_ALLOCATION_SHEET = "18 Raw allocation summary"
COMMODITY_COVERAGE_SHEET = "19 Which fuels are counted"

MILESTONE_FUEL_USE_SHEET = "20 Milestone fuel use"
MILESTONE_SOURCE_COST_SHEET = "21 Milestone source costs"

ANNUAL_FUEL_USE_SHEET = "30 Annual fuel use"
ANNUAL_SOURCE_COST_SHEET = "31 Annual source costs"

ALLOCATION_SHARE_SHEET = "40 Allocation shares"
ALLOCATED_COST_SHEET = "41 Allocated fuel costs"
ELECTRICITY_COST_SHEET = "42 Electricity costs"
RESOURCE_COST_SHEET = "43 Resource costs by sector"
FUELEX_SHEET = "44 Fuelex summary"

PATHWAY_LINKS_SHEET = "50 Pathway links"
PATHWAY_PROCESS_NET_SHEET = "51 Pathway process net"
PATHWAY_SOURCE_SHEET = "52 Pathway source output"
PATHWAY_DEMAND_SHEET = "53 Pathway demand input"
PATHWAY_BALANCE_SHEET = "54 Exact code balance"
PATHWAY_UNMATCHED_SHEET = "55 Pathway unmatched"
PATHWAY_HIDDEN_SHEET = "56 Pathway hidden"
PATHWAY_NOTES_SHEET = "57 Pathway notes"
SUPPLY_CHAIN_LINKS_SHEET = "58 Supply chain cost links"

CHECKS_SHEET = "90 Checks"
TECHNICAL_NOTES_SHEET = "91 Technical notes"


@dataclass(frozen=True)
class SheetSpec:
    """One sheet's identity and the role it plays in the calculation chain."""

    name: str
    stage: str
    description: str


SHEET_REGISTRY: tuple[SheetSpec, ...] = (
    SheetSpec(READ_ME_SHEET, "Guide", "How to read the workbook and trace any number back to raw model evidence."),
    SheetSpec(GLOSSARY_SHEET, "Guide", "Plain-English meaning of every retained model code, unit and term."),
    SheetSpec(POOLING_RULES_SHEET, "Guide", "The curated sheet that puts each commodity code in one resource pool."),
    SheetSpec(WORKED_EXAMPLE_SHEET, "Guide", "One allocated cost followed step by step, from reported flows to the final figure."),
    SheetSpec(FLOW_PROVENANCE_SHEET, "Raw evidence", "Every reported flow as first read from the run, showing how many result rows were combined."),
    SheetSpec(UNIT_CONVERSION_SHEET, "Raw evidence", "How each reported energy unit is converted to TWh before anything is added up."),
    SheetSpec(SOURCE_COST_ASSIGNMENT_SHEET, "Raw evidence", "Every reported source-cost row and the reviewed resource-pool assignment applied to it."),
    SheetSpec(RAW_FUEL_USE_SHEET, "Raw evidence", "Every fuel-use row taken from the selected run, including rows excluded from allocation."),
    SheetSpec(RAW_SOURCE_COST_SHEET, "Derived evidence", "Source costs totalled from the assigned process rows by model year, view and resource pool."),
    SheetSpec(RAW_POOL_SUMMARY_SHEET, "Raw evidence", "The run's own summary of each resource pool, kept for cross-checking."),
    SheetSpec(FUEL_TYPE_MAPPING_SHEET, "Raw evidence", "How each model commodity was grouped into a resource pool and reported fuel type."),
    SheetSpec(MODEL_CONNECTIONS_SHEET, "Raw evidence", "Process-to-commodity connections used to identify the supply chain."),
    SheetSpec(RAW_POWER_COST_SHEET, "Raw evidence", "Core cost rows on validated power-system processes, used for the electricity base cost."),
    SheetSpec(COMMODITY_COST_SHEET, "Raw evidence", "Commodity costs, disclosed separately and never added into the attribution."),
    SheetSpec(VINTAGE_DETAIL_SHEET, "Raw evidence", "Reported construction vintages behind investment and fixed operating costs."),
    SheetSpec(RAW_ALLOCATION_SHEET, "Raw evidence", "The application's own allocation rows, for comparison with the formulas here."),
    SheetSpec(COMMODITY_COVERAGE_SHEET, "Raw evidence", "Every fuel that carries energy, and whether its use counts towards the cost split."),
    SheetSpec(MILESTONE_FUEL_USE_SHEET, "Milestone", "Fuel use totalled from raw evidence for each model milestone year."),
    SheetSpec(MILESTONE_SOURCE_COST_SHEET, "Milestone", "Source cost totalled from raw evidence for each model milestone year."),
    SheetSpec(ANNUAL_FUEL_USE_SHEET, "Annual", "Fuel use interpolated in a straight line between milestone years."),
    SheetSpec(ANNUAL_SOURCE_COST_SHEET, "Annual", "Source cost interpolated in a straight line between milestone years."),
    SheetSpec(ALLOCATION_SHARE_SHEET, "Allocation", "Each sector's share of total use of a fuel, for every year."),
    SheetSpec(ALLOCATED_COST_SHEET, "Allocation", "Source cost multiplied by each sector's share of use."),
    SheetSpec(ELECTRICITY_COST_SHEET, "Allocation", "Electricity base cost and its distribution to electricity-using sectors."),
    SheetSpec(RESOURCE_COST_SHEET, "Allocation", "Allocated cost for every sector and fuel, brought together in one table."),
    SheetSpec(FUELEX_SHEET, "Allocation", "Grouped fuel-cost measures per sector, in billions of pounds."),
    SheetSpec(PATHWAY_LINKS_SHEET, "Pathways", "Physical supply-to-demand links for every model year, resource pool and fuel."),
    SheetSpec(PATHWAY_PROCESS_NET_SHEET, "Pathways", "Net physical position of each process behind the pathway links."),
    SheetSpec(PATHWAY_SOURCE_SHEET, "Pathways", "Output-side evidence behind the supply end of each pathway."),
    SheetSpec(PATHWAY_DEMAND_SHEET, "Pathways", "Input-side evidence behind the demand end of each pathway."),
    SheetSpec(PATHWAY_BALANCE_SHEET, "Pathways", "Balance audit for each exact commodity code within a pathway."),
    SheetSpec(PATHWAY_UNMATCHED_SHEET, "Pathways", "Reported rows that could not be matched to a model connection."),
    SheetSpec(PATHWAY_HIDDEN_SHEET, "Pathways", "Valid rows hidden from the on-screen chart but retained in full here."),
    SheetSpec(PATHWAY_NOTES_SHEET, "Pathways", "Technical notes recorded while building the physical pathways."),
    SheetSpec(SUPPLY_CHAIN_LINKS_SHEET, "Pathways", "Monetary supply-chain links behind the pool-level cost pathway view."),
    SheetSpec(CHECKS_SHEET, "Audit", "Reconciliation formulas and every warning raised while preparing the workbook."),
    SheetSpec(TECHNICAL_NOTES_SHEET, "Audit", "Technical allocation notes recorded by the application."),
)

SHEET_ORDER: tuple[str, ...] = tuple(spec.name for spec in SHEET_REGISTRY)

MAX_SHEET_NAME_LENGTH = 31
_INVALID_SHEET_CHARACTERS = set("[]:*?/\\")


def validate_sheet_names() -> None:
    """Raise when a registered sheet name would be rejected by Excel."""
    seen: set[str] = set()
    for name in SHEET_ORDER:
        if len(name) > MAX_SHEET_NAME_LENGTH:
            raise ValueError(f"Sheet name '{name}' exceeds {MAX_SHEET_NAME_LENGTH} characters.")
        if _INVALID_SHEET_CHARACTERS.intersection(name):
            raise ValueError(f"Sheet name '{name}' contains a character Excel rejects.")
        if name in seen:
            raise ValueError(f"Sheet name '{name}' is registered twice.")
        seen.add(name)


# ---------------------------------------------------------------------------
# Column vocabulary
# ---------------------------------------------------------------------------

MODEL_CODE_SUFFIX = "(model code)"

_EXPLICIT_HEADERS: dict[str, str] = {
    "period": "Model year",
    "year": "Calendar year",
    "view": "Attribution view",
    "sector": "DESNZ sector",
    "resource_pool": "Resource pool",
    "pool_kind": "Pool kind",
    "reference_fuel": "Reported fuel type",
    "source_fuel": "Source fuel type",
    "quantity_twh": "Fuel use (TWh/yr)",
    "sector_twh": "Sector fuel use (TWh/yr)",
    "pool_twh": "Total pool fuel use (TWh/yr)",
    "allocation_share": "Share of pool use",
    "cost_share": "Share of pool cost",
    "share_basis_year": "Year whose use mix split this cost",
    "share_basis": "Why that year",
    "allocation_status": "Allocation status",
    "source_cost_m": "Source cost (£m/yr)",
    "allocated_cost_m": "Allocated cost (£m/yr)",
    "unassigned_source_cost_m": "Source cost not assigned to a fuel (£m/yr)",
    "denominator_unallocated_cost_m": "Cost left unallocated for want of use (£m/yr)",
    "power_core_cost_m": "Power-system core cost (£m/yr)",
    "electricity_base_m": "Electricity base cost (£m/yr)",
    "cost_m": "Cost (£m/yr)",
    "cost_bn": "Cost (£bn/yr)",
    "included": "Included in allocation",
    "commodity": f"Commodity {MODEL_CODE_SUFFIX}",
    "commodity_name": "Commodity name",
    "commodity_group": "Commodity group",
    "commodity_subgroup": "Commodity subgroup",
    "process": f"Process {MODEL_CODE_SUFFIX}",
    "process_name": "Process name",
    "attribute": f"Cost attribute {MODEL_CODE_SUFFIX}",
    "region": f"Region {MODEL_CODE_SUFFIX}",
    "vintage": "Construction vintage",
    "source_id": f"Supply node {MODEL_CODE_SUFFIX}",
    "target_id": f"Demand node {MODEL_CODE_SUFFIX}",
    "source_label": "Supply node",
    "target_label": "Demand node",
    "source_kind": "Supply node type",
    "target_kind": "Demand node type",
    "signed_value": "Signed value",
    "display_value": "Displayed value",
    "sign": "Cost or credit",
    "stage": "Pathway stage",
    "detail": "Detail",
    "check": "Check",
    "value": "Value",
    "twh": "Fuel use (TWh/yr)",
    "direction": "Flow direction",
    "reported_value": "Reported value",
    "unit": "Unit as reported",
    "unit_factor": "Conversion factor to TWh",
    "vd_row_count": "Result rows combined",
    "timeslice_mode": "How time slices were handled",
    "vdt_match": "In model connections",
    "provenance_note": "Why it was set aside",
    "reported_total": "Reported total",
    "conversion_status": "Conversion status",
    "pooling_status": "How the sheet treats it",
    "counted_twh": "Counted fuel use (TWh/yr)",
    "excluded_twh": "Fuel use not counted (TWh/yr)",
    "coverage_status": "Counted towards the split",
    "coverage_reason": "Why",
    "step": "Step",
    "what_happens": "What happens",
    "where_to_look": "Where to look",
}

_UNIT_SUFFIXES: tuple[tuple[str, str], ...] = (
    ("_twh", "TWh/yr"),
    ("_bn", "£bn/yr"),
    ("_m", "£m/yr"),
    ("_pct", "%"),
)

_MODEL_CODE_COLUMNS = frozenset({
    "commodity", "process", "attribute", "region", "commodity_code",
    "process_code", "source_id", "target_id", "node_id", "vintage_process",
})


def readable_header(column: object) -> str:
    """Return a plain-English column heading for one raw column name.

    Year columns keep their number.  Known columns use the curated vocabulary.
    Anything else is expanded from ``snake_case`` and given a unit suffix, so
    no raw column name can reach a reader unexplained.
    """
    if isinstance(column, (int, float)) and not isinstance(column, bool):
        return str(int(column))
    key = str(column).strip()
    if not key:
        return "Unnamed column"
    if key.isdigit():
        return key
    lowered = key.casefold()
    if lowered in _EXPLICIT_HEADERS:
        return _EXPLICIT_HEADERS[lowered]
    if lowered in _MODEL_CODE_COLUMNS:
        return f"{_titlecase(lowered)} {MODEL_CODE_SUFFIX}"
    unit = ""
    for suffix, label in _UNIT_SUFFIXES:
        if lowered.endswith(suffix) and len(lowered) > len(suffix):
            lowered = lowered[: -len(suffix)]
            unit = f" ({label})"
            break
    return f"{_titlecase(lowered)}{unit}"


def _titlecase(value: str) -> str:
    words = value.replace("_", " ").strip()
    if not words:
        return "Unnamed column"
    return words[:1].upper() + words[1:]


def readable_headers(columns) -> list[str]:
    """Return plain-English headings for an ordered set of columns."""
    return [readable_header(column) for column in columns]


# ---------------------------------------------------------------------------
# Glossary
# ---------------------------------------------------------------------------

GLOSSARY_ENTRIES: tuple[tuple[str, str], ...] = (
    ("£m/yr", "Millions of pounds per year, on the price-year basis named on the Read me sheet."),
    ("£bn/yr", "Billions of pounds per year. One £bn is one thousand £m."),
    ("TWh/yr", "Terawatt-hours of energy per year."),
    ("Model year", "A milestone year solved by the model, such as 2025 or 2030."),
    ("Calendar year", "A single year. Values between milestone years are interpolated in a straight line."),
    ("Milestone year", "A year the model actually solves. Only these years carry original evidence."),
    ("Resource pool", "The reviewed group of fuels that costs are attributed at, such as Natural gas."),
    ("Reported fuel type", "The fixed fuel grouping used for reporting, derived from the resource pool and commodity name."),
    ("Source fuel type", "The fuel grouping that a source cost is reported against."),
    ("DESNZ sector", "The reporting sector a fuel-using process belongs to."),
    ("Attribution view", "Which set of cost attributes a figure was built from. The views overlap and must never be added together."),
    ("Cost_Flo", "Flow cost. The cost of a commodity flowing through a process. Used by Flow-cost Attribution."),
    ("Cost_Inv", "Investment cost. The annualised capital cost of building a process."),
    ("Cost_Fom", "Fixed operating and maintenance cost of a process."),
    ("Cost_Act", "Activity cost. The cost of operating a process at its chosen activity level."),
    ("Cost_Com", "Commodity cost. Disclosed separately on its own sheet and never added into the attribution."),
    ("Pool kind", "Whether a pool holds a fuel (burned by sectors, such as Natural gas) or a delivered service (consumed by sectors, such as Space heating and hot water). Fuel and service pools measure different things and are never added together or compared. 'Not pooled' marks energy the pooling sheet deliberately leaves out; 'No pool' marks energy it does not answer."),
    ("Share of pool use", "A sector's use divided by total use across all sectors, within one pool and one year. This is physical evidence only. Shares never span pools, and fuel and service pools measure different things."),
    ("Share of pool cost", "A sector's allocated cost divided by the pool's source cost for one year. It equals the share of pool use unless investment costs were split by their build year's mix, in which case the two differ."),
    ("Year whose use mix split this cost", "Investment costs (Cost_Inv) are split by the sector mix of the year the capacity was built, not the year the cost is reported. All other costs, including fixed operating costs, use the reporting year. A build year between milestones uses the milestone period containing it."),
    ("Why that year", "'Build year' or 'Reporting year' as above. 'Nearest available year' means the build year had no recorded use of the pool, or came before the first model year, so the nearest later year with use was taken; these amounts are counted on the checks sheet. 'No eligible year with use' means the cost stayed unallocated."),
    ("Allocation status", "Whether a source cost was allocated, or left unallocated because its fuel or use could not be established."),
    ("Distributed electricity", "Power-system cost shared out to electricity-using sectors. The power sector itself receives zero, to prevent circular allocation."),
    ("Fuelex", "Grouped fuel-cost measures. 'alt' covers synthetic, bio and hydrogen fuels; 'f/fuels' covers natural gas and oil."),
    ("Construction vintage", "The year a plant was built, as reported by the model."),
    ("Model code", "A short identifier used inside the model. Columns ending 'model code' are kept only for audit."),
    ("Price year", "The year whose prices all money figures are expressed in. Named on the Read me sheet."),
    ("Unallocated", "A source cost that stays unattributed because its fuel is unknown or that fuel has no positive recorded use."),
    ("Result rows combined", "How many rows of the run's own results were added together to make one annual figure for one connection."),
    ("Time slice", "A part of a year the model solves separately. Where a connection has no explicit annual figure, its time slices are summed."),
    ("Model connections", "The list of process-to-commodity links the run actually contains. A reported flow with no matching connection is set aside rather than used."),
    ("Conversion factor to TWh", "What a reported value is multiplied by to reach TWh. One TWh is 3.6 PJ, so PJ is multiplied by 0.2778."),
    ("Counted towards the split", "Whether a fuel's use helps decide how a pool's cost is divided between sectors."),
)


# ---------------------------------------------------------------------------
# Cell references
# ---------------------------------------------------------------------------

def column_letter(index: int) -> str:
    """Return the Excel column letter for a one-based column index."""
    if index < 1:
        raise ValueError("Column index is one-based.")
    letters = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def quote_sheet(sheet: str) -> str:
    """Return a sheet name quoted for use inside a formula."""
    return "'{}'".format(str(sheet).replace("'", "''"))


def cell_ref(sheet: str | None, row: int, column: int, *, absolute: bool = True) -> str:
    """Return an A1 reference, optionally qualified by sheet name."""
    marker = "$" if absolute else ""
    reference = f"{marker}{column_letter(column)}{marker}{row}"
    return reference if sheet is None else f"{quote_sheet(sheet)}!{reference}"


def range_ref(
    sheet: str | None,
    first_row: int,
    first_column: int,
    last_row: int,
    last_column: int,
    *,
    absolute: bool = True,
) -> str:
    """Return an A1 range reference, optionally qualified by sheet name."""
    start = cell_ref(None, first_row, first_column, absolute=absolute)
    end = cell_ref(None, last_row, last_column, absolute=absolute)
    reference = f"{start}:{end}"
    return reference if sheet is None else f"{quote_sheet(sheet)}!{reference}"


def column_range_ref(sheet: str, column: int, first_row: int, last_row: int) -> str:
    """Return an absolute single-column range used as a SUMIFS argument."""
    return range_ref(sheet, first_row, column, last_row, column)


def text_literal(value: object) -> str:
    """Return a value quoted for direct use inside a formula."""
    return '"{}"'.format(str(value).replace('"', '""'))
