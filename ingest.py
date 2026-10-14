"""
Offline VD Parsing and Canonicalization
=======================================
Canonicalize result rows for production SQL ingestion and parse TIMES `.VD`
text files only for offline tests and parity diagnostics.

VD format (from GDX2VEDA):
    - Lines beginning with '*' are header/comment lines — skip them.
        - Legacy data lines have 9 quoted comma-separated fields:
                    Attribute, Commodity, Process, Period, Region, Vintage, TimeSlice,
                    UserConstraint, Value.
        - Current data lines may include ``Sow`` after Attribute. Independent SOW
            solutions must not be added together: the final numeric SOW is selected
            by default, or callers may request one explicitly.
        - '-' in a field means "not applicable".
        - The scenario name is taken from the filename stem.

The dashboard and CLI do not call ``load_vd_files``.
"""

from __future__ import annotations

import csv
import hashlib
import math
import re
import warnings
from numbers import Integral, Real
from pathlib import Path
from typing import Iterable

import pandas as pd

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VD_COLUMNS = [
    "Attribute",
    "Commodity",
    "Process",
    "Period",
    "Region",
    "Vintage",
    "TimeSlice",
    "UserConstraint",
    "Value",
]

PARSED_VD_COLUMNS = VD_COLUMNS[:1] + ["Sow"] + VD_COLUMNS[1:]
CANONICAL_VD_COLUMNS = PARSED_VD_COLUMNS + ["Scenario"]

ESSENTIAL_ATTRIBUTES = [
    "Cost_Inv",
    "Cost_Fom",
    "Cost_Act",
    "Cost_Flo",
    "VAR_FIn",
    "VAR_FOut",
    "Reg_ACost",
    "ObjZ",
    "Time_NPV",
]

COST_ATTRIBUTES = [
    "Cost_Inv",
    "Cost_Fom",
    "Cost_Act",
    "Cost_Flo",
    "Cost_Com",
    "Cost_Salv",
    "Reg_ACost",
]


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def _extract_scenario_name(path: Path, header_lines: list[str]) -> str:
    """Extract scenario name from VD header or filename."""
    for line in header_lines:
        m = re.search(r"ImportID-\s*Scenario:(.+)", line)
        if m:
            return m.group(1).strip()
    return path.stem


def _sow_sort_key(sow: str) -> tuple[bool, int | str]:
    """Sort numeric SOW identifiers naturally, after any named identifiers."""
    return (sow.isdigit(), int(sow) if sow.isdigit() else sow.casefold())


def _canonical_text(value: object) -> str | None:
    """Return one canonical nullable VD text value without changing its case."""
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, Integral) and not isinstance(value, bool):
        return str(int(value))
    if isinstance(value, Real) and not isinstance(value, bool):
        numeric = float(value)
        if math.isfinite(numeric) and numeric.is_integer():
            return str(int(numeric))
    text = str(value).strip()
    if not text or text == "-":
        return None
    return text


def _normalise_requested_sow(sow: str | int | None) -> str | None:
    if sow is None:
        return None
    return _canonical_text(sow)


def _select_vd_sow(
    frame: pd.DataFrame,
    *,
    sow: str | int | None,
    source_name: str,
) -> pd.DataFrame:
    """Select one independent SOW and retain global rows without a SOW."""
    available_sows = sorted(
        set(frame["Sow"].dropna().astype(str)),
        key=_sow_sort_key,
    )
    requested_sow = _normalise_requested_sow(sow)
    if requested_sow is not None and requested_sow not in available_sows:
        available = ", ".join(available_sows) or "none"
        raise ValueError(
            f"Requested SOW {requested_sow!r} is not present in {source_name}; "
            f"available SOWs: {available}."
        )
    selected_sow = requested_sow or (available_sows[-1] if available_sows else None)
    global_rows = frame[frame["Sow"].isna()].copy()
    if selected_sow is None:
        return global_rows.reset_index(drop=True)
    selected_rows = frame[frame["Sow"].eq(selected_sow)].copy()
    return pd.concat([selected_rows, global_rows], ignore_index=True)


