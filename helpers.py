"""
Pure helper functions extracted from app.py.

These are substantial, testable functions that have no Streamlit
dependency and are shared by the pipeline, CLI, and dashboard.
"""

from __future__ import annotations

import weakref

import pandas as pd

from cost_allocation.labels import PROCESS_LABEL_COLUMNS
from cost_allocation.text_norm import normalise_code


# ---------------------------------------------------------------------------
# Cache identity
# ---------------------------------------------------------------------------

_FINGERPRINTS: dict[int, tuple[weakref.ReferenceType, tuple]] = {}


def frame_fingerprint(frame: pd.DataFrame | None) -> tuple:
    """Return a content fingerprint usable as part of a session cache key.

    ``id()`` cannot be used for this. A frame rebuilt with identical content
    gets a different id, and — worse — a freed frame's address can be reused by
    an unrelated frame, which would serve a cached result belonging to
    different data.

    Hashing every row costs roughly 0.15s per 500k rows, far too slow to repeat
    on every Streamlit rerun, so the answer is memoised per frame object. The
    weak reference check makes that memo safe against the same address reuse:
    a recycled id cannot match a live frame that was never fingerprinted. The
    memo does assume loaded run frames are not mutated in place, which is how
    ``ingest``/``vdt`` build them.
    """
    if frame is None:
        return ("none",)
    cached = _FINGERPRINTS.get(id(frame))
    if cached is not None and cached[0]() is frame:
        return cached[1]
    fingerprint = (
        frame.shape,
        tuple(str(column) for column in frame.columns),
        int(pd.util.hash_pandas_object(frame, index=True).sum()),
    )
    _FINGERPRINTS[id(frame)] = (weakref.ref(frame), fingerprint)
    while len(_FINGERPRINTS) > 16:
        _FINGERPRINTS.pop(next(iter(_FINGERPRINTS)))
    return fingerprint


# ---------------------------------------------------------------------------
# Cost-table builder
# ---------------------------------------------------------------------------

# Canonical cost attribute lists — define what the pipeline processes.
COST_ATTRS = [
    "Cost_Inv", "Cost_Fom", "Cost_Act", "Cost_Flo",
    "Cost_Com", "Cost_Salv",
]

ALL_COST_ATTRS = COST_ATTRS + [
    "Cost_ire",   # Implied endogenous trade costs
]


def build_cost_table(
    df: pd.DataFrame,
    process_labels: pd.DataFrame,
    scenario: str,
    *,
    commodity_labels: pd.DataFrame,
) -> pd.DataFrame:
    """Build the per-row cost table for *scenario*.

    Replicates the vistool cost pipeline:

    1. Filter to cost attributes + ObjZ + EQ_CombalM (shadow price).
    2. Join the authoritative process taxonomy on ``Process``.
    3. Apply vistool sector-override rules, read from the label fields
       (process sector ``Dummy Imports``; commodity type ``Air quality``),
       never from the spelling of a code.
    4. Keep per-row granularity for drill-down.
    """
    sc_df = df[df["Scenario"] == scenario].copy()

    cost_mask = sc_df["Attribute"].isin(
        ALL_COST_ATTRS + [
            "ObjZ", "EQ_CombalM", "Reg_ACost", "Time_NPV",
            "Reg_obj", "Reg_WOBJ",
        ]
    )
    costs = sc_df[cost_mask].copy()

    label_columns = [column for column in process_labels.columns if column != "_label_key"]
    if not process_labels.empty and {"process", "_label_key"}.issubset(process_labels.columns):
        label_map = process_labels.loc[:, label_columns + ["_label_key"]].copy()
        costs["_label_key"] = normalise_code(costs["Process"])
        costs = costs.merge(label_map, on="_label_key", how="left")
        costs = costs.drop(columns="_label_key")
    else:
        for column in PROCESS_LABEL_COLUMNS:
            costs[column] = ""

    # --- Vistool sector override rules ---
    costs["process_sector"] = costs["process_sector"].fillna("")

    # DUMMY IMPORTS: the process label's sector says so.
    mask_dummy = costs["process_sector"].eq("Dummy Imports")
    costs.loc[mask_dummy, "process_sector"] = "_DUMMY IMPORTS"

    # Air quality damage costs: Cost_Com on a commodity labelled Air quality.
    air_quality_codes: set[str] = set()
    if not commodity_labels.empty:
        air_quality_codes = set(
            normalise_code(commodity_labels["commodity"])[
                commodity_labels["commodity_type"].eq("Air quality")
            ]
        )
    mask_aq = (
        costs["Attribute"].eq("Cost_Com")
        & normalise_code(costs["Commodity"]).isin(air_quality_codes)
    )
    costs.loc[mask_aq, "process_sector"] = "_Air quality damage costs"

    # ObjZ
    costs.loc[costs["Attribute"] == "ObjZ", "process_sector"] = (
        "_Total system objective"
    )

    # Shadow price
    costs.loc[costs["Attribute"] == "EQ_CombalM", "process_sector"] = (
        "_Shadow price"
    )

    # Missing sector
    costs.loc[costs["process_sector"] == "", "process_sector"] = "_Missing sector!"

    return costs


# ---------------------------------------------------------------------------
# Period-duration computation (OBLONG contiguity)
# ---------------------------------------------------------------------------

def compute_period_durations(periods: list[int]) -> dict[int, int]:
    """Compute D(t) for OBLONG-contiguous periods from milestone years.

    Under ``$SET OBLONG YES``, period boundaries are::

        B(t) = M(t) - FLOOR(D(t) / 2)
        E(t) = B(t) + D(t) = M(t) + CEIL(D(t) / 2)

    Contiguity requires ``E(t) = B(t+1)`` for every consecutive pair.
    The algorithm works backwards from the last regular period (whose D
    equals the preceding gap) and solves for each ``D(t)``.
    """
    if not periods:
        return {}

    ps = sorted(periods)

    # Detect far-future bookkeeping markers (e.g. 2200) that are not real
    # optimisation milestones.  Only trim when the tail gap is both very
    # large in absolute terms and an outlier relative to the prior gap.
    terminal_marker: int | None = None
    if len(ps) >= 3:
        prev_gap = ps[-2] - ps[-3]
        tail_gap = ps[-1] - ps[-2]
        if prev_gap > 0 and tail_gap >= 50 and tail_gap >= 3 * prev_gap:
            terminal_marker = ps[-1]
            ps = ps[:-1]

    n = len(ps)
    durations: dict[int, int] = {}

    if n >= 2:
        durations[ps[-1]] = ps[-1] - ps[-2]
    else:
        durations[ps[0]] = 1
        if terminal_marker is not None:
            durations[terminal_marker] = 1
        return durations

    for i in range(n - 2, -1, -1):
        m = ps[i]
        d_next = durations[ps[i + 1]]
        b_next = ps[i + 1] - d_next // 2
        e_t = b_next

        c = e_t - m
        if c <= 0:
            durations[m] = ps[i + 1] - m if i < n - 1 else 1
            continue

        d_even = 2 * c
        d_odd = 2 * c - 1
        gap = ps[i + 1] - m if i < n - 1 else d_odd
        durations[m] = d_odd if d_odd <= gap else d_even

    if terminal_marker is not None:
        durations[terminal_marker] = durations[ps[-1]]

    return durations
