"""Derive explainable energy-system boundaries from labels, VDT, and VD.

Authoritative labels describe commodities and processes; the selected run's
VDT identifies final-use connections as energy ``IN`` links to processes with
useful output, and VD ``VAR_FIn``/``VAR_FOut`` rows provide observed annual
quantities. This module is deliberately pure: it does not import Streamlit or
the legacy allocation/classification engine.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Iterable

import pandas as pd

from cost_allocation.constants import PJ_PER_TWH
from cost_allocation.labels import COMMODITY_LABEL_COLUMNS, PROCESS_LABEL_COLUMNS
from cost_allocation.vdt import VDT_COLUMNS


FINAL_CONSUMPTION_MARKER = "final consumption"
ENERGY_COMMODITY_SET = "NRG"
ENVIRONMENT_COMMODITY_SET = "ENV"
DEMAND_COMMODITY_SET = "DEM"
FLOW_ATTRIBUTES = {"VAR_FIn": "IN", "VAR_FOut": "OUT"}

_SCOPE_LINK_COLUMNS = (
    "region", "process", "commodity", "direction", "link_role", "depth",
    "connection_role", "connection_role_reason",
)
_PROCESS_SCOPE_COLUMNS = (
    "region", "process", "role", "stage", "depth", "inclusion_reason",
    "boundary_commodities", "is_dmd", "has_dem_output",
) + PROCESS_LABEL_COLUMNS[1:]
_EXCLUSION_COLUMNS = (
    "region", "node_type", "process", "commodity", "direction", "reason",
    "related_process", "depth",
)
_DIAGNOSTIC_COLUMNS = (
    "diagnostic_type", "region", "process", "commodity", "detail",
)
_ANNUAL_FLOW_COLUMNS = (
    "region", "process", "commodity", "direction", "period", "value",
    "active", "timeslice_mode", "vd_row_count",
)
_FLOW_RESULT_COLUMNS = (
    _ANNUAL_FLOW_COLUMNS
    + PROCESS_LABEL_COLUMNS[1:]
    + COMMODITY_LABEL_COLUMNS[1:]
)


@dataclass(frozen=True)
class EnergyScopeResult:
    """Structural energy boundary and its auditable derivation."""

    final_consumption_commodities: frozenset[str]
    end_use_processes: frozenset[str]
    supply_processes: frozenset[str]
    process_scope: pd.DataFrame
    links: pd.DataFrame
    exclusions: pd.DataFrame
    diagnostics: pd.DataFrame


@dataclass(frozen=True)
class AnnualFlowResult:
    """VD annual flows matched to the selected run's VDT topology."""

    flows: pd.DataFrame
    unmatched_edges: pd.DataFrame
    diagnostics: pd.DataFrame


def attach_connection_roles(
    annual_flows: AnnualFlowResult,
    scope: EnergyScopeResult,
) -> AnnualFlowResult:
    """Attach selected-boundary connection roles and depth to flow evidence.

    A flow outside the selected energy boundary is deliberately retained and
    labelled as such. It is not silently treated as a final-use or supply link.
    """
    if annual_flows.flows.empty:
        flows = annual_flows.flows.copy()
        flows["connection_role"] = pd.Series(dtype=str)
        flows["connection_role_reason"] = pd.Series(dtype=str)
        flows["link_role"] = pd.Series(dtype=str)
        flows["depth"] = pd.Series(dtype="Int64")
        return AnnualFlowResult(
            flows=flows,
            unmatched_edges=annual_flows.unmatched_edges,
            diagnostics=annual_flows.diagnostics,
        )
    role_columns = [
        "region", "process", "commodity", "direction", "link_role", "depth",
        "connection_role", "connection_role_reason",
    ]
    if scope.links.empty:
        roles = pd.DataFrame(columns=role_columns)
    else:
        roles = scope.links.loc[:, role_columns].drop_duplicates(
            ["region", "process", "commodity", "direction"], keep="first"
        )
    flows = annual_flows.flows.drop(
        columns=["connection_role", "connection_role_reason", "link_role", "depth"],
        errors="ignore",
    ).merge(
        roles,
        on=["region", "process", "commodity", "direction"],
        how="left",
    )
    flows["connection_role"] = flows["connection_role"].fillna(
        "Outside selected energy boundary"
    )
    flows["connection_role_reason"] = flows["connection_role_reason"].fillna(
        "This observed VDT link is not part of the useful-output final-use or upstream boundary."
    )
    flows["link_role"] = flows["link_role"].fillna("outside_selected_boundary")
    flows["depth"] = pd.to_numeric(flows["depth"], errors="coerce").astype("Int64")
    return AnnualFlowResult(
        flows=flows,
        unmatched_edges=annual_flows.unmatched_edges,
        diagnostics=annual_flows.diagnostics,
    )


POWER_ROLES = ("Generation", "Storage", "Network", "Interconnector")
_POWER_SCOPE_COLUMNS = (
    "process", "role", "structural_evidence", "process_name",
    "process_sector", "process_group", "process_category",
    "process_subsector", "process_set",
)
_POWER_BALANCE_COLUMNS = (
    "period", "Generation (PJ)", "Imports (PJ)", "Storage discharge (PJ)",
    "Storage charge (PJ)", "Exports (PJ)", "Explicit losses (PJ)",
    "Net supplied (PJ)", "Net supplied (TWh/yr)",
    "Final consumption (TWh/yr)", "Reconciliation gap (TWh/yr)",
    "Reconciliation status", "Calculation status",
)
_POWER_EXCLUSION_COLUMNS = (
    "process", "reason", "detail", "process_name", "process_sector",
    "process_group", "process_subsector", "process_set",
)