def canonicalise_vd_frame(
    frame: pd.DataFrame,
    *,
    scenario: str,
    sow: str | int | None = None,
    source_name: str = "VD data",
) -> pd.DataFrame:
    """Validate and canonicalise source rows before any domain calculation.

    SQL and offline file readers must call this function so nullable text,
    periods, values, duplicate preservation, and SOW selection have one
    implementation. The input contains the ten fields in
    :data:`PARSED_VD_COLUMNS`; ``Scenario`` comes from authoritative source
    metadata rather than an individual result row.
    """
    missing = [column for column in PARSED_VD_COLUMNS if column not in frame.columns]
    if missing:
        raise ValueError(
            f"{source_name} is missing required VD columns: {', '.join(missing)}."
        )
    scenario_name = _canonical_text(scenario)
    if scenario_name is None:
        raise ValueError(f"{source_name} has no scenario identity.")

    canonical = frame.loc[:, PARSED_VD_COLUMNS].copy()
    text_columns = [
        column for column in PARSED_VD_COLUMNS
        if column not in {"Period", "Value"}
    ]
    for column in text_columns:
        canonical[column] = canonical[column].map(_canonical_text)

    blank_attributes = canonical["Attribute"].isna()
    if blank_attributes.any():
        raise ValueError(
            f"{source_name} contains {int(blank_attributes.sum())} row(s) without Attribute."
        )

    raw_periods = canonical["Period"].map(_canonical_text)
    periods = pd.to_numeric(raw_periods, errors="coerce")
    invalid_periods = raw_periods.notna() & periods.isna()
    fractional_periods = periods.notna() & periods.mod(1).ne(0)
    if invalid_periods.any() or fractional_periods.any():
        count = int((invalid_periods | fractional_periods).sum())
        raise ValueError(f"{source_name} contains {count} invalid or fractional Period value(s).")
    canonical["Period"] = periods.astype("Int64")

    values = pd.to_numeric(canonical["Value"], errors="coerce")
    invalid_values = values.isna() | ~values.map(
        lambda value: math.isfinite(float(value)) if pd.notna(value) else False
    )
    if invalid_values.any():
        raise ValueError(
            f"{source_name} contains {int(invalid_values.sum())} missing or non-finite Value(s)."
        )
    canonical["Value"] = values.astype(float)
    canonical["Scenario"] = scenario_name
    return _select_vd_sow(
        canonical,
        sow=sow,
        source_name=source_name,
    )


def canonical_vd_multiset_hash(frame: pd.DataFrame) -> str:
    """Hash canonical rows independent of order but sensitive to duplicates.

    Each field is length-prefixed before hashing, so nulls and text cannot
    collide through delimiters. Floating-point values use their exact Python
    hexadecimal representation. Sorting row digests removes source row-order
    differences while retaining one digest for every duplicate row.
    """
    missing = [column for column in CANONICAL_VD_COLUMNS if column not in frame.columns]
    if missing:
        raise ValueError(
            "Canonical VD data is missing columns required for hashing: "
            + ", ".join(missing)
            + "."
        )

    row_digests: list[bytes] = []
    for values in frame.loc[:, CANONICAL_VD_COLUMNS].itertuples(index=False, name=None):
        row_hash = hashlib.sha256()
        for column, value in zip(CANONICAL_VD_COLUMNS, values):
            if pd.isna(value):
                encoded = b"<NULL>"
            elif column == "Value":
                encoded = float(value).hex().encode("ascii")
            elif column == "Period":
                encoded = str(int(value)).encode("ascii")
            else:
                encoded = str(value).encode("utf-8")
            row_hash.update(len(encoded).to_bytes(8, "big"))
            row_hash.update(encoded)
        row_digests.append(row_hash.digest())

    multiset_hash = hashlib.sha256()
    multiset_hash.update(b"vd-canonical-multiset-v1\0")
    multiset_hash.update(len(row_digests).to_bytes(8, "big"))
    for digest in sorted(row_digests):
        multiset_hash.update(digest)
    return multiset_hash.hexdigest()


