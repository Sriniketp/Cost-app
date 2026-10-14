"""
Cost coefficient extraction for the Streamlit app.

Implements the core logic from Cost_filtering_CB7_scenario_new.r:
- Scenario precedence (supplied by the caller from the run's .RUN DD list)
- Per-process/year override selection
- Linear interpolation + forward fill
- totcapex calculation from VAR_Ncap * NCAP_COST
"""

from __future__ import annotations

import pandas as pd

from cost_allocation.constants import MODEL_PERIODS


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out.columns = [str(c).strip() for c in out.columns]
    # Normalize common column variants
    rename = {}
    for c in out.columns:
        cl = c.lower()
        if cl in {"process", "name"}:
            rename[c] = "process"
        elif cl == "scenario":
            rename[c] = "scenario"
        elif cl == "attribute":
            rename[c] = "attribute"
    if rename:
        out = out.rename(columns=rename)
    return out


def _extract_coeff_from_base_data_long(
    base_df: pd.DataFrame,
    attr_name: str,
    scenario_order: dict[str, int],
    model_years: list[int] | None = None,
) -> pd.DataFrame:
    """
    Extract one coefficient table from base_data long format.

    Expected columns in base_data:
    - attribute, scenario, process, year, val_field

    Pipeline (matches R Cost_filtering_CB7_scenario_new.r):
    1. Filter to attribute rows with valid numeric years (drop '-', '0' etc.)
    2. Apply scenario precedence (highest-order scenario wins per process/year)
    3. Reindex to full model year grid
    4. Interpolate linearly between known years only
    5. Forward-fill (persist flat) past the last known year

    Important: if a process has only one explicit value (for example only 2010),
    that value is carried forward through later model years until a later override
    appears. This matches the standard model-data interpretation that a parameter
    remains in force until changed. This function itself does not back-fill
    before the first explicit year, but in the live DD pipeline its input has
    already been through ``dd_inputs._apply_times_interpolation``, whose default
    TIMES IE option 0 extrapolates backward — so late-start series arrive here
    already filled back to the first model year. That is intended: NCAP_COST is
    a model input, and only enters the objective once a build happens.
    """
    df = _normalize_columns(base_df)
    required = {"attribute", "scenario", "process", "year", "val_field"}
    if not required.issubset(set(df.columns)):
        return pd.DataFrame()

    df = df.copy()
    df["attribute"] = df["attribute"].astype(str).str.upper().str.strip()
    df["scenario"] = df["scenario"].astype(str).str.lower().str.strip()
    df["process"] = df["process"].astype(str).str.strip()
    df["year"] = pd.to_numeric(df["year"], errors="coerce")
    df["val_field"] = pd.to_numeric(df["val_field"], errors="coerce")

    # Filter: correct attribute, valid year (>= 2000), valid value
    df = df[
        (df["attribute"] == attr_name.upper())
        & df["year"].notna()
        & (df["year"] >= 2000)
    ].copy()
    if df.empty:
        return pd.DataFrame()

    df["year"] = df["year"].astype(int)

    if scenario_order:
        df = df[df["scenario"].isin(scenario_order)].copy()
        if df.empty:
            return pd.DataFrame()
        df["_order"] = df["scenario"].map(scenario_order)
    else:
        scen = df["scenario"].dropna().unique().tolist()
        fallback = {s: i + 1 for i, s in enumerate(scen)}
        df["_order"] = df["scenario"].map(fallback)

    # Keep highest-precedence non-null value per process/year
    df["_rank"] = df["_order"].where(df["val_field"].notna(), 0)
    idx = df.groupby(["process", "year"], dropna=True)["_rank"].idxmax()
    chosen = df.loc[idx, ["process", "year", "val_field"]].copy()

    # Pivot to sparse wide table (only years that have data)
    sparse = chosen.pivot_table(index="process", columns="year", values="val_field", aggfunc="first")
    if sparse.empty:
        return pd.DataFrame()

    # Determine target year grid (model_years if provided, else union of data + standard periods)
    if model_years is None:
        model_years = list(MODEL_PERIODS)

    all_years = sorted(set(list(sparse.columns) + model_years))

    # Reindex to dense year grid -> creates NaN for missing years
    dense = sparse.reindex(columns=all_years)

    # Interpolate only inside known ranges, then persist the last explicit value
    # forward, keeping single-point inputs constant thereafter. No back-fill is
    # done here; any pre-first-year values come from upstream TIMES IE
    # extrapolation (see docstring).
    dense = dense.interpolate(axis=1, method="index", limit_area="inside")
    dense = dense.ffill(axis=1)

    # Keep only model years in output
    out_years = sorted(set(model_years) & set(dense.columns))
    wide = dense[out_years].copy()
    wide.columns = [str(int(c)) for c in wide.columns]
    wide.index.name = "Process"
    return wide


def compute_totcapex(
    capex_df: pd.DataFrame,
    vd_df: pd.DataFrame,
    scenario: str,
) -> pd.DataFrame:
    """Compute totcapex = VAR_Ncap * NCAP_COST per process/year."""
    if capex_df.empty:
        return pd.DataFrame()

    var_ncap = vd_df[
        (vd_df["Scenario"] == scenario)
        & (vd_df["Attribute"] == "VAR_Ncap")
    ].copy()

    if var_ncap.empty:
        return pd.DataFrame()

    var_ncap["Period"] = pd.to_numeric(var_ncap["Period"], errors="coerce")
    var_ncap = var_ncap.dropna(subset=["Period"]).copy()
    var_ncap["Period"] = var_ncap["Period"].astype(int).astype(str)

    ncap_wide = (
        var_ncap.groupby(["Process", "Period"], as_index=False)["Value"]
        .sum()
        .pivot_table(index="Process", columns="Period", values="Value", aggfunc="sum")
        .fillna(0)
    )

    common_years = sorted(set(capex_df.columns) & set(ncap_wide.columns), key=int)
    if not common_years:
        return pd.DataFrame()

    aligned_capex = capex_df.reindex(columns=common_years).fillna(0)
    aligned_ncap = ncap_wide.reindex(index=aligned_capex.index).reindex(columns=common_years).fillna(0)

    out = aligned_ncap * aligned_capex
    out.index.name = "Process"
    return out