@dataclass(frozen=True)
class PowerScopeResult:
    """Prepared, run-specific Power roles and net-supplied balance."""

    primary_electricity_commodity: str | None
    electricity_commodities: frozenset[str]
    role_processes: dict[str, frozenset[str]]
    process_scope: pd.DataFrame
    balance: pd.DataFrame
    exclusions: pd.DataFrame
    diagnostics: pd.DataFrame


def _empty_frame(columns: Iterable[str]) -> pd.DataFrame:
    return pd.DataFrame(columns=list(columns))


def empty_energy_scope(diagnostic_type: str = "missing_vdt") -> EnergyScopeResult:
    """Return a typed empty scope suitable for unavailable run topology."""
    diagnostics = pd.DataFrame([
        {
            "diagnostic_type": diagnostic_type,
            "region": "",
            "process": "",
            "commodity": "",
            "detail": "Energy scope could not be derived.",
        }
    ])
    return EnergyScopeResult(
        final_consumption_commodities=frozenset(),
        end_use_processes=frozenset(),
        supply_processes=frozenset(),
        process_scope=_empty_frame(_PROCESS_SCOPE_COLUMNS),
        links=_empty_frame(_SCOPE_LINK_COLUMNS),
        exclusions=_empty_frame(_EXCLUSION_COLUMNS),
        diagnostics=diagnostics.loc[:, list(_DIAGNOSTIC_COLUMNS)],
    )


def final_consumption_commodity_codes(commodity_labels: pd.DataFrame) -> frozenset[str]:
    """Return commodities carrying a final-consumption reporting label.

    This is deliberately a reporting helper, not a topology rule. A TIMES
    commodity may be final energy in one process and throughput or a
    transformation input in another. ``build_energy_scope`` therefore derives
    final-use *connections* from process useful-output metadata and VDT links.
    """
    required = {"commodity", "commodity_function"}
    if not required.issubset(commodity_labels.columns):
        raise ValueError("Commodity labels must contain commodity and commodity_function.")
    function = commodity_labels["commodity_function"].fillna("").astype(str)
    codes = commodity_labels.loc[
        function.str.contains(FINAL_CONSUMPTION_MARKER, case=False, regex=False),
        "commodity",
    ]
    return frozenset(_normalise_codes(codes))


def _normalise_codes(values: pd.Series) -> list[str]:
    normalised = values.fillna("").astype(str).str.strip().str.upper()
    return normalised[normalised.ne("")].tolist()


def _normalise_labels(labels: pd.DataFrame, key: str, columns: tuple[str, ...]) -> pd.DataFrame:
    if key not in labels.columns:
        raise ValueError(f"Labels must contain {key}.")
    result = labels.copy()
    for column in columns:
        if column not in result.columns:
            result[column] = ""
        result[column] = result[column].fillna("").astype(str).str.strip()
    result[key] = result[key].str.upper()
    result = result[result[key].ne("")].drop_duplicates(key, keep="first")
    return result.loc[:, list(columns)]


def _normalise_topology(vdt: pd.DataFrame) -> pd.DataFrame:
    if vdt.empty and not set(VDT_COLUMNS).issubset(vdt.columns):
        return _empty_frame(VDT_COLUMNS)
    if not set(VDT_COLUMNS).issubset(vdt.columns):
        raise ValueError("VDT topology must contain region, process, commodity, and direction.")
    topology = vdt.loc[:, list(VDT_COLUMNS)].copy()
    for column in VDT_COLUMNS:
        topology[column] = topology[column].fillna("").astype(str).str.strip().str.upper()
    topology = topology[
        topology["process"].ne("")
        & topology["commodity"].ne("")
        & topology["direction"].isin({"IN", "OUT"})
    ]
    return topology.drop_duplicates().reset_index(drop=True)


def _commodity_exclusion_reason(metadata: pd.Series | None) -> str | None:
    if metadata is None:
        return "missing_commodity_label"
    commodity_set = str(metadata.get("commodity_set", "")).strip().upper()
    commodity_type = str(metadata.get("commodity_type", "")).strip().casefold()
    if commodity_set == ENVIRONMENT_COMMODITY_SET or "emission" in commodity_type:
        return "environment_commodity"
    if commodity_set == DEMAND_COMMODITY_SET or commodity_type == "demand":
        return "final_service_commodity"
    # Read from the commodity type label only; a name containing "proxy" or
    # "dummy" does not make a commodity a proxy.
    if "proxy" in commodity_type:
        return "proxy_commodity"
    is_energy = commodity_set == ENERGY_COMMODITY_SET or commodity_type in {"fuel", "energy"}
    if not is_energy:
        return "non_energy_commodity"
    return None


def _process_exclusion_reason(
    process: str,
    metadata: pd.Series | None,
    end_use_nodes: set[tuple[str, str]],
    node: tuple[str, str],
) -> str | None:
    if node in end_use_nodes:
        return "end_use_process"
    if metadata is None:
        return "missing_process_label"
    process_set = str(metadata.get("process_set", "")).strip().upper()
    sector = str(metadata.get("process_sector", "")).strip().casefold()
    if process_set == "DMD":
        return "final_demand_process"
    if sector == "uk times allocation":
        return "allocation_process"
    # No label field yet marks dummy or accounting processes, so none are
    # inferred from the code or the wording of the name.
    return None


def _has_useful_output(metadata: pd.Series | None) -> bool:
    """Return whether a process is labelled as delivering an end-use service."""
    return metadata is not None and bool(
        str(metadata.get("process_useful_output", "")).strip()
    )