def _parse_vd_file(
    path: Path,
    *,
    sow: str | int | None = None,
) -> tuple[str, pd.DataFrame]:
    """Parse one VD file and select one independent SOW solution.

    Current multi-objective exports use ascending numeric SOW identifiers for
    successive solutions.  The final (highest) numeric SOW is the dashboard
    default, matching Vistool's final-solution presentation.  Rows without a
    SOW, such as the global ``ObjZ`` record, are retained with every selection.
    Legacy nine-field VD files have no SOW and are loaded unchanged.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"VD file not found: {path}")

    header_lines: list[str] = []
    data_rows: list[list] = []

    with path.open("r", encoding="utf-8", errors="replace") as fh:
        reader = csv.reader(fh, quotechar='"', skipinitialspace=True)
        for raw_row in reader:
            # raw_row is already split by csv.reader — but we need to check
            # if the original line started with '*' (comment/header).
            # csv.reader gives us the parsed fields; we need the first field.
            if not raw_row:
                continue
            first = raw_row[0].strip()
            if first.startswith("*"):
                header_lines.append(first)
                continue
            if len(raw_row) < 9:
                continue  # skip malformed rows
            # Current GDX2VEDA files add Sow immediately after Attribute.
            if len(raw_row) >= 10:
                raw_sow = raw_row[1].strip()
                row_sow = raw_sow if raw_sow and raw_sow != "-" else None
                fields = raw_row[:1] + raw_row[2:10]
            else:
                row_sow = None
                fields = raw_row[:9]
            try:
                value = float(fields[8])
            except ValueError:
                continue
            row = [f.strip() if f.strip() != "-" else None for f in fields[:8]]
            row.append(value)
            row.insert(1, row_sow)
            data_rows.append(row)

    scenario = _extract_scenario_name(path, header_lines)
    df = pd.DataFrame(data_rows, columns=PARSED_VD_COLUMNS)
    return scenario, canonicalise_vd_frame(
        df,
        scenario=scenario,
        sow=sow,
        source_name=path.name,
    )


def load_vd_files(
    paths: str | Path | Iterable[str | Path],
    verbose: bool = True,
    *,
    sow: str | int | None = None,
) -> pd.DataFrame:
    """
    Load one or more VD files and return a combined DataFrame.

    Parameters
    ----------
    paths : one path or list of paths to .VD files
    verbose : print summary statistics
    sow : optional SOW identifier
        Select this SOW from every current-format file. By default, select the
        final numeric SOW independently from each file.

    Returns
    -------
    pd.DataFrame with columns:
        Scenario, Attribute, Sow, Commodity, Process, Period, Region,
        Vintage, TimeSlice, UserConstraint, Value
    """
    if isinstance(paths, (str, Path)):
        paths = [paths]

    frames: list[pd.DataFrame] = []
    scenarios: list[str] = []

    for p in paths:
        scenario, df = _parse_vd_file(Path(p), sow=sow)
        frames.append(df)
        scenarios.append(scenario)
        if verbose:
            print(f"  Loaded '{scenario}' from {Path(p).name}: {len(df):,} rows")

    if not frames:
        raise ValueError("No VD files loaded.")

    combined = pd.concat(frames, ignore_index=True)

    if verbose:
        _print_summary(combined, scenarios)

    return combined


def _print_summary(df: pd.DataFrame, scenarios: list[str]) -> None:
    """Print ingestion summary statistics."""
    print("\n" + "=" * 60)
    print("VD FILE INGESTION SUMMARY")
    print("=" * 60)
    print(f"  Scenarios loaded  : {len(scenarios)}")
    for s in scenarios:
        n = (df["Scenario"] == s).sum()
        print(f"    • {s}: {n:,} rows")

    periods = sorted(df["Period"].dropna().unique().tolist())
    print(f"  Period range      : {periods[0]} – {periods[-1]}" if periods else "  Periods: none")
    print(f"  Unique processes  : {df['Process'].nunique():,}")
    print(f"  Unique commodities: {df['Commodity'].nunique():,}")
    print(f"  Unique attributes : {df['Attribute'].nunique():,}")
    print(f"  Total rows        : {len(df):,}")

    present = set(df["Attribute"].unique())
    missing = [a for a in ESSENTIAL_ATTRIBUTES if a not in present]
    if missing:
        warnings.warn(
            f"\n  WARNING: Missing essential attributes: {missing}\n"
            "  Some allocation calculations may be incomplete.",
            stacklevel=2,
        )
    else:
        print(f"  Essential attrs   : All {len(ESSENTIAL_ATTRIBUTES)} present [OK]")
    print("=" * 60 + "\n")


# ---------------------------------------------------------------------------
# Convenience accessors
# ---------------------------------------------------------------------------

def get_periods(df: pd.DataFrame, scenario: str | None = None) -> list[int]:
    """Return sorted list of model years present in df."""
    mask = df["Period"].notna()
    if scenario:
        mask &= df["Scenario"] == scenario
    return sorted(df.loc[mask, "Period"].dropna().unique().astype(int).tolist())
