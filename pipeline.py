"""
Core pipeline orchestration — zero Streamlit imports.

Exposes two entry points consumed by the dashboard and tests:

* :func:`run_pipeline`        — load & classify (single scenario).
* :func:`run_reconciliation`  — reconcile Cost_Inv vs. DD input costs.
"""

from __future__ import annotations

import logging
from pathlib import Path
from time import perf_counter

import pandas as pd

from cost_allocation.dd_inputs import (
    load_effective_dd_inputs,
    build_input_costs_from_dd_effective,
)
from cost_allocation.price_year import price_year_from_dd_inputs
from cost_allocation.run_selector import (
    RunDdUnavailableError,
    configured_dd_dir,
    dd_list_to_scenario_order,
    extract_dd_files_at_commit,
    extract_vdt_file_at_commit,
)
from cost_allocation.labels import load_commodity_labels, load_process_labels
from cost_allocation.energy_scope import (
    attach_connection_roles,
    build_power_scope,
    build_energy_scope,
    empty_energy_scope,
    empty_power_scope,
    prepare_annual_flows,
)
from cost_allocation.vdt import VdtParseError, load_vdt
from cost_allocation.helpers import (
    build_cost_table,
    compute_period_durations,
    COST_ATTRS,
    ALL_COST_ATTRS,
)
from cost_allocation.run_config import (
    RunConfig,
    RunResult,
    ReconciliationReport,
)

log = logging.getLogger(__name__)

# Re-export constants so callers can ``from cost_allocation.pipeline import COST_ATTRS``
__all__ = [
    "run_pipeline",
    "run_reconciliation",
    "COST_ATTRS",
    "ALL_COST_ATTRS",
]

# ---------------------------------------------------------------------------
# Comparison attribute lists (used by multi-run views in app.py)
# ---------------------------------------------------------------------------

COMPARE_ATTRS = ["Cost_Inv", "Cost_Fom", "Cost_Act", "Cost_Flo"]
COMPARE_EXTENDED = COMPARE_ATTRS + ["Cost_Com", "Cost_Salv"]


# ---------------------------------------------------------------------------
# Authoritative process-label loader
# ---------------------------------------------------------------------------

def _load_process_labels() -> pd.DataFrame:
    """Load the live process taxonomy for non-dashboard callers."""
    return load_process_labels()


def _load_commodity_labels() -> pd.DataFrame:
    """Load the live commodity taxonomy for non-dashboard callers."""
    return load_commodity_labels()


# ---------------------------------------------------------------------------
# run_pipeline — load & classify a single scenario
# ---------------------------------------------------------------------------