def _connection_role(
    row: pd.Series,
    *,
    process_map: dict[str, pd.Series],
    commodity_map: dict[str, pd.Series],
    end_use_nodes: set[tuple[str, str]],
    process_output_commodities: dict[tuple[str, str], set[str]],
) -> tuple[str, str]:
    """Classify one VDT connection without assigning one role per commodity.

    Roles are presentation and audit evidence only. Structural inclusion still
    follows the explicit end-use and upstream traversal below.
    """
    region = str(row.get("region", ""))
    process = str(row.get("process", ""))
    commodity = str(row.get("commodity", ""))
    direction = str(row.get("direction", ""))
    metadata = process_map.get(process)
    commodity_metadata = commodity_map.get(commodity)
    node = (region, process)
    commodity_exclusion = _commodity_exclusion_reason(commodity_metadata)
    process_exclusion = _process_exclusion_reason(process, metadata, end_use_nodes, node)

    if commodity_exclusion is not None:
        return "Non-energy", commodity_exclusion
    if process_exclusion in {"allocation_process", "final_demand_process"}:
        return "Allocation / accounting", process_exclusion
    if direction == "OUT":
        return "Production output", "Energy output from the process."
    if direction != "IN":
        return "Unclassified", "VDT direction is not a recognised energy input or output."
    if node in end_use_nodes:
        return "Final energy use", "Energy input to a process with useful output."

    subsector = "" if metadata is None else str(metadata.get("process_subsector", "")).casefold()
    group = "" if metadata is None else str(metadata.get("process_group", "")).casefold()
    if "network" in subsector or "network" in group or "terminal" in subsector:
        return "Throughput / network", "Process taxonomy identifies a network or terminal role."
    outputs = process_output_commodities.get(node, set())
    if commodity in outputs:
        return "Throughput / network", "The same energy commodity enters and exits this process."
    if outputs:
        return "Transformation input", "The process has a different eligible energy output."
    return "Auxiliary energy input", "Energy input has no eligible output-carrier evidence."


