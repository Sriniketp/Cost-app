"""Lightweight run presentation state used before analysis modules load.

This module deliberately has no Streamlit or Plotly dependency so the
selector-only stage can start without loading chart packages.
"""

from __future__ import annotations

from cost_allocation.colours import DESNZ_COLOURS, desnz_colour

APP_BUILD = "recon-v2-2026-05-18"

_RUN_COLOURS = DESNZ_COLOURS


def _run_colour(idx: int) -> str:
    """Return a consistent chart colour for a zero-based run index."""
    return desnz_colour(idx)


class RunBundle:
    """UI-facing copy of a prepared ``RunResult`` with a display colour."""

    __slots__ = (
        "run_id", "vd_df", "costs", "scenario", "periods",
        "period_durations", "input_costs", "scenario_order",
        "reference_data", "dd_dir", "run_dd_list", "vdt_path",
        "vdt_topology", "energy_scope", "annual_flows", "power_scope",
        "pipeline_timings", "price_year", "run_date", "vd_source_provenance",
        "missing_dd_files", "colour",
    )

    def __init__(
        self,
        _result,
        reference_data=None,
        dd_dir=None,
        run_dd_list=None,
        colour=DESNZ_COLOURS[0],
    ):
        self.run_id = _result.run_id
        self.vd_df = _result.vd_df
        self.costs = _result.costs
        self.scenario = _result.scenario
        self.periods = _result.periods
        self.period_durations = _result.period_durations
        self.input_costs = _result.input_costs
        self.scenario_order = _result.scenario_order
        self.reference_data = reference_data
        self.dd_dir = dd_dir
        self.run_dd_list = run_dd_list
        self.price_year = getattr(_result, "price_year", None)
        self.run_date = getattr(_result, "run_date", None)
        self.vdt_path = _result.vdt_path
        self.vdt_topology = _result.vdt_topology
        self.energy_scope = getattr(_result, "energy_scope", None)
        self.annual_flows = getattr(_result, "annual_flows", None)
        self.power_scope = getattr(_result, "power_scope", None)
        self.pipeline_timings = getattr(_result, "pipeline_timings", {})
        self.vd_source_provenance = getattr(_result, "vd_source_provenance", None)
        self.missing_dd_files = list(getattr(_result, "missing_dd_files", []))
        self.colour = colour