def run_pipeline(
    config: RunConfig,
    *,
    vd_df: pd.DataFrame,
    process_labels: pd.DataFrame | None = None,
    commodity_labels: pd.DataFrame | None = None,
) -> RunResult:
    """Execute the data-loading pipeline for a single scenario.

    Stages (mirrors the former ``_load_run`` in ``app.py``):

    1. Load VD data.
    2. Load authoritative process labels.
    3. Resolve scenario order (from the run's .RUN DD list).
    4. Extract DD files (if *run_id* provided; raises if unavailable).
    5. Build DD-derived input-cost tables.
    6. Build cost table (classify + filter).
    7. Compute period durations.

    Returns a :class:`RunResult` containing only derived outputs.
    The caller retains the :class:`RunConfig` for input parameters.
    """
    pipeline_started_at = perf_counter()
    pipeline_timings: dict[str, float] = {}

    # 1. Accept VD rows prepared by the source adapter. The calculation layer
    # never opens files or database connections.
    stage_started_at = perf_counter()
    df = vd_df
    pipeline_timings["Load VD"] = perf_counter() - stage_started_at

    # 2. Current labels. Dashboard callers reuse their cached live frames.
    stage_started_at = perf_counter()
    labels = process_labels if process_labels is not None else _load_process_labels()
    commodity_metadata = (
        commodity_labels if commodity_labels is not None else _load_commodity_labels()
    )
    pipeline_timings["Load labels"] = perf_counter() - stage_started_at

    # 3. Scenario order. Precedence always comes from the run's .RUN file DD
    # list, which step 4 resolves at the run's commit. An explicit
    # config.run_dd_list is used when there is no run_id; with neither there
    # is no DD evidence to order.
    stage_started_at = perf_counter()
    run_dd_list = list(config.run_dd_list) if config.run_dd_list else None
    scenario_order: dict[str, int] = (
        dd_list_to_scenario_order(run_dd_list) if run_dd_list else {}
    )
    pipeline_timings["Resolve scenario order"] = perf_counter() - stage_started_at

    # 4. DD extraction (optional)
    dd_dir = config.dd_dir
    input_costs: dict[str, pd.DataFrame] = {
        "capex": pd.DataFrame(),
        "fom": pd.DataFrame(),
        "act_cost": pd.DataFrame(),
        "totcapex": pd.DataFrame(),
    }
    effective_dd_inputs = pd.DataFrame()
    missing_dd_files: list[str] = []
    vdt_path: Path | None = None
    vdt_topology = pd.DataFrame()
    energy_scope = empty_energy_scope()
    annual_flows = None
    power_scope = empty_power_scope()

    stage_started_at = perf_counter()
    if config.run_id:
        dd_repo = config.dd_dir if config.dd_dir is not None else configured_dd_dir()
        dd_dir = dd_repo
        commit_dd_dir, commit_dd_list = extract_dd_files_at_commit(
            config.run_id, dd_repo,
        )
        if commit_dd_dir is None or not commit_dd_list:
            # Every run is committed to uktm-dds with its .RUN file and DD
            # files. Continuing without them would show the run with empty
            # input costs and no reference data.
            raise RunDdUnavailableError(
                f"DD inputs for run {config.run_id!r} could not be resolved from "
                f"the uktm-dds repository ({dd_repo}): no matching commit, .RUN "
                "file or DD files were found."
            )
        dd_dir = commit_dd_dir
        run_dd_list = commit_dd_list
        missing_dd_files = [
            name for name in run_dd_list
            if not (Path(commit_dd_dir) / f"{name}.dd").is_file()
        ]
        if missing_dd_files:
            log.warning(
                "Run %s: DD files listed in the .RUN file are missing at the "
                "commit: %s",
                config.run_id,
                ", ".join(f"{name}.dd" for name in missing_dd_files),
            )
        scenario_order = dd_list_to_scenario_order(run_dd_list)
        vdt_path = extract_vdt_file_at_commit(config.run_id, dd_repo)
        if vdt_path is not None:
            try:
                vdt_topology = load_vdt(vdt_path)
            except VdtParseError:
                log.warning("Could not parse VDT for %s", config.run_id, exc_info=True)
    pipeline_timings["Extract DD and VDT"] = perf_counter() - stage_started_at

    # The energy boundary is structural and is prepared once per selected run.
    # There is deliberately no taxonomy-based fallback when its VDT is absent.
    stage_started_at = perf_counter()
    if not vdt_topology.empty:
        try:
            energy_scope = build_energy_scope(vdt_topology, labels, commodity_metadata)
            annual_flows = prepare_annual_flows(
                df,
                vdt_topology,
                labels,
                commodity_metadata,
                scenario=config.scenario,
                periods=None,
            )
            annual_flows = attach_connection_roles(annual_flows, energy_scope)
            power_scope = build_power_scope(
                vdt_topology,
                labels,
                commodity_metadata,
                annual_flows,
            )
        except ValueError:
            log.warning(
                "Could not prepare graph-derived energy data for %s",
                config.run_id or config.scenario,
                exc_info=True,
            )
    pipeline_timings["Prepare graph artifacts"] = perf_counter() - stage_started_at

    # 5. Input costs from DD. A parse or build failure is an error, not a
    # cue to continue with empty input costs.
    stage_started_at = perf_counter()
    if dd_dir is not None and Path(dd_dir).exists():
        effective_dd_inputs = load_effective_dd_inputs(
            Path(dd_dir),
            dd_file_list=run_dd_list or [],
        )
        if not effective_dd_inputs.empty:
            input_costs = build_input_costs_from_dd_effective(
                dd_effective_df=effective_dd_inputs,
                vd_df=df,
                scenario=config.scenario,
            )
    pipeline_timings["Load DD inputs"] = perf_counter() - stage_started_at

    # 6. Cost table
    stage_started_at = perf_counter()
    costs = build_cost_table(
        df, labels, config.scenario, commodity_labels=commodity_metadata,
    )
    all_periods = sorted(
        costs.loc[costs["Period"].notna(), "Period"]
        .unique()
        .astype(int)
        .tolist()
    )
    periods = [p for p in all_periods if p >= 2010]
    pipeline_timings["Build cost table"] = perf_counter() - stage_started_at

    # 7. Period durations
    stage_started_at = perf_counter()
    period_durations = compute_period_durations(periods)
    pipeline_timings["Compute period durations"] = perf_counter() - stage_started_at
    pipeline_timings["Pipeline total"] = perf_counter() - pipeline_started_at

    return RunResult(
        run_id=config.run_id or config.scenario,
        vd_df=df,
        costs=costs,
        scenario=config.scenario,
        periods=periods,
        period_durations=period_durations,
        input_costs=input_costs,
        scenario_order=scenario_order,
        dd_dir=dd_dir,
        run_dd_list=run_dd_list,
        missing_dd_files=missing_dd_files,
        effective_dd_inputs=effective_dd_inputs,
        price_year=price_year_from_dd_inputs(effective_dd_inputs),
        run_date=config.run_date,
        vdt_path=vdt_path,
        vdt_topology=vdt_topology,
        energy_scope=energy_scope,
        annual_flows=annual_flows,
        power_scope=power_scope,
        pipeline_timings=pipeline_timings,
        vd_source_provenance=config.vd_source_provenance,
    )