def build_energy_scope(
    vdt: pd.DataFrame,
    process_labels: pd.DataFrame,
    commodity_labels: pd.DataFrame,
) -> EnergyScopeResult:
    """Derive end-use and recursively upstream supply processes.

    End-use processes have an energy structural VDT ``IN`` link and a
    non-blank authoritative ``process_useful_output``. Supply starts with
    structural VDT producers (``OUT``) of the energy commodities on those
    final-use links, then walks recursively through eligible energy inputs and
    their producers within the same region.
    """
    topology = _normalise_topology(vdt)
    if topology.empty:
        return empty_energy_scope()

    process_meta = _normalise_labels(process_labels, "process", PROCESS_LABEL_COLUMNS)
    commodity_meta = _normalise_labels(commodity_labels, "commodity", COMMODITY_LABEL_COLUMNS)
    process_map = {row["process"]: row for _, row in process_meta.iterrows()}
    commodity_map = {row["commodity"]: row for _, row in commodity_meta.iterrows()}

    energy_commodities = {
        commodity for commodity, metadata in commodity_map.items()
        if _commodity_exclusion_reason(metadata) is None
    }
    final_energy: set[str] = set()
    diagnostics: list[dict] = []
    exclusions: list[dict] = []

    # A commodity can be final energy for one process and throughput or a
    # transformation input for another. Final-use membership is consequently
    # an edge property: an energy IN link to a process with useful output.
    candidate_final_consumers = topology[
        topology["direction"].eq("IN")
        & topology["commodity"].isin(energy_commodities)
    ].copy()
    useful_output_mask = candidate_final_consumers.apply(
        lambda row: _has_useful_output(process_map.get(row["process"])), axis=1
    )
    final_consumers = candidate_final_consumers[useful_output_mask].copy()
    final_energy.update(final_consumers["commodity"].unique())

    if final_consumers.empty:
        diagnostics.append({
            "diagnostic_type": "no_useful_output_energy_consumer",
            "region": "",
            "process": "",
            "commodity": "",
            "detail": "No VDT energy input belongs to a process with useful output.",
        })
    end_use_nodes = set(zip(final_consumers["region"], final_consumers["process"]))
    end_use_processes = {process for _, process in end_use_nodes}

    # Index the topology once. groupby(sort=False) keeps each group's rows in
    # their original order, so traversal visits rows exactly as the previous
    # per-node boolean-mask scans did.
    out_links = topology[topology["direction"].eq("OUT")]
    in_links = topology[topology["direction"].eq("IN")]
    process_output_commodities = {
        (region, process): set(group["commodity"])
        for (region, process), group in out_links.groupby(["region", "process"], sort=False)
    }
    in_links_by_node = dict(tuple(in_links.groupby(["region", "process"], sort=False)))
    producers_by_region_commodity = dict(
        tuple(out_links.groupby(["region", "commodity"], sort=False))
    )
    final_consumers_by_node = dict(
        tuple(final_consumers.groupby(["region", "process"], sort=False))
    )
    consumed_commodities = set(in_links["commodity"])
    no_rows = topology.iloc[0:0]

    # DMD and DEM output remain validation evidence, but do not create
    # end-use membership without the authoritative useful-output label.
    dem_commodities = {
        commodity for commodity, metadata in commodity_map.items()
        if str(metadata.get("commodity_set", "")).strip().upper() == DEMAND_COMMODITY_SET
    }
    for region, process in sorted(end_use_nodes):
        metadata = process_map.get(process)
        output_commodities = process_output_commodities.get((region, process), set())
        is_dmd = bool(metadata is not None and str(metadata.get("process_set", "")).strip().upper() == "DMD")
        has_dem_output = bool(output_commodities & dem_commodities)
        if not is_dmd and not has_dem_output:
            diagnostics.append({
                "diagnostic_type": "useful_output_without_dmd_or_dem_validation",
                "region": region,
                "process": process,
                "commodity": "",
                "detail": "End-use membership is based on useful output; DMD/DEM validation is absent.",
            })

    labelled_final = final_consumption_commodity_codes(commodity_meta)
    for commodity in sorted(labelled_final.difference(final_energy)):
        diagnostics.append({
            "diagnostic_type": "commodity_final_consumption_label_not_used_for_boundary",
            "region": "",
            "process": "",
            "commodity": commodity,
            "detail": "Commodity function is reporting metadata; no useful-output consumer was found.",
        })

    for commodity in sorted(final_energy):
        if commodity in consumed_commodities:
            continue
        diagnostics.append({
            "diagnostic_type": "no_structural_consumer",
            "region": "",
            "process": "",
            "commodity": commodity,
            "detail": "A final-use energy commodity has no structural VDT IN link.",
        })

    process_records: dict[tuple[str, str], dict] = {}
    boundary_commodities: dict[tuple[str, str], set[str]] = {}
    link_records: dict[tuple[str, str, str, str], dict] = {}

    def add_link(row: pd.Series, role: str, depth: int) -> None:
        key = (row["region"], row["process"], row["commodity"], row["direction"])
        existing = link_records.get(key)
        if existing is None or depth < existing["depth"]:
            link_records[key] = {
                "region": row["region"],
                "process": row["process"],
                "commodity": row["commodity"],
                "direction": row["direction"],
                "link_role": role,
                "depth": depth,
                "connection_role": "",
                "connection_role_reason": "",
            }

    for node in sorted(end_use_nodes):
        region, process = node
        metadata = process_map.get(process)
        output_commodities = process_output_commodities.get(node, set())
        node_consumers = final_consumers_by_node[node]
        consumed = set(node_consumers["commodity"])
        boundary_commodities[node] = consumed
        process_records[node] = {
            "region": region,
            "process": process,
            "role": "end_use",
            "stage": "end_use_technology",
            "depth": 0,
            "inclusion_reason": "consumes_final_consumption",
            "is_dmd": bool(
                metadata is not None
                and str(metadata.get("process_set", "")).strip().upper() == "DMD"
            ),
            "has_dem_output": bool(output_commodities & dem_commodities),
        }
        if metadata is None:
            diagnostics.append({
                "diagnostic_type": "missing_process_label",
                "region": region,
                "process": process,
                "commodity": "",
                "detail": "Structurally derived end-use process has no current label.",
            })
        for _, row in node_consumers.iterrows():
            add_link(row, "end_use_input", 0)

    queue: deque[tuple[str, str]] = deque()
    supply_depth: dict[tuple[str, str], int] = {}
    supply_reason: dict[tuple[str, str], str] = {}

    def exclude_candidate(
        row: pd.Series,
        reason: str,
        related_process: str,
        depth: int,
        node_type: str = "process",
    ) -> None:
        exclusions.append({
            "region": row.get("region", ""),
            "node_type": node_type,
            "process": row.get("process", ""),
            "commodity": row.get("commodity", ""),
            "direction": row.get("direction", ""),
            "reason": reason,
            "related_process": related_process,
            "depth": depth,
        })

    def include_producer(row: pd.Series, depth: int, reason: str, related_process: str) -> None:
        node = (row["region"], row["process"])
        exclusion_reason = _process_exclusion_reason(
            row["process"], process_map.get(row["process"]), end_use_nodes, node
        )
        if exclusion_reason is not None:
            exclude_candidate(row, exclusion_reason, related_process, depth)
            return
        add_link(row, "supply_output", depth)
        boundary_commodities.setdefault(node, set()).add(row["commodity"])
        if node not in supply_depth or depth < supply_depth[node]:
            supply_depth[node] = depth
            supply_reason[node] = reason
            queue.append(node)

    boundary_pairs = final_consumers[["region", "commodity"]].drop_duplicates()
    for _, boundary in boundary_pairs.iterrows():
        producers = producers_by_region_commodity.get(
            (boundary["region"], boundary["commodity"]), no_rows
        )
        if producers.empty:
            diagnostics.append({
                "diagnostic_type": "no_boundary_producer",
                "region": boundary["region"],
                "process": "",
                "commodity": boundary["commodity"],
                "detail": "Consumed final energy has no same-region VDT OUT producer.",
            })
            continue
        for _, row in producers.iterrows():
            include_producer(row, 1, "produces_final_consumption", "")

    while queue:
        region, process = queue.popleft()
        depth = supply_depth[(region, process)]
        input_links = in_links_by_node.get((region, process), no_rows)
        for _, input_row in input_links.iterrows():
            commodity = input_row["commodity"]
            exclusion_reason = _commodity_exclusion_reason(commodity_map.get(commodity))
            if exclusion_reason is not None:
                exclude_candidate(
                    input_row, exclusion_reason, process, depth, node_type="commodity"
                )
                continue
            add_link(input_row, "supply_input", depth)
            producers = producers_by_region_commodity.get((region, commodity), no_rows)
            if producers.empty:
                diagnostics.append({
                    "diagnostic_type": "no_upstream_producer",
                    "region": region,
                    "process": process,
                    "commodity": commodity,
                    "detail": "Eligible supply input has no same-region VDT producer.",
                })
                continue
            for _, producer_row in producers.iterrows():
                include_producer(
                    producer_row,
                    depth + 1,
                    "produces_upstream_energy",
                    process,
                )

    for node, depth in supply_depth.items():
        region, process = node
        metadata = process_map.get(process)
        process_records[node] = {
            "region": region,
            "process": process,
            "role": "supply",
            "stage": "final_delivery" if depth == 1 else "upstream_supply",
            "depth": depth,
            "inclusion_reason": supply_reason[node],
            "is_dmd": bool(
                metadata is not None
                and str(metadata.get("process_set", "")).strip().upper() == "DMD"
            ),
            "has_dem_output": False,
        }

    process_rows = []
    for node, record in process_records.items():
        record = record.copy()
        record["boundary_commodities"] = ", ".join(sorted(boundary_commodities.get(node, set())))
        metadata = process_map.get(record["process"])
        for column in PROCESS_LABEL_COLUMNS[1:]:
            record[column] = "" if metadata is None else metadata.get(column, "")
        process_rows.append(record)
    process_scope = pd.DataFrame(process_rows, columns=_PROCESS_SCOPE_COLUMNS)
    if not process_scope.empty:
        process_scope = process_scope.sort_values(
            ["role", "depth", "region", "process"]
        ).reset_index(drop=True)

    boundary_links = pd.DataFrame(link_records.values(), columns=_SCOPE_LINK_COLUMNS)
    # Keep an auditable role for every VDT connection. Boundary-specific
    # ``link_role`` and depth are retained where traversal selected the edge;
    # other edges remain visible rather than being confused with absence.
    links = topology.merge(
        boundary_links.loc[:, [
            "region", "process", "commodity", "direction", "link_role", "depth",
        ]],
        on=["region", "process", "commodity", "direction"],
        how="left",
    )
    links["link_role"] = links["link_role"].fillna("outside_selected_boundary")
    links["depth"] = links["depth"].astype("Int64")
    links["connection_role"] = ""
    links["connection_role_reason"] = ""
    if not links.empty:
        roles = links.apply(
            lambda row: _connection_role(
                row,
                process_map=process_map,
                commodity_map=commodity_map,
                end_use_nodes=end_use_nodes,
                process_output_commodities=process_output_commodities,
            ),
            axis=1,
        )
        links[["connection_role", "connection_role_reason"]] = pd.DataFrame(
            roles.tolist(), index=links.index
        )
        links = links.sort_values(
            ["depth", "region", "commodity", "direction", "process"]
        ).reset_index(drop=True)
        links = _attach_metadata(links, process_meta, commodity_meta)

    exclusions_frame = pd.DataFrame(exclusions, columns=_EXCLUSION_COLUMNS).drop_duplicates()
    if not exclusions_frame.empty:
        exclusions_frame = exclusions_frame.sort_values(
            ["reason", "region", "commodity", "process"]
        ).reset_index(drop=True)
    diagnostics_frame = pd.DataFrame(diagnostics, columns=_DIAGNOSTIC_COLUMNS).drop_duplicates()
    if not diagnostics_frame.empty:
        diagnostics_frame = diagnostics_frame.sort_values(
            ["diagnostic_type", "region", "commodity", "process"]
        ).reset_index(drop=True)

    return EnergyScopeResult(
        final_consumption_commodities=frozenset(final_energy),
        end_use_processes=frozenset(end_use_processes),
        supply_processes=frozenset(process for _, process in supply_depth),
        process_scope=process_scope,
        links=links,
        exclusions=exclusions_frame,
        diagnostics=diagnostics_frame,
    )


