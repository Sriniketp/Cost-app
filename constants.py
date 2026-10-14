"""Model-wide constants shared across the calculation, export and UI layers.

This module is deliberately dependency-free so every layer can import it.
Each value below previously existed as a separate literal in two or more
modules, which is exactly the kind of duplication that drifts silently.
"""

from __future__ import annotations

# Standard UK TIMES milestone years. Used as the interpolation target grid for
# DD-derived coefficients and as the fallback period set when no run is loaded.
MODEL_PERIODS: tuple[int, ...] = (
    2010, 2011, 2012, 2015, 2020, 2025, 2030, 2035,
    2040, 2045, 2050, 2055, 2060,
)

# D(t) for the standard milestone set. A loaded run's own durations, derived by
# ``helpers.compute_period_durations``, always take precedence over this.
DEFAULT_PERIOD_DURATIONS: dict[int, int] = {
    2010: 1, 2011: 1, 2012: 1, 2015: 5, 2020: 5, 2025: 5, 2030: 5,
    2035: 5, 2040: 5, 2045: 5, 2050: 5, 2055: 5, 2060: 5,
}

# Energy unit conversion: one terawatt-hour is 3.6 petajoules.
PJ_PER_TWH: float = 3.6
TWH_PER_PJ: float = 1.0 / PJ_PER_TWH
