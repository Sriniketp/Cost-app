"""
Data structures for pipeline configuration and results.

These are pure dataclasses with no Streamlit dependency, shared by
the Streamlit dashboard (``app.py``) and headless tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from cost_allocation.energy_scope import AnnualFlowResult, EnergyScopeResult, PowerScopeResult
from cost_allocation.vd_source import VdSourceProvenance


@dataclass
class RunConfig:
    """All inputs required to execute the cost-allocation pipeline.

    Parameters
    ----------
    scenario : str
        TIMES scenario name to analyse.
    run_id : str | None
        Optional RunLog identifier (used to extract DD files at the
        corresponding git commit).
    run_dd_list : list[str] | None
        Ordered DD file stems; determines scenario precedence when there is
        no ``run_id`` (with a ``run_id`` the list comes from the run's .RUN
        file at its commit).
    dd_dir : Path | None
        Local uktm-dds git repository. When *None* and ``run_id`` is set, the
        pipeline uses :func:`run_selector.configured_dd_dir` (the app's own
        mirror of the GitLab repo, or ``UKTM_DDS_DIR``) and fails loudly if
        it is unavailable.
    vd_source_provenance : VdSourceProvenance | None
        Optional immutable source identity and row evidence attached by the
        SQL reader. It is metadata only and never changes pipeline calculations.
    """

    scenario: str
    run_id: str | None = None
    run_dd_list: list[str] | None = None
    run_date: pd.Timestamp | None = None
    dd_dir: Path | None = None
    vd_source_provenance: VdSourceProvenance | None = None


@dataclass
class RunResult:
    """Derived / computed outputs from a single pipeline run.

    Contains only values *produced* by the pipeline — input parameters
    are kept in :class:`RunConfig` and downstream code receives the
    ``(config, result)`` pair.
    """

    run_id: str
    vd_df: pd.DataFrame
    costs: pd.DataFrame
    scenario: str
    periods: list[int]
    period_durations: dict[int, int]
    input_costs: dict[str, pd.DataFrame]
    scenario_order: dict[str, int]
    dd_dir: Path | None = None
    run_dd_list: list[str] | None = None
    missing_dd_files: list[str] = field(default_factory=list)
    effective_dd_inputs: pd.DataFrame = field(default_factory=pd.DataFrame)
    price_year: int | None = None
    run_date: pd.Timestamp | None = None
    vdt_path: Path | None = None
    vdt_topology: pd.DataFrame = field(default_factory=pd.DataFrame)
    energy_scope: EnergyScopeResult | None = None
    annual_flows: AnnualFlowResult | None = None
    power_scope: PowerScopeResult | None = None
    pipeline_timings: dict[str, float] = field(default_factory=dict)
    vd_source_provenance: VdSourceProvenance | None = None


@dataclass
class ReconciliationReport:
    """Output of :func:`pipeline.run_reconciliation`."""

    reconciliation_df: pd.DataFrame
    summary_df: pd.DataFrame
    timings: dict[str, float] = field(default_factory=dict)