def _attach_metadata(
    frame: pd.DataFrame,
    process_labels: pd.DataFrame,
    commodity_labels: pd.DataFrame,
) -> pd.DataFrame:
    result = frame.merge(process_labels, on="process", how="left")
    result = result.merge(commodity_labels, on="commodity", how="left")
    return result


def _normalise_observed_flows(
    vd_df: pd.DataFrame,
    scenario: str | None,
    periods: Iterable[int] | None,
) -> tuple[pd.DataFrame, list[dict], int]:
    required = {"Attribute", "Commodity", "Process", "Period", "Region", "Value"}
    missing = required.difference(vd_df.columns)
    if missing:
        raise ValueError(f"VD data is missing required columns: {', '.join(sorted(missing))}")
    if scenario is not None and "Scenario" not in vd_df.columns:
        raise ValueError("VD data must contain Scenario when a scenario filter is requested.")

    flow = vd_df[vd_df["Attribute"].isin(FLOW_ATTRIBUTES)].copy()
    if scenario is not None:
        flow = flow[flow["Scenario"].eq(scenario)]
    if periods is not None:
        selected_periods = {int(period) for period in periods}
        numeric_period = pd.to_numeric(flow["Period"], errors="coerce")
        flow = flow[numeric_period.isin(selected_periods)].copy()

    before_dedup = len(flow)
    flow = flow.drop_duplicates().copy()
    duplicates_removed = before_dedup - len(flow)
    diagnostics: list[dict] = []
    if flow.empty:
        return flow, diagnostics, duplicates_removed

    flow["period"] = pd.to_numeric(flow["Period"], errors="coerce")
    flow["value"] = pd.to_numeric(flow["Value"], errors="coerce")
    flow["region"] = flow["Region"].fillna("").astype(str).str.strip().str.upper()
    flow["process"] = flow["Process"].fillna("").astype(str).str.strip().str.upper()
    flow["commodity"] = flow["Commodity"].fillna("").astype(str).str.strip().str.upper()
    flow["direction"] = flow["Attribute"].map(FLOW_ATTRIBUTES)
    if "TimeSlice" in flow.columns:
        flow["_timeslice"] = flow["TimeSlice"].fillna("").astype(str).str.strip().str.upper()
    else:
        flow["_timeslice"] = ""

    invalid = flow[
        flow["period"].isna()
        | flow["value"].isna()
        | flow["process"].eq("")
        | flow["commodity"].eq("")
    ]
    for _, row in invalid.iterrows():
        diagnostics.append({
            "diagnostic_type": "invalid_observed_flow",
            "region": row["region"],
            "process": row["process"],
            "commodity": row["commodity"],
            "detail": "Missing process/commodity or non-numeric period/value.",
        })
    flow = flow.drop(index=invalid.index)
    if not flow.empty:
        flow["period"] = flow["period"].astype(int)
    return flow, diagnostics, duplicates_removed