# ---------------------------------------------------------------------------
# run_reconciliation — reconcile Cost_Inv vs. DD inputs
# ---------------------------------------------------------------------------

def run_reconciliation(
    result: RunResult,
    config: RunConfig,
    *,
    reference_data=None,
) -> ReconciliationReport:
    """Reconcile VD ``Cost_Inv`` against DD-derived input costs.

    Wraps :func:`cost_backcalc.reconcile_cost_inv` and
    :func:`cost_backcalc.compute_reconciliation_summary` behind a clean,
    Streamlit-free entry point.

    Parameters
    ----------
    result : RunResult
        Output from :func:`run_pipeline`.
    config : RunConfig
        Pipeline configuration (provides ``dd_dir``, ``run_dd_list``).
    reference_data : ReferenceData | None
        Prepared reference data. If *None*, it is built in memory from
        ``result.effective_dd_inputs`` when that table is non-empty.
    """
    from cost_allocation.cost_backcalc import (
        ReferenceData,
        reconcile_cost_inv,
        compute_reconciliation_summary,
    )

    timings: dict[str, float] = {}
    started_at = perf_counter()
    ref_data = reference_data
    if ref_data is None and not result.effective_dd_inputs.empty:
        # The effective DD table is already precedence-resolved by
        # load_effective_dd_inputs and emitted single-scenario as "BASE", so
        # no scenario order is applied to it.
        ref_data = ReferenceData.from_frame(result.effective_dd_inputs)
    timings["Load reference data"] = perf_counter() - started_at

    started_at = perf_counter()
    reconciliation_df = reconcile_cost_inv(
        result.vd_df,
        result.input_costs,
        scenario=result.scenario,
        ref_data=ref_data,
        period_durations=result.period_durations,
    )
    timings["Reconcile investment costs"] = perf_counter() - started_at

    started_at = perf_counter()
    summary_df = compute_reconciliation_summary(reconciliation_df)
    timings["Summarise reconciliation"] = perf_counter() - started_at
    timings["Reconciliation total"] = sum(timings.values())

    return ReconciliationReport(
        reconciliation_df=reconciliation_df,
        summary_df=summary_df,
        timings=timings,
    )
