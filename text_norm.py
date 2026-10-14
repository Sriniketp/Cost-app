"""Canonical text normalisation for TIMES identifier codes.

Process, commodity, region, and direction codes arrive from VD, VDT, and the
label files with inconsistent casing, padding, and missing values. Comparing or
joining them requires one agreed normal form.

This module is deliberately dependency-free apart from pandas so that every
layer -- pure domain modules, I/O, and Streamlit tabs alike -- can import it
without acquiring unwanted dependencies.
"""

from __future__ import annotations

import pandas as pd


def normalise_code(values: pd.Series) -> pd.Series:
    """Return *values* as upper-case, whitespace-stripped strings.

    Missing values become empty strings rather than ``NaN`` so the result is
    always safe to compare, join, or test with ``.eq("")``.
    """
    return values.fillna("").astype(str).str.strip().str.upper()


def normalise_code_list(values: pd.Series) -> list[str]:
    """Return the distinct-order-preserving normalised codes, dropping blanks."""
    normalised = normalise_code(values)
    return normalised[normalised.ne("")].tolist()