def prepare_annual_flows(
    vd_df: pd.DataFrame,
    vdt: pd.DataFrame,
    process_labels: pd.DataFrame,
    commodity_labels: pd.DataFrame,
    *,
    scenario: str | None = None,
    periods: Iterable[int] | None = None,
    active_tolerance: float = 1e-9,
) -> AnnualFlowResult:
    """Aggregate observed flows annually and validate every edge against VDT.

    If an explicit ``ANNUAL`` row coexists with detailed timeslices for one
    edge, the annual row is authoritative and detailed rows are ignored.
    Otherwise detailed rows are summed.  Exact duplicate VD rows are removed
    before either operation.
    """
    if active_tolerance < 0:
        raise ValueError("active_tolerance must be non-negative.")
    topology = _normalise_topology(vdt)
    process_meta = _normalise_labels(process_labels, "process", PROCESS_LABEL_COLUMNS)
    commodity_meta = _normalise_labels(commodity_labels, "commodity", COMMODITY_LABEL_COLUMNS)
    flow, diagnostic_rows, duplicates_removed = _normalise_observed_flows(
        vd_df, scenario, periods
    )

    if flow.empty:
        diagnostics = pd.DataFrame(diagnostic_rows, columns=_DIAGNOSTIC_COLUMNS)
        return AnnualFlowResult(
            flows=_empty_frame(_FLOW_RESULT_COLUMNS),
            unmatched_edges=_empty_frame(_FLOW_RESULT_COLUMNS + ("reason",)),
            diagnostics=diagnostics,
        )

    group_columns = ["region", "process", "commodity", "direction", "period"]
    flow["_is_annual"] = flow["_timeslice"].eq("ANNUAL")
    flow["_has_annual"] = flow.groupby(group_columns, dropna=False)["_is_annual"].transform("any")
    ignored_detail = int((flow["_has_annual"] & ~flow["_is_annual"]).sum())
    selected = flow[~flow["_has_annual"] | flow["_is_annual"]].copy()
    selected["timeslice_mode"] = selected["_has_annual"].map(
        {True: "explicit_annual", False: "summed_timeslices"}
    )
    annual = selected.groupby(group_columns, as_index=False, dropna=False).agg(
        value=("value", "sum"),
        timeslice_mode=("timeslice_mode", "first"),
        vd_row_count=("value", "size"),
    )
    annual["active"] = annual["value"].abs().gt(active_tolerance)

    structural_edges = topology.loc[:, list(VDT_COLUMNS)].drop_duplicates()
    matched = annual.merge(
        structural_edges.assign(_vdt_matched=True),
        on=list(VDT_COLUMNS),
        how="left",
    )
    unmatched = matched[matched["_vdt_matched"].isna()].copy()
    unmatched["reason"] = "not_in_vdt"
    matched = matched[matched["_vdt_matched"].eq(True)].drop(columns="_vdt_matched")
    unmatched = unmatched.drop(columns="_vdt_matched")

    matched = _attach_metadata(matched, process_meta, commodity_meta)
    unmatched = _attach_metadata(unmatched, process_meta, commodity_meta)
    matched = matched.loc[:, list(_FLOW_RESULT_COLUMNS)]
    unmatched = unmatched.loc[:, list(_FLOW_RESULT_COLUMNS + ("reason",))]
    matched = matched.sort_values(group_columns).reset_index(drop=True)
    unmatched = unmatched.sort_values(group_columns).reset_index(drop=True)

    known_processes = set(process_meta["process"])
    known_commodities = set(commodity_meta["commodity"])
    for _, row in annual[
        ~annual["process"].isin(known_processes)
    ][["region", "process", "commodity"]].drop_duplicates().iterrows():
        diagnostic_rows.append({
            "diagnostic_type": "observed_missing_process_label",
            "region": row["region"],
            "process": row["process"],
            "commodity": row["commodity"],
            "detail": "Observed annual flow process has no current label.",
        })
    for _, row in annual[
        ~annual["commodity"].isin(known_commodities)
    ][["region", "process", "commodity"]].drop_duplicates().iterrows():
        diagnostic_rows.append({
            "diagnostic_type": "observed_missing_commodity_label",
            "region": row["region"],
            "process": row["process"],
            "commodity": row["commodity"],
            "detail": "Observed annual flow commodity has no current label.",
        })

    summary = [
        ("exact_duplicate_rows_removed", duplicates_removed),
        ("detailed_timeslice_rows_ignored", ignored_detail),
        ("matched_annual_edges", len(matched)),
        ("unmatched_annual_edges", len(unmatched)),
    ]
    diagnostic_rows.extend({
        "diagnostic_type": metric,
        "region": "",
        "process": "",
        "commodity": "",
        "detail": str(value),
    } for metric, value in summary)
    diagnostics = pd.DataFrame(diagnostic_rows, columns=_DIAGNOSTIC_COLUMNS)

    return AnnualFlowResult(
        flows=matched,
        unmatched_edges=unmatched,
        diagnostics=diagnostics,
    )


def empty_power_scope(diagnostic_type: str = "missing_power_topology") -> PowerScopeResult:
    """Return a typed unavailable Power scope without a taxonomy fallback."""
    diagnostics = pd.DataFrame([{
        "diagnostic_type": diagnostic_type,
        "region": "",
        "process": "",
        "commodity": "",
        "detail": "Power scope could not be derived from the selected run.",
    }])
    return PowerScopeResult(
        primary_electricity_commodity=None,
        electricity_commodities=frozenset(),
        role_processes={role: frozenset() for role in POWER_ROLES},
        process_scope=_empty_frame(_POWER_SCOPE_COLUMNS),
        balance=_empty_frame(_POWER_BALANCE_COLUMNS),
        exclusions=_empty_frame(_POWER_EXCLUSION_COLUMNS),
        diagnostics=diagnostics.loc[:, list(_DIAGNOSTIC_COLUMNS)],
    )


