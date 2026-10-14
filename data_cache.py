"""
Streamlit-cached data loading functions.

Extracted from app.py to allow tab modules to import without
circular dependencies.  All functions use @st.cache_data for
efficient reuse across Streamlit reruns.
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from cost_allocation.sql_connection import SqlSettings
from cost_allocation.vd_source import VD_QUERY_CONTRACT_VERSION, VdSourceProvenance
from cost_allocation.vd_sql import RAW_VD_SCHEMA_VERSION, load_sql_vd
from cost_allocation.run_selector import load_runlog, load_run_groups
from cost_allocation.helpers import frame_fingerprint
from cost_allocation.labels import (
    label_source_fingerprints,
    load_commodity_labels,
    load_process_labels,
)


@st.cache_data(show_spinner="Reading VD data from SQL...", max_entries=12)
def cached_load_sql_vd(
    run_id: str,
    sql_identity: tuple[str, ...],
) -> tuple[pd.DataFrame, VdSourceProvenance]:
    """Load one immutable SQL run, keyed by safe source identity."""
    del sql_identity
    return load_sql_vd(run_id)


@st.cache_data(show_spinner="Loading RunLog...", ttl="5m", max_entries=2)
def cached_load_runlog(sql_identity: tuple[str, ...]) -> pd.DataFrame:
    del sql_identity
    return load_runlog()


@st.cache_data(show_spinner="Loading RunGroups...", ttl="5m", max_entries=2)
def cached_load_run_groups(sql_identity: tuple[str, ...]) -> pd.DataFrame:
    del sql_identity
    return load_run_groups()


def current_sql_cache_identity() -> tuple[str, ...]:
    """Return the configured database identity without credentials or server."""
    return (
        *SqlSettings.from_environment().cache_identity(),
        RAW_VD_SCHEMA_VERSION,
        VD_QUERY_CONTRACT_VERSION,
    )


@st.cache_data(show_spinner=False, ttl="5m", max_entries=2)
def cached_label_source_fingerprints(
    sql_identity: tuple[str, ...],
) -> tuple[tuple[str, int, int], ...]:
    """Return SQL label-table fingerprints, re-checked at most every five minutes."""
    del sql_identity
    return label_source_fingerprints()


@st.cache_data(show_spinner="Loading authoritative process labels...", max_entries=2)
def cached_load_process_labels(
    source_fingerprint: tuple[str, int, int],
) -> pd.DataFrame:
    """Load the current process taxonomy, invalidated by source metadata."""
    del source_fingerprint
    return load_process_labels()


@st.cache_data(show_spinner="Loading authoritative commodity labels...", max_entries=2)
def cached_load_commodity_labels(
    source_fingerprint: tuple[str, int, int],
) -> pd.DataFrame:
    """Load the current commodity taxonomy, invalidated by source metadata."""
    del source_fingerprint
    return load_commodity_labels()


def reconciliation_inputs_identity(
    vd: pd.DataFrame | None,
    input_costs: dict[str, pd.DataFrame] | None,
    reference_data=None,
) -> tuple:
    """Content identity for :func:`cached_reconciliation_results` inputs.

    ``frame_fingerprint`` is memoised per frame object, so for a loaded run's
    persistent frames this is cheap on every rerun after the first, unlike
    Streamlit hashing the whole VD frame each time. Reference data carries
    the content identity of the DD table it was built from.
    """
    costs_identity = tuple(
        (name, frame_fingerprint(frame))
        for name, frame in sorted((input_costs or {}).items())
    )
    reference_identity = None if reference_data is None else reference_data.source_identity
    return (frame_fingerprint(vd), costs_identity, reference_identity)


@st.cache_data(show_spinner="Computing reconciliation...", max_entries=10)
def cached_reconciliation_results(
    _vd: pd.DataFrame,
    _input_costs: dict[str, pd.DataFrame],
    _reference_data,
    inputs_identity: tuple,
    selected_scenario: str | None,
    scenario_order_tuple: tuple[tuple[str, int], ...] | None,
    period_durations_items: tuple[tuple[int, int], ...],
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, float]]:
    """Reconcile one run.

    ``_vd``, ``_input_costs`` and ``_reference_data`` are not hashed by
    Streamlit; callers must pass
    ``reconciliation_inputs_identity(_vd, _input_costs, _reference_data)`` as
    ``inputs_identity`` so the key still changes whenever their content does.
    The scenario filter is applied here, once per cache miss.
    """
    from cost_allocation.pipeline import run_reconciliation
    from cost_allocation.run_config import RunConfig, RunResult

    # Reconciliation must run on the selected scenario only; otherwise
    # combined source rows can duplicate process-period values across
    # scenarios and distort process-level divergence.
    recon_vd = _vd
    if (
        selected_scenario is not None
        and recon_vd is not None
        and not recon_vd.empty
        and "Scenario" in recon_vd.columns
    ):
        recon_vd = recon_vd[recon_vd["Scenario"] == selected_scenario].copy()

    scenario_order = dict(scenario_order_tuple) if scenario_order_tuple else None
    period_durations = dict(period_durations_items)
    result = RunResult(
        run_id="recon",
        vd_df=recon_vd,
        costs=pd.DataFrame(),
        scenario=selected_scenario or "",
        periods=sorted(period_durations.keys()),
        period_durations=period_durations,
        input_costs=_input_costs,
        scenario_order=scenario_order or {},
    )
    config = RunConfig(scenario=result.scenario)
    report = run_reconciliation(result, config, reference_data=_reference_data)
    return report.reconciliation_df, report.summary_df, report.timings
