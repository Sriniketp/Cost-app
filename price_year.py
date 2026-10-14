"""Price-basis metadata and display labels for monetary model outputs."""

from __future__ import annotations

import math

import pandas as pd


def price_year_from_dd_inputs(effective_dd_inputs: pd.DataFrame) -> int | None:
    """Return the effective ``G_DYEAR`` value, preferring system settings.

    ``G_DYEAR`` is the TIMES objective/reference-price year.  ``None`` is
    deliberately returned when the selected run has no effective DD evidence:
    the UI must not silently label an unknown price basis as a known year.
    """
    required = {"attribute", "val_field"}
    if effective_dd_inputs is None or effective_dd_inputs.empty:
        return None
    if not required.issubset(effective_dd_inputs.columns):
        return None

    rows = effective_dd_inputs.loc[
        effective_dd_inputs["attribute"].astype(str).str.strip().str.upper().eq("G_DYEAR")
    ].copy()
    if rows.empty:
        return None
    if "scenario" in rows.columns:
        scenario = rows["scenario"].astype(str).str.strip().str.upper()
        preferred = rows.loc[scenario.isin(("SYSSETTINGS", "BASE"))]
        if not preferred.empty:
            rows = preferred
    for value in rows["val_field"]:
        try:
            year = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(year) and year.is_integer() and 1800 <= year <= 3000:
            return int(year)
    return None


def money_unit(price_year: int | None, unit: str) -> str:
    """Format a monetary unit without asserting an unavailable price basis."""
    return f"{int(price_year)} {unit}" if price_year is not None else f"{unit} (price year unavailable)"


def bundle_money_unit(bundles: list, unit: str) -> str:
    """Return a safe common unit label for a single- or multi-run chart."""
    years = {getattr(bundle, "price_year", None) for bundle in bundles}
    if len(years) == 1:
        return money_unit(next(iter(years)), unit)
    return f"{unit} (price year varies by run)"


def readable_money(value: float, unit: str) -> str:
    """Format a £M amount for display, switching to £bn from £1bn upward.

    Display only: *value* stays in £ millions everywhere else.  A *unit*
    without ``£M`` (for example TWh) is left unchanged.
    """
    if "£M" in unit and abs(value) >= 1000:
        return f"{value / 1000:,.3f} {unit.replace('£M', '£bn')}"
    return f"{value:,.3f} {unit}"