def _is_electricity(metadata: pd.Series) -> bool:
    """Return whether a current commodity label explicitly denotes electricity."""
    return "electricity" in str(metadata.get("commodity_name", "")).casefold()


def _is_surplus_electricity(metadata: pd.Series) -> bool:
    text = " ".join(
        str(metadata.get(column, ""))
        for column in ("commodity_name", "commodity_function", "commodity_type")
    ).casefold()
    return "surplus" in text or "curtail" in text


def _power_metadata_maps(
    process_labels: pd.DataFrame,
    commodity_labels: pd.DataFrame,
) -> tuple[dict[str, pd.Series], dict[str, pd.Series]]:
    process_meta = _normalise_labels(process_labels, "process", PROCESS_LABEL_COLUMNS)
    commodity_meta = _normalise_labels(commodity_labels, "commodity", COMMODITY_LABEL_COLUMNS)
    return (
        {row["process"]: row for _, row in process_meta.iterrows()},
        {row["commodity"]: row for _, row in commodity_meta.iterrows()},
    )


def _primary_electricity_commodity(
    topology: pd.DataFrame,
    process_map: dict[str, pd.Series],
    commodity_map: dict[str, pd.Series],
) -> tuple[str | None, list[dict]]:
    """Identify the unique non-surplus electricity bus produced by generators."""
    candidates: set[str] = set()
    for _, row in topology[topology["direction"].eq("OUT")].iterrows():
        process = process_map.get(row["process"])
        commodity = commodity_map.get(row["commodity"])
        if process is None or commodity is None:
            continue
        if (
            str(process.get("process_sector", "")).strip().casefold() == "power"
            and str(process.get("process_subsector", "")).strip().casefold()
            == "electricity generation"
            and _is_electricity(commodity)
            and not _is_surplus_electricity(commodity)
        ):
            candidates.add(row["commodity"])
    if len(candidates) == 1:
        return next(iter(candidates)), []
    detail = (
        "No non-surplus electricity commodity is structurally produced by current "
        "Electricity Generation processes."
        if not candidates else
        f"Multiple candidate primary electricity commodities: {', '.join(sorted(candidates))}."
    )
    return None, [{
        "diagnostic_type": "unresolved_primary_electricity_commodity",
        "region": "",
        "process": "",
        "commodity": ", ".join(sorted(candidates)),
        "detail": detail,
    }]


def _power_process_roles(
    topology: pd.DataFrame,
    process_map: dict[str, pd.Series],
    primary: str,
) -> tuple[dict[str, frozenset[str]], pd.DataFrame, pd.DataFrame, list[dict]]:
    """Assign each eligible Power process one validated role or exclude it."""
    role_sets = {role: set() for role in POWER_ROLES}
    scope_rows: list[dict] = []
    exclusion_rows: list[dict] = []
    diagnostics: list[dict] = []
    power_processes = sorted(
        process for process, metadata in process_map.items()
        if str(metadata.get("process_sector", "")).strip().casefold() == "power"
        and not topology[topology["process"].eq(process)].empty
    )
    for process in power_processes:
        metadata = process_map[process]
        links = topology[topology["process"].eq(process)]
        inputs = set(links.loc[links["direction"].eq("IN"), "commodity"])
        outputs = set(links.loc[links["direction"].eq("OUT"), "commodity"])
        subsector = str(metadata.get("process_subsector", "")).strip().casefold()
        role: str | None = None
        evidence = ""
        if subsector == "electricity storage":
            if primary in inputs and primary in outputs:
                role, evidence = "Storage", "primary electricity IN and OUT"
            else:
                evidence = "Storage label without both primary electricity IN and OUT"
        elif subsector == "interconnectors":
            is_import = primary in outputs and bool(inputs - {primary})
            is_export = primary in inputs and bool(outputs - {primary})
            if is_import ^ is_export:
                role = "Interconnector"
                evidence = "primary electricity OUT (import)" if is_import else "primary electricity IN (export)"
            else:
                evidence = "Interconnector label without one unambiguous primary-bus crossing"
        elif subsector == "electricity network":
            if primary in inputs or primary in outputs:
                role, evidence = "Network", "primary electricity network connection"
            else:
                evidence = "Network label without a primary electricity connection"
        elif subsector == "electricity generation":
            if primary in outputs and primary not in inputs:
                role, evidence = "Generation", "primary electricity OUT"
            else:
                evidence = "Generation label without a one-way primary electricity output"
        else:
            evidence = "Current Power label does not identify one of the four approved roles"

        if role is None:
            exclusion_rows.append({
                "process": process,
                "reason": "unclassified_or_ambiguous_power_process",
                "detail": evidence,
                **{column: metadata.get(column, "") for column in _POWER_SCOPE_COLUMNS[3:]},
            })
            diagnostics.append({
                "diagnostic_type": "excluded_power_process",
                "region": "",
                "process": process,
                "commodity": primary,
                "detail": evidence,
            })
            continue
        role_sets[role].add(process)
        scope_rows.append({
            "process": process,
            "role": role,
            "structural_evidence": evidence,
            **{column: metadata.get(column, "") for column in _POWER_SCOPE_COLUMNS[3:]},
        })
    return (
        {role: frozenset(values) for role, values in role_sets.items()},
        pd.DataFrame(scope_rows, columns=_POWER_SCOPE_COLUMNS),
        pd.DataFrame(exclusion_rows, columns=_POWER_EXCLUSION_COLUMNS),
        diagnostics,
    )


