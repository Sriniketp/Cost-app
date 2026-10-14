"""Shared Streamlit-session access to prepared resource-allocation results.

This module keeps the allocation calculation and cache identity consistent across
all user interfaces that present attributed fuel costs.  It deliberately does
not implement allocation mathematics; that remains in ``resource_allocation``.
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from cost_allocation.helpers import frame_fingerprint
from cost_allocation.resource_allocation import (
    VINTAGE_ALLOCATION_SCHEMA_VERSION,
    normalise_resource_views,
    prepare_resource_allocation,
)
from cost_allocation.resource_rules import resource_pools_fingerprint


def allocation_result(
    bundle,
    commodity_labels: pd.DataFrame,
    *,
    views: tuple[str, ...] | list[str],
    commodity_label_fingerprint: tuple[str, int, int] | None = None,
):
    """Return the selected run's cached allocation result for exactly ``views``.

    A missing VDT-derived scope or normalized annual flow result is treated as
    unavailable evidence rather than being approximated from process names.
    """
    scope = getattr(bundle, "energy_scope", None)
    annual = getattr(bundle, "annual_flows", None)
    if scope is None or annual is None:
        return None
    selected_views = normalise_resource_views(views)
    cache = st.session_state.setdefault("_resource_allocation_results", {})
    cache_key = (
        str(bundle.run_id),
        frame_fingerprint(bundle.costs),
        frame_fingerprint(annual.flows),
        commodity_label_fingerprint
        if commodity_label_fingerprint is not None
        else frame_fingerprint(commodity_labels),
        tuple(int(period) for period in bundle.periods),
        selected_views,
        resource_pools_fingerprint(),
        VINTAGE_ALLOCATION_SCHEMA_VERSION,
    )
    if cache_key in cache:
        return cache[cache_key]
    result = prepare_resource_allocation(
        bundle.costs,
        annual.flows,
        commodity_labels=commodity_labels,
        supply_processes=scope.supply_processes,
        periods=bundle.periods,
        views=selected_views,
    )
    cache[cache_key] = result
    while len(cache) > 8:
        cache.pop(next(iter(cache)))
    return result