def _power_balance(
    flows: pd.DataFrame,
    *,
    primary: str,
    role_processes: dict[str, frozenset[str]],
    commodity_map: dict[str, pd.Series],
) -> tuple[pd.DataFrame, list[dict]]:
    """Calculate net supplied electricity from explicit primary-bus crossings."""
    diagnostics: list[dict] = []
    periods = sorted(pd.to_numeric(flows.get("period"), errors="coerce").dropna().astype(int).unique())
    if not periods:
        return _empty_frame(_POWER_BALANCE_COLUMNS), diagnostics
    primary_flows = flows[flows["commodity"].eq(primary)].copy()
    non_pj = primary_flows[primary_flows["unit"].fillna("").str.upper().ne("PJ")]
    incompatible_periods = set(non_pj["period"].astype(int))
    if not non_pj.empty:
        diagnostics.append({
            "diagnostic_type": "incompatible_primary_electricity_unit",
            "region": "",
            "process": "",
            "commodity": primary,
            "detail": ", ".join(sorted(non_pj["unit"].fillna("").unique())),
        })
    primary_flows = primary_flows[primary_flows["unit"].fillna("").str.upper().eq("PJ")]
    final_electricity = {
        commodity for commodity, metadata in commodity_map.items()
        if _is_electricity(metadata)
        and not _is_surplus_electricity(metadata)
        and FINAL_CONSUMPTION_MARKER in str(metadata.get("commodity_function", "")).casefold()
    }
    rows = []
    for period in periods:
        selected = primary_flows[primary_flows["period"].eq(period)]
        def total(role: str, direction: str) -> float:
            return float(selected[
                selected["process"].isin(role_processes[role])
                & selected["direction"].eq(direction)
            ]["value"].sum())
        generation = total("Generation", "OUT")
        imports = total("Interconnector", "OUT")
        discharge = total("Storage", "OUT")
        charge = total("Storage", "IN")
        exports = total("Interconnector", "IN")
        # Losses only enter when their own observed primary-bus sink can be
        # structurally identified. Current validated roles do not infer losses
        # from generic network throughput, preventing double subtraction.
        explicit_losses = 0.0
        net_pj = generation + imports + discharge - charge - exports - explicit_losses
        final_twh = float(flows[
            flows["period"].eq(period)
            & flows["direction"].eq("IN")
            & flows["commodity"].isin(final_electricity)
            & flows["unit"].fillna("").str.upper().eq("PJ")
        ]["value"].sum()) / PJ_PER_TWH
        net_twh = net_pj / PJ_PER_TWH
        if period in incompatible_periods:
            calculation_status = "Incompatible unit"
        elif selected.empty:
            calculation_status = "Missing observed balance"
        elif net_twh <= 0:
            calculation_status = "Non-positive denominator"
        else:
            calculation_status = "Available"
        gap = net_twh - final_twh
        reconciliation_status = (
            "Unavailable" if not final_electricity else
            "Aligned" if abs(gap) <= max(0.01, abs(net_twh) * 0.01) else
            "Warning"
        )
        rows.append({
            "period": period,
            "Generation (PJ)": generation,
            "Imports (PJ)": imports,
            "Storage discharge (PJ)": discharge,
            "Storage charge (PJ)": charge,
            "Exports (PJ)": exports,
            "Explicit losses (PJ)": explicit_losses,
            "Net supplied (PJ)": net_pj,
            "Net supplied (TWh/yr)": net_twh,
            "Final consumption (TWh/yr)": final_twh,
            "Reconciliation gap (TWh/yr)": gap,
            "Reconciliation status": reconciliation_status,
            "Calculation status": calculation_status,
        })
    return pd.DataFrame(rows, columns=_POWER_BALANCE_COLUMNS), diagnostics


def build_power_scope(
    vdt: pd.DataFrame,
    process_labels: pd.DataFrame,
    commodity_labels: pd.DataFrame,
    annual_flows: AnnualFlowResult | None,
) -> PowerScopeResult:
    """Build four strict Power roles and a shared supplied-electricity balance.

    Labels validate a role, VDT proves its relevant structure, and normalized
    annual VD flows measure the balance.  No prefix or legacy-taxonomy fallback
    is used when the evidence is incomplete.
    """
    if annual_flows is None:
        return empty_power_scope("missing_annual_flows")
    topology = _normalise_topology(vdt)
    if topology.empty:
        return empty_power_scope()
    process_map, commodity_map = _power_metadata_maps(process_labels, commodity_labels)
    electricity = frozenset(
        code for code, metadata in commodity_map.items() if _is_electricity(metadata)
    )
    primary, diagnostics = _primary_electricity_commodity(topology, process_map, commodity_map)
    if primary is None:
        result = empty_power_scope("unresolved_primary_electricity_commodity")
        return PowerScopeResult(
            primary_electricity_commodity=None,
            electricity_commodities=electricity,
            role_processes=result.role_processes,
            process_scope=result.process_scope,
            balance=result.balance,
            exclusions=result.exclusions,
            diagnostics=pd.DataFrame(diagnostics, columns=_DIAGNOSTIC_COLUMNS),
        )
    role_processes, process_scope, exclusions, role_diagnostics = _power_process_roles(
        topology, process_map, primary
    )
    balance, balance_diagnostics = _power_balance(
        annual_flows.flows,
        primary=primary,
        role_processes=role_processes,
        commodity_map=commodity_map,
    )
    diagnostics.extend(role_diagnostics)
    diagnostics.extend(balance_diagnostics)
    diagnostics.append({
        "diagnostic_type": "primary_electricity_commodity",
        "region": "",
        "process": "",
        "commodity": primary,
        "detail": "Unique non-surplus electricity output of validated generation processes.",
    })
    return PowerScopeResult(
        primary_electricity_commodity=primary,
        electricity_commodities=electricity,
        role_processes=role_processes,
        process_scope=process_scope.sort_values(["role", "process"]).reset_index(drop=True),
        balance=balance,
        exclusions=exclusions,
        diagnostics=pd.DataFrame(diagnostics, columns=_DIAGNOSTIC_COLUMNS).drop_duplicates(),
    )
